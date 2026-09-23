"""Gateway restart recovery must re-enter a durable agent turn, not add a user turn."""

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway import delivery_ledger
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from gateway.turn_context import TurnContext
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = None
    return store


def test_durable_turn_record_round_trip_and_interrupted_finish(tmp_path):
    store = _store(tmp_path)
    source = SessionSource(platform=Platform.DISCORD, chat_id="room", user_id="owner")
    entry = store.get_or_create_session(source)
    turn_id = f"{entry.session_id}:{entry.session_id}:deadbeef"

    assert store.begin_active_turn(entry.session_key, turn_id, "boot-1") is True
    reloaded = _store(tmp_path)
    record = reloaded.get_or_create_session(source).active_turn
    assert record is not None
    assert record["turn_id"] == turn_id
    assert record["resume_count"] == 0

    assert reloaded.mark_active_turn_interrupted(entry.session_key, "restart_timeout") is True
    assert reloaded.finish_active_turn(entry.session_key, turn_id, turn_interrupted=True) is False
    assert reloaded.get_or_create_session(source).active_turn is not None

    assert reloaded.begin_active_turn(entry.session_key, turn_id, "boot-2", resume_count=1) is True
    assert reloaded.finish_active_turn(entry.session_key, "another-turn") is False
    assert reloaded.finish_active_turn(entry.session_key, turn_id) is True
    assert _store(tmp_path).get_or_create_session(source).active_turn is None


def test_failed_turn_record_write_never_leaks_into_memory(tmp_path):
    store = _store(tmp_path)
    entry = store.get_or_create_session(
        SessionSource(platform=Platform.DISCORD, chat_id="room", user_id="owner")
    )
    store._save_entry = MagicMock(side_effect=OSError("unavailable"))

    with pytest.raises(OSError, match="unavailable"):
        store.begin_active_turn(entry.session_key, "turn-1", "boot-1")

    assert entry.active_turn is None


@pytest.mark.asyncio
async def test_completed_reply_keeps_turn_record_until_adapter_delivery(tmp_path, monkeypatch):
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="handoff")
    event = MessageEvent(
        text="make a report", message_type=MessageType.TEXT, source=source,
        message_id="inbound-1",
    )
    key = runner._session_key_for_source(source)
    event._gateway_active_turn_session_key = key
    event._gateway_active_turn_token = "token-1"
    event._gateway_active_turn_id = "turn-1"
    finish = AsyncMock(return_value=True)
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, clear_turn_active=AsyncMock(return_value=True),
        finish_active_turn=finish,
    )

    await runner._clear_durable_active_turn(event, defer_delivery=True)
    finish.assert_not_awaited()

    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="finished report")
    adapter._active_sessions[key] = asyncio.Event()
    monkeypatch.setattr(delivery_ledger, "_db_path", lambda: tmp_path / "state.db")
    await adapter._process_message_background(event, key)

    finish.assert_awaited_once_with(key, "turn-1")
    with delivery_ledger._connect() as conn:
        row = conn.execute("SELECT turn_id, state FROM delivery_obligations").fetchone()
    assert row == ("turn-1", "delivered")


@pytest.mark.asyncio
async def test_turn_retirement_cancellation_releases_adapter_session_guard():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="cancel-retire")
    key = runner._session_key_for_source(source)
    event = MessageEvent(
        text="make a report", message_type=MessageType.TEXT, source=source,
        message_id="inbound-cancel-retire",
    )
    event._gateway_active_turn_delivery_pending = True
    event._gateway_active_turn_delivery_session_key = key
    event._gateway_active_turn_id = "turn-1"
    finish = AsyncMock(side_effect=asyncio.CancelledError)
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, finish_active_turn=finish,
    )
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="finished report")

    assert adapter._start_session_processing(event, key)
    task = adapter._session_tasks[key]
    with pytest.raises(asyncio.CancelledError):
        await task

    finish.assert_awaited_once_with(key, "turn-1")
    assert key not in adapter._active_sessions
    assert key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_interrupted_turn_record_is_not_retired_on_unwind():
    runner, _ = make_restart_runner()
    finish = AsyncMock(return_value=False)
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, clear_turn_active=AsyncMock(return_value=True),
        finish_active_turn=finish,
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="session-key",
        _gateway_active_turn_token="token-1",
        _gateway_active_turn_id="turn-1",
        _gateway_turn_result_interrupted=True,
    )

    await runner._clear_durable_active_turn(event)

    finish.assert_awaited_once_with("session-key", "turn-1", turn_interrupted=True)


