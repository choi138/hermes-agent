"""``compression.summary_prompt_drift_probe`` — a read-only R5 measurement probe.

WHAT IS UNDER TEST, AND WHAT IS NOT
-----------------------------------
This slice ships NO cache, no cache-consumption path, no background thread and
no shadow provider call.  It ships one default-off probe that records whether
the summariser prompt's auto-derived focus block at the last quiescent point
matches the one used at the next compaction.  The win is zero at every gate
setting: the auxiliary provider call in ``_generate_summary`` is unconditional.

So the load-bearing assertions here are:

1. GATE OFF is byte-identical to HEAD — the module is not even imported.
2. GATE ON does not move one prompt byte, does not skip the provider call, does
   not construct a second compressor, spawns no thread, writes no durable state
   and mutates neither the transcript nor the compressor.
3. The comparison itself is CORRECT IN BOTH DIRECTIONS.  A probe whose answer
   can only ever be "differs" would "confirm" the predicted zero hit rate
   without measuring anything, so ``test_agreement_is_reported_when_the_focus_
   block_is_unchanged`` is as important as the drift test next to it.
4. Structural mismatches (no park, wrong session, stale date, repeat pass,
   explicit ``/compress <focus>``) are each reported as themselves and never as
   prompt drift.
"""

from __future__ import annotations

import contextlib
import copy
import io
import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from agent import summary_prompt_drift as drift
from agent.context_compressor import ContextCompressor
from agent.turn_finalizer import finalize_turn


SESSION_ID = "drift-probe-session"


@pytest.fixture(autouse=True)
def _clean_probe_state():
    drift.reset()
    yield
    drift.reset()


# ── fixtures / helpers ────────────────────────────────────────────────────


def _raw_compressor(**kwargs) -> ContextCompressor:
    """A compressor exactly as ``__init__`` leaves it — nothing stamped on."""
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


def _compressor(*, gate: bool = False, **kwargs) -> ContextCompressor:
    compressor = _raw_compressor(**kwargs)
    compressor._session_id = SESSION_ID
    compressor._r5_prompt_drift_probe_enabled = gate
    return compressor


def _messages(n: int = 14) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message number {i} with some filler text " * 4,
        }
        for i in range(n)
    ]


def _tool_only_messages(n: int = 14) -> list[dict]:
    """A transcript with zero real user turns (cron / tool-loop population)."""
    out: list[dict] = []
    for i in range(n):
        if i % 2 == 0:
            out.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"c{i}",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                }
            )
        else:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": f"c{i - 1}",
                    "content": f"tool output number {i} " * 12,
                }
            )
    return out


def _summary_response() -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = (
        "[CONTEXT SUMMARY]: the earlier turns discussed filler text"
    )
    return response


class _StubBudget:
    used = 5
    max_total = 3
    remaining = 0


class _StubAgent:
    """Minimal agent surface ``finalize_turn`` reads from.

    Shape copied from tests/agent/test_turn_finalizer_cleanup_guard.py so the
    park is exercised through the REAL production call site rather than a
    test-local reimplementation of it.
    """

    def __init__(self, compressor, *, session_id: str = SESSION_ID):
        self.max_iterations = 3
        self.iteration_budget = _StubBudget()
        self.context_compressor = compressor
        self.model = "stub/model"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = session_id
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.persisted = 0
        for attr in (
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"

    def _save_trajectory(self, *a, **k):
        pass

    def _cleanup_task_resources(self, *a, **k):
        pass

    def _drop_trailing_empty_response_scaffolding(self, *a, **k):
        pass

    def _persist_session(self, *a, **k):
        self.persisted += 1

    def _emit_status(self, *a, **k):
        pass

    def _safe_print(self, *a, **k):
        pass

    def _handle_max_iterations(self, messages, n):
        return "PARTIAL SUMMARY FROM MODEL"

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **k):
        pass


def _finalize(agent, messages, *, final_response="done"):
    """Run the production park site (``finalize_turn``) on *messages*."""
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="do a thing",
        original_user_message="do a thing",
        _should_review_memory=False,
        _turn_exit_reason="completed",
    )


def _compact(compressor, messages, **kwargs):
    with patch(
        "agent.context_compressor.call_llm", return_value=_summary_response()
    ) as call_llm:
        result = compressor.compress(
            messages, current_tokens=999_999, force=True, **kwargs
        )
    return result, call_llm


