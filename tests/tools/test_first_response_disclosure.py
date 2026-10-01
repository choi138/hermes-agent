"""The staged YAML reduces provider schemas without weakening the bridge boundary."""

import copy
import json
import re
import socket
from pathlib import Path

import pytest
import yaml

from tools import tool_search as ts


ROOT = Path(__file__).resolve().parents[2]
EAGER = {
    "terminal", "read_file", "write_file", "patch", "search_files",
    "web_search", "web_extract", "skills_list", "skill_view", "skill_manage",
    "execute_code", "clarify", "delegate_task", "browser_navigate", "browser_snapshot",
    "browser_vault_list", "browser_vault_unlock", "browser_vault_fill",
    "browser_vault_save_login", "browser_vault_enter_code", "manage_connections",
}


def overlay_yaml():
    artifact = (ROOT / "profile-config" / "tool-search-overlay.md").read_text()
    blocks = re.findall(r"```yaml\n(.*?)\n```", artifact, re.S)
    assert len(blocks) == 1
    return yaml.safe_load(blocks[0])


def serialized(defs):
    return json.dumps(defs, ensure_ascii=False, separators=(",", ":")).encode()


def names(defs):
    return {td["function"]["name"] for td in defs}


@pytest.fixture
def production_assembly(tmp_path, monkeypatch):
    # Real imports, schemas, registry, selector, rewriters, assembly and bridge handler;
    # only configured-service readiness and side-effect handlers are offline fixtures.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import model_tools as model
    import hermes_cli.config as config
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    import tools.registry as registry_module
    from tools.registry import ToolRegistry

    def no_network(*args, **kwargs):
        raise AssertionError("offline disclosure test attempted network I/O")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    current = copy.deepcopy(DEFAULT_CONFIG)
    current["tools"]["tool_search"].update(overlay_yaml()["tools"]["tool_search"])
    monkeypatch.setattr(config, "load_config", lambda *a, **kw: copy.deepcopy(current))
    monkeypatch.setattr(config, "load_config_readonly", lambda *a, **kw: copy.deepcopy(current))
    monkeypatch.setattr(model, "_resolve_active_context_length", lambda: 128000)
    monkeypatch.setattr(model, "_tool_defs_cache", {})
    isolated = ToolRegistry()
    calls = []

    def control(args, **kwargs):
        calls.append(copy.deepcopy(args))
        return json.dumps({"success": True, "arguments": args})

    for entry in registry_module.registry.get_all_entries():
        if entry.name in ts.BRIDGE_TOOL_NAMES:
            continue
        isolated.register(
            name=entry.name, toolset=entry.toolset, schema=copy.deepcopy(entry.schema),
            handler=control, expose_to_model=entry.expose_to_model,
        )
    isolated.register(
        name="mcp__first_response__scoped_control", toolset="mcp-first-response",
        schema={"name": "mcp__first_response__scoped_control", "description": "Offline scoped control",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"], "additionalProperties": False}},
        handler=control,
    )
    monkeypatch.setattr(registry_module, "registry", isolated)
    monkeypatch.setattr(model, "registry", isolated)
    scopes = ["hermes-cli", "video", "video_gen", "mcp-first-response"]
    return model, current, scopes, calls, isolated


def test_exact_overlay_preserves_defaults_and_all_eager_safety_tools(production_assembly):
    model, current, scopes, calls, registry = production_assembly
    cfg = ts.ToolSearchConfig.from_raw(current["tools"]["tool_search"])
    assert ts._DEFAULT_DEFERRED_TOOLS <= cfg.effective_defer_tools
    assert not (cfg.effective_defer_tools & EAGER)
    safety = {e.name for e in registry.get_all_entries()
              if "vault" in e.name or "credential" in e.name}
    assert safety
    assert not (safety & cfg.effective_defer_tools)
    raw = model.get_tool_definitions(scopes, quiet_mode=True, skip_tool_search_assembly=True)
    added = cfg.effective_defer_tools - ts._DEFAULT_DEFERRED_TOOLS
    assert added <= names(raw), "Every newly deferred name must have a real production schema"
    after = ts.assemble_tool_defs(raw, context_length=128000, config=cfg)
    assert EAGER <= names(after.tool_defs)
    assert ts.BRIDGE_TOOL_NAMES <= names(after.tool_defs)
    assert safety & names(raw) <= names(after.tool_defs)


def test_actual_provider_array_catalog_and_describe_are_lossless(production_assembly, capsys):
    model, current, scopes, calls, registry = production_assembly
    raw = model.get_tool_definitions(scopes, quiet_mode=True, skip_tool_search_assembly=True)
    cfg = ts.ToolSearchConfig.from_raw(current["tools"]["tool_search"])
    before = ts.assemble_tool_defs(raw, context_length=128000, config=ts.ToolSearchConfig.from_raw(None))
    after = ts.assemble_tool_defs(raw, context_length=128000, config=cfg)
    actual = model.get_tool_definitions(scopes, quiet_mode=False)
    assert serialized(actual) == serialized(after.tool_defs)
    assert len(serialized(after.tool_defs)) < len(serialized(before.tool_defs))
    eager_before = names(before.tool_defs) - cfg.effective_defer_tools
    assert eager_before <= names(after.tool_defs)
    deferred = ts.classify_tools(raw, cfg.effective_defer_tools)[1]
    catalog = ts.build_catalog(deferred)
    assert {entry.name for entry in catalog} == names(deferred)
    by_name = {td["function"]["name"]: td["function"] for td in raw}
    listing = next(td["function"]["description"] for td in after.tool_defs
                   if td["function"]["name"] == ts.TOOL_SEARCH_NAME)
    for entry in catalog:
        assert entry.name in listing
        assert entry.schema["function"] == by_name[entry.name]
    # Describe ALL newly deferred tools, including nested schemas and full descriptions.
    for name in sorted(cfg.effective_defer_tools - ts._DEFAULT_DEFERRED_TOOLS):
        response = json.loads(model.handle_function_call(
            "tool_describe", {"names": [name]}, enabled_toolsets=scopes))
        assert response["tools"][name] == {
            "description": by_name[name]["description"],
            "parameters": by_name[name]["parameters"],
        }
    frozen = serialized(actual)
    for _ in range(3):
        model.handle_function_call("tool_search", {"queries": ["browser scroll"]}, enabled_toolsets=scopes)
        model.handle_function_call("tool_describe", {"names": ["browser_scroll"]}, enabled_toolsets=scopes)
        assert serialized(model.get_tool_definitions(scopes, quiet_mode=False)) == frozen
        assert {e.name for e in ts.build_catalog(ts.classify_tools(raw, cfg.effective_defer_tools)[1])} == names(deferred)
    metrics = {"raw_schema_bytes": len(serialized(raw)),
               "before_provider_bytes": len(serialized(before.tool_defs)),
               "after_provider_bytes": len(serialized(after.tool_defs)),
               "raw_count": len(raw), "before_provider_count": len(before.tool_defs),
               "after_provider_count": len(after.tool_defs), "catalog_count": len(catalog),
               "listing_form": after.listing_form}
    with capsys.disabled():
        print("FIRST_RESPONSE_SCHEMA_METRICS " + json.dumps(metrics, sort_keys=True))


def test_real_bridge_scoped_control_and_argument_rejection(production_assembly):
    model, current, scopes, calls, registry = production_assembly
    name = "mcp__first_response__scoped_control"
    payload = {"calls": [{"name": name, "arguments": {"value": "fixture"}}]}
    result = json.loads(model.handle_function_call("tool_call", payload, enabled_toolsets=scopes))
    assert result["success"] is True
    assert calls == [{"value": "fixture"}]
    for arguments in [{}, {"value": 7}, {"value": "fixture", "unexpected": True}]:
        invalid = {"calls": [{"name": name, "arguments": arguments}]}
        rejected = json.loads(model.handle_function_call("tool_call", invalid, enabled_toolsets=scopes))
        assert "error" in rejected
        assert calls == [{"value": "fixture"}]
    for enabled, disabled in [(["browser"], None), (scopes, ["mcp-first-response"]), (scopes, [name])]:
        rejected = json.loads(model.handle_function_call(
            "tool_call", payload, enabled_toolsets=enabled, disabled_toolsets=disabled))
        assert "not available in this session" in rejected["error"]
        described = json.loads(model.handle_function_call(
            "tool_describe", {"names": [name]}, enabled_toolsets=enabled, disabled_toolsets=disabled))
        assert name in described["not_found"]
        assert calls == [{"value": "fixture"}]
    browser_args = {"direction": "down"}
    result = json.loads(model.handle_function_call(
        "tool_call", {"calls": [{"name": "browser_scroll", "arguments": browser_args}]},
        enabled_toolsets=["browser"]))
    assert result["success"] is True
    assert calls[-1] == browser_args
    missing = json.loads(model.handle_function_call(
        "tool_call", {"calls": [{"name": "browser_scroll", "arguments": {"direction": "sideways"}}]},
        enabled_toolsets=["browser"]))
    assert "error" in missing
    assert len(calls) == 2
