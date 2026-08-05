"""The R5 `hermes.r5probe` DEBUG lines must be provably inert.

Three probe sites were added (two in ``should_defer_preflight_to_real_usage``,
one in ``update_from_response``) plus a wall-clock probe around the preflight
pass loop in ``agent/turn_context.py``.  They exist so the estimate-vs-real
token headroom and the prologue's blocking cost become reconstructable from a
log file at zero provider cost.  They must not change behaviour, and they must
NOT touch ``hermes_cli/observability`` — the metric funnel and its closed
``compression_failure`` vocabulary are read-only for this slice, so the
2026-08-04 compression baseline stays comparable.
"""

from __future__ import annotations

import logging

import pytest
from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


PROBE_PREFIX = "hermes.r5probe"


def _compressor(**kwargs) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000
    ):
        compressor = ContextCompressor(
            model="test/model",
            quiet_mode=True,
            protect_first_n=2,
            protect_last_n=2,
            **kwargs,
        )
        _ = compressor.context_length
        return compressor


def _messages(n: int = 14) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message number {i} with some filler text " * 4,
        }
        for i in range(n)
    ]


def _summary_response() -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = (
        "[CONTEXT SUMMARY]: the earlier turns discussed filler text"
    )
    return response


TELEMETRY_FIELDS = (
    "_active_compression_telemetry",
    "_compression_telemetry_seed",
    "_last_compression_telemetry",
)


class TestCompressIsIdenticalWithAndWithoutDebugLogging:
    def _run_compress(self, *, logging_enabled: bool):
        compressor = _compressor()
        cc_logger = logging.getLogger("agent.context_compressor")
        previous = cc_logger.disabled
        cc_logger.disabled = not logging_enabled
        try:
            with patch(
                "agent.context_compressor.call_llm",
                return_value=_summary_response(),
            ):
                result = compressor.compress(
                    _messages(), current_tokens=999_999, force=True
                )
        finally:
            cc_logger.disabled = previous
        telemetry = {
            field: getattr(compressor, field, "<missing>")
            for field in TELEMETRY_FIELDS
        }
        return result, telemetry

    def test_message_list_is_identical(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="agent.context_compressor"):
            with_logging, telemetry_on = self._run_compress(logging_enabled=True)
        without_logging, telemetry_off = self._run_compress(logging_enabled=False)

        assert with_logging == without_logging
        # Sanity: compression actually did something, so the comparison is not
        # vacuously true on two untouched lists.
        assert len(with_logging) < len(_messages())

    def test_telemetry_state_is_identical(self):
        _, telemetry_on = self._run_compress(logging_enabled=True)
        _, telemetry_off = self._run_compress(logging_enabled=False)
        for field in TELEMETRY_FIELDS:
            assert field in telemetry_on and field in telemetry_off
        # Attempt-id style volatile keys would differ run-to-run; compare the
        # shape and the non-volatile contents.
        assert set(telemetry_on) == set(telemetry_off)
        for field in TELEMETRY_FIELDS:
            left, right = telemetry_on[field], telemetry_off[field]
            assert type(left) is type(right), field
            if isinstance(left, dict) and isinstance(right, dict):
                assert set(left) == set(right), field


