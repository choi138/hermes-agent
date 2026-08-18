"""ADR-003 Phase 3b: route-enum self model switching (agent.runtime_control).

Everything here exercises the model_routes integration, so the whole module
skips cleanly on the branch-alone runtime-control build where the
hermes_cli.model_routes subsystem does not exist.

No network: provider health probing is short-circuited by the
PYTEST_CURRENT_TEST guard inside ``provider_health`` (returns healthy), and
tests that need scripted verdicts patch ``hermes_cli.model_routes.
provider_health`` directly.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("hermes_cli.model_routes")

from agent.runtime_control import model_status, model_switch


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

    def runtime_update_callback(self, **kwargs):
        self.runtime_updates.append(kwargs)


def _switch_result(model, provider):
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


def _route_config():
    """providers + model_routes: 'dev' pins effort xhigh with a fallback,
    'chat' declares no effort (any effort satisfies it)."""
    return {
        "providers": {
            "codex-nekos": {
                "base_url": "https://codex.example/v1",
                "default_model": "gpt-5.5",
                "models": {"gpt-5.5": {}, "gpt-5.4": {}},
            },
            "claude-nekos": {
                "base_url": "https://claude.example/v1",
                "default_model": "claude-opus-4-6",
                "models": {"claude-opus-4-6": {}, "claude-4-7-sonnet": {}},
            },
        },
        "model_routes": {
            "routes": {
                "dev": {
                    "description": "Deep coding and debugging",
                    "provider": "codex-nekos",
                    "model": "gpt-5.5",
                    "reasoning_effort": "xhigh",
                    "fallbacks": [
                        {"provider": "claude-nekos", "model": "claude-4-7-sonnet"},
                    ],
                },
                "chat": {
                    "description": "Casual conversation",
                    "provider": "claude-nekos",
                    "model": "claude-opus-4-6",
                },
            },
        },
    }


def _no_routes_config():
    cfg = _route_config()
    del cfg["model_routes"]
    return cfg


# ---------------------------------------------------------------------------
# model_switch: route resolution -> existing apply plumbing
# ---------------------------------------------------------------------------


def test_route_switch_applies_resolved_runtime_via_existing_plumbing():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.5", "codex-nekos"),
    ) as resolve:
        data = json.loads(model_switch(agent, route="dev", scope="session", reason="coding task"))

    assert data["success"] is True
    assert data["scope"] == "session"
    assert data["route"] == {"name": "dev"}
    # Resolved spec fed the EXISTING plumbing: shared resolver + switch_model.
    resolve.assert_called_once()
    assert resolve.call_args.kwargs["raw_input"] == "gpt-5.5"
    assert resolve.call_args.kwargs["explicit_provider"] == "codex-nekos"
    assert agent.switch_calls == [
        ("gpt-5.5", "codex-nekos", "secret-new", "https://new.example/v1", "codex_responses")
    ]
    # Route default effort (xhigh) applied since no explicit effort was given.
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert data["changed"] == ["model", "reasoning"]
    # Gateway persistence callback fired with the resolved override.
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
            "reasoning_config": {"enabled": True, "effort": "xhigh"},
        }
    ]
    assert "secret-new" not in json.dumps(data)


def test_route_switch_explicit_reasoning_effort_beats_route_default():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.5", "codex-nekos"),
    ):
        data = json.loads(model_switch(agent, route="dev", reasoning_effort="low", scope="session"))

    assert data["success"] is True
    # Explicit effort wins over the route's xhigh default.
    assert agent.reasoning_config == {"enabled": True, "effort": "low"}
    assert agent.runtime_updates[-1]["reasoning_config"] == {"enabled": True, "effort": "low"}


def test_route_switch_walks_to_healthy_fallback():
    agent = DummyAgent()

    def scripted_health(provider, model="", **kwargs):
        return (False, "HTTP 503") if provider == "codex-nekos" else (True, "HTTP 200")

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "hermes_cli.model_routes.provider_health", side_effect=scripted_health
    ), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("claude-4-7-sonnet", "claude-nekos"),
    ):
        data = json.loads(model_switch(agent, route="dev", scope="session"))

    assert data["success"] is True
    assert data["route"]["name"] == "dev"
    assert data["route"]["source"] == "fallback:1"
    assert "codex-nekos unhealthy" in data["route"]["failover"]
    assert agent.switch_calls[0][0] == "claude-4-7-sonnet"
    assert agent.switch_calls[0][1] == "claude-nekos"
    # Fallback declares no effort -> route applies no reasoning change.
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}
    assert data["changed"] == ["model"]


def test_route_switch_unknown_route_errors_with_declared_names():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        data = json.loads(model_switch(agent, route="nope", scope="session"))

    assert data["success"] is False
    assert "Unknown route 'nope'" in data["error"]
    assert "dev" in data["error"] and "chat" in data["error"]
    assert data["declared_routes"] == ["dev", "chat"]
    assert agent.switch_calls == []
    assert agent.runtime_updates == []


def test_route_switch_all_unhealthy_errors_with_declared_names():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "hermes_cli.model_routes.provider_health", return_value=(False, "HTTP 503")
    ):
        data = json.loads(model_switch(agent, route="dev", scope="session"))

    assert data["success"] is False
    assert "no healthy runtime" in data["error"]
    assert "dev" in data["error"] and "chat" in data["error"]
    assert agent.switch_calls == []
    assert agent.runtime_updates == []


def test_route_switch_noop_when_runtime_already_satisfies():
    agent = DummyAgent()
    agent.model = "claude-opus-4-6"
    agent.provider = "claude-nekos"

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch"
    ) as resolve:
        data = json.loads(model_switch(agent, route="chat", scope="session"))

    assert data["success"] is True
    assert data["noop"] is True
    assert data["route"] == {"name": "chat", "already_satisfied": True}
    assert data["changed"] == []
    assert "Already on route 'chat'" in data["message"]
    # Nothing was re-applied: no resolver call, no switch, no gateway callback.
    resolve.assert_not_called()
    assert agent.switch_calls == []
    assert agent.runtime_updates == []


def test_route_noop_with_explicit_reasoning_applies_effort_only():
    agent = DummyAgent()
    agent.model = "claude-opus-4-6"
    agent.provider = "claude-nekos"

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch"
    ) as resolve:
        data = json.loads(
            model_switch(agent, route="chat", reasoning_effort="high", scope="session")
        )

    assert data["success"] is True
    assert data["changed"] == ["reasoning"]
    assert data["route"] == {"name": "chat", "already_satisfied": True}
    resolve.assert_not_called()
    assert agent.switch_calls == []
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert agent.runtime_updates == [
        {
            "scope": "session",
            "model_override": None,
            "reasoning_config": {"enabled": True, "effort": "high"},
        }
    ]


def test_route_switch_reapplies_when_pinned_effort_differs():
    """A route that pins reasoning_effort is NOT satisfied by the same model
    at a different effort — the switch re-applies (mirror of the gateway
    router's effort-aware membership)."""
    agent = DummyAgent()
    agent.model = "gpt-5.5"
    agent.provider = "codex-nekos"
    agent.reasoning_config = {"enabled": True, "effort": "low"}  # dev pins xhigh

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.5", "codex-nekos"),
    ):
        data = json.loads(model_switch(agent, route="dev", scope="session"))

    assert data["success"] is True
    assert "noop" not in data
    assert agent.switch_calls != []
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}


