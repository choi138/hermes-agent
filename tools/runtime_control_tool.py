"""Agent model control tool schemas.

Execution is intentionally intercepted by the agent loop because these tools
need live AIAgent state.  Handlers here are defensive stubs only.
"""

from __future__ import annotations

import json

from tools.registry import registry

_MODEL_STATUS_SCHEMA = {
    "name": "model_status",
    "description": (
        "Inspect the current agent model state: model, provider, API mode, session, "
        "and reasoning_effort. Secret values such as API keys are never returned. "
        "Use this when you need to know your effective model/runtime before deciding "
        "whether to switch."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

_MODEL_SWITCH_SCHEMA = {
    "name": "model_switch",
    "description": (
        "Switch the current agent model/runtime for the current session. "
        "Can change model/provider and/or reasoning_effort. "
        "All switches are session-scoped and persist until /new. "
        "Global config changes are intentionally unsupported."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": "Optional target model name/alias. Omit to keep the current model.",
            },
            "provider": {
                "type": "string",
                "description": "Optional target provider slug. Omit to keep or infer the provider.",
            },
            "reasoning_effort": {
                "type": "string",
                "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                "description": "Optional reasoning level to apply.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation for why the runtime switch is needed.",
            },
        },
        "additionalProperties": False,
    },
}


def _build_model_switch_schema_overrides() -> dict:
    """Swap free-form model/provider for a route enum when routes are declared.

    ADR-003 Phase 3b: when the config declares at least one valid
    ``model_routes`` route, the agent switches by PURPOSE — the ``model`` /
    ``provider`` params are removed and replaced by a ``route`` enum whose
    description carries each route's purpose line.  ``reasoning_effort``
    stays (effort-only self-adjustment remains legitimate; an explicit effort
    wins over the route's default).

    With no declared routes — or on builds without the model_routes
    subsystem (the catalog helper returns ``[]`` on ImportError) — this
    returns ``{}`` so the registered schema stays byte-identical to the
    static free-form shape.
    """
    try:
        from agent.runtime_control import _route_catalog_pairs

        pairs = _route_catalog_pairs()
    except Exception:
        pairs = []
    if not pairs:
        return {}

    parameters = {**_MODEL_SWITCH_SCHEMA["parameters"]}
    properties = {
        key: dict(value)
        for key, value in _MODEL_SWITCH_SCHEMA["parameters"]["properties"].items()
        if key not in ("model", "provider")
    }
    catalog_lines = "\n".join(
        f"{name}: {description}" if description else name for name, description in pairs
    )
    route_param = {
        "type": "string",
        "enum": [name for name, _ in pairs],
        "description": (
            "Target route (switch by purpose). Each route maps to a "
            "config-declared provider/model with health-checked fallbacks:\n"
            + catalog_lines
        ),
    }
    properties["reasoning_effort"] = {
        **properties["reasoning_effort"],
        "description": (
            "Optional reasoning level to apply. Overrides the selected "
            "route's default effort."
        ),
    }
    parameters["properties"] = {"route": route_param, **properties}
    description = (
        "Switch the current agent model/runtime for the current session by "
        "selecting a declared route (purpose-based; the route picks the "
        "provider/model) and/or adjusting reasoning_effort. "
        "All switches are session-scoped and persist until /new. "
        "Global config changes are intentionally unsupported."
    )
    return {"description": description, "parameters": parameters}


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
    dynamic_schema_overrides=_build_model_switch_schema_overrides,
)