@pytest.mark.asyncio
async def test_shutdown_marks_running_turn_interrupted_before_unwind(tmp_path):
    runner, _ = make_restart_runner()
    source = make_restart_source(chat_id="shutdown")
    runner.session_store = _store(tmp_path)
    entry = runner.session_store.get_or_create_session(source)
    assert runner.session_store.begin_active_turn(entry.session_key, "turn-1", "old-boot")
    runner._running_agents[entry.session_key] = object()

    marked = await runner._mark_running_sessions_resume_pending("test")

    assert marked == [entry.session_key]
    record = _store(tmp_path).get_or_create_session(source).active_turn
    assert record["status"] == "interrupted"
    assert record["interrupted_reason"] == "shutdown_timeout"


@pytest.mark.asyncio
async def test_unledgered_failed_delivery_keeps_turn_for_recovery():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="undelivered")
    key = runner._session_key_for_source(source)
    event = MessageEvent(
        text="make a report", message_type=MessageType.TEXT, source=source,
        message_id="inbound-2",
    )
    event._gateway_active_turn_session_key = key
    event._gateway_active_turn_token = "token-1"
    event._gateway_active_turn_id = "turn-1"
    finish = AsyncMock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, clear_turn_active=AsyncMock(return_value=True),
        finish_active_turn=finish,
    )
    await runner._clear_durable_active_turn(event, defer_delivery=True)
    adapter.gateway_runner = runner
    adapter._message_handler = AsyncMock(return_value="finished report")
    adapter._record_delivery_obligation = AsyncMock(return_value=None)
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=False, error="offline"))
    adapter._active_sessions[key] = asyncio.Event()

    await adapter._process_message_background(event, key)

    finish.assert_not_awaited()
    assert event._gateway_active_turn_delivery_pending is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "delivered"])
async def test_answered_turn_is_not_reentered_after_restart(tmp_path, monkeypatch, state):
    monkeypatch.setattr(delivery_ledger, "_db_path", lambda: tmp_path / "state.db")
    runner, adapter = make_restart_runner()
    runner._boot_id = "new-boot"
    source = make_restart_source(chat_id="answered")
    entry = _store(tmp_path).get_or_create_session(source)
    turn_id = f"{entry.session_id}:{entry.session_id}:deadbeef"
    runner.session_store = _store(tmp_path)
    assert runner.session_store.begin_active_turn(entry.session_key, turn_id, "old-boot")
    delivery_ledger.record_obligation(
        obligation_id=f"ob-{state}", session_key=entry.session_key,
        platform="telegram", chat_id=source.chat_id, thread_id=None,
        content="completed answer", turn_id=turn_id,
    )
    if state == "delivered":
        delivery_ledger.mark_delivered(f"ob-{state}")
    adapter.handle_message = AsyncMock()

    await runner._reconcile_answered_turns()

    assert runner._schedule_resume_pending_sessions() == 0
    adapter.handle_message.assert_not_awaited()
    assert _store(tmp_path).get_or_create_session(source).active_turn is None


def test_answered_turn_is_not_scheduled_even_without_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery_ledger, "_db_path", lambda: tmp_path / "state.db")
    runner, adapter = make_restart_runner()
    runner._boot_id = "new-boot"
    source = make_restart_source(chat_id="answered-direct")
    runner.session_store = _store(tmp_path)
    entry = runner.session_store.get_or_create_session(source)
    turn_id = f"{entry.session_id}:{entry.session_id}:deadbeef"
    assert runner.session_store.begin_active_turn(entry.session_key, turn_id, "old-boot")
    delivery_ledger.record_obligation(
        obligation_id="ob-direct", session_key=entry.session_key,
        platform="telegram", chat_id=source.chat_id, thread_id=None,
        content="completed answer", turn_id=turn_id,
    )
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 0
    adapter.handle_message.assert_not_called()
    assert _store(tmp_path).get_or_create_session(source).active_turn is None


@pytest.mark.asyncio
async def test_orphaned_turn_is_scheduled_with_its_original_id():
    runner, adapter = make_restart_runner()
    runner._boot_id = "new-boot"
    source = make_restart_source(chat_id="orphaned")
    entry = SessionEntry(
        session_key=runner._session_key_for_source(source), session_id="sid",
        created_at=datetime.now(), updated_at=datetime.now(), origin=source,
        platform=Platform.TELEGRAM,
        active_turn={
            "turn_id": "sid:sid:deadbeef", "boot_id": "old-boot", "status": "running",
            "started_at": datetime.now().isoformat(), "resume_count": 0,
        },
    )
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store.begin_active_turn.return_value = True
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.sleep(0)

    event = adapter.handle_message.await_args.args[0]
    assert event.text == ""
    assert event._hermes_turn_resume == {
        "turn_id": "sid:sid:deadbeef", "resume_count": 1, "record_backed": True,
    }
    runner.session_store.begin_active_turn.assert_called_once_with(
        entry.session_key, "sid:sid:deadbeef", "new-boot", resume_count=1,
    )


