"""Regression coverage for /stop and background async delegations.

The gateway owns two independent execution lanes for one Discord session:
the foreground AIAgent and detached ``delegate_task`` workers.  /stop must
cancel both lanes, suppress an already-queued completion, and clear any
restart auto-resume marker without affecting another thread.
"""

import asyncio
import queue
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    Platform,
    _COMPLETION_ADMISSION_REJECTED_KEY,
    _COMPLETION_ADMISSION_TOKEN_KEY,
)
from gateway.run import GatewayRunner, _INTERRUPT_REASON_STOP
from gateway.session import SessionSource, build_session_key
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_async_delegations():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


class _SessionEntry:
    def __init__(self, session_key, session_id, *, resume_pending=True):
        self.session_key = session_key
        self.session_id = session_id
        self.resume_pending = resume_pending


class _SessionStore:
    def __init__(self, entry):
        self.entry = entry
        self.clear_calls = []

    def get_or_create_session(self, _source):
        return self.entry

    def clear_resume_pending(self, session_key):
        self.clear_calls.append(session_key)
        changed = self.entry.resume_pending
        self.entry.resume_pending = False
        return changed


class _AdmissionAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, **kwargs):
        return None

    async def get_chat_info(self, chat_id):
        return {}


def _source(thread_id):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_type="forum",
        chat_id="channel",
        thread_id=thread_id,
        user_id="user",
    )


def _wait_for_inactive(timeout=5):
    deadline = time.monotonic() + timeout
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)


def _take_completion(delegation_id):
    while not process_registry.completion_queue.empty():
        candidate = process_registry.completion_queue.get_nowait()
        if candidate.get("delegation_id") == delegation_id:
            return candidate
    return None


def _delivery_runner(*, mock_injection=True):
    runner = object.__new__(GatewayRunner)
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 16
    if mock_injection:
        runner._inject_watch_notification = AsyncMock(return_value=True)
    session_db = MagicMock()
    session_db.get_session = AsyncMock(
        return_value={"id": "parent-a", "ended_at": None}
    )
    runner._session_db = session_db
    return runner


def _admission_adapter(runner):
    adapter = _AdmissionAdapter(
        PlatformConfig(enabled=True, token="test-token"),
        Platform.DISCORD,
    )
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    adapter.set_completion_admission_validator(
        runner._validate_completion_admission
    )
    return adapter, handler


def _tokenized_completion(runner, source):
    session_key = build_session_key(source)
    return MessageEvent(
        text="[SYSTEM: background delegation completed]",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        metadata={
            _COMPLETION_ADMISSION_TOKEN_KEY: (
                runner._capture_completion_admission(session_key)
            )
        },
    )


