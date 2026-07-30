"""Durable one-thread-per-subject mention inbox coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.proposals import ProposalStatus, build_work_proposal
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
    actionable_kind: str = "own_pr_review_comment",
    disposition: str = "review_needed",
    include_brief: bool = True,
):
    metadata: dict[str, object] = {
        "actionable_kind": actionable_kind,
        "repository": "silviahealth/content",
        "subject_type": "PullRequest",
        "subject_number": 7,
        "subject_key": subject_key,
        "subject_head_sha": head_sha,
        "source_revision": source_revision,
        "actor_login": "alice",
    }
    if include_brief:
        metadata["preapproval_brief"] = {
            "schema_version": 1,
            "disposition": disposition,
            "summary": body[:350],
            "findings": [
                {
                    "source_event_id": event_id,
                    "body": body[:250],
                    "source_url": (
                        "https://github.com/silviahealth/content/pull/7"
                        "#discussion_r123"
                    ),
                    "path": "plugins/mention_inbox/voice.py",
                    "line": 181,
                    "review_id": "991",
                    "commit_id": head_sha or None,
                }
            ],
            "source_revision": source_revision,
            "head_sha": head_sha or None,
            "approvable": disposition in {"action_required", "review_needed"},
        }
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
            "action_detail": actionable_kind,
            "source_url": "https://github.com/silviahealth/content/pull/7#discussion_r123",
            "metadata": metadata,
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


def _coordinator(
    path: Path, discord: _Discord, *, approval_available: bool = True
) -> MentionInboxThreadCoordinator:
    return MentionInboxThreadCoordinator(
        store=MentionInboxStore(path, clock=lambda: NOW),
        discord=discord,
        bot_mention=BOT_MENTION,
        approval_available=approval_available,
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
async def test_approval_offer_is_bound_only_when_execution_and_preflight_allow_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "enabled.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=True).ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" in content
    assert binding is not None and binding.approval_offered is True


@pytest.mark.asyncio
async def test_execution_unavailable_renders_review_only_and_binds_false(
    tmp_path: Path,
) -> None:
    path = tmp_path / "disabled.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=False).ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" not in content
    assert "현재 실행 기능이 꺼져 있어" in content
    assert binding is not None and binding.approval_offered is False


@pytest.mark.asyncio
async def test_non_approvable_preflight_stays_review_only_when_execution_exists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=True).ensure_thread(
        _event(disposition="possibly_stale"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" not in content
    assert "현재 근거로는 변경 승인을 받을 수 없어요" in content
    assert binding is not None and binding.approval_offered is False


@pytest.mark.asyncio
async def test_review_summary_proposal_uses_concrete_review_evidence(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(actionable_kind="own_pr_review_summary"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "확인한 내용" in content
    assert "이 줄을 확인해 주세요" in content
    assert "plugins/mention‗inbox/voice.py:181" in content
    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in content
    assert "원본 event와 최신 repository 상태" not in content


@pytest.mark.asyncio
async def test_stale_review_explains_that_mutation_is_not_yet_approvable(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(disposition="possibly_stale"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "이전 commit의 의견일 수 있어" in content
    assert "먼저 읽기 전용으로 현재 상태를 다시 확인" in content


@pytest.mark.asyncio
async def test_missing_preapproval_evidence_fails_closed_in_proposal(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(include_brief=False),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "상세를 안전하게 확인하지 못했어요" in content
    assert "먼저 읽기 전용으로 원문과 현재 HEAD를 다시 확인" in content


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
    assert "승인 후 진행할 작업" in content
    assert BOT_MENTION in content
    assert not hasattr(discord, "run_agent")
    assert not hasattr(discord, "execute_tool")

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_available", "expected_new_offer"),
    ((False, False), (True, True)),
)
async def test_legacy_pending_is_reconciled_once_from_new_hydration(
    tmp_path: Path,
    approval_available: bool,
    expected_new_offer: bool,
) -> None:
    path = tmp_path / f"legacy-{approval_available}.db"
    store = MentionInboxStore(path, clock=lambda: NOW)
    event = _event()
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T09:00:00Z",
    )
    store.record_work_item_thread(event.thread.thread_id, "parent-1", "thread-1")
    legacy = build_work_proposal(
        revision=1,
        source_dedupe_key=event.dedupe_key,
        source_revision="2026-07-29T09:00:00Z",
        subject_key=event.thread.thread_id,
        head_sha="old-head",
        goal="과거 일반 제안",
        steps=("과거 요청을 확인한다.",),
        allowed_actions=("read_repository",),
        forbidden_actions=("edit_files", "merge", "deploy", "read_secrets"),
        verification=("새 hydration 필요",),
        executor_hint="direct",
    )
    store.create_proposal(legacy)
    store.record_proposal_message(
        legacy.proposal_id,
        legacy.revision,
        "legacy-message",
        approval_offered=False,
    )
    discord = _Discord()
    coordinator = _coordinator(
        path,
        discord,
        approval_available=approval_available,
    )

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

    restarted = MentionInboxStore(path)
    old_binding = restarted.get_proposal_message_binding(
        legacy.proposal_id, legacy.revision
    )
    old_proposal = restarted.get_proposal(legacy.proposal_id, legacy.revision)
    latest = restarted.get_latest_proposal(event.thread.thread_id)
    assert first == second
    assert old_binding is not None and old_binding.approval_offered is False
    assert old_proposal is not None
    assert old_proposal.status is ProposalStatus.NEEDS_REAPPROVAL
    assert latest is not None and latest.revision == 2
    latest_binding = restarted.get_proposal_message_binding(
        latest.proposal_id, latest.revision
    )
    assert latest_binding is not None
    assert latest_binding.approval_offered is expected_new_offer
    assert len(discord.messages["thread-1"]) == 1
    rendered = discord.messages["thread-1"][0][1]
    assert "이전 승인은 사용하지 않고" in rendered
    assert (f"{BOT_MENTION} 승인" in rendered) is expected_new_offer
