"""Durable, gateway-owned Kanban intake and operation receipts.

Every mutation here composes under one board write transaction. The receipt,
task, and notification subscription either commit together or not at all.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


class KanbanIntakeConflict(ValueError):
    """A durable intake key was reused with different immutable content."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def get_intake_source_context(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT source_context FROM kanban_intake_receipts "
        "WHERE task_id = ? ORDER BY created_at ASC, idempotency_key ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["source_context"])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def replay_intake_operation(
    conn: sqlite3.Connection, *, idempotency_key: str, request_hash: str,
    operation: str, task_id: str, source_context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Return a committed response, rejecting key/content collisions."""
    if conn.execute(
        "SELECT 1 FROM kanban_intake_receipts WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone() is not None:
        raise KanbanIntakeConflict(
            "idempotency receipt was reused with different immutable content"
        )
    row = conn.execute(
        "SELECT request_hash, operation, task_id, source_context, result_json "
        "FROM kanban_intake_operations WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["request_hash"], row["operation"], row["task_id"], row["source_context"]
    ) != (request_hash, operation, task_id, _canonical_json(source_context)):
        raise KanbanIntakeConflict(
            "idempotency receipt was reused with different immutable content"
        )
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError) as exc:
        raise KanbanIntakeConflict("idempotency receipt result is invalid") from exc
    if not isinstance(result, dict):
        raise KanbanIntakeConflict("idempotency receipt result is invalid")
    return result


def record_intake_operation(
    conn: sqlite3.Connection, *, idempotency_key: str, request_hash: str,
    operation: str, task_id: str, source_context: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if operation not in {"status", "update", "retry"}:
        raise ValueError("operation receipt must be status, update, or retry")
    conn.execute(
        "INSERT INTO kanban_intake_operations "
        "(idempotency_key, request_hash, operation, task_id, source_context, "
        "result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            idempotency_key, request_hash, operation, task_id,
            _canonical_json(source_context), _canonical_json(result), int(time.time()),
        ),
    )


def _insert_notify_sub(
    conn: sqlite3.Connection, *, task_id: str, platform: str, chat_id: str,
    chat_type: Optional[str] = None, thread_id: Optional[str] = None,
    user_id: Optional[str] = None, notifier_profile: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    created_at: Optional[int] = None,
) -> None:
    """Insert a caught-up subscription inside the caller's transaction."""
    from hermes_cli.kanban_db_notify import _encode_notify_delivery_metadata

    thread = thread_id or ""
    metadata = _encode_notify_delivery_metadata(delivery_metadata)
    now = int(time.time()) if created_at is None else int(created_at)
    conn.execute(
        """INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, chat_type, thread_id, user_id,
             notifier_profile, delivery_metadata, created_at, last_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))""",
        (
            task_id, platform, chat_id, chat_type, thread, user_id,
            notifier_profile, metadata, now, task_id,
        ),
    )
    key = (task_id, platform, chat_id, thread)
    for column, value in (
        ("chat_type", chat_type), ("user_id", user_id),
        ("notifier_profile", notifier_profile),
    ):
        if value:
            conn.execute(
                f"UPDATE kanban_notify_subs SET {column} = ? "
                "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
                f"AND ({column} IS NULL OR {column} = '')",
                (value, *key),
            )
    if metadata:
        conn.execute(
            "UPDATE kanban_notify_subs SET delivery_metadata = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (metadata, *key),
        )


