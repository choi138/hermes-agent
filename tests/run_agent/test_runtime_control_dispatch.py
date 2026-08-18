"""ADR-003 Phase 3c: the model_switch tool-dispatch layer.

The Phase 3b `route` parameter shipped broken because the two executors each
hand-listed the kwargs they forwarded to ``model_switch`` and the sequential
executor's list was stale — and no test exercised the dispatch layer (the
Phase 3b suite called the backend directly). These tests pin the single
dispatch entry point and both executor paths so that class of drift fails CI.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.runtime_control import dispatch_model_switch


class DummyAgent:
    pass


# ---------------------------------------------------------------------------
# dispatch_model_switch: route-only LLM boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("knob", ["model", "provider", "reasoning_effort"])
def test_dispatch_rejects_legacy_knobs_with_teaching_error(knob):
    """Raw ids / effort are catalog decisions; the error must teach `route`,
    not silently drop the argument."""
    data = json.loads(dispatch_model_switch(DummyAgent(), {knob: "gpt-4o", "route": "dev"}))

    assert data["success"] is False
    assert knob in data["error"]
    assert "route" in data["error"]


def test_dispatch_rejects_combined_legacy_knobs():
    data = json.loads(
        dispatch_model_switch(DummyAgent(), {"model": "o1", "provider": "openai"})
    )

    assert data["success"] is False
    assert "model" in data["error"] and "provider" in data["error"]


def test_dispatch_requires_route():
    data = json.loads(dispatch_model_switch(DummyAgent(), {"reason": "just because"}))

    assert data["success"] is False
    assert "route" in data["error"]


def test_dispatch_forwards_route_and_reason_to_backend():
    agent = DummyAgent()
    with patch(
        "agent.runtime_control.model_switch",
        return_value=json.dumps({"success": True}),
    ) as backend:
        dispatch_model_switch(agent, {"route": "dev", "reason": "coding task"})

    backend.assert_called_once_with(agent, route="dev", reason="coding task")


def test_dispatch_tolerates_non_dict_args():
    data = json.loads(dispatch_model_switch(DummyAgent(), None))

    assert data["success"] is False
    assert "route" in data["error"]


# ---------------------------------------------------------------------------
# Executor integration: both executors must hand the WHOLE parsed args dict
# to dispatch_model_switch (no per-executor kwarg lists to go stale).
# ---------------------------------------------------------------------------


def _tool_call(arguments: str):
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name="model_switch", arguments=arguments),
    )


def _assistant_msg(tool_calls):
    return SimpleNamespace(content="", tool_calls=tool_calls, reasoning=None)


@pytest.fixture()
def real_agent():
    from unittest.mock import MagicMock

    from run_agent import AIAgent

    def _make_tool_defs():
        return [
            {
                "type": "function",
                "function": {
                    "name": "model_switch",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
        )
        a.client = MagicMock()
        yield a


def test_sequential_executor_routes_through_dispatch(real_agent):
    args = {"route": "dev", "reason": "coding task"}
    msg = _assistant_msg([_tool_call(json.dumps(args))])
    messages = []
    captured = []

    def _fake_dispatch(agent_obj, function_args):
        captured.append((agent_obj, function_args))
        return json.dumps({"success": True, "route": {"name": "dev"}})

    with patch("agent.runtime_control.dispatch_model_switch", _fake_dispatch):
        real_agent._execute_tool_calls_sequential(msg, messages, "task-1")

    assert len(captured) == 1
    assert captured[0][0] is real_agent
    assert captured[0][1] == args
    assert messages[-1]["role"] == "tool"
    assert json.loads(messages[-1]["content"])["success"] is True


def test_invoke_tool_path_routes_through_dispatch(real_agent):
    from agent.agent_runtime_helpers import invoke_tool

    args = {"route": "chat", "reason": "wrap-up"}
    captured = []

    def _fake_dispatch(agent_obj, function_args):
        captured.append((agent_obj, function_args))
        return json.dumps({"success": True})

    with patch("agent.runtime_control.dispatch_model_switch", _fake_dispatch):
        result = invoke_tool(real_agent, "model_switch", dict(args), "task-1")

    assert len(captured) == 1
    assert captured[0][0] is real_agent
    assert captured[0][1] == args
    assert json.loads(result)["success"] is True
