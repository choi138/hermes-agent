"""Aggregate summary input stays bounded on initial and iterative compaction."""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX


def _response(text: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    return response


def _compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=272_000
    ):
        return ContextCompressor(model="test", quiet_mode=True)


def test_aggregate_summary_input_is_bounded_and_keeps_both_edges():
    compressor = _compressor()
    messages = [
        {"role": "user", "content": f"turn-{index}-" + "x" * 6_000}
        for index in range(80)
    ]
    messages[0]["content"] = "FIRST_SENTINEL " + messages[0]["content"]
    messages[-1]["content"] += " LAST_SENTINEL"

    with patch(
        "agent.context_compressor.call_llm", return_value=_response("bounded")
    ) as call:
        summary = compressor._generate_summary(messages)

    prompt = call.call_args.kwargs["messages"][0]["content"]
    assert summary.startswith(SUMMARY_PREFIX)
    assert len(prompt) < 180_000
    assert "summary input truncated" in prompt
    assert "FIRST_SENTINEL" in prompt
    assert "LAST_SENTINEL" in prompt


def test_iterative_previous_summary_input_is_also_bounded():
    compressor = _compressor()
    cap = 160_000
    compressor._previous_summary = (
        "PREV_HEAD " + "p" * (cap * 2) + " PREV_TAIL"
    )

    with patch(
        "agent.context_compressor.call_llm", return_value=_response("updated")
    ) as call:
        summary = compressor._generate_summary(
            [{"role": "user", "content": "new turn"}]
        )

    prompt = call.call_args.kwargs["messages"][0]["content"]
    assert summary.startswith(SUMMARY_PREFIX)
    assert len(prompt) < cap + 30_000
    assert "summary input truncated" in prompt
    assert "PREV_HEAD" in prompt
    assert "PREV_TAIL" in prompt
