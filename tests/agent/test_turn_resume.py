"""Tests for agent/turn_resume.py — same-turn resume tail normalization."""

from agent.turn_resume import (
    is_interrupt_closer_message,
    prepare_resume_history,
    resume_entry_reason,
)


def _assistant_tool_calls(call_id="c1", name="terminal"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }


def _tool_result(call_id="c1", name="terminal", content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


# ---------------------------------------------------------------------------
# is_interrupt_closer_message
# ---------------------------------------------------------------------------


def test_closer_detection_plain():
    assert is_interrupt_closer_message(
        {"role": "assistant", "content": "Operation interrupted."}
    )


def test_closer_detection_with_detail():
    assert is_interrupt_closer_message(
        {
            "role": "assistant",
            "content": "Operation interrupted: waiting for model response (94.0s elapsed).",
        }
    )


def test_closer_rejects_real_answers_and_other_roles():
    assert not is_interrupt_closer_message(
        {"role": "assistant", "content": "The operation succeeded."}
    )
    assert not is_interrupt_closer_message(
        {"role": "user", "content": "Operation interrupted."}
    )
    assert not is_interrupt_closer_message(
        {
            "role": "assistant",
            "content": "Operation interrupted.",
            "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}],
        }
    )
    assert not is_interrupt_closer_message({"role": "assistant", "content": None})
    assert not is_interrupt_closer_message("not a dict")


# ---------------------------------------------------------------------------
# prepare_resume_history
# ---------------------------------------------------------------------------


def test_tool_tail_passes_through_unchanged():
    history = [
        {"role": "user", "content": "do the thing"},
        _assistant_tool_calls(),
        _tool_result(),
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized == history
    # Input must not be mutated / aliased destructively.
    assert len(history) == 3


def test_trailing_interrupt_closer_is_dropped():
    history = [
        {"role": "user", "content": "do the thing"},
        _assistant_tool_calls(),
        _tool_result(),
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["role"] == "tool"
    assert len(normalized) == 3
    # Original list untouched.
    assert len(history) == 4


def test_multiple_trailing_closers_and_empty_assistant_rows_dropped():
    history = [
        {"role": "user", "content": "go"},
        _assistant_tool_calls(),
        _tool_result(),
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "Operation interrupted: handling API error."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["role"] == "tool"


def test_dangling_side_effect_tool_calls_get_unknown_results():
    history = [
        {"role": "user", "content": "restart the service"},
        _assistant_tool_calls(call_id="c9", name="terminal"),
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["role"] == "tool"
    assert normalized[-1]["tool_call_id"] == "c9"
    assert "UNKNOWN" in normalized[-1]["content"]


def test_composed_final_is_returned_not_rerun():
    history = [
        {"role": "user", "content": "summarize"},
        _assistant_tool_calls(),
        _tool_result(),
        {"role": "assistant", "content": "Here is the summary you asked for."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed == "Here is the summary you asked for."
    assert normalized[-1]["content"] == composed


def test_closer_then_composed_final_underneath():
    """A closer stacked on a real answer: drop the closer, deliver the answer."""
    history = [
        {"role": "user", "content": "summarize"},
        {"role": "assistant", "content": "Done: all tests pass."},
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed == "Done: all tests pass."


def test_user_tail_returns_no_composed_final():
    history = [{"role": "user", "content": "hello?"}]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized == history


def test_empty_history():
    normalized, composed = prepare_resume_history([])
    assert normalized == []
    assert composed is None


def test_history_of_only_closers_empties_out():
    normalized, composed = prepare_resume_history(
        [{"role": "assistant", "content": "Operation interrupted."}]
    )
    assert normalized == []
    assert composed is None


# ---------------------------------------------------------------------------
# resume_entry_reason
# ---------------------------------------------------------------------------


def test_resume_entry_reason_labels():
    assert resume_entry_reason([]) == "empty"
    assert resume_entry_reason([_tool_result()]) == "tool-tail"
    assert resume_entry_reason([_assistant_tool_calls()]) == "unanswered-tool-calls"
    assert resume_entry_reason([{"role": "assistant", "content": "hi"}]) == "assistant-tail"
    assert resume_entry_reason([{"role": "user", "content": "hi"}]) == "user-tail"
