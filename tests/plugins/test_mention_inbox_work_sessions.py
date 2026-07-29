"""Durable work-item session and exact proposal approval persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox.proposals import (
    ProposalStatus,
    build_work_proposal,
    revise_work_proposal,
)
from plugins.mention_inbox.store import MentionInboxStore

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
SUBJECT = "github:R_repo:PR_7"
DEDUPE = "github:IC_99:U_recent"
SOURCE_REVISION = "2026-07-29T10:01:00Z"
APPROVER = "396159160201658368"


def _store(path: Path) -> MentionInboxStore:
    return MentionInboxStore(path, clock=lambda: NOW)


def _proposal(*, revision: int = 1, head_sha: str = "head-1"):
    return build_work_proposal(
        revision=revision,
        source_dedupe_key=DEDUPE,
        source_revision=SOURCE_REVISION,
        subject_key=SUBJECT,
        head_sha=head_sha,
        goal="요청된 PR 변경을 확인하고 필요한 수정을 준비한다.",
        steps=("diff를 읽는다.", "범위 내 수정을 한다.", "테스트한다."),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과", "diff 검토"),
        executor_hint="direct",
    )


def test_one_active_thread_per_subject_and_record_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    first = store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    second = store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)

    assert first.subject_key == second.subject_key == SUBJECT
    assert second.discord_thread_id is None
    recorded = store.record_work_item_thread(SUBJECT, "parent-1", "thread-1")
    repeated = store.record_work_item_thread(SUBJECT, "parent-1", "thread-1")
    assert recorded.discord_thread_id == repeated.discord_thread_id == "thread-1"
    with pytest.raises(ValueError, match="different thread"):
        store.record_work_item_thread(SUBJECT, "parent-2", "thread-2")

    connection = sqlite3.connect(store.path)
    assert (
        connection.execute("SELECT COUNT(*) FROM work_item_sessions").fetchone()[0] == 1
    )
    connection.close()


def test_interrupted_thread_creation_retry_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    _store(path).reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)

    restarted = _store(path)
    restored = restarted.get_active_work_item_session(SUBJECT)
    assert restored is not None
    assert restored.parent_message_id is None
    assert restored.discord_thread_id is None
    restarted.record_work_item_thread(SUBJECT, "parent-1", "thread-1")
    assert (
        restarted.get_active_work_item_session(SUBJECT).discord_thread_id == "thread-1"
    )


def test_restart_restores_pending_proposal_and_message_mapping(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = _store(path)
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    proposal = _proposal()
    store.create_proposal(proposal)
    store.record_proposal_message(proposal.proposal_id, 1, "proposal-message-1")

    restarted = _store(path)
    restored = restarted.get_proposal_by_message_id("proposal-message-1")
    assert restored == proposal
    assert restarted.get_latest_proposal(SUBJECT) == proposal


def test_exact_pending_proposal_approval_succeeds_once(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    proposal = _proposal()
    store.create_proposal(proposal)
    store.record_proposal_message(proposal.proposal_id, 1, "proposal-message-1")

    result = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        proposal_hash=proposal.content_hash,
        source_revision=proposal.source_revision,
        current_head_sha=proposal.head_sha,
        approver_platform="discord",
        approver_user_id=APPROVER,
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id="approval-message-1",
    )
    replay = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        proposal_hash=proposal.content_hash,
        source_revision=proposal.source_revision,
        current_head_sha=proposal.head_sha,
        approver_platform="discord",
        approver_user_id=APPROVER,
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id="approval-message-1",
    )

    assert result.approved is True
    assert result.reason == "approved"
    assert result.proposal.status is ProposalStatus.APPROVED
    assert replay.approved is False
    assert replay.reason in {"already_approved", "approval_message_reused"}


def test_another_user_cannot_approve(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    proposal = _proposal()
    store.create_proposal(proposal)

    result = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=1,
        proposal_hash=proposal.content_hash,
        source_revision=proposal.source_revision,
        current_head_sha=proposal.head_sha,
        approver_platform="discord",
        approver_user_id="someone-else",
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id="approval-message-2",
    )

    assert result.approved is False
    assert result.reason == "unauthorized_approver"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.PENDING


def test_non_reply_cannot_select_a_proposal_implicitly(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    store.create_proposal(_proposal())
    assert store.get_proposal_by_message_id("not-a-proposal-message") is None


def test_r2_invalidates_old_pending_revision(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    first = _proposal()
    store.create_proposal(first)
    second = revise_work_proposal(
        first,
        source_revision="2026-07-29T10:02:00Z",
        head_sha="head-2",
        goal="최신 HEAD에서 다시 확인한다.",
        steps=("최신 diff를 읽는다.",),
        allowed_actions=first.allowed_actions,
        forbidden_actions=first.forbidden_actions,
        verification=first.verification,
        executor_hint=first.executor_hint,
    )
    store.create_proposal(second)

    stale = store.approve_proposal_cas(
        proposal_id=first.proposal_id,
        revision=1,
        proposal_hash=first.content_hash,
        source_revision=first.source_revision,
        current_head_sha=first.head_sha,
        approver_platform="discord",
        approver_user_id=APPROVER,
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id="approval-old",
    )
    assert stale.approved is False
    assert stale.reason in {"not_latest_revision", "needs_reapproval"}
    assert store.get_latest_proposal(SUBJECT).revision == 2


@pytest.mark.parametrize(
    "source_revision,current_head_sha,reason",
    [
        ("2026-07-29T10:05:00Z", "head-1", "source_changed"),
        (SOURCE_REVISION, "head-2", "head_changed"),
    ],
)
def test_source_or_head_mismatch_marks_needs_reapproval(
    tmp_path: Path, source_revision: str, current_head_sha: str, reason: str
) -> None:
    store = _store(tmp_path / f"{reason}.db")
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    proposal = _proposal()
    store.create_proposal(proposal)

    result = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=1,
        proposal_hash=proposal.content_hash,
        source_revision=source_revision,
        current_head_sha=current_head_sha,
        approver_platform="discord",
        approver_user_id=APPROVER,
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id=f"approval-{reason}",
    )

    assert result.approved is False
    assert result.reason == reason
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL


def test_additive_schema_keeps_database_private_and_healthy(tmp_path: Path) -> None:
    store = _store(tmp_path / "inbox.db")
    mode = store.path.stat().st_mode & 0o777
    connection = sqlite3.connect(store.path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
    connection.close()

    assert mode == 0o600
    assert {
        "mention_events",
        "delivery_outbox",
        "work_item_sessions",
        "work_proposals",
        "work_approvals",
    } <= tables
    assert quick_check == "ok"
