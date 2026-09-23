"""Normalize a persisted interrupted turn before re-entering the agent loop.

Only the transcript tail is changed. An already composed answer is returned for
delivery without another provider call; an unanswered side-effecting tool call
gets an UNKNOWN-effect result instead of being executed again.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agent.replay_cleanup import strip_dangling_tool_call_tail


INTERRUPT_CLOSER_PREFIX = "Operation interrupted"


def is_interrupt_closer_message(msg: Any) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
        return False
    content = msg.get("content")
    return isinstance(content, str) and content.strip().startswith(INTERRUPT_CLOSER_PREFIX)


def _is_empty_assistant_message(msg: Any) -> bool:
    if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
        return False
    content = msg.get("content")
    return content is None or (isinstance(content, str) and not content.strip())


def prepare_resume_history(
    history: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return ``(normalized_history, already_composed_final)`` without mutating input."""
    normalized = list(history or [])
    while normalized and (
        is_interrupt_closer_message(normalized[-1])
        or _is_empty_assistant_message(normalized[-1])
    ):
        normalized.pop()
    if not normalized:
        return normalized, None

    normalized = strip_dangling_tool_call_tail(normalized)
    if not normalized:
        return normalized, None

    tail = normalized[-1]
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
    if not history:
        return "empty"
    tail = history[-1]
    role = tail.get("role") if isinstance(tail, dict) else "?"
    if role == "tool":
        return "tool-tail"
    if role == "assistant" and isinstance(tail, dict) and tail.get("tool_calls"):
        return "unanswered-tool-calls"
    return f"{role}-tail"
