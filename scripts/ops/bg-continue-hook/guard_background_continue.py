#!/usr/bin/env python3
"""pre_tool_call hook: make waited-on background terminal runs wake the coordinator.

Why: a background terminal run launched with notify but without continue_on_complete only
opens a read-only verification turn when it exits, so the approved work stalls until the owner
sends a message. The rule lived in a skill and was forgotten repeatedly (2026-10-19..20).

Rules (terminal tool only; Hermes semantics verified in tools/terminal_tool.py of the live tree):
  Exit notification is decided first, exactly as Hermes does: an explicit boolean notify wins over
  notify_on_complete, a notify pattern list (watch_patterns) means no exit notification, and an
  integer heartbeat > 0 turns it on.
  A. background, exit notification on, and continue_on_complete not passed
     -> modify: continue_on_complete=true, plus persist_on_release=true when not passed.
  B. foreground (background falsy), numeric timeout above the foreground cap (600 s, or
     $TERMINAL_MAX_FOREGROUND_TIMEOUT), and continue_on_complete not exactly false
     -> block: Hermes would silently promote it to an unarmed background run.
  Only continue_on_complete=false (a boolean) is the opt-out; it is respected and logged as
  "opted-out". true is already armed. Any other value is left for Hermes to refuse.
  Sleep-poll rules (foreground only; a background watcher costs no turns and is never blocked).
  A sleep (N >= 2) pairs with a check inside the same polling loop (while/until, or for over
  $(seq ..) / {1..N} / 1 2 3), or when the check follows the sleep in the same command list with
  only neutral commands (cd, export, echo, text filters, reads) in between. A sleep inside a loop
  never pairs with checks after the loop, and nested ssh '...' / bash -c '...' bodies are judged
  on their own.
  C. check = delegation peek: pgrep/ps|grep/pkill -0 for codex exec, claude -p, run_codex_task or
     hermes-delegate, or ls/cat/tail/head/wc/find/stat/test/grep on ~/.local/state/hermes-delegate
     -> block "blocked_peek". hermes-delegate wait/status/cancel are the official paths.
  D. check = gh pr checks / gh run view / gh pr view statusCheckRollup / gh api .../actions/runs,
     or gh run list after a sleep over 30 s or in a polling loop -> block "blocked_ci_poll".
     Not polls: gh pr checks --watch and gh run watch (and the run-id lookup or startup delay
     feeding them), or a sleep <= 30 s right after a trigger (push, rerun, workflow run, merge).
  E. a polling loop with a sleep and a file wait ([ -f X ], test -e, grep -q on a .rc/.done/.log/
     marker) -> block "blocked_file_wait_loop". Allowed: readiness loops (curl/lsof/nc/adb/simctl/
     healthz) without a marker file, and a wait for a job the same call launched (cmux/tmux/nohup).
  Why: completion already arrives through the watcher or notify; sleep-polling burned turns
  (2026-10-22..26 replay: 69 delegation peeks, 31 CI polls, 8 file-wait loops in 5591 calls).

Exit 0 = allow (stdout empty, or a modify directive). Exit 2 = block. Any internal error or
malformed stdin = allow (fail open). The log never contains the command text.
"""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

DEFAULT_CAP = 600
LOG_ENV = "BG_CONTINUE_HOOK_LOG"
DEFAULT_LOG = "~/.hermes/profiles/koharu/logs/bg-continue-hook.jsonl"


STATE_MARKER = ".local/state/hermes-delegate"
PEEK_NEEDLES = ("codex exec", "claude -p", "run_codex_task", "hermes-delegate")
PEEK_NAMES = {"codex", "claude"}
PROCESS_GREPS = {"grep", "egrep", "rg", "awk"}
FILE_PEEKS = {"ls", "cat", "tail", "head", "wc", "find", "stat", "test", "[", "[["}
FILE_TESTS = {"test", "[", "[["}
READINESS = {"curl", "wget", "lsof", "nc", "adb", "xcrun", "simctl", "ping", "pg_isready", "redis-cli"}
NESTED_SHELLS = {"bash", "sh", "zsh", "ssh"}
WRAPPERS = {"nohup", "command", "exec", "time", "builtin"}
SKIP_WORDS = {"if", "then", "elif", "else", "!", "fi", "esac"}
LOOPS = {"for", "while", "until", "select"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2[ \t]*$", re.S | re.M)
SINGLE_QUOTED = re.compile(r"'[^']*'")
DOUBLE_QUOTED = re.compile(r'"[^"]*"')
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)")
COUNTER = re.compile(r"seq|\{\d+\.\.")
LAUNCHERS = {"cmux", "tmux", "screen", "nohup", "setsid", "systemd-run"}
NEUTRAL = {"cd", "pushd", "popd", "export", "echo", "printf", "true", ":", "set", "unset", "wc", "head",
           "tail", "grep", "egrep", "rg", "awk", "sed", "cut", "tr", "sort", "uniq", "jq", "xargs", "tee",
           "cat", "column", "date", "basename", "dirname", "ls", "find", "stat", "test", "[", "[[", "ps",
           "pgrep", "du", "md5", "md5sum", "shasum", "realpath", "readlink", "break", "continue"}
