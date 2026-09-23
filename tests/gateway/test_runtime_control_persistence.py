"""Agent runtime tools and Gateway share the durable session override contract."""

import json
from types import SimpleNamespace

from agent.runtime_control import dispatch_model_switch
from gateway.config import GatewayConfig, Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore
from gateway.session_state import SessionState


class Agent:
    def __init__(self, session_id):
        self.session_id = session_id
        self.platform = "discord"
        self.model = "old-model"
        self.provider = "old-provider"
        self.base_url = "https://old.example/v1"
        self.api_key = "old-secret"
        self.api_mode = "chat_completions"
        self.reasoning_config = {"enabled": True, "effort": "medium"}

    def switch_model(self, *, new_model, new_provider, api_key, base_url, api_mode):
        self.model = new_model
        self.provider = new_provider
        self.api_key = api_key
        self.base_url = base_url
        self.api_mode = api_mode


def _bound_runtime(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = SessionSource(platform=Platform.DISCORD, chat_id="runtime-control", user_id="owner")
    entry = store.get_or_create_session(source)
    state = SessionState()
    runner = SimpleNamespace(session_store=store, _session_state=lambda key: state)
    agent = Agent(entry.session_id)
    TurnRunner(runner, SimpleNamespace(session_key=entry.session_key))._bind_runtime_update_callback(agent)
    return store, entry.session_key, state.conversation, agent


def test_route_switch_persists_secret_free_model_and_route_intent(tmp_path, monkeypatch):
    store, key, state, agent = _bound_runtime(tmp_path)
    monkeypatch.setattr(
        "agent.runtime_control._resolve_route_switch_target",
        lambda _agent, _route: ("resolved", {
            "route": "dev", "provider": "new-provider", "model": "new-model",
            "reasoning_effort": "high", "source": "default",
        }),
    )
    monkeypatch.setattr(
        "agent.runtime_control.resolve_model_switch",
        lambda **_kwargs: SimpleNamespace(
            success=True, new_model="new-model", target_provider="new-provider",
            api_key="new-secret", base_url="https://user:pass@new.example/v1?token=secret",
            api_mode="codex_responses", warning_message="",
        ),
    )

    result = json.loads(dispatch_model_switch(agent, {"route": "dev", "reason": "coding"}))

    assert result["success"] is True
    assert state.active_route_name == agent._active_route_name == "dev"
    assert state.reasoning_override == {"enabled": True, "effort": "high"}
    assert store.get_reasoning_override(key)["effort"] == "high"
    saved = SessionStore(tmp_path, GatewayConfig()).get_model_override(key)
    assert saved["model"] == "new-model"
    assert saved["provider"] == "new-provider"
    assert "base_url" not in saved
    assert "api_key" not in saved
    assert "new-secret" not in json.dumps(store.lookup_by_session_key(key).to_dict())
    assert "token=secret" not in json.dumps(store.lookup_by_session_key(key).to_dict())
    assert "new-secret" not in json.dumps(result)


def test_satisfied_route_records_intent_without_reapplying_model(tmp_path, monkeypatch):
    store, key, state, agent = _bound_runtime(tmp_path)
    monkeypatch.setattr(
        "agent.runtime_control._resolve_route_switch_target",
        lambda _agent, _route: ("noop", {"route": "chat"}),
    )

    result = json.loads(dispatch_model_switch(agent, {"route": "chat"}))

    assert result["noop"] is True
    assert state.active_route_name == agent._active_route_name == "chat"
    assert store.get_model_override(key) is None


def test_explicit_pin_and_release_commit_before_live_change(tmp_path, monkeypatch):
    store, key, state, agent = _bound_runtime(tmp_path)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"agent": {"reasoning_effort": "medium"}})
    persist = store._persist_routing_data

    def failed_write(*_args, **_kwargs):
        raise OSError("disk write failed")

    monkeypatch.setattr(store, "_persist_routing_data", failed_write)
    failed_pin = json.loads(dispatch_model_switch(agent, {
        "operation": "pin_reasoning", "user_requested": True, "reasoning_effort": "max",
    }))
    assert failed_pin["success"] is False
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    assert state.reasoning_override is None
    assert SessionStore(tmp_path, GatewayConfig()).get_reasoning_override(key) is None
    monkeypatch.setattr(store, "_persist_routing_data", persist)

    pinned = json.loads(dispatch_model_switch(agent, {
        "operation": "pin_reasoning", "user_requested": True, "reasoning_effort": "max",
    }))
    assert pinned["success"] is True
    assert agent.reasoning_config["selection"] == "pinned"
    assert state.reasoning_override["effort"] == "max"
    assert SessionStore(tmp_path, GatewayConfig()).get_reasoning_override(key)["effort"] == "max"

    monkeypatch.setattr(store, "_persist_routing_data", failed_write)
    failed = json.loads(dispatch_model_switch(agent, {
        "operation": "release_reasoning", "user_requested": True,
    }))
    assert failed["success"] is False
    assert agent.reasoning_config["selection"] == "pinned"
    assert state.reasoning_override["effort"] == "max"
    assert SessionStore(tmp_path, GatewayConfig()).get_reasoning_override(key)["effort"] == "max"

    monkeypatch.setattr(store, "_persist_routing_data", persist)
    released = json.loads(dispatch_model_switch(agent, {
        "operation": "release_reasoning", "user_requested": True,
    }))
    assert released["success"] is True
    assert state.reasoning_override is None
    assert agent.reasoning_config.get("selection") != "pinned"
    assert SessionStore(tmp_path, GatewayConfig()).get_reasoning_override(key) is None
