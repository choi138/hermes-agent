"""Bounded read-side client for the Notion polling pilot."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_NOTION_OBJECT_ID = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)


def _validated_object_id(value: Any, location: str = "object_id") -> str:
    if not isinstance(value, str) or _NOTION_OBJECT_ID.fullmatch(value) is None:
        raise ValueError(f"{location} must be a Notion UUID")
    return value


@dataclass(frozen=True)
class NotionHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class NotionResultPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    has_more: bool


NotionTransport = Callable[[Request, float], NotionHttpResponse]


class NotionClientError(RuntimeError):
    """Secret-safe Notion transport or protocol failure metadata."""

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
            f"Notion API request failed: category={category}, status={status_label}"
        )


def _stdlib_transport(request: Request, timeout: float) -> NotionHttpResponse:
    try:
        with urlopen(request, timeout=timeout) as response:
            return NotionHttpResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except HTTPError as exc:
        headers = dict(exc.headers.items()) if exc.headers is not None else {}
        return NotionHttpResponse(
            status=exc.code,
            headers=headers,
            body=exc.read(),
        )
    except (URLError, TimeoutError, OSError) as exc:
        raise NotionClientError(
            category="transport_error",
            status=None,
            retryable=True,
        ) from exc


def _decode_object(response: NotionHttpResponse) -> dict[str, Any]:
    try:
        decoded = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotionClientError(
            category="protocol_error", status=response.status, retryable=True
        ) from exc
    if not isinstance(decoded, dict):
        raise NotionClientError(
            category="protocol_error", status=response.status, retryable=True
        )
    return decoded


def _raise_for_status(response: NotionHttpResponse) -> None:
    if response.status == 200:
        return
    retry_after_seconds: int | None = None
    if response.status == 429:
        retry_after_raw = next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() == "retry-after"
            ),
            None,
        )
        try:
            parsed_retry_after = int(retry_after_raw) if retry_after_raw is not None else None
        except (TypeError, ValueError):
            parsed_retry_after = None
        if parsed_retry_after is not None and 1 <= parsed_retry_after <= 86_400:
            retry_after_seconds = parsed_retry_after
    if response.status == 401:
        category, retryable = "unauthorized", False
    elif response.status == 403:
        category, retryable = "forbidden", False
    elif response.status == 404:
        category, retryable = "not_found", False
    elif response.status == 429:
        category, retryable = "rate_limited", True
    elif 500 <= response.status <= 599:
        category, retryable = "server_error", True
    else:
        category, retryable = "client_error", False
    raise NotionClientError(
        category=category,
        status=response.status,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


class NotionReadClient:
    """Explicit Notion read endpoints; no create/update/archive methods exist."""

    def __init__(
        self,
        *,
        token: str,
        transport: NotionTransport | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        min_request_interval_seconds: float = 1 / 3,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or "\r" in token
            or "\n" in token
        ):
            raise ValueError("Notion token must be a non-empty header-safe string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if min_request_interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must not be negative")
        self._token = token
        self._transport = transport or _stdlib_transport
        self._timeout_seconds = timeout_seconds
        self._min_request_interval_seconds = min_request_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_started_at: float | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_API_VERSION,
            "User-Agent": "hermes-agent-mention-inbox",
        }

    def _send(self, request: Request) -> NotionHttpResponse:
        now = self._monotonic()
        if self._last_request_started_at is not None:
            elapsed = now - self._last_request_started_at
            remaining = self._min_request_interval_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started_at = now
        return self._transport(request, self._timeout_seconds)

    def get_target_user_id(self) -> str:
        request = Request(
            f"{NOTION_API_BASE}/users/me",
            headers=self._headers(),
            method="GET",
        )
        response = self._send(request)
        _raise_for_status(response)
        decoded = _decode_object(response)
        bot = decoded.get("bot")
        owner = bot.get("owner") if isinstance(bot, Mapping) else None
        user = owner.get("user") if isinstance(owner, Mapping) else None
        target_id = user.get("id") if isinstance(user, Mapping) else None
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id != target_id.strip()
        ):
            raise NotionClientError(
                category="protocol_error", status=response.status, retryable=True
            )
        return target_id

    def search_pages(
        self,
        *,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionResultPage:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be an integer between 1 and 100")
        if start_cursor is not None and (
            not isinstance(start_cursor, str) or not start_cursor or start_cursor != start_cursor.strip()
        ):
            raise ValueError("start_cursor must be a non-empty trimmed string or None")
        payload: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "page_size": page_size,
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        if start_cursor is not None:
            payload["start_cursor"] = start_cursor
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        request = Request(
            f"{NOTION_API_BASE}/search",
            data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            headers=headers,
            method="POST",
        )
        response = self._send(request)
        _raise_for_status(response)
        decoded = _decode_object(response)
        results = decoded.get("results")
        has_more = decoded.get("has_more")
        next_cursor = decoded.get("next_cursor")
        if (
            decoded.get("object") != "list"
            or not isinstance(results, list)
            or any(not isinstance(item, dict) for item in results)
            or not isinstance(has_more, bool)
            or (next_cursor is not None and not isinstance(next_cursor, str))
            or (has_more and not next_cursor)
        ):
            raise NotionClientError(
                category="protocol_error", status=response.status, retryable=True
            )
        return NotionResultPage(
            items=tuple(results),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def list_block_children(
        self,
        block_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionResultPage:
        safe_id = _validated_object_id(block_id, "block_id")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be an integer between 1 and 100")
        if start_cursor is not None and (
            not isinstance(start_cursor, str) or not start_cursor or start_cursor != start_cursor.strip()
        ):
            raise ValueError("start_cursor must be a non-empty trimmed string or None")
        query: dict[str, str | int] = {"page_size": page_size}
        if start_cursor is not None:
            query["start_cursor"] = start_cursor
        request = Request(
            f"{NOTION_API_BASE}/blocks/{safe_id}/children?{urlencode(query)}",
            headers=self._headers(),
            method="GET",
        )
        response = self._send(request)
        _raise_for_status(response)
        decoded = _decode_object(response)
        results = decoded.get("results")
        has_more = decoded.get("has_more")
        next_cursor = decoded.get("next_cursor")
        if (
            decoded.get("object") != "list"
            or not isinstance(results, list)
            or any(not isinstance(item, dict) for item in results)
            or not isinstance(has_more, bool)
            or (next_cursor is not None and not isinstance(next_cursor, str))
            or (has_more and not next_cursor)
        ):
            raise NotionClientError(
                category="protocol_error", status=response.status, retryable=True
            )
        return NotionResultPage(
            items=tuple(results),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def list_comments(
        self,
        block_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionResultPage:
        safe_id = _validated_object_id(block_id, "block_id")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page_size must be an integer between 1 and 100")
        if start_cursor is not None and (
            not isinstance(start_cursor, str) or not start_cursor or start_cursor != start_cursor.strip()
        ):
            raise ValueError("start_cursor must be a non-empty trimmed string or None")
        query: dict[str, str | int] = {
            "block_id": safe_id,
            "page_size": page_size,
        }
        if start_cursor is not None:
            query["start_cursor"] = start_cursor
        request = Request(
            f"{NOTION_API_BASE}/comments?{urlencode(query)}",
            headers=self._headers(),
            method="GET",
        )
        response = self._send(request)
        _raise_for_status(response)
        decoded = _decode_object(response)
        results = decoded.get("results")
        has_more = decoded.get("has_more")
        next_cursor = decoded.get("next_cursor")
        if (
            decoded.get("object") != "list"
            or not isinstance(results, list)
            or any(not isinstance(item, dict) for item in results)
            or not isinstance(has_more, bool)
            or (next_cursor is not None and not isinstance(next_cursor, str))
            or (has_more and not next_cursor)
        ):
            raise NotionClientError(
                category="protocol_error", status=response.status, retryable=True
            )
        return NotionResultPage(
            items=tuple(results),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        safe_id = _validated_object_id(page_id, "page_id")
        request = Request(
            f"{NOTION_API_BASE}/pages/{safe_id}",
            headers=self._headers(),
            method="GET",
        )
        response = self._send(request)
        _raise_for_status(response)
        decoded = _decode_object(response)
        if decoded.get("object") != "page" or decoded.get("id") != safe_id:
            raise NotionClientError(
                category="protocol_error", status=response.status, retryable=True
            )
        return decoded