@pytest.mark.asyncio
async def test_interrupt_and_clear_session_cancels_foreground_and_background(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = _source("thread-a")
    session_key = build_session_key(source)
    entry = _SessionEntry(session_key, "parent-a")
    store = _SessionStore(entry)

    gate = threading.Event()
    child_interrupted = MagicMock(side_effect=gate.set)

    def background_runner():
        gate.wait(timeout=60)
        return {"status": "interrupted", "summary": None}

    dispatched = ad.dispatch_async_delegation(
        goal="background work",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key=session_key,
        parent_session_id=entry.session_id,
        runner=background_runner,
        interrupt_fn=child_interrupted,
    )

    runner = object.__new__(GatewayRunner)
    foreground = MagicMock()
    foreground.session_id = entry.session_id
    runner._running_agents = {session_key: foreground}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner.session_store = store
    runner._adapter_for_source = lambda _source: None
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(
        key, None
    )
    runner._evict_cached_agent = lambda _key: None

    await runner._interrupt_and_clear_session(
        session_key,
        source,
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
    )

    foreground.interrupt.assert_called_once_with(_INTERRUPT_REASON_STOP)
    child_interrupted.assert_called_once()
    assert store.clear_calls == [session_key]
    assert entry.resume_pending is False
    assert session_key not in runner._running_agents

    _wait_for_inactive()
    assert ad.active_count() == 0
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable["state"] == "cancelled"
    assert durable["delivery_state"] == "suppressed"


@pytest.mark.asyncio
async def test_stop_with_only_background_work_is_scoped_and_idempotent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source_a = _source("thread-a")
    source_b = _source("thread-b")
    key_a = build_session_key(source_a)
    key_b = build_session_key(source_b)
    entry = _SessionEntry(key_a, "parent-a")
    store = _SessionStore(entry)

    gate_a = threading.Event()
    gate_b = threading.Event()
    interrupted_a = MagicMock(side_effect=gate_a.set)
    interrupted_b = MagicMock(side_effect=gate_b.set)

    def runner_a():
        gate_a.wait(timeout=60)
        return {"status": "interrupted", "summary": None}

    def runner_b():
        gate_b.wait(timeout=60)
        return {"status": "completed", "summary": "other"}

    mine = ad.dispatch_async_delegation(
        goal="mine",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key=key_a,
        parent_session_id="parent-a",
        runner=runner_a,
        interrupt_fn=interrupted_a,
    )
    other = ad.dispatch_async_delegation(
        goal="other",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key=key_b,
        parent_session_id="parent-b",
        runner=runner_b,
        interrupt_fn=interrupted_b,
    )

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.session_store = store
    runner.adapters = {}
    runner._is_user_authorized = lambda _source: True

    event = MessageEvent(
        text="/stop",
        message_type=MessageType.TEXT,
        source=source_a,
    )
    try:
        first = await runner._handle_stop_command(event)
        assert "no active" not in str(getattr(first, "text", first)).lower()
        interrupted_a.assert_called_once()
        interrupted_b.assert_not_called()
        assert entry.resume_pending is False

        second = await runner._handle_stop_command(event)
        assert "no active" in str(getattr(second, "text", second)).lower()
        interrupted_a.assert_called_once()

        mine_row = ad.get_durable_delegation(mine["delegation_id"])
        other_row = ad.get_durable_delegation(other["delegation_id"])
        assert mine_row["state"] == "cancelled"
        assert mine_row["delivery_state"] == "suppressed"
        assert other_row["state"] == "running"
        assert other_row["delivery_state"] == "pending"
    finally:
        gate_b.set()


@pytest.mark.asyncio
async def test_gateway_drops_completion_suppressed_by_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="race",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="discord-thread-a",
        parent_session_id="parent-a",
        runner=lambda: {"status": "completed", "summary": "must not re-enter"},
    )
    _wait_for_inactive()
    assert ad.active_count() == 0
    assert ad.interrupt_for_session(
        session_key="discord-thread-a",
        parent_session_id="parent-a",
        reason="stop_command",
    ) == 1

    evt = _take_completion(dispatched["delegation_id"])
    assert evt is not None

    runner = object.__new__(GatewayRunner)
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 16
    runner._inject_watch_notification = AsyncMock(return_value=True)

    assert await runner._deliver_completion_notification("synthetic", evt) is None
    runner._inject_watch_notification.assert_not_awaited()
    assert ad.restore_undelivered_completions(queue.Queue()) == 0