class TestProbeRecordsDoNotTouchTheObservabilityFunnel:
    def test_no_probe_record_is_emitted_through_hermes_cli_observability(
        self, caplog
    ):
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor.last_real_prompt_tokens = 99_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000

        with caplog.at_level(logging.DEBUG):
            compressor.should_defer_preflight_to_real_usage(101_000)
            compressor.should_defer_preflight_to_real_usage(500_000)
            compressor.update_from_response({"prompt_tokens": 4_242})

        probe_records = [
            record
            for record in caplog.records
            if PROBE_PREFIX in record.getMessage()
        ]
        assert probe_records, "expected probe records to be emitted at DEBUG"
        for record in probe_records:
            assert not record.name.startswith("hermes_cli.observability"), (
                f"probe leaked into the read-only telemetry funnel: {record.name}"
            )
            assert record.levelno == logging.DEBUG, (
                "probes must stay at DEBUG so they are silent at default "
                f"verbosity (saw {record.levelname})"
            )

    def test_probe_lines_cover_all_three_compressor_sites(self, caplog):
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor.last_real_prompt_tokens = 99_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000

        with caplog.at_level(logging.DEBUG, logger="agent.context_compressor"):
            # granted: growth inside tolerance
            assert compressor.should_defer_preflight_to_real_usage(101_000) is True
            # refused: growth far beyond tolerance
            compressor.last_rough_tokens_when_real_prompt_fit = 100_000
            assert compressor.should_defer_preflight_to_real_usage(500_000) is False
            compressor.update_from_response({"prompt_tokens": 4_242})

        text = "\n".join(
            record.getMessage()
            for record in caplog.records
            if PROBE_PREFIX in record.getMessage()
        )
        assert "preflight_defer granted" in text
        assert "preflight_defer refused" in text
        assert "real_usage prompt_tokens=4242" in text
        # The paired fields the measurement procedure depends on.
        for field in ("rough=", "baseline=", "growth=", "tolerated=", "threshold="):
            assert field in text, field

    def test_probes_are_silent_at_default_verbosity(self, caplog):
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor.last_real_prompt_tokens = 99_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000

        with caplog.at_level(logging.INFO):
            compressor.should_defer_preflight_to_real_usage(101_000)
            compressor.update_from_response({"prompt_tokens": 4_242})

        assert not [
            record
            for record in caplog.records
            if PROBE_PREFIX in record.getMessage()
        ]


class TestPredicateReturnValueIsUnaffectedByLoggingState:
    @pytest.mark.parametrize("rough, want", [(101_000, True), (500_000, False)])
    @pytest.mark.parametrize("disabled", [True, False])
    def test_same_verdict_and_same_ratchet(self, rough, want, disabled):
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor.last_real_prompt_tokens = 99_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000

        cc_logger = logging.getLogger("agent.context_compressor")
        previous = cc_logger.disabled
        cc_logger.disabled = disabled
        try:
            assert compressor.should_defer_preflight_to_real_usage(rough) is want
        finally:
            cc_logger.disabled = previous

        expected_baseline = max(100_000, rough) if want else 100_000
        assert (
            compressor.last_rough_tokens_when_real_prompt_fit == expected_baseline
        )


class TestUpdateFromResponseProbeDoesNotDisturbAntiThrashState:
    def test_ineffective_verdict_still_fires_on_real_overrun(self):
        """The probe sits immediately before the anti-thrash accounting; that
        accounting (which at 2 strikes disables ALL automatic compaction) must
        be untouched."""
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor._verify_compaction_cleared_threshold = True
        compressor.update_from_response({"prompt_tokens": 150_000})
        assert compressor._ineffective_compression_count == 1
        assert compressor._verify_compaction_cleared_threshold is False

    def test_fitting_response_clears_the_strike(self):
        compressor = _compressor()
        compressor.threshold_tokens = 100_000
        compressor._verify_compaction_cleared_threshold = True
        compressor.update_from_response({"prompt_tokens": 150_000})
        assert compressor._ineffective_compression_count == 1

        compressor._verify_compaction_cleared_threshold = True
        compressor.update_from_response({"prompt_tokens": 10_000})
        assert compressor._ineffective_compression_count == 0

    def test_zero_prompt_tokens_emits_no_probe(self, caplog):
        """The probe is inside the `last_prompt_tokens > 0` branch, so a
        usage-less response must not log a misleading real_usage line."""
        compressor = _compressor()
        with caplog.at_level(logging.DEBUG, logger="agent.context_compressor"):
            compressor.update_from_response({})
        assert not [
            record
            for record in caplog.records
            if "real_usage" in record.getMessage()
        ]
        assert compressor.awaiting_real_usage_after_compression is False


