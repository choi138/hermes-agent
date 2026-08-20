"""Compaction prompt upgrades ported from Claude Code's /compact design:

1. Chronological <analysis> pre-pass before the summary body (stripped from
   the stored output so it never compounds through iterative updates).
2. "## User Messages" template section preserving the user's exact words.
3. Verbatim in-flight code snippet guidance for interrupted edits.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor, _strip_analysis_block


def _mk_compressor():
    return ContextCompressor(
        model="test-model",
        config_context_length=200000,
        provider="test",
    )


def _capture_summary_prompt(compressor, monkeypatch=None, previous_summary=None):
    """Run _generate_summary with a stubbed LLM; return (prompt, summary)."""
    captured = {}

    def fake_call_llm(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            "<analysis>chronological read-through: user asked X,"
                            " agent did Y</analysis>\n"
                            "## Historical Task Snapshot\nUser asked: 'fix the bug'\n\n"
                            "## User Messages\n- 'fix the bug'\n"
                        )
                    )
                )
            ]
        )

    if previous_summary is not None:
        compressor._previous_summary = previous_summary

    turns = [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": "looking at parser.py now"},
    ]
    with patch("agent.context_compressor.call_llm", side_effect=fake_call_llm):
        summary = compressor._generate_summary(turns)
    return captured["prompt"], summary


# ── 1. <analysis> pre-pass ───────────────────────────────────────────────────


def test_first_compaction_prompt_requests_analysis_pass():
    prompt, _ = _capture_summary_prompt(_mk_compressor())
    assert "<analysis>" in prompt
    assert "chronologically" in prompt


def test_iterative_update_prompt_requests_analysis_pass():
    prompt, _ = _capture_summary_prompt(
        _mk_compressor(), previous_summary="## Historical Task Snapshot\nNone."
    )
    assert "<analysis>" in prompt


def test_analysis_block_stripped_from_stored_summary():
    compressor = _mk_compressor()
    _, summary = _capture_summary_prompt(compressor)
    assert "<analysis>" not in summary
    assert "chronological read-through" not in summary
    # The real content survives the strip.
    assert "fix the bug" in summary
    # The stored iterative-update seed is also clean.
    assert "<analysis>" not in (compressor._previous_summary or "")


def test_strip_analysis_block_closed_tags():
    out = _strip_analysis_block(
        "<analysis>notes here</analysis>\n## Goal\nShip it"
    )
    assert out == "## Goal\nShip it"


def test_strip_analysis_block_unclosed_tag_recovers_at_heading():
    out = _strip_analysis_block(
        "<analysis>model forgot to close\nmore notes\n## Goal\nShip it"
    )
    assert out == "## Goal\nShip it"


def test_strip_analysis_block_unclosed_tag_no_heading_drops_tail():
    out = _strip_analysis_block("intro\n<analysis>dangling notes only")
    assert out == "intro"


def test_strip_analysis_block_noop_without_tags():
    body = "## Goal\nShip it"
    assert _strip_analysis_block(body) == body


# ── 2. "## User Messages" section ────────────────────────────────────────────


def test_template_includes_user_messages_section():
    prompt, _ = _capture_summary_prompt(_mk_compressor())
    assert "## User Messages" in prompt
    assert "exact words" in prompt


def test_iterative_update_appends_user_messages():
    prompt, _ = _capture_summary_prompt(
        _mk_compressor(), previous_summary="## Historical Task Snapshot\nNone."
    )
    assert 'APPEND new user messages to "## User Messages"' in prompt


# ── 3. in-flight code snippet guidance ───────────────────────────────────────


def test_template_requests_verbatim_inflight_snippets():
    prompt, _ = _capture_summary_prompt(_mk_compressor())
    assert "in-flight snippet or diff verbatim" in prompt
    # Relevant Files section carries the fenced-block variant too.
    assert "code\nsection verbatim in a fenced block" in prompt
