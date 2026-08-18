"""Post-execution performance advisor for slow tool calls.

Trace-mined problem (2026-07-16, 2,605 turns): agents route heavy work
through ``terminal`` in shapes that have a much cheaper native equivalent —
full-tree scans (``Path.rglob``/``os.walk``/unpruned ``find``/bare
``grep -r``) over repos whose ``node_modules`` alone holds >1.7M files,
foreground ``sleep`` polling loops, and multi-minute foreground jobs that
pin a session worker.  The native ``search_files`` tool (ripgrep-backed,
.gitignore-aware) answers the same questions in ~1s, and background
processes with ``notify_on_complete`` re-enter the session for free.

This module is the thin corrective shot: after a tool call finishes, if it
was slow AND matches a known antipattern, append ONE advisory line to the
tool result.  Advisory-only — nothing is blocked or rewritten; the model
sees the note next to the slow result it just paid for, which is the
highest-leverage moment to teach the cheaper shape (the observed 88s scan
was followed by a second 63s scan of the same tree in the very next
iteration).

Knobs (read per call so gateway .env changes apply without restart):
  HERMES_PERF_ADVISOR=0             kill switch
  HERMES_PERF_ADVISOR_MIN_S         antipattern threshold (default 10s)
  HERMES_PERF_ADVISOR_FOREGROUND_S  long-foreground threshold (default 120s)
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

__all__ = ["perf_advisory"]

_RGLOB_WALK = re.compile(r"\brglob\s*\(|\bos\.walk\s*\(|\biterdir\s*\(\s*\).*recursive", re.S)
# find rooted at a broad path (absolute, ~, or .) — pruning flags exempt it.
_FIND_BROAD = re.compile(r"(?:^|[|;&(\s])find\s+(?:/(?!tmp\b)|~|\$HOME|\.(?:\s|$))")
_FIND_EXEMPT = re.compile(r"-prune\b|-maxdepth\b|node_modules")
# grep -r / -R without directory exclusions.
_GREP_R = re.compile(r"\bgrep\s+(?:-[\w-]+\s+)*-[a-zA-Z]*[rR]")
_GREP_EXEMPT = re.compile(r"--exclude-dir|node_modules")
_SLEEP = re.compile(r"\bsleep\s+(\d+(?:\.\d+)?)")


def _flag(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _threshold(name: str, default: float) -> float:
    try:
        return float(_flag(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tree_scan_hint(command: str) -> bool:
    if _RGLOB_WALK.search(command):
        return True
    if _FIND_BROAD.search(command) and not _FIND_EXEMPT.search(command):
        return True
    if _GREP_R.search(command) and not _GREP_EXEMPT.search(command):
        return True
    return False


def _sleep_poll_hint(command: str) -> bool:
    return any(float(m) >= 5 for m in _SLEEP.findall(command))


def perf_advisory(
    tool_name: str,
    tool_args: Dict[str, Any],
    duration_s: float,
    result_text: Optional[str] = None,
) -> Optional[str]:
    """Return an advisory line to append to a slow tool result, or None.

    Pure function of (tool, args, duration); never raises to the caller's
    benefit — any internal error means "no advisory".
    """
    try:
        if _flag("HERMES_PERF_ADVISOR", "1").strip().lower() in ("0", "false", "off"):
            return None
        if tool_name != "terminal" or not isinstance(tool_args, dict):
            return None
        command = tool_args.get("command")
        if not isinstance(command, str) or not command:
            return None

        min_s = _threshold("HERMES_PERF_ADVISOR_MIN_S", 10.0)
        foreground_s = _threshold("HERMES_PERF_ADVISOR_FOREGROUND_S", 120.0)

        if duration_s >= min_s and _tree_scan_hint(command):
            return (
                f"\n\n[perf-advisor] This command ran {duration_s:.0f}s and walks a "
                "full directory tree (rglob/os.walk/unpruned find/bare grep -r). "
                "Large repos here hold millions of node_modules files. Use the "
                "search_files tool instead (ripgrep-backed: respects .gitignore, "
                "skips node_modules/.git, typically <1s), or constrain the scan "
                "(rg, find -prune/-maxdepth, grep --exclude-dir). Do not repeat "
                "a broad scan of the same tree."
            )

        if duration_s >= min_s and _sleep_poll_hint(command):
            return (
                f"\n\n[perf-advisor] This command spent {duration_s:.0f}s including "
                "foreground sleep. Don't poll with sleep loops: run the job with "
                "background=true + notify_on_complete=true and end your turn — "
                "the completion re-enters the session automatically."
            )

        if duration_s >= foreground_s and not tool_args.get("background"):
            return (
                f"\n\n[perf-advisor] This foreground command blocked the turn for "
                f"{duration_s:.0f}s. For jobs this long use background=true with "
                "notify_on_complete=true (one process(action='wait') is fine), so "
                "the session worker isn't pinned and you keep working or end the "
                "turn cleanly."
            )
        return None
    except Exception:
        return None
