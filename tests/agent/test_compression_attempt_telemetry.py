import json
import logging
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.conversation_compression import compress_context
from agent.context_compressor import ContextCompressor


class _TodoStore:
    def format_for_injection(self):
        return ""


class _Agent:
    def __init__(self, compressor):
        self.context_compressor = compressor
        self.session_id = "session-telemetry-test"
        self.platform = "cli"
        self.model = "test/main-model"
        self.provider = "test-provider"
        self.tools = []
        self._compression_feasibility_checked = True
        self.compression_in_place = False
        self._memory_manager = None
        self._session_db = None
        self._todo_store = _TodoStore()
        self._cached_system_prompt = None

    def _emit_status(self, _message):
        pass

    def _emit_warning(self, _message):
        pass

    def _invalidate_system_prompt(self):
        self._cached_system_prompt = None

    def _build_system_prompt(self, system_message):
        return system_message

    def commit_memory_session(self, _messages):
        pass


def _messages(secret_text="TOPSECRET_TRANSCRIPT_TEXT"):
    msgs = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        msgs.append({"role": "user", "content": f"user message {idx} {secret_text}"})
        msgs.append({"role": "assistant", "content": f"assistant reply {idx} {secret_text}"})
    return msgs


def _extract_telemetry(caplog):
    records = [
        record.getMessage()
        for record in caplog.records
        if "context compression attempt telemetry:" in record.getMessage()
    ]
    assert len(records) == 1
    return json.loads(records[0].split("context compression attempt telemetry: ", 1)[1])


def test_compression_attempt_telemetry_is_metadata_only(caplog):
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)

    with patch.object(compressor, "_generate_summary", return_value="SANITIZED SUMMARY"):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, system_prompt = compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    assert system_prompt == "system prompt"
    assert compressed is not None
    payload = _extract_telemetry(caplog)

    assert payload["event"] == "compression_attempt"
    assert payload["attempt_id"]
    assert payload["session_id"] == "session-telemetry-test"
    assert payload["trigger_source"] == "manual"
    assert payload["main_model"] == "test/main-model"
    assert payload["main_context_limit"] == 100_000
    assert payload["current_estimated_tokens"] == 75_000
    assert payload["effective_threshold"] == compressor.threshold_tokens
    assert payload["protected_head_tokens"] is not None
    assert payload["protected_tail_tokens"] is not None
    assert payload["middle_window_tokens"] is not None
    assert payload["chunking"] is False
    assert payload["chunk_count"] in {0, 1}
    assert payload["commit_status"] == "committed"
    assert payload["split_status"] == "not_applicable"
    assert payload["fallback_used"] is False
    assert isinstance(payload["total_duration_ms"], int)
    assert isinstance(payload["commit_ms"], int)
    assert payload["queue_wait_ms"] is None
    assert payload["prompt_build_ms"] is None
    assert payload["time_to_first_progress_ms"] is None
    assert payload["summary_generation_ms"] is None

    raw_log = json.dumps(payload)
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in raw_log
    assert "SANITIZED SUMMARY" not in raw_log
    assert "user message" not in raw_log
    assert "assistant reply" not in raw_log


def test_aux_call_telemetry_records_durations_without_content(caplog):
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    agent = _Agent(compressor)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SANITIZED SUMMARY"))]
    )

    with patch("agent.context_compressor.call_llm", return_value=response):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
            )

    payload = _extract_telemetry(caplog)
    assert payload["aux_prompt_tokens"] is not None
    # Current main intentionally omits max_tokens from the aux summary call
    # (the summary budget is prompt-level guidance only), so no output
    # reservation is recorded.
    assert payload["aux_output_reservation"] is None
    assert isinstance(payload["aux_call_duration_ms"], int)
    assert payload["aux_provider"]
    assert payload["aux_model"]

    raw_log = json.dumps(payload)
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in raw_log
    assert "SANITIZED SUMMARY" not in raw_log


# ── Local compression observation samples (R3) ──────────────────────────


import pytest  # noqa: E402

