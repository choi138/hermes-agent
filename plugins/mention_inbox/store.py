"""Profile-scoped SQLite persistence for canonical mention events."""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from hermes_constants import get_hermes_home
from plugins.mention_inbox.contract import (
    ApprovalState,
    MentionEvent,
    event_to_dict,
    event_to_json,
    restore_event,
    transition_approval as transition_event_approval,
)
from plugins.mention_inbox.proposals import (
    ProposalStatus,
    WorkProposal,
    proposal_to_json,
    restore_proposal,
    verify_proposal_hash,
)

Clock = Callable[[], datetime]
DEFAULT_DESTINATION = "discord:1531851208858275860"
SCHEMA_VERSION = 13
_DISCORD_MAX_SNOWFLAKE = (1 << 64) - 1


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
    source_revision: str
    revision_number: int
    destination: str
    marker: str
    message_id: str | None
    attempts: int
    requires_reconciliation: bool

    @property
    def token(self) -> int:
        return self.attempts


@dataclass(frozen=True)
class WorkItemSession:
    subject_key: str
    source_dedupe_key: str
    repository_node_id: str | None
    pr_node_id: str | None
    parent_message_id: str | None
    parent_channel_id: str | None
    discord_thread_id: str | None
    state: str
    last_event_revision: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ApprovalCASResult:
    approved: bool
    reason: str
    proposal: WorkProposal


@dataclass(frozen=True)
class ProposalMessageBinding:
    proposal: WorkProposal
    message_id: str
    approval_offered: bool


@dataclass(frozen=True)
class WorkExecution:
    execution_id: str
    proposal_id: str
    proposal_revision: int
    proposal_hash: str
    approval_message_id: str
    thread_id: str
    mode: str
    head_ref: str | None
    head_repository: str | None
    workspace: str | None
    status_message_id: str | None
    terminal_receipt_message_id: str | None
    status: str
    dispatch_id: str | None
    recovery_token: str | None
    owner_id: str | None
    evidence_json: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RecoverableExecution:
    execution: WorkExecution
    approver_user_id: str
    recovery_token: str | None = None


@dataclass(frozen=True)
class TerminalReceiptClaim:
    execution_id: str
    thread_id: str
    marker: str
    content: str
    claim_token: str
    requires_reconciliation: bool


class TerminalReceiptClaimLostError(ValueError):
    """The terminal receipt lease is expired, replaced, or foreign."""


_CLOSED_SESSION_STATES = frozenset({"completed", "rejected", "expired"})
_PROPOSAL_TRANSITIONS: dict[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.PENDING: frozenset(
        {ProposalStatus.REJECTED, ProposalStatus.NEEDS_REAPPROVAL}
    ),
    ProposalStatus.APPROVED: frozenset(
        {
            ProposalStatus.QUEUED,
            ProposalStatus.BLOCKED,
            ProposalStatus.NEEDS_REAPPROVAL,
        }
    ),
    ProposalStatus.NEEDS_REAPPROVAL: frozenset(
        {ProposalStatus.PENDING, ProposalStatus.REJECTED}
    ),
    ProposalStatus.QUEUED: frozenset(
        {ProposalStatus.RUNNING, ProposalStatus.BLOCKED, ProposalStatus.NEEDS_REAPPROVAL}
    ),
    ProposalStatus.RUNNING: frozenset(
        {ProposalStatus.VERIFYING, ProposalStatus.BLOCKED}
    ),
    ProposalStatus.VERIFYING: frozenset(
        {ProposalStatus.COMPLETED, ProposalStatus.BLOCKED}
    ),
    ProposalStatus.BLOCKED: frozenset(
        {ProposalStatus.QUEUED, ProposalStatus.NEEDS_REAPPROVAL}
    ),
    ProposalStatus.REJECTED: frozenset(),
    ProposalStatus.COMPLETED: frozenset(),
}


def _require_stable_text(value: str, name: str, *, limit: int = 500) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
    ):
        raise ValueError(f"{name} must be a bounded non-empty trimmed string")
    return value


def _require_discord_snowflake(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{5,19}", value) is None
        or int(value) > _DISCORD_MAX_SNOWFLAKE
    ):
        raise ValueError(f"{name} must be a valid Discord snowflake")
    return value


def _require_git_ref(value: str | None) -> str | None:
    if value is None:
        return None
    ref = _require_stable_text(value, "head_ref", limit=240)
    if (
        ref.startswith(("-", ".", "/"))
        or ref.endswith(("/", ".", ".lock"))
        or ".." in ref
        or "//" in ref
        or "@{" in ref
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref) is None
    ):
        raise ValueError("head_ref must be a safe Git branch ref")
    return ref


def _github_subject_identity(subject_key: str) -> tuple[str, str] | None:
    parts = subject_key.split(":")
    if len(parts) != 3 or parts[0] != "github" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def work_execution_id(proposal_id: str, revision: int) -> str:
    proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("revision must be a positive integer")
    identity = f"{proposal_key}\0{revision}"
    return "wx_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _require_repository(value: str | None) -> str | None:
    if value is None:
        return None
    repository = _require_stable_text(value, "head_repository", limit=200)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("head_repository must be an owner/repository name")
    return repository


def _require_workspace(value: str) -> str:
    workspace = _require_stable_text(value, "workspace", limit=1000)
    if "\\" in workspace or any(ord(char) < 32 for char in workspace):
        raise ValueError("workspace must be a canonical absolute POSIX path")
    parsed = PurePosixPath(workspace)
    if (
        not parsed.is_absolute()
        or parsed.as_posix() != workspace
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
        or workspace == "/"
    ):
        raise ValueError("workspace must be a canonical absolute POSIX path")
    return workspace


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


