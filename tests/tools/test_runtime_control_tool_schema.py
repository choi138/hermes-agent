import copy
import json

from tools.runtime_control_tool import (
    _MODEL_SWITCH_SCHEMA,
    _build_model_switch_schema_overrides,
)


def test_model_switch_schema_exposes_max_reasoning_effort():
    schema = json.loads(json.dumps(_MODEL_SWITCH_SCHEMA))
    reasoning_efforts = schema["parameters"]["properties"]["reasoning_effort"]["enum"]

    assert "max" in reasoning_efforts


# ---------------------------------------------------------------------------
# ADR-003 Phase 3b: route-enum schema swap.
#
# These tests monkeypatch the catalog helper (agent.runtime_control.
# _route_catalog_pairs) instead of importing hermes_cli.model_routes, so they
# run — and must pass — on both the full stack AND the branch-alone
# runtime-control build (where the helper returns [] via ImportError).
# ---------------------------------------------------------------------------


def test_model_switch_schema_unchanged_when_no_routes_declared(monkeypatch):
    """Empty catalog -> overrides are {} and the registered definition stays
    byte-identical to the static free-form schema."""
    monkeypatch.setattr("agent.runtime_control._route_catalog_pairs", lambda: [])
    static_snapshot = copy.deepcopy(_MODEL_SWITCH_SCHEMA)

    assert _build_model_switch_schema_overrides() == {}

    from tools.registry import registry

    definitions = registry.get_definitions({"model_switch"})
    assert len(definitions) == 1
    schema = definitions[0]["function"]
    assert schema["description"] == static_snapshot["description"]
    assert schema["parameters"] == static_snapshot["parameters"]
    props = schema["parameters"]["properties"]
    assert "model" in props
    assert "provider" in props
    assert "route" not in props
    # Building overrides never mutates the static schema.
    assert _MODEL_SWITCH_SCHEMA == static_snapshot


def test_model_switch_schema_swaps_model_provider_for_route_enum(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_catalog_pairs",
        lambda: [("dev", "Deep coding and debugging"), ("chat", "Casual conversation")],
    )
    static_snapshot = copy.deepcopy(_MODEL_SWITCH_SCHEMA)

    overrides = _build_model_switch_schema_overrides()

    props = overrides["parameters"]["properties"]
    assert "model" not in props
    assert "provider" not in props
    assert props["route"]["enum"] == ["dev", "chat"]
    # Each route's purpose line is carried in the description.
    assert "dev: Deep coding and debugging" in props["route"]["description"]
    assert "chat: Casual conversation" in props["route"]["description"]
    # Effort-only self-adjustment stays available.
    assert "reasoning_effort" in props
    assert "max" in props["reasoning_effort"]["enum"]
    assert "reason" in props
    assert overrides["parameters"]["additionalProperties"] is False
    # The static schema is never mutated by building overrides.
    assert _MODEL_SWITCH_SCHEMA == static_snapshot


def test_model_switch_route_schema_flows_through_registry_definitions(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_catalog_pairs",
        lambda: [("dev", "Deep coding")],
    )

    from tools.registry import registry

    definitions = registry.get_definitions({"model_switch"})
    assert len(definitions) == 1
    schema = definitions[0]["function"]
    props = schema["parameters"]["properties"]
    assert props["route"]["enum"] == ["dev"]
    assert "model" not in props
    assert "provider" not in props
    assert "route" in schema["description"] or "route" in props["route"]["description"]


def test_model_status_schema_is_static():
    """model_status keeps its empty-params schema; route info is added to the
    tool OUTPUT (agent.runtime_control.model_status), never the schema."""
    from tools.runtime_control_tool import _MODEL_STATUS_SCHEMA

    assert _MODEL_STATUS_SCHEMA["parameters"] == {"type": "object", "properties": {}}
