"""Read-only GitHub Notifications REST client."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_POLL_INTERVAL_SECONDS = 60
_MAX_RESPONSE_BYTES = 1_048_576
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_LOGIN_RE = re.compile(r"[A-Za-z0-9-]+")
_TEAM_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class GitHubHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class GitHubNotificationPage:
    items: tuple[dict[str, Any], ...]
    next_url: str | None
    last_modified: str | None
    poll_interval_seconds: int
    not_modified: bool = False


@dataclass(frozen=True)
class AuthenticatedGitHubUser:
    login: str
    node_id: str


GitHubTransport = Callable[[Request, float], GitHubHttpResponse]


class GitHubClientError(RuntimeError):
    """Secret-safe GitHub transport/protocol failure metadata."""

    def __init__(
        self,
        *,
        category: str,
        status: int | None,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.category = category
        self.status = status
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        status_label = "none" if status is None else str(status)
        super().__init__(
            f"GitHub API request failed: category={category}, status={status_label}"
        )


def _stdlib_transport(request: Request, timeout: float) -> GitHubHttpResponse:
    try:
        with urlopen(request, timeout=timeout) as response:
            return GitHubHttpResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return GitHubHttpResponse(
            status=exc.code,
            headers=headers,
            body=exc.read(),
        )
    except (URLError, TimeoutError, OSError) as exc:
        raise GitHubClientError(
            category="transport_error",
            status=None,
            retryable=True,
        ) from exc


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _next_link(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(","):
        segments = [segment.strip() for segment in part.split(";")]
        if len(segments) < 2 or 'rel="next"' not in segments[1:]:
            continue
        candidate = segments[0]
        if candidate.startswith("<") and candidate.endswith(">"):
            return candidate[1:-1]
    return None


def _poll_interval(headers: Mapping[str, str]) -> int:
    raw = _header(headers, "X-Poll-Interval")
    if raw is None:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_POLL_INTERVAL_SECONDS
    return parsed if parsed > 0 else _DEFAULT_POLL_INTERVAL_SECONDS


def _retry_after(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "Retry-After")
    if raw is not None:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed

    reset_raw = _header(headers, "X-RateLimit-Reset")
    if reset_raw is None:
        return None
    try:
        reset_epoch = int(reset_raw)
    except ValueError:
        return None
    return max(0, reset_epoch - int(time.time()))


def _raise_for_status(response: GitHubHttpResponse) -> None:
    status = response.status
    if status == 200:
        return
    retry_after = _retry_after(response.headers)
    if status == 401:
        category, retryable = "unauthorized", False
    elif status == 403 and (
        _header(response.headers, "X-RateLimit-Remaining") == "0"
        or retry_after is not None
    ):
        category, retryable = "rate_limited", True
    elif status == 403:
        category, retryable = "forbidden", False
    elif 500 <= status <= 599:
        category, retryable = "server_error", True
    else:
        category, retryable = "client_error", False
    raise GitHubClientError(
        category=category,
        status=status,
        retryable=retryable,
        retry_after_seconds=retry_after,
    )


def _protocol_error(status: int) -> GitHubClientError:
    return GitHubClientError(
        category="protocol_error",
        status=status,
        retryable=True,
    )


def _decode_json(response: GitHubHttpResponse) -> Any:
    if len(response.body) > _MAX_RESPONSE_BYTES:
        raise _protocol_error(response.status)
    try:
        return json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _protocol_error(response.status) from exc


def _require_notifications_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("GitHub notifications URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != "/notifications"
        or parsed.fragment
    ):
        raise ValueError("GitHub notifications URL must use the GitHub API origin")
    return value


def _require_subject_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("GitHub subject URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or re.fullmatch(r"/repos/[^/]+/[^/]+/(?:issues|pulls)/[1-9][0-9]*", parsed.path)
        is None
    ):
        raise ValueError("GitHub subject URL must identify an issue or pull request")
    return value


def _repository_parts(repository: str) -> tuple[str, str]:
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError("repository must be an owner/name pair")
    owner, name = repository.split("/", 1)
    return owner, name


def _require_hydration_origin(value: str) -> Any:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("GitHub hydration URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("GitHub hydration URL must use the GitHub API origin")
    return parsed


def _require_latest_event_url(value: str, repository: str) -> tuple[str, str]:
    owner, name = _repository_parts(repository)
    parsed = _require_hydration_origin(value)
    if parsed.query:
        raise ValueError("GitHub hydration URL must not contain a query")
    issue_pattern = rf"/repos/{re.escape(owner)}/{re.escape(name)}/issues/comments/[1-9][0-9]*"
    review_pattern = rf"/repos/{re.escape(owner)}/{re.escape(name)}/pulls/comments/[1-9][0-9]*"
    if re.fullmatch(issue_pattern, parsed.path):
        return value, "issue_comment"
    if re.fullmatch(review_pattern, parsed.path):
        return value, "review_comment"
    raise ValueError("GitHub hydration URL escaped the allowed repository or endpoint")


def _subject_coordinates(subject_url: str, repository: str) -> tuple[str, str, str, int]:
    owner, name = _repository_parts(repository)
    parsed = _require_hydration_origin(subject_url)
    if parsed.query:
        raise ValueError("GitHub hydration URL must not contain a query")
    match = re.fullmatch(
        rf"/repos/{re.escape(owner)}/{re.escape(name)}/(issues|pulls)/([1-9][0-9]*)",
        parsed.path,
    )
    if match is None:
        raise ValueError("GitHub hydration URL escaped the allowed repository or endpoint")
    return owner, name, match.group(1), int(match.group(2))


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("hydration limit must be an integer between 1 and 100")
    return limit


class GitHubNotificationsClient:
    """A GET-only client for authenticated-user notification reads."""

    def __init__(
        self,
        *,
        token: str,
        transport: GitHubTransport | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or "\r" in token
            or "\n" in token
        ):
            raise ValueError("GitHub token must be a non-empty header-safe string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._token = token
        self._transport = transport or _stdlib_transport
        self._timeout_seconds = timeout_seconds
        self._authenticated_user: AuthenticatedGitHubUser | None = None

    def list_notifications(
        self,
        *,
        if_modified_since: str | None = None,
        page_url: str | None = None,
    ) -> GitHubNotificationPage:
        if page_url is None:
            query = urlencode({"participating": "true", "per_page": "50"})
            url = f"{GITHUB_API_BASE}/notifications?{query}"
        else:
            url = _require_notifications_url(page_url)
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "hermes-agent-mention-inbox",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            method="GET",
        )
        if if_modified_since is not None:
            request.add_header("If-Modified-Since", if_modified_since)

        response = self._transport(request, self._timeout_seconds)
        if response.status == 304:
            return GitHubNotificationPage(
                items=(),
                next_url=None,
                last_modified=None,
                poll_interval_seconds=_poll_interval(response.headers),
                not_modified=True,
            )

        _raise_for_status(response)
        decoded = _decode_json(response)
        if not isinstance(decoded, list) or any(
            not isinstance(item, dict) for item in decoded
        ):
            raise _protocol_error(response.status)
        return GitHubNotificationPage(
            items=tuple(decoded),
            next_url=_next_link(_header(response.headers, "Link")),
            last_modified=_header(response.headers, "Last-Modified"),
            poll_interval_seconds=_poll_interval(response.headers),
        )

    def _get_json(
        self,
        url: str,
        *,
        accept: str = "application/vnd.github+json",
        allow_not_found: bool = False,
    ) -> Any:
        request = Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "hermes-agent-mention-inbox",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            method="GET",
        )
        response = self._transport(request, self._timeout_seconds)
        if allow_not_found and response.status == 404:
            return None
        _raise_for_status(response)
        return _decode_json(response)

    def get_authenticated_user(self) -> AuthenticatedGitHubUser:
        if self._authenticated_user is not None:
            return self._authenticated_user
        decoded = self._get_json(f"{GITHUB_API_BASE}/user")
        if not isinstance(decoded, dict):
            raise _protocol_error(200)
        login = decoded.get("login")
        node_id = decoded.get("node_id")
        if (
            not isinstance(login, str)
            or _LOGIN_RE.fullmatch(login) is None
            or not isinstance(node_id, str)
            or not node_id
            or node_id != node_id.strip()
        ):
            raise _protocol_error(200)
        self._authenticated_user = AuthenticatedGitHubUser(login=login, node_id=node_id)
        return self._authenticated_user

    def get_authenticated_user_id(self) -> str:
        return self.get_authenticated_user().node_id

    def fetch_subject(self, subject_url: str) -> dict[str, Any] | None:
        request = Request(
            _require_subject_url(subject_url),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "hermes-agent-mention-inbox",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            method="GET",
        )
        response = self._transport(request, self._timeout_seconds)
        if response.status == 404:
            return None
        _raise_for_status(response)
        decoded = _decode_json(response)
        if not isinstance(decoded, dict):
            raise _protocol_error(response.status)
        return decoded

    def fetch_latest_event(
        self, event_url: str, *, repository: str
    ) -> dict[str, Any] | None:
        url, event_type = _require_latest_event_url(event_url, repository)
        decoded = self._get_json(url, allow_not_found=True)
        if decoded is None:
            return None
        if not isinstance(decoded, dict):
            raise _protocol_error(200)
        return {**decoded, "event_type": event_type}

    def _fetch_collection(
        self,
        url: str,
        *,
        event_type: str | None,
        accept: str = "application/vnd.github+json",
    ) -> tuple[dict[str, Any], ...]:
        decoded = self._get_json(url, accept=accept)
        if not isinstance(decoded, list) or any(not isinstance(item, dict) for item in decoded):
            raise _protocol_error(200)
        if event_type is None:
            return tuple(decoded)
        return tuple({**item, "event_type": event_type} for item in decoded)

    def fetch_pull_timeline(
        self, subject_url: str, *, repository: str, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        limit = _bounded_limit(limit)
        owner, name, kind, number = _subject_coordinates(subject_url, repository)
        if kind != "pulls":
            raise ValueError("GitHub hydration URL must identify a pull request")
        url = (
            f"{GITHUB_API_BASE}/repos/{quote(owner)}/{quote(name)}/issues/{number}/timeline?"
            + urlencode({"per_page": str(limit)})
        )
        events = self._fetch_collection(
            url,
            event_type=None,
            accept="application/vnd.github+json",
        )
        return tuple(
            {
                **item,
                "event_type": (
                    str(item.get("event")).casefold()
                    if isinstance(item.get("event"), str)
                    else "timeline"
                ),
            }
            for item in events
        )

    def fetch_pull_reviews(
        self, subject_url: str, *, repository: str, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        limit = _bounded_limit(limit)
        owner, name, kind, number = _subject_coordinates(subject_url, repository)
        if kind != "pulls":
            return ()
        url = (
            f"{GITHUB_API_BASE}/repos/{quote(owner)}/{quote(name)}/pulls/{number}/reviews?"
            + urlencode({"per_page": str(limit)})
        )
        return self._fetch_collection(url, event_type="review")

    def fetch_pull_review_comments(
        self, subject_url: str, *, repository: str, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        limit = _bounded_limit(limit)
        owner, name, kind, number = _subject_coordinates(subject_url, repository)
        if kind != "pulls":
            return ()
        url = (
            f"{GITHUB_API_BASE}/repos/{quote(owner)}/{quote(name)}/pulls/{number}/comments?"
            + urlencode({"per_page": str(limit)})
        )
        return self._fetch_collection(url, event_type="review_comment")

    def is_active_team_member(self, team_slug: str, username: str) -> bool:
        if not isinstance(team_slug, str) or team_slug.count("/") != 1:
            raise ValueError("team_slug must be an organization/team pair")
        organization, slug = team_slug.split("/", 1)
        if (
            _LOGIN_RE.fullmatch(organization) is None
            or _TEAM_SLUG_RE.fullmatch(slug) is None
            or not isinstance(username, str)
            or _LOGIN_RE.fullmatch(username) is None
        ):
            raise ValueError("team_slug and username must be GitHub identifiers")
        url = (
            f"{GITHUB_API_BASE}/orgs/{quote(organization)}/teams/{quote(slug)}/"
            f"memberships/{quote(username)}"
        )
        decoded = self._get_json(url, allow_not_found=True)
        if decoded is None:
            return False
        if not isinstance(decoded, dict):
            raise _protocol_error(200)
        return decoded.get("state") == "active"
