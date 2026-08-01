"""Actionable GitHub notification classification behavior."""

from __future__ import annotations

from typing import Any

import pytest

from plugins.mention_inbox.actionable import (
    GitHubActionKind,
    GitHubHydrationContext,
    SuppressionReason,
    classify_actionable,
)


def _notification(*, reason: str = "mention") -> dict[str, Any]:
    return {
        "id": "987654321",
        "reason": reason,
        "unread": True,
        "updated_at": "2026-07-29T10:00:00Z",
        "subject": {
            "title": "Review requested for inbox contract",
            "type": "PullRequest",
            "url": "https://api.github.com/repos/silviahealth/content/pulls/7",
        },
        "repository": {
            "node_id": "R_kgDORepository",
            "full_name": "silviahealth/content",
        },
    }


def _context(
    *,
    latest_event: dict[str, Any] | None = None,
    subject_overrides: dict[str, Any] | None = None,
    verified_teams: frozenset[str] = frozenset(),
) -> GitHubHydrationContext:
    subject: dict[str, Any] = {
        "id": 777,
        "node_id": "PR_kwDOPullRequest",
        "number": 7,
        "title": "Review requested for inbox contract",
        "body": "Please review this pull request.",
        "html_url": "https://github.com/silviahealth/content/pull/7",
        "updated_at": "2026-07-29T10:00:00Z",
        "user": {"login": "octocat", "node_id": "U_actor", "type": "User"},
        "head": {"sha": "head-sha-1"},
        "requested_reviewers": [],
        "requested_teams": [],
        "assignees": [],
    }
    subject.update(subject_overrides or {})
    return GitHubHydrationContext(
        target_login="recent-won",
        target_node_id="U_kgDORecentWon",
        subject=subject,
        latest_event=latest_event,
        timeline=(),
        reviews=(),
        review_comments=(),
        verified_team_slugs=verified_teams,
    )


def _human_event(
    *,
    body: str = "",
    event_type: str = "issue_comment",
    login: str = "alice",
    actor_type: str = "User",
    state: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": 991,
        "node_id": "IC_kwDO991",
        "event_type": event_type,
        "body": body,
        "html_url": "https://github.com/silviahealth/content/pull/7#issuecomment-991",
        "created_at": "2026-07-29T10:00:00Z",
        "updated_at": "2026-07-29T10:00:00Z",
        "user": {
            "login": login,
            "node_id": f"U_{login}",
            "type": actor_type,
        },
    }
    if state is not None:
        event["state"] = state
    return event


def _own_pr() -> dict[str, Any]:
    return {
        "user": {
            "login": "recent-won",
            "node_id": "U_kgDORecentWon",
            "type": "User",
        }
    }


def test_human_latest_comment_direct_mention_is_actionable() -> None:
    decision = classify_actionable(
        _notification(),
        _context(latest_event=_human_event(body="@recent-won 확인 부탁해요")),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.DIRECT_MENTION
    assert decision.event.source_event_id == "IC_kwDO991"
    assert decision.event.source_url.endswith("#issuecomment-991")


def test_source_revision_is_current_subject_revision_not_comment_revision() -> None:
    latest = _human_event(body="@recent-won 확인 부탁해요")
    latest["updated_at"] = "2026-07-29T10:02:00Z"
    decision = classify_actionable(
        _notification(),
        _context(
            latest_event=latest,
            subject_overrides={"updated_at": "2026-07-29T10:01:00Z"},
        ),
    )
    assert decision.event is not None
    assert decision.event.source_revision == "2026-07-29T10:01:00Z"
    assert decision.event.subject_url == (
        "https://api.github.com/repos/silviahealth/content/pulls/7"
    )


def test_sticky_mention_without_latest_mention_is_suppressed() -> None:
    decision = classify_actionable(
        _notification(),
        _context(latest_event=_human_event(body="new commit pushed")),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.STALE_NOTIFICATION_REASON


def test_bot_release_body_mention_is_suppressed() -> None:
    decision = classify_actionable(
        _notification(),
        _context(
            latest_event=_human_event(
                body="release notes mention @recent-won",
                login="release-bot",
                actor_type="Bot",
            )
        ),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.BOT_GENERATED_MENTION


def test_direct_requested_reviewer_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="review_requested"),
        _context(
            latest_event=_human_event(event_type="review_requested"),
            subject_overrides={
                "requested_reviewers": [
                    {"login": "recent-won", "node_id": "U_kgDORecentWon"}
                ]
            },
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.REVIEW_REQUESTED


def test_team_mention_for_active_member_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="team_mention"),
        _context(
            latest_event=_human_event(body="@silviahealth/mobile 리뷰 부탁해요"),
            verified_teams=frozenset({"silviahealth/mobile"}),
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.TEAM_MENTION
    assert decision.event.metadata["team_slug"] == "silviahealth/mobile"


def test_team_review_request_for_active_member_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="review_requested"),
        _context(
            latest_event=_human_event(event_type="review_requested"),
            subject_overrides={
                "requested_teams": [
                    {"slug": "mobile", "organization": {"login": "silviahealth"}}
                ]
            },
            verified_teams=frozenset({"silviahealth/mobile"}),
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.TEAM_REVIEW_REQUESTED


def test_team_event_without_verified_membership_is_suppressed() -> None:
    decision = classify_actionable(
        _notification(reason="team_mention"),
        _context(latest_event=_human_event(body="@silviahealth/mobile 확인")),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.UNVERIFIED_TEAM_MEMBERSHIP


def test_direct_assignment_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="assign"),
        _context(
            latest_event=_human_event(event_type="assigned"),
            subject_overrides={
                "assignees": [{"login": "recent-won", "node_id": "U_kgDORecentWon"}]
            },
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.ASSIGNED


def test_external_comment_on_own_pr_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(body="모바일 위치가 달라요"),
            subject_overrides=_own_pr(),
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.OWN_PR_COMMENT


def test_own_pr_comment_does_not_reemit_sticky_review_request() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(body="새 댓글이에요"),
            subject_overrides={
                **_own_pr(),
                "requested_reviewers": [
                    {"login": "recent-won", "node_id": "U_kgDORecentWon"}
                ],
            },
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.OWN_PR_COMMENT


def test_comment_notification_does_not_reemit_sticky_assignment() -> None:
    decision = classify_actionable(
        _notification(reason="comment"),
        _context(
            latest_event=_human_event(body="새 댓글이에요"),
            subject_overrides={
                "assignees": [{"login": "recent-won", "node_id": "U_kgDORecentWon"}]
            },
        ),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.NON_ACTIONABLE


def test_bot_direct_review_request_is_suppressed() -> None:
    decision = classify_actionable(
        _notification(reason="review_requested"),
        _context(
            latest_event=_human_event(
                event_type="review_requested",
                login="review-bot",
                actor_type="Bot",
            ),
            subject_overrides={
                "requested_reviewers": [
                    {"login": "recent-won", "node_id": "U_kgDORecentWon"}
                ]
            },
        ),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.NON_ACTIONABLE


def test_self_comment_on_own_pr_is_suppressed() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(login="recent-won"), subject_overrides=_own_pr()
        ),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.SELF_AUTHORED


def test_external_review_comment_on_own_pr_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(event_type="review_comment"),
            subject_overrides=_own_pr(),
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.OWN_PR_REVIEW_COMMENT


def test_changes_requested_on_own_pr_is_actionable() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(event_type="review", state="CHANGES_REQUESTED"),
            subject_overrides=_own_pr(),
        ),
    )
    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.OWN_PR_CHANGES_REQUESTED


def test_approval_on_own_pr_is_suppressed_by_default() -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(event_type="review", state="APPROVED"),
            subject_overrides=_own_pr(),
        ),
    )
    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.NON_ACTIONABLE


@pytest.mark.parametrize(
    "login",
    (
        "coderabbitai[bot]",
        "chatgpt-codex-connector[bot]",
        "openai-codex[bot]",
        "codex[bot]",
    ),
)
def test_allowlisted_ai_bot_inline_comment_on_own_pr_is_actionable(login: str) -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(
                body="@recent-won 이 줄의 예외 처리를 보완해 주세요.",
                event_type="review_comment",
                login=login,
                actor_type="Bot",
            ),
            subject_overrides=_own_pr(),
        ),
    )

    assert decision.event is not None
    assert decision.event.kind is GitHubActionKind.OWN_PR_REVIEW_COMMENT


