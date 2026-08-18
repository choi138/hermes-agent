"""LLM activity recaps for long-running gateway notifications."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from gateway.run import GatewayRunner


class _Agent:
    model = "m"
    provider = "p"
    base_url = "http://x"
    api_key = "k"
    api_mode = "chat_completions"

    def __init__(self, context):
        self.context = context

    def get_activity_recap_context(self):
        return dict(self.context)


def _runner():
    return GatewayRunner.__new__(GatewayRunner)


def _response(text):
    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


CONTEXT = {
    "goal": "apifuse provider 10개 추가",
    "recent_tools": ["terminal: pytest ...", "patch: providers.py"],
    "voice_samples": ["테스트 돌려놓고 결과 보는 중이야"],
    "persona_snippet": "",
    "last_tool_result": "3 passed",
    "current_tool": "terminal",
    "seconds_since_activity": 45,
    "last_activity_desc": "running pytest",
    "iteration": 12,
    "max_iterations": 300,
}


def test_generates_on_compression_rail_and_caches_unchanged_context():
    runner = _runner()
    agent = _Agent(CONTEXT)
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response("pytest 실행 결과 확인 중"),
    ) as call_llm:
        first = asyncio.run(runner._llm_activity_recap(agent, "session"))
        second = asyncio.run(runner._llm_activity_recap(agent, "session"))

    assert first == "pytest 실행 결과 확인 중"
    assert second == first
    assert call_llm.call_count == 1
    kwargs = call_llm.call_args.kwargs
    assert kwargs["task"] == "activity_recap"
    assert kwargs["max_tokens"] == 80
    assert kwargs["timeout"] == 8
    assert "테스트 돌려놓고 결과 보는 중이야" in kwargs["messages"][0]["content"]


def test_regenerates_when_current_activity_changes():
    runner = _runner()
    agent = _Agent(CONTEXT)
    with patch(
        "agent.auxiliary_client.call_llm", return_value=_response("working")
    ) as call_llm:
        asyncio.run(runner._llm_activity_recap(agent, "session"))
        agent.context = dict(CONTEXT, current_tool="patch")
        asyncio.run(runner._llm_activity_recap(agent, "session"))

    assert call_llm.call_count == 2


def test_fresh_session_uses_persona_definition_as_voice():
    runner = _runner()
    context = dict(CONTEXT, voice_samples=[], persona_snippet="You are Ada. Be warm.")
    agent = _Agent(context)
    with patch(
        "agent.auxiliary_client.call_llm", return_value=_response("Still checking")
    ) as call_llm:
        asyncio.run(runner._llm_activity_recap(agent, "session"))

    prompt = call_llm.call_args.kwargs["messages"][0]["content"]
    assert "persona and conversation-style definition" in prompt
    assert "You are Ada. Be warm." in prompt


def test_generation_failure_returns_none_for_terse_fallback():
    runner = _runner()
    agent = _Agent(CONTEXT)
    with patch(
        "agent.auxiliary_client.call_llm", side_effect=RuntimeError("aux down")
    ):
        assert asyncio.run(runner._llm_activity_recap(agent, "session")) is None


def test_multiline_and_length_are_clamped():
    runner = _runner()
    agent = _Agent(CONTEXT)
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response("x" * 300 + "\nsecond line"),
    ):
        line = asyncio.run(runner._llm_activity_recap(agent, "session"))

    assert line == "x" * 140


def test_context_failure_returns_none():
    runner = _runner()
    agent = MagicMock()
    agent.get_activity_recap_context.side_effect = RuntimeError

    assert asyncio.run(runner._llm_activity_recap(agent, "session")) is None


def test_recap_mode_survives_display_config_normalisation():
    from gateway.display_config import resolve_display_setting

    config = {
        "display": {
            "platforms": {"whatsapp": {"long_running_notifications": "recap"}}
        }
    }
    assert (
        resolve_display_setting(config, "whatsapp", "long_running_notifications")
        == "recap"
    )

    other = {
        "display": {"platforms": {"whatsapp": {"thinking_progress": "recap"}}}
    }
    assert resolve_display_setting(other, "whatsapp", "thinking_progress") is False
