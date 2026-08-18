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
)