@pytest.mark.parametrize(
    "login",
    (
        "coderabbitai[bot]",
        "chatgpt-codex-connector[bot]",
        "openai-codex[bot]",
        "codex[bot]",
    ),
)
@pytest.mark.parametrize(
    ("state", "expected_kind"),
    (
        ("COMMENTED", "own_pr_review_summary"),
        ("APPROVED", "own_pr_review_summary"),
        ("CHANGES_REQUESTED", "own_pr_changes_requested"),
    ),
)
def test_allowlisted_ai_bot_nonempty_review_summary_on_own_pr_is_actionable(
    login: str,
    state: str,
    expected_kind: str,
) -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(
                body="전체적으로 안전하지만 이 경계 조건은 수정이 필요해요.",
                event_type="review",
                login=login,
                actor_type="Bot",
                state=state,
            ),
            subject_overrides=_own_pr(),
        ),
    )

    assert decision.event is not None
    assert decision.event.kind.value == expected_kind


@pytest.mark.parametrize(
    ("event_type", "body", "state"),
    (
        ("review", "   ", "COMMENTED"),
        ("review", "review 처리 중", "PENDING"),
        ("review", "dismissed review", "DISMISSED"),
        ("review_comment", "\n\t", None),
        ("issue_comment", "일반 PR 댓글", None),
    ),
)
def test_allowlisted_ai_bot_other_own_pr_activity_remains_suppressed(
    event_type: str,
    body: str,
    state: str | None,
) -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(
                body=body,
                event_type=event_type,
                login="coderabbitai[bot]",
                actor_type="Bot",
                state=state,
            ),
            subject_overrides=_own_pr(),
        ),
    )

    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.NON_ACTIONABLE


@pytest.mark.parametrize(
    ("event_type", "state"),
    (
        ("review", "COMMENTED"),
        ("review_comment", None),
    ),
)
def test_unallowlisted_bot_review_activity_on_own_pr_remains_suppressed(
    event_type: str,
    state: str | None,
) -> None:
    decision = classify_actionable(
        _notification(reason="author"),
        _context(
            latest_event=_human_event(
                body="자동 생성한 review 내용",
                event_type=event_type,
                login="other-review-bot[bot]",
                actor_type="Bot",
                state=state,
            ),
            subject_overrides=_own_pr(),
        ),
    )

    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.NON_ACTIONABLE


@pytest.mark.parametrize("event_type", ("review", "review_comment"))
def test_allowlisted_ai_bot_review_on_non_owned_pr_remains_suppressed(
    event_type: str,
) -> None:
    decision = classify_actionable(
        _notification(reason="mention"),
        _context(
            latest_event=_human_event(
                body="@recent-won 이 review를 확인해 주세요.",
                event_type=event_type,
                login="coderabbitai[bot]",
                actor_type="Bot",
                state="COMMENTED" if event_type == "review" else None,
            ),
        ),
    )

    assert decision.event is None
    assert decision.suppression_reason is SuppressionReason.BOT_GENERATED_MENTION
