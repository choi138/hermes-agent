"""Production-boundary coverage for main-loop API-call accounting."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from run_agent import AIAgent


def _tool_defs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def _response_without_usage():
    message = SimpleNamespace(
        content="done",
        reasoning=None,
        reasoning_content=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def test_successful_response_without_usage_persists_one_api_call(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="test/model",
            session_id="no-usage-call",
            session_db=db,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = _response_without_usage()
    agent._cached_system_prompt = "You are helpful."
    agent._disable_streaming = True
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False

    result = agent.run_conversation("hello")
    session = db.get_session(agent.session_id)
    model_usage = db._conn.execute(
        "SELECT api_call_count FROM session_model_usage WHERE session_id = ?",
        (agent.session_id,),
    ).fetchone()

    assert result["final_response"] == "done"
    assert agent.session_api_calls == 1
    assert session["api_call_count"] == 1
    assert model_usage["api_call_count"] == 1

    db.close()
