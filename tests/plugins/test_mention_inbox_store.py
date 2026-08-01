"""Behavior tests for the profile-scoped mention inbox store."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.mention_inbox import (
    ApprovalState,
    MentionEvent,
    ingest_event,
)
from plugins.mention_inbox.store import MentionInboxStore


def _event(*, body: str = "Please review this pull request.") -> MentionEvent:
    payload: dict[str, Any] = {
        "schema_version": "1",
        "source": {"platform": "github", "event_id": "notification-123"},
        "actor": {"actor_id": "U_actor", "kind": "user"},
        "target": {"target_id": "U_target", "kind": "user"},
        "thread": {"thread_id": "PR_thread", "container_id": "R_repo"},
        "requested_action": "review",
        "deadline": None,
        "untrusted": {
            "title": "Review requested",
            "body": body,
            "action_detail": "review_requested",
            "source_url": "https://github.com/org/repo/pull/7",
            "metadata": {
                "reason": "review_requested",
                "repository": "org/repo",
            },
        },
    }
    return ingest_event(payload)


def test_store_inserts_and_restores_canonical_event(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    event = _event()

    result = store.upsert(
        event,
        source_revision="2026-07-28T11:20:30Z",
    )
    stored = store.get(event.dedupe_key)

    assert result.created is True
    assert result.content_changed is True
    assert result.stale is False
    assert result.revision_number == 1
    assert store.count() == 1
    assert stored is not None
    assert stored.event == event
    assert stored.source_revision == "2026-07-28T11:20:30Z"
    assert stored.revision_number == 1
    assert stored.first_seen_at == now
    assert stored.last_seen_at == now


def test_same_content_updates_seen_and_source_revision_without_duplicate(
    tmp_path: Path,
) -> None:
    first = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 28, 12, 5, tzinfo=timezone.utc)
    current = [first]
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: current[0],
    )
    event = _event()
    store.upsert(event, source_revision="2026-07-28T11:20:30Z")

    current[0] = later
    result = store.upsert(event, source_revision="2026-07-28T11:25:30Z")
    stored = store.get(event.dedupe_key)

    assert result.created is False
    assert result.content_changed is False
    assert result.stale is False
    assert result.revision_number == 1
    assert store.count() == 1
    assert stored is not None
    assert stored.source_revision == "2026-07-28T11:25:30Z"
    assert stored.revision_number == 1
    assert stored.first_seen_at == first
    assert stored.last_seen_at == later


def test_local_approval_persists_across_identical_recollection(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "mention-inbox.db")
    event = _event()
    store.upsert(event, source_revision="2026-07-28T11:20:30Z")

    approved = store.transition_approval(
        event.dedupe_key,
        ApprovalState.APPROVED,
    )
    store.upsert(event, source_revision="2026-07-28T11:25:30Z")
    stored = store.get(event.dedupe_key)

    assert approved.approval_state is ApprovalState.APPROVED
    assert stored is not None
    assert stored.event.approval_state is ApprovalState.APPROVED
    assert stored.revision_number == 1


def test_content_change_increments_revision_and_resets_approval(tmp_path: Path) -> None:
    first = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 28, 12, 5, tzinfo=timezone.utc)
    current = [first]
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: current[0],
    )
    original = _event(body="Original body")
    store.upsert(original, source_revision="2026-07-28T11:20:30Z")
    store.transition_approval(original.dedupe_key, ApprovalState.APPROVED)

    current[0] = later
    revised = _event(body="Revised body")
    result = store.upsert(revised, source_revision="2026-07-28T11:25:30Z")
    stored = store.get(original.dedupe_key)

    assert result.created is False
    assert result.content_changed is True
    assert result.stale is False
    assert result.revision_number == 2
    assert store.count() == 1
    assert stored is not None
    assert stored.event.untrusted.body == "Revised body"
    assert stored.event.approval_state is ApprovalState.PENDING
    assert stored.source_revision == "2026-07-28T11:25:30Z"
    assert stored.revision_number == 2
    assert stored.first_seen_at == first
    assert stored.last_seen_at == later


def test_older_source_revision_cannot_overwrite_newer_event(tmp_path: Path) -> None:
    first = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 28, 12, 5, tzinfo=timezone.utc)
    current = [first]
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: current[0],
    )
    newest = _event(body="Newest body")
    store.upsert(newest, source_revision="2026-07-28T11:25:30Z")
    store.transition_approval(newest.dedupe_key, ApprovalState.APPROVED)

    current[0] = later
    stale_event = _event(body="Stale body")
    result = store.upsert(stale_event, source_revision="2026-07-28T11:20:30Z")
    stored = store.get(newest.dedupe_key)

    assert result.created is False
    assert result.content_changed is False
    assert result.stale is True
    assert result.revision_number == 1
    assert stored is not None
    assert stored.event.untrusted.body == "Newest body"
    assert stored.event.approval_state is ApprovalState.APPROVED
    assert stored.source_revision == "2026-07-28T11:25:30Z"
    assert stored.revision_number == 1
    assert stored.first_seen_at == first
    assert stored.last_seen_at == later


def test_collector_cursor_round_trips_and_updates(tmp_path: Path) -> None:
    path = tmp_path / "mention-inbox.db"
    store = MentionInboxStore(path)

    assert store.get_cursor("github.notifications") is None

    store.set_cursor(
        "github.notifications",
        "Tue, 28 Jul 2026 11:20:30 GMT",
    )
    assert store.get_cursor("github.notifications") == ("Tue, 28 Jul 2026 11:20:30 GMT")

    reloaded = MentionInboxStore(path)
    reloaded.set_cursor(
        "github.notifications",
        "Tue, 28 Jul 2026 11:25:30 GMT",
    )
    assert reloaded.get_cursor("github.notifications") == (
        "Tue, 28 Jul 2026 11:25:30 GMT"
    )


def test_collector_status_persists_failures_and_resets_on_success(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    current = [now]
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: current[0],
    )
    first_retry = datetime(2026, 7, 28, 12, 1, tzinfo=timezone.utc)

    first = store.record_poll_failure(
        "github.notifications",
        error_category="server_error",
        next_poll_at=first_retry,
    )
    second = store.record_poll_failure(
        "github.notifications",
        error_category="server_error",
        next_poll_at=first_retry,
    )

    assert first.status == "error"
    assert first.error_category == "server_error"
    assert first.consecutive_failures == 1
    assert first.last_success_at is None
    assert first.next_poll_at == first_retry
    assert second.consecutive_failures == 2

    current[0] = first_retry
    next_poll = datetime(2026, 7, 28, 12, 2, tzinfo=timezone.utc)
    success = store.record_poll_success(
        "github.notifications",
        next_poll_at=next_poll,
    )
    reloaded = MentionInboxStore(tmp_path / "mention-inbox.db")

    assert success.status == "ok"
    assert success.error_category is None
    assert success.consecutive_failures == 0
    assert success.last_attempt_at == first_retry
    assert success.last_success_at == first_retry
    assert success.next_poll_at == next_poll
    assert reloaded.get_collector_status("github.notifications") == success
