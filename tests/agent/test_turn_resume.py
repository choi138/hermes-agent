"""Same-turn restart normalization keeps the original user turn and side-effect safety."""

from agent.turn_resume import is_interrupt_closer_message, prepare_resume_history


def test_tool_tail_drops_only_synthetic_closer():
    history = [
        {"role": "user", "content": "run report"},
        {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "42"},
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["role"] == "tool"
    assert len(history) == 3


def test_dangling_side_effecting_call_gets_unknown_result():
    history = [
        {"role": "user", "content": "restart service"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
        ]},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["role"] == "tool"
    assert normalized[-1]["tool_call_id"] == "c1"
    assert "UNKNOWN" in normalized[-1]["content"]


def test_composed_final_is_reused():
    history = [
        {"role": "user", "content": "summarize"},
        {"role": "assistant", "content": "Final answer"},
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed == "Final answer"
    assert normalized[-1]["content"] == composed
    assert is_interrupt_closer_message(history[-1])