from hermes_cli.observability import local_observations  # noqa: E402
from hermes_cli.observability.shared_metrics import SharedMetricsStore  # noqa: E402
from hermes_cli.observability.shared_metrics_contract import (  # noqa: E402
    COMPRESSION_AUX_DURATION_METRIC,
    COMPRESSION_DURATION_METRIC,
    COMPRESSION_TOKENS_AFTER_METRIC,
    COMPRESSION_TOKENS_BEFORE_METRIC,
    WORK_LANES,
)


@pytest.fixture
def observations(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config_readonly",
        lambda: {"telemetry": {"shared_metrics": {"enabled": True}}},
    )
    local_observations._reset_for_tests()
    yield
    local_observations._reset_for_tests()


def _observation_rows(metric_name=None):
    return SharedMetricsStore().observation_samples(metric_name=metric_name)


def _build_compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model",
            provider="test-provider",
            threshold_percent=0.50,
            quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    return compressor


def test_committed_batch_attempt_records_one_duration_and_token_pair(observations, caplog):
    compressor = _build_compressor()
    agent = _Agent(compressor)
    agent._work_lane = "kanban"

    with patch.object(compressor, "_generate_summary", return_value="SANITIZED SUMMARY"):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    payload = _extract_telemetry(caplog)
    assert isinstance(payload["tokens_before"], int)
    assert isinstance(payload["tokens_after"], int)
    assert payload["tokens_after"] < payload["tokens_before"]

    [duration] = _observation_rows(COMPRESSION_DURATION_METRIC)
    [before] = _observation_rows(COMPRESSION_TOKENS_BEFORE_METRIC)
    [after] = _observation_rows(COMPRESSION_TOKENS_AFTER_METRIC)
    assert duration["dimensions"]["compression_kind"] == "batch"
    assert duration["dimensions"]["compression_outcome"] == "committed"
    assert duration["dimensions"]["work_lane"] == "kanban"
    assert duration["dimensions"]["execution_surface"] == "cli"
    assert before["value"] == float(payload["tokens_before"])
    assert after["value"] == float(payload["tokens_after"])
    # before/after belong to the same attempt: one transaction, one timestamp.
    assert before["recorded_at"] == after["recorded_at"]
    for row in _observation_rows():
        assert row["dimensions"]["work_lane"] in WORK_LANES


def test_aux_duration_is_recorded_for_an_aux_bearing_attempt(observations, caplog):
    compressor = _build_compressor()
    agent = _Agent(compressor)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="SANITIZED SUMMARY"))]
    )

    with patch("agent.context_compressor.call_llm", return_value=response):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
            )

    [aux] = _observation_rows(COMPRESSION_AUX_DURATION_METRIC)
    [duration] = _observation_rows(COMPRESSION_DURATION_METRIC)
    assert aux["value"] <= duration["value"]
    assert aux["dimensions"] == duration["dimensions"]


def test_pool_saturated_abort_records_no_duration_row(observations):
    """Its started_at is passed inline, so its duration is always ~0ms."""
    from agent.conversation_compression import _emit_compression_attempt_telemetry

    compressor = _build_compressor()
    agent = _Agent(compressor)
    agent._compression_attempt_id = "attempt-1"
    compressor._begin_compression_telemetry(
        attempt_id="attempt-1", current_tokens=75_000
    )

    _emit_compression_attempt_telemetry(
        agent,
        started_at=time.monotonic(),
        commit_status="aborted",
        split_status="aborted",
        failure_class="pool_saturated",
    )

    assert _observation_rows(COMPRESSION_DURATION_METRIC) == []
    assert all(
        row["dimensions"]["compression_outcome"] == "skipped"
        for row in _observation_rows()
    )


