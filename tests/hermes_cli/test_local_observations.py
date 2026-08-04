"""Tests for the Relay-independent local observation recorder (R3-b)."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from agent import model_call_timing
from hermes_cli.observability import local_observations, relay_shared_metrics, work_lane
from hermes_cli.observability.shared_metrics import SharedMetricsStore
from hermes_cli.observability.shared_metrics_contract import (
    FALLBACK_ACTIVATION_METRIC,
    FIRST_USEFUL_RESULT_METRIC,
    MODEL_CALL_DURATION_METRIC,
    RETRY_ATTEMPT_METRIC,
    TTFT_METRIC,
    observation_dimensions_are_valid,
)


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config_readonly",
        lambda: {"telemetry": {"shared_metrics": {"enabled": True}}},
    )
    local_observations._reset_for_tests()
    model_call_timing.reset_for_tests()
    work_lane.set_routing_lane("")
    yield
    local_observations._reset_for_tests()
    model_call_timing.reset_for_tests()
    work_lane.set_routing_lane("")


def _rows(metric_name: str | None = None) -> list[dict[str, Any]]:
    return SharedMetricsStore().observation_samples(metric_name=metric_name)


def _base(**overrides: Any) -> dict[str, Any]:
    payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "task_id": "task-1",
        "api_request_id": "turn-1:api:1",
        "platform": "cli",
        "provider": "anthropic",
        "model": "claude-sonnet",
        "api_mode": "anthropic_messages",
    }
    payload.update(overrides)
    return payload


# ── gating ──────────────────────────────────────────────────────────────


def test_gating_off_when_shared_metrics_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config_readonly",
        lambda: {"telemetry": {"shared_metrics": {"enabled": False}}},
    )
    local_observations._reset_for_tests()

    assert local_observations.enabled() is False
    local_observations.observe_lifecycle("pre_api_request", **_base())
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    assert not (tmp_path / "home" / "telemetry").exists()


def test_gating_off_when_local_observations_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "hermes_cli.config.read_raw_config_readonly",
        lambda: {
            "telemetry": {
                "shared_metrics": {"enabled": True, "local_observations": False}
            }
        },
    )
    local_observations._reset_for_tests()

    assert local_observations.enabled() is False


def test_recorder_never_calls_relay_enabled(recorder, monkeypatch):
    """relay_shared_metrics.enabled() deactivates the profile runtime."""
    calls: list[int] = []
    real_enabled = relay_shared_metrics.enabled

    def spy() -> bool:
        calls.append(1)
        return real_enabled()

    monkeypatch.setattr(relay_shared_metrics, "enabled", spy)

    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle("post_api_request", **_base())
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    assert calls == []


def test_store_is_recached_when_the_hermes_home_changes(recorder, tmp_path, monkeypatch):
    first = local_observations._store()
    assert local_observations._store() is first

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "second-home"))
    second = local_observations._store()

    assert second is not first
    assert str(tmp_path / "second-home") in str(second.database_path)


# ── model-call attempt rows ─────────────────────────────────────────────


def test_ttft_written_only_when_a_first_frame_was_stamped(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    token = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming", api_mode_family="anthropic_messages"
    )
    model_call_timing.stamp_first_frame(token)
    model_call_timing.finish_wire_attempt(token, "")
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=0
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    ttft = _rows(TTFT_METRIC)
    duration = _rows(MODEL_CALL_DURATION_METRIC)
    assert len(ttft) == 1
    assert len(duration) == 1
    assert ttft[0]["dimensions"]["attempt_outcome"] == "success"
    assert ttft[0]["dimensions"]["stream_mode"] == "streaming"
    assert ttft[0]["dimensions"]["api_mode_family"] == "anthropic_messages"
    assert ttft[0]["value"] <= duration[0]["value"]


def test_no_ttft_row_on_the_non_streaming_path(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    token = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="non_streaming"
    )
    model_call_timing.finish_wire_attempt(token, "success")
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=3
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    assert _rows(TTFT_METRIC) == []
    [duration] = _rows(MODEL_CALL_DURATION_METRIC)
    assert duration["dimensions"]["stream_mode"] == "non_streaming"


def test_failed_attempts_are_recorded_from_api_request_error(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    token = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming"
    )
    model_call_timing.stamp_first_frame(token)
    model_call_timing.finish_wire_attempt(token, "")
    local_observations.observe_lifecycle(
        "api_request_error",
        **_base(),
        retry_count=0,
        max_retries=3,
        retryable=True,
        reason="rate_limit",
        error={"type": "RateLimitError", "message": "429"},
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    [duration] = _rows(MODEL_CALL_DURATION_METRIC)
    assert duration["dimensions"]["attempt_outcome"] == "failed"
    [retry] = _rows(RETRY_ATTEMPT_METRIC)
    assert retry["dimensions"]["retry_reason"] == "rate_limit"
    assert retry["value"] == 1.0


def test_interrupted_attempts_are_labelled_cancelled(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    token = model_call_timing.begin_wire_attempt("turn-1:api:1")
    model_call_timing.finish_wire_attempt(token, "")
    local_observations.observe_lifecycle(
        "api_request_error",
        **_base(),
        retryable=False,
        reason="timeout",
        error={"type": "InterruptedError", "message": "stop"},
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    [duration] = _rows(MODEL_CALL_DURATION_METRIC)
    assert duration["dimensions"]["attempt_outcome"] == "cancelled"
    # retryable=False means the loop is not retrying, so no retry row.
    assert _rows(RETRY_ATTEMPT_METRIC) == []


def test_two_physical_attempts_produce_two_rows(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    first = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming"
    )
    model_call_timing.stamp_first_frame(first)
    model_call_timing.finish_wire_attempt(first, "")
    second = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming"
    )
    model_call_timing.stamp_first_frame(second)
    model_call_timing.finish_wire_attempt(second, "")
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=5
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    durations = _rows(MODEL_CALL_DURATION_METRIC)
    assert len(durations) == 2
    outcomes = [row["dimensions"]["attempt_outcome"] for row in durations]
    assert outcomes.count("success") == 1
    assert outcomes.count("failed") == 1
    assert all(row["value"] >= 0 for row in _rows(TTFT_METRIC))


# ── first useful result ─────────────────────────────────────────────────


def test_first_useful_result_fires_on_a_successful_tool_call(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle("pre_api_request", **_base())
    local_observations.observe_lifecycle(
        "post_tool_call",
        session_id="session-1",
        turn_id="turn-1",
        tool_name="read_file",
        status="ok",
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    [row] = _rows(FIRST_USEFUL_RESULT_METRIC)
    assert row["dimensions"]["first_result_kind"] == "tool_result"
    assert row["dimensions"]["provider_family"] == "direct"
    assert row["dimensions"]["model_family"] == "claude"


@pytest.mark.parametrize("status", ["blocked", "error", "", "denied"])
def test_first_useful_result_ignores_unsuccessful_tool_calls(recorder, status):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle(
        "post_tool_call",
        session_id="session-1",
        turn_id="turn-1",
        tool_name="write_file",
        status=status,
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    assert _rows(FIRST_USEFUL_RESULT_METRIC) == []


def test_first_useful_result_fires_on_assistant_text(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=17
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    [row] = _rows(FIRST_USEFUL_RESULT_METRIC)
    assert row["dimensions"]["first_result_kind"] == "assistant_text"


def test_first_useful_result_is_recorded_once_per_turn(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle(
        "post_tool_call",
        session_id="session-1",
        turn_id="turn-1",
        status="ok",
    )
    local_observations.observe_lifecycle(
        "post_tool_call",
        session_id="session-1",
        turn_id="turn-1",
        status="ok",
    )
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=40
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    assert len(_rows(FIRST_USEFUL_RESULT_METRIC)) == 1


def test_t0_prefers_pre_llm_call_over_the_first_pre_api_request(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    slot = local_observations._TURNS[("session-1", "turn-1")]
    assert slot.t0_source == "pre_llm_call"
    first_t0 = slot.t0_ns

    local_observations.observe_lifecycle("pre_api_request", **_base())
    slot = local_observations._TURNS[("session-1", "turn-1")]
    assert slot.t0_source == "pre_llm_call"
    assert slot.t0_ns == first_t0


def test_t0_falls_back_to_the_first_pre_api_request(recorder):
    local_observations.observe_lifecycle("pre_api_request", **_base())
    slot = local_observations._TURNS[("session-1", "turn-1")]
    assert slot.t0_source == "pre_api_request"


# ── buffering, eviction, retention ──────────────────────────────────────


def test_rows_are_buffered_and_flushed_once_per_turn(recorder):
    """A 5-api-call turn must not pay one connect+commit per api call."""
    from hermes_cli.observability import shared_metrics as shared_metrics_module

    local_observations.observe_lifecycle("pre_llm_call", **_base())
    # Warm the cached store so the connection count only measures writes.
    local_observations._store()

    connects: list[int] = []
    real_connect = shared_metrics_module.sqlite3.connect

    def counting_connect(*args: Any, **kwargs: Any):
        connects.append(1)
        return real_connect(*args, **kwargs)

    shared_metrics_module.sqlite3.connect = counting_connect
    try:
        for index in range(5):
            request_id = f"turn-1:api:{index}"
            token = model_call_timing.begin_wire_attempt(
                request_id, stream_mode="streaming"
            )
            model_call_timing.stamp_first_frame(token)
            model_call_timing.finish_wire_attempt(token, "")
            local_observations.observe_lifecycle(
                "post_api_request",
                **_base(api_request_id=request_id),
                assistant_content_chars=0,
            )
        # Nothing is written until the turn's single flush.
        assert connects == []

        local_observations.observe_lifecycle(
            "on_session_end", session_id="session-1"
        )
        insert_connects = len(connects)
    finally:
        shared_metrics_module.sqlite3.connect = real_connect

    # One insert transaction plus the retention pass's own two short ones.
    assert insert_connects <= 3
    assert len(_rows(MODEL_CALL_DURATION_METRIC)) == 5


def test_buffer_force_flushes_above_the_cap(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    row = {
        "metric_name": RETRY_ATTEMPT_METRIC,
        "dimensions": {
            "api_mode_family": "chat_completions",
            "call_role": "primary",
            "execution_surface": "cli",
            "model_family": "gpt",
            "provider_family": "direct",
            "retry_reason": "timeout",
            "work_lane": "direct",
        },
        "value": 1.0,
    }
    local_observations.buffer_rows(
        "session-1",
        "turn-1",
        [dict(row) for _ in range(local_observations._MAX_BUFFERED_ROWS + 1)],
    )

    # Flushed without waiting for the session to end.
    assert len(_rows(RETRY_ATTEMPT_METRIC)) == (
        local_observations._MAX_BUFFERED_ROWS + 1
    )


def test_turn_slots_are_lru_capped(recorder):
    for index in range(local_observations._MAX_TURN_SLOTS + 8):
        local_observations.observe_lifecycle(
            "pre_llm_call", **_base(turn_id=f"turn-{index}")
        )

    assert len(local_observations._TURNS) <= local_observations._MAX_TURN_SLOTS


def test_session_reset_flushes_buffered_rows(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=12
    )
    assert _rows(FIRST_USEFUL_RESULT_METRIC) == []

    local_observations.observe_lifecycle("on_session_reset", session_id="session-1")
    assert len(_rows(FIRST_USEFUL_RESULT_METRIC)) == 1


def test_retention_is_triggered_at_most_once_per_process(recorder, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        SharedMetricsStore,
        "prune_observation_samples",
        lambda self, **kwargs: calls.append(1),
    )

    local_observations.observe_lifecycle("on_session_end", session_id="a")
    local_observations.observe_lifecycle("on_session_finalize", session_id="b")

    assert len(calls) == 1


# ── failure isolation and contract compliance ───────────────────────────


def test_a_raising_store_does_not_propagate(recorder, monkeypatch):
    def exploding(self, rows):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SharedMetricsStore, "record_observations", exploding)

    local_observations.observe_lifecycle("pre_llm_call", **_base())
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=4
    )
    # Must not raise.
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")


def test_every_recorded_row_passes_the_closed_contract(recorder):
    local_observations.observe_lifecycle("pre_llm_call", **_base())
    token = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming"
    )
    model_call_timing.stamp_first_frame(token)
    model_call_timing.finish_wire_attempt(token, "")
    local_observations.observe_lifecycle(
        "post_api_request", **_base(), assistant_content_chars=9
    )
    local_observations.observe_lifecycle(
        "api_request_error",
        **_base(api_request_id="turn-1:api:2"),
        retryable=True,
        reason="overloaded",
        error={"type": "APIStatusError", "message": "529"},
    )
    local_observations.record_fallback_activation(
        session_id="session-1",
        fallback_ordinal=1,
        reason=None,
        platform="cli",
        provider="openai",
        model="gpt-5",
        api_mode="chat_completions",
        call_role="fallback",
        lane="direct",
    )
    local_observations.record_compression_attempt(
        kind="batch",
        outcome="committed",
        trigger="preflight",
        lane="direct",
        platform="cli",
        duration_ms=1200,
        aux_duration_ms=900,
        tokens_before=50_000,
        tokens_after=12_000,
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    rows = _rows()
    assert rows
    for row in rows:
        assert observation_dimensions_are_valid(
            row["metric_name"], row["dimensions"]
        ), row

    [fallback] = _rows(FALLBACK_ACTIVATION_METRIC)
    assert fallback["dimensions"]["fallback_reason"] == "none"
    assert fallback["value"] == 1.0


def test_retry_and_fallback_rows_are_distinguishable(recorder):
    local_observations.record_retry_attempt(
        session_id="session-1",
        turn_id="turn-1",
        reason="invalid_response",
        platform="cli",
        provider="openai",
        model="gpt-5",
        api_mode="chat_completions",
    )
    local_observations.record_fallback_activation(
        session_id="session-1",
        fallback_ordinal=2,
        reason="rate_limit",
        platform="cli",
        provider="openai",
        model="gpt-5",
        api_mode="chat_completions",
    )
    local_observations.flush("session-1")

    [retry] = _rows(RETRY_ATTEMPT_METRIC)
    [fallback] = _rows(FALLBACK_ACTIVATION_METRIC)
    assert retry["dimensions"]["retry_reason"] == "invalid_response"
    assert fallback["dimensions"]["fallback_reason"] == "rate_limit"
    assert fallback["value"] == 2.0


def test_handled_hooks_are_a_subset_of_the_relay_gate():
    assert local_observations.HANDLED_HOOKS <= relay_shared_metrics.HANDLED_HOOKS


# ── invalid_response single-emit regression (verified double-count, 2026-08-04) ──


def test_invalid_response_retry_is_recorded_exactly_once(recorder):
    """One loop retry must produce ONE row, not two.

    conversation_loop.py's ``if response_invalid:`` block calls
    ``_invoke_api_request_error_hook(retryable=True, reason="invalid_response")``
    and then falls straight through to ``retry_count += 1`` with no return or
    continue. An additional explicit ``record_retry_attempt`` in that block
    therefore doubled every invalid-response retry in the raw table.
    """
    local_observations.observe_lifecycle("pre_api_request", **_base())
    local_observations.observe_lifecycle(
        "api_request_error",
        **_base(),
        retry_count=0,
        max_retries=3,
        retryable=True,
        reason="invalid_response",
        error={"type": "InvalidAPIResponse", "message": "empty choices"},
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    rows = _rows(RETRY_ATTEMPT_METRIC)
    assert len(rows) == 1, f"expected exactly one retry row, got {len(rows)}"
    assert rows[0]["dimensions"]["retry_reason"] == "invalid_response"
    assert rows[0]["value"] == 1.0


def test_invalid_response_block_does_not_emit_a_second_retry_row():
    """Pin the fix at the source: the block must not re-emit the retry itself."""
    from pathlib import Path

    source = Path("agent/conversation_loop.py").read_text()
    start = source.index("if response_invalid:")
    end = source.index("if agent._fallback_index < len(agent._fallback_chain):", start)
    block = source[start:end]

    assert "_invoke_api_request_error_hook" in block, (
        "the hook call is the single retry emit route for this path; if it moved, "
        "the recorder-level guarantee above needs rechecking"
    )
    assert "record_retry_attempt" not in block, (
        "invalid-response retries are already counted from the api_request_error "
        "hook; emitting here doubles them"
    )


def test_retry_row_carries_the_turn_lane_and_learned_call_role(recorder):
    """The hook payload has no lane/call_role, so they come from the turn slot."""
    work_lane.set_routing_lane("research_readonly")
    local_observations.observe_lifecycle("pre_api_request", **_base())
    token = model_call_timing.begin_wire_attempt(
        "turn-1:api:1", stream_mode="streaming", call_role="fallback"
    )
    model_call_timing.stamp_first_frame(token)
    model_call_timing.finish_wire_attempt(token, "")
    local_observations.observe_lifecycle(
        "api_request_error",
        **_base(),
        retry_count=0,
        max_retries=3,
        retryable=True,
        reason="rate_limit",
        error={"type": "RateLimitError", "message": "429"},
    )
    local_observations.observe_lifecycle("on_session_end", session_id="session-1")

    [retry] = _rows(RETRY_ATTEMPT_METRIC)
    assert retry["dimensions"]["work_lane"] == "research"
    assert retry["dimensions"]["call_role"] == "fallback"
