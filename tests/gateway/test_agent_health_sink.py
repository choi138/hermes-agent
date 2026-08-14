"""Delivery-boundary and logging-bridge tests for #agent-health."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.agent_health_sink as sink_module
from gateway.agent_health import HealthEvent
from gateway.agent_health_sink import (
    AgentHealthLogHandler,
    AgentHealthSink,
    install_agent_health_log_handler,
    set_active_agent_health_sink,
)
from gateway.platforms.base import SendResult


class _Gateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _iter_gateway_adapters(self):
        return [self.adapter]


def _adapter(*, send=None):
    return SimpleNamespace(
        platform="discord",
        is_connected=True,
        send=send or AsyncMock(return_value=SendResult(success=True)),
    )


def _event(
    rule="A.output_silence",
    *,
    category="A",
    mention=True,
    occurred_at=100.0,
    reason="reason",
):
    return HealthEvent(
        rule=rule,
        category=category,
        title="title",
        reason=reason,
        session_key="session-1",
        mention=mention,
        occurred_at=occurred_at,
    )


def test_log_handler_attaches_session_and_record_timestamp():
    received = []
    fake_sink = SimpleNamespace(emit=lambda event: received.append(event) or True)
    record = logging.LogRecord(
        "agent.conversation_loop",
        logging.ERROR,
        __file__,
        1,
        "API call failed after 5 retries. APIConnectionError",
        (),
        None,
    )
    record.session_tag = " [session-uuid]"
    record.created = 1234.5
    set_active_agent_health_sink(fake_sink)
    try:
        AgentHealthLogHandler().emit(record)
    finally:
        set_active_agent_health_sink(None)

    assert len(received) == 1
    assert received[0].session_id == "session-uuid"
    assert received[0].occurred_at == 1234.5


def test_logging_handler_installation_is_idempotent():
    root = logging.Logger("isolated-health-root")
    install_agent_health_log_handler(root)
    install_agent_health_log_handler(root)
    assert sum(
        isinstance(handler, AgentHealthLogHandler) for handler in root.handlers
    ) == 1


def test_c3_uses_record_occurrence_times_not_queue_drain_time():
    sink = AgentHealthSink(
        _Gateway(_adapter()),
        enabled=True,
        channel="health",
        mention="@operator",
        upstream_failure_streak=3,
    )
    samples = [
        _event("C3.stream_stale", category="C", occurred_at=0),
        _event("C3.stream_stale", category="C", occurred_at=590),
        _event("C3.stream_stale", category="C", occurred_at=601),
    ]
    assert all(sink._expand_event(event) == [] for event in samples)
    outputs = sink._expand_event(
        _event("C3.stream_stale", category="C", occurred_at=700)
    )
    assert len(outputs) == 1
    assert outputs[0].rule == "C3.upstream_failure_streak"
    assert outputs[0].occurred_at == 700


@pytest.mark.asyncio
async def test_mentions_only_appear_for_policy_marked_a_or_c_events():
    adapter = _adapter()
    sink = AgentHealthSink(
        _Gateway(adapter),
        enabled=True,
        channel="health",
        mention="<@operator>",
        cooldown_seconds=0,
    )
    await sink._send_one(_event())
    await sink._send_one(
        _event(
            "B.unexpected_agent_error",
            category="B",
            mention=False,
            occurred_at=101,
        )
    )

    first = adapter.send.await_args_list[0].args[1]
    second = adapter.send.await_args_list[1].args[1]
    assert first.startswith("<@operator>\n")
    assert "<@operator>" not in second


@pytest.mark.asyncio
async def test_emit_is_nonblocking_and_queue_drops_are_summarized():
    adapter = _adapter()
    sink = AgentHealthSink(
        _Gateway(adapter),
        enabled=True,
        channel="health",
        mention="@operator",
        cooldown_seconds=0,
        queue_size=1,
    )
    sink._loop = asyncio.get_running_loop()

    assert sink.emit(_event("A.first")) is True
    assert sink.emit(_event("A.second", occurred_at=101)) is True
    await asyncio.sleep(0)
    assert sink.queue.qsize() == 1
    assert sink._dropped == 1
    sink.queue.get_nowait()
    sink.queue.task_done()

    await sink._send_one(_event("A.third", occurred_at=102))
    assert "(외 1건 억제)" in adapter.send.await_args.args[1]


@pytest.mark.asyncio
async def test_send_timeout_is_bounded(monkeypatch):
    async def blocked_send(*args, **kwargs):
        await asyncio.Event().wait()

    sink = AgentHealthSink(
        _Gateway(_adapter(send=blocked_send)),
        enabled=True,
        channel="health",
        mention="@operator",
    )
    monkeypatch.setattr(sink_module, "_SEND_TIMEOUT_SECONDS", 0.01)
    started = asyncio.get_running_loop().time()
    await sink._send_one(_event())
    assert asyncio.get_running_loop().time() - started < 0.5


@pytest.mark.asyncio
async def test_secret_text_is_redacted_before_discord_egress():
    adapter = _adapter()
    sink = AgentHealthSink(
        _Gateway(adapter),
        enabled=True,
        channel="health",
        mention="@operator",
    )
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    await sink._send_one(
        _event(reason=f"upstream returned Authorization: Bearer {secret}")
    )

    sent = adapter.send.await_args.args[1]
    assert secret not in sent
    assert "***" in sent or "redacted" in sent.lower()


def test_redaction_failure_clears_all_operator_controlled_fields(monkeypatch):
    import agent.redact

    def fail(*args, **kwargs):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(agent.redact, "redact_sensitive_text", fail)
    event = HealthEvent(
        rule="C.test",
        category="C",
        title="secret title",
        reason="secret reason",
        action="secret action",
        session_id="secret session",
        session_key="secret key",
        platform="secret platform",
        resource="secret resource",
        jump_url="https://secret.invalid/token=x",
        details=("secret details",),
        mention=True,
    )

    redacted = AgentHealthSink._redact_event(event)

    assert redacted.title == "에이전트 헬스 이벤트"
    assert redacted.session_id == ""
    assert redacted.session_key == ""
    assert redacted.platform == ""
    assert redacted.resource == ""
    assert redacted.jump_url == ""
    assert redacted.details == ()