CHECKS = {"peek", "ci", "ci_lookup", "file_wait", "sleep", "watch"}
STARTUP_DELAY = 30
DECLARATIONS = {"export", "local", "typeset", "declare", "readonly"}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$", re.S)
SLEEP_ARG = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
SLEEP_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
MARKER_FILE = re.compile(
    r"(?:\.(?:rc|done|exit|status|log|out|end|finished|complete)|(?:^|/)(?:done|[A-Z][A-Z0-9_]{2,}))[\"']?$")
PREFILTER = re.compile(r"odex|laude|hermes-delegate|\bgh\b|\b(?:for|while|until)\b")  # odex: [c]odex trick
PEEK_BUDGET_SECONDS = 0.1
PEEK_SIZE_LIMIT = 64 * 1024
NEST_LIMIT = 8
SLEEP_RULES = (  # (check kind, decision); the first match wins
    ("peek", "blocked_peek"), ("ci", "blocked_ci_poll"), ("file_wait", "blocked_file_wait_loop"))
MESSAGES = {
    "blocked_peek": (
        "bg-continue hook: delegation sleep-poll blocked / 위임 작업을 sleep으로 폴링하지 마세요. "
        "After launched=yes or a printed codex argv, check at most once with "
        "`hermes-delegate status PATH` (launched=yes 또는 codex argv 출력 뒤 확인은 이것 한 번만). "
        "Completion arrives via the hermes-delegate watcher / gateway notification "
        "(완료는 watcher·게이트웨이 알림으로 옵니다); end the turn instead of sleeping."),
    "blocked_ci_poll": (
        "bg-continue hook: CI sleep-poll blocked / CI를 sleep으로 폴링하지 마세요. Run "
        "`gh pr checks <PR> --watch --fail-fast` or `gh run watch <ID> --exit-status` as ONE "
        "background terminal call with notify_on_complete=true (백그라운드 한 번 + 완료 알림)."),
    "blocked_file_wait_loop": (
        "bg-continue hook: foreground file-wait loop blocked / 결과 파일을 sleep 루프로 기다리지 마세요. "
        "If the result is needed this turn, run that command in the foreground with a timeout "
        "instead of backgrounding it and polling (이번 턴에 필요하면 foreground+timeout으로 직접 실행); "
        "otherwise wait for its completion notification (아니면 완료 알림을 기다리세요)."),
}
_TOKENIZER = []


