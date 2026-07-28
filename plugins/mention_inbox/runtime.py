"""One-shot GitHub mention polling orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from plugins.mention_inbox.github_client import (
    GitHubClientError,
    GitHubNotificationsClient,
)
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.store import MentionInboxStore

Clock = Callable[[], datetime]
_CURSOR_KEY = "github.notifications"
_DEFAULT_RETRY_SECONDS = 60
_NON_RETRYABLE_DELAY_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600


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
    ) -> None:
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= 100
        ):
            raise ValueError("max_pages must be an integer between 1 and 100")
        self._client = client
        self._collector = collector
        self._store = store
        self._clock = clock
        self._max_pages = max_pages

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

    def _poll_once_success(self) -> GitHubPollResult:
        cursor = self._store.get_cursor(_CURSOR_KEY)
        page = self._client.list_notifications(if_modified_since=cursor)
        interval = page.poll_interval_seconds
        now = self._clock()

        if page.not_modified:
            self._store.record_poll_success(
                _CURSOR_KEY,
                next_poll_at=_next_poll_at(now, interval),
            )
            return GitHubPollResult(
                status="ok",
                pages=1,
                fetched=0,
                selected=0,
                created=0,
                updated=0,
                skipped=0,
                not_modified=True,
                next_poll_seconds=interval,
                error_category=None,
            )

        pages = 0
        fetched = 0
        selected = 0
        created = 0
        updated = 0
        skipped = 0
        last_modified = None
        while True:
            pages += 1
            fetched += len(page.items)
            interval = max(interval, page.poll_interval_seconds)
            if page.last_modified is not None:
                last_modified = page.last_modified

            for notification in page.items:
                if not self._collector.accepts(notification):
                    skipped += 1
                    continue
                selected += 1
                detail = None
                subject_url = _subject_url(notification)
                if subject_url is not None:
                    try:
                        detail = self._client.fetch_subject(subject_url)
                    except ValueError:
                        detail = None
                try:
                    collected = self._collector.normalize(notification, detail)
                except ValueError:
                    skipped += 1
                    continue
                if collected is None:
                    skipped += 1
                    continue
                result = self._store.upsert(
                    collected.event,
                    source_revision=collected.source_revision,
                )
                if result.created:
                    created += 1
                elif result.content_changed:
                    updated += 1

            if page.next_url is None:
                break
            if pages >= self._max_pages:
                raise GitHubClientError(
                    category="pagination_limit",
                    status=None,
                    retryable=False,
                )
            page = self._client.list_notifications(page_url=page.next_url)

        if last_modified is not None:
            self._store.set_cursor(_CURSOR_KEY, last_modified)
        self._store.record_poll_success(
            _CURSOR_KEY,
            next_poll_at=_next_poll_at(now, interval),
        )
        return GitHubPollResult(
            status="ok",
            pages=pages,
            fetched=fetched,
            selected=selected,
            created=created,
            updated=updated,
            skipped=skipped,
            not_modified=False,
            next_poll_seconds=interval,
            error_category=None,
        )
