from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def intake_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _intake_kwargs(**overrides):
    values = {
        "idempotency_key": "gateway-intake:key-1",
        "request_hash": "request-hash-1",
        "actor_profile": "default",
        "assignee": "default",
        "source_context": {
            "platform": "discord",
            "profile": "default",
            "chat_id": "thread-1",
            "user_id": "user-1",
            "message_id": "message-1",
        },
        "platform": "discord",
        "chat_id": "thread-1",
        "thread_id": "thread-1",
        "user_id": "user-1",
        "notifier_profile": "default",
        "title": "Implement durable intake",
        "body": "Preserve evidence and report completion.",
        "priority": 7,
        "max_runtime_seconds": 900,
        "max_retries": 1,
        "goal_mode": True,
        "goal_max_turns": 4,
        "session_id": "session-1",
    }
    values.update(overrides)
    return values


def test_intake_atomically_creates_task_receipt_and_subscription(intake_home):
    with kb.connect_closing() as conn:
        task_id, created = kb.create_intake_task(conn, **_intake_kwargs())
        task = kb.get_task(conn, task_id)
        receipt = conn.execute(
            "SELECT * FROM kanban_intake_receipts WHERE idempotency_key = ?",
            ("gateway-intake:key-1",),
        ).fetchone()
        subscription = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    assert created is True
    assert task is not None
    assert task.assignee == "default"
    assert task.priority == 7
    assert task.max_runtime_seconds == 900
    assert task.max_retries == 1
    assert task.goal_mode is True
    assert task.goal_max_turns == 4
    assert task.session_id == "session-1"
    assert receipt["task_id"] == task_id
    assert receipt["actor_profile"] == "default"
    assert subscription["platform"] == "discord"
    assert subscription["chat_id"] == "thread-1"
    assert subscription["notifier_profile"] == "default"


def test_intake_receipt_deduplicates_after_task_is_archived(intake_home):
    with kb.connect_closing() as conn:
        first_id, first_created = kb.create_intake_task(conn, **_intake_kwargs())
        assert kb.archive_task(conn, first_id)
        second_id, second_created = kb.create_intake_task(conn, **_intake_kwargs())
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ?",
            ("gateway-intake:key-1",),
        ).fetchall()

    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert [(row["id"], row["status"]) for row in rows] == [(first_id, "archived")]


def test_intake_rejects_same_key_with_different_immutable_content(intake_home):
    with kb.connect_closing() as conn:
        task_id, _ = kb.create_intake_task(conn, **_intake_kwargs())
        with pytest.raises(kb.KanbanIntakeConflict):
            kb.create_intake_task(
                conn,
                **_intake_kwargs(request_hash="different-request-hash"),
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT task_id FROM kanban_intake_receipts"
        ).fetchone()[0] == task_id


def test_intake_rolls_back_every_row_when_subscription_insert_fails(
    intake_home, monkeypatch
):
    def fail_subscription(*_args, **_kwargs):
        raise RuntimeError("subscription write failed")

    monkeypatch.setattr(kb, "_insert_notify_sub", fail_subscription)
    with kb.connect_closing() as conn:
        with pytest.raises(RuntimeError, match="subscription write failed"):
            kb.create_intake_task(conn, **_intake_kwargs())
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_receipts"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs"
        ).fetchone()[0] == 0


def test_concurrent_intake_creators_share_one_durable_receipt(intake_home):
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []
    errors: list[BaseException] = []

    def create():
        try:
            with kb.connect_closing() as conn:
                barrier.wait(timeout=5)
                results.append(kb.create_intake_task(conn, **_intake_kwargs()))
        except BaseException as exc:  # pragma: no cover - thread handoff
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert len({task_id for task_id, _created in results}) == 1
    assert sorted(created for _task_id, created in results) == [False, True]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_receipts"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs"
        ).fetchone()[0] == 1
