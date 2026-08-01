"""One-shot GitHub mention polling orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from plugins.mention_inbox.github_client import (
    GitHubClientError,
    GitHubNotificationPage,
    GitHubNotificationsClient,
)
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.store import MentionInboxStore

Clock = Callable[[], datetime]
_CURSOR_KEY = "github.notifications"
_READ_REPLAY_CURSOR_KEY = "github.notifications.read-replay"
_DEFAULT_RETRY_SECONDS = 60
_NON_RETRYABLE_DELAY_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600
_READ_REPLAY_OVERLAP = timedelta(minutes=5)
_MAX_READ_REPLAY_LOOKBACK = timedelta(days=7)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubPollResult:
    status: str
    pages: int
    fetched: int
    selected: int
    created: int
    updated: int
    skipped: int
    not_modified: bool
    next_poll_seconds: int
    error_category: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _next_poll_at(now: datetime, seconds: int) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc) + timedelta(seconds=seconds)


def _subject_url(notification: Mapping[str, Any]) -> str | None:
    subject = notification.get("subject")
    repository = notification.get("repository")
    if not isinstance(subject, Mapping) or not isinstance(repository, Mapping):
        return None
    value = subject.get("url")
    full_name = repository.get("full_name")
    if not isinstance(value, str) or not value or not isinstance(full_name, str):
        return None
    try:
        path_parts = urlsplit(value).path.split("/")
    except ValueError:
        return None
    if len(path_parts) != 6 or path_parts[1] != "repos":
        return None
    url_repository = f"{path_parts[2]}/{path_parts[3]}"
    return value if url_repository.casefold() == full_name.casefold() else None


def _backoff_seconds(error: GitHubClientError, previous_failures: int) -> int:
    if error.retry_after_seconds is not None:
        requested = max(1, error.retry_after_seconds)
    elif error.retryable:
        exponent = min(max(0, previous_failures), 6)
        requested = _DEFAULT_RETRY_SECONDS * (2**exponent)
    else:
        requested = _NON_RETRYABLE_DELAY_SECONDS
    return min(requested, _MAX_BACKOFF_SECONDS)


class GitHubMentionPoller:
    """Run one read-only notification poll; scheduling is an external concern."""

    def __init__(
        self,
        *,
        client: GitHubNotificationsClient,
        collector: GitHubNotificationCollector,
        store: MentionInboxStore,
        clock: Clock = _utc_now,
        max_pages: int = 20,
        read_replay_lookback: timedelta | None = None,
        max_replay_pages: int = 2,
    ) -> None:
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError("max_pages must be an integer between 1 and 100")
        if read_replay_lookback is not None and (
            not isinstance(read_replay_lookback, timedelta)
            or read_replay_lookback <= timedelta(0)
            or read_replay_lookback > _MAX_READ_REPLAY_LOOKBACK
        ):
            raise ValueError("read_replay_lookback must be between 0 and 7 days")
        if (
            isinstance(max_replay_pages, bool)
            or not isinstance(max_replay_pages, int)
            or not 1 <= max_replay_pages <= 10
        ):
            raise ValueError("max_replay_pages must be an integer between 1 and 10")
        self._client = client
        self._collector = collector
        self._store = store
        self._clock = clock
        self._max_pages = max_pages
        self._read_replay_lookback = read_replay_lookback
        self._max_replay_pages = max_replay_pages

    def poll_once(self) -> GitHubPollResult:
        try:
            return self._poll_once_success()
        except GitHubClientError as error:
            previous = self._store.get_collector_status(_CURSOR_KEY)
            previous_failures = 0 if previous is None else previous.consecutive_failures
            delay = _backoff_seconds(error, previous_failures)
            now = self._clock()
            self._store.record_poll_failure(
                _CURSOR_KEY,
                error_category=error.category,
                next_poll_at=_next_poll_at(now, delay),
            )
            return GitHubPollResult(
                status="error",
                pages=0,
                fetched=0,
                selected=0,
                created=0,
                updated=0,
                skipped=0,
                not_modified=False,
                next_poll_seconds=delay,
                error_category=error.category,
            )

    @staticmethod
    def _normalized_cursor(value: str | None) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _read_replay_since(self, now: datetime) -> datetime:
        assert self._read_replay_lookback is not None
        lower_bound = now - self._read_replay_lookback
        previous = self._normalized_cursor(
            self._store.get_cursor(_READ_REPLAY_CURSOR_KEY)
        )
        if previous is None:
            return lower_bound
        return max(lower_bound, previous - _READ_REPLAY_OVERLAP)

    def _normalize_batch(
        self,
        notification: Mapping[str, Any],
        detail: object,
    ) -> tuple[Any, ...]:
        normalizer = getattr(self._collector, "normalize_many", None)
        if callable(normalizer):
            collected = normalizer(notification, detail)
            if collected is None:
                return ()
            return tuple(collected)
        single = self._collector.normalize(notification, detail)
        return () if single is None else (single,)

    def _process_notification(
        self,
        notification: Mapping[str, Any],
        counters: dict[str, int],
    ) -> None:
        if not self._collector.accepts(notification):
            counters["skipped"] += 1
            return
        counters["selected"] += 1
        detail = None
        hydrator = getattr(self._collector, "hydrate", None)
        if callable(hydrator):
            try:
                detail = hydrator(notification)
            except ValueError:
                detail = None
        else:
            subject_url = _subject_url(notification)
            if subject_url is not None:
                try:
                    detail = self._client.fetch_subject(subject_url)
                except ValueError:
                    detail = None
        try:
            collected_batch = self._normalize_batch(notification, detail)
        except ValueError:
            counters["skipped"] += 1
            logger.debug(
                "GitHub mention notification skipped id=%s reason=invalid_hydration",
                notification.get("id"),
            )
            return
        if not collected_batch:
            counters["skipped"] += 1
            logger.debug(
                "GitHub mention notification skipped id=%s reason=non_actionable",
                notification.get("id"),
            )
            return

        created = 0
        updated = 0
        unchanged = 0
        for collected in collected_batch:
            result = self._store.upsert(
                collected.event,
                source_revision=collected.source_revision,
            )
            if result.created:
                counters["created"] += 1
                created += 1
            elif result.content_changed:
                counters["updated"] += 1
                updated += 1
            else:
                unchanged += 1
        log = logger.info if created or updated else logger.debug
        log(
            "GitHub mention notification processed id=%s actionable=%d "
            "created=%d updated=%d unchanged=%d",
            notification.get("id"),
            len(collected_batch),
            created,
            updated,
            unchanged,
        )

    def _consume_pages(
        self,
        page: GitHubNotificationPage,
        *,
        max_pages: int,
        counters: dict[str, int],
        seen_notification_ids: set[str],
        truncate_at_limit: bool = False,
    ) -> tuple[int, str | None, bool]:
        pages = 0
        interval = page.poll_interval_seconds
        last_modified = None
        truncated = False
        while True:
            pages += 1
            counters["pages"] += 1
            counters["fetched"] += len(page.items)
            interval = max(interval, page.poll_interval_seconds)
            if page.last_modified is not None:
                last_modified = page.last_modified
            for notification in page.items:
                notification_id = notification.get("id")
                if isinstance(notification_id, str):
                    if notification_id in seen_notification_ids:
                        continue
                    seen_notification_ids.add(notification_id)
                self._process_notification(notification, counters)
            if page.next_url is None:
                break
            if pages >= max_pages:
                if truncate_at_limit:
                    truncated = True
                    logger.warning(
                        "GitHub read notification replay truncated pages=%d",
                        pages,
                    )
                    break
                raise GitHubClientError(
                    category="pagination_limit",
                    status=None,
                    retryable=False,
                )
            page = self._client.list_notifications(page_url=page.next_url)
        return interval, last_modified, truncated

    def _poll_once_success(self) -> GitHubPollResult:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        counters = {
            "pages": 0,
            "fetched": 0,
            "selected": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
        }
        seen_notification_ids: set[str] = set()
        cursor = self._store.get_cursor(_CURSOR_KEY)
        unread_page = self._client.list_notifications(if_modified_since=cursor)
        interval, last_modified, _ = self._consume_pages(
            unread_page,
            max_pages=self._max_pages,
            counters=counters,
            seen_notification_ids=seen_notification_ids,
        )

        replay_fetched = 0
        if self._read_replay_lookback is not None:
            fetched_before = counters["fetched"]
            replay_page = self._client.list_notifications(
                include_read=True,
                since=self._read_replay_since(now),
            )
            replay_interval, _, _ = self._consume_pages(
                replay_page,
                max_pages=self._max_replay_pages,
                counters=counters,
                seen_notification_ids=seen_notification_ids,
                truncate_at_limit=True,
            )
            replay_fetched = counters["fetched"] - fetched_before
            interval = max(interval, replay_interval)

        if last_modified is not None:
            self._store.set_cursor(_CURSOR_KEY, last_modified)
        if self._read_replay_lookback is not None:
            self._store.set_cursor(
                _READ_REPLAY_CURSOR_KEY,
                now.isoformat().replace("+00:00", "Z"),
            )
        self._store.record_poll_success(
            _CURSOR_KEY,
            next_poll_at=_next_poll_at(now, interval),
        )
        return GitHubPollResult(
            status="ok",
            pages=counters["pages"],
            fetched=counters["fetched"],
            selected=counters["selected"],
            created=counters["created"],
            updated=counters["updated"],
            skipped=counters["skipped"],
            not_modified=unread_page.not_modified and replay_fetched == 0,
            next_poll_seconds=interval,
            error_category=None,
        )

    def replay_notification(self, notification_id: str) -> GitHubPollResult:
        """Hydrate one exact notification without advancing polling cursors."""

        notification = self._client.fetch_notification(notification_id)
        counters = {
            "pages": 1,
            "fetched": 0,
            "selected": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
        }
        if notification is None:
            counters["skipped"] = 1
        else:
            counters["fetched"] = 1
            self._process_notification(notification, counters)
        return GitHubPollResult(
            status="ok",
            pages=counters["pages"],
            fetched=counters["fetched"],
            selected=counters["selected"],
            created=counters["created"],
            updated=counters["updated"],
            skipped=counters["skipped"],
            not_modified=False,
            next_poll_seconds=0,
            error_category=None,
        )
