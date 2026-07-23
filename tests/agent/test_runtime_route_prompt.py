from types import SimpleNamespace

import pytest

from agent.system_prompt import (
    build_runtime_route_block,
    compose_effective_system_prompt,
    format_routing_directive,
)


def _agent(**overrides):
    data = {
        "model": "gpt-5.5",
        "provider": "codex-nekos",
        "base_url": "https://user:pass@example.com/v1?api_key=secret#frag",
        "api_mode": "codex_responses",
        "reasoning_config": {"enabled": True, "effort": "high"},
        "_runtime_model_source": "pre_gateway_dispatch",
        "_runtime_reasoning_source": "pre_gateway_dispatch",
        "ephemeral_system_prompt": "EPHEMERAL",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture(autouse=True)
def _dormant_route_catalog(monkeypatch):
    """Keep the branch's no-catalog mode deterministic across environments."""
    monkeypatch.setattr("agent.runtime_control._route_status_info", lambda agent: None)


def test_runtime_route_block_reports_current_runtime_without_secrets():
    """Model-facing identity is model + reasoning when routes are dormant;
    raw provider/endpoint/api-mode values remain cache-key-only operator data."""
    block = build_runtime_route_block(_agent())

    assert "# Runtime/Route State" in block
    assert (
        "CurrentRuntime: model=gpt-5.5 reasoning=high "
        "source=pre_gateway_dispatch" in block
    )
    assert "route=" not in block
    assert "provider=" not in block
    assert "api=" not in block
    assert "endpoint=" not in block
    assert "reasoning_source=pre_gateway_dispatch" in block
    assert "DesiredRoute:" not in block
    assert "model_status is diagnostic fallback only" in block
    assert "user:pass" not in block
    assert "api_key" not in block
    assert "secret" not in block
    assert "example.com" not in block


def test_runtime_route_block_renders_current_route_when_catalog_matches(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_status_info",
        lambda agent: {"current": "SYSTEM_DEV", "available": []},
    )

    block = build_runtime_route_block(_agent())

    assert "CurrentRuntime: route=SYSTEM_DEV model=gpt-5.5" in block
    assert "DesiredRoute: label=UNCLASSIFIED target=current" in block


def test_runtime_route_block_marks_off_catalog_runtime(monkeypatch):
    monkeypatch.setattr(
        "agent.runtime_control._route_status_info",
        lambda agent: {"current": None, "available": [{"name": "dev"}]},
    )

    block = build_runtime_route_block(_agent())

    assert "CurrentRuntime: route=off-catalog model=gpt-5.5" in block
    assert "DesiredRoute: label=UNCLASSIFIED target=current" in block


def test_runtime_route_block_dormant_catalog_stays_current_runtime_only():
    agent = _agent(
        _runtime_route_state={
            "label": "SYSTEM_DEV",
            "target_provider": "codex-nekos",
            "target_model": "gpt-5.5",
            "target_reasoning_effort": "high",
            "strictness": "auto_reconsiderable",
            "confidence": 0.91,
            "source": "skill-gate/context-policy-router",
            "reason": "Hermes runtime work",
        }
    )

    block = build_runtime_route_block(agent)

    assert "DesiredRoute:" not in block
    assert "SYSTEM_DEV" not in block


def test_runtime_route_block_desired_route_is_permanently_static(monkeypatch):
    """With a declared catalog, current-turn route state must not move the
    system-prompt tail; it is delivered on the user message instead."""
    monkeypatch.setattr(
        "agent.runtime_control._route_status_info",
        lambda agent: {"current": None, "available": [{"name": "dev"}]},
    )
    route_state = {
        "label": "SYSTEM_DEV",
        "target_provider": "codex-nekos",
        "target_model": "gpt-5.5",
        "target_reasoning_effort": "high",
        "strictness": "auto_reconsiderable",
        "confidence": 0.91,
        "source": "skill-gate/context-policy-router",
        "reason": "Hermes runtime work",
    }

    routed = build_runtime_route_block(_agent(_runtime_route_state=route_state))
    unrouted = build_runtime_route_block(_agent(_runtime_route_state=None))

    assert routed == unrouted
    assert "DesiredRoute: label=UNCLASSIFIED target=current" in routed
    assert "SYSTEM_DEV" not in routed


def test_format_routing_directive_renders_route_state_for_user_message():
    directive = format_routing_directive(
        {
            "label": "SYSTEM_DEV",
            "target_provider": "codex-nekos",
            "target_model": "gpt-5.5",
            "target_reasoning_effort": "high",
            "strictness": "auto_reconsiderable",
            "confidence": 0.91,
            "source": "skill-gate/context-policy-router",
            "reason": "Hermes runtime work",
        }
    )

    assert directive.startswith(
        "[Routing directive: label=SYSTEM_DEV target=codex-nekos/gpt-5.5/high"
    )
    assert "strictness=auto_reconsiderable" in directive
    assert "confidence=0.91" in directive
    assert "source=skill-gate/context-policy-router" in directive
    assert 'reason="Hermes runtime work"' in directive
    assert directive.endswith("]")


def test_format_routing_directive_empty_for_no_state():
    assert format_routing_directive(None) == ""
    assert format_routing_directive({}) == ""


def test_compose_effective_system_prompt_appends_runtime_block_after_ephemeral():
    agent = _agent(ephemeral_system_prompt="EPHEMERAL")

    prompt = compose_effective_system_prompt(agent, "BASE")

    assert prompt.startswith("BASE\n\nEPHEMERAL\n\n# Runtime/Route State")
    assert "CurrentRuntime:" in prompt


def test_compose_effective_system_prompt_refreshes_live_runtime_each_call():
    agent = _agent(ephemeral_system_prompt=None)

    first = compose_effective_system_prompt(agent, "CACHED BASE")
    agent.model = "gpt-5.6"
    agent.reasoning_config = {"enabled": False}
    second = compose_effective_system_prompt(agent, "CACHED BASE")

    assert "model=gpt-5.5 reasoning=high" in first
    assert "model=gpt-5.6 reasoning=none" in second
    assert first.startswith("CACHED BASE\n\n# Runtime/Route State")
    assert second.startswith("CACHED BASE\n\n# Runtime/Route State")