def _tokenizer():
    """guard_review_gate's shell tokenizer, or None. Deployed side by side; repo layout second."""
    if _TOKENIZER:
        return _TOKENIZER[0]
    module = None
    here = Path(__file__).resolve().parent
    for candidate in (here / "guard_review_gate.py", here.parent / "review-gate/hooks/guard_review_gate.py"):
        if not candidate.is_file():
            continue
        try:
            sys.dont_write_bytecode = True  # keep ~/.hermes/scripts free of __pycache__
            spec = importlib.util.spec_from_file_location("_bg_guard_review_gate", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            break
        except Exception:
            module = None
    _TOKENIZER.append(module)
    return module


def _fallback_commands(text):
    """Rough split for when the shared tokenizer is unavailable or gives up."""
    bodies, quoted = [], []

    def quote(match):
        quoted.append(match[0][1:-1])
        return "\x01%d\x01" % (len(quoted) - 1)

    def hide(match):
        bodies.append(re.sub(r"\$<(\d+)_\w*>", lambda m: "$(%s)" % bodies[int(m[1])], match[1]))
        # Keep a hint of the body (for example "seq") so loop headers stay recognizable.
        return "$<%d_%s>" % (len(bodies) - 1, re.sub(r"\W", "_", match[1])[:40])

    text = SINGLE_QUOTED.sub(quote, HEREDOC.sub("", text))
    while True:
        hidden = SUBSTITUTION.sub(hide, text)
        if hidden == text:
            break
        text = hidden
    text = DOUBLE_QUOTED.sub(quote, text)

    def restore(word):
        return re.sub("\x01(\\d+)\x01", lambda m: quoted[int(m[1])], word)

    for segment in re.split(r"[;&|\n()`]", text):
        argv = [restore(w) for w in segment.split()]
        subs = [restore(bodies[int(n)]) for n in re.findall(r"\$<(\d+)_", restore(segment))]
        if argv:
            yield argv, subs


def _commands(text):
    """Yield (argv, substitution bodies) per simple command, in source order."""
    module = _tokenizer()
    if module is None or len(text) > PEEK_SIZE_LIMIT:
        yield from _fallback_commands(text)
        return
    try:
        module.PARSE_DEADLINE[0] = time.perf_counter() + PEEK_BUDGET_SECONDS
        parsed = module.tokenize(text)
    except Exception:
        parsed = None
    finally:
        module.PARSE_DEADLINE[0] = float("inf")
    if parsed is None:
        yield from _fallback_commands(text)
        return
    for words in parsed:
        yield [w.value for w in words if not w.redirection], [s for w in words for s in w.substitutions]


class _Loop(list):
    def __init__(self, polling):
        super().__init__()
        self.polling = polling


class _Script:
    """One shell scope. Nested shell bodies (ssh '...', bash -c '...') get their own _Script."""

    def __init__(self, text, depth=0):
        self.depth, self.vars, self.ps, self.cancel, self.nested = depth, {}, False, False, []
        self.root = []
        stack = [self.root]
        for argv, subs in _commands(text):
            self._add(stack, argv, subs)

    def _add(self, stack, argv, subs):
        for body in subs:  # a substitution runs at its command's position
            if self.depth < NEST_LIMIT:
                for sub_argv, sub_subs in _commands(body):
                    self._add(stack, sub_argv, sub_subs)
        while argv:
            head = argv[0]
            if head in SKIP_WORDS:
                argv = argv[1:]
            elif head in LOOPS:
                if head in ("for", "select"):
                    items = argv[3:] if len(argv) >= 3 and argv[2] == "in" else []
                    loop = _Loop(_is_counter(items))
                    if items:
                        self.vars[argv[1]] = " ".join(items)
                    argv = []
                else:
                    loop = _Loop(True)
                    argv = argv[1:]
                stack[-1].append(loop)
                stack.append(loop)
            elif head == "do":
                argv = argv[1:]
            elif head == "done":
                if len(stack) > 1:
                    stack.pop()
                return
            elif ASSIGNMENT.match(head):
                match = ASSIGNMENT.match(head)
                self.vars[match[1]] = match[2]
                argv = argv[1:]
            else:
                break
        if argv:
            stack[-1].append(self._command(argv))

    def _command(self, argv):
        program = os.path.basename(argv[0])
        launch = program in LAUNCHERS
        while program in WRAPPERS or program in ("timeout", "env"):
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or (program == "env" and ASSIGNMENT.match(argv[0]))):
                argv = argv[2:] if program == "timeout" and argv[0] in ("-k", "-s") else argv[1:]
            if program == "timeout":
                argv = argv[1:]
            if not argv:
                return {"program": None, "args": [], "launch": launch}
            program = os.path.basename(argv[0])
        args = argv[1:]
        if program in DECLARATIONS:
            for arg in args:
                match = ASSIGNMENT.match(arg)
                if match:
                    self.vars[match[1]] = match[2]
        elif program == "ps":
            self.ps = True
        elif program == "hermes-delegate" and args[:1] == ["cancel"]:
            self.cancel = True
        elif program in NESTED_SHELLS and self.depth < NEST_LIMIT:
            self.nested.extend(_Script(a, self.depth + 1) for a in args if " " in a or ";" in a)
        return {"program": program, "args": args, "launch": launch}

    def kinds(self, command, tainted):
        program, args = command.get("program"), command.get("args", [])
        kinds = set()
        if command.get("launch"):
            kinds.add("launch")
        if program == "sleep" and _sleep_seconds(args) >= 2:
            kinds.add("sleep")
        if program in ("pgrep", "pkill") and (program == "pgrep" or "-0" in args) and _names_delegation(args):
            kinds.add("peek")
        if program in PROCESS_GREPS and self.ps and _names_delegation(args):
            kinds.add("peek")
        if program in FILE_PEEKS or program in PROCESS_GREPS:
            if any(_references_state(a, tainted) for a in args):
                kinds.add("peek")
        if program == "gh":
            kinds.add(_gh_kind(args))
        elif program == "git" and args[:1] == ["push"]:
            kinds.add("trigger")
        if program in FILE_TESTS:
            for i, arg in enumerate(args[:-1]):
                if arg in ("-f", "-e", "-s"):
                    kinds.add("file_wait")
                    if MARKER_FILE.search(args[i + 1]):
                        kinds.add("marker_wait")
        if program in PROCESS_GREPS and "-q" in args and args and MARKER_FILE.search(args[-1]):
            kinds.update(("file_wait", "marker_wait"))
        if program in READINESS or any("healthz" in a for a in args):
            kinds.add("ready")
        if program is None or program in NEUTRAL or kinds & CHECKS:
            kinds.add("neutral")
        return kinds

    def hits(self, tainted):
        found = set()
        for script in self.nested:
            found |= script.hits(_state_tainted(script.vars, tainted))
        found |= self._walk(self.root, tainted, False)[0]
        if self.cancel:  # cancel-then-confirm is a one-shot check, not a poll
            found.discard("peek")
        return found

    def _walk(self, items, tainted, launched):
        """Return (hits, kinds seen, whether the list ran a non-neutral command)."""
        found, all_kinds, closes = set(), set(), False
        window, startup, triggered, ci = 0, False, False, False
        for item in items:
            if isinstance(item, _Loop):
                sub_found, kinds, item_closes = self._walk(item, tainted, launched)
                ci |= "ci" in sub_found
                found |= sub_found - {"ci"}
                if item.polling and "sleep" in kinds:
                    found |= kinds & {"peek"}
                    ci |= bool(kinds & {"ci", "ci_lookup"})
                    waits = "marker_wait" in kinds or ("file_wait" in kinds and "ready" not in kinds)
                    # A job launched earlier in this same call and awaited here is a foreground run.
                    if waits and not launched:
                        found.add("file_wait")
                slept = False
            else:
                kinds = self.kinds(item, tainted)
                item_closes = "neutral" not in kinds
                slept = "sleep" in kinds
            if window:
                found |= kinds & {"peek"}
                # A short delay after a trigger (or a watcher), or before a run-id lookup, is not a poll.
                ci |= not startup and ("ci" in kinds or ("ci_lookup" in kinds and window > STARTUP_DELAY))
            if "watch" in kinds:
                # Lookups and startup delays that feed a watcher are not polls.
                ci, window, triggered = False, 0, True
            elif slept:
                window = _sleep_seconds(item["args"])
                startup = triggered and window <= STARTUP_DELAY
            elif item_closes:
                window = 0
            triggered |= "trigger" in kinds
            launched |= "launch" in kinds
            closes |= item_closes
            all_kinds |= kinds
        if ci:
            found.add("ci")
        return found, all_kinds, closes


