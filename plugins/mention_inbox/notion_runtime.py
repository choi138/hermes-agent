"""One-shot bounded Notion mention polling orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from plugins.mention_inbox.notion_client import NotionClientError, NotionReadClient
from plugins.mention_inbox.notion_collector import (
    NotionCollectedEvent,
    NotionMentionCollector,
)
from plugins.mention_inbox.store import MentionInboxStore

Clock = Callable[[], datetime]
COLLECTOR_KEY = "notion.selected_pages"
_DEFAULT_RETRY_SECONDS = 60
_NON_RETRYABLE_DELAY_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600


@dataclass(frozen=True)
class NotionPollResult:
    status: str
    roots: int
    api_pages: int
    fetched: int
    selected: int
    created: int
    updated: int
    skipped: int
    next_poll_seconds: int
    error_category: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _cursor(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _backoff_seconds(error: NotionClientError, previous_failures: int) -> int:
    if error.retry_after_seconds is not None:
        requested = max(1, error.retry_after_seconds)
    elif error.retryable:
        exponent = min(max(0, previous_failures), 6)
        requested = _DEFAULT_RETRY_SECONDS * (2**exponent)
    else:
        requested = _NON_RETRYABLE_DELAY_SECONDS
    return min(requested, _MAX_BACKOFF_SECONDS)


class NotionMentionPoller:
    """Scan explicitly selected roots with serial read-only Notion calls."""

    collector_key = COLLECTOR_KEY

    def __init__(
        self,
        *,
        client: NotionReadClient,
        page_ids: tuple[str, ...],
        store: MentionInboxStore,
        poll_interval_seconds: int = 300,
        clock: Clock = _utc_now,
        max_api_pages: int = 100,
        max_objects: int = 1000,
        max_depth: int = 16,
    ) -> None:
        if not page_ids or any(not isinstance(value, str) or not value for value in page_ids):
            raise ValueError("page_ids must contain at least one non-empty string")
        if len(set(page_ids)) != len(page_ids):
            raise ValueError("page_ids must not contain duplicates")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int)
            or not 120 <= poll_interval_seconds <= 300
        ):
            raise ValueError("poll_interval_seconds must be between 120 and 300")
        for name, value, upper in (
            ("max_api_pages", max_api_pages, 1000),
            ("max_objects", max_objects, 10000),
            ("max_depth", max_depth, 64),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{name} must be an integer between 1 and {upper}")
        self._client = client
        self._page_ids = page_ids
        self._store = store
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._max_api_pages = max_api_pages
        self._max_objects = max_objects
        self._max_depth = max_depth
        self._collector: NotionMentionCollector | None = None

    def poll_once(self) -> NotionPollResult:
        try:
            return self._poll_once_success()
        except NotionClientError as error:
            previous = self._store.get_collector_status(COLLECTOR_KEY)
            previous_failures = 0 if previous is None else previous.consecutive_failures
            delay = _backoff_seconds(error, previous_failures)
            now = _utc(self._clock())
            self._store.record_poll_failure(
                COLLECTOR_KEY,
                error_category=error.category,
                next_poll_at=now + timedelta(seconds=delay),
            )
            return NotionPollResult(
                status="error",
                roots=len(self._page_ids),
                api_pages=0,
                fetched=0,
                selected=0,
                created=0,
                updated=0,
                skipped=0,
                next_poll_seconds=delay,
                error_category=error.category,
            )

    def _poll_once_success(self) -> NotionPollResult:
        if self._collector is None:
            self._collector = NotionMentionCollector(
                target_user_id=self._client.get_target_user_id()
            )

        api_pages = 0
        fetched = 0
        selected = 0
        skipped = 0
        collected_events: list[NotionCollectedEvent] = []
        seen_objects: set[tuple[str, str]] = set()

        def consume(kind: str, payload: Mapping[str, Any], root_page_id: str) -> None:
            nonlocal fetched, selected, skipped
            fetched += 1
            if fetched > self._max_objects:
                raise NotionClientError(
                    category="object_limit", status=None, retryable=False
                )
            object_id = payload.get("id")
            identity = (kind, object_id) if isinstance(object_id, str) else None
            if identity is not None and identity in seen_objects:
                skipped += 1
                return
            if identity is not None:
                seen_objects.add(identity)
            try:
                if kind == "comment":
                    collected = self._collector.normalize_comment(
                        payload, root_page_id=root_page_id
                    )
                else:
                    collected = self._collector.normalize_block(
                        payload, root_page_id=root_page_id
                    )
            except ValueError:
                skipped += 1
                return
            if collected is None:
                skipped += 1
                return
            selected += 1
            collected_events.append(collected)

        def count_page() -> None:
            nonlocal api_pages
            api_pages += 1
            if api_pages > self._max_api_pages:
                raise NotionClientError(
                    category="pagination_limit", status=None, retryable=False
                )

        def scan_comments(root_page_id: str) -> None:
            cursor: str | None = None
            while True:
                page = self._client.list_comments(
                    root_page_id,
                    start_cursor=cursor,
                    page_size=100,
                )
                count_page()
                for item in page.items:
                    consume("comment", item, root_page_id)
                if not page.has_more:
                    return
                cursor = page.next_cursor

        def scan_children(parent_id: str, root_page_id: str, depth: int) -> None:
            if depth > self._max_depth:
                raise NotionClientError(
                    category="depth_limit", status=None, retryable=False
                )
            cursor: str | None = None
            child_containers: list[str] = []
            while True:
                page = self._client.list_block_children(
                    parent_id,
                    start_cursor=cursor,
                    page_size=100,
                )
                count_page()
                for item in page.items:
                    consume("block", item, root_page_id)
                    child_id = item.get("id")
                    if item.get("has_children") is True and isinstance(child_id, str):
                        child_containers.append(child_id)
                if not page.has_more:
                    break
                cursor = page.next_cursor
            for child_id in child_containers:
                scan_children(child_id, root_page_id, depth + 1)

        for root_page_id in self._page_ids:
            self._client.retrieve_page(root_page_id)
            scan_comments(root_page_id)
            scan_children(root_page_id, root_page_id, 1)

        created = 0
        updated = 0
        for collected in collected_events:
            result = self._store.upsert(
                collected.event,
                source_revision=collected.source_revision,
            )
            if result.created:
                created += 1
            elif result.content_changed:
                updated += 1

        now = _utc(self._clock())
        self._store.set_cursor(COLLECTOR_KEY, _cursor(now))
        self._store.record_poll_success(
            COLLECTOR_KEY,
            next_poll_at=now + timedelta(seconds=self._poll_interval_seconds),
        )
        return NotionPollResult(
            status="ok",
            roots=len(self._page_ids),
            api_pages=api_pages,
            fetched=fetched,
            selected=selected,
            created=created,
            updated=updated,
            skipped=skipped,
            next_poll_seconds=self._poll_interval_seconds,
            error_category=None,
        )
