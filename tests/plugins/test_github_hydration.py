"""GET-only GitHub hydration boundary tests."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from plugins.mention_inbox.github_client import (
    GitHubHttpResponse,
    GitHubNotificationsClient,
)

REPOSITORY = "silviahealth/content"
SUBJECT_URL = f"https://api.github.com/repos/{REPOSITORY}/pulls/7"
LATEST_COMMENT_URL = f"https://api.github.com/repos/{REPOSITORY}/issues/comments/99"


def test_client_caches_authenticated_login_and_stable_id() -> None:
    requests: list[Any] = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200,
            headers={},
            body=b'{"login":"recent-won","node_id":"U_recent"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    first = client.get_authenticated_user()
    second = client.get_authenticated_user()

    assert first.login == "recent-won"
    assert first.node_id == "U_recent"
    assert second is first
    assert len(requests) == 1
    assert requests[0].get_method() == "GET"
    assert requests[0].data is None


def test_client_fetches_latest_comment_from_allowed_github_url() -> None:
    requests: list[Any] = []
    payload = {
        "id": 99,
        "node_id": "IC_99",
        "body": "@recent-won please review",
        "html_url": "https://github.com/silviahealth/content/pull/7#issuecomment-99",
        "user": {"login": "alice", "node_id": "U_alice", "type": "User"},
        "created_at": "2026-07-29T10:00:00Z",
        "updated_at": "2026-07-29T10:00:00Z",
    }

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200, headers={}, body=json.dumps(payload).encode()
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    hydrated = client.fetch_latest_event(LATEST_COMMENT_URL, repository=REPOSITORY)

    assert hydrated == {**payload, "event_type": "issue_comment"}
    assert len(requests) == 1
    assert requests[0].get_method() == "GET"
    assert requests[0].data is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com.evil.example/repos/silviahealth/content/issues/comments/99",
        "http://api.github.com/repos/silviahealth/content/issues/comments/99",
        "https://api.github.com/repos/other/project/issues/comments/99",
        "https://user@api.github.com/repos/silviahealth/content/issues/comments/99",
    ],
)
def test_client_rejects_latest_comment_url_from_other_origin_or_repository(
    url: str,
) -> None:
    client = GitHubNotificationsClient(
        token="test-token",
        transport=lambda request, timeout: GitHubHttpResponse(
            status=200, headers={}, body=b"{}"
        ),
    )
    with pytest.raises(ValueError, match="hydration URL"):
        client.fetch_latest_event(url, repository=REPOSITORY)


def test_client_fetches_bounded_pull_timeline_with_get_only() -> None:
    requests: list[Any] = []
    payload = [
        {
            "id": 7,
            "node_id": "TE_7",
            "event": "review_requested",
            "created_at": "2026-07-29T09:00:00Z",
            "actor": {"login": "alice", "node_id": "U_alice", "type": "User"},
        }
    ]

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200, headers={}, body=json.dumps(payload).encode()
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    events = client.fetch_pull_timeline(SUBJECT_URL, repository=REPOSITORY, limit=25)

    assert events == ({**payload[0], "event_type": "review_requested"},)
    assert len(requests) == 1
    request = requests[0]
    parsed = urlparse(request.full_url)
    assert parsed.path == "/repos/silviahealth/content/issues/7/timeline"
    assert parse_qs(parsed.query) == {"per_page": ["25"]}
    assert request.get_method() == "GET"
    assert request.data is None


@pytest.mark.parametrize("limit", [0, 101, True])
def test_client_rejects_unbounded_timeline_limit(limit: Any) -> None:
    client = GitHubNotificationsClient(
        token="test-token",
        transport=lambda request, timeout: GitHubHttpResponse(
            status=200, headers={}, body=b"[]"
        ),
    )
    with pytest.raises(ValueError, match="limit"):
        client.fetch_pull_timeline(SUBJECT_URL, repository=REPOSITORY, limit=limit)


def test_client_verifies_active_team_membership_with_get_only() -> None:
    requests: list[Any] = []

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        requests.append(request)
        return GitHubHttpResponse(
            status=200,
            headers={},
            body=b'{"state":"active","role":"member"}',
        )

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    assert client.is_active_team_member("silviahealth/mobile", "recent-won") is True
    assert len(requests) == 1
    assert urlparse(requests[0].full_url).path == (
        "/orgs/silviahealth/teams/mobile/memberships/recent-won"
    )
    assert requests[0].get_method() == "GET"
    assert requests[0].data is None


def test_client_bounds_hydration_response_size_without_exposing_body() -> None:
    private_body = b"x" * (1_048_576 + 1)

    def transport(request: Any, timeout: float) -> GitHubHttpResponse:
        return GitHubHttpResponse(status=200, headers={}, body=private_body)

    client = GitHubNotificationsClient(token="test-token", transport=transport)
    with pytest.raises(Exception) as raised:
        client.fetch_latest_event(LATEST_COMMENT_URL, repository=REPOSITORY)

    assert "xxxx" not in str(raised.value)
    assert "test-token" not in str(raised.value)