def _is_counter(items):
    """for i in $(seq ..) / {1..9} / 1 2 3 counts attempts; for n in 27 30 iterates over items."""
    if any(COUNTER.search(i) for i in items):
        return True
    if len(items) < 2 or items[0] not in ("0", "1"):
        return False
    return items == [str(n) for n in range(int(items[0]), int(items[0]) + len(items))]


def _sleep_seconds(args):
    best = 0
    for arg in args:
        match = SLEEP_ARG.match(arg)
        if match:
            best = max(best, float(match[1]) * SLEEP_UNITS[match[2]])
    return best


def _gh_kind(args):
    if args[:2] == ["run", "watch"] or (args[:2] == ["pr", "checks"] and "--watch" in args):
        return "watch"
    if args[:2] in (["run", "rerun"], ["workflow", "run"], ["pr", "create"], ["pr", "merge"],
                    ["pr", "comment"], ["pr", "ready"]):
        return "trigger"
    if args[:2] == ["run", "list"]:
        return "ci_lookup"
    if _is_ci_check(args):
        return "ci"
    return "neutral" if args[:2] == ["pr", "view"] else "other"


def _is_ci_check(args):
    if "--watch" in args:
        return False
    if args[:2] in (["pr", "checks"], ["run", "view"]):
        return True
    if args[:2] == ["pr", "view"] and any("statusCheckRollup" in a for a in args):
        return True
    return args[:1] == ["api"] and any("actions/runs" in a for a in args)


