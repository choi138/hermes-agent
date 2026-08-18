"""Agent model control tool schemas.

Execution is intentionally intercepted by the agent loop because these tools
need live AIAgent state.  Handlers here are defensive stubs only.

Route-only contract (ADR-003 Phase 3c): the LLM-facing switch surface is a
declared ``model_routes`` route (purpose category) plus a free-text reason.
Provider ids, model ids, and reasoning effort are deliberately NOT model
inputs — the benchmark-informed choice of model and effort lives in the
route catalog (config SoT), and exposing raw ids only invites stale-model
bias (a model that predates the catalog asking for last year's flagship).
With no declared routes the switch tool goes dormant (``check_fn`` returns
False, so it is dropped from tool definitions) instead of degrading to
free-form model/provider switching.
"""

from __future__ import annotations

import json

from tools.registry import registry


def _route_catalog_pairs_safe() -> list:
    """(name, description) pairs for declared routes; [] when absent/dormant."""
    try:
        from agent.runtime_control import _route_catalog_pairs

        return _route_catalog_pairs()
    except Exception:
        return []


_MODEL_STATUS_SCHEMA = {
    "name": "model_status",
    "description": (
        "Inspect the current agent runtime: active route (purpose category), "
        "model, and reasoning state. Secret values such as API keys are never "
        "returned. Use this when you need to know your effective runtime "
        "before deciding whether to switch routes."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

_MODEL_SWITCH_SCHEMA = {
    "name": "model_switch",
    "description": (
        "Switch the current agent runtime by selecting a declared route "
        "(purpose category). The route picks the provider, model, and "
        "reasoning effort from the config-declared catalog with "
        "health-checked fallbacks — these are not model inputs. "
        "All switches are session-scoped and persist until /new. "
        "Global config changes are intentionally unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "description": (
                    "Target route (purpose category) from the declared catalog."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Short explanation for why the route switch is needed.",
            },
        },
        "required": ["route"],
        "additionalProperties": False,
    },
}


def _build_model_switch_schema_overrides() -> dict:
    """Inject the declared route enum + purpose catalog at definitions time.

    The static schema already has the route-only shape; this override fills
    in the live enum values and per-route purpose lines so the model picks
    from the actual catalog.  With no declared routes it returns ``{}`` —
    the tool is already hidden by ``check_fn`` in that case, so the static
    shape is never served with an empty catalog.
    """
    pairs = _route_catalog_pairs_safe()
    if not pairs:
        return {}

    catalog_lines = "\n".join(
        f"{name}: {description}" if description else name for name, description in pairs
    )
    parameters = {**_MODEL_SWITCH_SCHEMA["parameters"]}
    properties = {
        key: dict(value)
        for key, value in _MODEL_SWITCH_SCHEMA["parameters"]["properties"].items()
    }
    properties["route"] = {
        "type": "string",
        "enum": [name for name, _ in pairs],
        "description": (
            "Target route (switch by purpose). Each route maps to a "
            "config-declared provider/model/effort with health-checked "
            "fallbacks:\n" + catalog_lines
        ),
    }
    parameters["properties"] = properties
    return {"parameters": parameters}


def _model_switch_available() -> bool:
    """model_switch is served only when the config declares routes."""
    return bool(_route_catalog_pairs_safe())


def _agent_loop_only(*_args, **_kwargs) -> str:
    return json.dumps({"error": "model control tools must be handled by the agent loop"})


registry.register(
    name="model_status",
    toolset="runtime",
    schema=_MODEL_STATUS_SCHEMA,
    handler=lambda args, **kwargs: _agent_loop_only(),
)

registry.register(
    name="model_switch",
    toolset="runtime",
    schema=_MODEL_SWITCH_SCHEMA,
    handler=lambda args, **kwargs: _agent_loop_only(),
    check_fn=_model_switch_available,
    dynamic_schema_overrides=_build_model_switch_schema_overrides,
)
