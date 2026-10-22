#!/usr/bin/env python3
"""Hermes pre_tool_call guard. Detection is local, fail-open, and never logs input."""

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import re
import sys


DEFAULT_LOG = "~/.hermes/profiles/koharu/logs/review-gate-hook.jsonl"
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$", re.S)
PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!"}
BOUNDARIES = {"fi", "done", "for", "case", "esac", "in"}
SSH_VALUES = set("oiplFJLRDSWEbcemOQwB")
CODEX_VALUES = {
    "-m", "--model", "-c", "--config", "-p", "--profile", "-C", "--cd",
    "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
    "--add-dir", "--oss-provider", "--local-provider", "--enable", "--disable",
    "--remote", "--remote-auth-token-env", "--thread-source", "--cyber-access-program",
    "-o", "--output-last-message", "--output-schema", "--color",
}
CLAUDE_VALUES = {
    "--model", "--permission-mode", "--output-format", "--input-format",
    "--system-prompt", "--system-prompt-file", "--append-system-prompt",
    "--append-system-prompt-file", "--settings", "--setting-sources", "--effort",
    "--fallback-model", "--max-budget-usd", "--session-id", "--json-schema",
    "--permission-prompts", "--plugin-dir", "--plugin-url", "-n", "--name",
    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
    "--tools", "--mcp-config", "--add-dir", "--agents", "--agent", "--betas",
}
SCRIPT_SUFFIXES = (".sh", ".zsh", ".bash", ".command")
TMUX_LAUNCH = {"new-session", "new", "new-window", "neww", "split-window", "splitw",
               "respawn-pane", "respawnp", "respawn-window", "respawnw"}
NPX_PACKAGES = {"@openai/codex": "codex", "@anthropic-ai/claude-code": "claude"}
DYNAMIC_REVIEWER = re.compile(r"(?i)codex|claude|magi")
DECLARATIONS = {"export", "local", "typeset", "declare", "readonly"}
CMUX_COMMAND = "--command"
REDIRECT = re.compile(r"(?:<<<|<<-|&>>?|[<>](?:[<>]|&|\|)?)")
# Runs that need no per-character decision; the tokenizer otherwise costs one Python step per byte.
PLAIN_RUN = re.compile(r"[^\s\\$`'\"#;&|()<>{}]+")
DOUBLE_QUOTED_RUN = re.compile(r'[^"\\$`]+')
WRITE_TOOL = re.compile(r"(?:^|[\s,])(Edit|Write|MultiEdit|NotebookEdit)(?=\(|[\s,]|$)")


class ParseFailure(ValueError):
    pass


@dataclass
class Word:
    value: str
    substitutions: list = field(default_factory=list)
    evaluated: str = None
    redirection: bool = False
    heredoc: str = None
    quoted_delimiter: bool = False


def heredoc_start(text, start):
    """Read a <<[-] delimiter with quote removal, without evaluating it."""
    strip_tabs = text.startswith("<<-", start)
    i = start + (3 if strip_tabs else 2)
    while i < len(text) and text[i] in " \t":
        i += 1
    value, quote, quoted, active = [], None, False, False
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            if i + 1 == len(text):
                raise ParseFailure("heredoc delimiter escape")
            quoted, active = True, True
            nxt = text[i + 1]
            if quote == '"' and nxt not in '$`"\\\n':
                value.append("\\")
            if nxt != "\n":
                value.append(nxt)
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                value.append(c)
        elif c in "'\"":
            quote, quoted, active = c, True, True
        elif c.isspace() or c in ";&|()<>":
            break
        else:
            value.append(c)
            active = True
        i += 1
    if quote or not active:
        raise ParseFailure("heredoc delimiter")
    return Word("".join(value), heredoc="", quoted_delimiter=quoted), strip_tabs, i


def heredoc_bodies(text, start, pending):
    """Attach bodies in declaration order and return the next command offset."""
    i = start
    for operand, strip_tabs in pending:
        body = []
        while i < len(text):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            line = text[i:end]
            if strip_tabs:
                line = line.lstrip("\t")
            i = end + (end < len(text))
            if line == operand.value:
                operand.heredoc = "".join(body)
                break
            body.append(line + ("\n" if end < len(text) else ""))
        else:
            raise ParseFailure("unterminated heredoc")
    pending.clear()
    return i


