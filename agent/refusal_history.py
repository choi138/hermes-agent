"""History anchoring for refusal-driven model hops."""

from __future__ import annotations


def current_user_ordinal_from_tail(
    messages: list[dict] | None,
    current_turn_user_idx: int,
    *,
    keep_user_turns: int = 5,
) -> int | None:
    """Return the current user row's 1-based user ordinal from the tail.

    Synthetic recovery/nudge users may be appended after the real turn user,
    so "the last user" is not a safe anchor. ``keep_user_turns`` bounds this
    reverse search; a stale or out-of-range anchor fails closed with ``None``.
    """
    if not messages or not isinstance(current_turn_user_idx, int):
        return None
    if not 0 <= current_turn_user_idx < len(messages):
        return None
    if messages[current_turn_user_idx].get("role") != "user":
        return None
    try:
        limit = int(keep_user_turns)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    ordinal = 1 + sum(
        1
        for message in messages[current_turn_user_idx + 1 :]
        if isinstance(message, dict) and message.get("role") == "user"
    )
    return ordinal if ordinal <= limit else None


def user_anchor_from_tail(
    messages: list[dict] | None,
    user_from_tail: int,
    *,
    keep_user_turns: int = 5,
) -> int | None:
    """Find a bounded Nth user-role anchor from the end of ``messages``."""
    if not messages:
        return None
    try:
        ordinal = int(user_from_tail)
        limit = int(keep_user_turns)
    except (TypeError, ValueError):
        return None
    if ordinal <= 0 or limit <= 0 or ordinal > limit:
        return None
    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            seen += 1
            if seen == ordinal:
                return index
    return None
