"""Compression cannot leave the model believing removed skills are loaded."""

from unittest.mock import MagicMock, patch

from agent.context_compressor import (
    ContextCompressor,
    _skill_pruned_marker,
    _summarize_tool_result,
)
from agent.prompt_builder import SKILLS_GUIDANCE


def test_large_skill_view_summary_emits_canonical_reload_marker():
    summary = _summarize_tool_result(
        "skill_view", '{"name":"pdf"}', "instructions\n" + "x" * 6_000
    )
    marker = (
        "[SKILL_PRUNED: content lost in compression; "
        "reload with skill_view(name='pdf')]"
    )
    assert summary.startswith("[skill_view] name=pdf")
    assert marker in summary
    assert "## Skill Safety Rule" in SKILLS_GUIDANCE
    assert "DEDUP" in SKILLS_GUIDANCE


def test_small_skill_and_non_view_metadata_do_not_emit_marker():
    small = _summarize_tool_result("skill_view", '{"name":"pdf"}', "x" * 1_000)
    listing = _summarize_tool_result("skills_list", '{"name":"pdf"}', "x" * 6_000)
    assert "SKILL_PRUNED" not in small
    assert "SKILL_PRUNED" not in listing


def _compressor():
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000
    ):
        return ContextCompressor(
            model="test", quiet_mode=True, protect_first_n=1, protect_last_n=2
        )


def _skill_pair(name, *, size=6_000):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{name}",
                    "type": "function",
                    "function": {
                        "name": "skill_view",
                        "arguments": f'{{"name":"{name}"}}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call_{name}",
            "content": f"# {name} instructions\n" + "x" * size,
        },
    ]


def _filler(count, *, start=0):
    return [
        {
            "role": "user" if (start + index) % 2 == 0 else "assistant",
            "content": f"filler {start + index} " + "y" * 400,
        }
        for index in range(count)
    ]


def test_recently_loaded_and_tail_referenced_skills_survive_ordinary_prune():
    compressor = _compressor()
    recent = _filler(10) + _skill_pair("fresh") + _filler(6, start=10)
    recent_result, _ = compressor._prune_old_tool_results(
        recent, protect_tail_count=4
    )
    assert recent_result[11]["content"].startswith("# fresh instructions")

    referenced = (
        _skill_pair("steered")
        + _filler(14)
        + [{"role": "user", "content": "keep following the steered steps"}]
    )
    referenced_result, _ = compressor._prune_old_tool_results(
        referenced, protect_tail_count=4
    )
    assert referenced_result[1]["content"].startswith("# steered instructions")


def test_pressure_override_can_demote_a_recent_skill_body():
    compressor = _compressor()
    messages = (
        _filler(2)
        + _skill_pair("fresh", size=60_000)
        + [{"role": "user", "content": "active ask"}]
    )
    result, count = compressor._prune_old_tool_results(
        messages,
        protect_tail_count=4,
        protect_tail_tokens=100,
    )
    assert count >= 1
    assert "[SKILL_PRUNED:" in result[3]["content"]


def _summary_response(text):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    return response


def test_raw_skill_body_gets_marker_when_summarizer_drops_instructions():
    compressor = _compressor()
    turns = [
        {"role": "user", "content": "Build the report with the pdf skill"},
        *_skill_pair("pdf", size=8_000),
    ]
    with patch(
        "agent.context_compressor.call_llm",
        return_value=_summary_response("## Goal\nBuild the report."),
    ):
        summary = compressor._generate_summary(turns)
    assert _skill_pruned_marker("pdf") in summary
    assert _skill_pruned_marker("pdf") in compressor._previous_summary


def test_parallel_raw_skill_bodies_each_get_their_own_reload_marker():
    compressor = _compressor()
    turns = [
        {"role": "user", "content": "Use both skills"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_pdf",
                    "type": "function",
                    "function": {
                        "name": "skill_view",
                        "arguments": '{"name":"pdf"}',
                    },
                },
                {
                    "id": "call_spreadsheets",
                    "type": "function",
                    "function": {
                        "name": "skill_view",
                        "arguments": '{"name":"spreadsheets"}',
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_pdf",
            "content": "# pdf instructions\n" + "x" * 8_000,
        },
        {
            "role": "tool",
            "tool_call_id": "call_spreadsheets",
            "content": "# spreadsheets instructions\n" + "y" * 8_000,
        },
    ]
    with patch(
        "agent.context_compressor.call_llm",
        return_value=_summary_response("## Goal\nUse both skills."),
    ):
        summary = compressor._generate_summary(turns)

    assert _skill_pruned_marker("pdf") in summary
    assert _skill_pruned_marker("spreadsheets") in summary


def test_marker_survives_iterative_summary_rewrite_without_duplication():
    compressor = _compressor()
    compressor._previous_summary = (
        "## Goal\nPrior work.\n\n## Pruned Skills\n" + _skill_pruned_marker("pdf")
    )
    with patch(
        "agent.context_compressor.call_llm",
        return_value=_summary_response("## Goal\nNew work."),
    ):
        summary = compressor._generate_summary(
            [{"role": "user", "content": "continue"}]
        )
    assert summary.count(_skill_pruned_marker("pdf")) == 1


def test_static_fallback_marks_raw_skill_body():
    compressor = _compressor()
    summary = compressor._build_static_fallback_summary(
        [
            {"role": "user", "content": "Use the pdf skill"},
            *_skill_pair("pdf", size=8_000),
        ],
        reason="provider unavailable",
    )
    assert _skill_pruned_marker("pdf") in summary
