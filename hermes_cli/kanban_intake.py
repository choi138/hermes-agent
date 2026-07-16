"""Gateway-owned operations for Discord Kanban intake tasks."""

from __future__ import annotations

import sqlite3
from typing import Any

from hermes_cli import kanban_db as kb


_OWNER_FIELDS = (
    "platform",
    "profile",
    "scope_id",
    "parent_chat_id",
    "chat_id",
    "thread_id",
    "user_id",
)
_UPDATEABLE_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "blocked"})


class KanbanIntakeAccessError(ValueError):
    """The task is absent or not owned by the trusted Discord source."""


def _owner_identity(source_context: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(source_context.get(name) or "") for name in _OWNER_FIELDS)


def require_owned_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_context: dict[str, Any],
) -> kb.Task:
    """Load an intake task after matching its durable create provenance."""

    task = kb.get_task(conn, task_id)
    provenance = kb.get_intake_source_context(conn, task_id)
    if (
        task is None
        or provenance is None
        or _owner_identity(provenance) != _owner_identity(source_context)
    ):
        raise KanbanIntakeAccessError(
            "task not found or not owned by this Discord conversation"
        )
    return task


def _task_payload(task: kb.Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "status": task.status,
        "priority": task.priority,
        "goal_mode": task.goal_mode,
        "goal_max_turns": task.goal_max_turns,
        "max_retries": task.max_retries,
        "max_runtime_seconds": task.max_runtime_seconds,
    }


def status_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_context: dict[str, Any],
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any]:
    """Return a safe task snapshot plus its latest run state and summary."""

    with kb.write_txn(conn):
        replay = kb.replay_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="status",
            task_id=task_id,
            source_context=source_context,
        )
        if replay is not None:
            return {**replay, "deduplicated": True}

        task = require_owned_task(
            conn,
            task_id=task_id,
            source_context=source_context,
        )
        latest_run = kb.latest_run(conn, task_id)
        result = {
            "task": _task_payload(task),
            "latest_run": (
                {
                    "status": latest_run.status,
                    "outcome": latest_run.outcome,
                    "summary": latest_run.summary,
                    "started_at": latest_run.started_at,
                    "ended_at": latest_run.ended_at,
                }
                if latest_run is not None
                else None
            ),
            "latest_summary": kb.latest_summary(conn, task_id),
        }
        kb.record_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="status",
            task_id=task_id,
            source_context=source_context,
            result=result,
        )
        return {**result, "deduplicated": False}


def update_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_context: dict[str, Any],
    changes: dict[str, Any],
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any]:
    """Update safe user fields on an owned, inactive intake task."""

    with kb.write_txn(conn):
        replay = kb.replay_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="update",
            task_id=task_id,
            source_context=source_context,
        )
        if replay is not None:
            return {**replay, "deduplicated": True}

        require_owned_task(
            conn,
            task_id=task_id,
            source_context=source_context,
        )
        task, changed_fields = kb.update_task_fields(
            conn,
            task_id,
            changes=changes,
            allowed_statuses=_UPDATEABLE_STATUSES,
            _manage_transaction=False,
        )
        result = {
            "task": _task_payload(task),
            "updated_fields": changed_fields,
        }
        kb.record_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="update",
            task_id=task_id,
            source_context=source_context,
            result=result,
        )
        return {**result, "deduplicated": False}


def retry_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source_context: dict[str, Any],
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any]:
    """Requeue an eligible owned task through supported state-machine APIs."""

    with kb.write_txn(conn):
        replay = kb.replay_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="retry",
            task_id=task_id,
            source_context=source_context,
        )
        if replay is not None:
            return {**replay, "deduplicated": True}

        task = require_owned_task(
            conn,
            task_id=task_id,
            source_context=source_context,
        )
        retried_from: str
        if task.status == "blocked":
            retried_from = "blocked"
            changed = kb.unblock_task(
                conn,
                task_id,
                _manage_transaction=False,
            )
        elif task.status == "triage":
            retried_from = "triage"
            changed = kb.specify_triage_task(
                conn,
                task_id,
                _manage_transaction=False,
            )
        else:
            retried_from = "failed"
            changed = kb.retry_failed_task(
                conn,
                task_id,
                _manage_transaction=False,
            )
            if not changed:
                raise ValueError(f"cannot retry task in {task.status} state")

        if not changed:
            current = kb.get_task(conn, task_id)
            state = current.status if current is not None else "missing"
            raise ValueError(f"cannot retry task in {state} state")
        updated = kb.get_task(conn, task_id)
        if updated is None:  # pragma: no cover - guarded by ownership + transition
            raise KanbanIntakeAccessError(
                "task not found or not owned by this Discord conversation"
            )
        result = {
            "task": _task_payload(updated),
            "retried_from": retried_from,
        }
        kb.record_intake_operation(
            conn,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            operation="retry",
            task_id=task_id,
            source_context=source_context,
            result=result,
        )
        return {**result, "deduplicated": False}
