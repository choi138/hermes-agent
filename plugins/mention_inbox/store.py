"""Profile-scoped SQLite persistence for canonical mention events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from hermes_constants import get_hermes_home
from plugins.mention_inbox.contract import (
    ApprovalState,
    MentionEvent,
    event_to_dict,
    event_to_json,
    restore_event,
    transition_approval as transition_event_approval,
)

Clock = Callable[[], datetime]
DEFAULT_DESTINATION = "discord:1526407515313668247"
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class UpsertResult:
    created: bool
    content_changed: bool
    stale: bool
    revision_number: int


@dataclass(frozen=True)
class StoredMention:
    event: MentionEvent
    source_revision: str
    revision_number: int
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class CollectorStatus:
    collector_key: str
    status: str
    error_category: str | None
    consecutive_failures: int
    last_attempt_at: datetime
    last_success_at: datetime | None
    next_poll_at: datetime


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: int
    event: MentionEvent
    revision_number: int
    destination: str
    marker: str
    attempts: int
    requires_reconciliation: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _require_collector_key(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("collector_key must be a non-empty trimmed string")
    return value


def _require_error_category(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or value != value.casefold()
        or not value.replace("_", "").isalnum()
    ):
        raise ValueError("error_category must be a bounded lowercase identifier")
    return value


def _collector_status(row: sqlite3.Row) -> CollectorStatus:
    return CollectorStatus(
        collector_key=str(row["collector_key"]),
        status=str(row["status"]),
        error_category=(
            None if row["error_category"] is None else str(row["error_category"])
        ),
        consecutive_failures=int(row["consecutive_failures"]),
        last_attempt_at=_parse_datetime(str(row["last_attempt_at"])),
        last_success_at=_optional_datetime(row["last_success_at"]),
        next_poll_at=_parse_datetime(str(row["next_poll_at"])),
    )


def _content_hash(event: MentionEvent) -> str:
    payload = event_to_dict(event)
    payload["approval_state"] = "pending"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MentionInboxStore:
    """Durable canonical events, revisions, cursors, and collector status."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        clock: Clock = _utc_now,
        delivery_destinations: tuple[str, ...] = (DEFAULT_DESTINATION,),
    ) -> None:
        self.path = Path(db_path or (get_hermes_home() / "mention_inbox" / "inbox.db"))
        self._clock = clock
        self._delivery_destinations = delivery_destinations
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            stored_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if stored_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {stored_version} is newer than "
                    f"supported schema version {SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS mention_events (
                    dedupe_key TEXT PRIMARY KEY,
                    source_platform TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS collector_cursors (
                    collector_key TEXT PRIMARY KEY,
                    cursor_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS collector_status (
                    collector_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error_category TEXT,
                    consecutive_failures INTEGER NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    next_poll_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    destination TEXT NOT NULL,
                    marker TEXT NOT NULL,
                    event_json TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT,
                    lease_until TEXT,
                    error_category TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(dedupe_key, revision_number, destination)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS mention_event_lineage (
                    dedupe_key TEXT PRIMARY KEY,
                    latest_revision INTEGER NOT NULL,
                    latest_source_revision TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            outbox_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(delivery_outbox)")
            }
            if "event_json" not in outbox_columns:
                connection.execute("ALTER TABLE delivery_outbox ADD COLUMN event_json TEXT")
            connection.execute("""
                UPDATE delivery_outbox
                SET event_json = (
                    SELECT e.event_json FROM mention_events e
                    WHERE e.dedupe_key = delivery_outbox.dedupe_key
                      AND e.revision_number = delivery_outbox.revision_number
                )
                WHERE event_json IS NULL AND EXISTS (
                    SELECT 1 FROM mention_events e
                    WHERE e.dedupe_key = delivery_outbox.dedupe_key
                      AND e.revision_number = delivery_outbox.revision_number
                )
            """)
            connection.execute("""
                UPDATE delivery_outbox
                SET status = 'superseded', lease_until = NULL
                WHERE status IN ('pending', 'sending') AND event_json IS NULL
            """)
            connection.execute("""
                INSERT OR IGNORE INTO mention_event_lineage (
                    dedupe_key, latest_revision, latest_source_revision, updated_at
                )
                SELECT dedupe_key, revision_number, source_revision, last_seen_at
                FROM mention_events
            """)
            connection.execute("""
                INSERT OR IGNORE INTO mention_event_lineage (
                    dedupe_key, latest_revision, latest_source_revision, updated_at
                )
                SELECT dedupe_key, MAX(revision_number), NULL, MAX(updated_at)
                FROM delivery_outbox GROUP BY dedupe_key
            """)
            connection.execute("""
                UPDATE mention_event_lineage
                SET latest_revision = MAX(
                    latest_revision,
                    COALESCE((
                        SELECT MAX(o.revision_number) FROM delivery_outbox o
                        WHERE o.dedupe_key = mention_event_lineage.dedupe_key
                    ), latest_revision),
                    COALESCE((
                        SELECT e.revision_number FROM mention_events e
                        WHERE e.dedupe_key = mention_event_lineage.dedupe_key
                    ), latest_revision)
                ),
                latest_source_revision = COALESCE((
                    SELECT e.source_revision FROM mention_events e
                    WHERE e.dedupe_key = mention_event_lineage.dedupe_key
                ), latest_source_revision)
            """)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _enqueue_deliveries(
        self,
        connection: sqlite3.Connection,
        event: MentionEvent,
        revision_number: int,
        now: str,
    ) -> None:
        event_json = event_to_json(event)
        for destination in self._delivery_destinations:
            identity = f"{event.dedupe_key}\0{revision_number}\0{destination}"
            marker = "[hermes-inbox:" + hashlib.sha256(identity.encode()).hexdigest()[:24] + "]"
            connection.execute(
                """
                INSERT OR IGNORE INTO delivery_outbox (
                    dedupe_key, revision_number, destination, marker, event_json,
                    status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event.dedupe_key,
                    revision_number,
                    destination,
                    marker,
                    event_json,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _record_lineage(
        connection: sqlite3.Connection,
        *,
        dedupe_key: str,
        revision_number: int,
        source_revision: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO mention_event_lineage (
                dedupe_key, latest_revision, latest_source_revision, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                latest_revision = MAX(latest_revision, excluded.latest_revision),
                latest_source_revision = excluded.latest_source_revision,
                updated_at = excluded.updated_at
            """,
            (dedupe_key, revision_number, source_revision, now),
        )

    def upsert(
        self,
        event: MentionEvent,
        *,
        source_revision: str,
    ) -> UpsertResult:
        if not isinstance(event, MentionEvent):
            raise ValueError("event must be a MentionEvent")
        if not isinstance(source_revision, str) or not source_revision:
            raise ValueError("source_revision must be a non-empty string")
        incoming_revision = _parse_datetime(source_revision)
        now = _iso_datetime(self._clock())
        incoming_hash = _content_hash(event)
        incoming_json = event_to_json(event)
        connection = self._connect()
        try:
            with connection:
                existing = connection.execute(
                    "SELECT content_hash, source_revision, revision_number "
                    "FROM mention_events WHERE dedupe_key = ?",
                    (event.dedupe_key,),
                ).fetchone()
                lineage = connection.execute(
                    """
                    SELECT latest_revision, latest_source_revision
                    FROM mention_event_lineage WHERE dedupe_key = ?
                    """,
                    (event.dedupe_key,),
                ).fetchone()
                if existing is not None and incoming_revision < _parse_datetime(
                    existing["source_revision"]
                ):
                    connection.execute(
                        "UPDATE mention_events SET last_seen_at = ? WHERE dedupe_key = ?",
                        (now, event.dedupe_key),
                    )
                    return UpsertResult(
                        created=False,
                        content_changed=False,
                        stale=True,
                        revision_number=existing["revision_number"],
                    )
                if existing is not None and existing["content_hash"] == incoming_hash:
                    connection.execute(
                        """
                        UPDATE mention_events
                        SET source_revision = ?, last_seen_at = ?
                        WHERE dedupe_key = ?
                        """,
                        (source_revision, now, event.dedupe_key),
                    )
                    self._record_lineage(
                        connection,
                        dedupe_key=event.dedupe_key,
                        revision_number=int(existing["revision_number"]),
                        source_revision=source_revision,
                        now=now,
                    )
                    return UpsertResult(
                        created=False,
                        content_changed=False,
                        stale=False,
                        revision_number=existing["revision_number"],
                    )
                if existing is not None:
                    revision_number = max(
                        int(existing["revision_number"]),
                        0 if lineage is None else int(lineage["latest_revision"]),
                    ) + 1
                    connection.execute(
                        """
                        UPDATE mention_events
                        SET source_revision = ?, content_hash = ?, event_json = ?,
                            revision_number = ?, last_seen_at = ?
                        WHERE dedupe_key = ?
                        """,
                        (
                            source_revision,
                            incoming_hash,
                            incoming_json,
                            revision_number,
                            now,
                            event.dedupe_key,
                        ),
                    )
                    self._record_lineage(
                        connection,
                        dedupe_key=event.dedupe_key,
                        revision_number=revision_number,
                        source_revision=source_revision,
                        now=now,
                    )
                    self._enqueue_deliveries(connection, event, revision_number, now)
                    return UpsertResult(
                        created=False,
                        content_changed=True,
                        stale=False,
                        revision_number=revision_number,
                    )

                if (
                    lineage is not None
                    and lineage["latest_source_revision"] is not None
                    and incoming_revision
                    <= _parse_datetime(str(lineage["latest_source_revision"]))
                ):
                    return UpsertResult(
                        created=False,
                        content_changed=False,
                        stale=True,
                        revision_number=int(lineage["latest_revision"]),
                    )
                revision_number = (
                    1 if lineage is None else int(lineage["latest_revision"]) + 1
                )
                connection.execute(
                    """
                    INSERT INTO mention_events (
                        dedupe_key,
                        source_platform,
                        source_event_id,
                        source_revision,
                        content_hash,
                        event_json,
                        revision_number,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.dedupe_key,
                        event.source.platform.value,
                        event.source.event_id,
                        source_revision,
                        incoming_hash,
                        incoming_json,
                        revision_number,
                        now,
                        now,
                    ),
                )
                self._record_lineage(
                    connection,
                    dedupe_key=event.dedupe_key,
                    revision_number=revision_number,
                    source_revision=source_revision,
                    now=now,
                )
                self._enqueue_deliveries(connection, event, revision_number, now)
                return UpsertResult(
                    created=True,
                    content_changed=True,
                    stale=False,
                    revision_number=revision_number,
                )
        finally:
            connection.close()

    def get_cursor(self, collector_key: str) -> str | None:
        if not isinstance(collector_key, str) or not collector_key:
            raise ValueError("collector_key must be a non-empty string")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT cursor_value FROM collector_cursors WHERE collector_key = ?",
                (collector_key,),
            ).fetchone()
            return None if row is None else str(row["cursor_value"])
        finally:
            connection.close()

    def set_cursor(self, collector_key: str, cursor_value: str) -> None:
        if not isinstance(collector_key, str) or not collector_key:
            raise ValueError("collector_key must be a non-empty string")
        if not isinstance(cursor_value, str) or not cursor_value:
            raise ValueError("cursor_value must be a non-empty string")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO collector_cursors (
                        collector_key, cursor_value, updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(collector_key) DO UPDATE SET
                        cursor_value = excluded.cursor_value,
                        updated_at = excluded.updated_at
                    """,
                    (collector_key, cursor_value, now),
                )
        finally:
            connection.close()

    def get_collector_status(self, collector_key: str) -> CollectorStatus | None:
        key = _require_collector_key(collector_key)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM collector_status WHERE collector_key = ?",
                (key,),
            ).fetchone()
            return None if row is None else _collector_status(row)
        finally:
            connection.close()

    def record_poll_failure(
        self,
        collector_key: str,
        *,
        error_category: str,
        next_poll_at: datetime,
    ) -> CollectorStatus:
        key = _require_collector_key(collector_key)
        category = _require_error_category(error_category)
        attempted_at = _iso_datetime(self._clock())
        next_at = _iso_datetime(next_poll_at)
        connection = self._connect()
        try:
            with connection:
                previous = connection.execute(
                    "SELECT consecutive_failures, last_success_at "
                    "FROM collector_status WHERE collector_key = ?",
                    (key,),
                ).fetchone()
                failures = (
                    1 if previous is None else int(previous["consecutive_failures"]) + 1
                )
                last_success = None if previous is None else previous["last_success_at"]
                connection.execute(
                    """
                    INSERT INTO collector_status (
                        collector_key, status, error_category,
                        consecutive_failures, last_attempt_at,
                        last_success_at, next_poll_at
                    ) VALUES (?, 'error', ?, ?, ?, ?, ?)
                    ON CONFLICT(collector_key) DO UPDATE SET
                        status = excluded.status,
                        error_category = excluded.error_category,
                        consecutive_failures = excluded.consecutive_failures,
                        last_attempt_at = excluded.last_attempt_at,
                        last_success_at = excluded.last_success_at,
                        next_poll_at = excluded.next_poll_at
                    """,
                    (
                        key,
                        category,
                        failures,
                        attempted_at,
                        last_success,
                        next_at,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM collector_status WHERE collector_key = ?",
                    (key,),
                ).fetchone()
                return _collector_status(row)
        finally:
            connection.close()

    def record_poll_success(
        self,
        collector_key: str,
        *,
        next_poll_at: datetime,
    ) -> CollectorStatus:
        key = _require_collector_key(collector_key)
        attempted_at = _iso_datetime(self._clock())
        next_at = _iso_datetime(next_poll_at)
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO collector_status (
                        collector_key, status, error_category,
                        consecutive_failures, last_attempt_at,
                        last_success_at, next_poll_at
                    ) VALUES (?, 'ok', NULL, 0, ?, ?, ?)
                    ON CONFLICT(collector_key) DO UPDATE SET
                        status = excluded.status,
                        error_category = NULL,
                        consecutive_failures = 0,
                        last_attempt_at = excluded.last_attempt_at,
                        last_success_at = excluded.last_success_at,
                        next_poll_at = excluded.next_poll_at
                    """,
                    (key, attempted_at, attempted_at, next_at),
                )
                row = connection.execute(
                    "SELECT * FROM collector_status WHERE collector_key = ?",
                    (key,),
                ).fetchone()
                return _collector_status(row)
        finally:
            connection.close()

    def transition_approval(
        self,
        dedupe_key: str,
        requested_state: ApprovalState,
    ) -> MentionEvent:
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT event_json FROM mention_events WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("mention event not found")
                current = restore_event(json.loads(row["event_json"]))
                updated = transition_event_approval(current, requested_state)
                connection.execute(
                    "UPDATE mention_events SET event_json = ? WHERE dedupe_key = ?",
                    (event_to_json(updated), dedupe_key),
                )
                return updated
        finally:
            connection.close()

    def get(self, dedupe_key: str) -> StoredMention | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM mention_events WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return StoredMention(
            event=restore_event(json.loads(row["event_json"])),
            source_revision=row["source_revision"],
            revision_number=row["revision_number"],
            first_seen_at=_parse_datetime(row["first_seen_at"]),
            last_seen_at=_parse_datetime(row["last_seen_at"]),
        )

    def pending_delivery_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM delivery_outbox "
                "WHERE status IN ('pending', 'sending')"
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def claim_delivery(self, destination: str, *, lease_seconds: int) -> DeliveryClaim | None:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        now_dt = self._clock().astimezone(timezone.utc)
        now = _iso_datetime(now_dt)
        lease_until = _iso_datetime(now_dt + timedelta(seconds=lease_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT o.*
                FROM delivery_outbox o
                WHERE o.destination = ? AND (
                    o.status = 'pending' OR
                    (o.status = 'sending' AND o.lease_until <= ?)
                ) AND o.event_json IS NOT NULL
                ORDER BY o.delivery_id LIMIT 1
                """,
                (destination, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            uncertain = str(row["status"]) == "sending"
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE delivery_outbox
                SET status = 'sending', attempts = ?, lease_until = ?,
                    error_category = NULL, updated_at = ?
                WHERE delivery_id = ?
                """,
                (attempts, lease_until, now, row["delivery_id"]),
            )
            connection.commit()
            return DeliveryClaim(
                delivery_id=int(row["delivery_id"]),
                event=restore_event(json.loads(row["event_json"])),
                revision_number=int(row["revision_number"]),
                destination=str(row["destination"]),
                marker=str(row["marker"]),
                attempts=attempts,
                requires_reconciliation=uncertain,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_delivery(self, delivery_id: int, *, error_category: str) -> None:
        category = _require_error_category(error_category)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'pending', lease_until = NULL,
                        error_category = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'sending'
                    """,
                    (category, now, delivery_id),
                )
        finally:
            connection.close()

    def mark_delivery_sent(self, delivery_id: int, *, message_id: str) -> None:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'sent', message_id = ?, lease_until = NULL,
                        error_category = NULL, updated_at = ?
                    WHERE delivery_id = ?
                    """,
                    (message_id, now, delivery_id),
                )
        finally:
            connection.close()

    def health(self, collector_key: str) -> dict[str, object]:
        status = self.get_collector_status(collector_key)
        return {
            "status": "degraded" if status is None else status.status,
            "error_category": "not_started" if status is None else status.error_category,
            "last_attempt_at": None if status is None else status.last_attempt_at,
            "last_success_at": None if status is None else status.last_success_at,
            "next_poll_at": None if status is None else status.next_poll_at,
            "consecutive_failures": 0 if status is None else status.consecutive_failures,
            "pending_delivery_count": self.pending_delivery_count(),
        }

    def prune(self, *, retention_days: int) -> int:
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        cutoff = _iso_datetime(self._clock() - timedelta(days=retention_days))
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET event_json = NULL
                    WHERE status = 'sent' AND event_json IS NOT NULL
                      AND dedupe_key IN (
                        SELECT e.dedupe_key FROM mention_events e
                        WHERE e.last_seen_at < ? AND NOT EXISTS (
                            SELECT 1 FROM delivery_outbox pending
                            WHERE pending.dedupe_key = e.dedupe_key
                              AND pending.status IN ('pending', 'sending')
                        )
                      )
                    """,
                    (cutoff,),
                )
                cursor = connection.execute(
                    """
                    DELETE FROM mention_events
                    WHERE last_seen_at < ? AND NOT EXISTS (
                        SELECT 1 FROM delivery_outbox o
                        WHERE o.dedupe_key = mention_events.dedupe_key
                          AND o.status IN ('pending', 'sending')
                    )
                    """,
                    (cutoff,),
                )
                return int(cursor.rowcount)
        finally:
            connection.close()

    def delivery_audit(self, delivery_id: int) -> dict[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT delivery_id, dedupe_key, revision_number, destination,
                       marker, status, attempts, message_id, error_category,
                       created_at, updated_at
                FROM delivery_outbox WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            connection.close()

    def count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("SELECT COUNT(*) FROM mention_events").fetchone()
            return int(row[0])
        finally:
            connection.close()
