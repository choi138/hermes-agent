"""Deterministic proactive pruning reuses the safe compression pre-pass."""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _compressor(**overrides):
    options = {
        "model": "test/model",
        "threshold_percent": 0.5,
        "protect_first_n": 1,
        "protect_last_n": 4,
        "quiet_mode": True,
    }
    options.update(overrides)
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=1_000_000
    ):
        return ContextCompressor(**options)


def _messages(big_indices=()):
    rows = [{"role": "system", "content": "system"}]
    for index in range(8):
        call_id = f"call_{index}"
        rows.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"cmd":"status"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": (
                        chr(65 + index) * 9_000 if index in big_indices else "ok"
                    ),
                },
            ]
        )
    return rows


def _tool(rows, call_id):
    return next(row for row in rows if row.get("tool_call_id") == call_id)


def test_proactive_prune_is_disabled_by_default_and_preserves_identity():
    compressor = _compressor()
    rows = _messages({0, 1, 2})
    result, count = compressor.prune_tool_results_only(rows, current_tokens=500_000)
    assert compressor.proactive_prune_tokens == 0
    assert count == 0
    assert result is rows


def test_proactive_prune_fires_below_full_compression_and_preserves_pairing():
    compressor = _compressor(
        proactive_prune_tokens=48_000,
        proactive_prune_min_result_chars=8_000,
        proactive_prune_min_reclaim_tokens=1_000,
    )
    rows = _messages({0, 1, 2, 7})
    assert compressor.should_compress(120_000) is False

    result, count = compressor.prune_tool_results_only(
        rows, current_tokens=120_000
    )

    assert count >= 3
    assert result is not rows
    assert len(_tool(result, "call_0")["content"]) < 9_000
    assert len(_tool(result, "call_7")["content"]) == 9_000
    call_ids = {
        call["id"]
        for row in result
        for call in (row.get("tool_calls") or [])
    }
    result_ids = {
        row["tool_call_id"] for row in result if row.get("role") == "tool"
    }
    assert call_ids == result_ids

    second, second_count = compressor.prune_tool_results_only(
        result, current_tokens=120_000
    )
    assert second_count == 0
    assert second is result


def test_proactive_prune_rejects_insufficient_or_regressive_reclaim():
    compressor = _compressor(
        proactive_prune_tokens=48_000,
        proactive_prune_min_result_chars=8_000,
        proactive_prune_min_reclaim_tokens=1_000_000,
    )
    rows = _messages({0, 1, 2})
    result, count = compressor.prune_tool_results_only(
        rows, current_tokens=120_000
    )
    assert count == 0
    assert result is rows
