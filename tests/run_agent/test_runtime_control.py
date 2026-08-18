import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.runtime_control import (
    get_runtime_state,
    restore_pending_turn_runtime,
    model_status,
    model_switch,
)


class DummyAgent:
    def __init__(self):
        self.model = "old-model"
        self.provider = "old-provider"
        self.base_url = "https://old.example/v1"
        self.api_key = "secret-old"
        self.api_mode = "chat_completions"
        self.reasoning_config = {"enabled": True, "effort": "medium"}
        self.session_id = "sess-1"
        self.platform = "discord"
        self._gateway_session_key = "gw-key"
        self.switch_calls = []
        self.runtime_updates = []
        self._cached_system_prompt = "cached"
        self._fallback_chain = [{"provider": "old-provider", "model": "fallback-old"}]
        self._fallback_model = self._fallback_chain[0]
        self._fallback_index = 0
        self._fallback_activated = False

    def switch_model(self, new_model, new_provider, api_key="", base_url="", api_mode=""):
        self.switch_calls.append((new_model, new_provider, api_key, base_url, api_mode))
        self.model = new_model
        self.provider = new_provider
        if api_key:
            self.api_key = api_key
        if base_url:
            self.base_url = base_url
        if api_mode:
            self.api_mode = api_mode
        self._cached_system_prompt = None
        # Mimic AIAgent.switch_model side effects that should be restored for
        # turn-scoped switches.
        self._fallback_chain = []
        self._fallback_model = None
        self._fallback_index = 99
        self._fallback_activated = True

    def runtime_update_callback(self, **kwargs):
        self.runtime_updates.append(kwargs)


def _switch_result(model="new-model", provider="new-provider"):
    return SimpleNamespace(
        success=True,
        new_model=model,
        target_provider=provider,
        api_key="secret-new",
        base_url="https://new.example/v1",
        api_mode="codex_responses",
        error_message="",
        warning_message="",
        provider_label="New Provider",
    )


def test_model_status_reports_route_language_without_operator_ids():
    """ADR-003 Phase 3c: the LLM-facing status is route + model + reasoning.

    Raw provider/endpoint/api-mode identifiers are operator data — they stay
    in the telemetry event, never in the tool payload."""
    agent = DummyAgent()
    agent.base_url = "https://user:pass@example.com/v1?api_key=secret-old&x=1#frag"

    data = json.loads(model_status(agent))

    assert data["success"] is True
    assert data["model"] == "old-model"
    assert "provider" not in data
    assert "api_mode" not in data
    assert "base_url" not in data
    assert "has_gateway_session" not in data
    assert "gateway_session_key" not in data
    assert data["reasoning"] == {
        "enabled": True,
        "effort": "medium",
        "source": "agent",
    }
    serialized = json.dumps(data)
    assert "secret-old" not in serialized
    assert "user:pass" not in serialized
    assert "api_key" not in serialized
    assert "gw-key" not in serialized


def test_model_status_emits_runtime_state_hook(monkeypatch):
    agent = DummyAgent()
    events = []

    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: events.append((hook_name, kwargs)),
    )

    data = json.loads(model_status(agent))

    assert data["success"] is True
    assert len(events) == 1
    hook_name, payload = events[0]
    assert hook_name == "runtime_state"
    assert payload["event"] == "status"
    assert payload["session_id"] == "sess-1"
    assert payload["platform"] == "discord"
    assert payload["state"]["success"] is True
    assert payload["state"]["model"] == "old-model"
    assert payload["state"]["provider"] == "old-provider"
    assert "api_key" not in json.dumps(payload["state"])


def test_model_switch_forces_session_scope_even_when_global_requested():
    """Even if scope='global' is passed, it is forced to session."""
    agent = DummyAgent()

    data = json.loads(model_switch(agent, reasoning_effort="high", scope="global"))

    assert data["success"] is True
    assert data["scope"] == "session"
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_model_switch_turn_scope_forced_to_session():
    """scope='turn' is ignored and forced to session; no restore snapshot is created."""
    agent = DummyAgent()

    changed = json.loads(model_switch(agent, reasoning_effort="high", scope="turn"))

    assert changed["success"] is True
    assert changed["scope"] == "session"
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    # No turn restore snapshot should be created
    assert json.loads(model_status(agent))["turn_override_active"] is False
    # Gateway callback should have been called (session-scoped)
    assert len(agent.runtime_updates) == 1
    assert agent.runtime_updates[0]["scope"] == "session"