def substitution(text, start, backtick=False, nesting=0):
    """Return the raw body/end, retaining quote state through nested $()."""
    if nesting > 80:
        raise ParseFailure("nesting limit")
    i = start + (1 if backtick else 2)
    begin = i
    depth, quote = 1, None
    pending = []
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if backtick and c == "`":
            return text[begin:i], i + 1
        if quote is None and text.startswith("<<<", i):
            i += 3
            continue
        if quote is None and text.startswith("<<", i):
            operand, strip_tabs, i = heredoc_start(text, i)
            pending.append((operand, strip_tabs))
            continue
        if quote is None and c == "\n" and pending:
            i = heredoc_bodies(text, i + 1, pending)
            continue
        if c == quote:
            quote = None
        elif quote != "'" and text.startswith("$(", i):
            _, i = substitution(text, i, nesting=nesting + 1)
            continue
        elif quote is None:
            if c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return text[begin:i], i + 1
        i += 1
    raise ParseFailure("unterminated substitution")


def run_end(text, i, quote=None):
    """End of the run at i that needs no per-character decision (at least one character)."""
    if quote == "'":
        end = text.find("'", i)
        return len(text) if end < 0 else end
    run = (DOUBLE_QUOTED_RUN if quote else PLAIN_RUN).match(text, i)
    return run.end() if run else i + 1


def tokenize(text):
    """Split simple commands without discarding substitution evaluation context."""
    commands, command, value, subs, evaluated = [], [], [], [], []
    quote, active, i = None, False, 0
    pending = []

    def word():
        nonlocal value, subs, active, evaluated
        if active:
            command.append(Word("".join(value), subs, "".join(evaluated)))
        value, subs, active, evaluated = [], [], False, []

    def append(raw, expanded=None):
        value.append(raw)
        evaluated.append(raw if expanded is None else expanded)

    def boundary():
        word()
        if command:
            commands.append(command[:])
            command.clear()

    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            if i + 1 == len(text):
                raise ParseFailure("trailing escape")
            nxt = text[i + 1]
            if nxt != "\n":
                if quote == '"' and nxt not in '$`"\\':
                    append("\\")
                append(nxt)
                active = True
            i += 2
            continue
        if quote != "'" and (text.startswith("$(", i) or c == "`"):
            body, end = substitution(text, i, c == "`")
            append(text[i:end], "__substitution__")
            subs.append(body)
            active, i = True, end
            continue
        if quote:
            if c == quote:
                quote = None
                i += 1
            else:
                end = run_end(text, i, quote)
                append(text[i:end])
                i = end
            continue
        if c in "'\"":
            quote, active = c, True
        elif c == "#" and not active:
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        elif text.startswith("${", i):
            end = text.find("}", i + 2)
            if end < 0:
                raise ParseFailure("unterminated parameter")
            append(text[i:end + 1])
            active, i = True, end + 1
            continue
        elif c in "<>" or text.startswith("&>", i):
            if active and "".join(value).isdigit():
                value.clear()
                evaluated.clear()
                subs.clear()
                active = False
            word()
            operator = REDIRECT.match(text, i)[0]
            command.append(Word(operator, redirection=True))
            if operator in ("<<", "<<-"):
                operand, strip_tabs, i = heredoc_start(text, i)
                command.append(operand)
                pending.append((operand, strip_tabs))
                continue
            i += len(operator)
            continue
        elif c in ";&|\n()" or (c in "{}" and not active):
            boundary()
            if c == "\n" and pending:
                i = heredoc_bodies(text, i + 1, pending)
                continue
        elif c.isspace():
            word()
        else:
            end = run_end(text, i)
            append(text[i:end])
            active, i = True, end
            continue
        i += 1
    if quote:
        raise ParseFailure("unterminated quote")
    if pending:
        raise ParseFailure("unterminated heredoc")
    boundary()
    return commands


