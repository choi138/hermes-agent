"""Model control reaches the same live-agent dispatcher on both execution paths."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.runtime_control import dispatch_model_switch


@pytest.fixture
def agent():
    from run_agent import AIAgent

    definitions = [{
        "type": "function",
        "function": {"name": "model_switch", "parameters": {"type": "object", "properties": {}}},
    }]
    with (
        patch("run_agent.get_tool_definitions", return_value=definitions),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.invalid/v1",
            quiet_mode=True,
        )
        instance.client = MagicMock()
        yield instance


def test_sequential_and_invoke_paths_forward_complete_route_request(agent):
    from agent.agent_runtime_helpers import invoke_tool

    args = {"route": "dev", "reason": "coding task"}
    call = SimpleNamespace(
        id="call_runtime_control", type="function",
        function=SimpleNamespace(name="model_switch", arguments=json.dumps(args)),
    )
    message = SimpleNamespace(content="", tool_calls=[call], reasoning=None)
    seen = []

    def dispatch(current_agent, current_args):
        seen.append((current_agent, current_args))
        return json.dumps({"success": True})

    with patch("agent.runtime_control.dispatch_model_switch", dispatch):
        messages = []
        agent._execute_tool_calls_sequential(message, messages, "task-1")
        result = invoke_tool(agent, "model_switch", dict(args), "task-1")

    assert seen == [(agent, args), (agent, args)]
    assert json.loads(messages[-1]["content"])["success"] is True
    assert json.loads(result)["success"] is True


@pytest.mark.parametrize("raw_key", ["model", "provider", "reasoning_effort"])
def test_agent_dispatch_rejects_free_form_route_inputs(raw_key):
    result = json.loads(dispatch_model_switch(object(), {"route": "dev", raw_key: "stale-id"}))

    assert result["success"] is False
    assert raw_key in result["error"]
    assert "route" in result["error"]
