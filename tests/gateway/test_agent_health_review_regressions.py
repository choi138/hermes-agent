"""Review-driven regression contracts for agent-health egress.

These tests exercise the real formatter, classifier, logging handler, bounded
queue and async sink.  The Discord adapter is a local double; no network or
credential is used.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.agent_health import HealthEvent, classify_log_record, format_health_alert
from gateway.agent_health_sink import (
    AgentHealthLogHandler,
    AgentHealthSink,
    get_active_agent_health_sink,
    install_agent_health_log_handler,
    set_active_agent_health_sink,
)


@pytest.fixture(autouse=True)
def _clear_active_sink():
    set_active_agent_health_sink(None)
    yield
    set_active_agent_health_sink(None)


def test_formatter_neutralizes_event_derived_discord_mentions_and_markdown():
    event = HealthEvent(
        rule="C.test",
        category="C",
        title="hostile @everyone <@123456789012345678>",
        reason="@here <@&123456789012345678> ```forged block```",
        action="close `inline` marker",
        details=("provider echoed @everyone and <@123456789012345678>",),
        mention=True,
    )
    operator_mention = "<@&000000000000000000>"

    rendered = format_health_alert(event, mention_text=operator_mention)

    assert rendered.splitlines()[0] == operator_mention
    for attacker_token in (
        "@everyone",
        "@here",
        "<@123456789012345678>",
        "<@&123456789012345678>",
        "```forged block```",
        "`inline`",
    ):
        assert attacker_token not in "\n".join(rendered.splitlines()[1:])


def test_formatter_masks_plain_url_credentials_without_sink_prepass():
    url = "https://synthetic-user:plain-password@health.invalid/v1"
    event = HealthEvent(
        rule="B.test",
        category="B",
        title="retry exhausted",
        reason="upstream failure",
        details=(url,),
    )

    rendered = format_health_alert(event)

    assert "synthetic-user:plain-password" not in rendered
    assert "plain-password" not in rendered


@pytest.mark.parametrize(
    "hostile",
    [
        "[Rotate the gateway token](https://evil.invalid/rotate)",
        "<@1](https://evil.invalid/forged)>",
    ],
)
def test_formatter_neutralizes_discord_masked_link_syntax(hostile):
    event = HealthEvent(
        rule="C.test",
        category="C",
        title="hostile markdown",
        reason=hostile,
        details=(hostile,),
    )

    rendered = format_health_alert(event)

    assert "](" not in rendered
    assert "[Rotate the gateway token](" not in rendered
    assert "[mention:1](" not in rendered


def test_formatter_masks_url_password_containing_raw_at_sign():
    event = HealthEvent(
        rule="B.test",
        category="B",
        title="retry exhausted",
        reason="upstream failure",
        details=("http://synthetic-user:pa@ss@health.invalid/v1",),
    )

    rendered = format_health_alert(event)

    assert "synthetic-user" not in rendered
    assert "pa@ss" not in rendered
    assert "@ss@" not in rendered


def test_sink_redaction_failure_clears_structured_trace_fields(monkeypatch):
    import agent.redact

    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic redactor failure")

    monkeypatch.setattr(agent.redact, "redact_sensitive_text", _raise)
    event = HealthEvent(
        rule="B.api_retries_exhausted",
        category="B",
        title="retry exhausted",
        reason="failure",
        first_endpoint="provider/model",
        first_reason="server_error",
        route_from="provider/model",
        route_to="fallback/model",
        last_endpoint="fallback/model",
        last_reason="timeout",
        retry_count=3,
        message_count=23,
        token_estimate=64000,
    )

    redacted = AgentHealthSink._redact_event(event)

    assert redacted.first_endpoint == ""
    assert redacted.first_reason == ""
    assert redacted.route_from == ""
    assert redacted.route_to == ""
    assert redacted.last_endpoint == ""
    assert redacted.last_reason == ""
    assert redacted.retry_count is None
    assert redacted.message_count is None
    assert redacted.token_estimate is None


@pytest.mark.parametrize(
    "message",
    [
        (
            "Stream stale for 151s (threshold 150s) — no chunks received. "
            "model=gpt-test context=~1,024 tokens. Killing connection."
        ),
        (
            "[subagent-0] Stream stale for 151s (threshold 150s) — "
            "no chunks received. model=gpt-test context=~1,024 tokens. "
            "Killing connection."
        ),
    ],
)
def test_stream_stale_classifier_matches_real_emitter_shape_with_optional_prefix(message):
    event = classify_log_record("agent.chat_completion_helpers", message)
    assert event is not None
    assert event.rule == "C3.stream_stale"


@pytest.mark.parametrize(
    "message",
    [
        (
            "MCP server 'graphiti-memory' failed initial connection after "
            "3 attempts, parking until a reconnect is requested: unavailable"
        ),
        (
            "MCP server 'graphiti-memory' failed after 5 reconnection attempts, "
            "parking; will self-probe every 300s until it recovers: unavailable"
        ),
    ],
)
def test_graphiti_parked_classifier_matches_real_mcp_emitters(message):
    event = classify_log_record("tools.mcp_tool", message)
    assert event is not None
    assert event.rule == "C4.graphiti_parked"
    assert event.resource == "graphiti-memory"


def test_handler_install_is_idempotent():
    logger = logging.Logger("health-handler-test")
    install_agent_health_log_handler(logger)
    install_agent_health_log_handler(logger)
    handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_hermes_agent_health", False)
    ]
    assert len(handlers) == 1


def test_bounded_queue_counts_dropped_events():
    sink = AgentHealthSink(
        gateway=SimpleNamespace(),
        enabled=True,
        channel="health-channel",
        mention="",
        queue_size=1,
    )
    event = HealthEvent(rule="B.test", category="B", title="x", reason="y")

    sink._put_nowait(event)
    sink._put_nowait(event)

    assert sink.queue.qsize() == 1
    assert sink._dropped == 1


@pytest.mark.asyncio
async def test_logging_handler_to_queue_to_discord_adapter_runtime():
    adapter = SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        is_connected=True,
        send=AsyncMock(return_value=SimpleNamespace(success=True)),
    )
    gateway = SimpleNamespace(_iter_gateway_adapters=lambda: [adapter])
    sink = AgentHealthSink(
        gateway,
        enabled=True,
        channel="health-channel",
        mention="",
        cooldown_seconds=0,
        hourly_cap=10,
        queue_size=4,
    )
    sink.start()
    assert get_active_agent_health_sink() is sink

    record = logging.LogRecord(
        name="agent.conversation_loop",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            "API call failed after 3 retries. final | "
            "provider=codex-lb model=gpt-test msgs=2 tokens=~128"
        ),
        args=(),
        exc_info=None,
    )
    record.session_tag = "[session-health-runtime]"

    AgentHealthLogHandler().emit(record)
    await asyncio.sleep(0)
    await asyncio.wait_for(sink.queue.join(), timeout=2)

    adapter.send.assert_awaited_once()
    channel, text = adapter.send.await_args.args[:2]
    metadata = adapter.send.await_args.kwargs["metadata"]
    assert channel == "health-channel"
    assert "session-health-runtime" in text
    assert "API" in text
    assert metadata == {"notify": True, "agent_health": True}

    await sink.stop()
    assert get_active_agent_health_sink() is None