def options(argv, value_options, skip_positionals=False):
    """Yield (option, value); positional arguments stop option interpretation."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if not arg.startswith("-") or arg == "-":
            if skip_positionals:
                i += 1
                continue
            break
        key, equal, value = arg.partition("=")
        if key in value_options:
            if not equal:
                i += 1
                value = argv[i] if i < len(argv) else ""
        elif len(arg) > 2 and arg[:2] in value_options and not arg.startswith("--"):
            key, value = arg[:2], arg[2:]
        yield key, value
        i += 1


def subcommand(argv, value_options):
    consumed = 0
    for key, value in options(argv, value_options):
        arg = argv[consumed]
        consumed += 1
        if arg == key and key in value_options:
            consumed += 1
    return (argv[consumed] if consumed < len(argv) else None), consumed


def python_script(argv):
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return i + 1 if i + 1 < len(argv) else None
        if not arg.startswith("-"):
            return i
        if arg[:2] in ("-c", "-m"):
            return None
        i += 2 if arg in ("-W", "-X", "--check-hash-based-pycs") else 1
    return None


MAGI_INFO = {"--help", "-h", "--version", "--schema", "--examples"}
MAGI_UNGATED = {"lanes", "doctor", "report", "report-plan", "plan", "publish", "sample", "quality"}


def magi_reviewer(args):
    # Exactly the shim's scan: help-like options anywhere; first non-option is subcommand.
    if any(arg.split("=", 1)[0] in MAGI_INFO for arg in args):
        return None
    sub = next((arg for arg in args if not arg.startswith("-")), None)
    return None if sub is None or sub in MAGI_UNGATED else "magi"


def reviewer(argv):
    if not argv:
        return None
    program, args = os.path.basename(argv[0]), argv[1:]
    if program == "codex":
        if any(arg in ("-h", "--help", "-V", "--version") for arg in args):
            return None
        sub, index = subcommand(args, CODEX_VALUES)
        if sub == "review":
            return "codex"
        if sub not in ("exec", "e"):
            return None
        flags = list(options(args[:index], CODEX_VALUES))
        flags.extend(options(args[index + 1:], CODEX_VALUES, skip_positionals=True))
        for key, value in flags:
            if key in {"--full-auto", "--approve-for-me", "--yolo",
                       "--dangerously-bypass-approvals-and-sandbox"}:
                return None
            if key in ("-s", "--sandbox") and value in ("workspace-write", "danger-full-access"):
                return None
            if key in ("-c", "--config"):
                name, sep, mode = value.partition("=")
                if sep and name.strip() == "sandbox_mode" and mode.strip().strip("\"'") != "read-only":
                    return None
        return "codex"
    if program == "claude":
        sub, _ = subcommand(args, CLAUDE_VALUES)
        if sub == "ultrareview":
            return "claude"
        flags = list(options(args, CLAUDE_VALUES, skip_positionals=True))
        # Commander variadic tool options can contain several argv words.
        for i, arg in enumerate(args):
            if arg.split("=", 1)[0] in ("--allowedTools", "--allowed-tools"):
                values = [arg.split("=", 1)[1]] if "=" in arg else []
                for value in args[i + 1:]:
                    if value.startswith("-"):
                        break
                    values.append(value)
                flags.append(("--allowedTools", " ".join(values)))
        if not any(k in ("-p", "--print") for k, _ in flags):
            return None
        if any(k == "--dangerously-skip-permissions" or
               (k == "--permission-mode" and v in ("acceptEdits", "bypassPermissions")) or
               (k in ("--allowedTools", "--allowed-tools") and WRITE_TOOL.search(v))
               for k, v in flags):
            return None
        return "claude"
    if program == "magi":
        return magi_reviewer(args)
    if program.startswith("python"):
        index = python_script(argv)
        if index is not None and argv[index].endswith("run_codex_task.py") and any(
                a == "--sandbox=read-only" or
                (a == "--sandbox" and i + 1 < len(args) and args[i + 1] == "read-only")
                for i, a in enumerate(args)):
            return "runner"
    return None


def event(kind, decision, ticket=None, reason=None, parse="ok", error=None):
    if ticket and ("$(" in ticket or "`" in ticket):
        ticket = None  # A dynamic expression is command text, not a ticket ID.
    return dict(decision=decision, reviewer=kind, ticket=ticket,
                bypass_reason=reason[:200] if reason else None, parse=parse, error=error)


def wrapped(argv):
    offset = 1
    program = os.path.basename(argv[0])
    if program.startswith("python"):
        offset = python_script(argv)
        if offset is None or os.path.basename(argv[offset]) != "review_gate.py":
            return None
        offset += 1
    elif program not in ("review-gate", "review_gate.py"):
        return None
    if argv[offset:offset + 1] != ["run"]:
        return None
    end = argv.index("--") if "--" in argv else len(argv)
    for i in range(offset + 1, end):
        if argv[i].startswith("--ticket=") and argv[i][9:]:
            return argv[i][9:], end
        if argv[i] == "--ticket" and i + 1 < end and argv[i + 1]:
            return argv[i + 1], end
    return None


def short_options(argv, start, values, long_values=()):
    """Return the first operand index after getopt-style options; values take an argument."""
    i = start
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        arg = argv[i]
        if arg == "--":
            return i + 1
        if arg.startswith("--"):
            i += 2 if arg in long_values else 1
            continue
        letters = arg[1:]
        taking = next((j for j, letter in enumerate(letters) if letter in values), None)
        i += 2 if taking == len(letters) - 1 else 1
    return i


def option_value(argv, start, end, short, long):
    """Return the value of -X/--long (or a cluster ending in X) within argv[start:end]."""
    for i in range(start, end):
        arg = argv[i]
        if arg.startswith(long + "="):
            return arg[len(long) + 1:]
        if arg == long or (arg.startswith("-") and not arg.startswith("--") and arg.endswith(short)):
            return argv[i + 1] if i + 1 < len(argv) else None
    return None


def tmux_targets(words, argv):
    """tmux runs its new-session/new-window/split-window command and types send-keys text."""
    shell = option_value(argv, 1, short_options(argv, 1, "LSfTc"), "c", "-c")
    targets = [shell] if shell else []
    start = short_options(argv, 1, "LSfTc")
    while start < len(argv):
        end = next((j for j in range(start, len(argv)) if argv[j] in (";", "\\;")), len(argv))
        sub = argv[start]
        if sub in TMUX_LAUNCH:
            first = short_options(argv[:end], start + 1, "cefFlnpstxy")
            rest = words[first:end]
            if len(rest) == 1:
                targets.append(text_of(rest[0]))
            elif rest:
                targets.append(rest)
        elif sub in ("send-keys", "send"):
            first = short_options(argv[:end], start + 1, "cNt")
            targets.append(" ".join(text_of(w) for w in words[first:end]))
        start = end + 1
    return targets


def script_targets(words, argv):
    """util-linux script runs -c/--command; BSD script runs the argv after its file operand."""
    end = short_options(argv, 1, "cIOBTmEot", ("--command", "--log-in", "--log-out", "--log-io",
                                                "--log-timing", "--logging-format", "--echo",
                                                "--output-limit"))
    command = option_value(argv, 1, end, "c", "--command")
    if command is not None:
        return [command]
    first = short_options(argv, 1, "t")
    return [words[first + 1:]] if first + 1 < len(words) else []


def npx_targets(words, argv):
    command = option_value(argv, 1, short_options(argv, 1, "cpw", ("--package", "--workspace", "--call")),
                           "c", "--call")
    if command is not None:
        return [command]
    first = short_options(argv, 1, "cpw", ("--package", "--workspace", "--call"))
    if first >= len(words):
        return []
    package = re.sub(r"(?<=.)@[^/]*$", "", argv[first])
    if package in NPX_PACKAGES:
        return [[Word(NPX_PACKAGES[package])] + words[first + 1:]]
    return [words[first:]]


def launch_targets(program, words):
    """Shell text (str) or argv (list of Word) that a launcher program runs, or None."""
    argv = [w.value for w in words]
    if program == "eval":
        return [" ".join(text_of(w) for w in words[1:])]
    if program == "xargs":
        return [words[short_options(argv, 1, "adEIJLnPRSs", (
            "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines", "--max-args",
            "--max-procs", "--max-chars", "--process-slot-var")):]]
    if program == "stdbuf":
        return [words[short_options(argv, 1, "ioe", ("--input", "--output", "--error")):]]
    if program == "script":
        return script_targets(words, argv)
    if program == "tmux":
        return tmux_targets(words, argv)
    if program == "npx":
        return npx_targets(words, argv)
    return None


TIMEOUT_VALUES = frozenset({"-k", "--kill-after", "-s", "--signal", "-o", "--output", "-f", "--format"})
UTILITY_VALUES = {"nice": {"-n"}, "caffeinate": {"-t", "-w"},
                  "timeout": TIMEOUT_VALUES, "gtimeout": TIMEOUT_VALUES, "time": TIMEOUT_VALUES}


def utility_cut(program, argv):
    cut = 1
    while cut < len(argv) and argv[cut].startswith("-"):
        cut += 2 if argv[cut] in UTILITY_VALUES[program] else 1
    return cut + (program in ("timeout", "gtimeout"))


def declare(variables, args):
    for match in filter(None, map(ASSIGNMENT.match, args)):
        variables[match[1]] = match[2]


def text_of(word):
    return word.evaluated if word.evaluated is not None else word.value


def dynamic_reviewer(value, variables):
    """A variable or substituted program is matched by the name or value it expands from."""
    name = os.path.basename(value)
    if "$" not in name and "`" not in name:
        return None
    variable = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z_0-9]*)\}?", name)
    hits = DYNAMIC_REVIEWER.findall(name + " " + variables.get(variable[1], "") if variable else name)
    return hits[-1].lower() if hits else None


def scan_command(words, assignments=None, depth=0, inputs=None, variables=None):
    if depth > 80:
        raise ParseFailure("nesting limit")
    assignments = dict(assignments or {})
    inputs = list(inputs or [])
    variables = variables if variables is not None else {}
    local = {}
    argv_words, targets, i = [], [], 0
    while i < len(words):
        if words[i].redirection:
            if i + 1 < len(words):
                if words[i].value in ("<<", "<<-", "<<<"):
                    inputs.append(words[i + 1])
                else:
                    if words[i].value.startswith((">", "&>")):
                        targets.append(words[i + 1].value)
                    yield from scan_substitutions(words[i + 1].substitutions, depth)
            i += 2
        else:
            argv_words.append(words[i])
            i += 1
    words = argv_words
    while words:
        match = ASSIGNMENT.match(words[0].value)
        if match:
            assignments[match[1]] = local[match[1]] = match[2]
            # Assignment substitutions execute even without a following program.
            prefix = words[0].substitutions
            words = words[1:]
            yield from scan_substitutions(prefix, depth)
        elif words[0].value in PREFIXES:
            words = words[1:]
        else:
            break
    if not words:
        variables.update(local)
        yield from scan_inputs(inputs, False, assignments, depth)
        return
    argv = [w.value for w in words]
    program = os.path.basename(argv[0])
    if program in DECLARATIONS:
        declare(variables, argv[1:])
    wrap = wrapped(argv)
    if wrap:
        ticket, end = wrap
        for word in words[:end]:
            yield from scan_substitutions(word.substitutions, depth)
        yield event("unknown", "wrapped", ticket=ticket)
        return
    # Wrappers are removed before scanning their arguments for substitutions.
    cut = None
    launched = launch_targets(program, words)
    if program in ("sh", "bash", "zsh", "dash"):
        for i, arg in enumerate(argv[1:], 1):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                yield from scan_inputs(inputs, False, assignments, depth)
                if i + 1 < len(argv):
                    for word in words:
                        yield from scan_substitutions(word.substitutions, depth)
                    script = words[i + 1]
                    yield from scan_shell(script.evaluated if script.evaluated is not None else script.value,
                                          assignments, depth + 1)
                return
        yield from scan_inputs(inputs, True, assignments, depth)
        inputs = []
    elif program == "ssh":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            arg = argv[i]
            i += 2 if len(arg) == 2 and arg[1:] in SSH_VALUES else 1
        for word in words:
            yield from scan_substitutions(word.substitutions, depth)
        if i + 1 < len(argv):
            remote = " ".join(w.evaluated if w.evaluated is not None else w.value
                              for w in words[i + 1:])
            for remote_words in tokenize(remote):
                yield from scan_command(remote_words, assignments, depth + 1, inputs)
        else:
            yield from scan_inputs(inputs, True, assignments, depth)
        return
    elif program == "cmux" and any(a == CMUX_COMMAND or a.startswith(CMUX_COMMAND + "=") for a in argv[1:]):
        # new-workspace/new-pane/new-surface --command runs its text in a shell like `cmux send`.
        yield from scan_inputs(inputs, False, assignments, depth)
        for word in words:
            yield from scan_substitutions(word.substitutions, depth)
        for i, word in enumerate(words[1:], 1):
            text = word.evaluated if word.evaluated is not None else word.value
            if word.value.startswith(CMUX_COMMAND + "="):
                yield from scan_shell(text[len(CMUX_COMMAND) + 1:], assignments, depth + 1)
            elif word.value == CMUX_COMMAND and i + 1 < len(words):
                script = words[i + 1]
                yield from scan_shell(script.evaluated if script.evaluated is not None else script.value,
                                      assignments, depth + 1)
        return
    elif program in ("cat", "tee") and any(w.heredoc is not None for w in inputs) and (
            any(t.endswith(SCRIPT_SUFFIXES) for t in targets + (argv[1:] if program == "tee" else []))
            or any((w.heredoc or "").startswith("#!") for w in inputs)):
        # A heredoc written to a script file is a later launch; scan it like write_file content.
        yield from scan_inputs(inputs, True, assignments, depth)
        return
    elif program == "cmux" and "send" in argv[1:] and len(argv) > 2:
        yield from scan_inputs(inputs, False, assignments, depth)
        for word in words:
            yield from scan_substitutions(word.substitutions, depth)
        script = words[-1]
        yield from scan_shell(script.evaluated if script.evaluated is not None else script.value,
                              assignments, depth + 1)
        return
    elif launched is not None:
        yield from scan_inputs(inputs, False, assignments, depth)
        yield from scan_launched(launched, words, assignments, depth, variables)
        return
    elif program == "env":
        cut = 1
        while cut < len(argv) and argv[cut] in ("-i", "--ignore-environment", "--"):
            cut += 1
    elif program == "sudo":
        cut = 1
        while cut < len(argv) and argv[cut].startswith("-"):
            cut += 2 if argv[cut] in ("-u", "-g") else 1
    elif program in UTILITY_VALUES:
        cut = utility_cut(program, argv)
    elif program == "screen":
        # screen runs its trailing argv as the window's program; -S/-c/-e/-h/-p/-t/-T take a value.
        cut = 1
        while cut < len(argv) and argv[cut].startswith("-"):
            cut += 2 if argv[cut][-1] in "ScehptT" and argv[cut] != "-" else 1
    elif program in ("nohup", "exec", "command", "builtin"):
        cut = 1
        while cut < len(argv) and argv[cut].startswith("-"):
            cut += 1
    if cut is not None:
        for word in words[:cut]:
            yield from scan_substitutions(word.substitutions, depth)
        yield from scan_command(words[cut:], assignments, depth + 1, inputs, variables)
        return
    yield from scan_inputs(inputs, False, assignments, depth)
    for word in words:
        yield from scan_substitutions(word.substitutions, depth)
    if program in BOUNDARIES:
        return
    kind = reviewer([dynamic_reviewer(argv[0], variables) or argv[0]] + argv[1:])
    if kind:
        reason = assignments.get("REVIEW_GATE_NOT_REVIEW")
        yield event(kind, "bypass" if reason else "blocked", reason=reason)


def scan_launched(launched, words, assignments, depth, variables):
    passed = {id(w) for target in launched if not isinstance(target, str) for w in target}
    for word in words:
        if id(word) not in passed:
            yield from scan_substitutions(word.substitutions, depth)
    for target in launched:
        if isinstance(target, str):
            yield from scan_shell(target, assignments, depth + 1)
        else:
            yield from scan_command(target, assignments, depth + 1, None, variables)


def scan_substitutions(subs, depth):
    for body in subs:
        yield from scan_shell(body, depth=depth + 1)


def scan_inputs(inputs, shell, assignments, depth):
    for operand in inputs:
        if operand.heredoc is None:  # A here-string has ordinary word expansion.
            yield from scan_substitutions(operand.substitutions, depth)
            body = operand.evaluated if operand.evaluated is not None else operand.value
        else:
            body = operand.heredoc
            if not operand.quoted_delimiter:
                # Heredoc body quotes are data; only $, backticks and escapes act
                # as they do within double quotes. Mask expanded text for shells.
                expanded, i = [], 0
                while i < len(body):
                    c = body[i]
                    if c == "\\" and i + 1 < len(body) and body[i + 1] in "$`\\\n":
                        expanded.append(body[i:i + 2])
                        i += 2
                        continue
                    if body.startswith("$(", i) or c == "`":
                        script, i = substitution(body, i, c == "`")
                        yield from scan_shell(script, depth=depth + 1)
                        expanded.append("__substitution__")
                        continue
                    expanded.append(c)
                    i += 1
                body = "".join(expanded)
        if shell:
            yield from scan_shell(body, assignments, depth + 1)


def scan_shell(text, assignments=None, depth=0):
    if depth > 80:
        raise ParseFailure("nesting limit")
    variables = {}
    for words in tokenize(text):
        yield from scan_command(words, assignments, depth, variables=variables)


# Conservative recovery only: exemptions must directly cover this same match.
FALLBACK = re.compile(
    r"(?<![\w./-])(?:[^\s;|&\"'()]*/)?(?P<program>codex|claude|magi|python[\w.-]*)\s+")
FALLBACK_WRAP = re.compile(
    r"(?:review-gate|review_gate\.py)\s+run\s+[^;|&\n]*--ticket(?:=|\s+)\S+[^;|&\n]*\s--\s")
FALLBACK_BYPASS = re.compile(
    r"REVIEW_GATE_NOT_REVIEW=(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s;|&]+)\s*$")


def fallback(text):
    for line in text.splitlines():
        for match in FALLBACK.finditer(line):
            program = match.group("program")
            args = re.split(r"[;|&]", line[match.end():], maxsplit=1)[0]
            # Salvage complete words despite an unrelated broken quote on the line.
            rough = re.findall(r'''"[^"\n]*"|'[^'\n]*'|[^\s]+''', args)
            argv = [program] + [a.strip("\"'") for a in rough]
            kind = reviewer(argv)
            if not kind:
                continue
            before = re.split(r"[;|&]", line[:match.start()])[-1]
            if FALLBACK_WRAP.search(before):
                ticket = re.search(r"--ticket(?:=|\s+)(\S+)", before)
                yield event(kind, "wrapped", ticket=ticket[1].strip("\"'"), parse="fallback")
            elif FALLBACK_BYPASS.search(before):
                reason = FALLBACK_BYPASS.search(before)[0].split("=", 1)[1].strip().strip("\"'")
                yield event(kind, "bypass", reason=reason, parse="fallback")
            else:
                yield event(kind, "blocked", parse="fallback")


def string_command(node, constants):
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(n.value if isinstance(n, ast.Constant) and isinstance(n.value, str)
                       else "__dynamic__" for n in node.values)
    return None


EXECUTION_CALLS = {"terminal", "subprocess.run", "subprocess.call", "subprocess.check_call",
                   "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen"}


def python_names(tree):
    """Import aliases of the execution calls, and names bound once to a constant string."""
    modules, functions, constants, rebound = {}, {}, {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = "terminal" if alias.name == "terminal" else node.module + "." + alias.name
                if full in EXECUTION_CALLS:
                    functions[alias.asname or alias.name] = full
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in constants or not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                rebound.add(name)
            constants[name] = getattr(node.value, "value", None)
    return modules, functions, {k: v for k, v in constants.items() if k not in rebound}


def call_name(fn, modules, functions):
    if isinstance(fn, ast.Name):
        return functions.get(fn.id, fn.id)
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        return modules.get(fn.value.id, fn.value.id) + "." + fn.attr
    return ""


def argv_word(node, constants, program=False):
    value = string_command(node, constants)
    if value is not None and "__dynamic__" not in value:
        return Word(value)
    # A variable program is shown as shell would show it, so dynamic_reviewer can match its name.
    if program and isinstance(node, (ast.Name, ast.Attribute)):
        return Word("$" + (node.id if isinstance(node, ast.Name) else node.attr))
    return Word("__dynamic__")


def execution_calls(code):
    tree = ast.parse(code)
    modules, functions, constants = python_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or call_name(node.func, modules, functions) not in EXECUTION_CALLS:
            continue
        arg = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "command"), None)
        if isinstance(arg, (ast.List, ast.Tuple)):
            first = arg.elts[0] if arg.elts else None
            if isinstance(first, (ast.Name, ast.Attribute)) or (
                    isinstance(first, ast.Constant) and isinstance(first.value, str)):
                yield [argv_word(first, constants, True)] + [argv_word(n, constants) for n in arg.elts[1:]]
        else:
            value = string_command(arg, constants)
            if value is not None:
                yield value


def detect(payload):
    tool, data = payload.get("tool_name"), payload.get("tool_input", {})
    if not isinstance(data, dict):
        return []
    commands = []
    if tool == "terminal" and isinstance(data.get("command"), str):
        commands = [data["command"]]
    elif tool == "execute_code" and isinstance(data.get("code"), str):
        commands = list(execution_calls(data["code"]))
    elif tool in ("write_file", "patch"):
        content = data.get("content" if tool == "write_file" else "new_string", "")
        path = data.get("path", "")
        if isinstance(content, str) and isinstance(path, str) and (
                path.endswith((".sh", ".zsh", ".bash", ".command")) or content.startswith("#!")):
            commands = [content]
    results = []
    for command in commands:
        try:
            results.extend(list(scan_shell(command) if isinstance(command, str)
                                else scan_command(command)))
        except ParseFailure:
            results.extend(fallback(command if isinstance(command, str) else " ".join(w.value for w in command)))
    return results


def log_events(payload, records):
    if not records:
        return
    try:
        path = Path(os.environ.get("REVIEW_GATE_HOOK_LOG", DEFAULT_LOG)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            os.fchmod(stream.fileno(), 0o600)
            for record in records:
                record = dict(record, ts=datetime.now(timezone.utc).isoformat(),
                              session_id=payload.get("session_id"), tool=payload.get("tool_name"))
                stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        # An unavailable log must not turn a pre-tool hook into a tool outage.
        pass


REVIEW_ENTRY = ("hermes-delegate review --reviewer {} --item <id> --commit <sha> --prompt <file> "
                "--workdir <dir> [--base <sha>]")
CLOSE_RULE = ("When it finishes, reproduce the findings yourself and record the verdict with "
              "review-gate close (PASS: --blockers 0 or omitted; BLOCK: --blockers N). ")
ROUTES = {
    "claude": ("Claude reviews go only through " + REVIEW_ENTRY.format("claude") + ", after "
               "review-gate open --item <id> --goal ... once; do not hand-build claude -p, "
               "cmux/tmux/screen scripts or review-gate run for them. " + CLOSE_RULE +
               "Claude implementation runs go through hermes-delegate claude. "),
    "codex": ("Codex reviews go only through " + REVIEW_ENTRY.format("codex") + ", after "
              "review-gate open --item <id> --goal ... once; it fixes the model, read-only sandbox "
              "and ledger ticket. " + CLOSE_RULE +
              "Device/simulator QA is not a review: launch it with hermes-delegate qa. "),
    "runner": ("A read-only Codex delegation is not a review: print it with hermes-delegate codex, "
               "which omits --sandbox read-only (the runner default). Codex reviews go through " +
               REVIEW_ENTRY.format("codex") + ". "),
    "magi": ("Run magi reviews through the ledger: review-gate open --item <id> --goal ... (once); "
             'T=$(review-gate start --item <id> --reviewer magi --commit <sha>); '
             'review-gate run --ticket "$T" -- magi ... ' + CLOSE_RULE),
}


def block_message(kind):
    return ('review-gate: unwrapped {} review launch blocked. {}'
            'Auth/exec check only? Use hermes-delegate smoke codex|claude. Other non-review '
            '(research)? Prefix: REVIEW_GATE_NOT_REVIEW="<reason>" <command>.').format(kind, ROUTES[kind])


def main(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str) or payload["tool_name"] not in {
            "terminal", "execute_code", "write_file", "patch"}:
        return 0
    try:
        records = detect(payload)
    except Exception as exc:
        log_events(payload, [event("unknown", "error", error=type(exc).__name__)])
        return 0
    log_events(payload, records)
    blocked = next((r for r in records if r["decision"] == "blocked"), None)
    if blocked:
        message = block_message(blocked["reviewer"])
        stdout.write(json.dumps({"action": "block", "message": message}) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    # A one-shot process: cyclic collection over the parser's many small objects only
    # adds time (about half of it on a large script) and frees nothing before exit.
    gc.disable()
    sys.exit(main())
