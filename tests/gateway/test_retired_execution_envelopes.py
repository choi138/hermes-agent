"""Retired execution payloads must not fall back to unrestricted chat turns."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource


def _event(internal, payload):
    return MessageEvent(
        text="/approve",
        source=SessionSource(platform=Platform.DISCORD, chat_id="1526136893614460969", chat_type="group"),
        internal=internal,
        metadata={"mention_inbox_execution": payload},
    )


_PAYLOADS = [None, {}, "malformed", {
    "execution_id": "wx_" + "a" * 24,
    "proposal_hash": "b" * 64,
    "mode": "direct",
    "recovery_token": "old-token",
    "owner_id": "1" * 32,
}]


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_retired_envelope_rejected_before_adapter_control_or_queue(internal, payload):
    event = _event(internal, payload)
    adapter = SimpleNamespace(
        _message_handler=AsyncMock(),
        _drop_unresolved=MagicMock(return_value=True),
    )
    await BasePlatformAdapter.handle_message(adapter, event)
    assert event._gateway_accepted is False
    adapter._drop_unresolved.assert_not_called()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_retired_envelope_rejected_before_gateway_admission(internal, payload):
    event = _event(internal, payload)
    runner = SimpleNamespace(_hm_admit_event=AsyncMock(return_value=None))
    assert await GatewayInboundMixin._handle_message(runner, event) is None
    runner._hm_admit_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_retired_envelope_rejected_before_session_creation(internal, payload):
    event = _event(internal, payload)
    runner = SimpleNamespace(
        _recover_telegram_topic_thread_id=MagicMock(return_value=None),
        _cache_session_source=MagicMock(),
        _is_telegram_topic_lane=MagicMock(return_value=False),
        async_session_store=SimpleNamespace(get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(session_key="normal", session_id="normal"))),
    )
    with pytest.raises(ValueError):
        await GatewayTurnMixin._hmwa_resolve_session(runner, event, event.source)
    runner._recover_telegram_topic_thread_id.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("payload", _PAYLOADS)
@pytest.mark.parametrize("method_name", ["_run_agent_inner", "_run_agent_via_proxy"])
async def test_retired_envelope_cannot_reach_local_or_proxy_agent(method_name, internal, payload):
    event = _event(internal, payload)
    runner = SimpleNamespace(
        _get_proxy_url=MagicMock(return_value=""),
        _proxy_error_result=lambda text: {"final_response": text},
    )
    method = getattr(GatewayTurnMixin, method_name)
    with pytest.raises(ValueError, match="unsupported execution envelope"):
        await method(runner, "task", "", [], event.source, "session", event=event)
    runner._get_proxy_url.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_retired_envelope_cannot_reach_agent_handler(internal, payload):
    event = _event(internal, payload)
    runner = SimpleNamespace(_hmwa_resolve_session=AsyncMock(), _run_agent=AsyncMock())
    with pytest.raises(ValueError, match="unsupported execution envelope"):
        await GatewayTurnMixin._handle_message_with_agent(runner, event, event.source, "key", 1)
    runner._hmwa_resolve_session.assert_not_awaited()
    runner._run_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {}, {"gateway_session_key": "normal"}, {"gateway_session_strict": True},
    {"process_completion": {"process_id": "normal"}},
])
@pytest.mark.parametrize("internal", [False, True])
async def test_normal_event_metadata_preserves_gateway_admission(metadata, internal):
    event = _event(internal, None)
    event.metadata = metadata
    runner = SimpleNamespace(_hm_admit_event=AsyncMock(return_value=None))
    assert await GatewayInboundMixin._handle_message(runner, event) is None
    runner._hm_admit_event.assert_awaited_once_with(event)
