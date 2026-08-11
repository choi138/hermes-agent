"""History shaping for refusal-driven model hops."""

from __future__ import annotations

from typing import Any


def clean_fork_messages(
    messages: list[dict] | None,
    *,
    keep_user_turns: int = 5,
) -> list[dict]:
    """Return leading system messages plus the last requested user turns.

    Assistant and tool messages are deliberately excluded so a model selected
    after a safety refusal does not inherit the prior model's refusal framing
    or tool-derived policy narrative.  The input list and its dictionaries are
    never mutated.
    """
    if not messages:
        return []

    try:
        user_limit = max(0, int(keep_user_turns))
    except (TypeError, ValueError):
        user_limit = 0

    leading_system: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        leading_system.append(dict(message))

    users = [
        dict(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    if user_limit == 0:
        users = []
    else:
        users = users[-user_limit:]

    return [*leading_system, *users]
