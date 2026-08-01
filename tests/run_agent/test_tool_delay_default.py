"""Compatibility contracts for the removed sequential-tool delay."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _make_agent(**kwargs) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            **kwargs,
        )
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    return agent


def _run_sequential_batch(agent: AIAgent):
    tool_calls = [
        SimpleNamespace(
            id=f"call-{index}",
            type="function",
            function=SimpleNamespace(name="web_search", arguments="{}"),
        )
        for index in range(3)
    ]
    assistant_message = SimpleNamespace(content="", tool_calls=tool_calls)
    messages: list = []

    with (
        patch("run_agent.handle_function_call", return_value="ok") as dispatch,
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **call_kwargs: call_kwargs["content"],
        ),
        patch("agent.tool_executor.time.sleep") as sleep,
    ):
        agent._execute_tool_calls_sequential(
            assistant_message,
            messages,
            "task-1",
        )

    return dispatch, sleep


def test_sequential_tool_delay_is_disabled_by_default():
    agent = _make_agent()

    dispatch, sleep = _run_sequential_batch(agent)

    assert not hasattr(agent, "tool_delay")
    assert dispatch.call_count == 3
    sleep.assert_not_called()


def test_explicit_sequential_tool_delay_is_deprecated_and_ignored():
    with pytest.warns(DeprecationWarning, match="tool_delay"):
        agent = _make_agent(tool_delay=0.25)

    dispatch, sleep = _run_sequential_batch(agent)

    assert not hasattr(agent, "tool_delay")
    assert dispatch.call_count == 3
    sleep.assert_not_called()
