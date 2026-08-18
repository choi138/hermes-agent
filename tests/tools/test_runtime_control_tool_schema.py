import copy

from tools.registry import invalidate_check_fn_cache, registry
from tools.runtime_control_tool import (
    _MODEL_SWITCH_SCHEMA,
    _build_model_switch_schema_overrides,
    _model_switch_available,
)


# ---------------------------------------------------------------------------
# ADR-003 Phase 3c: route-only switch contract.
#
# These tests monkeypatch the catalog helper (agent.runtime_control.
# _route_catalog_pairs) instead of importing hermes_cli.model_routes, so they
# run — and must pass — on both the full stack AND the branch-alone
# runtime-control build (where the helper returns [] via ImportError).
# ---------------------------------------------------------------------------


def test_model_switch_static_schema_is_route_only():
    """The LLM-facing switch surface is route + reason, nothing else.

    Raw model/provider ids and reasoning effort are catalog (config SoT)
    decisions — exposing them invites stale-model bias.
    """
    props = _MODEL_SWITCH_SCHEMA["parameters"]["properties"]
    assert set(props) == {"route", "reason"}
    assert _MODEL_SWITCH_SCHEMA["parameters"]["required"] == ["route"]
    assert _MODEL_SWITCH_SCHEMA["parameters"]["additionalProperties"] is False


def test_model_switch_hidden_when_no_routes_declared(monkeypatch):
    """Empty catalog -> overrides are {} and check_fn hides the tool entirely
    (dormant), instead of degrading to free-form model/provider switching."""
    monkeypatch.setattr("agent.runtime_control._route_catalog_pairs", lambda: [])
    invalidate_check_fn_cache()

    assert _build_model_switch_schema_overrides() == {}
    assert _model_switch_available() is False
    assert registry.get_definitions({"model_switch"}) == []

    invalidate_check_fn_cache()


def test_model_switch_route_enum_injected(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_catalog_pairs",
        lambda: [("dev", "Deep coding and debugging"), ("chat", "Casual conversation")],
    )
    static_snapshot = copy.deepcopy(_MODEL_SWITCH_SCHEMA)

    overrides = _build_model_switch_schema_overrides()

    props = overrides["parameters"]["properties"]
    assert set(props) == {"route", "reason"}
    assert props["route"]["enum"] == ["dev", "chat"]
    # Each route's purpose line is carried in the description.
    assert "dev: Deep coding and debugging" in props["route"]["description"]
    assert "chat: Casual conversation" in props["route"]["description"]
    assert overrides["parameters"]["required"] == ["route"]
    assert overrides["parameters"]["additionalProperties"] is False
    # The static schema is never mutated by building overrides.
    assert _MODEL_SWITCH_SCHEMA == static_snapshot


def test_model_switch_route_schema_flows_through_registry_definitions(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_catalog_pairs",
        lambda: [("dev", "Deep coding")],
    )
    invalidate_check_fn_cache()

    definitions = registry.get_definitions({"model_switch"})
    assert len(definitions) == 1
    schema = definitions[0]["function"]
    props = schema["parameters"]["properties"]
    assert props["route"]["enum"] == ["dev"]
    assert set(props) == {"route", "reason"}

    invalidate_check_fn_cache()


def test_dispatch_forwarding_matches_schema():
    """Parity pin: every schema property is exactly the dispatch forward set.

    The Phase 3b `route` omission shipped because two executors hand-listed
    the forwarded kwargs; forwarding now lives in ONE place
    (agent.runtime_control.dispatch_model_switch) and this test pins it to
    the registered schema so schema/executor drift fails CI.
    """
    from agent.runtime_control import (
        _MODEL_SWITCH_FORWARD_KEYS,
        _MODEL_SWITCH_REJECTED_KEYS,
    )

    props = set(_MODEL_SWITCH_SCHEMA["parameters"]["properties"])
    assert props == set(_MODEL_SWITCH_FORWARD_KEYS)
    # The legacy free-form knobs must stay actively rejected (teaching error),
    # not silently dropped — and must never leak back into the schema.
    assert set(_MODEL_SWITCH_REJECTED_KEYS) == {"model", "provider", "reasoning_effort"}
    assert props.isdisjoint(_MODEL_SWITCH_REJECTED_KEYS)


def test_model_status_schema_is_static():
    """model_status keeps its empty-params schema; route info is added to the
    tool OUTPUT (agent.runtime_control.model_status), never the schema."""
    from tools.runtime_control_tool import _MODEL_STATUS_SCHEMA

    assert _MODEL_STATUS_SCHEMA["parameters"] == {"type": "object", "properties": {}}
