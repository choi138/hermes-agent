"""Eligibility for automatic post-turn memory and skill review."""

from __future__ import annotations

from typing import Any, Optional


def is_primary_foreground_agent(agent: Any) -> bool:
    """Exclude delegated workers and internal forks even when they share a platform."""
    if int(getattr(agent, "_delegate_depth", 0) or 0) > 0:
        return False
    if str(getattr(agent, "platform", "") or "").strip().lower() == "subagent":
        return False
    if bool(getattr(agent, "_persist_disabled", False)):
        return False

    origin = str(getattr(agent, "_memory_write_origin", "") or "").strip().lower()
    context = str(getattr(agent, "_memory_write_context", "") or "").strip().lower()
    return origin != "background_review" and context != "background_review"


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
    """Require a healthy top-level final; fallback text alone is not success."""
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
