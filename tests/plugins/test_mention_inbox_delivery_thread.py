"""Alert delivery and anchored-thread bootstrap reconciliation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    render_discord_event,
)
from plugins.mention_inbox.thread_session import (
    MentionInboxThreadCoordinator,
    _proposal_content,
)

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1531851208858275860"


def _event(
    *,
    kind: str = "own_pr_review_comment",
    body: str = "이 줄을 확인해 주세요.",
    source_revision: str = "2026-07-29T10:01:00Z",
    include_source_revision_metadata: bool = True,
):
    metadata = {
        "actionable_kind": kind,
        "repository": "silviahealth/content",
        "subject_type": "PullRequest",
        "subject_number": 7,
        "subject_key": "github:R_repo:PR_7",
        "subject_head_sha": "head-1",
        "actor_login": "alice",
    }
    if include_source_revision_metadata:
        metadata["source_revision"] = source_revision
        metadata["preapproval_brief"] = {
            "schema_version": 1,
            "disposition": "review_needed",
            "summary": body,
            "findings": [
                {
                    "source_event_id": "RC_123",
                    "body": body,
                    "source_url": (
                        "https://github.com/silviahealth/content/pull/7"
                        "#discussion_r123"
                    ),
                    "path": "plugins/mention_inbox/voice.py",
                    "line": 181,
                    "review_id": None,
                    "commit_id": "head-1",
                }
            ],
            "source_revision": source_revision,
            "head_sha": "head-1",
            "approvable": True,
        }
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": "RC_123"},
        "actor": {"actor_id": "U_alice", "kind": "user"},
        "target": {"target_id": "U_recent", "kind": "user"},
        "thread": {"thread_id": "github:R_repo:PR_7", "container_id": "R_repo"},
        "requested_action": "reply",
        "deadline": None,
        "untrusted": {
            "title": "Inbox contract",
            "body": body,
            "action_detail": kind,
            "source_url": "https://github.com/silviahealth/content/pull/7#discussion_r123",
            "metadata": metadata,
        },
    })


class _Discord:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.history: list[tuple[str, str]] = []
        self.marker_searches = 0
        self.marker_search_enabled = True
        self.marker_search_error = False

    async def find_marker(
        self, channel_id: str, marker: str, *, limit: int
    ) -> str | None:
        self.marker_searches += 1
        if self.marker_search_error:
            raise RuntimeError("marker lookup failed")
        if not self.marker_search_enabled:
            return None
        for message_id, content in self.history[-limit:]:
            if marker in content:
                return message_id
        return None

    async def send(
        self, channel_id: str, content: str, *, allowed_mentions: dict[str, Any]
    ) -> str:
        self.sends.append(content)
        self.history.append(("parent-1", content))
        return "parent-1"


class _Coordinator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def ensure_thread(
        self, event, *, parent_message_id: str, source_revision: str
    ):
        self.calls.append((event.dedupe_key, parent_message_id, source_revision))
        if self.fail:
            raise RuntimeError("thread bootstrap failed")
        return object()


class _ThreadDiscord:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.messages: list[tuple[str, str]] = []

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        return self.thread_id

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str:
        self.thread_id = "thread-1"
        return self.thread_id

    def mark_thread_participation(self, thread_id: str) -> None:
        return None

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        return next(
            (message_id for message_id, existing in self.messages[-limit:] if existing == content),
            None,
        )

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        message_id = f"proposal-{len(self.messages) + 1}"
        self.messages.append((message_id, content))
        return message_id


def test_proposal_content_uses_concrete_preflight_for_review_and_assignment() -> None:
    review = _proposal_content(_event(kind="review_requested"))
    assignment = _proposal_content(_event(kind="assigned"))

    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in str(review["goal"])
    assert "이 줄을 확인해 주세요" in str(review["goal"])
    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in str(assignment["goal"])


@pytest.mark.asyncio
async def test_thread_bootstrap_happens_before_delivery_is_marked_sent(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    coordinator = _Coordinator()
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "sent"
    assert coordinator.calls == [
        (_event().dedupe_key, "parent-1", "2026-07-29T10:01:00Z")
    ]
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_post_send_bootstrap_failure_reconciles_without_duplicate_alert(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    failing = _Coordinator(fail=True)
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=failing,
    )

    assert await delivery.deliver_once() == "error"
    assert len(discord.sends) == 1
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        persisted_parent = connection.execute(
            "SELECT message_id FROM delivery_outbox"
        ).fetchone()[0]
    finally:
        connection.close()
    assert persisted_parent == "parent-1"
    now[0] += timedelta(seconds=11)
    marker_searches = discord.marker_searches
    discord.marker_search_enabled = False

    recovered = _Coordinator()
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=recovered,
    )
    assert await delivery.deliver_once() == "reconciled"
    assert len(discord.sends) == 1
    assert discord.marker_searches == marker_searches
    assert recovered.calls == [
        (_event().dedupe_key, "parent-1", "2026-07-29T10:01:00Z")
    ]
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_uncertain_marker_failure_stays_reconcilable_without_duplicate_send(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    event = _event()
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    assert store.claim_delivery(DESTINATION, lease_seconds=10) is not None
    discord = _Discord()
    rendered = render_discord_event(
        event,
        revision_number=1,
        destination=DESTINATION,
    )
    discord.history.append(("parent-existing", rendered.content))
    discord.marker_search_error = True
    now[0] += timedelta(seconds=11)
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
    )

    assert await delivery.deliver_once() == "error"
    assert discord.sends == []
    discord.marker_search_error = False
    now[0] += timedelta(seconds=11)

    assert await delivery.deliver_once() == "reconciled"
    assert discord.sends == []
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_delivery_uses_exact_outbox_source_revision_for_thread_bootstrap(
    tmp_path: Path,
) -> None:
    first_revision = "2026-07-29T10:01:00Z"
    second_revision = "2026-07-29T10:02:00Z"
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    first_event = _event(
        body="revision one",
        source_revision=first_revision,
        include_source_revision_metadata=False,
    )
    second_event = _event(
        body="revision two",
        source_revision=second_revision,
        include_source_revision_metadata=False,
    )
    store.upsert(first_event, source_revision=first_revision)
    store.upsert(second_event, source_revision=second_revision)
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "sent"
    proposal = store.get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    assert proposal.source_revision == first_revision
    assert store.pending_delivery_count() == 1
