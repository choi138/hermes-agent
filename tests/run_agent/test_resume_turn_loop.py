"""End-to-end tests for run_conversation(resume_turn=True) — same-turn resume.

The gateway re-enters an interrupted turn on its persisted transcript after a
restart (ADR durable-turns).  These tests drive the REAL conversation loop
with a mocked OpenAI client to verify the resume semantics:

* no new user row is appended — the transcript tail IS the turn;
* the model is called with the interrupted tail (tool results last);
* synthetic "Operation interrupted…" closers are stripped before the call;
* a turn that had already composed its final answer is delivered without
  another model call;
* the recorded turn_id is adopted for the resumed turn.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tests.run_agent.test_run_agent import (  # reuse canonical helpers
    _make_tool_defs,
    _mock_response,
)


@pytest.fixture()
def agent():
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def _interrupted_tool_tail_history():
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    return [
        {"role": "user", "content": "run the report and summarize it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": "web_search",
            "content": "report data: 42",
        },
        {"role": "assistant", "content": "Operation interrupted."},
    ]


def _run_resume(agent, history, turn_id="sid:sid:deadbeef"):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(
            "",
            conversation_history=history,
            task_id="sid",
            resume_turn=True,
            turn_id=turn_id,
        )


class TestResumeTurnLoop:
    def test_resume_continues_from_tool_tail(self, agent):
        history = _interrupted_tool_tail_history()
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="The report says 42.")
        ]

        result = _run_resume(agent, history)

        assert result["completed"] is True
        assert result["final_response"] == "The report says 42."
        assert result["turn_id"] == "sid:sid:deadbeef"

        # The API call saw the interrupted tail — closer stripped, tool
        # result last, and NO new user message appended after it.
        kwargs = agent.client.chat.completions.create.call_args.kwargs
        sent = kwargs["messages"]
        non_system = [m for m in sent if m.get("role") != "system"]
        assert non_system[-1]["role"] == "tool"
        assert all(
            not (
                m.get("role") == "assistant"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("Operation interrupted")
            )
            for m in sent
        )
        # Exactly the one original user row.
        assert sum(1 for m in sent if m.get("role") == "user") == 1

    def test_resume_does_not_duplicate_user_row_in_result(self, agent):
        history = _interrupted_tool_tail_history()
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="Done.")
        ]

        result = _run_resume(agent, history)

        user_rows = [
            m for m in result["messages"] if m.get("role") == "user"
        ]
        assert len(user_rows) == 1
        assert user_rows[0]["content"] == "run the report and summarize it"

    def test_resume_delivers_composed_final_without_api_call(self, agent):
        history = [
            {"role": "user", "content": "summarize"},
            {"role": "assistant", "content": "Summary: everything passed."},
        ]

        result = _run_resume(agent, history)

        assert result["final_response"] == "Summary: everything passed."
        assert result["completed"] is True
        agent.client.chat.completions.create.assert_not_called()
        # No duplicate assistant row appended.
        assistant_rows = [
            m for m in result["messages"] if m.get("role") == "assistant"
        ]
        assert len(assistant_rows) == 1

    def test_resume_fills_dangling_side_effect_call_and_continues(self, agent):
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        history = [
            {"role": "user", "content": "restart the service"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
        ]
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="Verified: the service is back up.")
        ]

        result = _run_resume(agent, history)

        assert result["final_response"] == "Verified: the service is back up."
        sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
        orphan = [
            m for m in sent
            if m.get("role") == "tool" and m.get("tool_call_id") == call_id
        ]
        assert len(orphan) == 1
        assert "UNKNOWN" in orphan[0]["content"]

    def test_resume_does_not_advance_user_turn_count(self, agent):
        history = _interrupted_tool_tail_history()
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="ok")
        ]
        agent._user_turn_count = 7

        _run_resume(agent, history)

        # Hydration from history may not run (count already nonzero), but the
        # resume itself must not add a turn.
        assert agent._user_turn_count == 7

    def test_normal_turn_still_appends_user_row(self, agent):
        """Control: without resume_turn the behavior is unchanged."""
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="hi there")
        ]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")

        assert result["final_response"] == "hi there"
        sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
        assert sent[-1]["role"] == "user"
        assert sent[-1]["content"] == "hello"
