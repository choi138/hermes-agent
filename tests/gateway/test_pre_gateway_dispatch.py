"""Tests for the pre_gateway_dispatch plugin hook.

The hook allows plugins to intercept incoming messages before auth and
agent dispatch. It runs in _handle_message and acts on returned action
dicts: {"action": "skip"|"rewrite"|"prepend"|"runtime_override"|"allow"}.
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
    runner._pending_runtime_route_states = {}
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


def test_runtime_override_normalizes_one_shot_route_state(monkeypatch):
    directive = {
        "action": "runtime_override",
        "model": "gpt-5.5",
        "provider": "codex-nekos",
        "reasoning_effort": "high",
        "reason": "codex-lb PR review",
    }

    def _fake_switch_model(**_kwargs):
        return SimpleNamespace(
            success=True,
            new_model="gpt-5.5",
            target_provider="codex-nekos",
            api_key="test-key",
            base_url="https://codex.nekos.me/v1",
            api_mode="codex_responses",
            provider_label="codex-nekos",
            error_message="",
        )

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _fake_switch_model)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "model": {"default": "old-model", "provider": "openrouter"},
            "providers": {},
        },
    )
    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._get_session_model_override = lambda _key: {}
    runner._persist_session_runtime_override = lambda *_args, **_kwargs: None
    runner._evict_cached_agent = lambda _key: None
    runner._pending_model_notes = {}
    source = _make_event().source

    assert runner._apply_gateway_runtime_override(directive, source) is True

    session_key = runner._session_key_for_source(source)
    expected = {
        "label": "RUNTIME_OVERRIDE",
        "target_provider": "codex-nekos",
        "target_model": "gpt-5.5",
        "target_reasoning_effort": "high",
        "source": "pre_gateway_dispatch",
        "strictness": "auto_reconsiderable",
        "confidence": "unknown",
        "reason": "codex-lb PR review",
    }
    assert runner._pending_runtime_route_states[session_key] == expected
    assert runner._consume_pending_runtime_route_state(session_key) == expected
    assert runner._consume_pending_runtime_route_state(session_key) is None


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


@pytest.mark.asyncio
async def test_hook_prepend_applies_to_plain_chat(monkeypatch):
    """A plugin returning {'action': 'prepend', 'text': ...} prefixes event.text."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    seen_text = {}

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "prepend", "text": "[INBOX_MATTER det_key=x]"}]
        return []

    async def _capture(event, source, _quick_key, _run_generation):
        seen_text["value"] = event.text
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    await runner._handle_message(_make_event("hello there"))

    assert seen_text.get("value") == "[INBOX_MATTER det_key=x]\n\nhello there"


@pytest.mark.asyncio
async def test_hook_prepend_dropped_for_slash_command(monkeypatch):
    """Prepends must not mutate slash commands.

    is_command()/get_command() key off text.startswith("/"): a prepended
    advisory marker would demote a recognized command (/model, /status, ...)
    into plain chat that falls through to the agent (live incident
    2026-07-15: /model in an inbox-matter Discord thread answered by the
    agent's model_status tool instead of the interactive picker).
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")

    def _fake_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            return [{"action": "prepend", "text": "[INBOX_MATTER det_key=x]"}]
        return []

    seen = {}

    async def _capture_model(event):
        seen["model_event_text"] = event.text
        return "picker"

    async def _capture_agent(event, source, _quick_key, _run_generation):
        seen["agent_event_text"] = event.text
        return "ok"

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", _fake_hook)

    runner, _adapter = _make_runner(Platform.WHATSAPP)
    runner._handle_model_command = _capture_model  # noqa: SLF001
    runner._handle_message_with_agent = _capture_agent  # noqa: SLF001

    result = await runner._handle_message(_make_event("/model"))

    # The command must reach the /model handler with its text intact —
    # not fall through to the agent as prepended plain chat.
    assert seen.get("model_event_text") == "/model"
    assert "agent_event_text" not in seen
    assert result == "picker"
