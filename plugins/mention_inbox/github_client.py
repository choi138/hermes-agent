"""Read-only GitHub Notifications REST client."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_POLL_INTERVAL_SECONDS = 60


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

    def get_authenticated_user_id(self) -> str:
        request = Request(
            f"{GITHUB_API_BASE}/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "hermes-agent-mention-inbox",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            method="GET",
        )
        response = self._transport(request, self._timeout_seconds)
        _raise_for_status(response)
        decoded = _decode_json(response)
        if not isinstance(decoded, dict):
            raise _protocol_error(response.status)
        node_id = decoded.get("node_id")
        if not isinstance(node_id, str) or not node_id or node_id != node_id.strip():
            raise _protocol_error(response.status)
        return node_id

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
