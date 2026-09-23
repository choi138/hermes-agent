"""Automatic review is reserved for successful, primary foreground turns."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.background_review_policy import (
    is_primary_foreground_agent,
    is_successful_review_outcome,
)


def _agent(**overrides):
    values = {
        "_delegate_depth": 0,
        "platform": "discord",
        "_persist_disabled": False,
        "_memory_write_origin": "assistant_tool",
        "_memory_write_context": "foreground",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("overrides", [
    {"_delegate_depth": 1},
    {"platform": "subagent"},
    {"_persist_disabled": True},
    {"_memory_write_origin": "background_review"},
    {"_memory_write_context": "background_review"},
])
def test_internal_or_delegated_agents_are_not_foreground(overrides):
    assert not is_primary_foreground_agent(_agent(**overrides))


def test_primary_agent_is_foreground():
    assert is_primary_foreground_agent(_agent())


@pytest.mark.parametrize("reason", [
    "tool_persistence_failure",
    "guardrail_halt",
    "partial_stream_recovery",
    "all_retries_exhausted_no_response",
    "max_iterations_reached(60/60)",
    "fallback_prior_turn_content",
])
def test_abnormal_chat_exit_is_not_reviewable_even_with_text(reason):
    assert not is_successful_review_outcome(
        _agent(), final_response="fallback", completed=True, exit_reason=reason,
    )


def test_normal_chat_exit_is_reviewable():
    assert is_successful_review_outcome(
        _agent(), final_response="Done", completed=True,
        exit_reason="text_response(finish_reason=stop)",
    )


@pytest.mark.parametrize("fields", [
    {"completed": False},
    {"failed": True},
    {"interrupted": True},
    {"cleanup_failed": True},
    {"final_response": "  "},
])
def test_incomplete_outcome_is_not_reviewable(fields):
    values = {"final_response": "Done", "completed": True}
    values.update(fields)
    assert not is_successful_review_outcome(_agent(), **values)


def test_memory_nudge_stays_due_until_a_review_is_accepted():
    from agent.turn_context import _tick_memory_nudge

    agent = _agent(
        _memory_nudge_interval=2, _turns_since_memory=1,
        valid_tool_names={"memory"}, _memory_store=object(),
    )
    assert _tick_memory_nudge(agent)
    assert _tick_memory_nudge(agent)
    assert agent._turns_since_memory >= agent._memory_nudge_interval


def test_delegated_memory_nudge_does_not_advance_primary_cadence():
    from agent.turn_context import _tick_memory_nudge

    agent = _agent(
        _delegate_depth=1, _memory_nudge_interval=2, _turns_since_memory=1,
        valid_tool_names={"memory"}, _memory_store=object(),
    )
    assert not _tick_memory_nudge(agent)
    assert agent._turns_since_memory == 1


@pytest.mark.parametrize("turn_changes,agent_changes,expected_count", [
    ({"error": "provider failed"}, {}, 1),
    ({"interrupted": True}, {}, 1),
    ({"final_text": ""}, {}, 1),
    ({}, {"_delegate_depth": 1}, 1),
    ({}, {"skip_background_review": True}, 2),
])
def test_codex_runtime_does_not_review_unreviewable_turn(
    monkeypatch, turn_changes, agent_changes, expected_count,
):
    from agent import codex_runtime

    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *_: None)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *_, **__: {})
    spawned = Mock(return_value=True)
    agent = _agent(
        _skill_nudge_interval=2, _iters_since_skill=1,
        _turns_since_memory=2, valid_tool_names={"skill_manage"},
        _sync_external_memory_for_turn=Mock(), _spawn_background_review=spawned,
        skip_background_review=False,
    )
    for name, value in agent_changes.items():
        setattr(agent, name, value)
    turn = SimpleNamespace(tool_iterations=1, interrupted=False, error=None, final_text="Done")
    for name, value in turn_changes.items():
        setattr(turn, name, value)

    codex_runtime._finish_codex_turn(
        agent, turn, [{"role": "assistant", "content": "Done"}],
        original_user_message="Go", should_review_memory=True,
    )

    spawned.assert_not_called()
    assert agent._iters_since_skill == expected_count
    assert agent._turns_since_memory == 2


def test_codex_success_resets_cadence_only_after_review_acceptance(monkeypatch):
    from agent import codex_runtime

    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_compaction", lambda *_: None)
    monkeypatch.setattr(codex_runtime, "_record_codex_app_server_usage", lambda *_, **__: {})
    spawned = Mock(side_effect=[False, True])
    agent = _agent(
        _skill_nudge_interval=2, _iters_since_skill=1,
        _turns_since_memory=2, valid_tool_names={"skill_manage"},
        _sync_external_memory_for_turn=Mock(), _spawn_background_review=spawned,
        skip_background_review=False,
    )
    turn = SimpleNamespace(tool_iterations=1, interrupted=False, error=None, final_text="Done")

    for expected_count in (2, 0):
        codex_runtime._finish_codex_turn(
            agent, turn, [{"role": "assistant", "content": "Done"}],
            original_user_message="Go", should_review_memory=True,
        )
        assert agent._iters_since_skill == expected_count
    assert spawned.call_count == 2
    assert agent._turns_since_memory == 0
