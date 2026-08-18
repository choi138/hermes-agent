from types import SimpleNamespace

from agent.system_prompt import (
    build_runtime_route_block,
    compose_effective_system_prompt,
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


def test_runtime_route_block_reports_current_runtime_without_secrets():
    block = build_runtime_route_block(_agent())

    assert "# Runtime/Route State" in block
    assert (
        "CurrentRuntime: provider=codex-nekos model=gpt-5.5 reasoning=high "
        "api=codex_responses endpoint=https://example.com/v1 "
        "source=pre_gateway_dispatch" in block
    )
    assert "reasoning_source=pre_gateway_dispatch" in block
    assert "DesiredRoute:" not in block
    assert "model_status is diagnostic fallback only" in block
    assert "user:pass" not in block
    assert "api_key" not in block
    assert "secret" not in block


def test_runtime_route_block_renders_desired_route_state():
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

    assert "DesiredRoute: label=SYSTEM_DEV target=codex-nekos/gpt-5.5/high" in block
    assert "strictness=auto_reconsiderable" in block
    assert "confidence=0.91" in block
    assert "source=skill-gate/context-policy-router" in block
    assert 'reason="Hermes runtime work"' in block


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