def _park_then_compact(compressor, messages, *, mutate=None, **compress_kwargs):
    """Park at the quiescent point, optionally mutate, then compact.

    Returns ``(snapshot, call_llm_mock)``.
    """
    agent = _StubAgent(compressor)
    _finalize(agent, messages)
    if mutate is not None:
        mutate(messages)
    _, call_llm = _compact(compressor, messages, **compress_kwargs)
    return drift.snapshot(), call_llm


# ── 1. gate off: nothing happens, and the module is never imported ────────


class TestGateOffIsInert:
    def test_default_construction_declares_the_gate_false(self):
        # This declaration is load-bearing: the agent_init stamp is
        # hasattr-guarded, so without it the config key is a SILENT no-op and
        # the probe would report zero observations — indistinguishable from the
        # zero hit rate it predicts. Built WITHOUT the test helper's stamp so
        # the assertion is about __init__, not about the helper.
        compressor = _raw_compressor()
        assert "_r5_prompt_drift_probe_enabled" in vars(compressor)
        assert compressor._r5_prompt_drift_probe_enabled is False
        assert hasattr(compressor, "_r5_prompt_drift_probe_enabled")

    def test_no_park_and_no_observation_with_the_gate_off(self):
        compressor = _compressor(gate=False)
        snap, call_llm = _park_then_compact(compressor, _messages())
        assert snap["park"] == 0
        assert snap["observe"] == 0
        assert snap["park_failed"] == 0
        assert snap["parked_sessions"] == 0
        # The compaction really did happen, so the zeros are not vacuous.
        assert call_llm.call_count == 1

    def test_gate_off_sets_no_new_attribute_on_agent_or_compressor(self):
        compressor = _compressor(gate=False)
        before = set(vars(compressor))
        agent = _StubAgent(compressor)
        agent_before = set(vars(agent))
        messages = _messages()
        _finalize(agent, messages)
        _compact(compressor, messages)
        assert "_r5_focus_was_explicit" not in vars(compressor)
        assert "_r5_drift_turn_seq" not in vars(agent)
        # finalize_turn and compress() legitimately set attributes of their
        # own; assert only that the probe contributed none of them.
        assert not [k for k in set(vars(agent)) - agent_before if "_r5_" in k]
        assert not [k for k in set(vars(compressor)) - before if "_r5_" in k]

    def test_module_is_not_imported_when_the_gate_is_off(self):
        """A default install must not even import the probe module."""
        script = (
            "import sys\n"
            "from unittest.mock import MagicMock, patch\n"
            "from agent.context_compressor import ContextCompressor\n"
            "resp = MagicMock()\n"
            "resp.choices = [MagicMock()]\n"
            "resp.choices[0].message.content = '[CONTEXT SUMMARY]: x'\n"
            "with patch('agent.context_compressor.get_model_context_length',"
            " return_value=100_000):\n"
            "    c = ContextCompressor(model='m', quiet_mode=True,"
            " protect_first_n=2, protect_last_n=2)\n"
            "    _ = c.context_length\n"
            "msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',"
            " 'content': 'filler text ' * 8} for i in range(14)]\n"
            "with patch('agent.context_compressor.call_llm', return_value=resp):\n"
            "    out = c.compress(msgs, current_tokens=999999, force=True)\n"
            "assert len(out) < len(msgs), 'compaction did not run'\n"
            "assert 'agent.summary_prompt_drift' not in sys.modules, "
            "'probe module imported with the gate OFF'\n"
            "print('GATE_OFF_CLEAN')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "GATE_OFF_CLEAN" in proc.stdout

    def test_module_is_imported_when_the_gate_is_on(self):
        """Positive control for the assertion above."""
        script = (
            "import sys\n"
            "from unittest.mock import MagicMock, patch\n"
            "from agent.context_compressor import ContextCompressor\n"
            "resp = MagicMock()\n"
            "resp.choices = [MagicMock()]\n"
            "resp.choices[0].message.content = '[CONTEXT SUMMARY]: x'\n"
            "with patch('agent.context_compressor.get_model_context_length',"
            " return_value=100_000):\n"
            "    c = ContextCompressor(model='m', quiet_mode=True,"
            " protect_first_n=2, protect_last_n=2)\n"
            "    _ = c.context_length\n"
            "c._r5_prompt_drift_probe_enabled = True\n"
            "msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',"
            " 'content': 'filler text ' * 8} for i in range(14)]\n"
            "with patch('agent.context_compressor.call_llm', return_value=resp):\n"
            "    c.compress(msgs, current_tokens=999999, force=True)\n"
            "assert 'agent.summary_prompt_drift' in sys.modules\n"
            "print('GATE_ON_IMPORTED')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "GATE_ON_IMPORTED" in proc.stdout


# ── 2. gate on moves no prompt byte and skips no provider call ────────────


def _captured_prompt(compressor, messages):
    with patch(
        "agent.context_compressor.call_llm", return_value=_summary_response()
    ) as call_llm:
        compressor.compress(messages, current_tokens=999_999, force=True)
    assert call_llm.call_count == 1
    kwargs = call_llm.call_args.kwargs
    return kwargs["messages"][0]["content"], kwargs


class TestGateOnChangesNothingAboutTheRequest:
    @pytest.mark.parametrize("iterative", [False, True])
    def test_prompt_and_call_kwargs_are_byte_identical(self, iterative):
        previous = "[CONTEXT SUMMARY]: earlier work" if iterative else None

        off = _compressor(gate=False)
        off._previous_summary = previous
        prompt_off, kwargs_off = _captured_prompt(off, _messages())

        on = _compressor(gate=True)
        on._previous_summary = previous
        prompt_on, kwargs_on = _captured_prompt(on, _messages())

        assert prompt_on == prompt_off
        assert set(kwargs_on) == set(kwargs_off)
        assert kwargs_on["task"] == kwargs_off["task"]
        # Sanity: the focus block really is inside the hashed prompt bytes, so
        # focus agreement is genuinely a necessary condition for an
        # exact-prompt key to hit.
        assert "FOCUS TOPIC:" in prompt_on
        assert "Recent user focus:" in prompt_on

    def test_provider_call_is_not_skipped_even_on_a_focus_agreement(self):
        """The point of outcome (B): an agreement changes only a counter."""
        compressor = _compressor(gate=True)
        messages = _messages()
        snap, call_llm = _park_then_compact(compressor, messages)
        assert snap["focus_agree"] == 1, snap
        # A hit-shaped observation still made the real auxiliary call.
        assert call_llm.call_count == 1
        assert compressor._previous_summary is not None
        assert "the earlier turns discussed filler text" in (
            compressor._previous_summary
        )

    def test_aux_telemetry_still_populated_with_the_gate_on(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _park_then_compact(compressor, messages)
        telemetry = compressor._last_compression_telemetry
        assert isinstance(telemetry, dict)
        assert telemetry.get("aux_prompt_tokens")
        assert "aux_call_duration_ms" in telemetry


# ── 3. wiring: the config key must actually reach the compressor ──────────


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
    from hermes_state import SessionDB
    from run_agent import AIAgent

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
            session_id="drift-probe-wiring-test",
        )
    return agent


class TestConfigWiring:
    def test_default_is_off_end_to_end(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path)
        assert agent.context_compressor._r5_prompt_drift_probe_enabled is False

    def test_enabled_key_reaches_the_compressor(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, summary_prompt_drift_probe=True
        )
        assert agent.context_compressor._r5_prompt_drift_probe_enabled is True

    def test_adjacent_optin_features_are_undisturbed(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, summary_prompt_drift_probe=True
        )
        compressor = agent.context_compressor
        assert compressor.proactive_prune_tokens == 0
        assert compressor._micro_compact_enabled is False

    def test_key_present_and_false_in_shipped_defaults(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        compression = DEFAULT_CONFIG["compression"]
        assert compression["summary_prompt_drift_probe"] is False
        assert compression["proactive_prune_tokens"] == 0
        assert compression["micro_compact"] is False

    def test_non_boolean_config_values_fail_closed(self, monkeypatch, tmp_path):
        agent = _make_agent(
            monkeypatch, tmp_path, summary_prompt_drift_probe="not-a-bool"
        )
        assert agent.context_compressor._r5_prompt_drift_probe_enabled is False

    def test_foreign_compressor_without_the_attribute_is_skipped(self):
        """The park guard must skip an alternate engine, not raise."""

        class _ForeignEngine:
            last_prompt_tokens = 0

        agent = _StubAgent(_ForeignEngine())
        result = _finalize(agent, _messages())
        assert result["final_response"] == "done"
        assert drift.snapshot()["park"] == 0
        assert drift.snapshot()["park_failed"] == 0


# ── 4. the proof mechanism itself, in both directions ─────────────────────


class TestFocusDriftDetection:
    def test_a_new_user_turn_is_reported_as_drift(self):
        compressor = _compressor(gate=True)
        messages = _messages()

        def _append_user(msgs):
            msgs.append({"role": "user", "content": "now do the next thing"})

        snap, _ = _park_then_compact(compressor, messages, mutate=_append_user)
        assert snap["focus_differ"] == 1, snap
        assert snap["focus_agree"] == 0
        assert snap["focus_both_none"] == 0
        assert snap["observe_no_entry"] == 0

    def test_agreement_is_reported_when_the_focus_block_is_unchanged(self):
        """The single most important guard against a false negative.

        A probe that can only ever answer "differs" would "confirm" the
        predicted zero hit rate while measuring nothing.
        """
        compressor = _compressor(gate=True)
        snap, _ = _park_then_compact(compressor, _messages())
        assert snap["focus_agree"] == 1, snap
        assert snap["focus_differ"] == 0

    def test_zero_real_user_turns_is_counted_separately(self):
        compressor = _compressor(gate=True)
        snap, _ = _park_then_compact(compressor, _tool_only_messages())
        assert snap["focus_both_none"] == 1, snap
        assert snap["focus_agree"] == 0
        assert snap["focus_differ"] == 0

    def test_explicit_compress_focus_does_not_pollute_the_auto_population(self):
        compressor = _compressor(gate=True)
        snap, _ = _park_then_compact(
            compressor, _messages(), focus_topic="the deploy pipeline"
        )
        assert snap["explicit_focus"] == 1, snap
        assert snap["focus_agree"] == 0
        assert snap["focus_differ"] == 0
        assert snap["focus_both_none"] == 0

    def test_production_turn_ordering_never_agrees(self):
        """The headline prediction, pinned as a test.

        Production order inside a turn is: append the user message -> run the
        compaction gate -> append the assistant reply -> park.  So a park at the
        end of turn N sees the newest three real user turns as
        [U_{N-2}, U_{N-1}, U_N] and the next compaction — which fires AFTER
        U_{N+1} was appended — sees [U_{N-1}, U_N, U_{N+1}].

        This is also the single ordering mistake that would make a measurement
        run report a spurious 100%: parking and then compacting without
        appending the next user message first measures a within-turn repeat, not
        a cross-turn precompute.
        """
        compressor = _compressor(gate=True)
        agent = _StubAgent(compressor)
        messages: list[dict] = []
        compactions = 0
        for turn in range(9):
            messages.append(
                {
                    "role": "user",
                    "content": f"turn {turn} instruction " + "detail " * 30,
                }
            )
            if turn % 3 == 2:
                messages = list(_compact(compressor, messages)[0])
                compactions += 1
            messages.append(
                {"role": "assistant", "content": f"reply {turn} " + "body " * 40}
            )
            _finalize(agent, messages)

        snap = drift.snapshot()
        assert compactions == 3
        # The earliest gate is a structural no-op (too few messages to have a
        # compressible middle), so it never reaches the summariser — which is
        # exactly the denominator hygiene this seam gives for free.
        assert snap["observe"] >= 2, snap
        assert snap["focus_differ"] == snap["observe"], snap
        assert snap["focus_agree"] == 0, snap
        assert snap["observe_no_entry"] == 0
        assert snap["repeat_observation"] == 0
        assert snap["park"] == 9

    def test_one_side_having_no_block_is_a_proven_miss_not_an_agreement(self):
        compressor = _compressor(gate=True)
        messages = _tool_only_messages()

        def _append_user(msgs):
            msgs.append({"role": "user", "content": "please summarise"})

        snap, _ = _park_then_compact(compressor, messages, mutate=_append_user)
        assert snap["focus_none_mismatch"] == 1, snap
        assert snap["focus_differ"] == 1
        assert snap["focus_both_none"] == 0
        assert snap["focus_agree"] == 0


class TestStructuralGuardsAreNeverReportedAsDrift:
    def test_missing_park_reports_no_entry(self):
        compressor = _compressor(gate=True)
        _compact(compressor, _messages())
        snap = drift.snapshot()
        assert snap["observe"] == 1
        assert snap["observe_no_entry"] == 1
        assert snap["focus_agree"] == 0 and snap["focus_differ"] == 0

    def test_a_park_from_another_session_is_not_compared(self):
        compressor = _compressor(gate=True)
        agent = _StubAgent(compressor, session_id="some-other-session")
        messages = _messages()
        _finalize(agent, messages)
        _compact(compressor, messages)
        snap = drift.snapshot()
        assert snap["park"] == 1
        assert snap["observe_no_entry"] == 1
        assert snap["focus_agree"] == 0 and snap["focus_differ"] == 0

    def test_corrupted_entry_session_reports_different_session(self):
        """White-box: the belt-and-braces guard reports itself, not drift."""
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        entry = drift.parked_entry_for_test(SESSION_ID)
        assert entry is not None
        entry.session_id = "mangled"
        _compact(compressor, messages)
        snap = drift.snapshot()
        assert snap["observe_different_session"] == 1, snap
        assert snap["focus_agree"] == 0 and snap["focus_differ"] == 0

    def test_a_park_that_straddles_midnight_reports_stale_date(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        import hermes_time

        class _Yesterday:
            @staticmethod
            def strftime(fmt):
                return "1999-01-01"

        with patch.object(hermes_time, "now", lambda: _Yesterday()):
            _finalize(_StubAgent(compressor), messages)
        _compact(compressor, messages)
        snap = drift.snapshot()
        assert snap["stale_date"] == 1, snap
        # The focus block was byte-identical, yet nothing is reported as
        # agreement — a structural mismatch must never flatter the numerator.
        assert snap["focus_agree"] == 0 and snap["focus_differ"] == 0

    def test_a_second_pass_against_one_park_reports_repeat_observation(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        first = _compact(compressor, messages)[0]
        _compact(compressor, list(first))
        snap = drift.snapshot()
        assert snap["observe"] == 2
        assert snap["repeat_observation"] == 1, snap
        assert snap["focus_agree"] + snap["focus_differ"] <= 1


class TestHashConsistency:
    """``prev_summary_hash`` must not manufacture false "differs".

    The foreground redacts ``_previous_summary`` IN PLACE before it builds the
    prompt, so the park has to hash the redacted form or the two sides would
    disagree for a reason that is not drift — exactly the measurement artifact
    this slice exists to avoid.
    """

    def test_iterative_compaction_agrees_on_the_previous_summary_hash(self):
        compressor = _compressor(gate=True)
        long_messages = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"turn {i} " + "filler text " * 20,
            }
            for i in range(24)
        ]
        # First compaction establishes a real _previous_summary and inserts the
        # compaction marker the self-heal scan looks for.
        after_first, _ = _compact(compressor, long_messages)
        assert compressor._previous_summary
        transcript = list(after_first) + [
            {"role": "user", "content": "next instruction " + "detail " * 20},
            {"role": "assistant", "content": "acknowledged " * 40},
        ]
        _finalize(_StubAgent(compressor), transcript)
        parked = drift.parked_entry_for_test(SESSION_ID)
        assert parked is not None
        _compact(compressor, transcript)
        observed = drift.snapshot()["component_observations"][-1]
        assert observed["prev_summary_hash"] == parked.prev_summary_hash
        # Non-vacuous: the hash is over real content, not over "".
        assert parked.prev_summary_hash != drift._text_digest("PREV1", "")

    def test_park_hashes_the_redacted_form_not_the_raw_one(self):
        """A resumed pre-redaction handoff is the case line 3588 exists for."""
        from agent.context_compressor import _redact_compaction_text

        raw = "[CONTEXT SUMMARY]: earlier work used sk-abcdefghijklmnopqrstuvwx"
        redacted = _redact_compaction_text(raw)
        assert redacted != raw, "fixture must actually contain a secret shape"

        compressor = _compressor(gate=True)
        compressor._previous_summary = raw
        _finalize(_StubAgent(compressor), _messages())
        parked = drift.parked_entry_for_test(SESSION_ID)
        assert parked is not None
        assert parked.prev_summary_hash == drift._text_digest("PREV1", redacted)
        assert parked.prev_summary_hash != drift._text_digest("PREV1", raw)
        # And the park did NOT store the redacted form back onto the compressor.
        assert compressor._previous_summary == raw

    def test_compaction_redaction_is_idempotent(self):
        """Why the end-to-end agreement above holds: the observe side hashes a
        value that has already been through the same redactor."""
        from agent.context_compressor import _redact_compaction_text

        raw = "handoff sk-abcdefghijklmnopqrstuvwx and ghp_ABCDEFGHIJKLMNOP"
        once = _redact_compaction_text(raw)
        assert _redact_compaction_text(once) == once


# ── 5. the park is read-only ──────────────────────────────────────────────


class TestParkPurity:
    def test_transcript_is_untouched_by_the_park(self):
        baseline_messages = _messages()
        off = _StubAgent(_compressor(gate=False))
        _finalize(off, baseline_messages)

        probe_messages = _messages()
        expected = copy.deepcopy(probe_messages)
        on = _StubAgent(_compressor(gate=True))
        _finalize(on, probe_messages)

        assert probe_messages == baseline_messages
        assert probe_messages == expected
        assert len(probe_messages) == len(expected)

    def test_compressor_state_is_untouched_by_the_park(self):
        compressor = _compressor(gate=True)
        before = dict(vars(compressor))
        _finalize(_StubAgent(compressor), _messages())
        after = dict(vars(compressor))
        assert set(after) == set(before)
        for key, value in before.items():
            assert after[key] == value, key

    def test_park_retains_no_reference_to_the_message_list(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        entry = drift.parked_entry_for_test(SESSION_ID)
        assert entry is not None
        focus_hash = entry.focus_hash
        messages.append({"role": "user", "content": "a brand new instruction"})
        messages[0]["content"] = "rewritten in place"
        assert drift.parked_entry_for_test(SESSION_ID).focus_hash == focus_hash

    def test_park_is_deterministic(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        first = drift.parked_entry_for_test(SESSION_ID).focus_hash
        _finalize(_StubAgent(compressor), messages)
        second = drift.parked_entry_for_test(SESSION_ID).focus_hash
        assert first == second

    def test_park_constructs_no_second_compressor_and_probes_no_model(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        constructed = []
        real_init = ContextCompressor.__init__

        def _counting_init(self, *a, **k):
            constructed.append(1)
            return real_init(self, *a, **k)

        def _boom(*a, **k):
            raise AssertionError("model metadata resolver reached by the park")

        import agent.model_metadata as model_metadata

        with patch.object(ContextCompressor, "__init__", _counting_init), \
                patch.object(model_metadata, "get_model_context_length", _boom), \
                patch.object(model_metadata, "save_context_length", _boom), \
                patch("agent.context_compressor.get_model_context_length", _boom):
            _finalize(_StubAgent(compressor), messages)

        assert constructed == []
        assert drift.snapshot()["park"] == 1

    def test_park_starts_no_thread_and_makes_no_provider_call(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        before = threading.active_count()

        def _boom(*a, **k):
            raise AssertionError("provider call made by the park")

        with patch("agent.context_compressor.call_llm", _boom):
            _finalize(_StubAgent(compressor), messages)

        assert threading.active_count() == before
        assert drift.snapshot()["park"] == 1

    def test_park_never_invokes_the_memory_provider_hook(self):
        from agent.memory_manager import MemoryManager

        compressor = _compressor(gate=True)

        def _boom(*a, **k):
            raise AssertionError("on_pre_compress reached by the park")

        with patch.object(MemoryManager, "on_pre_compress", _boom):
            _finalize(_StubAgent(compressor), _messages())
        assert drift.snapshot()["park"] == 1

    def test_park_writes_no_durable_compression_state(self):
        compressor = _compressor(gate=True)
        forbidden = (
            "_record_compression_failure_cooldown",
            "_clear_compression_failure_cooldown",
            "_record_ineffective_compression_verdict",
            "_persist_ineffective_compression_count",
            "_record_aux_compression_call",
        )

        def _boom(*a, **k):
            raise AssertionError("durable writer reached by the park")

        with contextlib.ExitStack() as stack:
            for name in forbidden:
                assert hasattr(compressor, name), name
                stack.enter_context(patch.object(compressor, name, _boom))
            _finalize(_StubAgent(compressor), _messages())

        assert drift.snapshot()["park"] == 1
        assert compressor._previous_summary is None
        assert compressor._micro_compact_rolling_summary == ""
        assert compressor._active_compression_telemetry is None


# ── 6. failure isolation ──────────────────────────────────────────────────


class TestFailureIsolation:
    def test_a_raising_focus_derivation_is_counted_and_swallowed(self):
        compressor = _compressor(gate=True)

        def _boom(*a, **k):
            raise RuntimeError("derivation exploded")

        agent = _StubAgent(compressor)
        with patch.object(
            ContextCompressor, "_derive_auto_focus_topic", _boom
        ):
            result = _finalize(agent, _messages())
        assert result["final_response"] == "done"
        assert agent.persisted == 1
        snap = drift.snapshot()
        assert snap["park_failed"] == 1
        assert snap["park"] == 0

    def test_a_raising_observe_does_not_break_the_compaction(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)

        def _boom(**k):
            raise RuntimeError("observe exploded")

        with patch.object(drift, "observe", _boom):
            result, call_llm = _compact(compressor, messages)
        assert call_llm.call_count == 1
        assert len(result) < len(messages)
        assert "the earlier turns discussed filler text" in (
            compressor._previous_summary or ""
        )

    def test_a_broken_clock_still_parks(self):
        compressor = _compressor(gate=True)
        import hermes_time

        def _boom():
            raise RuntimeError("no clock")

        with patch.object(hermes_time, "now", _boom):
            _finalize(_StubAgent(compressor), _messages())
        entry = drift.parked_entry_for_test(SESSION_ID)
        assert entry is not None
        assert entry.today_str == ""
        assert drift.snapshot()["park"] == 1


# ── 7. micro-compaction is not instrumented ───────────────────────────────


class TestMicroCompactionIsUntouched:
    def test_micro_compaction_produces_no_park_and_no_observation(self):
        compressor = _compressor(gate=True)
        compressor._micro_compact_enabled = True
        messages = _messages(20)
        # ``_micro_summarize_one`` imports call_llm from agent.auxiliary_client
        # at call time, so that is the seam to stub — patching the
        # context_compressor global would let a real provider resolution run.
        import agent.auxiliary_client as auxiliary_client

        with patch.object(
            auxiliary_client, "call_llm", return_value=_summary_response()
        ) as call_llm:
            compressor._micro_compact(messages)
        assert call_llm.call_count >= 1
        snap = drift.snapshot()
        assert snap["observe"] == 0, snap
        assert snap["component_observations"] == []


# ── 8. store bounds, concurrency, snapshot isolation ──────────────────────


class TestStoreBounds:
    def test_lru_caps_the_store_at_sixteen_sessions(self):
        for index in range(40):
            drift.park(
                session_id=f"s{index}",
                focus_block=f"Recent user focus:\n- item {index}",
                prev_summary_redacted="",
                has_user_turn=True,
                today_str="2026-08-04",
                msg_count=10,
                turn_seq=index,
                elapsed_ms=0,
            )
        snap = drift.snapshot()
        assert snap["park"] == 40
        assert snap["parked_sessions"] == 16
        # Newest survive, oldest evicted.
        assert snap["parked_session_ids"][-1] == "s39"
        assert "s0" not in snap["parked_session_ids"]

    def test_snapshot_is_independent_of_later_activity(self):
        drift.park(
            session_id="a",
            focus_block="focus a",
            prev_summary_redacted="",
            has_user_turn=True,
            today_str="2026-08-04",
            msg_count=1,
            turn_seq=1,
            elapsed_ms=3,
        )
        snap = drift.snapshot()
        assert snap["park"] == 1
        assert snap["park_ms"] == [3]
        drift.park(
            session_id="b",
            focus_block="focus b",
            prev_summary_redacted="",
            has_user_turn=True,
            today_str="2026-08-04",
            msg_count=1,
            turn_seq=2,
            elapsed_ms=9,
        )
        assert snap["park"] == 1
        assert snap["park_ms"] == [3]
        assert drift.snapshot()["park"] == 2

    def test_concurrent_park_and_observe_never_raise_or_tear(self):
        errors: list[BaseException] = []

        def _parker(index: int):
            try:
                for step in range(40):
                    drift.park(
                        session_id=f"sess{index}",
                        focus_block=f"focus {index}-{step}",
                        prev_summary_redacted="prev",
                        has_user_turn=True,
                        today_str="2026-08-04",
                        msg_count=step,
                        turn_seq=step,
                        elapsed_ms=step,
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        def _observer(index: int):
            try:
                for step in range(40):
                    drift.observe(
                        session_id=f"sess{index}",
                        focus_topic=f"focus {index}-{step}",
                        explicit_focus=False,
                        prev_summary="prev",
                        has_user_turn=True,
                        today_str="2026-08-04",
                        window_text="window",
                        window_bounded=False,
                        budget=1000,
                        memory_context="",
                        span_len=4,
                    )
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=_parker, args=(i,)) for i in range(4)
        ] + [threading.Thread(target=_observer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        snap = drift.snapshot()
        assert snap["park"] == 160
        assert snap["observe"] == 160
        assert snap["observe_failed"] == 0
        assert snap["park_failed"] == 0
        # Every observation landed in exactly one bucket.
        accounted = (
            snap["observe_no_entry"]
            + snap["observe_different_session"]
            + snap["stale_date"]
            + snap["repeat_observation"]
            + snap["focus_agree"]
            + snap["focus_differ"]
            + snap["focus_both_none"]
        )
        assert accounted == snap["observe"]


class TestSessionBoundaryClears:
    def test_on_session_end_drops_the_parked_entry(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        assert drift.parked_entry_for_test(SESSION_ID) is not None
        compressor.on_session_end(SESSION_ID, messages)
        assert drift.parked_entry_for_test(SESSION_ID) is None

    def test_on_session_reset_drops_the_parked_entry(self):
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        assert drift.parked_entry_for_test(SESSION_ID) is not None
        compressor.on_session_reset()
        assert drift.parked_entry_for_test(SESSION_ID) is None


# ── 9. cost accounting is readable from one place ─────────────────────────


class TestCostAccountingIsReadable:
    def test_park_and_observe_counters_share_one_store(self):
        compressor = _compressor(gate=True)
        snap, _ = _park_then_compact(compressor, _messages())
        assert snap["park"] == 1
        assert len(snap["park_ms"]) == 1
        assert snap["park_ms"][0] >= 0
        # Sub-millisecond resolution, or the cost question is unanswerable.
        assert len(snap["park_us"]) == 1
        assert snap["park_us"][0] > 0
        assert snap["observe"] == 1
        assert len(snap["park_age_ms"]) == 1
        assert snap["park_turn_seq"] == [1]
        assert len(snap["component_observations"]) == 1
        observation = snap["component_observations"][0]
        for key in (
            "window_hash",
            "prev_summary_hash",
            "memory_hash",
            "budget",
            "span_len",
            "window_chars",
            "window_bounded",
        ):
            assert key in observation, key

    def test_parked_entry_cannot_hold_prompt_or_summary_content(self):
        """Serving a wrong summary must be unrepresentable, not merely
        unreached."""
        fields = set(drift._ParkedEntry.__dataclass_fields__)
        assert fields == {
            "session_id",
            "focus_hash",
            "focus_is_none",
            "prev_summary_hash",
            "has_user_turn",
            "today_str",
            "msg_count",
            "turn_seq",
            "park_monotonic",
            "observe_count",
        }
        compressor = _compressor(gate=True)
        messages = _messages()
        _finalize(_StubAgent(compressor), messages)
        entry = drift.parked_entry_for_test(SESSION_ID)
        text = " ".join(
            str(getattr(entry, name)) for name in fields
        )
        assert "Recent user focus" not in text
        assert "message number" not in text


class TestProbeDebugLinesAreInert:
    def _run(self, *, logging_enabled: bool):
        compressor = _compressor(gate=True)
        probe_logger = logging.getLogger("agent.summary_prompt_drift")
        previous = probe_logger.disabled
        probe_logger.disabled = not logging_enabled
        try:
            messages = _messages()
            _finalize(_StubAgent(compressor), messages)
            result, _ = _compact(compressor, messages)
        finally:
            probe_logger.disabled = previous
        return result, drift.snapshot()

    def test_result_and_counters_are_identical_either_way(self):
        with_logging, snap_on = self._run(logging_enabled=True)
        drift.reset()
        without_logging, snap_off = self._run(logging_enabled=False)
        assert with_logging == without_logging
        for key in ("park", "observe", "focus_agree", "focus_differ"):
            assert snap_on[key] == snap_off[key], key

    def test_both_probe_lines_carry_the_pinned_prefix_at_debug(self, caplog):
        with caplog.at_level(
            logging.DEBUG, logger="agent.summary_prompt_drift"
        ):
            self._run(logging_enabled=True)
        text = "\n".join(
            record.getMessage()
            for record in caplog.records
            if "hermes.r5probe" in record.getMessage()
        )
        assert "drift_park" in text
        assert "drift_observe" in text
        for field in ("focus=", "focus_agree=", "reason=", "park_age_ms="):
            assert field in text, field

    def test_probe_lines_are_silent_at_default_verbosity(self, caplog):
        with caplog.at_level(logging.INFO):
            self._run(logging_enabled=True)
        assert not [
            record
            for record in caplog.records
            if "hermes.r5probe" in record.getMessage()
        ]
