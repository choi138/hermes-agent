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
  C. any terminal command that both sleeps (sleep N, N >= 2) and peeks at a delegation (pgrep/ps/
     pkill -0 for codex exec, claude -p, run_codex_task or hermes-delegate, or ls/cat/tail/head/wc/
     find/stat/test on ~/.local/state/hermes-delegate) -> block, logged as "blocked_peek".
     Completion already arrives through the hermes-delegate watcher; sleep-polling burned turns
     (2026-10-23..26). hermes-delegate wait/status/cancel are the official paths and never peeks.

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
NESTED_SHELLS = {"bash", "sh", "zsh", "ssh"}
SKIP_WORDS = {"if", "then", "elif", "else", "while", "until", "do", "!", "time", "command", "exec"}
DECLARATIONS = {"export", "local", "typeset", "declare", "readonly"}
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$", re.S)
SLEEP_ARG = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
SLEEP_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
PREFILTER = re.compile(r"odex|laude|hermes-delegate")  # also matches the [c]odex grep trick
PEEK_BUDGET_SECONDS = 0.1
PEEK_SIZE_LIMIT = 64 * 1024
PEEK_MESSAGE = (
    "bg-continue hook: delegation sleep-poll blocked / 위임 작업을 sleep으로 폴링하지 마세요. "
    "After launched=yes or a printed codex argv, check at most once with "
    "`hermes-delegate status PATH` (launched=yes 또는 codex argv 출력 뒤 확인은 이것 한 번만). "
    "Completion arrives via the hermes-delegate watcher / gateway notification "
    "(완료는 watcher·게이트웨이 알림으로 옵니다); end the turn instead of sleeping."
)
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
    for segment in re.split(r"\$\(|[;&|\n()`]", text):
        argv = [w.strip("\"'") for w in segment.split()]
        if argv:
            yield argv, []


def _commands(text):
    """Yield (argv, nested scripts) per simple command; nested = substitutions and -c/ssh bodies."""
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


def _is_sleep(argv):
    for arg in argv[1:]:
        match = SLEEP_ARG.match(arg)
        if match and float(match[1]) * SLEEP_UNITS[match[2]] >= 2:
            return True
    return False


def _normalized(args):
    # "[c]odex exec" is the usual trick to keep grep from matching itself.
    return re.sub(r"\[(\w)\]", r"\1", " ".join(args))


def _names_delegation(args):
    text = _normalized(args)
    return any(needle in text for needle in PEEK_NEEDLES) or any(
        a in PEEK_NAMES for a in args if not a.startswith("-"))


def _scan(text, facts, depth=0):
    if depth > 8:
        return
    for argv, nested in _commands(text):
        for body in nested:
            _scan(body, facts, depth + 1)
        while argv and (argv[0] in SKIP_WORDS or ASSIGNMENT.match(argv[0])):
            match = ASSIGNMENT.match(argv[0])
            if match:
                facts["vars"][match[1]] = match[2]
            argv = argv[1:]
        if not argv:
            continue
        program, args = os.path.basename(argv[0]), argv[1:]
        if program in DECLARATIONS:
            for arg in args:
                match = ASSIGNMENT.match(arg)
                if match:
                    facts["vars"][match[1]] = match[2]
        elif program == "for" and len(args) >= 2 and args[1] == "in":
            facts["vars"][args[0]] = " ".join(args[2:])
        elif program == "sleep" and _is_sleep(argv):
            facts["sleep"] = True
        elif program == "hermes-delegate" and args[:1] == ["cancel"]:
            facts["cancel"] = True
        elif program in ("pgrep", "pkill"):
            if (program == "pgrep" or "-0" in args) and _names_delegation(args):
                facts["process_peek"] = True
        elif program == "ps":
            facts["ps"] = True
            facts["ps_match"] |= _names_delegation(args)
        elif program in PROCESS_GREPS:
            facts["ps_match"] |= _names_delegation(args)
            facts["file_args"].extend(args)
        elif program in FILE_PEEKS:
            facts["file_args"].extend(args)
        elif program in NESTED_SHELLS:
            for arg in args:
                if " " in arg or ";" in arg:
                    _scan(arg, facts, depth + 1)


def _state_tainted(variables):
    tainted = {k for k, v in variables.items() if STATE_MARKER in v}
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


def is_delegation_peek(command):
    if not isinstance(command, str) or "sleep" not in command or not PREFILTER.search(command):
        return False
    facts = {"vars": {}, "sleep": False, "cancel": False, "process_peek": False,
             "ps": False, "ps_match": False, "file_args": []}
    _scan(command, facts)
    if not facts["sleep"] or facts["cancel"]:
        return False
    if facts["process_peek"] or (facts["ps"] and facts["ps_match"]):
        return True
    tainted = _state_tainted(facts["vars"])
    return any(_references_state(arg, tainted) for arg in facts["file_args"])


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
    """Return (decision, directive): None, 'modified', 'opted-out', 'blocked' or 'blocked_peek'."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "pre_tool_call":
        return None, None
    if payload.get("tool_name") != "terminal":
        return None, None
    args = payload.get("tool_input")  # Hermes sends tool args only here (None when not a dict)
    if not isinstance(args, dict):
        return None, None
    if is_delegation_peek(args.get("command")):
        return "blocked_peek", {"action": "block", "message": PEEK_MESSAGE}
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
    return 2 if decision in ("blocked", "blocked_peek") else 0


if __name__ == "__main__":
    sys.exit(main())
