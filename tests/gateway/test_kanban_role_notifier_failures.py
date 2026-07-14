import asyncio

from gateway.config import Platform
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_role_notifier import (
    FailingAdapter,
    RecordingAdapter,
    _make_discord_runner,
    _run_one_notifier_tick,
)


def test_discord_run_send_failure_warns_via_coordinator(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "role-send-failure.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="permission failure", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="worker result")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    runner = _make_discord_runner(
        coordinator, {"shinei": {Platform.DISCORD: FailingAdapter()}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(coordinator.sent) == 1
    warning = coordinator.sent[0]["text"].lower()
    assert "delivery failed" in warning
    assert "check gateway logs" in warning
    assert "missing channel permission" not in warning
    assert "worker result" not in warning


def test_discord_status_stays_on_coordinator_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "coordinator-status.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="assignment", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb._append_event(conn, tid, "status", {"status": "ready"})
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    shinei = RecordingAdapter()
    runner = _make_discord_runner(
        coordinator, {"shinei": {Platform.DISCORD: shinei}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(coordinator.sent) == 1
    assert "ready" in coordinator.sent[0]["text"]
    assert shinei.sent == []


def test_legacy_execution_event_uses_explicit_assignee_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy-fallback.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy event", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb._append_event(conn, tid, "completed", {"summary": "legacy result"})
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    shinei = RecordingAdapter()
    runner = _make_discord_runner(
        coordinator, {"shinei": {Platform.DISCORD: shinei}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert "legacy result" in shinei.sent[0]["text"]
    assert coordinator.sent == []


def test_final_synthesis_stays_on_coordinator_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "coordinator-wake.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="synthesis",
            assignee="shinei",
            session_id="discord:group:thread-1",
        )
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="ready for synthesis")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    shinei = RecordingAdapter()
    runner = _make_discord_runner(
        coordinator, {"shinei": {Platform.DISCORD: shinei}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert len(coordinator.handled) == 1
    assert shinei.handled == []
