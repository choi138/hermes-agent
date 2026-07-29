"""Durable one-thread-per-subject mention inbox coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.proposals import ProposalStatus
from plugins.mention_inbox.thread_session import MentionInboxThreadCoordinator

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
BOT_MENTION = "<@1525050525641805886>"


def _event(
    *,
    event_id: str = "RC_123",
    subject_key: str = "github:R_repo:PR_7",
    source_revision: str = "2026-07-29T10:01:00Z",
    head_sha: str = "head-1",
    body: str = "이 줄을 확인해 주세요.",
):
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": event_id},
        "actor": {"actor_id": "U_alice", "kind": "user"},
        "target": {"target_id": "U_recent", "kind": "user"},
        "thread": {"thread_id": subject_key, "container_id": "R_repo"},
        "requested_action": "reply",
        "deadline": None,
        "untrusted": {
            "title": "Inbox contract",
            "body": body,
            "action_detail": "own_pr_review_comment",
            "source_url": "https://github.com/silviahealth/content/pull/7#discussion_r123",
            "metadata": {
                "actionable_kind": "own_pr_review_comment",
                "repository": "silviahealth/content",
                "subject_type": "PullRequest",
                "subject_number": 7,
                "subject_key": subject_key,
                "subject_head_sha": head_sha,
                "source_revision": source_revision,
                "actor_login": "alice",
            },
        },
    })


class _Discord:
    def __init__(self) -> None:
        self.threads: dict[str, str] = {}
        self.created: list[tuple[str, str, int]] = []
        self.marked: list[str] = []
        self.messages: dict[str, list[tuple[str, str]]] = {}
        self.next_message = 1

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        return self.threads.get(parent_message_id)

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str:
        existing = self.threads.get(parent_message_id)
        if existing is not None:
            return existing
        thread_id = f"thread-{len(self.threads) + 1}"
        self.threads[parent_message_id] = thread_id
        self.created.append((parent_message_id, name, auto_archive_duration))
        return thread_id

    def mark_thread_participation(self, thread_id: str) -> None:
        self.marked.append(thread_id)

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        for message_id, existing in self.messages.get(thread_id, [])[-limit:]:
            if existing == content:
                return message_id
        return None

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        message_id = f"proposal-message-{self.next_message}"
        self.next_message += 1
        self.messages.setdefault(thread_id, []).append((message_id, content))
        return message_id


def _coordinator(path: Path, discord: _Discord) -> MentionInboxThreadCoordinator:
    return MentionInboxThreadCoordinator(
        store=MentionInboxStore(path, clock=lambda: NOW),
        discord=discord,
        bot_mention=BOT_MENTION,
    )


@pytest.mark.asyncio
async def test_same_subject_creates_one_thread_and_one_proposal(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(tmp_path / "inbox.db", discord)
    event = _event()

    first = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    second = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert first.discord_thread_id == second.discord_thread_id == "thread-1"
    assert len(discord.created) == 1
    assert len(discord.messages["thread-1"]) == 1
    assert discord.marked == ["thread-1", "thread-1"]


@pytest.mark.asyncio
async def test_interrupted_creation_recovers_existing_anchored_thread(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.db"
    store = MentionInboxStore(path, clock=lambda: NOW)
    event = _event()
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T10:01:00Z",
    )
    store.prepare_work_item_parent(event.thread.thread_id, "parent-1")

    discord = _Discord()
    discord.threads["parent-1"] = "existing-thread"
    session = await _coordinator(path, discord).ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert session.discord_thread_id == "existing-thread"
    assert discord.created == []
    assert len(discord.messages["existing-thread"]) == 1


@pytest.mark.asyncio
async def test_new_source_revision_reuses_thread_and_posts_r2(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    discord = _Discord()
    coordinator = _coordinator(path, discord)
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    revised_event = _event(
        event_id="RC_124",
        source_revision="2026-07-29T10:02:00Z",
        head_sha="head-2",
    )
    await coordinator.ensure_thread(
        revised_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )
    await coordinator.ensure_thread(
        revised_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )

    store = MentionInboxStore(path, clock=lambda: NOW)
    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None
    assert latest.revision == 2
    assert latest.source_dedupe_key == revised_event.dedupe_key
    assert latest.head_sha == "head-2"
    assert latest.status is ProposalStatus.PENDING
    assert len(discord.created) == 1
    assert len(discord.messages["thread-1"]) == 2


@pytest.mark.asyncio
async def test_different_subjects_create_different_threads(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(tmp_path / "inbox.db", discord)
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    await coordinator.ensure_thread(
        _event(
            event_id="IC_8",
            subject_key="github:R_repo:I_8",
            head_sha="",
        ),
        parent_message_id="parent-2",
        source_revision="2026-07-29T10:01:00Z",
    )
    assert {value for value in discord.threads.values()} == {"thread-1", "thread-2"}


@pytest.mark.asyncio
async def test_pending_proposal_is_local_no_tools_and_omits_full_body(
    tmp_path: Path,
) -> None:
    body = "앞부분 " + ("x" * 1000) + " FULL_BODY_SENTINEL"
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(body=body),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    content = discord.messages["thread-1"][0][1]
    assert "FULL_BODY_SENTINEL" not in content
    assert "진행 순서" in content
    assert BOT_MENTION in content
    assert not hasattr(discord, "run_agent")
    assert not hasattr(discord, "execute_tool")
