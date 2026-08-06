from types import SimpleNamespace

import pytest

from agent.background_review_policy import (
    is_primary_foreground_agent,
    is_successful_review_outcome,
)


def _agent(**overrides):
    values = {
        "_delegate_depth": 0,
        "platform": "telegram",
        "_persist_disabled": False,
        "_memory_write_origin": "assistant_tool",
        "_memory_write_context": "foreground",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"_delegate_depth": 1},
        {"platform": "subagent"},
        {"_persist_disabled": True},
        {"_memory_write_origin": "background_review"},
        {"_memory_write_context": "background_review"},
    ],
)
def test_internal_and_delegated_agents_are_not_foreground(overrides):
    assert is_primary_foreground_agent(_agent(**overrides)) is False


def test_primary_user_facing_agent_is_foreground():
    assert is_primary_foreground_agent(_agent()) is True


@pytest.mark.parametrize(
    "reason",
    [
        "tool_persistence_failure",
        "guardrail_halt",
        "partial_stream_recovery",
        "all_retries_exhausted_no_response",
        "max_iterations_reached(60/60)",
        "fallback_prior_turn_content",
    ],
)
def test_abnormal_chat_exit_is_not_reviewable_even_with_text(reason):
    assert not is_successful_review_outcome(
        _agent(),
        final_response="A short fallback response",
        completed=True,
        exit_reason=reason,
    )


def test_normal_chat_exit_is_reviewable():
    assert is_successful_review_outcome(
        _agent(),
        final_response="Done.",
        completed=True,
        exit_reason="text_response(finish_reason=stop)",
    )


def test_codex_error_and_interrupt_are_not_reviewable():
    assert not is_successful_review_outcome(
        _agent(),
        final_response="partial",
        completed=False,
        failed=True,
    )
    assert not is_successful_review_outcome(
        _agent(),
        final_response="partial",
        completed=False,
        interrupted=True,
    )