def _work_item_session(row: sqlite3.Row) -> WorkItemSession:
    return WorkItemSession(
        subject_key=str(row["subject_key"]),
        source_dedupe_key=str(row["source_dedupe_key"]),
        repository_node_id=(
            None
            if row["repository_node_id"] is None
            else str(row["repository_node_id"])
        ),
        pr_node_id=None if row["pr_node_id"] is None else str(row["pr_node_id"]),
        parent_message_id=(
            None if row["parent_message_id"] is None else str(row["parent_message_id"])
        ),
        parent_channel_id=(
            None if row["parent_channel_id"] is None else str(row["parent_channel_id"])
        ),
        discord_thread_id=(
            None if row["discord_thread_id"] is None else str(row["discord_thread_id"])
        ),
        state=str(row["state"]),
        last_event_revision=str(row["last_event_revision"]),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _stored_proposal(row: sqlite3.Row) -> WorkProposal:
    proposal = restore_proposal(str(row["proposal_json"]))
    if proposal.status.value != str(row["status"]):
        raise RuntimeError("stored proposal status disagrees with canonical JSON")
    if proposal.content_hash != str(row["proposal_hash"]):
        raise RuntimeError("stored proposal hash disagrees with canonical JSON")
    return proposal


def _proposal_message_binding(row: sqlite3.Row) -> ProposalMessageBinding:
    message_id = row["discord_message_id"]
    if message_id is None:
        raise RuntimeError("proposal message binding is incomplete")
    offered = int(row["approval_offered"])
    if offered not in {0, 1}:
        raise RuntimeError("stored approval capability is invalid")
    return ProposalMessageBinding(
        proposal=_stored_proposal(row),
        message_id=str(message_id),
        approval_offered=bool(offered),
    )


def _work_execution(row: sqlite3.Row) -> WorkExecution:
    return WorkExecution(
        execution_id=str(row["execution_id"]),
        proposal_id=str(row["proposal_id"]),
        proposal_revision=int(row["proposal_revision"]),
        proposal_hash=str(row["proposal_hash"]),
        approval_message_id=str(row["approval_message_id"]),
        thread_id=str(row["thread_id"]),
        mode=str(row["mode"]),
        head_ref=(None if row["head_ref"] is None else str(row["head_ref"])),
        head_repository=(
            None if row["head_repository"] is None else str(row["head_repository"])
        ),
        workspace=(None if row["workspace"] is None else str(row["workspace"])),
        status_message_id=(
            None
            if row["status_message_id"] is None
            else str(row["status_message_id"])
        ),
        terminal_receipt_message_id=(
            None
            if row["terminal_receipt_message_id"] is None
            else str(row["terminal_receipt_message_id"])
        ),
        status=str(row["status"]),
        dispatch_id=(None if row["dispatch_id"] is None else str(row["dispatch_id"])),
        recovery_token=(
            None if row["recovery_token"] is None else str(row["recovery_token"])
        ),
        owner_id=(None if row["owner_id"] is None else str(row["owner_id"])),
        evidence_json=(
            None if row["evidence_json"] is None else str(row["evidence_json"])
        ),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


_DERIVED_BRIEF_FIELDS = ("diff_hunk",)


def _strip_derived_brief_fields(payload: Any) -> None:
    """Drop brief fields that only re-render inputs the hash already covers.

    ``diff_hunk`` is GitHub's rendering of code that ``metadata.subject_head_sha``
    and each finding's ``commit_id`` already pin, and GitHub re-renders it on
    every force-push.  Hashing it would report a content change — and so post a
    new proposal revision into every open work thread — for a review request
    that did not actually change.

    Only these named fields are exempt.  The rest of the brief stays hashed: a
    sibling finding appearing or its body changing must still count as a change.
    """

    if not isinstance(payload, dict):
        return
    untrusted = payload.get("untrusted")
    metadata = untrusted.get("metadata") if isinstance(untrusted, dict) else None
    brief = metadata.get("preapproval_brief") if isinstance(metadata, dict) else None
    findings = brief.get("findings") if isinstance(brief, dict) else None
    if not isinstance(findings, list):
        return
    for finding in findings:
        if isinstance(finding, dict):
            for name in _DERIVED_BRIEF_FIELDS:
                finding.pop(name, None)


def _content_hash(event: MentionEvent) -> str:
    payload = copy.deepcopy(event_to_dict(event))
    payload["approval_state"] = "pending"
    _strip_derived_brief_fields(payload)
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
                    source_revision TEXT,
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
            if "source_revision" not in outbox_columns:
                connection.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN source_revision TEXT"
                )
            connection.execute("""
                UPDATE delivery_outbox
                SET source_revision = (
                    SELECT e.source_revision FROM mention_events e
                    WHERE e.dedupe_key = delivery_outbox.dedupe_key
                      AND e.revision_number = delivery_outbox.revision_number
                )
                WHERE source_revision IS NULL AND EXISTS (
                    SELECT 1 FROM mention_events e
                    WHERE e.dedupe_key = delivery_outbox.dedupe_key
                      AND e.revision_number = delivery_outbox.revision_number
                )
            """)
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
                SET status = 'superseded', lease_until = NULL,
                    error_category = CASE
                        WHEN source_revision IS NULL THEN 'missing_source_revision'
                        ELSE error_category
                    END
                WHERE status IN ('pending', 'sending')
                  AND (event_json IS NULL OR source_revision IS NULL)
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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS work_item_sessions (
                    subject_key TEXT PRIMARY KEY,
                    source_dedupe_key TEXT NOT NULL,
                    repository_node_id TEXT,
                    pr_node_id TEXT,
                    parent_message_id TEXT,
                    parent_channel_id TEXT,
                    discord_thread_id TEXT,
                    state TEXT NOT NULL,
                    last_event_revision TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(work_item_sessions)")
            }
            for column in ("repository_node_id", "pr_node_id", "parent_channel_id"):
                if column not in session_columns:
                    connection.execute(
                        f"ALTER TABLE work_item_sessions ADD COLUMN {column} TEXT"
                    )
            for row in connection.execute(
                """
                SELECT subject_key FROM work_item_sessions
                WHERE repository_node_id IS NULL OR pr_node_id IS NULL
                """
            ).fetchall():
                identity = _github_subject_identity(str(row["subject_key"]))
                if identity is None:
                    continue
                connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET repository_node_id = ?, pr_node_id = ?
                    WHERE subject_key = ?
                    """,
                    (*identity, str(row["subject_key"])),
                )
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_thread
                ON work_item_sessions(discord_thread_id)
                WHERE discord_thread_id IS NOT NULL
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_parent
                ON work_item_sessions(parent_message_id)
                WHERE parent_message_id IS NOT NULL
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_github_pr
                ON work_item_sessions(repository_node_id, pr_node_id)
                WHERE repository_node_id IS NOT NULL AND pr_node_id IS NOT NULL
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS work_proposals (
                    proposal_id TEXT NOT NULL,
                    proposal_revision INTEGER NOT NULL,
                    subject_key TEXT NOT NULL,
                    repository_node_id TEXT,
                    pr_node_id TEXT,
                    source_dedupe_key TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    head_sha TEXT,
                    proposal_json TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    discord_message_id TEXT,
                    approval_offered INTEGER NOT NULL DEFAULT 0
                        CHECK (approval_offered IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (proposal_id, proposal_revision)
                )
            """)
            proposal_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(work_proposals)")
            }
            if "approval_offered" not in proposal_columns:
                connection.execute(
                    """
                    ALTER TABLE work_proposals
                    ADD COLUMN approval_offered INTEGER NOT NULL DEFAULT 0
                        CHECK (approval_offered IN (0, 1))
                    """
                )
            for column in ("repository_node_id", "pr_node_id"):
                if column not in proposal_columns:
                    connection.execute(
                        f"ALTER TABLE work_proposals ADD COLUMN {column} TEXT"
                    )
            for row in connection.execute(
                """
                SELECT proposal_id, proposal_revision, subject_key
                FROM work_proposals
                WHERE repository_node_id IS NULL OR pr_node_id IS NULL
                """
            ).fetchall():
                identity = _github_subject_identity(str(row["subject_key"]))
                if identity is None:
                    continue
                connection.execute(
                    """
                    UPDATE work_proposals
                    SET repository_node_id = ?, pr_node_id = ?
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (
                        *identity,
                        str(row["proposal_id"]),
                        int(row["proposal_revision"]),
                    ),
                )
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_work_proposal_message
                ON work_proposals(discord_message_id)
                WHERE discord_message_id IS NOT NULL
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS ix_work_proposal_subject
                ON work_proposals(subject_key, proposal_revision DESC)
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS work_approvals (
                    proposal_id TEXT NOT NULL,
                    proposal_revision INTEGER NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    approver_platform TEXT NOT NULL,
                    approver_user_id TEXT NOT NULL,
                    approval_message_id TEXT NOT NULL UNIQUE,
                    approved_at TEXT NOT NULL,
                    PRIMARY KEY (proposal_id, proposal_revision)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS work_executions (
                    execution_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    proposal_revision INTEGER NOT NULL,
                    proposal_hash TEXT NOT NULL,
                    approval_message_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    head_ref TEXT,
                    head_repository TEXT,
                    workspace TEXT,
                    status_message_id TEXT,
                    terminal_receipt_message_id TEXT,
                    recovery_lease_until TEXT,
                    recovery_token TEXT,
                    owner_id TEXT,
                    terminal_receipt_marker TEXT,
                    terminal_receipt_content TEXT,
                    terminal_receipt_status TEXT,
                    terminal_receipt_lease_until TEXT,
                    terminal_receipt_claim_token TEXT,
                    status TEXT NOT NULL,
                    dispatch_id TEXT,
                    evidence_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (proposal_id, proposal_revision)
                )
            """)
            execution_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(work_executions)")
            }
            for column in (
                "head_ref",
                "head_repository",
                "workspace",
                "status_message_id",
                "terminal_receipt_message_id",
                "recovery_lease_until",
                "recovery_token",
                "owner_id",
                "terminal_receipt_marker",
                "terminal_receipt_content",
                "terminal_receipt_status",
                "terminal_receipt_lease_until",
                "terminal_receipt_claim_token",
            ):
                if column not in execution_columns:
                    connection.execute(
                        f"ALTER TABLE work_executions ADD COLUMN {column} TEXT"
                    )
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
        source_revision: str,
        now: str,
    ) -> None:
        event_json = event_to_json(event)
        for destination in self._delivery_destinations:
            identity = f"{event.dedupe_key}\0{revision_number}\0{destination}"
            marker = "[hermes-inbox:" + hashlib.sha256(identity.encode()).hexdigest()[:24] + "]"
            connection.execute(
                """
                INSERT OR IGNORE INTO delivery_outbox (
                    dedupe_key, revision_number, source_revision, destination,
                    marker, event_json, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    event.dedupe_key,
                    revision_number,
                    source_revision,
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
                    self._enqueue_deliveries(
                        connection, event, revision_number, source_revision, now
                    )
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
                self._enqueue_deliveries(
                    connection, event, revision_number, source_revision, now
                )
                return UpsertResult(
                    created=True,
                    content_changed=True,
                    stale=False,
                    revision_number=revision_number,
                )
        finally:
            connection.close()

    def reserve_work_item_session(
        self, subject_key: str, source_dedupe_key: str, source_revision: str
    ) -> WorkItemSession:
        subject = _require_stable_text(subject_key, "subject_key")
        dedupe = _require_stable_text(source_dedupe_key, "source_dedupe_key")
        _parse_datetime(source_revision)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                if row is None:
                    identity = _github_subject_identity(subject)
                    connection.execute(
                        """
                        INSERT INTO work_item_sessions (
                            subject_key, source_dedupe_key,
                            repository_node_id, pr_node_id, state,
                            last_event_revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                        """,
                        (
                            subject,
                            dedupe,
                            None if identity is None else identity[0],
                            None if identity is None else identity[1],
                            source_revision,
                            now,
                            now,
                        ),
                    )
                else:
                    state = str(row["state"])
                    if state in _CLOSED_SESSION_STATES:
                        state = "reserved"
                    connection.execute(
                        """
                        UPDATE work_item_sessions
                        SET source_dedupe_key = ?, last_event_revision = ?,
                            state = ?, updated_at = ?
                        WHERE subject_key = ?
                        """,
                        (dedupe, source_revision, state, now, subject),
                    )
                stored = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                return _work_item_session(stored)
        finally:
            connection.close()

    def get_active_work_item_session(self, subject_key: str) -> WorkItemSession | None:
        subject = _require_stable_text(subject_key, "subject_key")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                (subject,),
            ).fetchone()
            if row is None or str(row["state"]) in _CLOSED_SESSION_STATES:
                return None
            return _work_item_session(row)
        finally:
            connection.close()

    def get_work_item_session(self, subject_key: str) -> WorkItemSession | None:
        """Return a subject card even when its latest execution is terminal."""

        subject = _require_stable_text(subject_key, "subject_key")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                (subject,),
            ).fetchone()
            return None if row is None else _work_item_session(row)
        finally:
            connection.close()

    def list_active_work_item_sessions(
        self,
        *,
        limit: int = 100,
        include_overflow: bool = False,
    ) -> tuple[WorkItemSession, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(include_overflow, bool):
            raise ValueError("include_overflow must be a bool")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM work_item_sessions
                WHERE state NOT IN ('completed', 'rejected', 'expired')
                ORDER BY updated_at, subject_key
                LIMIT ?
                """,
                (limit + int(include_overflow),),
            ).fetchall()
            return tuple(_work_item_session(row) for row in rows)
        finally:
            connection.close()

    def active_work_item_session_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM work_item_sessions
                WHERE state NOT IN ('completed', 'rejected', 'expired')
                """
            ).fetchone()
            return 0 if row is None else int(row["count"])
        finally:
            connection.close()

    def prepare_work_item_parent(
        self,
        subject_key: str,
        parent_message_id: str,
        parent_channel_id: str,
    ) -> WorkItemSession:
        subject = _require_stable_text(subject_key, "subject_key")
        parent = _require_stable_text(parent_message_id, "parent_message_id", limit=80)
        parent_channel = _require_discord_snowflake(
            parent_channel_id, "parent_channel_id"
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                if row is None:
                    raise KeyError("work item session not found")
                existing = row["parent_message_id"]
                existing_channel = row["parent_channel_id"]
                if existing is not None and str(existing) != parent:
                    raise ValueError("work item session already maps to a different parent")
                if (
                    existing_channel is not None
                    and str(existing_channel) != parent_channel
                ):
                    raise ValueError(
                        "work item session belongs to a different Discord destination"
                    )
                connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET parent_message_id = ?, parent_channel_id = ?, updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (parent, parent_channel, now, subject),
                )
                stored = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                return _work_item_session(stored)
        finally:
            connection.close()

    def replace_unthreaded_work_item_parent(
        self,
        subject_key: str,
        *,
        expected_parent_message_id: str,
        parent_message_id: str,
        parent_channel_id: str,
    ) -> WorkItemSession:
        subject = _require_stable_text(subject_key, "subject_key")
        expected_parent = _require_stable_text(
            expected_parent_message_id,
            "expected_parent_message_id",
            limit=80,
        )
        parent = _require_stable_text(
            parent_message_id,
            "parent_message_id",
            limit=80,
        )
        parent_channel = _require_discord_snowflake(
            parent_channel_id, "parent_channel_id"
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                updated = connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET parent_message_id = ?, parent_channel_id = ?, updated_at = ?
                    WHERE subject_key = ?
                      AND parent_message_id = ?
                      AND (parent_channel_id IS NULL OR parent_channel_id = ?)
                      AND discord_thread_id IS NULL
                    """,
                    (
                        parent,
                        parent_channel,
                        now,
                        subject,
                        expected_parent,
                        parent_channel,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError(
                        "unthreaded work item parent changed concurrently"
                    )
                stored = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                return _work_item_session(stored)
        finally:
            connection.close()

    def get_work_item_session_by_thread(self, thread_id: str) -> WorkItemSession | None:
        value = _require_stable_text(thread_id, "thread_id", limit=80)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_item_sessions WHERE discord_thread_id = ?",
                (value,),
            ).fetchone()
            return None if row is None else _work_item_session(row)
        finally:
            connection.close()

    def record_work_item_thread(
        self,
        subject_key: str,
        parent_message_id: str,
        parent_channel_id: str,
        thread_id: str,
    ) -> WorkItemSession:
        subject = _require_stable_text(subject_key, "subject_key")
        parent = _require_stable_text(parent_message_id, "parent_message_id", limit=80)
        parent_channel = _require_discord_snowflake(
            parent_channel_id, "parent_channel_id"
        )
        thread = _require_stable_text(thread_id, "thread_id", limit=80)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                if row is None:
                    raise KeyError("work item session not found")
                existing_parent = row["parent_message_id"]
                existing_parent_channel = row["parent_channel_id"]
                existing_thread = row["discord_thread_id"]
                if (
                    (existing_parent is not None and str(existing_parent) != parent)
                    or (
                        existing_parent_channel is not None
                        and str(existing_parent_channel) != parent_channel
                    )
                    or (existing_thread is not None and str(existing_thread) != thread)
                ):
                    raise ValueError("work item session already maps to a different thread")
                state = "thread_open" if str(row["state"]) == "reserved" else str(row["state"])
                connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET parent_message_id = ?, parent_channel_id = ?,
                        discord_thread_id = ?, state = ?, updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (parent, parent_channel, thread, state, now, subject),
                )
                stored = connection.execute(
                    "SELECT * FROM work_item_sessions WHERE subject_key = ?",
                    (subject,),
                ).fetchone()
                return _work_item_session(stored)
        finally:
            connection.close()

    @staticmethod
    def _write_proposal_status(
        connection: sqlite3.Connection,
        proposal: WorkProposal,
        status: ProposalStatus,
        now: str,
    ) -> WorkProposal:
        updated = replace(proposal, status=status)
        cursor = connection.execute(
            """
            UPDATE work_proposals
            SET status = ?, proposal_json = ?, updated_at = ?
            WHERE proposal_id = ? AND proposal_revision = ?
            """,
            (
                status.value,
                proposal_to_json(updated),
                now,
                proposal.proposal_id,
                proposal.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("proposal status CAS target disappeared")
        return updated

    def create_proposal(self, proposal: WorkProposal) -> WorkProposal:
        if not isinstance(proposal, WorkProposal) or not verify_proposal_hash(proposal):
            raise ValueError("proposal must have a valid canonical hash")
        if proposal.status is not ProposalStatus.PENDING:
            raise ValueError("new proposal status must be pending")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                session = connection.execute(
                    "SELECT subject_key FROM work_item_sessions WHERE subject_key = ?",
                    (proposal.subject_key,),
                ).fetchone()
                if session is None:
                    raise KeyError("work item session not found")
                existing = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal.proposal_id, proposal.revision),
                ).fetchone()
                if existing is not None:
                    restored = _stored_proposal(existing)
                    if restored == proposal:
                        return restored
                    raise ValueError("proposal revision already exists with different content")
                latest = connection.execute(
                    """
                    SELECT MAX(proposal_revision) FROM work_proposals
                    WHERE proposal_id = ?
                    """,
                    (proposal.proposal_id,),
                ).fetchone()[0]
                if latest is not None and proposal.revision <= int(latest):
                    raise ValueError("proposal revision must advance monotonically")
                older = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND status IN ('pending', 'approved')
                    """,
                    (proposal.proposal_id,),
                ).fetchall()
                for row in older:
                    self._write_proposal_status(
                        connection,
                        _stored_proposal(row),
                        ProposalStatus.NEEDS_REAPPROVAL,
                        now,
                    )
                connection.execute(
                    """
                    INSERT INTO work_proposals (
                        proposal_id, proposal_revision, subject_key,
                        repository_node_id, pr_node_id,
                        source_dedupe_key, source_revision, head_sha,
                        proposal_json, proposal_hash, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.revision,
                        proposal.subject_key,
                        *(_github_subject_identity(proposal.subject_key) or (None, None)),
                        proposal.source_dedupe_key,
                        proposal.source_revision,
                        proposal.head_sha,
                        proposal_to_json(proposal),
                        proposal.content_hash,
                        proposal.status.value,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET state = 'pending_proposal',
                        source_dedupe_key = ?, last_event_revision = ?, updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (
                        proposal.source_dedupe_key,
                        proposal.source_revision,
                        now,
                        proposal.subject_key,
                    ),
                )
                return proposal
        finally:
            connection.close()

    def record_proposal_message(
        self,
        proposal_id: str,
        revision: int,
        message_id: str,
        *,
        approval_offered: bool,
    ) -> ProposalMessageBinding:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        message = _require_stable_text(message_id, "message_id", limit=80)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("revision must be a positive integer")
        if not isinstance(approval_offered, bool):
            raise ValueError("approval_offered must be a boolean")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal_key, revision),
                ).fetchone()
                if row is None:
                    raise KeyError("proposal not found")
                existing = row["discord_message_id"]
                if existing is not None and str(existing) != message:
                    raise ValueError("proposal already maps to a different message")
                stored_capability = bool(int(row["approval_offered"]))
                if existing is not None and stored_capability is not approval_offered:
                    raise ValueError("proposal message capability cannot be changed")
                connection.execute(
                    """
                    UPDATE work_proposals
                    SET discord_message_id = ?, approval_offered = ?, updated_at = ?
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (
                        message,
                        int(approval_offered),
                        now,
                        proposal_key,
                        revision,
                    ),
                )
                stored = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal_key, revision),
                ).fetchone()
                if stored is None:
                    raise RuntimeError("proposal message binding disappeared")
                return _proposal_message_binding(stored)
        finally:
            connection.close()

    def get_proposal(
        self, proposal_id: str, revision: int
    ) -> WorkProposal | None:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("revision must be a positive integer")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                (proposal_key, revision),
            ).fetchone()
            return None if row is None else _stored_proposal(row)
        finally:
            connection.close()

    def get_proposal_message_binding(
        self, proposal_id: str, revision: int
    ) -> ProposalMessageBinding | None:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("revision must be a positive integer")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM work_proposals
                WHERE proposal_id = ? AND proposal_revision = ?
                """,
                (proposal_key, revision),
            ).fetchone()
            if row is None:
                raise KeyError("proposal not found")
            if row["discord_message_id"] is None:
                return None
            return _proposal_message_binding(row)
        finally:
            connection.close()

    def get_proposal_message_id(self, proposal_id: str, revision: int) -> str | None:
        binding = self.get_proposal_message_binding(proposal_id, revision)
        return None if binding is None else binding.message_id

    def get_proposal_message_binding_by_message_id(
        self, message_id: str
    ) -> ProposalMessageBinding | None:
        message = _require_stable_text(message_id, "message_id", limit=80)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_proposals WHERE discord_message_id = ?",
                (message,),
            ).fetchone()
            return None if row is None else _proposal_message_binding(row)
        finally:
            connection.close()

    def get_proposal_by_message_id(self, message_id: str) -> WorkProposal | None:
        message = _require_stable_text(message_id, "message_id", limit=80)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_proposals WHERE discord_message_id = ?",
                (message,),
            ).fetchone()
            return None if row is None else _stored_proposal(row)
        finally:
            connection.close()

    def get_latest_proposal(self, subject_key: str) -> WorkProposal | None:
        subject = _require_stable_text(subject_key, "subject_key")
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM work_proposals WHERE subject_key = ?
                ORDER BY proposal_revision DESC LIMIT 1
                """,
                (subject,),
            ).fetchone()
            return None if row is None else _stored_proposal(row)
        finally:
            connection.close()

    def mark_proposal_needs_reapproval(
        self, proposal_id: str, revision: int
    ) -> WorkProposal:
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal_id, revision),
                ).fetchone()
                if row is None:
                    raise KeyError("proposal not found")
                proposal = _stored_proposal(row)
                if proposal.status is ProposalStatus.NEEDS_REAPPROVAL:
                    return proposal
                updated = self._write_proposal_status(
                    connection, proposal, ProposalStatus.NEEDS_REAPPROVAL, now
                )
                connection.execute(
                    "UPDATE work_item_sessions SET state = 'needs_reapproval', updated_at = ? WHERE subject_key = ?",
                    (now, proposal.subject_key),
                )
                return updated
        finally:
            connection.close()

    def transition_proposal_status(
        self,
        proposal_id: str,
        revision: int,
        requested_status: ProposalStatus,
        *,
        expected_statuses: Iterable[ProposalStatus] | None = None,
    ) -> WorkProposal:
        if not isinstance(requested_status, ProposalStatus):
            raise ValueError("requested_status must be a ProposalStatus")
        expected = None if expected_statuses is None else frozenset(expected_statuses)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal_id, revision),
                ).fetchone()
                if row is None:
                    raise KeyError("proposal not found")
                proposal = _stored_proposal(row)
                if expected is not None and proposal.status not in expected:
                    raise ValueError("proposal status did not match expected state")
                if requested_status not in _PROPOSAL_TRANSITIONS[proposal.status]:
                    raise ValueError("illegal proposal status transition")
                updated = self._write_proposal_status(
                    connection, proposal, requested_status, now
                )
                connection.execute(
                    "UPDATE work_item_sessions SET state = ?, updated_at = ? WHERE subject_key = ?",
                    (requested_status.value, now, proposal.subject_key),
                )
                return updated
        finally:
            connection.close()

    def approve_proposal_cas(
        self,
        *,
        proposal_id: str,
        revision: int,
        proposal_hash: str,
        source_revision: str,
        current_head_sha: str | None,
        approver_platform: str,
        approver_user_id: str,
        authorized_approver_ids: frozenset[str],
        approval_message_id: str,
    ) -> ApprovalCASResult:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        expected_hash = _require_stable_text(proposal_hash, "proposal_hash", limit=64)
        _parse_datetime(source_revision)
        platform = _require_stable_text(approver_platform, "approver_platform", limit=32)
        approver = _require_stable_text(approver_user_id, "approver_user_id", limit=80)
        approval_message = _require_stable_text(
            approval_message_id, "approval_message_id", limit=80
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (proposal_key, revision),
                ).fetchone()
                if row is None:
                    raise KeyError("proposal not found")
                proposal = _stored_proposal(row)
                if approver not in authorized_approver_ids:
                    return ApprovalCASResult(False, "unauthorized_approver", proposal)
                reused = connection.execute(
                    "SELECT 1 FROM work_approvals WHERE approval_message_id = ?",
                    (approval_message,),
                ).fetchone()
                if reused is not None:
                    return ApprovalCASResult(False, "approval_message_reused", proposal)
                latest = connection.execute(
                    "SELECT MAX(proposal_revision) FROM work_proposals WHERE proposal_id = ?",
                    (proposal_key,),
                ).fetchone()[0]
                if int(latest) != revision:
                    return ApprovalCASResult(False, "not_latest_revision", proposal)
                if proposal.status is ProposalStatus.APPROVED:
                    return ApprovalCASResult(False, "already_approved", proposal)
                if proposal.status is not ProposalStatus.PENDING:
                    return ApprovalCASResult(False, proposal.status.value, proposal)
                if not verify_proposal_hash(proposal) or proposal.content_hash != expected_hash:
                    return ApprovalCASResult(False, "proposal_hash_mismatch", proposal)
                if (
                    row["discord_message_id"] is None
                    or int(row["approval_offered"]) != 1
                ):
                    return ApprovalCASResult(False, "approval_not_offered", proposal)
                if proposal.source_revision != source_revision:
                    updated = self._write_proposal_status(
                        connection, proposal, ProposalStatus.NEEDS_REAPPROVAL, now
                    )
                    connection.execute(
                        "UPDATE work_item_sessions SET state = 'needs_reapproval', updated_at = ? WHERE subject_key = ?",
                        (now, proposal.subject_key),
                    )
                    return ApprovalCASResult(False, "source_changed", updated)
                if proposal.head_sha != current_head_sha:
                    updated = self._write_proposal_status(
                        connection, proposal, ProposalStatus.NEEDS_REAPPROVAL, now
                    )
                    connection.execute(
                        "UPDATE work_item_sessions SET state = 'needs_reapproval', updated_at = ? WHERE subject_key = ?",
                        (now, proposal.subject_key),
                    )
                    return ApprovalCASResult(False, "head_changed", updated)
                connection.execute(
                    """
                    INSERT INTO work_approvals (
                        proposal_id, proposal_revision, proposal_hash,
                        approver_platform, approver_user_id,
                        approval_message_id, approved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.revision,
                        proposal.content_hash,
                        platform,
                        approver,
                        approval_message,
                        now,
                    ),
                )
                updated = self._write_proposal_status(
                    connection, proposal, ProposalStatus.APPROVED, now
                )
                connection.execute(
                    "UPDATE work_item_sessions SET state = 'approved', updated_at = ? WHERE subject_key = ?",
                    (now, proposal.subject_key),
                )
                return ApprovalCASResult(True, "approved", updated)
        finally:
            connection.close()

    def reserve_work_execution(
        self,
        *,
        proposal_id: str,
        revision: int,
        proposal_hash: str,
        approval_message_id: str,
        thread_id: str,
        mode: str,
        head_ref: str | None,
        head_repository: str | None,
        workspace: str,
    ) -> WorkExecution:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        expected_hash = _require_stable_text(proposal_hash, "proposal_hash", limit=64)
        approval_message = _require_stable_text(
            approval_message_id, "approval_message_id", limit=80
        )
        thread = _require_stable_text(thread_id, "thread_id", limit=80)
        route = _require_stable_text(mode, "mode", limit=32)
        branch = _require_git_ref(head_ref)
        repository = _require_repository(head_repository)
        workspace_root = _require_workspace(workspace)
        if route != route.casefold() or not route.replace("_", "").replace("-", "").isalnum():
            raise ValueError("mode must be a lowercase identifier")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
            raise ValueError("revision must be a positive integer")
        execution_id = work_execution_id(proposal_key, revision)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                existing = connection.execute(
                    "SELECT * FROM work_executions WHERE proposal_id = ? AND proposal_revision = ?",
                    (proposal_key, revision),
                ).fetchone()
                if existing is not None:
                    execution = _work_execution(existing)
                    if (
                        execution.proposal_hash != expected_hash
                        or execution.approval_message_id != approval_message
                        or execution.thread_id != thread
                        or execution.mode != route
                        or execution.head_ref != branch
                        or execution.head_repository != repository
                        or execution.workspace != workspace_root
                    ):
                        raise ValueError("execution receipt conflicts with existing scope")
                    return execution
                proposal_row = connection.execute(
                    "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                    (proposal_key, revision),
                ).fetchone()
                if proposal_row is None:
                    raise KeyError("proposal not found")
                proposal = _stored_proposal(proposal_row)
                if proposal.status is not ProposalStatus.APPROVED:
                    raise ValueError("proposal must be approved before execution is reserved")
                if proposal.executor_hint != route:
                    raise ValueError("execution mode must match the approved proposal")
                if proposal.content_hash != expected_hash:
                    raise ValueError("proposal hash changed before execution reservation")
                if proposal.head_sha is None:
                    if branch is not None or repository is not None:
                        raise ValueError("non-PR execution cannot carry a head branch")
                elif branch is None or repository is None:
                    raise ValueError("PR execution requires a verified head branch")
                approval = connection.execute(
                    """
                    SELECT proposal_hash FROM work_approvals
                    WHERE proposal_id = ? AND proposal_revision = ?
                      AND approval_message_id = ?
                    """,
                    (proposal_key, revision, approval_message),
                ).fetchone()
                if approval is None or str(approval["proposal_hash"]) != expected_hash:
                    raise ValueError("matching committed approval receipt is required")
                session = connection.execute(
                    "SELECT discord_thread_id FROM work_item_sessions WHERE subject_key = ?",
                    (proposal.subject_key,),
                ).fetchone()
                if session is None or str(session["discord_thread_id"] or "") != thread:
                    raise ValueError("execution thread does not match work-item session")
                connection.execute(
                    """
                    INSERT INTO work_executions (
                        execution_id, proposal_id, proposal_revision, proposal_hash,
                        approval_message_id, thread_id, mode, head_ref,
                        head_repository, workspace, status, dispatch_id,
                        evidence_json, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, NULL, ?, ?
                    )
                    """,
                    (
                        execution_id,
                        proposal_key,
                        revision,
                        expected_hash,
                        approval_message,
                        thread,
                        route,
                        branch,
                        repository,
                        workspace_root,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                return _work_execution(row)
        finally:
            connection.close()

    def mark_execution_dispatched(
        self,
        execution_id: str,
        dispatch_id: str,
        *,
        lease_seconds: int = 60,
        recovery_token: str | None = None,
    ) -> WorkExecution:
        """Promote a reservation and establish its durable dispatch generation."""
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        dispatch = _require_stable_text(dispatch_id, "dispatch_id", limit=200)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        token = (
            secrets.token_hex(16)
            if recovery_token is None
            else _require_stable_text(recovery_token, "recovery_token", limit=80)
        )
        now_dt = self._clock()
        now = _iso_datetime(now_dt)
        lease_until = _iso_datetime(now_dt + timedelta(seconds=lease_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_executions WHERE execution_id = ?",
                (execution_key,),
            ).fetchone()
            if row is None:
                raise KeyError("execution receipt not found")
            current = _work_execution(row)
            if current.status == "queued":
                if current.dispatch_id != dispatch:
                    raise ValueError("execution already maps to a different dispatch")
                if current.recovery_token is not None:
                    connection.commit()
                    return current
            elif current.status != "reserved":
                raise ValueError("only a reserved execution may be dispatched")
            updated = connection.execute(
                """
                UPDATE work_executions
                SET status = 'queued', dispatch_id = ?, recovery_token = ?,
                    owner_id = NULL, recovery_lease_until = ?, updated_at = ?
                WHERE execution_id = ? AND status IN ('reserved', 'queued')
                  AND (recovery_token IS NULL OR recovery_token = ?)
                """,
                (dispatch, token, lease_until, now, execution_key, token),
            )
            if updated.rowcount != 1:
                raise RuntimeError("execution dispatch ownership changed concurrently")
            stored = connection.execute(
                "SELECT * FROM work_executions WHERE execution_id = ?",
                (execution_key,),
            ).fetchone()
            connection.commit()
            return _work_execution(stored)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_recoverable_executions(
        self, *, limit: int = 100
    ) -> tuple[RecoverableExecution, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT e.*, a.approver_user_id AS recovery_approver_user_id
                FROM work_executions AS e
                JOIN work_approvals AS a
                  ON a.proposal_id = e.proposal_id
                 AND a.proposal_revision = e.proposal_revision
                 AND a.approval_message_id = e.approval_message_id
                WHERE e.status IN ('reserved', 'queued')
                ORDER BY e.created_at, e.execution_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(
                RecoverableExecution(
                    execution=_work_execution(row),
                    approver_user_id=_require_stable_text(
                        str(row["recovery_approver_user_id"]),
                        "approver_user_id",
                        limit=80,
                    ),
                )
                for row in rows
            )
        finally:
            connection.close()

    def claim_recoverable_executions(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> tuple[RecoverableExecution, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        now_text = _iso_datetime(now)
        lease_until = _iso_datetime(now + timedelta(seconds=lease_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT e.*, a.approver_user_id AS recovery_approver_user_id
                FROM work_executions AS e
                JOIN work_approvals AS a
                  ON a.proposal_id = e.proposal_id
                 AND a.proposal_revision = e.proposal_revision
                 AND a.approval_message_id = e.approval_message_id
                WHERE e.status IN ('reserved', 'queued')
                  AND (
                    e.recovery_lease_until IS NULL
                    OR julianday(e.recovery_lease_until) <= julianday(?)
                  )
                ORDER BY e.created_at, e.execution_id
                LIMIT ?
                """,
                (now_text, limit),
            ).fetchall()
            claimed: list[RecoverableExecution] = []
            for row in rows:
                token = secrets.token_hex(16)
                updated = connection.execute(
                    """
                    UPDATE work_executions
                    SET recovery_token = ?, owner_id = NULL,
                        recovery_lease_until = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND status IN ('reserved', 'queued')
                      AND (
                        recovery_lease_until IS NULL
                        OR julianday(recovery_lease_until) <= julianday(?)
                      )
                    """,
                    (
                        token,
                        lease_until,
                        now_text,
                        str(row["execution_id"]),
                        now_text,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                claimed.append(
                    RecoverableExecution(
                        execution=_work_execution(row),
                        approver_user_id=_require_stable_text(
                            str(row["recovery_approver_user_id"]),
                            "approver_user_id",
                            limit=80,
                        ),
                        recovery_token=token,
                    )
                )
            connection.commit()
            return tuple(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_execution_recovery_lease(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        lease_seconds: int = 60,
    ) -> str | None:
        execution_key = _require_stable_text(
            execution_id,
            "execution_id",
            limit=80,
        )
        token = _require_stable_text(
            recovery_token,
            "recovery_token",
            limit=80,
        )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        now_dt = self._clock()
        now = _iso_datetime(now_dt)
        lease_until = _iso_datetime(
            now_dt + timedelta(seconds=lease_seconds)
        )
        connection = self._connect()
        try:
            with connection:
                updated = connection.execute(
                    """
                    UPDATE work_executions
                    SET recovery_lease_until = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND status IN ('reserved', 'queued')
                      AND recovery_token = ?
                      AND julianday(recovery_lease_until) > julianday(?)
                    """,
                    (lease_until, now, execution_key, token, now),
                )
                return token if updated.rowcount == 1 else None
        finally:
            connection.close()

    def release_execution_recovery(
        self,
        execution_id: str,
        *,
        recovery_token: str,
    ) -> bool:
        execution_key = _require_stable_text(
            execution_id,
            "execution_id",
            limit=80,
        )
        token = _require_stable_text(
            recovery_token,
            "recovery_token",
            limit=80,
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                updated = connection.execute(
                    """
                    UPDATE work_executions
                    SET recovery_token = NULL, owner_id = NULL,
                        recovery_lease_until = NULL, updated_at = ?
                    WHERE execution_id = ?
                      AND status IN ('reserved', 'queued')
                      AND recovery_token = ?
                    """,
                    (now, execution_key, token),
                )
                return updated.rowcount == 1
        finally:
            connection.close()

    def admit_execution_owner(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        owner_id: str,
    ) -> bool:
        """Bind one gateway run instance to the current queued generation."""
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                updated = connection.execute(
                    """
                    UPDATE work_executions
                    SET owner_id = ?, updated_at = ?
                    WHERE execution_id = ? AND status = 'queued'
                      AND recovery_token = ?
                      AND julianday(recovery_lease_until) > julianday(?)
                      AND (owner_id IS NULL OR owner_id = ?)
                    """,
                    (owner, now, execution_key, token, now, owner),
                )
                return updated.rowcount == 1
        finally:
            connection.close()

    def invalidate_execution_for_reapproval(
        self,
        execution_id: str,
        *,
        evidence_category: str,
        recovery_token: str,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        category = _require_error_category(evidence_category)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        evidence_json = json.dumps(
            {"category": category}, separators=(",", ":"), sort_keys=True
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id is not None:
                    raise ValueError("execution recovery lease is stale")
                if current.status == "blocked":
                    return current
                if current.status not in {"reserved", "queued"}:
                    raise ValueError("only unstarted execution may require reapproval")
                proposal_row = connection.execute(
                    "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                    (current.proposal_id, current.proposal_revision),
                ).fetchone()
                if proposal_row is None:
                    raise KeyError("execution proposal not found")
                proposal = _stored_proposal(proposal_row)
                if ProposalStatus.NEEDS_REAPPROVAL not in _PROPOSAL_TRANSITIONS.get(
                    proposal.status, frozenset()
                ):
                    raise ValueError("proposal cannot require reapproval")
                self._write_proposal_status(
                    connection, proposal, ProposalStatus.NEEDS_REAPPROVAL, now
                )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions
                    SET status = 'blocked', evidence_json = ?, updated_at = ?
                    WHERE execution_id = ? AND recovery_token = ?
                      AND owner_id IS NULL
                    """,
                    (evidence_json, now, execution_key, token),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution recovery lease is stale")
                connection.execute(
                    "UPDATE work_item_sessions SET state = 'needs_reapproval', updated_at = ? WHERE subject_key = ?",
                    (now, proposal.subject_key),
                )
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    def invalidate_queued_external_executions(
        self,
        *,
        trusted_repositories: frozenset[str],
        evidence_category: str,
    ) -> tuple[WorkExecution, ...]:
        trusted = frozenset(
            _require_stable_text(repository, "repository", limit=200)
            for repository in trusted_repositories
        )
        category = _require_error_category(evidence_category)
        evidence_json = json.dumps(
            {"category": category},
            separators=(",", ":"),
            sort_keys=True,
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                rows = connection.execute(
                    """
                    SELECT e.execution_id, m.event_json
                    FROM work_executions AS e
                    JOIN work_proposals AS p
                      ON p.proposal_id = e.proposal_id
                     AND p.proposal_revision = e.proposal_revision
                    JOIN work_item_sessions AS s
                      ON s.subject_key = p.subject_key
                    LEFT JOIN mention_events AS m
                      ON m.dedupe_key = s.source_dedupe_key
                    WHERE e.status IN ('reserved', 'queued')
                    ORDER BY e.created_at, e.execution_id
                    """
                ).fetchall()
                changed: list[WorkExecution] = []
                for candidate in rows:
                    repository: str | None = None
                    raw_event = candidate["event_json"]
                    if raw_event is not None:
                        try:
                            event = restore_event(json.loads(str(raw_event)))
                            raw_repository = event.untrusted.metadata.get("repository")
                            if isinstance(raw_repository, str):
                                repository = raw_repository
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            repository = None
                    if repository in trusted:
                        continue
                    execution_id = str(candidate["execution_id"])
                    execution_row = connection.execute(
                        "SELECT * FROM work_executions WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()
                    if execution_row is None:
                        continue
                    current = _work_execution(execution_row)
                    proposal_row = connection.execute(
                        """
                        SELECT * FROM work_proposals
                        WHERE proposal_id = ? AND proposal_revision = ?
                        """,
                        (current.proposal_id, current.proposal_revision),
                    ).fetchone()
                    if proposal_row is None:
                        continue
                    proposal = _stored_proposal(proposal_row)
                    if ProposalStatus.NEEDS_REAPPROVAL not in _PROPOSAL_TRANSITIONS.get(
                        proposal.status,
                        frozenset(),
                    ):
                        continue
                    self._write_proposal_status(
                        connection,
                        proposal,
                        ProposalStatus.NEEDS_REAPPROVAL,
                        now,
                    )
                    connection.execute(
                        """
                        UPDATE work_executions
                        SET status = 'blocked', evidence_json = ?,
                            recovery_lease_until = NULL, updated_at = ?
                        WHERE execution_id = ?
                          AND status IN ('reserved', 'queued')
                        """,
                        (evidence_json, now, execution_id),
                    )
                    connection.execute(
                        """
                        UPDATE work_item_sessions
                        SET state = 'needs_reapproval', updated_at = ?
                        WHERE subject_key = ?
                        """,
                        (now, proposal.subject_key),
                    )
                    updated = connection.execute(
                        "SELECT * FROM work_executions WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()
                    changed.append(_work_execution(updated))
                return tuple(changed)
        finally:
            connection.close()

    def mark_execution_blocked(
        self,
        execution_id: str,
        *,
        evidence_category: str,
        recovery_token: str | None,
        owner_id: str | None,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        category = _require_error_category(evidence_category)
        if recovery_token is None and owner_id is not None:
            raise ValueError("execution owner identity is invalid")
        token = (
            None
            if recovery_token is None
            else _require_stable_text(recovery_token, "recovery_token", limit=80)
        )
        owner = (
            None
            if owner_id is None
            else _require_stable_text(owner_id, "owner_id", limit=80)
        )
        evidence_json = json.dumps(
            {"category": category}, separators=(",", ":"), sort_keys=True
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token is None:
                    authorized = token is None and owner is None
                elif current.owner_id is None:
                    authorized = current.recovery_token == token and owner is None
                else:
                    authorized = (
                        current.recovery_token == token
                        and current.owner_id == owner
                    )
                if not authorized:
                    raise ValueError("execution owner is stale or foreign")
                if current.status == "blocked":
                    return current
                if current.status not in {"reserved", "queued", "running", "verifying"}:
                    raise ValueError("execution cannot transition to blocked")
                proposal_row = connection.execute(
                    "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                    (current.proposal_id, current.proposal_revision),
                ).fetchone()
                if proposal_row is None:
                    raise KeyError("execution proposal not found")
                proposal = _stored_proposal(proposal_row)
                if ProposalStatus.BLOCKED not in _PROPOSAL_TRANSITIONS.get(
                    proposal.status, frozenset()
                ):
                    raise ValueError("proposal cannot transition to blocked")
                self._write_proposal_status(
                    connection, proposal, ProposalStatus.BLOCKED, now
                )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions
                    SET status = 'blocked', evidence_json = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token IS ? AND owner_id IS ?
                    """,
                    (evidence_json, now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                connection.execute(
                    "UPDATE work_item_sessions SET state = 'blocked', updated_at = ? WHERE subject_key = ?",
                    (now, proposal.subject_key),
                )
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    @staticmethod
    def _decode_execution_evidence(raw: str | None) -> dict[str, object]:
        if raw is None:
            return {"tool_starts": {}, "tool_completions": []}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeError("stored execution evidence is invalid") from None
        if not isinstance(value, dict):
            raise RuntimeError("stored execution evidence is invalid")
        starts = value.get("tool_starts", {})
        completions = value.get("tool_completions", [])
        if not isinstance(starts, dict) or not isinstance(completions, list):
            raise RuntimeError("stored execution evidence is invalid")
        return {"tool_starts": dict(starts), "tool_completions": list(completions)}

    def mark_execution_running(
        self,
        execution_id: str,
        *,
        tool_name: str,
        recovery_token: str,
        owner_id: str,
        transition_proposal: bool = True,
    ) -> tuple[WorkExecution, bool]:
        if not isinstance(transition_proposal, bool):
            raise ValueError("transition_proposal must be boolean")
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        tool = _require_stable_text(tool_name, "tool_name", limit=100)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        if not all(char.isalnum() or char in "_.:-" for char in tool):
            raise ValueError("tool_name contains unsupported characters")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id != owner:
                    raise ValueError("execution owner is stale or foreign")
                if current.status not in {"queued", "running"}:
                    raise ValueError("execution cannot record tool start")
                if current.status == "queued" and (
                    row["recovery_lease_until"] is None
                    or _parse_datetime(str(row["recovery_lease_until"])) <= self._clock()
                ):
                    raise ValueError("execution owner lease expired")
                evidence = self._decode_execution_evidence(current.evidence_json)
                starts = evidence["tool_starts"]
                assert isinstance(starts, dict)
                count = starts.get(tool, 0)
                starts[tool] = (count if isinstance(count, int) else 0) + 1
                evidence_json = json.dumps(
                    evidence, separators=(",", ":"), sort_keys=True
                )
                changed = current.status == "queued"
                if changed and transition_proposal:
                    proposal_row = connection.execute(
                        "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                        (current.proposal_id, current.proposal_revision),
                    ).fetchone()
                    proposal = _stored_proposal(proposal_row)
                    if proposal.status is not ProposalStatus.QUEUED:
                        raise ValueError("queued execution proposal state mismatch")
                    self._write_proposal_status(
                        connection, proposal, ProposalStatus.RUNNING, now
                    )
                    connection.execute(
                        "UPDATE work_item_sessions SET state = 'running', updated_at = ? WHERE subject_key = ?",
                        (now, proposal.subject_key),
                    )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions
                    SET status = 'running', evidence_json = ?,
                        recovery_lease_until = NULL, updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token = ? AND owner_id = ?
                    """,
                    (evidence_json, now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated), changed
        finally:
            connection.close()

    def record_execution_tool_completion(
        self,
        execution_id: str,
        *,
        tool_name: str,
        success: bool,
        exit_code: int | None,
        recovery_token: str,
        owner_id: str,
        action: str | None = None,
        detail: str | None = None,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        tool = _require_stable_text(tool_name, "tool_name", limit=100)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        if not all(char.isalnum() or char in "_.:-" for char in tool):
            raise ValueError("tool_name contains unsupported characters")
        if not isinstance(success, bool):
            raise ValueError("success must be boolean")
        normalized_action = None
        if action is not None:
            normalized_action = _require_stable_text(action, "action", limit=100)
            if not all(
                char.isalnum() or char in "_.:-" for char in normalized_action
            ):
                raise ValueError("action contains unsupported characters")
        normalized_detail = None
        if detail is not None:
            normalized_detail = _require_stable_text(detail, "detail", limit=100)
            if not all(
                char.isalnum() or char in "._:/-" for char in normalized_detail
            ):
                raise ValueError("detail contains unsupported characters")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise ValueError("exit_code must be integer or null")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id != owner:
                    raise ValueError("execution owner is stale or foreign")
                if current.status != "running":
                    raise ValueError("tool completion requires a running execution")
                evidence = self._decode_execution_evidence(current.evidence_json)
                completions = evidence["tool_completions"]
                assert isinstance(completions, list)
                if len(completions) < 100:
                    receipt: dict[str, object] = {
                        "tool": tool,
                        "success": success,
                        "exit_code": exit_code,
                    }
                    if normalized_action is not None:
                        receipt["action"] = normalized_action
                    if normalized_detail is not None:
                        receipt["detail"] = normalized_detail
                    completions.append(receipt)
                evidence_json = json.dumps(
                    evidence, separators=(",", ":"), sort_keys=True
                )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions SET evidence_json = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token = ? AND owner_id = ?
                    """,
                    (evidence_json, now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    def mark_kanban_execution_admitted(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        owner_id: str,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id != owner:
                    raise ValueError("execution owner is stale or foreign")
                if current.status == "completed":
                    return current
                if current.status != "running" or current.mode != "kanban":
                    raise ValueError("only running Kanban intake may be admitted")
                evidence = self._decode_execution_evidence(current.evidence_json)
                completions = evidence["tool_completions"]
                if not any(
                    isinstance(item, dict)
                    and item.get("tool") == "kanban_task"
                    and item.get("success") is True
                    for item in completions
                ):
                    raise ValueError("successful kanban_task receipt is required")
                proposal_row = connection.execute(
                    "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                    (current.proposal_id, current.proposal_revision),
                ).fetchone()
                if proposal_row is None:
                    raise KeyError("execution proposal not found")
                proposal = _stored_proposal(proposal_row)
                if proposal.status is not ProposalStatus.QUEUED:
                    raise ValueError("Kanban-admitted proposal must remain queued")
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions SET status = 'completed', updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token = ? AND owner_id = ?
                    """,
                    (now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                connection.execute(
                    "UPDATE work_item_sessions SET state = 'queued', updated_at = ? WHERE subject_key = ?",
                    (now, proposal.subject_key),
                )
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    def mark_execution_verifying(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        owner_id: str,
    ) -> WorkExecution:
        return self._advance_execution_and_proposal(
            execution_id,
            recovery_token=recovery_token,
            owner_id=owner_id,
            expected_execution="running",
            target_execution="verifying",
            expected_proposal=ProposalStatus.RUNNING,
            target_proposal=ProposalStatus.VERIFYING,
        )

    def complete_execution_with_terminal_receipt(
        self,
        execution_id: str,
        *,
        content: str,
        recovery_token: str,
        owner_id: str,
    ) -> WorkExecution:
        execution_key = _require_stable_text(
            execution_id,
            "execution_id",
            limit=80,
        )
        receipt_body = _require_stable_text(content, "content", limit=1900)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        marker = (
            "[hermes-execution-receipt:"
            + hashlib.sha256(execution_key.encode("utf-8")).hexdigest()[:24]
            + "]"
        )
        receipt_content = f"{receipt_body}\n\n{marker}"
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id != owner:
                    raise ValueError("execution owner is stale or foreign")
                if current.status == "completed":
                    if (
                        row["terminal_receipt_marker"] == marker
                        and row["terminal_receipt_content"] == receipt_content
                        and row["terminal_receipt_status"] in {
                            "pending",
                            "sending",
                            "sent",
                        }
                    ):
                        return current
                    raise ValueError("completed execution receipt is missing")
                if current.status != "verifying":
                    raise ValueError("execution status transition mismatch")
                evidence = self._decode_execution_evidence(current.evidence_json)
                completions = evidence["tool_completions"]
                if not any(
                    isinstance(item, dict) and item.get("success") is True
                    for item in completions
                ):
                    raise ValueError("successful tool evidence is required")
                proposal_row = connection.execute(
                    """
                    SELECT * FROM work_proposals
                    WHERE proposal_id = ? AND proposal_revision = ?
                    """,
                    (current.proposal_id, current.proposal_revision),
                ).fetchone()
                if proposal_row is None:
                    raise KeyError("execution proposal not found")
                proposal = _stored_proposal(proposal_row)
                if proposal.status is not ProposalStatus.VERIFYING:
                    raise ValueError("execution proposal status transition mismatch")
                self._write_proposal_status(
                    connection,
                    proposal,
                    ProposalStatus.COMPLETED,
                    now,
                )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions
                    SET status = 'completed',
                        terminal_receipt_marker = ?,
                        terminal_receipt_content = ?,
                        terminal_receipt_status = 'pending',
                        terminal_receipt_lease_until = NULL,
                        terminal_receipt_claim_token = NULL,
                        recovery_lease_until = NULL,
                        updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token = ? AND owner_id = ?
                    """,
                    (marker, receipt_content, now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                connection.execute(
                    """
                    UPDATE work_item_sessions
                    SET state = 'completed', updated_at = ?
                    WHERE subject_key = ?
                    """,
                    (now, proposal.subject_key),
                )
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    def claim_terminal_receipt(
        self,
        *,
        lease_seconds: int = 60,
    ) -> TerminalReceiptClaim | None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        now_text = _iso_datetime(now)
        lease_until = _iso_datetime(now + timedelta(seconds=lease_seconds))
        claim_token = secrets.token_urlsafe(32)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM work_executions
                WHERE terminal_receipt_message_id IS NULL
                  AND terminal_receipt_marker IS NOT NULL
                  AND terminal_receipt_content IS NOT NULL
                  AND (
                    terminal_receipt_status = 'pending'
                    OR (
                        terminal_receipt_status = 'sending'
                        AND julianday(terminal_receipt_lease_until) <= julianday(?)
                    )
                  )
                ORDER BY updated_at, execution_id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            requires_reconciliation = str(row["terminal_receipt_status"]) == "sending"
            updated = connection.execute(
                """
                UPDATE work_executions
                SET terminal_receipt_status = 'sending',
                    terminal_receipt_lease_until = ?,
                    terminal_receipt_claim_token = ?, updated_at = ?
                WHERE execution_id = ?
                  AND terminal_receipt_message_id IS NULL
                  AND (
                    terminal_receipt_status = 'pending'
                    OR (
                        terminal_receipt_status = 'sending'
                        AND julianday(terminal_receipt_lease_until) <= julianday(?)
                    )
                  )
                """,
                (
                    lease_until,
                    claim_token,
                    now_text,
                    str(row["execution_id"]),
                    now_text,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claim = TerminalReceiptClaim(
                execution_id=str(row["execution_id"]),
                thread_id=str(row["thread_id"]),
                marker=str(row["terminal_receipt_marker"]),
                content=str(row["terminal_receipt_content"]),
                claim_token=claim_token,
                requires_reconciliation=requires_reconciliation,
            )
            connection.commit()
            return claim
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_terminal_receipt_lease(
        self,
        execution_id: str,
        *,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        token = _require_stable_text(claim_token, "claim_token", limit=100)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("lease_seconds must be between 1 and 3600")
        now_dt = self._clock()
        now = _iso_datetime(now_dt)
        lease_until = _iso_datetime(now_dt + timedelta(seconds=lease_seconds))
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE work_executions
                    SET terminal_receipt_lease_until = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND terminal_receipt_status = 'sending'
                      AND terminal_receipt_message_id IS NULL
                      AND terminal_receipt_claim_token = ?
                      AND julianday(terminal_receipt_lease_until) > julianday(?)
                    """,
                    (lease_until, now, execution_key, token, now),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def mark_terminal_receipt_sent(
        self,
        execution_id: str,
        *,
        claim_token: str,
        message_id: str,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        token = _require_stable_text(claim_token, "claim_token", limit=100)
        message = _require_stable_text(message_id, "message_id", limit=80)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_executions WHERE execution_id = ?",
                (execution_key,),
            ).fetchone()
            if row is None:
                raise KeyError("execution receipt not found")
            if row["terminal_receipt_claim_token"] != token:
                raise TerminalReceiptClaimLostError(
                    "terminal receipt claim is stale or foreign"
                )
            existing = row["terminal_receipt_message_id"]
            if existing is not None:
                if str(existing) != message:
                    raise ValueError("execution message binding is immutable")
                if row["terminal_receipt_status"] != "sent":
                    raise ValueError("terminal receipt state is invalid")
                connection.commit()
                return _work_execution(row)
            result = connection.execute(
                """
                UPDATE work_executions
                SET terminal_receipt_message_id = ?,
                    terminal_receipt_status = 'sent',
                    terminal_receipt_lease_until = NULL,
                    updated_at = ?
                WHERE execution_id = ?
                  AND terminal_receipt_status = 'sending'
                  AND terminal_receipt_message_id IS NULL
                  AND terminal_receipt_claim_token = ?
                  AND julianday(terminal_receipt_lease_until) > julianday(?)
                """,
                (message, now, execution_key, token, now),
            )
            if result.rowcount != 1:
                raise TerminalReceiptClaimLostError(
                    "terminal receipt claim is expired"
                )
            stored = connection.execute(
                "SELECT * FROM work_executions WHERE execution_id = ?",
                (execution_key,),
            ).fetchone()
            connection.commit()
            return _work_execution(stored)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_execution_completed(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        owner_id: str,
    ) -> WorkExecution:
        return self._advance_execution_and_proposal(
            execution_id,
            recovery_token=recovery_token,
            owner_id=owner_id,
            expected_execution="verifying",
            target_execution="completed",
            expected_proposal=ProposalStatus.VERIFYING,
            target_proposal=ProposalStatus.COMPLETED,
        )

    def _advance_execution_and_proposal(
        self,
        execution_id: str,
        *,
        recovery_token: str,
        owner_id: str,
        expected_execution: str,
        target_execution: str,
        expected_proposal: ProposalStatus,
        target_proposal: ProposalStatus,
    ) -> WorkExecution:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        token = _require_stable_text(recovery_token, "recovery_token", limit=80)
        owner = _require_stable_text(owner_id, "owner_id", limit=80)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                current = _work_execution(row)
                if current.recovery_token != token or current.owner_id != owner:
                    raise ValueError("execution owner is stale or foreign")
                if current.status == target_execution:
                    return current
                if current.status != expected_execution:
                    raise ValueError("execution status transition mismatch")
                evidence = self._decode_execution_evidence(current.evidence_json)
                completions = evidence["tool_completions"]
                if not any(
                    isinstance(item, dict) and item.get("success") is True
                    for item in completions
                ):
                    raise ValueError("successful tool evidence is required")
                proposal_row = connection.execute(
                    "SELECT * FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
                    (current.proposal_id, current.proposal_revision),
                ).fetchone()
                proposal = _stored_proposal(proposal_row)
                if proposal.status is not expected_proposal:
                    raise ValueError("execution proposal status transition mismatch")
                self._write_proposal_status(
                    connection, proposal, target_proposal, now
                )
                updated_execution = connection.execute(
                    """
                    UPDATE work_executions
                    SET status = ?, updated_at = ?
                    WHERE execution_id = ?
                      AND recovery_token = ? AND owner_id = ?
                    """,
                    (target_execution, now, execution_key, token, owner),
                )
                if updated_execution.rowcount != 1:
                    raise ValueError("execution owner is stale or foreign")
                connection.execute(
                    "UPDATE work_item_sessions SET state = ?, updated_at = ? WHERE subject_key = ?",
                    (target_execution, now, proposal.subject_key),
                )
                updated = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(updated)
        finally:
            connection.close()

    def get_execution(self, execution_id: str) -> WorkExecution | None:
        execution_key = _require_stable_text(execution_id, "execution_id", limit=80)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_executions WHERE execution_id = ?",
                (execution_key,),
            ).fetchone()
            return None if row is None else _work_execution(row)
        finally:
            connection.close()

    def record_execution_message(
        self,
        execution_id: str,
        message_id: str,
        *,
        terminal: bool = False,
    ) -> WorkExecution:
        execution_key = _require_stable_text(
            execution_id,
            "execution_id",
            limit=80,
        )
        message = _require_stable_text(message_id, "message_id", limit=80)
        column = (
            "terminal_receipt_message_id"
            if terminal
            else "status_message_id"
        )
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution receipt not found")
                existing = row[column]
                if existing is not None and str(existing) != message:
                    raise ValueError("execution message binding is immutable")
                connection.execute(
                    f"""
                    UPDATE work_executions
                    SET {column} = ?, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (message, now, execution_key),
                )
                stored = connection.execute(
                    "SELECT * FROM work_executions WHERE execution_id = ?",
                    (execution_key,),
                ).fetchone()
                return _work_execution(stored)
        finally:
            connection.close()

    def get_execution_for_proposal(
        self, proposal_id: str, revision: int
    ) -> WorkExecution | None:
        proposal_key = _require_stable_text(proposal_id, "proposal_id", limit=80)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM work_executions WHERE proposal_id = ? AND proposal_revision = ?",
                (proposal_key, revision),
            ).fetchone()
            return None if row is None else _work_execution(row)
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
                    (
                        o.status = 'sending'
                        AND julianday(o.lease_until) <= julianday(?)
                    )
                ) AND o.event_json IS NOT NULL AND o.source_revision IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM delivery_outbox active
                      WHERE active.destination = o.destination
                        AND active.status = 'sending'
                        AND julianday(active.lease_until) > julianday(?)
                  )
                ORDER BY o.delivery_id LIMIT 1
                """,
                (destination, now, now),
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
                source_revision=str(row["source_revision"]),
                revision_number=int(row["revision_number"]),
                destination=str(row["destination"]),
                marker=str(row["marker"]),
                message_id=None if row["message_id"] is None else str(row["message_id"]),
                attempts=attempts,
                requires_reconciliation=uncertain,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_delivery_lease(
        self,
        delivery_id: int,
        *,
        expected_attempt: int,
        lease_seconds: int,
    ) -> bool:
        if (
            isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or expected_attempt <= 0
        ):
            raise ValueError("expected_attempt must be a positive integer")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be a positive integer")
        now_dt = self._clock().astimezone(timezone.utc)
        lease_until = _iso_datetime(now_dt + timedelta(seconds=lease_seconds))
        now = _iso_datetime(now_dt)
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET lease_until = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'sending'
                      AND attempts = ?
                      AND julianday(lease_until) > julianday(?)
                    """,
                    (lease_until, now, delivery_id, expected_attempt, now),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def release_delivery(
        self,
        delivery_id: int,
        *,
        claim_token: int,
        error_category: str,
    ) -> bool:
        category = _require_error_category(error_category)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'pending', lease_until = NULL,
                        error_category = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'sending'
                      AND attempts = ?
                    """,
                    (category, now, delivery_id, claim_token),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def note_delivery_error(
        self,
        delivery_id: int,
        *,
        error_category: str,
        claim_token: int,
    ) -> bool:
        category = _require_error_category(error_category)
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET error_category = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'sending'
                      AND attempts = ?
                    """,
                    (category, now, delivery_id, claim_token),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def mark_delivery_parent_confirmed(
        self,
        delivery_id: int,
        *,
        claim_token: int,
        message_id: str,
    ) -> bool:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, message_id, attempts
                FROM delivery_outbox
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError("delivery not found")
            existing = row["message_id"]
            if existing is not None and str(existing) != message_id:
                raise ValueError("delivery already maps to a different message")
            if int(row["attempts"]) != claim_token:
                connection.rollback()
                return False
            if str(row["status"]) == "sent":
                connection.commit()
                return True
            if str(row["status"]) != "sending":
                raise ValueError("only a sending delivery may confirm its parent")
            result = connection.execute(
                """
                UPDATE delivery_outbox
                SET message_id = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'sending'
                  AND attempts = ?
                """,
                (message_id, now, delivery_id, claim_token),
            )
            connection.commit()
            return result.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_delivery_sent(
        self,
        delivery_id: int,
        *,
        claim_token: int,
        message_id: str,
    ) -> bool:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a non-empty string")
        now = _iso_datetime(self._clock())
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'sent', message_id = ?, lease_until = NULL,
                        error_category = NULL, updated_at = ?
                    WHERE delivery_id = ? AND status = 'sending'
                      AND attempts = ?
                    """,
                    (message_id, now, delivery_id, claim_token),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def pending_delivery_error_category(self) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT error_category
                FROM delivery_outbox
                WHERE status IN ('pending', 'sending')
                  AND error_category IS NOT NULL
                ORDER BY delivery_id
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else str(row["error_category"])
        finally:
            connection.close()

    def health(self, collector_key: str) -> dict[str, object]:
        collector = self.get_collector_status(collector_key)
        delivery_error = self.pending_delivery_error_category()
        if collector is None:
            status = "degraded"
            error_category = "not_started"
        elif collector.status != "ok":
            status = collector.status
            error_category = collector.error_category
        elif delivery_error is not None:
            status = "degraded"
            error_category = delivery_error
        else:
            status = collector.status
            error_category = collector.error_category
        return {
            "status": status,
            "error_category": error_category,
            "last_attempt_at": None if collector is None else collector.last_attempt_at,
            "last_success_at": None if collector is None else collector.last_success_at,
            "next_poll_at": None if collector is None else collector.next_poll_at,
            "consecutive_failures": (
                0 if collector is None else collector.consecutive_failures
            ),
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
