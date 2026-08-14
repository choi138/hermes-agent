"""Behavioral coverage for the gateway's output-silence health wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as run_module
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _health_runner(*, generation: int = 7, started_at: float = 100.0):
    runner = GatewayRunner.__new__(GatewayRunner)
    state = SimpleNamespace(
        persistent=SimpleNamespace(run_generation=generation),
        turn=SimpleNamespace(agent=object(), started_ts=started_at),
    )
    source = SimpleNamespace(chat_id="thread-1")
    events = []
    runner._running_agents = {"session-1": state.turn.agent}
    runner._turn_started_at = {"session-1": started_at}
    runner._last_content_sent_at = {}
    runner._output_silence_notified = {}
    runner._turn_deadline_enforced = {}
    runner._output_silence_user_waiting = {}
    runner._session_waiting_on_user = lambda _key: False
    runner._peek_session_state = lambda _key: state
    runner._is_session_run_current = (
        lambda _key, candidate: candidate == state.persistent.run_generation
    )
    runner._get_cached_session_source = lambda _key: source
    runner._agent_health_event_context = lambda _key, _source: {
        "session_id": "sid-1",
        "session_key": "session-1",
        "platform": "discord",
        "jump_url": "https://discord.com/channels/guild/thread-1/message-1",
    }

    def emit(event):
        events.append(event)
        return True

    runner._emit_agent_health_event = emit
    runner._adapter_for_source = lambda _source: None
    return runner, state, source, events


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_kind", ["clarify", "approval"])
async def test_explicit_user_wait_suppresses_warning_and_deadline(
    monkeypatch,
    wait_kind,
):
    runner, _state, _source, events = _health_runner()
    monkeypatch.setattr(run_module.time, "time", lambda: 1600.0)

    if wait_kind == "clarify":
        monkeypatch.setattr(
            "tools.clarify_gateway.get_pending_for_session",
            lambda session_key, include_choice_prompts=False: (
                object()
                if session_key == "session-1" and include_choice_prompts
                else None
            ),
        )
        monkeypatch.setattr(
            "tools.approval.has_blocking_approval", lambda _key: False
        )
    else:
        monkeypatch.setattr(
            "tools.clarify_gateway.get_pending_for_session",
            lambda _key, include_choice_prompts=False: None,
        )
        monkeypatch.setattr(
            "tools.approval.has_blocking_approval",
            lambda key: key == "session-1",
        )
    del runner.__dict__["_session_waiting_on_user"]
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner._check_output_silence(600, 1500) == 0
    assert events == []
    runner._interrupt_and_clear_session.assert_not_awaited()
    assert runner._output_silence_user_waiting[("session-1", 7)] is True


@pytest.mark.asyncio
async def test_user_wait_end_restarts_clock_before_alerting(monkeypatch):
    runner, _state, _source, events = _health_runner()
    now = [1600.0]
    waiting = [True]
    monkeypatch.setattr(run_module.time, "time", lambda: now[0])
    runner._session_waiting_on_user = lambda _key: waiting[0]
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner._check_output_silence(600, 1500) == 0
    waiting[0] = False
    now[0] = 1700.0
    assert await runner._check_output_silence(600, 1500) == 0
    assert runner._last_content_sent_at["session-1"] == 1700.0
    assert ("session-1", 7) not in runner._output_silence_user_waiting

    now[0] = 2299.0
    assert await runner._check_output_silence(600, 1500) == 0
    now[0] = 2300.0
    assert await runner._check_output_silence(600, 1500) == 1
    assert [event.rule for event in events] == ["A.output_silence"]
    runner._interrupt_and_clear_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_user_wait_recheck_cancels_due_interrupt(monkeypatch):
    runner, _state, _source, events = _health_runner()
    monkeypatch.setattr(run_module.time, "time", lambda: 1600.0)
    observations = iter([False, True])
    runner._session_waiting_on_user = lambda _key: next(observations)
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner._check_output_silence(600, 1500) == 0
    assert events == []
    runner._interrupt_and_clear_session.assert_not_awaited()
    assert runner._output_silence_user_waiting[("session-1", 7)] is True


@pytest.mark.asyncio
async def test_long_tool_work_without_user_wait_still_warns(monkeypatch):
    runner, _state, _source, events = _health_runner()
    monkeypatch.setattr(run_module.time, "time", lambda: 700.0)
    runner._session_waiting_on_user = lambda _key: False
    runner._interrupt_and_clear_session = AsyncMock()

    assert await runner._check_output_silence(600, 1500) == 1
    assert [event.rule for event in events] == ["A.output_silence"]
    runner._interrupt_and_clear_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_warning_needs_no_pending_inbound_or_activity_summary(monkeypatch):
    runner, _state, _source, events = _health_runner()
    monkeypatch.setattr(run_module.time, "time", lambda: 700.0)

    emitted = await runner._check_output_silence(600, 0)

    assert emitted == 1
    assert [event.rule for event in events] == ["A.output_silence"]
    assert runner._output_silence_notified[("session-1", 7)] is True
    assert "pending" not in runner.__dict__
    assert not hasattr(runner._running_agents["session-1"], "get_activity_summary")
    assert "비활성화" in events[0].action


@pytest.mark.asyncio
async def test_confirmed_content_clears_and_rearms_warning(monkeypatch):
    runner, _state, _source, events = _health_runner()
    now = [700.0]
    monkeypatch.setattr(run_module.time, "time", lambda: now[0])

    assert await runner._check_output_silence(600, 0) == 1
    now[0] = 701.0
    runner._output_silence_user_waiting[("session-1", 7)] = True
    runner._record_content_delivered("session-1", 7)
    assert ("session-1", 7) not in runner._output_silence_notified
    assert ("session-1", 7) not in runner._output_silence_user_waiting

    now[0] = 1301.0
    assert await runner._check_output_silence(600, 0) == 1
    assert [event.rule for event in events] == [
        "A.output_silence",
        "A.output_silence",
    ]


@pytest.mark.asyncio
async def test_generation_replacement_rearms_and_stale_delivery_is_ignored(monkeypatch):
    runner, state, _source, events = _health_runner()
    now = [700.0]
    monkeypatch.setattr(run_module.time, "time", lambda: now[0])

    assert await runner._check_output_silence(600, 0) == 1
    state.persistent.run_generation = 8
    runner._turn_started_at["session-1"] = 800.0
    runner._last_content_sent_at["session-1"] = 850.0
    runner._record_content_delivered("session-1", 7)
    assert runner._last_content_sent_at["session-1"] == 850.0

    now[0] = 1450.0
    assert await runner._check_output_silence(600, 0) == 1
    assert ("session-1", 8) in runner._output_silence_notified
    assert len(events) == 2


@pytest.mark.asyncio
async def test_deadline_interrupts_and_health_summary_survives_source_timeout(
    monkeypatch,
):
    runner, _state, source, events = _health_runner()
    monkeypatch.setattr(run_module.time, "time", lambda: 1600.0)
    monkeypatch.setattr(run_module, "_STALL_NOTIFY_SEND_TIMEOUT_SECONDS", 0.01)
    interrupted = []

    async def interrupt(*args, **kwargs):
        interrupted.append((args, kwargs))

    async def never_returns(*args, **kwargs):
        await asyncio.Event().wait()

    adapter = SimpleNamespace(send=never_returns)
    runner._interrupt_and_clear_session = interrupt
    runner._adapter_for_source = lambda candidate: (
        adapter if candidate is source else None
    )
    runner._thread_metadata_for_source = lambda _source: {"thread_id": "thread-1"}

    started = asyncio.get_running_loop().time()
    emitted = await runner._check_output_silence(600, 1500)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5
    assert emitted == 1
    assert len(interrupted) == 1
    assert interrupted[0][1]["invalidation_reason"] == "agent_health_turn_deadline"
    assert events[-1].rule == "A.turn_deadline"
    assert "시간 초과" in events[-1].action


def test_restart_health_falls_back_to_latest_rss_memory_line(
    tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "gateway.log").write_text(
        "[MEMORY] rss=111MB gc=1 threads=2 uptime=3s\n"
        "[MEMORY] Periodic memory monitoring stopped\n"
        "[MEMORY] shutdown rss=321MB gc=2 threads=2 uptime=9s\n",
        encoding="utf-8",
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_health_previous_status = {
        "gateway_state": "running",
        "exit_reason": "SIGTERM",
    }
    runner._agent_health_lifecycle_evidence = {}
    events = []
    runner._emit_agent_health_event = lambda event: events.append(event) or True
    monkeypatch.setattr(run_module, "_hermes_home", tmp_path)

    assert runner._emit_gateway_restart_health() is True
    assert len(events) == 1
    assert "직전 RSS=321.0 MiB" in events[0].reason
    assert "rss=321MB" in "\n".join(events[0].details)


def test_restart_health_uses_pre_start_snapshot_not_current_process_log(
    tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "gateway.log").write_text(
        "[MEMORY] baseline rss=999MB gc=1 threads=2 uptime=1s\n",
        encoding="utf-8",
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_health_previous_status = {}
    runner._agent_health_lifecycle_evidence = {}
    runner._agent_health_previous_exit_diag = ""
    runner._agent_health_previous_memory_line = (
        "[MEMORY] shutdown rss=321MB gc=2 threads=2 uptime=9s"
    )
    events = []
    runner._emit_agent_health_event = lambda event: events.append(event) or True
    monkeypatch.setattr(run_module, "_hermes_home", tmp_path)

    assert runner._emit_gateway_restart_health() is True
    assert "직전 RSS=321.0 MiB" in events[0].reason
    assert "rss=999MB" not in "\n".join(events[0].details)


def test_previous_exit_diag_snapshot_skips_current_process_start(tmp_path):
    path = tmp_path / "gateway-exit-diag.log"
    path.write_text(
        '{"tag":"gateway.exit_nonzero","pid":111}\n'
        '{"tag":"gateway.start","pid":222}\n',
        encoding="utf-8",
    )

    line = GatewayRunner._agent_health_previous_exit_diag_line(
        path,
        previous_pid=111,
        current_pid=222,
    )

    assert '"gateway.exit_nonzero"' in line
    assert '"pid":222' not in line


def test_restart_health_uses_previous_heartbeat_rss(monkeypatch, tmp_path):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_health_previous_status = {
        "gateway_state": "running",
        "pid": 111,
    }
    runner._agent_health_lifecycle_evidence = {}
    runner._agent_health_previous_heartbeat = {
        "pid": 111,
        "updated_at": "2026-08-10T11:57:00+00:00",
        "mem": {"rss_kib": 429684, "mem_available_kib": 1000000},
    }
    runner._agent_health_previous_exit_diag = (
        '{"tag":"gateway.exit_nonzero","pid":111}'
    )
    runner._agent_health_previous_memory_line = (
        "memory trim: rss_kib=435736->429684"
    )
    events = []
    runner._emit_agent_health_event = lambda event: events.append(event) or True
    monkeypatch.setattr(run_module, "_hermes_home", tmp_path)

    assert runner._emit_gateway_restart_health() is True
    assert "exit_reason=gateway.exit_nonzero" in events[0].reason
    assert "직전 RSS=419.6 MiB" in events[0].reason
    assert "직전 heartbeat:" in "\n".join(events[0].details)


def test_platform_health_emits_once_per_transition_and_recovery():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_health_platform_states = {}
    events = []
    runner._emit_agent_health_event = lambda event: events.append(event) or True

    with patch("gateway.status.write_runtime_status"):
        runner._update_platform_runtime_status(
            "discord",
            platform_state="retrying",
            error_message="gateway disconnected",
        )
        runner._update_platform_runtime_status(
            "discord",
            platform_state="retrying",
            error_message="same episode",
        )
        runner._update_platform_runtime_status(
            "discord", platform_state="connected"
        )

    assert [event.rule for event in events] == [
        "C2.platform_degraded",
        "C2.platform_recovered",
    ]
    assert all(event.mention for event in events)


class _FinalAdapter(BasePlatformAdapter):  # type: ignore[misc]
    def __init__(self, *, success: bool):
        super().__init__(
            PlatformConfig(enabled=True, typing_indicator=False),
            Platform.SLACK,
        )
        self.success = success

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(
            success=self.success,
            message_id="message-1" if self.success else None,
            error=None if self.success else "send failed",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_final_adapter_callback_requires_successful_send_result(success):
    adapter = _FinalAdapter(success=success)
    callbacks = []
    adapter.set_content_delivered_handler(
        lambda session_key, generation: callbacks.append(
            (session_key, generation)
        )
    )
    adapter.set_message_handler(lambda _event: asyncio.sleep(0, result="answer"))
    event = MessageEvent(
        text="question",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="channel-1",
            chat_type="channel",
        ),
        message_id="inbound-1",
    )
    session_key = "agent:main:slack:channel:channel-1"
    with patch("gateway.delivery_ledger.ledger_enabled", return_value=False):
        await adapter._process_message_background(event, session_key)

    assert bool(callbacks) is success


@pytest.mark.asyncio
async def test_stream_send_edit_and_draft_callbacks_only_follow_success():
    adapter = SimpleNamespace(
        send=AsyncMock(
            side_effect=[
                SendResult(success=False, error="send failed"),
                SendResult(success=True, message_id="message-1"),
            ]
        ),
        edit_message=AsyncMock(
            side_effect=[
                SendResult(success=False, error="edit failed"),
                SendResult(success=True, message_id="message-1"),
            ]
        ),
        send_draft=AsyncMock(
            side_effect=[
                SendResult(success=False, error="draft failed"),
                SendResult(success=True),
            ]
        ),
        MAX_MESSAGE_LENGTH=4096,
    )
    delivered = []
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        StreamConsumerConfig(cursor=""),
        on_content_delivered=lambda: delivered.append(True),
    )

    assert await consumer._send_or_edit("first") is False
    assert delivered == []
    consumer._message_id = None
    consumer._edit_supported = True
    assert await consumer._send_or_edit("second") is True
    assert len(delivered) == 1

    consumer._message_id = "message-1"
    assert (await consumer._edit_message(
        message_id="message-1", content="edit one"
    )).success is False
    assert len(delivered) == 1
    assert (await consumer._edit_message(
        message_id="message-1", content="edit two"
    )).success is True
    assert len(delivered) == 2

    consumer._draft_id = 1
    consumer._use_draft_streaming = True
    assert await consumer._send_draft_frame("draft one") is False
    assert len(delivered) == 2
    consumer._use_draft_streaming = True
    assert await consumer._send_draft_frame("draft two") is True
    assert len(delivered) == 3
