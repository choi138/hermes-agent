"""Tests for durable turn records (ADR durable-turns).

Covers the SessionStore active_turn lifecycle and the gateway-side helpers
that decide which orphaned turns get same-turn resumed after a restart.
"""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import (
    _gateway_turn_resume_enabled,
    _orphaned_active_turn,
    _turn_resume_max,
)
from gateway.session import SessionSource, SessionStore


BOOT_A = "boot-aaaa"
BOOT_B = "boot-bbbb"


def _make_store(tmp_path):
    return SessionStore(sessions_dir=tmp_path, config=GatewayConfig())


def _make_source(chat_id="123"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id="u1")


def _create_session(store):
    entry = store.get_or_create_session(_make_source())
    assert entry is not None
    return entry.session_key


# ---------------------------------------------------------------------------
# SessionStore.begin/mark/finish active turn
# ---------------------------------------------------------------------------


def test_begin_active_turn_writes_running_record(tmp_path):
    store = _make_store(tmp_path)
    key = _create_session(store)
    assert store.begin_active_turn(key, "t1", BOOT_A) is True
    record = store._entries[key].active_turn
    assert record["turn_id"] == "t1"
    assert record["status"] == "running"
    assert record["boot_id"] == BOOT_A
    assert record["resume_count"] == 0


def test_begin_active_turn_missing_session(tmp_path):
    store = _make_store(tmp_path)
    assert store.begin_active_turn("nope", "t1", BOOT_A) is False


def test_finish_clears_completed_turn(tmp_path):
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_A)
    assert store.finish_active_turn(key, "t1") is True
    assert store._entries[key].active_turn is None


def test_finish_keeps_gateway_interrupted_turn(tmp_path):
    """The post-turn path may still run inside the drain grace window after
    the shutdown flagged the record — the record must survive for boot resume."""
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_A)
    store.mark_active_turn_interrupted(key, "restart_timeout")
    assert store.finish_active_turn(key, "t1", turn_interrupted=True) is False
    record = store._entries[key].active_turn
    assert record is not None
    assert record["status"] == "interrupted"
    assert record["interrupted_reason"] == "restart_timeout"


def test_finish_clears_user_interrupted_turn(tmp_path):
    """A user interrupt (/stop, steer) ends the turn WITHOUT the shutdown flag
    — the record must be cleared so nothing auto-resumes against the user's
    explicit stop."""
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_A)
    assert store.finish_active_turn(key, "t1", turn_interrupted=True) is True
    assert store._entries[key].active_turn is None


def test_finish_ignores_stale_turn_id(tmp_path):
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t2", BOOT_A)
    assert store.finish_active_turn(key, "t1") is False
    assert store._entries[key].active_turn is not None


def test_finish_force_clears_regardless(tmp_path):
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_A)
    store.mark_active_turn_interrupted(key, "restart_timeout")
    assert store.finish_active_turn(key, force=True, turn_interrupted=True) is True
    assert store._entries[key].active_turn is None


def test_active_turn_survives_store_reload(tmp_path):
    """The whole point: the record must be durable across a process death."""
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_A)
    store.mark_active_turn_interrupted(key, "restart_timeout")

    reloaded = _make_store(tmp_path)
    with reloaded._lock:
        reloaded._ensure_loaded_locked()
    record = reloaded._entries[key].active_turn
    assert record is not None
    assert record["turn_id"] == "t1"
    assert record["status"] == "interrupted"


def test_begin_with_resume_count_marks_resuming(tmp_path):
    store = _make_store(tmp_path)
    key = _create_session(store)
    store.begin_active_turn(key, "t1", BOOT_B, resume_count=1)
    record = store._entries[key].active_turn
    assert record["status"] == "resuming"
    assert record["resume_count"] == 1


# ---------------------------------------------------------------------------
# _orphaned_active_turn
# ---------------------------------------------------------------------------


class _EntryStub:
    def __init__(self, active_turn):
        self.active_turn = active_turn


def test_orphan_detection_interrupted_status():
    record = {"turn_id": "t1", "status": "interrupted", "boot_id": BOOT_A}
    assert _orphaned_active_turn(_EntryStub(record), BOOT_A) == record


def test_orphan_detection_stale_boot():
    record = {"turn_id": "t1", "status": "running", "boot_id": BOOT_A}
    assert _orphaned_active_turn(_EntryStub(record), BOOT_B) == record
    record2 = {"turn_id": "t1", "status": "resuming", "boot_id": BOOT_A}
    assert _orphaned_active_turn(_EntryStub(record2), BOOT_B) == record2


def test_orphan_detection_live_turn_is_not_orphaned():
    record = {"turn_id": "t1", "status": "running", "boot_id": BOOT_A}
    assert _orphaned_active_turn(_EntryStub(record), BOOT_A) is None


def test_orphan_detection_no_record():
    assert _orphaned_active_turn(_EntryStub(None), BOOT_A) is None
    assert _orphaned_active_turn(_EntryStub("garbage"), BOOT_A) is None


# ---------------------------------------------------------------------------
# Config knobs
# ---------------------------------------------------------------------------


