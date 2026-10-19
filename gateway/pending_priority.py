"""Ordering rules for a session's pending queue: human turns first, process notifications batched.

A busy session's queue is the adapter slot head, its ``_gateway_pending_followups`` chain, and
the runner's overflow FIFO. Internal wakes (background-process completions, watch matches) share
that queue with human messages; served strictly FIFO, a burst of wakes made the human wait behind
one full agent turn per wake. Storage stays FIFO; only the pick order changes, and nothing is dropped.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

# Ledger-bound wakes: each owns one durable turn marker, so they never fold into another turn.
_LEDGER_MARKER_KEYS = ("process_followup", "process_continuation")
_PROCESS_NOTIFICATION_ORIGIN = "process_registry_synthetic"


def is_deferrable_internal(event: Any) -> bool:
    """Internal wake that may yield to a human turn. Recovery tickets keep their place."""
    return bool(getattr(event, "internal", False)) and not hasattr(event, "_hermes_turn_resume")


def human_first(events: Iterable[Any]) -> list:
    """Stable partition: non-deferrable events keep arrival order ahead of deferrable wakes."""
    return sorted(events, key=is_deferrable_internal)


def _flatten(events: Iterable[Any]) -> list:
    # A re-queued head can carry its own followups; relinking must not drop them.
    flat = []
    for event in events:
        flat.append(event)
        flat.extend(_flatten(getattr(event, "_gateway_pending_followups", ())))
    return flat


def pending_chain(pending_messages: dict, session_key: str) -> list:
    head = pending_messages.get(session_key)
    return [] if head is None else _flatten([head])


def relink_chain(pending_messages: dict, session_key: str, events: Sequence[Any]) -> None:
    for event in events:
        if hasattr(event, "_gateway_pending_followups"):
            delattr(event, "_gateway_pending_followups")
    if not events:
        pending_messages.pop(session_key, None)
        return
    head, *rest = events
    if rest:
        head._gateway_pending_followups = list(rest)
    pending_messages[session_key] = head


def pop_human_first(pending_messages: dict, session_key: str) -> Optional[Any]:
    chain = pending_chain(pending_messages, session_key)
    if not chain:
        return None
    event, *rest = human_first(chain)
    relink_chain(pending_messages, session_key, rest)
    if hasattr(event, "_gateway_pending_followups"):
        delattr(event, "_gateway_pending_followups")
    return event


def requeue_front(pending_messages: dict, session_key: str, event: Any) -> None:
    """Put a dequeued-but-unrun event back at the head without displacing the existing chain."""
    relink_chain(pending_messages, session_key, [*_flatten([event]), *pending_chain(pending_messages, session_key)])


def prefer_queued_human(pending_messages: dict, session_key: str, picked: Optional[Any]) -> Optional[Any]:
    """Swap a picked wake for the oldest human turn still queued in the slot chain."""
    if picked is None or not is_deferrable_internal(picked):
        return picked
    chain = pending_chain(pending_messages, session_key)
    human = next((event for event in chain if not is_deferrable_internal(event)), None)
    if human is None:
        return picked
    relink_chain(pending_messages, session_key, [picked, *(event for event in chain if event is not human)])
    return human


def discard_human_keep_wakes(pending_messages: dict, session_key: str, overflow: Optional[list]) -> Optional[Any]:
    """/stop: drop the oldest queued human follow-up, keep every internal wake queued.

    Wakes were claim-settled on admission, so losing one idles its work until the next message.
    """
    chain = pending_chain(pending_messages, session_key)
    human = next((event for event in chain if not getattr(event, "internal", False)), None)
    rest = [event for event in chain if event is not human]
    if not rest and overflow:
        wake = next((event for event in overflow if getattr(event, "internal", False)), None)
        if wake is not None:
            overflow.remove(wake)
            rest = [wake]
    relink_chain(pending_messages, session_key, rest)
    return human


def is_refusable_wake(event: Any) -> bool:
    """Process-registry wakes retry on refusal; other internal producers may not."""
    return (is_deferrable_internal(event)
            and (getattr(event, "metadata", None) or {}).get("notification_origin") == _PROCESS_NOTIFICATION_ORIGIN)


def queued_count(pending_messages: dict, session_key: str, overflow: Optional[list]) -> tuple[int, int]:
    """``(total, deferrable_internal)`` across the slot chain and the overflow FIFO."""
    events = [*pending_chain(pending_messages, session_key), *(overflow or ())]
    return len(events), sum(1 for event in events if is_deferrable_internal(event))


def _is_plain_process_notification(event: Any) -> bool:
    metadata = getattr(event, "metadata", None) or {}
    return (
        is_refusable_wake(event)
        and not metadata.get("gateway_completion_ids")
        and not any(metadata.get(key) for key in _LEDGER_MARKER_KEYS)
        and not getattr(event, "media_urls", None)
    )


def _same_route(head: Any, other: Any, security_keys: Sequence[str]) -> bool:
    head_meta = getattr(head, "metadata", None) or {}
    other_meta = getattr(other, "metadata", None) or {}
    head_source, other_source = getattr(head, "source", None), getattr(other, "source", None)
    return (
        getattr(head_source, "chat_id", None) == getattr(other_source, "chat_id", None)
        and getattr(head_source, "thread_id", None) == getattr(other_source, "thread_id", None)
        and getattr(head, "allow_gateway_control", True) == getattr(other, "allow_gateway_control", True)
        and all(head_meta.get(key) == other_meta.get(key) for key in security_keys)
    )


def absorb_process_notifications(
    pending_messages: dict, session_key: str, head: Any, security_keys: Sequence[str],
) -> int:
    """Fold queued marker-less process notifications into ``head`` so one turn reads them all.

    Ledger-bound wakes stay out on both sides: a continuation whose admission is skipped ends its
    turn silently and would take absorbed text with it, and verification follow-ups run muted.
    """
    if not _is_plain_process_notification(head):
        return 0
    chain = pending_chain(pending_messages, session_key)
    absorbed = [event for event in chain
                if _is_plain_process_notification(event) and _same_route(head, event, security_keys)]
    if not absorbed:
        return 0
    relink_chain(pending_messages, session_key,
                 [event for event in chain if not any(event is taken for taken in absorbed)])
    head.text = "\n\n".join(text for text in (head.text, *(event.text for event in absorbed)) if text)
    for event in absorbed:
        head.absorb_processing_event(event)
    return len(absorbed)