# ── turn_context.py: preflight_block_ms probe ─────────────────────────────


@pytest.fixture()
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


class TestPreflightBlockProbe:
    """The prologue wall-clock probe.

    This is the only signal that can show the preflight block stalling a turn:
    ``_touch_turn`` starts the turn slot on ``pre_llm_call``, which is
    dispatched AFTER the preflight loop, so
    ``hermes.turn.first_useful_result_ms`` provably cannot see this stall.
    """

    def _pressured_agent(self):
        import types as _types

        from tests.agent.test_turn_context import _FakeAgent

        agent = _FakeAgent()
        agent.compression_enabled = True
        agent.context_compressor = _types.SimpleNamespace(
            protect_first_n=0,
            protect_last_n=0,
            threshold_tokens=1,
            context_length=100_000,
            last_prompt_tokens=0,
            should_compress=lambda _tokens=None: True,
            should_compress_info=lambda _tokens=None: (True, None),
            should_defer_preflight_to_real_usage=lambda _t: False,
            get_active_compression_failure_cooldown=lambda: None,
        )
        agent._emit_status = MagicMock()
        return agent

    def _probe_lines(self, caplog):
        return [
            record.getMessage()
            for record in caplog.records
            if "preflight_block_ms" in record.getMessage()
        ]

    def test_probe_reports_pass_count_and_does_not_change_control_flow(
        self, caplog, _stub_runtime_main
    ):
        from tests.agent.test_turn_context import _build

        agent = self._pressured_agent()
        calls = []

        def _no_progress_compress(messages, _system_message, **_kwargs):
            calls.append(1)
            return messages, "SYSTEM"

        agent._compress_context = _no_progress_compress

        with caplog.at_level(logging.DEBUG, logger="agent.turn_context"):
            ctx = _build(
                agent,
                conversation_history=[
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "older"},
                ],
            )

        # A no-progress pass arms the blocker and breaks after ONE pass —
        # exactly as before the probe was added.
        assert calls == [1]
        assert ctx.preflight_compression_blocked is True

        lines = self._probe_lines(caplog)
        assert len(lines) == 1, lines
        assert "passes=1" in lines[0]
        assert "blocked=True" in lines[0]
        assert "hermes.r5probe" in lines[0]

    def test_probe_is_silent_at_default_verbosity(
        self, caplog, _stub_runtime_main
    ):
        from tests.agent.test_turn_context import _build

        agent = self._pressured_agent()
        agent._compress_context = lambda messages, _sys, **_kw: (
            messages,
            "SYSTEM",
        )

        with caplog.at_level(logging.INFO):
            _build(
                agent,
                conversation_history=[{"role": "user", "content": "old"}],
            )

        assert not self._probe_lines(caplog)

    def test_no_probe_when_preflight_block_does_not_run(
        self, caplog, _stub_runtime_main
    ):
        """Sub-threshold turns never enter the block, so no timing line."""
        import types as _types

        from tests.agent.test_turn_context import _FakeAgent, _build

        agent = _FakeAgent()
        agent.compression_enabled = True
        agent.context_compressor = _types.SimpleNamespace(
            protect_first_n=0,
            protect_last_n=0,
            threshold_tokens=10_000_000,
            context_length=100_000,
            last_prompt_tokens=0,
            should_compress=lambda _tokens=None: False,
            should_compress_info=lambda _tokens=None: (False, None),
            should_defer_preflight_to_real_usage=lambda _t: False,
            get_active_compression_failure_cooldown=lambda: None,
        )
        agent._emit_status = MagicMock()
        agent._compress_context = MagicMock()

        with caplog.at_level(logging.DEBUG, logger="agent.turn_context"):
            _build(agent, conversation_history=[{"role": "user", "content": "old"}])

        assert not self._probe_lines(caplog)
        agent._compress_context.assert_not_called()
