"""Canonical work proposal domain contract."""

from __future__ import annotations

from dataclasses import replace

import pytest

from plugins.mention_inbox.proposals import (
    ProposalStatus,
    build_work_proposal,
    proposal_to_json,
    restore_proposal,
    revise_work_proposal,
    verify_proposal_hash,
)


def _proposal():
    return build_work_proposal(
        revision=1,
        source_dedupe_key="github:IC_99:U_recent",
        source_revision="2026-07-29T10:01:00Z",
        subject_key="github:R_repo:PR_7",
        head_sha="head-1",
        goal="PR의 요청 사항을 확인하고 필요한 수정을 준비한다.",
        steps=("관련 diff와 테스트를 확인한다.", "필요한 수정안을 적용한다."),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과", "변경 diff 검토"),
        executor_hint="direct",
    )


def test_proposal_id_and_hash_are_local_and_deterministic() -> None:
    first = _proposal()
    second = _proposal()

    assert first.proposal_id == second.proposal_id
    assert first.proposal_id.startswith("wp_")
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64
    assert verify_proposal_hash(first) is True


def test_proposal_hash_changes_when_executable_scope_changes() -> None:
    original = _proposal()
    changed = build_work_proposal(
        revision=1,
        source_dedupe_key=original.source_dedupe_key,
        source_revision=original.source_revision,
        subject_key=original.subject_key,
        head_sha=original.head_sha,
        goal=original.goal,
        steps=original.steps + ("추가 배포를 수행한다.",),
        allowed_actions=original.allowed_actions + ("deploy",),
        forbidden_actions=original.forbidden_actions,
        verification=original.verification,
        executor_hint=original.executor_hint,
    )

    assert changed.proposal_id == original.proposal_id
    assert changed.content_hash != original.content_hash


def test_status_transition_does_not_redefine_approved_content_hash() -> None:
    proposal = _proposal()
    approved = replace(proposal, status=ProposalStatus.APPROVED)

    assert approved.content_hash == proposal.content_hash
    assert verify_proposal_hash(approved) is True


def test_restore_rejects_external_or_tampered_hash() -> None:
    payload = proposal_to_json(_proposal()).replace(
        '"content_hash":"', '"content_hash":"tampered-', 1
    )
    with pytest.raises(ValueError, match="hash"):
        restore_proposal(payload)


def test_revision_creates_pending_r2_and_invalidates_r1_identity() -> None:
    first = _proposal()
    second = revise_work_proposal(
        first,
        source_revision="2026-07-29T10:03:00Z",
        head_sha="head-2",
        goal="최신 HEAD에서 요청 사항을 다시 확인한다.",
        steps=("최신 diff를 다시 읽는다.",),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=first.forbidden_actions,
        verification=first.verification,
        executor_hint="direct",
    )

    assert second.proposal_id == first.proposal_id
    assert second.revision == 2
    assert second.status is ProposalStatus.PENDING
    assert second.content_hash != first.content_hash


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 0),
        ("steps", ()),
        ("allowed_actions", ()),
        ("verification", ()),
        ("executor_hint", ""),
    ],
)
def test_proposal_rejects_incomplete_execution_contract(
    field: str, value: object
) -> None:
    kwargs = {
        "revision": 1,
        "source_dedupe_key": "github:IC_99:U_recent",
        "source_revision": "2026-07-29T10:01:00Z",
        "subject_key": "github:R_repo:PR_7",
        "head_sha": "head-1",
        "goal": "목표",
        "steps": ("단계",),
        "allowed_actions": ("read_repository",),
        "forbidden_actions": ("deploy",),
        "verification": ("테스트",),
        "executor_hint": "direct",
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        build_work_proposal(**kwargs)
