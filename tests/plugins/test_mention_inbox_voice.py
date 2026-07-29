"""Compact, code-owned Vladilena work-inbox voice contract."""

from __future__ import annotations

import pytest

from plugins.mention_inbox import ingest_event
from plugins.mention_inbox.proposals import build_work_proposal
from plugins.mention_inbox.voice import (
    CompletionReceipt,
    render_action_alert,
    render_completed,
    render_proposal,
    render_queued,
    render_thread_opened,
)

DESTINATION = "discord:1531851208858275860"
PERMALINK = "https://github.com/silviahealth/content/pull/7#discussion_r123"


def _event(
    *,
    body: str = "이 줄을 확인해 주세요.",
    title: str = "Inbox contract",
    kind: str = "own_pr_review_comment",
):
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": "RC_123"},
        "actor": {"actor_id": "U_alice", "kind": "user"},
        "target": {"target_id": "U_recent", "kind": "user"},
        "thread": {
            "thread_id": "github:R_repo:PR_7",
            "container_id": "R_repo",
        },
        "requested_action": "reply",
        "deadline": None,
        "untrusted": {
            "title": title,
            "body": body,
            "action_detail": kind,
            "source_url": PERMALINK,
            "metadata": {
                "actionable_kind": kind,
                "repository": "silviahealth/content",
                "subject_type": "PullRequest",
                "subject_number": 7,
                "subject_key": "github:R_repo:PR_7",
                "actor_login": "alice",
                "subject_head_sha": "head-1",
            },
        },
    })


def _proposal():
    return build_work_proposal(
        revision=1,
        source_dedupe_key="github:RC_123:U_recent",
        source_revision="2026-07-29T10:01:00Z",
        subject_key="github:R_repo:PR_7",
        head_sha="head-1",
        goal="리뷰 의견을 확인하고 범위 내 수정안을 준비한다.",
        steps=("diff를 확인한다.", "수정하고 테스트한다."),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과", "diff 검토"),
        executor_hint="direct",
    )


def test_alert_does_not_render_full_subject_body() -> None:
    body = "앞부분 " + ("가" * 400) + " FULL_BODY_SENTINEL"
    rendered = render_action_alert(
        _event(body=body), revision_number=1, destination=DESTINATION
    )
    assert "FULL_BODY_SENTINEL" not in rendered.content
    assert len(rendered.content) <= 1900


def test_alert_uses_exact_comment_permalink() -> None:
    rendered = render_action_alert(_event(), revision_number=1, destination=DESTINATION)
    assert PERMALINK in rendered.content
    assert "https://github.com/silviahealth/content/pull/7\n" not in rendered.content


def test_alert_names_direct_review_and_assignment_actions() -> None:
    review = render_action_alert(
        _event(kind="review_requested"),
        revision_number=1,
        destination=DESTINATION,
    )
    assignment = render_action_alert(
        _event(kind="assigned"),
        revision_number=1,
        destination=DESTINATION,
    )

    assert "review를 요청했어요" in review.content
    assert "담당자로 지정했어요" in assignment.content


def test_alert_excerpt_is_bounded_to_240_characters() -> None:
    rendered = render_action_alert(
        _event(body="x" * 1000), revision_number=1, destination=DESTINATION
    )
    context_line = next(
        line for line in rendered.content.splitlines() if line.startswith("맥락:")
    )
    assert len(context_line.removeprefix("맥락: ")) <= 240


def test_alert_neutralizes_discord_mentions() -> None:
    rendered = render_action_alert(
        _event(title="@everyone 확인", body="<@123> @here 확인"),
        revision_number=1,
        destination=DESTINATION,
    )
    assert "@everyone" not in rendered.content
    assert "@here" not in rendered.content
    assert "<@123>" not in rendered.content
    assert rendered.allowed_mentions == {
        "parse": [],
        "users": [],
        "roles": [],
        "replied_user": False,
    }


def test_queued_voice_does_not_use_bureaucratic_or_completed_fields() -> None:
    text = render_queued(_proposal())
    for prohibited in (
        "자동 접수",
        "승인 근거:",
        "Discord message",
        "상태: queued",
        "✅",
        "검증 완료",
    ):
        assert prohibited not in text


def test_queued_voice_says_actual_waiting_state_naturally() -> None:
    text = render_queued(_proposal())
    assert "기다리고" in text or "대기" in text
    assert "아직 시작" in text


def test_thread_opened_voice_states_no_changes_started() -> None:
    text = render_thread_opened(_event())
    assert "아직" in text
    assert "변경" in text
    assert "시작" in text


def test_proposal_and_status_messages_hide_internal_ids() -> None:
    proposal = _proposal()
    combined = "\n".join((
        render_proposal(proposal, bot_mention="<@1525050525641805886>"),
        render_queued(proposal),
    ))
    assert proposal.proposal_id not in combined
    assert proposal.content_hash not in combined
    assert proposal.subject_key not in combined
    assert "승인" in combined


def test_completed_voice_requires_verified_evidence() -> None:
    with pytest.raises(ValueError, match="verified"):
        render_completed(
            CompletionReceipt(
                summary="수정을 완료했습니다.",
                evidence=("tests passed",),
                verified=False,
            )
        )
    with pytest.raises(ValueError, match="evidence"):
        render_completed(
            CompletionReceipt(
                summary="수정을 완료했습니다.", evidence=(), verified=True
            )
        )

    completed = render_completed(
        CompletionReceipt(
            summary="요청한 수정을 완료했습니다.",
            evidence=("tests/plugins/test_x.py: 4 passed", "commit abc123 verified"),
            verified=True,
        )
    )
    assert "완료" in completed
    assert "4 passed" in completed