def _normalized(args):
    # "[c]odex exec" is the usual trick to keep grep from matching itself.
    return re.sub(r"\[(\w)\]", r"\1", " ".join(args))


def _names_delegation(args):
    text = _normalized(args)
    return any(needle in text for needle in PEEK_NEEDLES) or any(
        a in PEEK_NAMES for a in args if not a.startswith("-"))


def _state_tainted(variables, inherited=frozenset()):
    tainted = {k for k, v in variables.items() if STATE_MARKER in v} | set(inherited)
    for _ in range(4):
        grown = {k for k, v in variables.items()
                 if any(re.search(r"\$\{?%s\b" % re.escape(t), v) for t in tainted)} | tainted
        if grown == tainted:
            break
        tainted = grown
    return tainted


def _references_state(arg, tainted):
    if STATE_MARKER in arg:
        return True
    return any(name in tainted for name in re.findall(r"\$\{?([A-Za-z_][A-Za-z_0-9]*)", arg))


def sleep_poll_decision(command):
    """SLEEP_RULES decision for a foreground command (pairing rules in the module docstring)."""
    if not isinstance(command, str) or "sleep" not in command or not PREFILTER.search(command):
        return None
    script = _Script(command)
    found = script.hits(_state_tainted(script.vars))
    return next((decision for kind, decision in SLEEP_RULES if kind in found), None)


def _cap():
    raw = os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT", "")
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_CAP
    except (TypeError, ValueError):
        return DEFAULT_CAP


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _notifies_on_exit(args):
    notify = args.get("notify")
    if isinstance(notify, bool):
        on_exit = notify
    else:
        on_exit = notify is None and args.get("notify_on_complete") is True
    heartbeat = args.get("heartbeat")
    return on_exit or (isinstance(heartbeat, int) and not isinstance(heartbeat, bool) and heartbeat > 0)


def decide(payload):
    """Return (decision, directive): None, 'modified', 'opted-out', 'blocked' or a SLEEP_RULES decision."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "pre_tool_call":
        return None, None
    if payload.get("tool_name") != "terminal":
        return None, None
    args = payload.get("tool_input")  # Hermes sends tool args only here (None when not a dict)
    if not isinstance(args, dict):
        return None, None
    opted_out = args.get("continue_on_complete") is False

    if args.get("background"):
        if not _notifies_on_exit(args):
            return None, None
        if opted_out:
            return "opted-out", None
        if "continue_on_complete" in args:
            return None, None
        patch = {"continue_on_complete": True}
        if "persist_on_release" not in args:
            patch["persist_on_release"] = True
        return "modified", {"action": "modify", "args": patch}

    if not args.get("background"):
        timeout = args.get("timeout")
        if _is_number(timeout) and timeout > _cap() and opted_out:
            return "opted-out", None
        if _is_number(timeout) and timeout > _cap():
            message = (
                "bg-continue hook: foreground timeout %ss exceeds the %ss cap, so Hermes would promote "
                "this call to a background run that does NOT wake you when it ends. Re-run it as "
                "terminal(command=..., background=true, notify=true, continue_on_complete=true, "
                "persist_on_release=true), or keep it foreground with timeout <= %s. To opt out on "
                "purpose, pass continue_on_complete=false explicitly." % (timeout, _cap(), _cap())
            )
            return "blocked", {"action": "block", "message": message}
        decision = sleep_poll_decision(args.get("command"))
        if decision:
            return decision, {"action": "block", "message": MESSAGES[decision]}
    return None, None


def _log(decision, payload, error=None):
    path = os.path.expanduser(os.environ.get(LOG_ENV) or DEFAULT_LOG)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "session_id": payload.get("session_id") if isinstance(payload, dict) else None,
            "decision": decision,
            "error": error,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)  # os.open's mode only applies on creation
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        decision, directive = decide(payload)
    except Exception as exc:  # fail open, but leave a trace
        _log("error", payload, type(exc).__name__)
        return 0
    if decision is None:
        return 0
    _log(decision, payload)
    if directive is None:
        return 0
    sys.stdout.write(json.dumps(directive))
    return 2 if decision.startswith("blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
