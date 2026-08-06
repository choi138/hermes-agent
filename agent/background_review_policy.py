"""Shared eligibility policy for automatic memory/skill reviews.

The self-improvement review is an auxiliary, post-turn activity.  It must not
be inherited by delegated workers or by the review fork itself, and it must not
learn from partial/error recovery transcripts.  Keeping these checks in one
module prevents the chat-completions and Codex runtimes from drifting apart.
"""

from __future__ import annotations

from typing import Any, Optional


def is_primary_foreground_agent(agent: Any) -> bool:
    """Return whether *agent* owns a user-facing, top-level turn.

    Delegated agents are identified both by depth and platform because older
    call sites did not consistently set both fields.  ``_persist_disabled`` and
    the write-origin checks exclude internal review/curator forks even if their
    platform mirrors the parent for prompt-cache parity.
    """

    if int(getattr(agent, "_delegate_depth", 0) or 0) > 0:
        return False
    if str(getattr(agent, "platform", "") or "").strip().lower() == "subagent":
        return False
    if bool(getattr(agent, "_persist_disabled", False)):
        return False

    origin = str(getattr(agent, "_memory_write_origin", "") or "").strip().lower()
    context = str(getattr(agent, "_memory_write_context", "") or "").strip().lower()
    if origin == "background_review" or context == "background_review":
        return False
    return True


def is_successful_review_outcome(
    agent: Any,
    *,
    final_response: Optional[str],
    completed: bool,
    failed: bool = False,
    interrupted: bool = False,
    exit_reason: Optional[str] = None,
    cleanup_failed: bool = False,
) -> bool:
    """Return whether a finished turn is safe input for self-improvement.

    A short fallback response can make a failed turn look superficially
    complete.  The normal chat runtime therefore also has to prove a healthy
    terminal reason.  Codex has no equivalent reason string, so callers omit it
    and rely on its explicit ``error``/``interrupted`` outcome.
    """

    if not is_primary_foreground_agent(agent):
        return False
    if not completed or failed or interrupted or cleanup_failed:
        return False
    if not isinstance(final_response, str) or not final_response.strip():
        return False

    if exit_reason is None:
        return True

    reason = str(exit_reason)
    return reason.startswith("text_response(") or reason == "kanban_terminal"


__all__ = [
    "is_primary_foreground_agent",
    "is_successful_review_outcome",
]
