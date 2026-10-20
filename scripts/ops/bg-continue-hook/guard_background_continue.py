#!/usr/bin/env python3
"""pre_tool_call hook: make waited-on background terminal runs wake the coordinator.

Why: a background terminal run launched with notify but without continue_on_complete only
opens a read-only verification turn when it exits, so the approved work stalls until the owner
sends a message. The rule lived in a skill and was forgotten repeatedly (2026-10-19..20).

Rules (terminal tool only; Hermes semantics verified in tools/terminal_tool.py of the live tree):
  A. background=true, completion notification on (notify is True, or notify_on_complete is True,
     or heartbeat > 0), and the caller did NOT pass continue_on_complete at all
     -> modify: continue_on_complete=true, plus persist_on_release=true when not passed.
  B. foreground (background falsy), numeric timeout above the foreground cap (600 s, or
     $TERMINAL_MAX_FOREGROUND_TIMEOUT), and continue_on_complete not passed
     -> block: Hermes would silently promote it to an unarmed background run.
  An explicit continue_on_complete (true or false) is always respected: that is the opt-out.
  notify as a pattern list (watch_patterns) is never touched: it does not notify on exit.

Exit 0 = allow (stdout empty, or a modify directive). Exit 2 = block. Any internal error or
malformed stdin = allow (fail open). The log never contains the command text.
"""
import datetime
import json
import os
import sys

DEFAULT_CAP = 600
LOG_ENV = "BG_CONTINUE_HOOK_LOG"
DEFAULT_LOG = "~/.hermes/profiles/koharu/logs/bg-continue-hook.jsonl"


def _cap():
    raw = os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT", "")
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_CAP
    except (TypeError, ValueError):
        return DEFAULT_CAP


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def decide(payload):
    """Return (decision, directive) where decision is None, 'modified' or 'blocked'."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "pre_tool_call":
        return None, None
    if payload.get("tool_name") != "terminal":
        return None, None
    args = payload.get("tool_input")  # Hermes sends tool args only here (None when not a dict)
    if not isinstance(args, dict):
        return None, None
    if "continue_on_complete" in args:
        return None, None

    if args.get("background") is True:
        heartbeat = args.get("heartbeat")
        notify_on_exit = (
            args.get("notify") is True
            or args.get("notify_on_complete") is True
            or (_is_number(heartbeat) and heartbeat > 0)
        )
        if not notify_on_exit:
            return None, None
        patch = {"continue_on_complete": True}
        if "persist_on_release" not in args:
            patch["persist_on_release"] = True
        return "modified", {"action": "modify", "args": patch}

    if not args.get("background"):
        timeout = args.get("timeout")
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
    sys.stdout.write(json.dumps(directive))
    return 2 if decision == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
