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


def test_failed_closers_preserve_tool_pair_and_input_history():
    from copy import deepcopy
    from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE

    history = [
        {"role": "user", "content": "inspect file"},
        {"role": "assistant", "tool_calls": [
            {"id": "read1", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "read1", "content": "data"},
        {"role": "assistant", "content": FAILED_TURN_NOTICE},
        {"role": "assistant", "content": PARTIAL_FAILED_TURN_NOTICE},
        {"role": "assistant", "content": "Operation interrupted."},
    ]
    original = deepcopy(history)
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized == history[:3]
    assert history == original


def test_failure_closer_does_not_hide_unanswered_external_effect():
    from agent.turn_failure_copy import FAILED_TURN_NOTICE

    history = [
        {"role": "user", "content": "external operation"},
        {"role": "assistant", "tool_calls": [
            {"id": "write1", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "assistant", "content": FAILED_TURN_NOTICE},
    ]
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized[-1]["tool_call_id"] == "write1"
    assert normalized[-1]["effect_disposition"] == "unknown"


def test_merged_failure_notices_are_not_a_composed_answer():
    from agent.agent_runtime_helpers import repair_message_sequence
    from agent.turn_failure_copy import FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE
    history = [{"role": "user", "content": "read"}] + [
        {"role": "assistant", "content": notice}
        for notice in (FAILED_TURN_NOTICE, PARTIAL_FAILED_TURN_NOTICE, FAILED_TURN_NOTICE)
    ]
    repair_message_sequence(None, history)
    normalized, composed = prepare_resume_history(history)
    assert composed is None
    assert normalized == [{"role": "user", "content": "read"}]
    # A real answer quoting a notice must remain deliverable.
    answer = "Actual answer\n\n" + FAILED_TURN_NOTICE
    _, composed = prepare_resume_history([{"role": "user", "content": "read"},
                                         {"role": "assistant", "content": answer}])
    assert composed == answer