def test_stale_token_counts_are_not_persisted_by_a_later_abort(observations):
    """The abort sites emit WITHOUT calling _begin_compression_telemetry."""
    from agent.conversation_compression import _emit_compression_attempt_telemetry

    compressor = _build_compressor()
    agent = _Agent(compressor)

    # A successful attempt records real token counts.
    agent._compression_attempt_id = "attempt-1"
    telemetry = compressor._begin_compression_telemetry(
        attempt_id="attempt-1", current_tokens=75_000
    )
    telemetry["tokens_before"] = 40_000
    telemetry["tokens_after"] = 9_000
    _emit_compression_attempt_telemetry(
        agent,
        started_at=time.monotonic() - 0.05,
        commit_status="committed",
        split_status="not_applicable",
    )
    assert len(_observation_rows(COMPRESSION_TOKENS_BEFORE_METRIC)) == 1

    # A later abort under a NEW attempt id must not persist the stale counts.
    agent._compression_attempt_id = "attempt-2"
    _emit_compression_attempt_telemetry(
        agent,
        started_at=time.monotonic() - 0.05,
        commit_status="aborted",
        split_status="aborted",
        failure_class="commit_fence_cancelled",
    )

    assert len(_observation_rows(COMPRESSION_TOKENS_BEFORE_METRIC)) == 1
    assert len(_observation_rows(COMPRESSION_TOKENS_AFTER_METRIC)) == 1
    aborted = [
        row
        for row in _observation_rows(COMPRESSION_DURATION_METRIC)
        if row["dimensions"]["compression_outcome"] == "aborted"
    ]
    assert len(aborted) == 1


def test_compression_rows_keep_the_lane_seeded_at_attempt_start(observations, caplog):
    """A concurrent turn must not relabel a pooled worker's compression row."""
    compressor = _build_compressor()
    agent = _Agent(compressor)
    agent._work_lane = "research"

    original_generate = compressor._generate_summary

    def mutate_lane_then_summarize(*args, **kwargs):
        # Simulate a concurrent turn switching the agent's lane mid-compression.
        agent._work_lane = "gjc"
        return "SANITIZED SUMMARY"

    del original_generate
    with patch.object(
        compressor, "_generate_summary", side_effect=mutate_lane_then_summarize
    ):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    assert agent._work_lane == "gjc"
    [duration] = _observation_rows(COMPRESSION_DURATION_METRIC)
    assert duration["dimensions"]["work_lane"] == "research"


def test_a_raising_recorder_does_not_break_compression(observations, caplog, monkeypatch):
    compressor = _build_compressor()
    agent = _Agent(compressor)

    def exploding(self, rows):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SharedMetricsStore, "record_observations", exploding)

    with patch.object(compressor, "_generate_summary", return_value="SANITIZED SUMMARY"):
        with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
            compressed, system_prompt = compress_context(
                agent,
                _messages(),
                "system prompt",
                approx_tokens=75_000,
                force=True,
            )

    assert compressed is not None
    assert system_prompt == "system prompt"
    # The existing log line is still emitted exactly once.
    _extract_telemetry(caplog)


def test_aux_call_telemetry_records_content_free_phase_timings():
    compressor = _build_compressor()
    compressor._begin_compression_telemetry(current_tokens=75_000)

    compressor._record_aux_compression_call(
        prompt_messages=[{"role": "user", "content": "TOPSECRET_TRANSCRIPT_TEXT"}],
        max_tokens=1400,
        duration_ms=22,
        aux_provider="ollama",
        aux_model="qwen3:8b",
        phase_timings={
            "queue_wait_ms": 3,
            "prompt_build_ms": 5,
            "time_to_first_progress_ms": 7,
            "summary_generation_ms": 19,
            "commit_ms": 11,
        },
    )

    payload = compressor._last_compression_telemetry
    assert payload is not None
    assert {key: payload[key] for key in (
        "queue_wait_ms",
        "prompt_build_ms",
        "time_to_first_progress_ms",
        "summary_generation_ms",
        "commit_ms",
    )} == {
        "queue_wait_ms": 3,
        "prompt_build_ms": 5,
        "time_to_first_progress_ms": 7,
        "summary_generation_ms": 19,
        "commit_ms": 11,
    }
    assert "TOPSECRET_TRANSCRIPT_TEXT" not in json.dumps(payload)
