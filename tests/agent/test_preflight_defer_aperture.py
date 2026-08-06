"""compression.preflight_defer_growth_* — aperture of the EXISTING preflight
deferral predicate (``should_defer_preflight_to_real_usage``).

The predicate decides WHETHER a foreground preflight compaction runs on this
turn; it never reshapes a transcript.  Its growth tolerance used to be
hardcoded as ``max(4096, int(threshold_tokens * 0.05))``.  These tests pin three
properties:

1. EXACT EQUIVALENCE at the shipped defaults — the refactored expression must
   evaluate to the identical integer for every threshold, so the predicate's
   truth table (and therefore which turns compress) is unchanged.  This is the
   load-bearing inertness gate: the change is supposed to be a no-op today.
2. GUARD PRESERVATION under a WIDENED aperture — in particular that one real
   provider reading at or above the threshold breaks the
   ``last_rough_tokens_when_real_prompt_fit`` ratchet, so a session cannot defer
   forever.  This is what makes the dial safe to expose at all.
3. MALFORMED-CONFIG CONTAINMENT — a bad value must degrade toward NEVER
   deferring (today's most conservative behavior), never toward a wider
   aperture.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB
from run_agent import AIAgent


# Threshold matrix spans a small-window model, typical mid-size windows, and
# 400K/1M windows where the ratio term dominates the floor.
THRESHOLDS = [8_000, 40_000, 81_920, 100_000, 400_000, 1_000_000]


def _compressor(**kwargs) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100_000
    ):
        compressor = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
            **kwargs,
        )
        _ = compressor.context_length
        return compressor


def _tolerated(compressor: ContextCompressor) -> int:
    """Mirror of the expression under test."""
    return max(
        int(compressor.preflight_defer_growth_tokens),
        int(compressor.threshold_tokens * compressor.preflight_defer_growth_ratio),
    )


class TestApertureDefaultsAreExactlyTodaysConstants:
    def test_defaults_when_no_kwargs_passed(self):
        compressor = _compressor()
        assert compressor.preflight_defer_growth_tokens == 4096
        assert compressor.preflight_defer_growth_ratio == 0.05

    @pytest.mark.parametrize("threshold", THRESHOLDS)
    def test_derived_tolerance_is_the_identical_integer(self, threshold):
        """max(int(4096), int(t * 0.05)) == max(4096, int(t * 0.05)) for every t.

        If this ever fails, the refactor changed which turns compress and the
        slice is NOT inert.
        """
        compressor = _compressor()
        compressor.threshold_tokens = threshold
        assert _tolerated(compressor) == max(4096, int(threshold * 0.05))

    @pytest.mark.parametrize("threshold", THRESHOLDS)
    def test_boundary_is_exact_at_defaults(self, threshold):
        """growth == tolerated defers; growth == tolerated + 1 does not."""
        compressor = _compressor()
        compressor.threshold_tokens = threshold
        tolerated = _tolerated(compressor)

        baseline = threshold  # rough estimate that was proven to fit
        # Real provider count strictly under threshold and > 0 so the two
        # early-out guards do not short-circuit the growth test.
        compressor.last_real_prompt_tokens = threshold - 1
        compressor.awaiting_real_usage_after_compression = False
        compressor.last_rough_tokens_when_real_prompt_fit = baseline

        assert compressor.should_defer_preflight_to_real_usage(
            baseline + tolerated
        ) is True

        # Re-seed: the granted call above ratchets
        # last_rough_tokens_when_real_prompt_fit forward.
        compressor.last_rough_tokens_when_real_prompt_fit = baseline
        assert compressor.should_defer_preflight_to_real_usage(
            baseline + tolerated + 1
        ) is False

    @pytest.mark.parametrize("threshold", THRESHOLDS)
    def test_granted_call_ratchets_baseline_forward(self, threshold):
        compressor = _compressor()
        compressor.threshold_tokens = threshold
        tolerated = _tolerated(compressor)
        baseline = threshold
        compressor.last_real_prompt_tokens = threshold - 1
        compressor.last_rough_tokens_when_real_prompt_fit = baseline

        rough = baseline + tolerated
        assert compressor.should_defer_preflight_to_real_usage(rough) is True
        assert compressor.last_rough_tokens_when_real_prompt_fit == rough


class TestGuardsSurviveAWidenedAperture:
    """The safety property that makes the dial shippable.

    With a very wide tolerance the growth test stops being the binding
    constraint, so the surrounding guards are the only thing preventing
    unbounded deferral.  Each is asserted directly.
    """

    def _wide(self, threshold: int = 100_000) -> ContextCompressor:
        compressor = _compressor(preflight_defer_growth_ratio=0.90)
        compressor.threshold_tokens = threshold
        return compressor

    def test_wide_ratio_is_actually_applied(self):
        compressor = self._wide()
        assert compressor.preflight_defer_growth_ratio == 0.90
        assert _tolerated(compressor) == 90_000

    @pytest.mark.parametrize(
        "ratio, expected_tolerated", [(0.05, 5_000), (0.20, 20_000), (0.90, 90_000)]
    )
    def test_configured_ratio_moves_the_predicates_real_boundary(
        self, ratio, expected_tolerated
    ):
        """Drive the PREDICATE (not the mirror helper) at the exact boundary for
        several apertures.  This is the direct config -> behaviour coupling: if
        the predicate ignored the configured ratio and kept the old hardcoded
        0.05, the 0.20 and 0.90 cases would refuse where they must defer.
        """
        threshold = 100_000
        baseline = threshold
        for rough, want in (
            (baseline + expected_tolerated, True),
            (baseline + expected_tolerated + 1, False),
        ):
            compressor = _compressor(preflight_defer_growth_ratio=ratio)
            compressor.threshold_tokens = threshold
            # The floor term must not mask the ratio term at this threshold.
            assert compressor.preflight_defer_growth_tokens == 4096
            compressor.last_real_prompt_tokens = threshold - 1
            compressor.awaiting_real_usage_after_compression = False
            compressor.last_rough_tokens_when_real_prompt_fit = baseline
            assert (
                compressor.should_defer_preflight_to_real_usage(rough) is want
            ), f"ratio={ratio} rough={rough} expected {want}"

    def test_configured_tokens_floor_moves_the_predicates_real_boundary(self):
        """Same for the absolute-tokens term, on a threshold small enough that
        the floor dominates the ratio."""
        threshold = 40_000  # ratio term at 0.05 = 2_000, floor wins
        baseline = threshold
        compressor = _compressor(preflight_defer_growth_tokens=30_000)
        compressor.threshold_tokens = threshold
        compressor.last_real_prompt_tokens = threshold - 1
        compressor.last_rough_tokens_when_real_prompt_fit = baseline
        # Growth of 20_000 is far beyond the default 4096 floor but inside the
        # configured 30_000 one.
        assert compressor.should_defer_preflight_to_real_usage(
            baseline + 20_000
        ) is True

        compressor = _compressor(preflight_defer_growth_tokens=30_000)
        compressor.threshold_tokens = threshold
        compressor.last_real_prompt_tokens = threshold - 1
        compressor.last_rough_tokens_when_real_prompt_fit = baseline
        assert compressor.should_defer_preflight_to_real_usage(
            baseline + 30_001
        ) is False

    def test_sub_threshold_rough_never_defers(self):
        compressor = self._wide()
        compressor.last_real_prompt_tokens = 50_000
        compressor.last_rough_tokens_when_real_prompt_fit = 50_000
        # rough < threshold_tokens: nothing to defer, compression would not
        # fire anyway.
        assert compressor.should_defer_preflight_to_real_usage(99_999) is False

    def test_no_real_reading_yet_never_defers(self):
        compressor = self._wide()
        compressor.last_real_prompt_tokens = 0
        compressor.awaiting_real_usage_after_compression = False
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000
        assert compressor.should_defer_preflight_to_real_usage(120_000) is False

    def test_real_reading_at_or_above_threshold_breaks_the_ratchet(self):
        """THE critical guard: one real provider reading at or above the
        threshold ends deferral, so a widened aperture cannot compound
        indefinitely."""
        compressor = self._wide()
        compressor.awaiting_real_usage_after_compression = False
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000

        # Just under: growth is well inside the wide tolerance, so it defers.
        compressor.last_real_prompt_tokens = 99_999
        assert compressor.should_defer_preflight_to_real_usage(150_000) is True

        # Exactly at the threshold: refuses regardless of how wide the
        # tolerance is.
        compressor.last_real_prompt_tokens = 100_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000
        assert compressor.should_defer_preflight_to_real_usage(150_000) is False

        # Above: same.
        compressor.last_real_prompt_tokens = 130_000
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000
        assert compressor.should_defer_preflight_to_real_usage(150_000) is False

    def test_awaiting_real_usage_still_defers_unconditionally(self):
        """#36718 one-turn defer must be untouched by the aperture."""
        compressor = self._wide()
        # Stale pre-compression value above threshold — would hit the
        # >= threshold => False short-circuit without the flag guard.
        compressor.last_real_prompt_tokens = 130_000
        compressor.awaiting_real_usage_after_compression = True
        assert compressor.should_defer_preflight_to_real_usage(120_000) is True

    def test_zero_baseline_never_defers(self):
        compressor = self._wide()
        compressor.last_real_prompt_tokens = 50_000
        compressor.awaiting_real_usage_after_compression = False
        compressor.last_rough_tokens_when_real_prompt_fit = 0
        compressor.last_compression_rough_tokens = 0
        assert compressor.should_defer_preflight_to_real_usage(120_000) is False


