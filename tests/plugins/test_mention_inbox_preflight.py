"""Bounded, fail-closed pre-approval analysis of hydrated GitHub reviews."""

from __future__ import annotations

from typing import Any

from plugins.mention_inbox.actionable import GitHubActionKind, GitHubHydrationContext
from plugins.mention_inbox.preflight import (
    PreApprovalDisposition,
    brief_from_metadata,
    brief_to_metadata,
    build_preapproval_brief,
)


HEAD_SHA = "head-sha-current"
REVISION = "2026-07-29T10:01:00Z"


def _review(
    *,
    state: str = "COMMENTED",
    body: str = "Please update the lifecycle guard.",
    review_id: int = 991,
    commit_id: str = HEAD_SHA,
) -> dict[str, Any]:
    return {
        "id": review_id,
        "node_id": f"PRR_{review_id}",
        "event_type": "pull_request_review",
        "state": state,
        "body": body,
        "commit_id": commit_id,
        "submitted_at": "2026-07-29T10:00:00Z",
        "html_url": f"https://github.com/silviahealth/content/pull/7#pullrequestreview-{review_id}",
        "user": {
            "login": "coderabbitai[bot]",
            "node_id": "BOT_coderabbit",
            "type": "Bot",
        },
    }


def _comment(
    *,
    body: str = "Handle the disabled capability before rendering the CTA.",
    review_id: int = 991,
    comment_id: int = 1001,
    commit_id: str = HEAD_SHA,
    path: str = "plugins/mention_inbox/voice.py",
    line: int | None = 181,
    actor: str = "coderabbitai[bot]",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": comment_id,
        "node_id": f"PRRC_{comment_id}",
        "pull_request_review_id": review_id,
        "body": body,
        "path": path,
        "line": line,
        "original_line": line,
        "commit_id": commit_id,
        "html_url": f"https://github.com/silviahealth/content/pull/7#discussion_r{comment_id}",
        "user": {"login": actor, "node_id": f"U_{actor}", "type": "Bot"},
    }
    return result


def _context(
    *,
    latest_event: dict[str, Any],
    reviews: tuple[dict[str, Any], ...] | None = None,
    review_comments: tuple[dict[str, Any], ...] = (),
) -> GitHubHydrationContext:
    return GitHubHydrationContext(
        target_login="recent-won",
        target_node_id="U_recent",
        subject={
            "node_id": "PR_7",
            "number": 7,
            "title": "Mention inbox approval",
            "html_url": "https://github.com/silviahealth/content/pull/7",
            "updated_at": REVISION,
            "head": {"sha": HEAD_SHA},
        },
        latest_event=latest_event,
        timeline=(),
        reviews=reviews if reviews is not None else (latest_event,),
        review_comments=review_comments,
    )


def _build(
    *,
    review: dict[str, Any],
    kind: GitHubActionKind = GitHubActionKind.OWN_PR_REVIEW_SUMMARY,
    comments: tuple[dict[str, Any], ...] = (),
):
    return build_preapproval_brief(
        kind=kind,
        source_event=review,
        context=_context(latest_event=review, review_comments=comments),
        source_revision=REVISION,
        head_sha=HEAD_SHA,
    )


def test_changes_requested_with_matching_inline_comment_requires_action() -> None:
    review = _review(state="CHANGES_REQUESTED")

    brief = _build(
        review=review,
        kind=GitHubActionKind.OWN_PR_CHANGES_REQUESTED,
        comments=(_comment(),),
    )

    assert brief.disposition is PreApprovalDisposition.ACTION_REQUIRED
    assert brief.approvable is True
    assert brief.summary == "Please update the lifecycle guard."
    assert len(brief.findings) == 1
    assert brief.findings[0].path == "plugins/mention_inbox/voice.py"
    assert brief.findings[0].line == 181
    assert brief.findings[0].review_id == "991"


def test_commented_review_needs_review_before_mutation() -> None:
    brief = _build(review=_review(), comments=(_comment(),))

    assert brief.disposition is PreApprovalDisposition.REVIEW_NEEDED
    assert brief.approvable is True


def test_comment_on_noncurrent_commit_is_possibly_stale_and_not_approvable() -> None:
    brief = _build(
        review=_review(commit_id="older-sha"),
        comments=(_comment(commit_id="older-sha"),),
    )

    assert brief.disposition is PreApprovalDisposition.POSSIBLY_STALE
    assert brief.approvable is False


def test_approved_review_without_actionable_comment_is_informational() -> None:
    brief = _build(review=_review(state="APPROVED", body="Looks good"))

    assert brief.disposition is PreApprovalDisposition.INFORMATIONAL
    assert brief.approvable is False
    assert brief.findings == ()


def test_comments_from_other_review_or_actor_are_not_mixed() -> None:
    brief = _build(
        review=_review(),
        comments=(
            _comment(),
            _comment(review_id=992, comment_id=1002),
            _comment(comment_id=1003, actor="unrelated-reviewer"),
        ),
    )

    assert [finding.source_event_id for finding in brief.findings] == ["PRRC_1001"]


def test_malformed_source_fails_closed_without_dumping_payload() -> None:
    review = _review(body="")
    review.pop("id")
    review.pop("node_id")

    brief = _build(
        review=review,
        comments=(_comment(body="x", path="", line=-1),),
    )

    assert brief.disposition is PreApprovalDisposition.INSUFFICIENT_EVIDENCE
    assert brief.approvable is False
    assert brief.findings == ()


def test_brief_is_bounded_and_round_trips_through_strict_metadata() -> None:
    review = _review(body="summary " + "s" * 1000)
    comments = tuple(
        _comment(comment_id=1000 + index, body=(f"finding-{index} " + "x" * 600))
        for index in range(15)
    )

    brief = _build(review=review, comments=comments)
    metadata = brief_to_metadata(brief)
    restored = brief_from_metadata(metadata)

    assert restored == brief
    assert len(brief.summary) <= 400
    assert len(brief.findings) <= 10
    assert all(len(finding.body) <= 300 for finding in brief.findings)
    assert len(brief.summary) + sum(len(item.body) for item in brief.findings) <= 1600


def test_invalid_metadata_is_not_restored_as_approvable() -> None:
    assert brief_from_metadata({"disposition": "action_required", "approvable": True}) is None
    assert brief_from_metadata("not-a-mapping") is None
