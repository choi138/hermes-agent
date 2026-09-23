"""Exercise resumed turns through the real AIAgent facade with a fake provider."""

from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tests.agent.test_run_agent import _make_tool_defs


def _answer(text):
    return MagicMock(
        choices=[MagicMock(message=MagicMock(content=text, tool_calls=None, reasoning_content=None),
                           finish_reason="stop")],
        usage=None,
    )


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        instance = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
        )
        instance.client = MagicMock()
        return instance


def _run_resume(agent, history):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(
            "", conversation_history=history, task_id="sid", resume_turn=True,
            turn_id="sid:sid:deadbeef",
        )


def test_resume_continues_tool_tail_without_new_user_row(agent):
    history = [
        {"role": "user", "content": "run report"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "42"},
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    agent.client.chat.completions.create.return_value = _answer("The report says 42.")
    agent._user_turn_count = 7

    result = _run_resume(agent, history)

    assert result["final_response"] == "The report says 42."
    assert result["turn_id"] == "sid:sid:deadbeef"
    assert agent._user_turn_count == 7
    assert sum(row.get("role") == "user" for row in result["messages"]) == 1
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert [row for row in sent if row.get("role") != "system"][-1]["role"] == "tool"


def test_composed_final_is_delivered_without_provider_call(agent):
    history = [
        {"role": "user", "content": "summarize"},
        {"role": "assistant", "content": "Summary ready."},
    ]
    result = _run_resume(agent, history)
    assert result["completed"] is True
    assert result["final_response"] == "Summary ready."
    assert sum(row.get("role") == "assistant" for row in result["messages"]) == 1
    agent.client.chat.completions.create.assert_not_called()


def test_resume_rejects_missing_persisted_user_turn(agent):
    with pytest.raises(ValueError, match="persisted user turn"):
        _run_resume(agent, [{"role": "assistant", "content": "orphan"}])