class TestZeroApertureIsTheMostConservativeBehaviour:
    def test_zero_pair_never_defers_on_growth(self):
        compressor = _compressor(
            preflight_defer_growth_tokens=0,
            preflight_defer_growth_ratio=0.0,
        )
        compressor.threshold_tokens = 100_000
        assert _tolerated(compressor) == 0

        compressor.last_real_prompt_tokens = 50_000
        compressor.awaiting_real_usage_after_compression = False
        compressor.last_rough_tokens_when_real_prompt_fit = 100_000
        # Any growth at all now refuses.
        assert compressor.should_defer_preflight_to_real_usage(100_001) is False
        # Zero growth is still tolerated (growth == 0 is not > 0).
        assert compressor.should_defer_preflight_to_real_usage(100_000) is True

    @pytest.mark.parametrize(
        "tokens, ratio",
        [
            (-5000, -0.5),
            (None, None),
            (0, 0.0),
        ],
    )
    def test_hostile_constructor_values_clamp_to_zero_not_unbounded(
        self, tokens, ratio
    ):
        compressor = _compressor(
            preflight_defer_growth_tokens=tokens,
            preflight_defer_growth_ratio=ratio,
        )
        assert compressor.preflight_defer_growth_tokens == 0
        assert compressor.preflight_defer_growth_ratio == 0.0

    def test_ratio_is_capped_at_one(self):
        compressor = _compressor(preflight_defer_growth_ratio=5.0)
        assert compressor.preflight_defer_growth_ratio == 1.0


