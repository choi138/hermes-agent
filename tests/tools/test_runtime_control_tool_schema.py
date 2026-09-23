"""Model-facing runtime controls stay route-only and catalog-gated."""

from tools.registry import invalidate_check_fn_cache, registry
from tools.runtime_control_tool import _MODEL_SWITCH_SCHEMA, _MODEL_STATUS_SCHEMA
from toolsets import resolve_toolset


def test_default_discord_toolset_offers_runtime_control():
    names = resolve_toolset("hermes-discord")
    assert "model_status" in names
    assert "model_switch" in names


def test_route_catalog_controls_switch_schema(monkeypatch):
    monkeypatch.setattr("agent.runtime_control._route_catalog_pairs", lambda: [])
    invalidate_check_fn_cache()
    assert registry.get_definitions({"model_switch"}) == []

    monkeypatch.setattr("agent.runtime_control._route_catalog_pairs", lambda: [("dev", "Coding"), ("chat", "Conversation")])
    invalidate_check_fn_cache()
    definitions = registry.get_definitions({"model_switch"})
    assert len(definitions) == 1
    properties = definitions[0]["function"]["parameters"]["properties"]
    assert properties["route"]["enum"] == ["dev", "chat"]
    assert set(properties) == {"route", "reason", "operation", "user_requested", "reasoning_effort"}
    assert "model" not in properties and "provider" not in properties
    assert _MODEL_STATUS_SCHEMA["parameters"]["properties"] == {}
    assert _MODEL_SWITCH_SCHEMA["parameters"]["additionalProperties"] is False
    invalidate_check_fn_cache()
