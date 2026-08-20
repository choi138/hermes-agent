from types import SimpleNamespace

from agent.conversation_loop import _apply_refusal_clean_fork
from agent.refusal_history import (
    current_user_ordinal_from_tail,
    user_anchor_from_tail,
)


def test_current_turn_anchor_is_bounded_and_ignores_synthetic_users():
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "partial"},
        {"role": "user", "content": "synthetic nudge"},
    ]
    ordinal = current_user_ordinal_from_tail(
        messages, 3, keep_user_turns=2,
    )
    assert ordinal == 2
    assert user_anchor_from_tail(
        messages, ordinal, keep_user_turns=2,
    ) == 3
    assert current_user_ordinal_from_tail(
        messages, 3, keep_user_turns=1,
    ) is None


def test_mid_turn_clean_fork_preserves_completed_history_and_drops_current_tail(
    monkeypatch,
):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": "earlier completed answer"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        {"role": "assistant", "content": "I cannot assist with that."},
    ]
    api_messages = [dict(message) for message in messages]
    statuses = []
    agent = SimpleNamespace(
        _session_messages=None,
        _refusal_clean_fork_active=False,
        _refusal_recall_quarantine=False,
        _buffer_status=statuses.append,
    )

    dropped = _apply_refusal_clean_fork(
        agent, messages, api_messages, current_turn_user_idx=3,
    )

    expected = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "earlier request"},
        {"role": "assistant", "content": "earlier completed answer"},
        {"role": "user", "content": "current request"},
    ]
    assert dropped == 3
    assert messages == expected
    assert api_messages == expected
    assert agent._session_messages is messages
    assert agent._refusal_clean_fork_active is True
    assert agent._refusal_recall_quarantine is True
    assert statuses == ["⚠️ Refusal fallback clean_fork=yes dropped=3"]


def test_clean_fork_fails_safe_when_current_anchor_is_invalid(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    messages = [
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "refusal"},
    ]
    api_messages = [dict(message) for message in messages]
    agent = SimpleNamespace(_buffer_status=lambda _message: None)

    assert _apply_refusal_clean_fork(
        agent, messages, api_messages, current_turn_user_idx=99,
    ) == 0
    assert messages[-1]["content"] == "refusal"
    assert api_messages[-1]["content"] == "refusal"
