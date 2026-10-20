#!/usr/bin/env python3
"""Hermes pre_tool_call guard. Detection is local, fail-open, and never logs input."""

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
CMUX_COMMAND = "--command"
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
            else:
                append(c)
            i += 1
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
            operator = re.match(r"(?:<<<|<<-|&>>?|[<>](?:[<>]|&|\|)?)", text[i:])[0]
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
            append(c)
            active = True
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


def reviewer(argv):
    if not argv:
        return None
    program, args = os.path.basename(argv[0]), argv[1:]
    if program == "codex":
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
        # Exactly the shim's scan: help anywhere; first non-option is subcommand.
        if any(arg in ("--help", "-h") for arg in args):
            return None
        sub = next((arg for arg in args if not arg.startswith("-")), None)
        if sub is None or sub in {"lanes", "doctor", "report", "report-plan", "plan", "publish"}:
            return None
        return "magi"
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


def scan_command(words, assignments=None, depth=0, inputs=None):
    if depth > 80:
        raise ParseFailure("nesting limit")
    assignments = dict(assignments or {})
    inputs = list(inputs or [])
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
            assignments[match[1]] = match[2]
            # Assignment substitutions execute even without a following program.
            prefix = words[0].substitutions
            words = words[1:]
            yield from scan_substitutions(prefix, depth)
        elif words[0].value in PREFIXES:
            words = words[1:]
        else:
            break
    if not words:
        yield from scan_inputs(inputs, False, assignments, depth)
        return
    argv = [w.value for w in words]
    program = os.path.basename(argv[0])
    wrap = wrapped(argv)
    if wrap:
        ticket, end = wrap
        for word in words[:end]:
            yield from scan_substitutions(word.substitutions, depth)
        yield event("unknown", "wrapped", ticket=ticket)
        return
    # Wrappers are removed before scanning their arguments for substitutions.
    cut = None
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
    elif program == "env":
        cut = 1
        while cut < len(argv) and argv[cut] in ("-i", "--ignore-environment", "--"):
            cut += 1
    elif program == "sudo":
        cut = 1
        while cut < len(argv) and argv[cut].startswith("-"):
            cut += 2 if argv[cut] in ("-u", "-g") else 1
    elif program in ("nice", "caffeinate", "timeout", "gtimeout", "time"):
        cut = 1
        value_flags = {"-n"} if program == "nice" else {"-t", "-w"} if program == "caffeinate" else {
            "-k", "--kill-after", "-s", "--signal", "-o", "--output", "-f", "--format"}
        while cut < len(argv) and argv[cut].startswith("-"):
            cut += 2 if argv[cut] in value_flags else 1
        if program in ("timeout", "gtimeout"):
            cut += 1
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
        yield from scan_command(words[cut:], assignments, depth + 1, inputs)
        return
    yield from scan_inputs(inputs, False, assignments, depth)
    for word in words:
        yield from scan_substitutions(word.substitutions, depth)
    if program in BOUNDARIES:
        return
    kind = reviewer(argv)
    if kind:
        reason = assignments.get("REVIEW_GATE_NOT_REVIEW")
        yield event(kind, "bypass" if reason else "blocked", reason=reason)


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
    for words in tokenize(text):
        yield from scan_command(words, assignments, depth)


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


def string_command(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(n.value if isinstance(n, ast.Constant) and isinstance(n.value, str)
                       else "__dynamic__" for n in node.values)
    return None


def execution_calls(code):
    tree = ast.parse(code)
    calls = {"terminal", "subprocess.run", "subprocess.call", "subprocess.check_call",
             "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.value.id + "." + fn.attr if isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name) else "")
        if name not in calls:
            continue
        arg = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "command"), None)
        if isinstance(arg, (ast.List, ast.Tuple)):
            if arg.elts and isinstance(arg.elts[0], ast.Constant) and isinstance(arg.elts[0].value, str):
                yield [Word(n.value if isinstance(n, ast.Constant) and isinstance(n.value, str)
                            else "__dynamic__") for n in arg.elts]
        else:
            value = string_command(arg)
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
        kind = blocked["reviewer"]
        reviewer_name = "codex" if kind == "runner" else kind
        if kind == "claude":
            route = ('Claude reviews on the Mac go only through hermes-delegate review '
                     '(after review-gate open --item <id> --goal ... once); do not hand-build '
                     'claude -p, cmux scripts or review-gate run for them. Claude implementation '
                     'runs go through hermes-delegate claude. ')
        else:
            route = ('Run reviews through the ledger: review-gate open --item <id> --goal ... (once); '
                     'T=$(review-gate start --item <id> --reviewer {} --commit <sha>); '
                     'review-gate run --ticket "$T" -- <your command>. ').format(reviewer_name)
        if kind == "runner":
            route += ('A read-only Codex delegation is not a review: print it with hermes-delegate codex, '
                      'which omits --sandbox read-only (the runner default). ')
        if kind == "codex":
            route += 'Device/simulator QA is not a review: launch it with hermes-delegate qa. '
        message = (
            'review-gate: unwrapped {} review launch blocked. {}'
            'Not a review (smoke test/research)? Prefix: '
            'REVIEW_GATE_NOT_REVIEW="<reason>" <command>.'
        ).format(kind, route)
        stdout.write(json.dumps({"action": "block", "message": message}) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