def create_intake_task(
    conn: sqlite3.Connection, *, idempotency_key: str, request_hash: str,
    actor_profile: str, assignee: str, source_context: dict[str, Any],
    platform: str, chat_id: str, title: str,
    chat_type: Optional[str] = None, thread_id: Optional[str] = None,
    user_id: Optional[str] = None, notifier_profile: Optional[str] = None,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    body: Optional[str] = None, priority: int = 0,
    max_runtime_seconds: Optional[int] = None, max_retries: Optional[int] = None,
    goal_mode: bool = False, goal_max_turns: Optional[int] = None,
    session_id: Optional[str] = None,
) -> tuple[str, bool]:
    """Create the task, create receipt, and subscription in one transaction."""
    key = str(idempotency_key or "").strip()
    digest = str(request_hash or "").strip()
    actor = str(actor_profile or "").strip()
    canonical_assignee = _kb._canonical_assignee(assignee)
    platform_name = str(platform or "").strip()
    target_chat = str(chat_id or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    if not digest:
        raise ValueError("request_hash is required")
    if not actor:
        raise ValueError("actor_profile is required")
    if not canonical_assignee:
        raise ValueError("assignee is required")
    if not isinstance(source_context, dict):
        raise ValueError("source_context must be an object")
    if not platform_name or not target_chat:
        raise ValueError("notification platform and chat_id are required")

    source_json = _canonical_json(source_context)
    now = int(time.time())
    with _kb.write_txn(conn):
        if conn.execute(
            "SELECT 1 FROM kanban_intake_operations WHERE idempotency_key = ?",
            (key,),
        ).fetchone() is not None:
            raise KanbanIntakeConflict(
                "idempotency receipt was reused with different immutable content"
            )
        receipt = conn.execute(
            "SELECT request_hash, task_id, actor_profile, assignee, source_context "
            "FROM kanban_intake_receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if receipt is not None:
            if (
                receipt["request_hash"], receipt["actor_profile"],
                receipt["assignee"], receipt["source_context"],
            ) != (digest, actor, canonical_assignee, source_json):
                raise KanbanIntakeConflict(
                    "idempotency receipt was reused with different immutable content"
                )
            task_id = str(receipt["task_id"])
            if _kb.get_task(conn, task_id) is None:
                raise KanbanIntakeConflict(
                    f"idempotency receipt references missing task {task_id}"
                )
            _kb._insert_notify_sub(
                conn, task_id=task_id, platform=platform_name, chat_id=target_chat,
                chat_type=chat_type, thread_id=thread_id, user_id=user_id,
                notifier_profile=notifier_profile,
                delivery_metadata=delivery_metadata, created_at=now,
            )
            return task_id, False

        # create_task uses a savepoint when called under this outer transaction.
        task_id = _kb.create_task(
            conn, title=title, body=body, assignee=canonical_assignee,
            created_by=actor, priority=priority, idempotency_key=key,
            max_runtime_seconds=max_runtime_seconds, max_retries=max_retries,
            goal_mode=goal_mode, goal_max_turns=goal_max_turns,
            session_id=session_id,
        )
        conn.execute(
            "INSERT INTO kanban_intake_receipts "
            "(idempotency_key, request_hash, task_id, actor_profile, assignee, "
            "source_context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, digest, task_id, actor, canonical_assignee, source_json, now),
        )
        _kb._insert_notify_sub(
            conn, task_id=task_id, platform=platform_name, chat_id=target_chat,
            chat_type=chat_type, thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
            delivery_metadata=delivery_metadata, created_at=now,
        )
        return task_id, True


def update_task_fields(
    conn: sqlite3.Connection, task_id: str, *, changes: dict[str, Any],
    allowed_statuses: Iterable[str], _manage_transaction: bool = True,
) -> tuple[Task, list[str]]:
    unknown = set(changes) - {"title", "body", "priority"}
    if unknown:
        raise ValueError("unsupported task field(s): " + ", ".join(sorted(unknown)))
    if not changes:
        raise ValueError("at least one task field is required")
    allowed = frozenset(str(status) for status in allowed_statuses)
    transaction = _kb.write_txn(conn) if _manage_transaction else contextlib.nullcontext(conn)
    with transaction:
        row = conn.execute(
            "SELECT title, body, priority, status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"task {task_id} not found")
        if row["status"] not in allowed:
            raise ValueError(f"cannot update task in {row['status']} state")
        changed_fields = sorted(name for name, value in changes.items() if row[name] != value)
        if changed_fields:
            assignments = ", ".join(f"{name} = ?" for name in changed_fields)
            values = [changes[name] for name in changed_fields]
            cur = conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ? AND status = ?",
                (*values, task_id, row["status"]),
            )
            if cur.rowcount != 1:
                raise ValueError("task state changed during update")
            _kb._append_event(conn, task_id, "edited", {"fields": changed_fields})
    task = _kb.get_task(conn, task_id)
    if task is None:  # pragma: no cover - guarded by the transaction
        raise ValueError(f"task {task_id} not found")
    return task, changed_fields


def retry_failed_task(
    conn: sqlite3.Connection, task_id: str, *, _manage_transaction: bool = True,
) -> bool:
    """Requeue a ready task with a failed latest run, or a legacy failed task."""
    retryable = {"spawn_failed", "crashed", "timed_out", "gave_up"}
    transaction = _kb.write_txn(conn) if _manage_transaction else contextlib.nullcontext(conn)
    with transaction:
        task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return False
        latest = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE task_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        failed_run = latest is not None and (
            latest["status"] == "failed" or latest["outcome"] in retryable
        )
        if task["status"] != "failed" and not (task["status"] == "ready" and failed_run):
            return False
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status = ?",
            (task_id, task["status"]),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn, task_id, "retried",
            {
                "from": "failed",
                "run_status": latest["status"] if latest is not None else None,
                "run_outcome": latest["outcome"] if latest is not None else None,
            },
        )
        return True


# Facade bindings (including test patch seams) are resolved only after this module loads.
from hermes_cli import kanban_db as _kb  # noqa: E402
