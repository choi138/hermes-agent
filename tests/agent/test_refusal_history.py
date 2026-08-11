from agent.refusal_history import clean_fork_messages


def test_clean_fork_keeps_leading_system_and_last_user_turns():
    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "second system"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "policy context"},
        {"role": "user", "content": "newer question"},
        {"role": "assistant", "content": "I cannot assist with that."},
        {"role": "user", "content": "latest question"},
    ]
    original = [dict(message) for message in messages]

    cleaned = clean_fork_messages(messages, keep_user_turns=2)

    assert cleaned == [
        {"role": "system", "content": "stable system"},
        {"role": "system", "content": "second system"},
        {"role": "user", "content": "newer question"},
        {"role": "user", "content": "latest question"},
    ]
    assert messages == original
    assert all(output is not source for output, source in zip(cleaned[:2], messages[:2]))


def test_clean_fork_is_empty_safe_and_drops_nonleading_system_messages():
    assert clean_fork_messages(None) == []
    assert clean_fork_messages([]) == []
    assert clean_fork_messages([
        {"role": "user", "content": "request"},
        {"role": "system", "content": "late system"},
        {"role": "assistant", "content": "refusal"},
    ]) == [{"role": "user", "content": "request"}]


def test_clean_fork_zero_user_limit_keeps_only_leading_system_messages():
    assert clean_fork_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
    ], keep_user_turns=0) == [{"role": "system", "content": "system"}]
