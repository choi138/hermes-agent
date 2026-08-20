"""Alert delivery and anchored-thread bootstrap reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    render_discord_event,
)
from plugins.mention_inbox.proposals import ProposalStatus
from plugins.mention_inbox.thread_session import (
    MentionInboxThreadCoordinator,
    ThreadDestinationMismatchError,
    _proposal_content,
)

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1531851208858275860"


def _event(
    *,
    event_id: str = "RC_123",
    kind: str = "own_pr_review_comment",
    body: str = "이 줄을 확인해 주세요.",
    source_revision: str = "2026-07-29T10:01:00Z",
    disposition: str = "review_needed",
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
            "disposition": disposition,
            "summary": body,
            "findings": (
                []
                if disposition == "informational"
                else [
                    {
                        "source_event_id": event_id,
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
                ]
            ),
            "source_revision": source_revision,
            "head_sha": "head-1",
            "approvable": disposition in {"action_required", "review_needed"},
        }
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": event_id},
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
        self.send_started: asyncio.Event | None = None
        self.send_release: asyncio.Event | None = None
        self.send_blocks_remaining = 0
        self.nonces: list[str | None] = []

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
        self,
        channel_id: str,
        content: str,
        *,
        allowed_mentions: dict[str, Any],
        nonce: str | None = None,
    ) -> str:
        self.nonces.append(nonce)
        if self.send_blocks_remaining:
            self.send_blocks_remaining -= 1
            if self.send_started is not None:
                self.send_started.set()
            if self.send_release is not None:
                await self.send_release.wait()
        message_id = f"parent-{len(self.sends) + 1}"
        self.sends.append(content)
        self.history.append((message_id, content))
        return message_id


class _Coordinator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def ensure_thread(
        self,
        event,
        *,
        parent_message_id: str,
        parent_channel_id: str,
        source_revision: str,
        delivery_checkpoint=None,
    ):
        self.calls.append((event.dedupe_key, parent_message_id, source_revision))
        if delivery_checkpoint is not None:
            await delivery_checkpoint()
        if self.fail:
            raise RuntimeError("thread bootstrap failed")
        return object()


class _ThreadDiscord:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.created = 0
        self.created_parents: list[str] = []
        self.fail_activation = False
        self.fail_participant_sync = False
        self.participant_sync_started: asyncio.Event | None = None
        self.participant_sync_release: asyncio.Event | None = None
        self.participant_sync_blocks_remaining = 0
        self.participant_syncs: list[tuple[str, frozenset[str]]] = []
        self.proposal_send_started: asyncio.Event | None = None
        self.proposal_send_release: asyncio.Event | None = None
        self.proposal_send_blocks_remaining = 0
        self.messages: list[tuple[str, str]] = []

    def remember_parent_message(
        self, parent_message_id: str, parent_channel_id: str
    ) -> None:
        return None

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        return self.thread_id

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str:
        self.created += 1
        self.created_parents.append(parent_message_id)
        self.thread_id = "thread-1"
        return self.thread_id

    async def ensure_thread_participants(
        self,
        thread_id: str,
        user_ids: frozenset[str],
    ) -> None:
        self.participant_syncs.append((thread_id, user_ids))
        if self.participant_sync_blocks_remaining:
            self.participant_sync_blocks_remaining -= 1
            if self.participant_sync_started is not None:
                self.participant_sync_started.set()
            if self.participant_sync_release is not None:
                await self.participant_sync_release.wait()
        if self.fail_participant_sync:
            raise RuntimeError("discord participant sync failed")

    async def is_thread_active(self, thread_id: str) -> bool:
        return True

    async def thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool:
        return True

    async def activate_thread(self, thread_id: str) -> None:
        if self.fail_activation:
            raise RuntimeError("discord thread activation failed")
        return None

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
        if self.proposal_send_blocks_remaining:
            self.proposal_send_blocks_remaining -= 1
            if self.proposal_send_started is not None:
                self.proposal_send_started.set()
            if self.proposal_send_release is not None:
                await self.proposal_send_release.wait()
        message_id = f"proposal-{len(self.messages) + 1}"
        self.messages.append((message_id, content))
        return message_id


class _HeartbeatStore(MentionInboxStore):
    def __init__(self, path: Path, *, clock) -> None:
        super().__init__(path, clock=clock)
        self.renewed = asyncio.Event()

    def renew_delivery_lease(
        self,
        delivery_id: int,
        *,
        expected_attempt: int,
        lease_seconds: int,
    ) -> bool:
        renewed = super().renew_delivery_lease(
            delivery_id,
            expected_attempt=expected_attempt,
            lease_seconds=lease_seconds,
        )
        self.renewed.set()
        return renewed


@pytest.mark.asyncio
async def test_parent_send_heartbeat_prevents_reclaim_and_uses_marker_nonce(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = _HeartbeatStore(tmp_path / "inbox.db", clock=lambda: now[0])
    event = _event()
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    discord.send_started = asyncio.Event()
    discord.send_release = asyncio.Event()
    discord.send_blocks_remaining = 1
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=1,
    )

    first = asyncio.create_task(delivery.deliver_once())
    await asyncio.wait_for(discord.send_started.wait(), timeout=2)
    store.renewed.clear()
    now[0] += timedelta(milliseconds=500)
    await asyncio.wait_for(store.renewed.wait(), timeout=2)
    now[0] += timedelta(milliseconds=600)

    assert store.claim_delivery(DESTINATION, lease_seconds=1) is None
    discord.send_release.set()
    assert await first == "sent"
    rendered = render_discord_event(
        event,
        revision_number=1,
        destination=DESTINATION,
    )
    assert discord.nonces == [
        hashlib.sha256(
            f"mention-inbox-parent\0{rendered.marker}".encode()
        ).hexdigest()[:25]
    ]


@pytest.mark.asyncio
async def test_destination_mismatch_records_delivery_error(
    tmp_path: Path,
) -> None:
    class WrongDestinationCoordinator:
        async def deliver_to_existing_thread(self, *args, **kwargs) -> str | None:
            raise ThreadDestinationMismatchError(
                "thread belongs to another Discord destination"
            )

    db = tmp_path / "destination-mismatch.db"
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=cast(
            MentionInboxThreadCoordinator,
            cast(object, WrongDestinationCoordinator()),
        ),
    )

    assert await delivery.deliver_once() == "error"
    connection = sqlite3.connect(db)
    try:
        category = connection.execute(
            "SELECT error_category FROM delivery_outbox"
        ).fetchone()[0]
    finally:
        connection.close()
    assert category == "discord_thread_destination_mismatch"
    assert discord.sends == []


@pytest.mark.asyncio
async def test_partial_session_rebinds_parent_before_thread_creation(
    tmp_path: Path,
) -> None:
    class WrongDestinationDiscord(_ThreadDiscord):
        async def thread_has_parent(
            self,
            thread_id: str,
            parent_channel_id: str,
        ) -> bool:
            return False

    db = tmp_path / "partial-session-destination.db"
    event = _event()
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    subject = "github:R_repo:PR_7"
    store.reserve_work_item_session(
        subject,
        event.dedupe_key,
        "2026-07-29T10:01:00Z",
    )
    store.prepare_work_item_parent(
        subject, "stale-parent", DESTINATION.split(":", 1)[1]
    )
    thread_discord = WrongDestinationDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@777>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        approval_available=False,
        participant_parent_channel_id=DESTINATION.split(":", 1)[1],
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=_Discord(),
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "error"
    session = store.get_active_work_item_session(subject)
    assert session is not None
    assert session.parent_message_id == "parent-1"
    assert session.discord_thread_id is None
    assert thread_discord.created_parents == ["parent-1"]
    connection = sqlite3.connect(db)
    try:
        category = connection.execute(
            "SELECT error_category FROM delivery_outbox"
        ).fetchone()[0]
    finally:
        connection.close()
    assert category == "discord_thread_destination_mismatch"


def test_proposal_content_uses_concrete_preflight_for_review_and_assignment() -> None:
    review = _proposal_content(_event(kind="review_requested"))
    assignment = _proposal_content(_event(kind="assigned"))

    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in str(review["goal"])
    assert "이 줄을 확인해 주세요" in str(review["goal"])
    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in str(assignment["goal"])


def test_only_own_pr_proposal_can_commit_and_push_current_branch() -> None:
    own_pr = _proposal_content(_event(kind="own_pr_review_comment"))
    requested_review = _proposal_content(_event(kind="review_requested"))

    assert {
        "switch_to_pr_branch",
        "commit_changes",
        "push_current_branch",
    }.issubset(set(own_pr["allowed_actions"]))
    assert "현재 PR branch non-force push 성공" in own_pr["verification"]
    assert "push_current_branch" not in requested_review["allowed_actions"]


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
async def test_participant_sync_retry_reuses_parent_thread_and_proposal(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    thread_discord.fail_participant_sync = True
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "error"
    assert len(parent_discord.sends) == 1
    assert thread_discord.created == 1
    assert thread_discord.messages == []
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        delivery_state = connection.execute(
            """
            SELECT status, error_category, message_id
            FROM delivery_outbox
            """
        ).fetchone()
    finally:
        connection.close()
    assert delivery_state == (
        "sending",
        "discord_thread_participant_sync_failed",
        "parent-1",
    )
    store.record_poll_success(
        "github.notifications",
        next_poll_at=NOW + timedelta(minutes=1),
    )
    health = store.health("github.notifications")
    assert health["status"] == "degraded"
    assert (
        health["error_category"]
        == "discord_thread_participant_sync_failed"
    )
    now[0] += timedelta(seconds=11)
    marker_searches = parent_discord.marker_searches
    parent_discord.marker_search_enabled = False
    recovered = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=recovered,
    )

    assert await delivery.deliver_once() == "error"
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        repeated_failure_state = connection.execute(
            """
            SELECT status, error_category, message_id, attempts
            FROM delivery_outbox
            """
        ).fetchone()
    finally:
        connection.close()
    assert repeated_failure_state == (
        "sending",
        "discord_thread_participant_sync_failed",
        "parent-1",
        2,
    )
    assert len(parent_discord.sends) == 1
    assert parent_discord.marker_searches == marker_searches
    assert thread_discord.created == 1
    assert thread_discord.messages == []

    now[0] += timedelta(seconds=11)
    thread_discord.fail_participant_sync = False
    recovered = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=recovered,
    )

    assert await delivery.deliver_once() == "reconciled"
    assert len(parent_discord.sends) == 1
    assert parent_discord.marker_searches == marker_searches
    assert thread_discord.created == 1
    assert thread_discord.participant_syncs == [
        ("thread-1", frozenset({"789391209067446323"})),
        ("thread-1", frozenset({"789391209067446323"})),
        ("thread-1", frozenset({"789391209067446323"})),
    ]
    assert len(thread_discord.messages) == 1
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_thread_activation_failure_uses_participant_error_category(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    thread_discord = _ThreadDiscord()
    thread_discord.fail_activation = True
    delivery = DiscordMentionDelivery(
        store=store,
        discord=_Discord(),
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=MentionInboxThreadCoordinator(
            store=store,
            discord=thread_discord,
            bot_mention="<@1525050460166426694>",
            trusted_repositories=frozenset({"silviahealth/content"}),
            participant_user_ids=frozenset({"789391209067446323"}),
        ),
    )

    assert await delivery.deliver_once() == "error"
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        category = connection.execute(
            "SELECT error_category FROM delivery_outbox"
        ).fetchone()[0]
    finally:
        connection.close()
    assert category == "discord_thread_participant_sync_failed"
    assert thread_discord.messages == []


@pytest.mark.asyncio
async def test_participant_sync_renews_lease_before_competing_claim(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = _HeartbeatStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    thread_discord.participant_sync_started = asyncio.Event()
    thread_discord.participant_sync_release = asyncio.Event()
    thread_discord.participant_sync_blocks_remaining = 1
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=1,
        thread_coordinator=coordinator,
    )

    first = asyncio.create_task(delivery.deliver_once())
    await asyncio.wait_for(
        thread_discord.participant_sync_started.wait(),
        timeout=2,
    )
    store.renewed.clear()
    now[0] += timedelta(milliseconds=500)
    await asyncio.wait_for(store.renewed.wait(), timeout=2)
    now[0] += timedelta(milliseconds=600)
    competing = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=1,
        thread_coordinator=MentionInboxThreadCoordinator(
            store=store,
            discord=thread_discord,
            bot_mention="<@1525050460166426694>",
            trusted_repositories=frozenset({"silviahealth/content"}),
            participant_user_ids=frozenset({"789391209067446323"}),
        ),
    )

    assert await competing.deliver_once() == "idle"
    thread_discord.participant_sync_release.set()
    assert await first == "sent"
    assert len(parent_discord.sends) == 1
    assert thread_discord.created == 1
    assert len(thread_discord.messages) == 1


@pytest.mark.asyncio
async def test_stale_participant_worker_stops_after_lease_reclaim(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = _HeartbeatStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    thread_discord.participant_sync_started = asyncio.Event()
    thread_discord.participant_sync_release = asyncio.Event()
    thread_discord.participant_sync_blocks_remaining = 1
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=1,
        thread_coordinator=MentionInboxThreadCoordinator(
            store=store,
            discord=thread_discord,
            bot_mention="<@1525050460166426694>",
            trusted_repositories=frozenset({"silviahealth/content"}),
            participant_user_ids=frozenset({"789391209067446323"}),
        ),
    )

    first = asyncio.create_task(delivery.deliver_once())
    await asyncio.wait_for(
        thread_discord.participant_sync_started.wait(),
        timeout=2,
    )
    await asyncio.wait_for(store.renewed.wait(), timeout=2)
    store.renewed.clear()
    now[0] += timedelta(seconds=2)
    reclaimed = store.claim_delivery(DESTINATION, lease_seconds=1)
    assert reclaimed is not None and reclaimed.attempts == 2
    await asyncio.wait_for(store.renewed.wait(), timeout=2)

    assert await first == "error"
    assert thread_discord.messages == []
    assert (
        store.release_delivery(
            reclaimed.delivery_id,
            claim_token=1,
            error_category="stale_release",
        )
        is False
    )
    assert (
        store.note_delivery_error(
            reclaimed.delivery_id,
            error_category="stale_attempt",
            claim_token=1,
        )
        is False
    )
    assert (
        store.mark_delivery_parent_confirmed(
            reclaimed.delivery_id,
            claim_token=1,
            message_id="parent-1",
        )
        is False
    )
    assert (
        store.mark_delivery_sent(
            reclaimed.delivery_id,
            claim_token=1,
            message_id="parent-1",
        )
        is False
    )
    assert store.note_delivery_error(
        reclaimed.delivery_id,
        error_category="current_attempt",
        claim_token=reclaimed.token,
    )
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        category = connection.execute(
            "SELECT error_category FROM delivery_outbox"
        ).fetchone()[0]
    finally:
        connection.close()
    assert category == "current_attempt"


@pytest.mark.asyncio
async def test_stale_attempt_checkpoint_prevents_duplicate_proposal_send(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    thread_discord.participant_sync_started = asyncio.Event()
    thread_discord.participant_sync_release = asyncio.Event()
    thread_discord.participant_sync_blocks_remaining = 1
    thread_discord.proposal_send_started = asyncio.Event()
    thread_discord.proposal_send_release = asyncio.Event()
    thread_discord.proposal_send_blocks_remaining = 1

    def delivery() -> DiscordMentionDelivery:
        return DiscordMentionDelivery(
            store=store,
            discord=parent_discord,
            destination=DESTINATION,
            lease_seconds=300,
            thread_coordinator=MentionInboxThreadCoordinator(
                store=store,
                discord=thread_discord,
                bot_mention="<@1525050460166426694>",
                trusted_repositories=frozenset({"silviahealth/content"}),
                participant_user_ids=frozenset({"789391209067446323"}),
            ),
        )

    first = asyncio.create_task(delivery().deliver_once())
    await asyncio.wait_for(
        thread_discord.participant_sync_started.wait(),
        timeout=2,
    )
    now[0] += timedelta(seconds=301)
    second = asyncio.create_task(delivery().deliver_once())
    await asyncio.wait_for(
        thread_discord.proposal_send_started.wait(),
        timeout=2,
    )
    thread_discord.participant_sync_release.set()

    assert await first == "error"
    assert thread_discord.messages == []
    thread_discord.proposal_send_release.set()
    assert await second == "reconciled"
    assert len(parent_discord.sends) == 1
    assert thread_discord.created == 1
    assert len(thread_discord.messages) == 1
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        status, attempts = connection.execute(
            "SELECT status, attempts FROM delivery_outbox"
        ).fetchone()
    finally:
        connection.close()
    assert (status, attempts) == ("sent", 2)


@pytest.mark.asyncio
async def test_concurrent_same_pr_rows_share_one_parent_alert(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    store.upsert(
        _event(event_id="RC_123", body="first revision"),
        source_revision="2026-07-29T10:01:00Z",
    )
    store.upsert(
        _event(
            event_id="RC_124",
            body="second revision",
            source_revision="2026-07-29T10:02:00Z",
        ),
        source_revision="2026-07-29T10:02:00Z",
    )
    parent_discord = _Discord()
    parent_discord.send_started = asyncio.Event()
    parent_discord.send_release = asyncio.Event()
    parent_discord.send_blocks_remaining = 1
    thread_discord = _ThreadDiscord()

    def delivery() -> DiscordMentionDelivery:
        return DiscordMentionDelivery(
            store=store,
            discord=parent_discord,
            destination=DESTINATION,
            lease_seconds=300,
            thread_coordinator=MentionInboxThreadCoordinator(
                store=store,
                discord=thread_discord,
                bot_mention="<@1525050460166426694>",
                trusted_repositories=frozenset({"silviahealth/content"}),
                participant_user_ids=frozenset({"789391209067446323"}),
            ),
        )

    first = asyncio.create_task(delivery().deliver_once())
    await asyncio.wait_for(parent_discord.send_started.wait(), timeout=2)

    assert await delivery().deliver_once() == "idle"
    parent_discord.send_release.set()
    assert await first == "sent"
    assert await delivery().deliver_once() == "threaded"
    assert len(parent_discord.sends) == 1
    assert thread_discord.created == 1
    assert len(thread_discord.messages) == 2
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_cancelled_participant_worker_leaves_reconcilable_delivery(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    thread_discord.participant_sync_started = asyncio.Event()
    thread_discord.participant_sync_release = asyncio.Event()
    thread_discord.participant_sync_blocks_remaining = 1

    def delivery() -> DiscordMentionDelivery:
        return DiscordMentionDelivery(
            store=store,
            discord=parent_discord,
            destination=DESTINATION,
            lease_seconds=300,
            thread_coordinator=MentionInboxThreadCoordinator(
                store=store,
                discord=thread_discord,
                bot_mention="<@1525050460166426694>",
                trusted_repositories=frozenset({"silviahealth/content"}),
                participant_user_ids=frozenset({"789391209067446323"}),
            ),
        )

    first = asyncio.create_task(delivery().deliver_once())
    await asyncio.wait_for(
        thread_discord.participant_sync_started.wait(),
        timeout=2,
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    connection = sqlite3.connect(tmp_path / "inbox.db")
    try:
        interrupted = connection.execute(
            "SELECT status, attempts, message_id FROM delivery_outbox"
        ).fetchone()
    finally:
        connection.close()
    assert interrupted == ("sending", 1, "parent-1")

    now[0] += timedelta(seconds=301)
    assert await delivery().deliver_once() == "reconciled"
    assert len(parent_discord.sends) == 1
    assert thread_discord.created == 1
    assert len(thread_discord.messages) == 1


@pytest.mark.asyncio
async def test_uncertain_parent_send_reconciles_without_duplicate(
    tmp_path: Path,
) -> None:
    class AcceptedThenLostDiscord(_Discord):
        async def send(
            self,
            channel_id: str,
            content: str,
            *,
            allowed_mentions: dict[str, Any],
            nonce: str | None = None,
        ) -> str:
            await super().send(
                channel_id,
                content,
                allowed_mentions=allowed_mentions,
                nonce=nonce,
            )
            raise RuntimeError("response lost after Discord accepted message")

    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T10:01:00Z")
    discord = AcceptedThenLostDiscord()
    delivery = DiscordMentionDelivery(
        store=store,
        discord=discord,
        destination=DESTINATION,
        lease_seconds=10,
    )

    assert await delivery.deliver_once() == "error"
    assert len(discord.sends) == 1
    now[0] += timedelta(seconds=11)

    assert await delivery.deliver_once() == "reconciled"
    assert len(discord.sends) == 1
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
        trusted_repositories=frozenset({"silviahealth/content"}),
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


@pytest.mark.asyncio
async def test_later_pr_event_uses_existing_thread_without_second_parent_card(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    first = _event()
    second = _event(
        event_id="RC_124",
        body="두 번째 finding을 반영해 주세요.",
        source_revision="2026-07-29T10:02:00Z",
    )
    store.upsert(first, source_revision="2026-07-29T10:01:00Z")
    store.upsert(second, source_revision="2026-07-29T10:02:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        approval_available=True,
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "sent"
    assert await delivery.deliver_once() == "threaded"

    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None and latest.revision == 2
    assert len(parent_discord.sends) == 1
    assert len(thread_discord.messages) == 2
    assert thread_discord.thread_id == "thread-1"
    assert store.pending_delivery_count() == 0


@pytest.mark.asyncio
async def test_same_finding_and_head_do_not_advance_proposal_revision(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    first = _event()
    duplicate = _event(
        event_id="RC_124",
        source_revision="2026-07-29T10:02:00Z",
    )
    store.upsert(first, source_revision="2026-07-29T10:01:00Z")
    store.upsert(duplicate, source_revision="2026-07-29T10:02:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        approval_available=True,
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "sent"
    assert await delivery.deliver_once() == "threaded"

    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None and latest.revision == 1
    assert len(parent_discord.sends) == 1
    assert len(thread_discord.messages) == 1


@pytest.mark.asyncio
async def test_informational_event_is_threaded_without_replacing_actionable_proposal(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    actionable = _event()
    informational = _event(
        event_id="PRR_approved",
        kind="own_pr_review_summary",
        body="Looks good",
        source_revision="2026-07-29T10:02:00Z",
        disposition="informational",
    )
    store.upsert(actionable, source_revision="2026-07-29T10:01:00Z")
    store.upsert(informational, source_revision="2026-07-29T10:02:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        approval_available=True,
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )

    assert await delivery.deliver_once() == "sent"
    original = store.get_latest_proposal("github:R_repo:PR_7")
    assert original is not None
    assert await delivery.deliver_once() == "threaded"

    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest == original
    assert len(parent_discord.sends) == 1
    assert len(thread_discord.messages) == 2
    assert "🟢 확인 완료" in thread_discord.messages[-1][1]
    assert "Looks good" in thread_discord.messages[-1][1]


@pytest.mark.asyncio
async def test_terminal_work_item_reuses_original_parent_and_thread(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    first = _event()
    duplicate = _event(
        event_id="RC_124",
        source_revision="2026-07-29T10:02:00Z",
    )
    store.upsert(first, source_revision="2026-07-29T10:01:00Z")
    store.upsert(duplicate, source_revision="2026-07-29T10:02:00Z")
    parent_discord = _Discord()
    thread_discord = _ThreadDiscord()
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=thread_discord,
        bot_mention="<@1525050460166426694>",
        trusted_repositories=frozenset({"silviahealth/content"}),
        approval_available=True,
    )
    delivery = DiscordMentionDelivery(
        store=store,
        discord=parent_discord,
        destination=DESTINATION,
        lease_seconds=10,
        thread_coordinator=coordinator,
    )
    assert await delivery.deliver_once() == "sent"
    original = store.get_latest_proposal("github:R_repo:PR_7")
    assert original is not None
    store.transition_proposal_status(
        original.proposal_id,
        original.revision,
        ProposalStatus.REJECTED,
        expected_statuses=(ProposalStatus.PENDING,),
    )

    assert await delivery.deliver_once() == "threaded"

    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None
    assert latest.revision == 1
    assert latest.status is ProposalStatus.REJECTED
    assert len(parent_discord.sends) == 1
    assert len(thread_discord.messages) == 1
    assert thread_discord.thread_id == "thread-1"
