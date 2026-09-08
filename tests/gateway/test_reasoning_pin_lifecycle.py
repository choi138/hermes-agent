"""User-command pin through real session serialization and routing seams."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.runtime_control import model_status, model_switch
from agent.transports.codex import ResponsesApiTransport
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


def runner_for(directory):
    runner = object.__new__(GatewayRunner)
    cfg = GatewayConfig()
    cfg.sessions_dir = directory
    runner.session_store = SessionStore(sessions_dir=directory, config=cfg)
    runner.session_store._db = None
    runner._load_reasoning_config = lambda *a: {"enabled": True, "effort": "high"}
    runner._evict_cached_agent = MagicMock()
    return runner


def source():
    return SessionSource(platform=Platform.LOCAL, chat_id="pin-test", chat_type="dm", user_id="owner")


def test_tool_pin_schema_executor_callback_disk_restore_router_and_release(tmp_path, monkeypatch):
    from unittest.mock import patch
    from agent.agent_runtime_helpers import invoke_tool
    from gateway import model_router as mr
    from hermes_cli.model_routes import load_routes
    from tools.registry import registry, invalidate_check_fn_cache
    from run_agent import AIAgent

    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.getaddrinfo", deny)

    cfg = {"providers": {"test": {"base_url": "https://local.invalid/v1"}},
           "model_routes": {"routes": {
               "dev": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "max"},
               "chat": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "medium"}},
               "router": {"mode": "enforce", "normal_downgrade_streak": 2, "chat_route": "chat"}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 128000)
    import tools.runtime_control_tool  # register the actual exposed schema
    invalidate_check_fn_cache()
    schema = registry.get_definitions({"model_switch"})[0]["function"]["parameters"]
    assert set(schema["properties"]["route"]["enum"]) == {"dev", "chat"}
    assert "pin_reasoning" in schema["properties"].get("operation", {}).get("enum", []), "F1: tool cannot express a user pin"

    runner = runner_for(tmp_path)
    key = runner.session_store.get_or_create_session(source()).session_key
    def rebuild(owner):
        with patch("run_agent.get_tool_definitions", return_value=[]), patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
            agent = AIAgent(model="gpt-6-astra", provider="test", api_key="offline-only",
                base_url="https://local.invalid/v1", api_mode="codex_responses", quiet_mode=True,
                skip_context_files=True, skip_memory=True,
                reasoning_config=owner._resolve_session_reasoning_config(session_key=key))
        owner._bind_runtime_update_callback(agent, key)
        return agent
    agent = rebuild(runner)
    result = json.loads(invoke_tool(agent, "model_switch", {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": True}, "local"))
    assert result["success"], result
    assert agent.reasoning_config["selection"] == "pinned"
    assert agent._runtime_reasoning_source == "user:model_switch"
    # Exercise actual cache eviction, then reconstruct from disk on a fresh runner.
    runner._agent_cache = {key: agent}
    runner._running_agent_items = lambda: [(key, agent)]
    GatewayRunner._evict_cached_agent(runner, key)
    assert key not in runner._agent_cache
    del agent
    resumed = runner_for(tmp_path)
    agent = rebuild(resumed)
    assert agent.reasoning_config["selection"] == "pinned"
    resumed._resolve_session_agent_runtime = lambda **kw: (agent.model, {"provider": agent.provider})
    catalog = load_routes(cfg)
    state = {}
    def decide():
        return mr.classifier_decision_from_detail(
            context=mr.PolicyClassificationContext(current_user_message="status?", recent_turns=[], session_key=key),
            detail={"label": "NORMAL", "source": "llm", "confidence": .99, "evidence": "status", "reason": "normal"},
            session_store=resumed.session_store,
            runtime=resumed._model_router_runtime_snapshot(source(), key, user_config=cfg),
            cfg=cfg, catalog=catalog, router=catalog.router, mode="enforce", state=state, provider="test", model="classifier")
    for _ in range(3):
        assert decide().outcome == "user_pinned"
    released = json.loads(invoke_tool(agent, "model_switch", {"operation": "release_reasoning", "user_requested": True}, "local"))
    assert released["success"], released
    assert (agent.reasoning_config or {}).get("selection") != "pinned"
    assert (agent.reasoning_config or {}).get("effort") != "max"
    entry = resumed.session_store.get_entry(key)
    assert entry.runtime_reasoning_effort is None
    assert entry.runtime_reasoning_selection == "auto"
    assert rebuild(runner_for(tmp_path)).reasoning_config["effort"] == "high"
    decide()
    assert decide().outcome == "downgrade_to_chat"
    assert json.loads(invoke_tool(agent, "model_switch", {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": True}, "local"))["success"]
    resumed.session_store.reset_session(key)
    resumed._clear_conversation_scope(key, reason="new")
    assert rebuild(runner_for(tmp_path)).reasoning_config.get("selection") != "pinned"


def test_user_pin_survives_rebuild_restart_release_and_new(tmp_path):
    runner = runner_for(tmp_path)
    entry = runner.session_store.get_or_create_session(source())
    key = entry.session_key
    runner._apply_reasoning_selection(key, "local", "max")
    pinned = runner._resolve_session_reasoning_config(session_key=key)
    assert pinned["selection"] == "pinned"
    assert pinned["effort"] == "max"
    # Fresh store loads actual on-disk serialization, fresh runner rebuilds.
    resumed = runner_for(tmp_path)
    assert resumed._resolve_session_reasoning_config(session_key=key) == pinned
    resumed._apply_reasoning_selection(key, "local", "reset")
    assert runner_for(tmp_path)._resolve_session_reasoning_config(session_key=key)["effort"] == "high"
    resumed._apply_reasoning_selection(key, "local", "max")
    resumed.session_store.reset_session(key)
    resumed._clear_conversation_scope(key, reason="new")
    assert resumed._resolve_session_reasoning_config(session_key=key)["effort"] == "high"
    assert runner_for(tmp_path)._resolve_session_reasoning_config(session_key=key).get("selection") != "pinned"


def test_pin_guards_real_gateway_auto_apply_then_release(tmp_path, monkeypatch):
    from hermes_cli.model_switch import ModelSwitchResult
    runner = runner_for(tmp_path)
    key = runner.session_store.get_or_create_session(source()).session_key
    runner._apply_reasoning_selection(key, "local", "max")
    switch = MagicMock(return_value=ModelSwitchResult(success=True, new_model="gpt-6-astra", target_provider="test"))
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", switch)
    directive = {"route": "chat", "model": "gpt-6-astra", "provider": "test", "reasoning_effort": "medium"}
    for _ in range(5):
        assert asyncio.run(runner._apply_model_router_directive(key, directive, {})) == (False, False)
    switch.assert_not_called()
    assert runner._resolve_session_reasoning_config(session_key=key)["effort"] == "max"
    runner._apply_reasoning_selection(key, "local", "reset")
    assert asyncio.run(runner._apply_model_router_directive(key, directive, {})) == (True, True)
    assert runner._resolve_session_reasoning_config(session_key=key)["effort"] == "medium"


def test_predispatch_runtime_hook_cannot_replace_pin(tmp_path):
    runner = runner_for(tmp_path)
    key = runner.session_store.get_or_create_session(source()).session_key
    runner._session_key_for_source = lambda _: key
    runner._apply_reasoning_selection(key, "local", "max")
    directive = {"reasoning_effort": "medium"}
    assert runner._apply_gateway_runtime_override(directive, source()) is False
    assert runner._resolve_session_reasoning_config(session_key=key)["selection"] == "pinned"
    runner._apply_reasoning_selection(key, "local", "reset")
    assert runner._apply_gateway_runtime_override(directive, source()) is True
    assert runner._resolve_session_reasoning_config(session_key=key)["effort"] == "medium"


def test_ordinary_route_cannot_replace_pin_and_status_is_local(monkeypatch):
    cfg = {"providers": {"test": {"base_url": "https://test.example/v1"}},
           "model_routes": {"routes": {"chat": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "medium"},
                                       "dev": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "high"}}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    agent = SimpleNamespace(model="gpt-6-astra", provider="test", api_mode="codex_responses",
                            reasoning_config={"enabled": True, "effort": "max", "selection": "pinned"})
    result = json.loads(model_switch(agent, route="chat"))
    assert result["success"] is False and "pin" in result["error"].lower()
    state = json.loads(model_status(agent))["reasoning"]
    assert state["selection"] == "pinned" and state["scope"] == "session"
    assert state["requested_effort"] == "max" and state["internal_effort"] == "max"
    assert state["wire_effort"] is None
    from tools.runtime_control_tool import _build_model_switch_schema_overrides
    assert set(_build_model_switch_schema_overrides()["parameters"]["properties"]["route"]["enum"]) == {"chat", "dev"}


def test_normal_sequence_holds_pin_then_release_resumes_routing():
    from gateway import model_router as mr
    from hermes_cli.model_routes import load_routes
    cfg = {"providers": {"test": {"base_url": "https://test.example/v1"}},
           "model_routes": {"routes": {
               "dev": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "max"},
               "chat": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "medium"}},
               "router": {"mode": "enforce", "normal_downgrade_streak": 2, "chat_route": "chat"}}}
    catalog = load_routes(cfg)
    runtime = {"model": "gpt-6-astra", "provider": "test", "reasoning_effort": "max", "reasoning_selection": "pinned"}
    state = {}
    def decide():
        return mr.classifier_decision_from_detail(
            context=mr.PolicyClassificationContext(current_user_message="status?", recent_turns=[], session_key="key"),
            detail={"label": "NORMAL", "source": "llm", "confidence": .99, "evidence": "status question", "reason": "normal"},
            session_store=None, runtime=runtime, cfg=cfg, catalog=catalog, router=catalog.router,
            mode="enforce", state=state, provider="test", model="classifier")
    for _ in range(5):
        assert decide().outcome == "user_pinned"
    runtime.pop("reasoning_selection")
    decide()
    assert decide().outcome == "downgrade_to_chat"


def test_unsupported_pin_is_not_silently_clamped_or_overridden():
    transport = ResponsesApiTransport()
    with pytest.raises(ValueError, match="[Pp]inned"):
        transport.build_kwargs(model="gpt-5.5", messages=[], tools=[],
                               reasoning_config={"effort": "max", "selection": "pinned"})
    with pytest.raises(ValueError, match="[Pp]inned"):
        transport.build_kwargs(model="gpt-6-astra", messages=[], tools=[],
                               reasoning_config={"effort": "max", "selection": "pinned"},
                               request_overrides={"reasoning": {"effort": "high"}})


def test_agent_effort_only_switch_cannot_release_user_pin():
    agent = SimpleNamespace(model="gpt-6-astra", provider="test", api_mode="codex_responses",
                            reasoning_config={"enabled": True, "effort": "max", "selection": "pinned"})
    result = json.loads(model_switch(agent, reasoning_effort="medium"))
    assert result["success"] is False
    assert agent.reasoning_config["selection"] == "pinned"
    assert agent.reasoning_config["effort"] == "max"


@pytest.mark.parametrize("override", ["high", [], None])
def test_pin_status_handles_non_object_reasoning_override(override):
    agent = SimpleNamespace(model="gpt-6-astra", provider="test", api_mode="codex_responses",
                            reasoning_config={"effort": "max", "selection": "pinned"},
                            request_overrides={"reasoning": override})
    assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"


def test_gateway_snapshot_and_static_status_rule_keep_pin(tmp_path, monkeypatch):
    from gateway import model_router as mr
    from hermes_cli.model_routes import load_routes
    runner = runner_for(tmp_path)
    key = runner.session_store.get_or_create_session(source()).session_key
    runner._apply_reasoning_selection(key, "local", "max")
    runner._resolve_session_agent_runtime = lambda **kw: ("gpt-6-astra", {"provider": "test"})
    runtime = runner._model_router_runtime_snapshot(source(), key, user_config={})
    assert runtime["reasoning_selection"] == "pinned"
    cfg = {"providers": {"test": {"base_url": "https://test.example/v1"}},
           "model_routes": {"routes": {"chat": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "medium"}},
                            "router": {"mode": "enforce"}}}
    catalog = load_routes(cfg)
    decision = mr.static_rule_decision(rule={"route": "chat"}, rule_name="status", text="/status",
        session_key=key, runtime=runtime, cfg=cfg, catalog=catalog, router=catalog.router, mode="enforce", state={})
    assert decision.outcome == "user_pinned" and decision.directive is None