def test_turn_resume_enabled_default(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_TURN_RESUME", raising=False)
    assert _gateway_turn_resume_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off"])
def test_turn_resume_kill_switch(monkeypatch, raw):
    monkeypatch.setenv("HERMES_GATEWAY_TURN_RESUME", raw)
    assert _gateway_turn_resume_enabled() is False


def test_turn_resume_enabled_config_bridge_truthy(monkeypatch):
    # config bridge writes str(True) — must parse as enabled
    monkeypatch.setenv("HERMES_GATEWAY_TURN_RESUME", "True")
    assert _gateway_turn_resume_enabled() is True


def test_turn_resume_max_default_and_override(monkeypatch):
    monkeypatch.delenv("HERMES_TURN_RESUME_MAX", raising=False)
    assert _turn_resume_max() == 2
    monkeypatch.setenv("HERMES_TURN_RESUME_MAX", "5")
    assert _turn_resume_max() == 5
    monkeypatch.setenv("HERMES_TURN_RESUME_MAX", "0")
    assert _turn_resume_max() == 0
    monkeypatch.setenv("HERMES_TURN_RESUME_MAX", "-3")
    assert _turn_resume_max() == 2
    monkeypatch.setenv("HERMES_TURN_RESUME_MAX", "garbage")
    assert _turn_resume_max() == 2


# ---------------------------------------------------------------------------
# Scheduler: same-turn resume dispatch (_schedule_resume_pending_sessions)
# ---------------------------------------------------------------------------

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


def _pending_turn_entry(
    source,
    *,
    session_key="agent:main:telegram:dm:123456",
    record=None,
    resume_pending=False,
    resume_reason=None,
):
    return SessionEntry(
        session_key=session_key,
        session_id="sid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=resume_pending,
        resume_reason=resume_reason,
        last_resume_marked_at=datetime.now() if resume_pending else None,
        active_turn=record,
    )


@pytest.mark.asyncio
async def test_orphaned_record_dispatches_same_turn_resume():
    """A durable record from a dead process schedules a resume event carrying
    the _hermes_turn_resume marker — even with resume_pending unset (SIGKILL
    never got to mark anything)."""
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    record = {
        "turn_id": "sid:sid:abc12345",
        "status": "running",
        "boot_id": "boot-dead",
        "started_at": datetime.now().isoformat(),
        "resume_count": 0,
    }
    entry = _pending_turn_entry(source, record=record)
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store.begin_active_turn = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.text == ""
    marker = getattr(event, "_hermes_turn_resume", None)
    assert marker is not None
    assert marker["turn_id"] == "sid:sid:abc12345"
    assert marker["resume_count"] == 1
    assert marker["record_backed"] is True
    # The record was refreshed for the resume attempt.
    runner.session_store.begin_active_turn.assert_called_once_with(
        entry.session_key, "sid:sid:abc12345", runner._boot_id, resume_count=1,
    )


@pytest.mark.asyncio
async def test_legacy_resume_pending_gets_unbacked_marker():
    """resume_pending without a durable record (first boot after deploy /
    unclean-boot sweep) still resumes, but the marker is not record-backed."""
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    entry = _pending_turn_entry(
        source, resume_pending=True, resume_reason="restart_interrupted",
    )
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store.begin_active_turn = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    event = adapter.handle_message.await_args.args[0]
    marker = getattr(event, "_hermes_turn_resume", None)
    assert marker is not None
    assert marker["record_backed"] is False
    assert marker["turn_id"]


@pytest.mark.asyncio
async def test_poison_turn_abandoned_at_cap(monkeypatch):
    """A record at the resume cap is abandoned with an honest notice instead
    of being replayed again."""
    monkeypatch.setenv("HERMES_TURN_RESUME_MAX", "2")
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    record = {
        "turn_id": "t-poison",
        "status": "interrupted",
        "boot_id": "boot-dead",
        "started_at": datetime.now().isoformat(),
        "interrupted_at": datetime.now().isoformat(),
        "resume_count": 2,
    }
    entry = _pending_turn_entry(source, record=record)
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store.finish_active_turn = MagicMock(return_value=True)
    runner.session_store.clear_resume_pending = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    # Let the abandonment-notice send task run.
    await asyncio.sleep(0.01)

    assert scheduled == 0
    adapter.handle_message.assert_not_called()
    runner.session_store.finish_active_turn.assert_called_once_with(
        entry.session_key, force=True,
    )
    assert any("couldn't automatically resume" in c for c in adapter.sent)


@pytest.mark.asyncio
async def test_stale_record_is_abandoned_silently():
    """A record older than the freshness window must not revive days-old work."""
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    stale = datetime.now() - timedelta(hours=3)
    record = {
        "turn_id": "t-stale",
        "status": "interrupted",
        "boot_id": "boot-dead",
        "started_at": stale.isoformat(),
        "interrupted_at": stale.isoformat(),
        "resume_count": 0,
    }
    entry = _pending_turn_entry(source, record=record)
    runner.session_store._entries = {entry.session_key: entry}
    runner.session_store.finish_active_turn = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()

    assert scheduled == 0
    adapter.handle_message.assert_not_called()
    runner.session_store.finish_active_turn.assert_called_once_with(
        entry.session_key, force=True,
    )
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_kill_switch_restores_legacy_empty_turn(monkeypatch):
    """HERMES_GATEWAY_TURN_RESUME=0: records are ignored, resume_pending
    sessions get the legacy empty event with NO resume marker."""
    monkeypatch.setenv("HERMES_GATEWAY_TURN_RESUME", "0")
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    entry = _pending_turn_entry(
        source,
        record={
            "turn_id": "t1",
            "status": "interrupted",
            "boot_id": "boot-dead",
            "started_at": datetime.now().isoformat(),
            "resume_count": 0,
        },
        resume_pending=True,
        resume_reason="restart_timeout",
    )
    runner.session_store._entries = {entry.session_key: entry}
    adapter.handle_message = AsyncMock()

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    event = adapter.handle_message.await_args.args[0]
    assert getattr(event, "_hermes_turn_resume", None) is None