@pytest.mark.asyncio
async def test_suspended_or_stale_or_exhausted_turn_never_auto_reenters():
    runner, adapter = make_restart_runner()
    runner._boot_id = "new-boot"
    source = make_restart_source(chat_id="unsafe")
    entry = SessionEntry(
        session_key=runner._session_key_for_source(source), session_id="sid",
        created_at=datetime.now(), updated_at=datetime.now(), origin=source,
        platform=Platform.TELEGRAM,
        active_turn={
            "turn_id": "sid:sid:deadbeef", "boot_id": "old-boot", "status": "running",
            "started_at": datetime.now().isoformat(), "resume_count": 0,
        },
    )
    runner.session_store._entries = {entry.session_key: entry}
    adapter.handle_message = AsyncMock()

    entry.suspended = True  # /stop wins over crash recovery.
    assert runner._schedule_resume_pending_sessions() == 0
    entry.suspended = False
    entry.active_turn["started_at"] = (datetime.now() - timedelta(hours=3)).isoformat()
    assert runner._schedule_resume_pending_sessions() == 0
    entry.active_turn["started_at"] = datetime.now().isoformat()
    entry.active_turn["resume_count"] = 2
    assert runner._schedule_resume_pending_sessions() == 0
    adapter.handle_message.assert_not_called()


def _turn_runner(entry, marker, history):
    runner = SimpleNamespace(
        session_store=SimpleNamespace(_entries={entry.session_key: entry}),
        _pending_model_notes={}, _pending_skills_reload_notes={},
        _delivery_adapter_for=lambda _source: SimpleNamespace(interactive_resume=True),
        _consume_pending_native_image_paths=lambda _key: [],
    )
    ctx = TurnContext(
        source=entry.origin, session_id=entry.session_id, session_key=entry.session_key,
        message="", history=history,
        event=SimpleNamespace(_hermes_turn_resume=marker),
    )
    return TurnRunner(runner, ctx), ctx


def test_record_backed_resume_calls_agent_without_new_user_message():
    source = make_restart_source(chat_id="resume")
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:resume", session_id="sid",
        created_at=datetime.now(), updated_at=datetime.now(), origin=source,
        resume_pending=True, resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
        active_turn={"turn_id": "sid:sid:deadbeef", "boot_id": "new-boot",
                     "status": "resuming", "resume_count": 1},
    )
    history = [{"role": "user", "content": "make a report"},
               {"role": "tool", "content": "42", "tool_call_id": "call-1"}]
    marker = {"turn_id": "sid:sid:deadbeef", "resume_count": 1, "record_backed": True}
    turn, ctx = _turn_runner(entry, marker, history)
    agent = SimpleNamespace(run_conversation=MagicMock(return_value={"final_response": "done"}))

    persist_message, persist_timestamp = turn._prepare_turn_message(history)
    with patch("agent.notification_presentation.notification_turn", return_value=nullcontext()):
        turn._run_conversation_with_approval(
            agent, history, None, persist_message, persist_timestamp,
        )

    assert ctx.message == ""
    args, kwargs = agent.run_conversation.call_args
    assert args == ("",)
    assert kwargs["resume_turn"] is True
    assert kwargs["turn_id"] == "sid:sid:deadbeef"
    assert "persist_user_message" not in kwargs


def test_legacy_clean_tail_falls_back_to_recovery_note():
    source = make_restart_source(chat_id="legacy")
    entry = SessionEntry(
        session_key="agent:main:telegram:dm:legacy", session_id="sid",
        created_at=datetime.now(), updated_at=datetime.now(), origin=source,
        resume_pending=True, resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
    )
    history = [{"role": "user", "content": "make a report"},
               {"role": "assistant", "content": "Report completed."}]
    turn, ctx = _turn_runner(
        entry, {"turn_id": "sid:sid:newturn", "resume_count": 1, "record_backed": False}, history,
    )

    turn._prepare_turn_message(history)

    assert ctx.turn_resume_marker is None
    assert "Report completed." not in ctx.message
    assert ctx.message.strip()
