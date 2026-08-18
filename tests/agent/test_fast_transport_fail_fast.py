"""Fail fast on consecutive near-instant transport failures."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.turn_retry_state import TurnRetryState


def _make_agent():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="http://127.0.0.1:1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.compression_enabled = False
    return agent


def _install_transport_failure(monkeypatch, agent):
    import openai

    calls = {"count": 0}

    def _fail(*args, **kwargs):
        calls["count"] += 1
        raise openai.APIConnectionError(request=MagicMock())

    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", _fail)
    monkeypatch.setattr(agent, "_interruptible_api_call", _fail)
    monkeypatch.setattr(agent, "_has_pending_fallback", lambda: False)
    monkeypatch.setattr(agent, "_try_activate_fallback", lambda *args, **kwargs: False)
    monkeypatch.setattr("agent.conversation_loop.jittered_backoff", lambda *args, **kwargs: 0.0)
    return calls


def test_streak_field_defaults_to_zero():
    assert TurnRetryState().fast_transport_failures == 0


def test_fail_fast_terminates_after_streak(monkeypatch):
    monkeypatch.setenv("HERMES_FAST_CONN_FAIL_LIMIT", "3")
    agent = _make_agent()
    agent._api_max_retries = 12
    calls = _install_transport_failure(monkeypatch, agent)

    result = agent.run_conversation(user_message="hi")

    assert result["failed"] is True
    assert "unreachable" in result["error"].lower()
    assert calls["count"] <= 4


def test_env_zero_disables_fail_fast(monkeypatch):
    monkeypatch.setenv("HERMES_FAST_CONN_FAIL_LIMIT", "0")
    agent = _make_agent()
    agent._api_max_retries = 5
    calls = _install_transport_failure(monkeypatch, agent)

    result = agent.run_conversation(user_message="hi")

    assert result.get("failed") or result.get("error")
    assert calls["count"] >= 5


def test_slow_failures_do_not_trip_fail_fast(monkeypatch):
    import time as time_module

    monkeypatch.setenv("HERMES_FAST_CONN_FAIL_LIMIT", "3")
    agent = _make_agent()
    agent._api_max_retries = 5
    calls = _install_transport_failure(monkeypatch, agent)
    real_time = time_module.time
    clock = {"offset": 0.0}

    def _ticking_time():
        clock["offset"] += 1.6
        return real_time() + clock["offset"]

    monkeypatch.setattr("agent.conversation_loop.time.time", _ticking_time)

    agent.run_conversation(user_message="hi")

    assert calls["count"] >= 5