# ── config parse seam (agent_init) ────────────────────────────────────────


def _config(**compression_keys) -> dict:
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    compression.update(compression_keys)
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path, **compression_keys):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config", lambda: _config(**compression_keys)
    )
    monkeypatch.setattr(
        config_mod, "load_config_readonly", lambda: _config(**compression_keys)
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        agent = AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="preflight-defer-aperture-test",
        )
    return agent


class TestPreflightDeferApertureConfig:
    def test_unset_keys_are_todays_constants(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        compressor = agent.context_compressor
        assert compressor.preflight_defer_growth_tokens == 4096
        assert compressor.preflight_defer_growth_ratio == 0.05

    def test_custom_values_are_honored(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch,
            tmp_path,
            preflight_defer_growth_tokens=16_384,
            preflight_defer_growth_ratio=0.25,
        )
        compressor = agent.context_compressor
        assert compressor.preflight_defer_growth_tokens == 16_384
        assert compressor.preflight_defer_growth_ratio == 0.25

    def test_integral_float_and_numeric_string_tokens_accepted(
        self, monkeypatch, tmp_path
    ):
        agent = _make_agent(
            monkeypatch,
            tmp_path,
            preflight_defer_growth_tokens=8192.0,
            preflight_defer_growth_ratio="0.10",
        )
        compressor = agent.context_compressor
        assert compressor.preflight_defer_growth_tokens == 8192
        assert compressor.preflight_defer_growth_ratio == pytest.approx(0.10)

    @pytest.mark.parametrize("bad", [True, False, "abc", None, 4.7, [1]])
    def test_malformed_tokens_falls_back_to_default(
        self, monkeypatch, tmp_path, bad
    ):
        agent = _make_agent(
            monkeypatch, tmp_path, preflight_defer_growth_tokens=bad
        )
        assert agent.context_compressor.preflight_defer_growth_tokens == 4096

    @pytest.mark.parametrize("bad", [True, False, "abc", None, [1]])
    def test_malformed_ratio_falls_back_to_default(
        self, monkeypatch, tmp_path, bad
    ):
        agent = _make_agent(
            monkeypatch, tmp_path, preflight_defer_growth_ratio=bad
        )
        assert agent.context_compressor.preflight_defer_growth_ratio == 0.05

    def test_negative_values_clamp_to_zero_never_widen(
        self, monkeypatch, tmp_path
    ):
        agent = _make_agent(
            monkeypatch,
            tmp_path,
            preflight_defer_growth_tokens=-9999,
            preflight_defer_growth_ratio=-1.0,
        )
        compressor = agent.context_compressor
        assert compressor.preflight_defer_growth_tokens == 0
        assert compressor.preflight_defer_growth_ratio == 0.0

    def test_adjacent_optin_features_stay_default_off(self, monkeypatch, tmp_path):
        """The new config keys sit immediately next to proactive_prune_tokens
        and micro_compact; neither may be disturbed."""
        agent = _make_agent(monkeypatch, tmp_path)
        compressor = agent.context_compressor
        assert compressor.proactive_prune_tokens == 0
        assert compressor._micro_compact_enabled is False


class TestApertureKeysExistInDefaultsAndBustTheGatewayAgentCache:
    def test_keys_are_present_in_config_defaults_at_todays_constants(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        compression = DEFAULT_CONFIG["compression"]
        assert compression["preflight_defer_growth_tokens"] == 4096
        assert compression["preflight_defer_growth_ratio"] == 0.05
        # Adjacent opt-in features must remain off in the shipped defaults.
        assert compression["proactive_prune_tokens"] == 0
        assert compression["micro_compact"] is False

    def test_keys_bust_the_gateway_agent_cache(self):
        """Both values are baked into the compressor at construction time, so a
        live config edit must rebuild the cached gateway agent — otherwise the
        dial would silently have no effect until an unrelated eviction."""
        from gateway.run import GatewayRunner

        keys = set(GatewayRunner._CACHE_BUSTING_CONFIG_KEYS)
        assert ("compression", "preflight_defer_growth_tokens") in keys
        assert ("compression", "preflight_defer_growth_ratio") in keys

        out = GatewayRunner._extract_cache_busting_config(
            {
                "compression": {
                    "preflight_defer_growth_tokens": 16_384,
                    "preflight_defer_growth_ratio": 0.25,
                }
            }
        )
        assert out["compression.preflight_defer_growth_tokens"] == 16_384
        assert out["compression.preflight_defer_growth_ratio"] == 0.25

    def test_absent_keys_contribute_none_so_no_spurious_rebuild(self):
        from gateway.run import GatewayRunner

        out = GatewayRunner._extract_cache_busting_config({})
        assert out["compression.preflight_defer_growth_tokens"] is None
        assert out["compression.preflight_defer_growth_ratio"] is None
