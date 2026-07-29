"""Runtime integration of candidate filtering and actionable hydration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.mention_inbox.github_client import (
    AuthenticatedGitHubUser,
    GitHubNotificationPage,
)
from plugins.mention_inbox.operational import _LazyGitHubNotificationCollector
from plugins.mention_inbox.runtime import GitHubMentionPoller
from plugins.mention_inbox.store import MentionInboxStore


def _notification(*, notification_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": notification_id,
        "reason": reason,
        "unread": True,
        "updated_at": "2026-07-29T10:00:00Z",
        "subject": {
            "title": "Inbox contract",
            "type": "PullRequest",
            "url": "https://api.github.com/repos/silviahealth/content/pulls/7",
            "latest_comment_url": (
                "https://api.github.com/repos/silviahealth/content/issues/comments/99"
            ),
        },
        "repository": {
            "node_id": "R_repo",
            "full_name": "silviahealth/content",
        },
    }


class _Client:
    def __init__(
        self, *, team_review: bool = False, team_mention: bool = False
    ) -> None:
        if team_review and team_mention:
            raise ValueError("test client supports one team event at a time")
        self.team_review = team_review
        self.team_mention = team_mention
        self.calls: list[str] = []

    def list_notifications(self, **kwargs: Any) -> GitHubNotificationPage:
        self.calls.append("notifications")
        reason = (
            "review_requested"
            if self.team_review
            else ("team_mention" if self.team_mention else "mention")
        )
        return GitHubNotificationPage(
            items=(
                _notification(notification_id="candidate", reason=reason),
                _notification(notification_id="ignored", reason="subscribed"),
            ),
            next_url=None,
            last_modified="Wed, 29 Jul 2026 10:00:00 GMT",
            poll_interval_seconds=60,
        )

    def get_authenticated_user(self) -> AuthenticatedGitHubUser:
        self.calls.append("user")
        return AuthenticatedGitHubUser(login="recent-won", node_id="U_recent")

    def fetch_subject(self, url: str) -> dict[str, Any]:
        self.calls.append("subject")
        return {
            "id": 7,
            "node_id": "PR_7",
            "number": 7,
            "title": "Inbox contract",
            "body": "subject body must not decide actionability",
            "html_url": "https://github.com/silviahealth/content/pull/7",
            "updated_at": "2026-07-29T10:00:00Z",
            "user": {"login": "alice", "node_id": "U_alice", "type": "User"},
            "head": {"sha": "head-1"},
            "requested_reviewers": [],
            "requested_teams": (
                [{"slug": "mobile", "organization": {"login": "silviahealth"}}]
                if self.team_review
                else []
            ),
            "assignees": [],
        }

    def fetch_latest_event(self, url: str, *, repository: str) -> dict[str, Any]:
        self.calls.append("latest")
        return {
            "id": 99,
            "node_id": "IC_actual_99",
            "event_type": "review_requested" if self.team_review else "issue_comment",
            "body": (
                ""
                if self.team_review
                else (
                    "@silviahealth/mobile 확인 부탁해요"
                    if self.team_mention
                    else "@recent-won 확인 부탁해요"
                )
            ),
            "html_url": "https://github.com/silviahealth/content/pull/7#issuecomment-99",
            "created_at": "2026-07-29T10:01:00Z",
            "updated_at": "2026-07-29T10:01:00Z",
            "user": {"login": "alice", "node_id": "U_alice", "type": "User"},
        }

    def fetch_pull_timeline(self, url: str, *, repository: str, limit: int = 50):
        self.calls.append("timeline")
        return ()

    def fetch_pull_reviews(self, url: str, *, repository: str, limit: int = 50):
        self.calls.append("reviews")
        return ()

    def fetch_pull_review_comments(self, url: str, *, repository: str, limit: int = 50):
        self.calls.append("review_comments")
        return ()

    def is_active_team_member(self, team_slug: str, username: str) -> bool:
        self.calls.append(f"team:{team_slug}")
        return team_slug == "silviahealth/mobile" and username == "recent-won"


def _poll(
    tmp_path: Path,
    client: _Client,
    *,
    team_mentions: bool = False,
    team_review_requests: bool = False,
):
    store = MentionInboxStore(
        tmp_path / "inbox.db",
        clock=lambda: datetime(2026, 7, 29, 10, 2, tzinfo=timezone.utc),
    )
    collector = _LazyGitHubNotificationCollector(
        client,
        ("silviahealth/content",),
        team_mentions=team_mentions,
        team_review_requests=team_review_requests,
    )
    result = GitHubMentionPoller(
        client=client,
        collector=collector,
        store=store,
        clock=lambda: datetime(2026, 7, 29, 10, 2, tzinfo=timezone.utc),
    ).poll_once()
    return result, store


def test_runtime_hydrates_only_candidate_and_uses_actual_event_id(
    tmp_path: Path,
) -> None:
    client = _Client()
    result, store = _poll(tmp_path, client)

    assert result.created == 1
    assert result.skipped == 1
    assert client.calls.count("subject") == 1
    assert client.calls.count("latest") == 1
    connection = sqlite3.connect(store.path)
    assert connection.execute(
        "SELECT source_event_id, source_revision FROM mention_events"
    ).fetchone() == ("IC_actual_99", "2026-07-29T10:00:00Z")
    connection.close()


def test_runtime_team_mention_is_suppressed_before_hydration_by_default(
    tmp_path: Path,
) -> None:
    client = _Client(team_mention=True)
    result, store = _poll(tmp_path, client)

    assert result.created == 0
    assert result.skipped == 2
    assert "subject" not in client.calls
    assert not any(call.startswith("team:") for call in client.calls)
    connection = sqlite3.connect(store.path)
    assert connection.execute("SELECT COUNT(*) FROM mention_events").fetchone()[0] == 0
    connection.close()


def test_runtime_team_mention_requires_explicit_opt_in_and_active_membership(
    tmp_path: Path,
) -> None:
    client = _Client(team_mention=True)
    result, store = _poll(tmp_path, client, team_mentions=True)

    assert result.created == 1
    assert "team:silviahealth/mobile" in client.calls
    connection = sqlite3.connect(store.path)
    event_json = connection.execute("SELECT event_json FROM mention_events").fetchone()[
        0
    ]
    connection.close()
    assert '"actionable_kind":"team_mention"' in event_json


def test_runtime_team_review_request_is_suppressed_by_default(tmp_path: Path) -> None:
    client = _Client(team_review=True)
    result, store = _poll(tmp_path, client)

    assert result.created == 0
    assert result.skipped == 2
    assert not any(call.startswith("team:") for call in client.calls)
    connection = sqlite3.connect(store.path)
    assert connection.execute("SELECT COUNT(*) FROM mention_events").fetchone()[0] == 0
    connection.close()


def test_runtime_team_review_request_requires_verified_active_membership(
    tmp_path: Path,
) -> None:
    client = _Client(team_review=True)
    result, store = _poll(tmp_path, client, team_review_requests=True)

    assert result.created == 1
    assert "team:silviahealth/mobile" in client.calls
    connection = sqlite3.connect(store.path)
    event_json = connection.execute("SELECT event_json FROM mention_events").fetchone()[
        0
    ]
    connection.close()
    assert '"actionable_kind":"team_review_requested"' in event_json
