"""Tests for the pre_gateway_dispatch plugin hook.

The hook allows plugins to intercept incoming messages before auth and
agent dispatch. It runs in _handle_message and acts on returned action
dicts: {"action": "skip"|"rewrite"|"allow"}.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(text: str = "hello", platform: Platform = Platform.WHATSAPP) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id="15551234567@s.whatsapp.net",
            chat_id="15551234567@s.whatsapp.net",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner(platform: Platform):
    from gateway.run import GatewayRunner

    config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True)},
    )
    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {platform: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._external_drain_active = False
    runner._turn_started_at = {}
    runner._claim_active_session_slot = lambda *_args, **_kwargs: (None, None)
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._cache_session_source = lambda *_args, **_kwargs: None
    runner._restore_moa_one_shot = lambda *_args, **_kwargs: None
    runner._restore_pending_one_turn_model_override = lambda *_args, **_kwargs: None
    runner._release_running_agent_state = lambda *_args, **_kwargs: True
    runner._release_turn_lease = lambda *_args, **_kwargs: None
    return runner, adapter


@pytest.mark.asyncio
async def test_internal_events_bypass_hook(monkeypatch):
    """Internal events (event.internal=True) skip the plugin hook entirely."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    called = {"count": 0}

    def _fake_hook(name, **kwargs):
        called["count"] += 1
        return [{"action": "skip"}]

    async def _capture(event, source, _quick_key, _run_generation):
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    event = _make_event("hi")
    event.internal = True

    # Even though the hook would say skip, internal events bypass it.
    await runner._handle_message(event)
    assert called["count"] == 0

@pytest.mark.asyncio
async def test_hook_fires_without_session_store_attribute(monkeypatch):
    """A runner missing session_store still delivers the event to plugins.

    Regression: the hook kwargs read ``self.session_store`` directly, so a
    partially-initialized runner raised AttributeError inside the dispatch
    try-block — the hook never fired, and every message logged
    "pre_gateway_dispatch invocation failed: 'GatewayRunner' object has no
    attribute 'session_store'". Plugins must receive the event (with
    session_store=None) instead.
    """
    _clear_auth_env(monkeypatch)

    seen = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            seen["session_store"] = kwargs.get("session_store", "MISSING")
            return [{"action": "skip", "reason": "plugin-handled"}]
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, adapter = _make_runner(Platform.WHATSAPP)
    del runner.session_store

    result = await runner._handle_message(_make_event("hi"))
    assert result is None
    # Hook actually fired (skip short-circuited before auth) with a None store.
    assert seen == {"session_store": None}
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_hook_prepends_accumulate_before_dispatch(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [
                {"action": "prepend", "text": "First context."},
                {"action": "prepend", "text": "Second context."},
            ]
        return []

    captured = {}

    async def _capture(event, source, _quick_key, _run_generation):
        captured["text"] = event.text
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)
    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture

    await runner._handle_message(_make_event("original"))

    assert captured["text"] == "First context.\n\nSecond context.\n\noriginal"


@pytest.mark.asyncio
async def test_runtime_override_applies_after_auth_before_agent(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    directive = {
        "action": "runtime_override",
        "model": "gpt-5.5",
        "provider": "codex-nekos",
    }

    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: [directive] if name == "pre_gateway_dispatch" else [],
    )
    order = []

    def _apply(observed, source):
        order.append(("override", observed, source))
        return True

    async def _capture(event, source, _quick_key, _run_generation):
        order.append(("agent", event.text, source))
        return "ok"

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._apply_gateway_runtime_override = _apply
    runner._handle_message_with_agent = _capture

    await runner._handle_message(_make_event("review this"))

    assert [item[0] for item in order] == ["override", "agent"]
    assert order[0][1] is directive


@pytest.mark.asyncio
async def test_runtime_override_never_applies_for_unauthorized_sender(monkeypatch):
    _clear_auth_env(monkeypatch)
    directive = {"action": "runtime_override", "model": "gpt-5.5"}
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda name, **kwargs: [directive] if name == "pre_gateway_dispatch" else [],
    )

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner.pairing_store.generate_code.return_value = None
    runner._apply_gateway_runtime_override = MagicMock()

    assert await runner._handle_message(_make_event("review this")) is None
    runner._apply_gateway_runtime_override.assert_not_called()
