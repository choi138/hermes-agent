"""Regression tests for send-time thinking-signature recovery."""

from agent.conversation_loop import _strip_anthropic_thinking_replay_channels


def _shallow_copies(messages):
    return [m.copy() for m in messages]


def test_pop_on_shallow_copy_does_not_affect_source():
    rd = [{"type": "thinking", "thinking": "r", "signature": "s"}]
    src = {"role": "assistant", "content": "x", "reasoning_details": rd}
    cp = src.copy()

    cp.pop("reasoning_details", None)

    assert "reasoning_details" not in cp
    assert "reasoning_details" in src
    assert src["reasoning_details"] is rd


def test_strip_api_messages_removes_both_channels_and_keeps_canonical_messages():
    rd_one = [{"type": "thinking", "thinking": "one", "signature": "sig_one"}]
    rd_two = [{"type": "thinking", "thinking": "two", "signature": "sig_two"}]
    ordered_one = [
        {"type": "thinking", "thinking": "one", "signature": "sig_one"},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
    ]
    ordered_two = [
        {"type": "thinking", "thinking": "two", "signature": "sig_two"},
    ]
    messages = [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": "a1",
            "reasoning_details": rd_one,
            "anthropic_content_blocks": ordered_one,
        },
        {"role": "user", "content": "q2"},
        {
            "role": "assistant",
            "content": "a2",
            "reasoning_details": rd_two,
            "anthropic_content_blocks": ordered_two,
        },
    ]
    api_messages = _shallow_copies(messages)

    stripped = _strip_anthropic_thinking_replay_channels(api_messages)

    assert stripped == 2
    assert all(
        "reasoning_details" not in m and "anthropic_content_blocks" not in m
        for m in api_messages
    )
    canonical_rd = [
        m.get("reasoning_details") for m in messages if m["role"] == "assistant"
    ]
    assert canonical_rd == [rd_one, rd_two]
    canonical_ordered = [
        m.get("anthropic_content_blocks")
        for m in messages
        if m["role"] == "assistant"
    ]
    assert canonical_ordered == [ordered_one, ordered_two]


def test_strip_is_idempotent_when_run_twice():
    api_messages = [
        {
            "role": "assistant",
            "content": "a",
            "reasoning_details": [{"x": 1}],
            "anthropic_content_blocks": [{"type": "thinking"}],
        },
        {"role": "user", "content": "q"},
    ]

    assert _strip_anthropic_thinking_replay_channels(api_messages) == 1
    assert _strip_anthropic_thinking_replay_channels(api_messages) == 0


def test_strip_skips_messages_without_replay_channels():
    api_messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "tool_call_id": "1", "content": "ok"},
    ]
    snapshot = [dict(m) for m in api_messages]

    assert _strip_anthropic_thinking_replay_channels(api_messages) == 0
    assert api_messages == snapshot
