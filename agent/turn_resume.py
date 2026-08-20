"""Same-turn resume helpers for durable gateway turns.

When the gateway process stops mid-turn (deploy restart, drain timeout,
SIGKILL), the transcript layer already preserves everything that matters:
messages are flushed to SQLite incrementally, assistant ``tool_calls`` blocks
are persisted *before* their tools execute, and ``agent/replay_cleanup.py``
knows how to surface unanswered side-effecting calls as UNKNOWN-effect
orphan-recovery results instead of erasing them.

What was missing is an entry point that re-enters the conversation loop on
that interrupted transcript — the SAME turn, not a fabricated follow-up user
turn.  These helpers normalize a persisted transcript tail so
``run_conversation(..., resume_turn=True)`` can continue it:

* synthetic interrupt closers ("Operation interrupted…") appended by shutdown
  paths purely to keep role alternation are dropped — on a real resume the raw
  tool tail is exactly what the model must see;
* a trailing ``assistant(tool_calls)`` block with missing tool results is
  completed through the existing orphan-recovery semantics
  (``strip_dangling_tool_call_tail``);
* a turn that had already composed its final assistant text but was killed
  before delivery/finalize is recognized so the caller can deliver that text
  directly instead of re-invoking the model.

Everything here is a pure function over message dicts; no agent state is
touched.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from agent.replay_cleanup import strip_dangling_tool_call_tail

logger = logging.getLogger(__name__)

# Prefix used by every synthetic assistant row the interrupt paths append to
# close an aborted turn ("Operation interrupted.", "Operation interrupted:
# waiting for model response (94.0s elapsed).", "Operation interrupted:
# handling API error…").  These rows exist only to keep user/assistant
# alternation valid for the LEGACY new-turn recovery path; a same-turn resume
# must remove them so the transcript tail is the actual interrupted work.
INTERRUPT_CLOSER_PREFIX = "Operation interrupted"


def is_interrupt_closer_message(msg: Any) -> bool:
    """Return True for a synthetic assistant interrupt-closer row."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    if msg.get("tool_calls"):
        return False
    content = msg.get("content")
    return isinstance(content, str) and content.strip().startswith(
        INTERRUPT_CLOSER_PREFIX
    )


def _is_empty_assistant_message(msg: Any) -> bool:
    """True for an assistant row with neither content nor tool calls."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    if msg.get("tool_calls"):
        return False
    content = msg.get("content")
    if content is None:
        return True
    return isinstance(content, str) and not content.strip()


def prepare_resume_history(
    history: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Normalize an interrupted transcript for same-turn re-entry.

    Returns ``(normalized_history, composed_final)``:

    * ``composed_final`` is the final assistant text when the interrupted turn
      had already finished generating (tail is a genuine assistant text row) —
      the caller should deliver it as the turn's response without another
      model call.  Note: an *interim* assistant text emitted between tool
      rounds is indistinguishable by shape; delivering it is the conservative
      choice (the user can nudge), re-calling on an assistant tail would rely
      on provider-specific prefill semantics.
    * otherwise ``composed_final`` is ``None`` and the history tail is ready
      for the next model call (tool results present for every persisted
      ``tool_calls`` id, courtesy of orphan recovery).

    The input list is not mutated.
    """
    normalized = list(history or [])

    # 1. Drop trailing synthetic closers / empty assistant rows.  Only the
    #    tail is inspected: closers deeper in the history belong to OLD
    #    interrupted turns that a later real turn already moved past.
    while normalized and (
        is_interrupt_closer_message(normalized[-1])
        or _is_empty_assistant_message(normalized[-1])
    ):
        normalized.pop()

    if not normalized:
        return normalized, None

    # 2. Complete an unanswered trailing ``assistant(tool_calls)`` block.
    #    Side-effecting calls get UNKNOWN-effect orphan-recovery results;
    #    a read-only block is stripped (cheap to redo).
    normalized = strip_dangling_tool_call_tail(normalized)

    if not normalized:
        return normalized, None

    tail = normalized[-1]

    # 3. Turn had already composed its final response → deliver, don't re-run.
    if (
        isinstance(tail, dict)
        and tail.get("role") == "assistant"
        and not tail.get("tool_calls")
        and isinstance(tail.get("content"), str)
        and tail["content"].strip()
    ):
        return normalized, tail["content"]

    return normalized, None


def resume_entry_reason(history: List[Dict[str, Any]]) -> str:
    """Small diagnostic label for logs: what kind of tail we resumed from."""
    if not history:
        return "empty"
    tail = history[-1]
    role = tail.get("role") if isinstance(tail, dict) else "?"
    if role == "tool":
        return "tool-tail"
    if role == "assistant" and isinstance(tail, dict) and tail.get("tool_calls"):
        return "unanswered-tool-calls"
    if role == "assistant":
        return "assistant-tail"
    if role == "user":
        return "user-tail"
    return f"{role}-tail"
