"""Real fallback/restore must retain each model's own automatic reasoning policy."""
import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import invoke_tool
from agent.runtime_control import restore_pending_turn_runtime, snapshot_runtime
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_constants import resolve_reasoning_config
from run_agent import AIAgent


@pytest.mark.parametrize("primary_effort,fallback_effort", [("low", "max"), ("medium", "low"), (False, "max"), (None, "max"), ("low", None)])
@pytest.mark.parametrize("restore", ["primary", "saved_turn", "direct_primary"])
def test_release_restores_each_models_automatic_policy(tmp_path, monkeypatch, primary_effort, fallback_effort, restore):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")

    for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo"):
        monkeypatch.setattr(target, deny)
    cfg = {"agent": {"reasoning_effort": None, "reasoning_overrides": {
        "gpt-6-astra": primary_effort, "gpt-5.6-sol": fallback_effort}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: cfg)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 128000)

    def runner_for():
        runner = object.__new__(GatewayRunner)
        config = GatewayConfig()
        config.sessions_dir = tmp_path / "sessions"
        runner.session_store = SessionStore(sessions_dir=config.sessions_dir, config=config)
        runner.session_store._db = None
        return runner

    runner = runner_for()
    key = runner.session_store.get_or_create_session(SessionSource(
        platform=Platform.LOCAL, chat_id="release", chat_type="dm", user_id="owner")).session_key
    destination = {"provider": "custom", "model": "gpt-5.6-sol", "base_url": "https://fallback.invalid/v1",
                   "api_key": "offline-only", "api_mode": "codex_responses"}

    def build(owner):
        with patch("run_agent.get_tool_definitions", return_value=[]), \
             patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
            agent = AIAgent(model="gpt-6-astra", provider="test", api_key="offline-only",
                            base_url="https://local.invalid/v1", api_mode="codex_responses",
                            quiet_mode=True, skip_context_files=True, skip_memory=True,
                            reasoning_config=owner._resolve_session_reasoning_config(session_key=key, model="gpt-6-astra"),
                            fallback_model=[destination])
        owner._bind_runtime_update_callback(agent, key)
        return agent

    def select(agent, operation, **kwargs):
        result = json.loads(invoke_tool(agent, "model_switch", {"operation": operation, "user_requested": True, **kwargs}, "offline-f1"))
        assert result["success"], result

    agent = build(runner)
    agent._runtime_turn_restore_snapshot = snapshot_runtime(agent)
    select(agent, "pin_reasoning", reasoning_effort="max")
    pinned = copy.deepcopy(agent.reasoning_config)
    assert agent._primary_runtime["reasoning_config"] == pinned
    assert agent._runtime_turn_restore_snapshot["reasoning_config"] == pinned
    if restore != "direct_primary":
        with patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None), \
             patch("agent.auxiliary_client.resolve_provider_client", return_value=(MagicMock(base_url=destination["base_url"], api_key="offline-only"), destination["model"])), \
             patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m):
            assert agent._try_activate_fallback()
        assert agent.reasoning_config == pinned
    select(agent, "release_reasoning")
    assert agent.reasoning_config == resolve_reasoning_config(cfg, agent.model)
    expected_primary = resolve_reasoning_config(cfg, "gpt-6-astra")
    # Invoke the real restoration before asserting, so RED exercises its consumer.
    primary_snapshot = copy.deepcopy(agent._primary_runtime)
    turn_snapshot = copy.deepcopy(agent._runtime_turn_restore_snapshot)
    with patch.object(agent, "_create_openai_client", return_value=MagicMock()), \
         patch("agent.credential_pool.load_pool", return_value=None):
        if restore == "saved_turn":
            assert restore_pending_turn_runtime(agent)
        elif restore == "primary":
            assert agent._restore_primary_runtime()
    rebuilt = build(runner_for())
    assert agent.model == rebuilt.model == "gpt-6-astra"
    assert agent.reasoning_config == rebuilt.reasoning_config == expected_primary
    assert primary_snapshot["reasoning_config"] == expected_primary
    assert turn_snapshot["reasoning_config"] == expected_primary