def test_route_switch_rejects_route_plus_explicit_model():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        data = json.loads(model_switch(agent, route="dev", model="gpt-5.5", scope="session"))

    assert data["success"] is False
    assert "not both" in data["error"]
    assert agent.switch_calls == []


def test_free_form_switch_still_works_when_routes_declared():
    """Declaring routes changes the SCHEMA the model sees, but the handler
    keeps accepting explicit model/provider (strict config-declared)."""
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_route_config()), patch(
        "agent.runtime_control.resolve_model_switch",
        return_value=_switch_result("gpt-5.4", "codex-nekos"),
    ):
        data = json.loads(
            model_switch(agent, model="gpt-5.4", provider="codex-nekos", scope="session")
        )

    assert data["success"] is True
    assert "route" not in data
    assert agent.model == "gpt-5.4"


# ---------------------------------------------------------------------------
# model_status: current route + catalog
# ---------------------------------------------------------------------------


def test_model_status_reports_current_route_and_catalog():
    agent = DummyAgent()
    agent.model = "claude-opus-4-6"
    agent.provider = "claude-nekos"

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        data = json.loads(model_status(agent))

    assert data["success"] is True
    assert data["routes"]["current"] == "chat"
    assert data["routes"]["available"] == [
        {"name": "dev", "description": "Deep coding and debugging"},
        {"name": "chat", "description": "Casual conversation"},
    ]


def test_model_status_current_route_none_when_nothing_satisfied():
    agent = DummyAgent()  # old-model/old-provider matches no route

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        data = json.loads(model_status(agent))

    assert data["routes"]["current"] is None
    assert [entry["name"] for entry in data["routes"]["available"]] == ["dev", "chat"]


def test_model_status_effort_pinned_route_membership():
    """dev pins xhigh: same model at medium is NOT current; at xhigh it is."""
    agent = DummyAgent()
    agent.model = "gpt-5.5"
    agent.provider = "codex-nekos"

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        assert json.loads(model_status(agent))["routes"]["current"] is None
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
        assert json.loads(model_status(agent))["routes"]["current"] == "dev"


def test_model_status_unchanged_when_no_routes_declared():
    agent = DummyAgent()

    with patch("hermes_cli.config.load_config", return_value=_no_routes_config()):
        data = json.loads(model_status(agent))

    assert "routes" not in data


# ---------------------------------------------------------------------------
# Schema integration against a REAL catalog (not mocked pairs)
# ---------------------------------------------------------------------------


def test_model_switch_schema_route_enum_from_real_catalog():
    from tools.runtime_control_tool import _build_model_switch_schema_overrides

    with patch("hermes_cli.config.load_config", return_value=_route_config()):
        overrides = _build_model_switch_schema_overrides()

    props = overrides["parameters"]["properties"]
    assert props["route"]["enum"] == ["dev", "chat"]
    assert "dev: Deep coding and debugging" in props["route"]["description"]
    assert "model" not in props
    assert "provider" not in props


def test_model_switch_schema_untouched_when_catalog_dormant():
    from tools.runtime_control_tool import _build_model_switch_schema_overrides

    with patch("hermes_cli.config.load_config", return_value=_no_routes_config()):
        assert _build_model_switch_schema_overrides() == {}
