"""Reachable fallback and live switch regression: no real provider requests."""
import json
from unittest.mock import MagicMock, patch

import pytest

from agent.runtime_control import model_status
from agent.transports.codex import ResponsesApiTransport
from run_agent import AIAgent


def make_agent(destination):
    with patch("run_agent.get_tool_definitions", return_value=[]), patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"), patch("agent.model_metadata.get_model_context_length", return_value=128000):
        return AIAgent(model="gpt-6-astra", provider="openrouter", api_key="test-only",
            base_url="https://openrouter.ai/api/v1", quiet_mode=True, skip_context_files=True, skip_memory=True,
            reasoning_config={"enabled": True, "effort": "max", "selection": "pinned"},
            fallback_model=[{"provider": "custom", "model": destination, "base_url": "https://fallback.example/v1",
                             "api_key": "test-only", "api_mode": "codex_responses"}])


@pytest.mark.parametrize("destination", ["gpt-6-astra", "gpt-5.5"])
def test_reachable_fallback_preserves_pin_over_global_high(destination):
    agent = make_agent(destination)
    client = MagicMock(base_url="https://fallback.example/v1", api_key="test-only")
    with patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None), \
         patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, destination)), \
         patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m), \
         patch("hermes_cli.config.load_config", return_value={"agent": {"reasoning_effort": "high"}}):
        assert agent._try_activate_fallback() is True
    assert agent.model == destination
    assert agent.reasoning_config == {"enabled": True, "effort": "max", "selection": "pinned"}
    transport = ResponsesApiTransport()
    if destination == "gpt-5.5":
        assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"
        with pytest.raises(ValueError, match="[Pp]inned"):
            transport.build_kwargs(model=agent.model, messages=[], tools=[], reasoning_config=agent.reasoning_config)
    else:
        payload = transport.build_kwargs(model=agent.model, messages=[], tools=[], reasoning_config=agent.reasoning_config)
        assert json.loads(json.dumps(payload))["reasoning"]["effort"] == "max"
    with patch.object(agent, "_create_openai_client", return_value=MagicMock()), \
         patch("agent.credential_pool.load_pool", return_value=None):
        assert agent._restore_primary_runtime() is True
    assert agent.model == "gpt-6-astra"
    assert agent.reasoning_config == {"enabled": True, "effort": "max", "selection": "pinned"}


def test_live_model_switch_preserves_pin_and_primary_restore():
    agent = make_agent("gpt-6-astra")
    with patch("hermes_cli.config.load_config", return_value={"agent": {"reasoning_effort": "high"}}), \
         patch.object(agent, "_create_openai_client", return_value=MagicMock()), \
         patch("agent.model_metadata.get_model_context_length", return_value=128000):
        agent.switch_model("gpt-6-astra", "custom", api_key="test-only", base_url="https://fallback.example/v1", api_mode="codex_responses")
    assert agent.reasoning_config["effort"] == "max"
    assert agent._primary_runtime["reasoning_config"]["selection"] == "pinned"