@pytest.mark.asyncio
async def test_gateway_revalidates_claim_when_stop_wins_delivery_race(
    tmp_path, monkeypatch
):
    """A stop arriving just after claim must still block synthetic injection."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="claim race",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="discord-thread-a",
        parent_session_id="parent-a",
        runner=lambda: {"status": "completed", "summary": "must not re-enter"},
    )
    _wait_for_inactive()
    assert ad.active_count() == 0

    evt = _take_completion(dispatched["delegation_id"])
    assert evt is not None

    real_claim = ad.claim_completion_delivery

    def claim_then_stop(delegation_id, claim_id):
        assert real_claim(delegation_id, claim_id) is True
        assert ad.interrupt_for_session(
            session_key="discord-thread-a",
            parent_session_id="parent-a",
            reason="stop_command",
        ) == 1
        return True

    monkeypatch.setattr(ad, "claim_completion_delivery", claim_then_stop)

    runner = object.__new__(GatewayRunner)
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 16
    runner._inject_watch_notification = AsyncMock(return_value=True)

    assert await runner._deliver_completion_notification("synthetic", evt) is None
    runner._inject_watch_notification.assert_not_awaited()
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable["state"] == "cancelled"
    assert durable["delivery_state"] == "suppressed"


@pytest.mark.asyncio
async def test_stop_after_claim_revalidation_blocks_adapter_admission(
    tmp_path, monkeypatch
):
    """A stop during topic recovery wins after the final durable-claim read."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = _source("thread-a")
    session_key = build_session_key(source)
    dispatched = ad.dispatch_async_delegation(
        goal="post-claim race",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key=session_key,
        parent_session_id="parent-a",
        runner=lambda: {"status": "completed", "summary": "must not re-enter"},
    )
    _wait_for_inactive()
    evt = _take_completion(dispatched["delegation_id"])
    assert evt is not None

    runner = _delivery_runner(mock_injection=False)
    adapter, handler = _admission_adapter(runner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries={session_key: SimpleNamespace(origin=source)},
    )
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        clear_resume_pending=AsyncMock(return_value=False),
    )

    recovery_entered = threading.Event()
    release_recovery = threading.Event()

    def blocking_topic_recovery(_source):
        recovery_entered.set()
        release_recovery.wait(timeout=5)
        return None

    adapter.set_topic_recovery_fn(blocking_topic_recovery)
    delivery_task = asyncio.create_task(
        runner._deliver_completion_notification("synthetic", evt)
    )

    entered = await asyncio.wait_for(
        asyncio.to_thread(recovery_entered.wait, 3),
        timeout=4,
    )
    cancelled = -1
    if entered:
        try:
            cancelled = await runner._cancel_session_background_work(
                session_key,
                parent_session_id="parent-a",
                reason="stop_command",
            )
        finally:
            release_recovery.set()
    else:
        release_recovery.set()

    result = await asyncio.wait_for(delivery_task, timeout=5)
    await adapter.cancel_background_tasks()

    assert entered is True
    assert cancelled == 1
    assert result is None
    handler.assert_not_awaited()
    assert session_key not in adapter._active_sessions
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable["state"] == "cancelled"
    assert durable["delivery_state"] == "suppressed"


@pytest.mark.asyncio
async def test_stale_queued_completion_is_not_drained_after_stop_epoch():
    runner = object.__new__(GatewayRunner)
    source = _source("thread-a")
    session_key = build_session_key(source)
    adapter, handler = _admission_adapter(runner)
    command_guard = asyncio.Event()
    adapter._active_sessions[session_key] = command_guard
    event = _tokenized_completion(runner, source)

    await adapter.handle_message(event)
    assert adapter._pending_messages[session_key] is event

    runner._invalidate_completion_admission_epoch(
        session_key,
        reason="stop_command",
    )
    await adapter._drain_pending_after_session_command(
        session_key,
        command_guard,
    )
    await asyncio.sleep(0)

    handler.assert_not_awaited()
    assert event.metadata[_COMPLETION_ADMISSION_REJECTED_KEY] is True
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_current_queued_completion_drains_normally():
    runner = object.__new__(GatewayRunner)
    source = _source("thread-a")
    session_key = build_session_key(source)
    adapter, handler = _admission_adapter(runner)
    processed = asyncio.Event()

    async def mark_processed(_event):
        processed.set()

    handler.side_effect = mark_processed
    command_guard = asyncio.Event()
    adapter._active_sessions[session_key] = command_guard
    event = _tokenized_completion(runner, source)

    await adapter.handle_message(event)
    assert adapter._pending_messages[session_key] is event

    await adapter._drain_pending_after_session_command(
        session_key,
        command_guard,
    )
    await asyncio.wait_for(processed.wait(), timeout=2)
    await adapter.cancel_background_tasks()

    handler.assert_awaited_once_with(event)
    assert _COMPLETION_ADMISSION_REJECTED_KEY not in event.metadata
