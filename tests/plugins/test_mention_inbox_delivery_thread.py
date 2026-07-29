"""Alert delivery and anchored-thread bootstrap reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    render_discord_event,
)
from plugins.mention_inbox.thread_session import _proposal_content

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1531851208858275860"


def _event(*, kind: str = "own_pr_review_comment"):
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
            "body": "이 줄을 확인해 주세요.",
            "action_detail": kind,
            "source_url": "https://github.com/silviahealth/content/pull/7#discussion_r123",
            "metadata": {
                "actionable_kind": kind,
                "repository": "silviahealth/content",
                "subject_type": "PullRequest",
                "subject_number": 7,
                "subject_key": "github:R_repo:PR_7",
                "source_revision": "2026-07-29T10:01:00Z",
                "subject_head_sha": "head-1",
                "actor_login": "alice",
            },
        },
    })


class _Discord:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.history: list[tuple[str, str]] = []

    async def find_marker(
        self, channel_id: str, marker: str, *, limit: int
    ) -> str | None:
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
        self.calls: list[tuple[str, str]] = []

    async def ensure_thread(self, event, *, parent_message_id: str):
        self.calls.append((event.dedupe_key, parent_message_id))
        if self.fail:
            raise RuntimeError("thread bootstrap failed")
        return object()


def test_proposal_content_names_direct_review_and_assignment() -> None:
    review = _proposal_content(_event(kind="review_requested"))
    assignment = _proposal_content(_event(kind="assigned"))

    assert "review 범위" in str(review["goal"])
    assert "할당된 항목" in str(assignment["goal"])


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
    assert coordinator.calls == [(_event().dedupe_key, "parent-1")]
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
    now[0] += timedelta(seconds=11)

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
    assert recovered.calls == [(_event().dedupe_key, "parent-1")]
    assert store.pending_delivery_count() == 0
