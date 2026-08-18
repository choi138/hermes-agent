"""Regression tests for session-scoped model/provider overrides in gateway agents.

These cover the bug where `/model ...` stored a session override, but fresh
agent constructions still resolved model/provider from global config/runtime.
That let helper agents (and cache-miss main agents) route GPT-5.4 to the wrong
provider, e.g. Nous instead of OpenAI Codex.
"""

import asyncio
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner.session_store = None
    runner.config = None
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._service_tier = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_approvals = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    return runner


def _codex_override():
    return {
        "model": "gpt-5.4",
        "provider": "openai-codex",
        "api_key": "***",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }


def _explode_runtime_resolution():
    raise AssertionError(
        "global runtime resolution should not run when a complete session override exists"
    )


def _session_store(tmp_path):
    config = GatewayConfig()
    config.sessions_dir = tmp_path
    store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = None
    return store


def test_persisted_runtime_override_survives_gateway_restart(tmp_path, monkeypatch):
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )
    first_store = _session_store(tmp_path)
    entry = first_store.get_or_create_session(source)
    assert first_store.update_runtime_override(
        entry.session_key,
        model="gpt-5.5",
        provider="codex-nekos",
        reasoning_effort="high",
    )

    restarted_store = _session_store(tmp_path)
    runner = _make_runner()
    runner.session_store = restarted_store
    runner._load_reasoning_config = MagicMock(
        return_value={"enabled": True, "effort": "low"}
    )
    def resolve_persisted_runtime(provider, *, target_model=None):
        assert provider == "codex-nekos"
        assert target_model == "gpt-5.5"
        return {
            "api_key": "runtime-secret",
            "base_url": "https://codex.nekos.me/v1",
            "provider": provider,
            "requested_provider": provider,
            "api_mode": "codex_responses",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        resolve_persisted_runtime,
    )

    model, runtime = runner._resolve_session_agent_runtime(
        session_key=entry.session_key,
        user_config={
            "model": {"default": "base-model", "provider": "base-provider"}
        },
    )
    reasoning = runner._resolve_session_reasoning_config(
        session_key=entry.session_key,
        model=model,
    )

    assert model == "gpt-5.5"
    assert runtime["provider"] == "codex-nekos"
    assert runtime["api_key"] == "runtime-secret"
    assert runtime["base_url"] == "https://codex.nekos.me/v1"
    assert runtime["api_mode"] == "codex_responses"
    assert reasoning == {"enabled": True, "effort": "high"}
    runner._load_reasoning_config.assert_not_called()

    restored = restarted_store.get_entry(entry.session_key)
    assert restored is not None
    persisted = restored.to_dict()
    assert persisted["runtime_model"] == "gpt-5.5"
    assert persisted["runtime_provider"] == "codex-nekos"
    assert persisted["runtime_reasoning_effort"] == "high"
    assert "api_key" not in persisted
    assert "base_url" not in persisted
    assert "api_mode" not in persisted


def test_gateway_auth_fallback_uses_fallback_model_from_config(tmp_path, monkeypatch):
    """Regression: fallback provider must not inherit the primary model.

    If primary openai-codex auth fails and fallback_providers selects
    OpenRouter/minimax, the gateway must instantiate AIAgent with the fallback
    model, not the primary config model (e.g. gpt-5.5). Otherwise OpenRouter
    receives an unintended GPT request.
    """
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  default: gpt-5.5
  provider: openai-codex
fallback_providers:
  - provider: openrouter
    model: minimax/minimax-m2.7
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    def fake_resolve_runtime_provider(*, requested=None, explicit_base_url=None, explicit_api_key=None):
        if requested in {None, "", "openai-codex"}:
            from hermes_cli.auth import AuthError
            raise AuthError("No Codex credentials stored. Run `hermes auth` to authenticate.")
        assert requested == "openrouter"
        return {
            "api_key": "sk-openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    import hermes_cli.runtime_provider as runtime_provider

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider)

    runner = _make_runner()
    model, runtime_kwargs = runner._resolve_session_agent_runtime(
        session_key="agent:main:telegram:group:-1003715515980:63",
        user_config={
            "model": {"default": "gpt-5.5", "provider": "openai-codex"},
            "fallback_providers": [{"provider": "openrouter", "model": "minimax/minimax-m2.7"}],
        },
    )

    assert model == "minimax/minimax-m2.7"
    assert runtime_kwargs["provider"] == "openrouter"
    assert runtime_kwargs["api_key"] == "sk-openrouter"