def test_model_switch_session_scope_persists_reasoning_callback():
    agent = DummyAgent()

    changed = json.loads(model_switch(agent, reasoning_effort="none", scope="session"))

    assert changed["success"] is True
    assert agent.reasoning_config == {"enabled": False}
    assert agent.runtime_updates == [
        {
            "scope": "session",
            "model_override": None,
            "reasoning_config": {"enabled": False},
        }
    ]


def test_model_switch_emits_runtime_state_hook_after_switch(monkeypatch):
    agent = DummyAgent()
    events = []

    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook",
        lambda hook_name, **kwargs: events.append((hook_name, kwargs)),
    )

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch", return_value=_switch_result("gpt-5.5", "codex-nekos")
    ):
        changed = json.loads(
            model_switch(
                agent,
                model="gpt-5.5",
                provider="codex-nekos",
                reasoning_effort="low",
                scope="session",
            )
        )

    assert changed["success"] is True
    assert len(events) == 1
    hook_name, payload = events[0]
    assert hook_name == "runtime_state"
    assert payload["event"] == "switch"
    assert payload["session_id"] == "sess-1"
    assert payload["platform"] == "discord"
    assert payload["state"]["model"] == "gpt-5.5"
    assert payload["state"]["provider"] == "codex-nekos"
    assert payload["state"]["api_mode"] == "codex_responses"
    assert payload["state"]["reasoning"] == {
        "enabled": True,
        "effort": "low",
        "source": "model_switch:session",
    }
    assert "secret-new" not in json.dumps(payload["state"])


def _configured_provider_models_config():
    return {
        "providers": {
            "codex-nekos": {
                "default_model": "gpt-5.5",
                "models": {
                    "gpt-5.5": {"context_length": 272000},
                    "gpt-5.4": {"context_length": 400000},
                },
            },
            "claude-nekos": {
                "default_model": "claude-opus-4-6",
                "models": {
                    "claude-opus-4-6": {"context_length": 200000},
                    "shared-model": {"context_length": 100000},
                },
            },
            "other-nekos": {
                "default_model": "other-default",
                "models": ["shared-model", {"name": "other-default"}],
            },
        }
    }


def test_model_switch_rejects_agent_free_form_provider_model_before_fuzzy_resolution():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("o3", "openrouter"),
    ) as resolve:
        changed = json.loads(
            model_switch(
                agent,
                model="o3",
                provider="openrouter",
                scope="session",
                reason="test free-form rejection",
            )
        )

    assert changed["success"] is False
    assert "config" in changed["error"].lower()
    resolve.assert_not_called()
    assert agent.switch_calls == []


def test_model_switch_resolves_model_only_when_configured_unique_provider():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.5", "codex-nekos"),
    ) as resolve:
        changed = json.loads(model_switch(agent, model="gpt-5.5", scope="session"))

    assert changed["success"] is True
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["raw_input"] == "gpt-5.5"
    assert resolve.call_args.kwargs["explicit_provider"] == "codex-nekos"
    assert agent.switch_calls == [
        ("gpt-5.5", "codex-nekos", "secret-new", "https://new.example/v1", "codex_responses")
    ]


def test_model_switch_rejects_model_only_when_configured_ambiguous():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("shared-model", "claude-nekos"),
    ) as resolve:
        changed = json.loads(model_switch(agent, model="shared-model", scope="session"))

    assert changed["success"] is False
    assert "ambiguous" in changed["error"].lower()
    resolve.assert_not_called()
    assert agent.switch_calls == []


def test_model_switch_provider_only_uses_configured_default_model():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.5", "codex-nekos"),
    ) as resolve:
        changed = json.loads(model_switch(agent, provider="codex-nekos", scope="session"))

    assert changed["success"] is True
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["raw_input"] == "gpt-5.5"
    assert resolve.call_args.kwargs["explicit_provider"] == "codex-nekos"


