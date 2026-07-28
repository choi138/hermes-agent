"""Behavior tests for GitHub notification normalization."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

import plugins.mention_inbox.github_client as github_client_module
from plugins.mention_inbox import (
    ActorKind,
    ApprovalState,
    MentionSource,
    RequestedAction,
)
from plugins.mention_inbox.github_client import (
    GITHUB_API_VERSION,
    GitHubClientError,
    GitHubHttpResponse,
    GitHubNotificationsClient,
)
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.runtime import GitHubMentionPoller
from plugins.mention_inbox.store import MentionInboxStore


def _notification(
    *,
    notification_id: str = "987654321",
    reason: str = "review_requested",
    repository: str = "silviahealth/content",
    updated_at: str = "2026-07-28T11:20:30Z",
) -> dict[str, Any]:
    owner, name = repository.split("/", maxsplit=1)
    return {
        "id": notification_id,
        "reason": reason,
        "unread": True,
        "updated_at": updated_at,
        "last_read_at": None,
        "subject": {
            "title": "Review requested for inbox contract",
            "url": (f"https://api.github.com/repos/{repository}/pulls/7"),
            "latest_comment_url": (
                f"https://api.github.com/repos/{repository}/issues/comments/11"
            ),
            "type": "PullRequest",
        },
        "repository": {
            "id": 123,
            "node_id": "R_kgDORepository",
            "name": name,
            "full_name": repository,
            "owner": {
                "login": owner,
                "id": 42,
                "node_id": "U_kgDOOwner",
                "type": "Organization",
            },
        },
        "url": f"https://api.github.com/notifications/threads/{notification_id}",
        "subscription_url": (
            "https://api.github.com/notifications/threads/"
            f"{notification_id}/subscription"
        ),
    }


def _pull_request_detail() -> dict[str, Any]:
    return {
        "id": 777,
        "node_id": "PR_kwDOPullRequest",
        "number": 7,
        "title": "Review requested for inbox contract",
        "body": "Please review this pull request.",
        "html_url": "https://github.com/silviahealth/content/pull/7",
        "updated_at": "2026-07-28T11:20:30Z",
        "user": {
            "login": "octocat",
            "id": 1,
            "node_id": "U_kgDOActor",
            "type": "User",
        },
    }


def test_review_request_normalizes_to_versioned_mention_event() -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    collected = collector.normalize(_notification(), _pull_request_detail())

    assert collected is not None
    assert collected.source_revision == "2026-07-28T11:20:30Z"
    event = collected.event
    assert event.source.platform is MentionSource.GITHUB
    assert event.source.event_id == "987654321"
    assert event.actor.actor_id == "U_kgDOActor"
    assert event.actor.kind is ActorKind.USER
    assert event.target.target_id == "U_kgDORecentWon"
    assert event.thread.thread_id == "PR_kwDOPullRequest"
    assert event.thread.container_id == "R_kgDORepository"
    assert event.requested_action is RequestedAction.REVIEW
    assert event.approval_state is ApprovalState.PENDING
    assert event.untrusted.title == "Review requested for inbox contract"
    assert event.untrusted.body == "Please review this pull request."
    assert event.untrusted.source_url == (
        "https://github.com/silviahealth/content/pull/7"
    )
    assert event.untrusted.metadata == {
        "reason": "review_requested",
        "repository": "silviahealth/content",
        "subject_type": "PullRequest",
        "unread": True,
    }
    assert not hasattr(event, "body")


@pytest.mark.parametrize(
    "notification",
    [
        _notification(reason="author"),
        _notification(repository="other/project"),
        _notification(reason="MENTION"),
        {"reason": "mention"},
    ],
)
def test_normalizer_rejects_unselected_notifications(
    notification: dict[str, Any],
) -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    assert collector.accepts(notification) is False
    assert collector.normalize(notification, {}) is None


@pytest.mark.parametrize(
    "notification",
    [
        {**_notification(), "id": None},
        {**_notification(), "updated_at": "not-a-date"},
        {**_notification(), "subject": {}},
    ],
)
def test_normalizer_fails_closed_for_malformed_selected_notification(
    notification: dict[str, Any],
) -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    assert collector.accepts(notification) is False
    with pytest.raises(ValueError, match="notification"):
        collector.normalize(notification, _pull_request_detail())


@pytest.mark.parametrize(
    ("reason", "expected_action"),
    [
        ("review_requested", RequestedAction.REVIEW),
        ("mention", RequestedAction.REPLY),
        ("team_mention", RequestedAction.REPLY),
        ("assign", RequestedAction.INVESTIGATE),
    ],
)
def test_selected_reason_maps_to_requested_action(
    reason: str,
    expected_action: RequestedAction,
) -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    collected = collector.normalize(
        _notification(reason=reason),
        _pull_request_detail(),
    )

    assert collected is not None
    assert collected.event.requested_action is expected_action


def test_missing_subject_detail_keeps_notification_with_safe_fallbacks() -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    collected = collector.normalize(_notification(reason="mention"), None)

    assert collected is not None
    event = collected.event
    assert event.actor.actor_id == "github:unknown"
    assert event.actor.kind is ActorKind.UNKNOWN
    assert event.thread.thread_id == "github-notification:987654321"
    assert event.thread.container_id == "R_kgDORepository"
    assert event.untrusted.title == "Review requested for inbox contract"
    assert event.untrusted.body == ""
    assert event.untrusted.source_url is None


@pytest.mark.parametrize(
    "updated_at",
    [None, 42, "", "not-a-date", "2026-07-28T11:20:30"],
)
def test_normalizer_rejects_invalid_source_revision(updated_at: object) -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )
    notification = _notification()
    notification["updated_at"] = updated_at

    with pytest.raises(ValueError, match="updated_at"):
        collector.normalize(notification, _pull_request_detail())


def test_client_lists_participating_notifications_with_poll_headers() -> None:
    requests = []
    next_url = (
        "https://api.github.com/notifications?participating=true&per_page=50&page=2"
    )

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append((request, timeout))
        return GitHubHttpResponse(
            status=200,
            headers={
                "Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"',
                "Last-Modified": "Tue, 28 Jul 2026 11:20:30 GMT",
                "X-Poll-Interval": "75",
            },
            body=json.dumps([_notification()]).encode(),
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    page = client.list_notifications(if_modified_since="Tue, 28 Jul 2026 10:20:30 GMT")

    assert GITHUB_API_VERSION == "2026-03-10"
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == 10.0
    parsed = urlparse(request.full_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.github.com"
    assert parsed.path == "/notifications"
    assert parse_qs(parsed.query) == {
        "participating": ["true"],
        "per_page": ["50"],
    }
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("X-github-api-version") == GITHUB_API_VERSION
    assert request.get_header("If-modified-since") == ("Tue, 28 Jul 2026 10:20:30 GMT")
    assert page.items == (_notification(),)
    assert page.next_url == next_url
    assert page.last_modified == "Tue, 28 Jul 2026 11:20:30 GMT"
    assert page.poll_interval_seconds == 75
    assert page.not_modified is False


def test_client_treats_304_as_empty_unchanged_page() -> None:
    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status=304,
            headers={"X-Poll-Interval": "90"},
            body=b"",
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    page = client.list_notifications(if_modified_since="Tue, 28 Jul 2026 11:20:30 GMT")

    assert page.items == ()
    assert page.next_url is None
    assert page.last_modified is None
    assert page.poll_interval_seconds == 90
    assert page.not_modified is True


def test_client_follows_only_github_notification_page_urls() -> None:
    requests = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(status=200, headers={}, body=b"[]")

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    allowed = (
        "https://api.github.com/notifications?participating=true&per_page=50&page=2"
    )

    page = client.list_notifications(page_url=allowed)

    assert page.items == ()
    assert [request.full_url for request in requests] == [allowed]

    for denied in (
        "https://api.github.com.evil.example/notifications?page=2",
        "http://api.github.com/notifications?page=2",
        "https://api.github.com/user",
        "https://user@api.github.com/notifications?page=2",
    ):
        with pytest.raises(ValueError, match="GitHub notifications URL"):
            client.list_notifications(page_url=denied)

    assert len(requests) == 1


def test_client_fetches_only_issue_and_pull_request_subjects() -> None:
    requests = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200,
            headers={},
            body=json.dumps(_pull_request_detail()).encode(),
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    issue_url = "https://api.github.com/repos/silviahealth/content/issues/7"
    pull_url = "https://api.github.com/repos/silviahealth/content/pulls/7"

    assert client.fetch_subject(issue_url) == _pull_request_detail()
    assert client.fetch_subject(pull_url) == _pull_request_detail()
    assert [request.get_method() for request in requests] == ["GET", "GET"]
    assert all(request.data is None for request in requests)

    for denied in (
        "https://api.github.com/repos/silviahealth/content/issues/comments/11",
        "https://api.github.com/user",
        "https://api.github.com.evil.example/repos/org/repo/issues/7",
        "http://api.github.com/repos/org/repo/pulls/7",
    ):
        with pytest.raises(ValueError, match="GitHub subject URL"):
            client.fetch_subject(denied)

    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "headers", "category", "retryable", "retry_after"),
    [
        (401, {}, "unauthorized", False, None),
        (
            403,
            {"X-RateLimit-Remaining": "0", "Retry-After": "120"},
            "rate_limited",
            True,
            120,
        ),
        (403, {}, "forbidden", False, None),
        (500, {}, "server_error", True, None),
        (422, {}, "client_error", False, None),
    ],
)
def test_client_classifies_http_errors_without_exposing_payload(
    status: int,
    headers: dict[str, str],
    category: str,
    retryable: bool,
    retry_after: int | None,
) -> None:
    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status=status,
            headers=headers,
            body=b'{"message":"private-response-body"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    with pytest.raises(GitHubClientError) as raised:
        client.list_notifications()

    error = raised.value
    assert error.category == category
    assert error.status == status
    assert error.retryable is retryable
    assert error.retry_after_seconds == retry_after
    assert "private-response-body" not in str(error)
    assert "test-token" not in str(error)


def test_subject_404_returns_none_for_collector_fallback() -> None:
    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status=404,
            headers={},
            body=b'{"message":"not found"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    assert (
        client.fetch_subject(
            "https://api.github.com/repos/silviahealth/content/issues/7"
        )
        is None
    )


def test_client_reads_authenticated_user_stable_id() -> None:
    requests = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200,
            headers={},
            body=b'{"login":"recent-won","id":123,"node_id":"U_kgDORecentWon"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    assert client.get_authenticated_user_id() == "U_kgDORecentWon"
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://api.github.com/user"
    assert request.get_method() == "GET"
    assert request.data is None


def test_client_default_transport_performs_read_only_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    class Response:
        status = 200
        headers = {"X-Poll-Interval": "60"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"[]"

    def fake_urlopen(request: Any, timeout: float) -> Response:
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(github_client_module, "urlopen", fake_urlopen)
    client = GitHubNotificationsClient(token="test-token")

    page = client.list_notifications()

    assert page.items == ()
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert timeout == 10.0


def test_poller_collects_selected_notifications_and_persists_cursor(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    requests = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        path = urlparse(request.full_url).path
        if path == "/notifications":
            return GitHubHttpResponse(
                status=200,
                headers={
                    "Last-Modified": "Tue, 28 Jul 2026 11:20:30 GMT",
                    "X-Poll-Interval": "75",
                },
                body=json.dumps([
                    _notification(),
                    _notification(notification_id="ignored-reason", reason="author"),
                    _notification(
                        notification_id="ignored-repository",
                        repository="other/project",
                    ),
                ]).encode(),
            )
        if path == "/repos/silviahealth/content/pulls/7":
            return GitHubHttpResponse(
                status=200,
                headers={},
                body=json.dumps(_pull_request_detail()).encode(),
            )
        raise AssertionError(f"unexpected request path: {path}")

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    poller = GitHubMentionPoller(
        client=client,
        collector=collector,
        store=store,
        clock=lambda: now,
    )

    result = poller.poll_once()

    assert result.status == "ok"
    assert result.pages == 1
    assert result.fetched == 3
    assert result.selected == 1
    assert result.created == 1
    assert result.updated == 0
    assert result.skipped == 2
    assert result.not_modified is False
    assert result.next_poll_seconds == 75
    assert result.error_category is None
    assert store.count() == 1
    assert store.get_cursor("github.notifications") == ("Tue, 28 Jul 2026 11:20:30 GMT")
    status = store.get_collector_status("github.notifications")
    assert status is not None
    assert status.status == "ok"
    assert status.next_poll_at == datetime(2026, 7, 28, 12, 1, 15, tzinfo=timezone.utc)
    normalized = collector.normalize(_notification(), _pull_request_detail())
    assert normalized is not None
    stored = store.get(normalized.event.dedupe_key)
    assert stored is not None
    assert stored.event.source.event_id == "987654321"
    assert [request.get_method() for request in requests] == ["GET", "GET"]
    assert all(request.data is None for request in requests)


def test_poller_follows_pagination_and_uses_largest_poll_interval(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    requests = []
    next_url = (
        "https://api.github.com/notifications?participating=true&per_page=50&page=2"
    )

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        parsed = urlparse(request.full_url)
        if parsed.path == "/notifications" and parse_qs(parsed.query).get("page") == [
            "2"
        ]:
            return GitHubHttpResponse(
                status=200,
                headers={"X-Poll-Interval": "90"},
                body=json.dumps([
                    _notification(notification_id="second-notification")
                ]).encode(),
            )
        if parsed.path == "/notifications":
            return GitHubHttpResponse(
                status=200,
                headers={
                    "Link": f'<{next_url}>; rel="next"',
                    "Last-Modified": "Tue, 28 Jul 2026 11:20:30 GMT",
                    "X-Poll-Interval": "60",
                },
                body=json.dumps([_notification()]).encode(),
            )
        if parsed.path == "/repos/silviahealth/content/pulls/7":
            return GitHubHttpResponse(
                status=200,
                headers={},
                body=json.dumps(_pull_request_detail()).encode(),
            )
        raise AssertionError(f"unexpected request URL: {request.full_url}")

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    poller = GitHubMentionPoller(
        client=client,
        collector=collector,
        store=store,
        clock=lambda: now,
    )

    result = poller.poll_once()

    assert result.status == "ok"
    assert result.pages == 2
    assert result.fetched == 2
    assert result.selected == 2
    assert result.created == 2
    assert result.skipped == 0
    assert result.next_poll_seconds == 90
    assert store.count() == 2
    assert store.get_cursor("github.notifications") == ("Tue, 28 Jul 2026 11:20:30 GMT")
    assert [urlparse(request.full_url).path for request in requests] == [
        "/notifications",
        "/repos/silviahealth/content/pulls/7",
        "/notifications",
        "/repos/silviahealth/content/pulls/7",
    ]


def test_poller_records_rate_limit_without_advancing_cursor(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status=403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "120"},
            body=b'{"message":"private rate-limit body"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )
    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    original_cursor = "Tue, 28 Jul 2026 10:20:30 GMT"
    store.set_cursor("github.notifications", original_cursor)
    poller = GitHubMentionPoller(
        client=client,
        collector=collector,
        store=store,
        clock=lambda: now,
    )

    result = poller.poll_once()

    assert result.status == "error"
    assert result.pages == 0
    assert result.fetched == 0
    assert result.created == 0
    assert result.next_poll_seconds == 120
    assert result.error_category == "rate_limited"
    assert store.get_cursor("github.notifications") == original_cursor
    status = store.get_collector_status("github.notifications")
    assert status is not None
    assert status.status == "error"
    assert status.error_category == "rate_limited"
    assert status.consecutive_failures == 1
    assert status.next_poll_at == datetime(2026, 7, 28, 12, 2, tzinfo=timezone.utc)
    assert "private rate-limit body" not in str(result)
    assert "test-token" not in str(result)


def test_poller_bounds_pagination_without_committing_cursor(tmp_path: Path) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    requests = []
    looping_url = (
        "https://api.github.com/notifications?participating=true&per_page=50&page=2"
    )

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        parsed = urlparse(request.full_url)
        if parsed.path == "/notifications":
            return GitHubHttpResponse(
                status=200,
                headers={
                    "Link": f'<{looping_url}>; rel="next"',
                    "Last-Modified": "Tue, 28 Jul 2026 11:20:30 GMT",
                    "X-Poll-Interval": "60",
                },
                body=b"[]",
            )
        raise AssertionError(f"unexpected request URL: {request.full_url}")

    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    poller = GitHubMentionPoller(
        client=GitHubNotificationsClient(token="test-token", transport=transport),
        collector=GitHubNotificationCollector(
            target_id="U_kgDORecentWon",
            allowed_repositories={"silviahealth/content"},
        ),
        store=store,
        clock=lambda: now,
        max_pages=2,
    )

    result = poller.poll_once()

    assert result.status == "error"
    assert result.error_category == "pagination_limit"
    assert result.next_poll_seconds == 300
    assert len(requests) == 2
    assert store.get_cursor("github.notifications") is None


@pytest.mark.parametrize("body", [b"not-json", b'{"unexpected":"object"}'])
def test_client_classifies_malformed_notification_response_as_protocol_error(
    body: bytes,
) -> None:
    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(status=200, headers={}, body=body)

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    with pytest.raises(GitHubClientError) as raised:
        client.list_notifications()

    assert raised.value.category == "protocol_error"
    assert raised.value.retryable is True
    assert raised.value.status == 200
    assert "not-json" not in str(raised.value)
    assert "unexpected" not in str(raised.value)


def test_client_uses_rate_limit_reset_when_retry_after_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_client_module.time, "time", lambda: 1_000.0)

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(
            status=403,
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1120",
            },
            body=b"{}",
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)

    with pytest.raises(GitHubClientError) as raised:
        client.list_notifications()

    assert raised.value.category == "rate_limited"
    assert raised.value.retry_after_seconds == 120


def test_nullable_subject_detail_fields_use_safe_fallbacks() -> None:
    detail = _pull_request_detail()
    detail["body"] = None
    detail["user"] = None
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )

    collected = collector.normalize(_notification(reason="mention"), detail)

    assert collected is not None
    assert collected.event.actor.actor_id == "github:unknown"
    assert collected.event.actor.kind is ActorKind.UNKNOWN
    assert collected.event.untrusted.body == ""


def test_source_revision_only_change_preserves_content_and_approval(
    tmp_path: Path,
) -> None:
    collector = GitHubNotificationCollector(
        target_id="U_kgDORecentWon",
        allowed_repositories={"silviahealth/content"},
    )
    first = collector.normalize(
        _notification(updated_at="2026-07-28T11:20:30Z"),
        _pull_request_detail(),
    )
    second = collector.normalize(
        _notification(updated_at="2026-07-28T11:25:30Z"),
        _pull_request_detail(),
    )
    assert first is not None
    assert second is not None
    store = MentionInboxStore(tmp_path / "mention-inbox.db")
    store.upsert(first.event, source_revision=first.source_revision)
    store.transition_approval(first.event.dedupe_key, ApprovalState.APPROVED)

    result = store.upsert(second.event, source_revision=second.source_revision)
    stored = store.get(first.event.dedupe_key)

    assert result.content_changed is False
    assert result.revision_number == 1
    assert stored is not None
    assert stored.source_revision == "2026-07-28T11:25:30Z"
    assert stored.event.approval_state is ApprovalState.APPROVED


def test_poller_does_not_enrich_subject_from_different_repository(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    notification = _notification(reason="mention")
    notification["subject"]["url"] = (
        "https://api.github.com/repos/other/project/pulls/7"
    )
    requests = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        path = urlparse(request.full_url).path
        if path == "/notifications":
            return GitHubHttpResponse(
                status=200,
                headers={"X-Poll-Interval": "60"},
                body=json.dumps([notification]).encode(),
            )
        raise AssertionError(f"subject enrichment escaped repository scope: {path}")

    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    result = GitHubMentionPoller(
        client=GitHubNotificationsClient(token="test-token", transport=transport),
        collector=GitHubNotificationCollector(
            target_id="U_kgDORecentWon",
            allowed_repositories={"silviahealth/content"},
        ),
        store=store,
        clock=lambda: now,
    ).poll_once()

    assert result.status == "ok"
    assert result.selected == 1
    assert result.created == 1
    assert len(requests) == 1
    assert urlparse(requests[0].full_url).path == "/notifications"


def test_poller_skips_contract_invalid_notification_and_continues(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    invalid = _notification(notification_id="invalid-notification")
    invalid["subject"]["title"] = "\ud800"
    valid = _notification(notification_id="valid-notification")

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        path = urlparse(request.full_url).path
        if path == "/notifications":
            return GitHubHttpResponse(
                status=200,
                headers={
                    "Last-Modified": "Tue, 28 Jul 2026 11:20:30 GMT",
                    "X-Poll-Interval": "60",
                },
                body=json.dumps([invalid, valid]).encode(),
            )
        if path == "/repos/silviahealth/content/pulls/7":
            return GitHubHttpResponse(
                status=200,
                headers={},
                body=json.dumps(_pull_request_detail()).encode(),
            )
        raise AssertionError(f"unexpected request path: {path}")

    store = MentionInboxStore(
        tmp_path / "mention-inbox.db",
        clock=lambda: now,
    )
    result = GitHubMentionPoller(
        client=GitHubNotificationsClient(token="test-token", transport=transport),
        collector=GitHubNotificationCollector(
            target_id="U_kgDORecentWon",
            allowed_repositories={"silviahealth/content"},
        ),
        store=store,
        clock=lambda: now,
    ).poll_once()

    assert result.status == "ok"
    assert result.fetched == 2
    assert result.selected == 2
    assert result.created == 1
    assert result.skipped == 1
    assert store.count() == 1
    assert store.get_cursor("github.notifications") == ("Tue, 28 Jul 2026 11:20:30 GMT")