def test_model_switch_fails_closed_if_shared_resolver_changes_configured_target():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("o3", "openrouter"),
    ):
        changed = json.loads(model_switch(agent, model="gpt-5.5", provider="codex-nekos", scope="session"))

    assert changed["success"] is False
    assert "resolved outside" in changed["error"].lower()
    assert agent.switch_calls == []


def test_model_switch_model_uses_model_switch_and_agent_switch_model():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch", return_value=_switch_result("gpt-5.5", "codex-nekos")
    ) as resolve:
        changed = json.loads(
            model_switch(
                agent,
                model="gpt-5.5",
                provider="codex-nekos",
                reasoning_effort="low",
                scope="session",
                reason="test",
            )
        )

    resolve.assert_called_once()
    assert agent.switch_calls == [
        ("gpt-5.5", "codex-nekos", "secret-new", "https://new.example/v1", "codex_responses")
    ]
    assert agent.model == "gpt-5.5"
    assert agent.provider == "codex-nekos"
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert changed["success"] is True
    assert "secret-new" not in json.dumps(changed)
    assert agent.runtime_updates == [
        {
            "scope": "session",
            "model_override": {
                "model": "gpt-5.5",
                "provider": "codex-nekos",
                "api_key": "secret-new",
                "base_url": "https://new.example/v1",
                "api_mode": "codex_responses",
            },
            "reasoning_config": {"enabled": True, "effort": "low"},
        }
    ]


def test_session_switch_after_turn_switch_updates_pending_restore_snapshot():
    agent = DummyAgent()

    model_switch(agent, reasoning_effort="high", scope="turn")
    model_switch(agent, reasoning_effort="low", scope="session")
    restore_pending_turn_runtime(agent)

    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert agent.runtime_updates[-1] == {
        "scope": "session",
        "model_override": None,
        "reasoning_config": {"enabled": True, "effort": "low"},
    }


def test_session_model_switch_after_turn_switch_updates_pending_restore_snapshot():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch", return_value=_switch_result("gpt-5.4", "codex-nekos")
    ):
        model_switch(agent, model="gpt-5.4", provider="codex-nekos", scope="turn")
    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch", return_value=_switch_result("gpt-5.5", "codex-nekos")
    ):
        model_switch(agent, model="gpt-5.5", provider="codex-nekos", scope="session")
    restore_pending_turn_runtime(agent)

    assert agent.model == "gpt-5.5"
    assert agent.provider == "codex-nekos"
    assert agent._fallback_chain == []
    assert agent._fallback_index == 99


def test_session_switch_reports_callback_persistence_warning():
    agent = DummyAgent()

    def fail_callback(**kwargs):
        raise RuntimeError("boom")

    agent.runtime_update_callback = fail_callback

    changed = json.loads(model_switch(agent, reasoning_effort="low", scope="session"))

    assert changed["success"] is True
    assert "persistence_warning" in changed
    assert "boom" in changed["persistence_warning"]
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}


def test_session_scoped_model_switch_persists_and_callbacks():
    """Model switch with scope='turn' is forced to session; gateway callback fires,
    no turn restore snapshot is created, and the model stays switched."""
    agent = DummyAgent()
    original_chain = list(agent._fallback_chain)

    with patch("hermes_cli.config.load_config", return_value=_configured_provider_models_config()), patch(
        "agent.runtime_control.resolve_model_switch", return_value=_switch_result("gpt-5.4", "codex-nekos")
    ):
        model_switch(agent, model="gpt-5.4", provider="codex-nekos", scope="turn")

    assert agent._fallback_chain == []  # switch_model mutated it
    assert agent.model == "gpt-5.4"
    assert agent.provider == "codex-nekos"
    # No turn restore snapshot — scope forced to session
    assert not hasattr(agent, "_runtime_turn_restore_snapshot")
    # Gateway callback should have fired
    assert len(agent.runtime_updates) == 1
    assert agent.runtime_updates[0]["scope"] == "session"


def test_get_runtime_state_marks_disabled_reasoning_as_none():
    agent = DummyAgent()
    agent.reasoning_config = {"enabled": False}

    state = get_runtime_state(agent)

    assert state["reasoning"] == {
        "enabled": False,
        "effort": "none",
        "source": "agent",
    }
