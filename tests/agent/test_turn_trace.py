"""Tests for agent.turn_trace (per-turn waterfall tracing).

Covers the env gate (everything is a no-op when HERMES_TURN_TRACE is unset),
the single-JSON-line-per-turn sink contract, trace carrying across threads
(bind/get_bound, adopt), and error tolerance (tracing must never raise into
the turn).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent import turn_trace
from agent.turn_trace import _NULL_SPAN, TurnTrace


class _Carrier:
    """Stand-in for the shared object (agent instance) traces bind to."""


@pytest.fixture(autouse=True)
def _clean_trace_state(monkeypatch):
    """Isolate env gate and thread-local current between tests."""
    monkeypatch.delenv("HERMES_TURN_TRACE", raising=False)
    monkeypatch.delenv("HERMES_TURN_TRACE_FILE", raising=False)
    turn_trace.adopt(None)
    yield
    turn_trace.adopt(None)


@pytest.fixture
def sink(tmp_path, monkeypatch):
    """Enable tracing with the sink redirected into tmp_path."""
    path = tmp_path / "turn_traces.jsonl"
    monkeypatch.setenv("HERMES_TURN_TRACE", "1")
    monkeypatch.setenv("HERMES_TURN_TRACE_FILE", str(path))
    return path


def _read_records(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Disabled by default
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_enabled_false_when_env_unset(self):
        assert turn_trace.enabled() is False

    def test_begin_returns_none(self):
        assert turn_trace.begin(key="k", platform="test") is None

    def test_span_without_trace_is_working_noop(self):
        handle = turn_trace.span("x")
        assert handle is _NULL_SPAN
        # Must behave as a full context manager with a chainable tag().
        with turn_trace.span("x", foo=1) as h:
            assert h.tag(bar=2) is h

    def test_mark_without_trace_is_noop(self):
        turn_trace.mark("x", foo=1)  # must not raise

    def test_null_span_swallows_nothing(self):
        # Exceptions still propagate through the null span.
        with pytest.raises(ValueError):
            with turn_trace.span("x"):
                raise ValueError("boom")


# ---------------------------------------------------------------------------
# Enabled: full begin -> span -> finish cycle
# ---------------------------------------------------------------------------

class TestEnabled:
    def test_full_cycle_emits_one_json_line(self, sink):
        trace = turn_trace.begin(key="sess:1", platform="test")
        assert isinstance(trace, TurnTrace)
        assert turn_trace.current() is trace

        trace.tag(model="fake-model")
        with turn_trace.span("iteration", trace=trace, i=1) as h:
            time.sleep(0.01)
            h.tag(late="yes")
        turn_trace.mark("event", trace=trace, kind="ping")

        record = trace.finish(exit_reason="done")
        assert record is not None

        records = _read_records(sink)
        assert len(records) == 1
        rec = records[0]
        assert rec == record
        assert rec["schema"] == 1
        assert rec["key"] == "sess:1"
        assert rec["tags"] == {
            "platform": "test",
            "model": "fake-model",
            "exit_reason": "done",
        }

        by_name = {s["n"]: s for s in rec["spans"]}
        assert set(by_name) == {"iteration", "event"}

        span = by_name["iteration"]
        assert span["tags"] == {"i": 1, "late": "yes"}
        # t0/d are relative milliseconds; the sleep(0.01) must be visible
        # but the whole test ran in well under a second.
        assert 0 <= span["t0"] < 1000
        assert 10 <= span["d"] < 1000

        mark = by_name["event"]
        assert mark["d"] == 0
        assert mark["tags"] == {"kind": "ping"}

        assert rec["duration_ms"] >= span["d"]

    def test_finish_is_idempotent(self, sink):
        trace = turn_trace.begin(key="k")
        assert trace.finish() is not None
        assert trace.finish() is None
        assert len(_read_records(sink)) == 1

    def test_span_after_finish_is_noop(self, sink):
        trace = turn_trace.begin(key="k")
        trace.finish()
        assert turn_trace.span("late", trace=trace) is _NULL_SPAN
        turn_trace.mark("late", trace=trace)
        assert len(_read_records(sink)) == 1
        assert _read_records(sink)[0]["spans"] == []

    def test_add_span_retrofit(self, sink):
        trace = turn_trace.begin(key="k")
        start = trace.started_at + 0.100
        end = start + 0.250
        trace.add_span("llm.call", start, end, model="fake")
        rec = trace.finish()

        spans = rec["spans"]
        assert len(spans) == 1
        assert spans[0]["n"] == "llm.call"
        assert spans[0]["t0"] == pytest.approx(100.0, abs=0.5)
        assert spans[0]["d"] == pytest.approx(250.0, abs=0.5)
        assert spans[0]["tags"] == {"model": "fake"}

    def test_spans_sorted_by_start(self, sink):
        trace = turn_trace.begin(key="k")
        t0 = trace.started_at
        trace.add_span("second", t0 + 0.2, t0 + 0.3)
        trace.add_span("first", t0 + 0.1, t0 + 0.4)
        rec = trace.finish()
        assert [s["n"] for s in rec["spans"]] == ["first", "second"]


# ---------------------------------------------------------------------------
# Carrying the trace: bind / get_bound / adopt
# ---------------------------------------------------------------------------

class TestCarrying:
    def test_bind_and_get_bound(self):
        obj = _Carrier()
        assert turn_trace.get_bound(obj) is None
        trace = TurnTrace(key="k")
        turn_trace.bind(obj, trace)
        assert turn_trace.get_bound(obj) is trace

    def test_span_resolves_bound_object(self, sink):
        obj = _Carrier()
        trace = TurnTrace(key="k")
        turn_trace.bind(obj, trace)
        # No thread-local current (begin() was not used).
        assert turn_trace.current() is None

        with turn_trace.span("gateway.ingest", obj=obj, platform="test"):
            pass
        rec = trace.finish()
        assert [s["n"] for s in rec["spans"]] == ["gateway.ingest"]

    def test_bind_none_clears(self):
        obj = _Carrier()
        trace = TurnTrace(key="k")
        turn_trace.bind(obj, trace)
        turn_trace.bind(obj, None)
        assert turn_trace.get_bound(obj) is None
        assert turn_trace.span("x", obj=obj) is _NULL_SPAN

    def test_bind_swallows_setattr_failure(self):
        # e.g. objects with __slots__ — must not raise.
        turn_trace.bind(object(), TurnTrace(key="k"))

    def test_explicit_trace_wins_over_bound_and_current(self, sink):
        obj = _Carrier()
        bound = TurnTrace(key="bound")
        explicit = turn_trace.begin(key="explicit")  # also becomes current
        turn_trace.bind(obj, bound)

        with turn_trace.span("x", trace=explicit, obj=obj):
            pass
        assert len(explicit.finish()["spans"]) == 1
        assert len(bound.finish()["spans"]) == 0

    def test_adopt_makes_trace_current_in_worker_thread(self, sink):
        trace = TurnTrace(key="k")
        results = {}

        def adopting_worker():
            turn_trace.adopt(trace)
            results["adopting"] = turn_trace.current()
            with turn_trace.span("tools.call", tool="x"):
                pass

        def non_adopting_worker():
            results["non_adopting_current"] = turn_trace.current()
            results["non_adopting_span"] = turn_trace.span("orphan")

        t1 = threading.Thread(target=adopting_worker)
        t2 = threading.Thread(target=non_adopting_worker)
        t1.start(); t1.join()
        t2.start(); t2.join()

        assert results["adopting"] is trace
        # Thread-local does not leak into a thread that never adopted.
        assert results["non_adopting_current"] is None
        assert results["non_adopting_span"] is _NULL_SPAN

        rec = trace.finish()
        assert [s["n"] for s in rec["spans"]] == ["tools.call"]


# ---------------------------------------------------------------------------
# Error tolerance
# ---------------------------------------------------------------------------

class TestErrorTolerance:
    def test_safe_entry_points_swallow_collector_failures(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("trace broke")

        monkeypatch.setattr(turn_trace, "begin", boom)
        monkeypatch.setattr(turn_trace, "span", boom)

        assert turn_trace.safe_begin(key="k") is None
        with turn_trace.safe_span("turn"):
            pass

    def test_safe_span_exit_swallows_add_failure(self, sink, monkeypatch):
        trace = turn_trace.begin(key="k")

        def boom(*args, **kwargs):
            raise RuntimeError("append broke")

        monkeypatch.setattr(trace, "add_span", boom)
        with turn_trace.safe_span("turn", trace=trace):
            pass

    def test_finish_survives_unwritable_sink(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_TURN_TRACE", "1")
        # A directory is not appendable — the emit must swallow the error.
        monkeypatch.setenv("HERMES_TURN_TRACE_FILE", str(tmp_path))
        trace = turn_trace.begin(key="k")
        record = trace.finish()  # must not raise
        assert record is not None

    def test_span_records_error_tag_and_reraises(self, sink):
        trace = turn_trace.begin(key="k")
        with pytest.raises(RuntimeError, match="boom"):
            with turn_trace.span("tools.call", trace=trace, tool="x"):
                raise RuntimeError("boom")

        rec = trace.finish()
        spans = rec["spans"]
        assert len(spans) == 1
        assert spans[0]["tags"]["error"] == "RuntimeError"
        assert spans[0]["tags"]["tool"] == "x"

    def test_span_error_tag_does_not_clobber_explicit(self, sink):
        trace = turn_trace.begin(key="k")
        with pytest.raises(RuntimeError):
            with turn_trace.span("x", trace=trace) as h:
                h.tag(error="Custom")
                raise RuntimeError("boom")
        assert trace.finish()["spans"][0]["tags"]["error"] == "Custom"

    def test_non_serializable_tag_falls_back_to_str(self, sink):
        trace = turn_trace.begin(key="k")
        trace.tag(weird=object())
        rec = trace.finish()
        assert rec is not None
        # default=str in the sink keeps the emitted line valid JSON.
        records = _read_records(sink)
        assert len(records) == 1
        assert isinstance(records[0]["tags"]["weird"], str)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_span_appends_all_land(self, sink):
        trace = turn_trace.begin(key="k")
        n = 16
        barrier = threading.Barrier(n)

        def worker(i: int):
            turn_trace.adopt(trace)
            barrier.wait()
            with turn_trace.span("tools.call", tool=f"t{i}"):
                time.sleep(0.001)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        trace.finish()
        records = _read_records(sink)
        assert len(records) == 1
        spans = records[0]["spans"]
        assert len(spans) == n
        assert {s["tags"]["tool"] for s in spans} == {f"t{i}" for i in range(n)}
        assert all(s["n"] == "tools.call" for s in spans)


# ---------------------------------------------------------------------------
# Renderer smoke test
# ---------------------------------------------------------------------------

class TestRenderer:
    def test_demo_mode_renders(self):
        repo_root = Path(__file__).resolve().parents[2]
        proc = subprocess.run(
            [sys.executable, "-m", "agent.turn_trace_render", "--demo", "--no-color"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        # The demo traces carry the contract's root span name.
        assert "turn" in proc.stdout


# ---------------------------------------------------------------------------
# Prefix fingerprint (HERMES_TURN_TRACE_PREFIX)
# ---------------------------------------------------------------------------

class TestPrefixFingerprint:
    def test_flag_gates(self, monkeypatch):
        monkeypatch.delenv("HERMES_TURN_TRACE_PREFIX", raising=False)
        assert not turn_trace.prefix_fingerprint_enabled()
        monkeypatch.setenv("HERMES_TURN_TRACE_PREFIX", "1")
        assert turn_trace.prefix_fingerprint_enabled()

    def test_fingerprint_shape_and_determinism(self):
        kwargs = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        }
        fp1 = turn_trace.prefix_fingerprint(kwargs)
        fp2 = turn_trace.prefix_fingerprint(json.loads(json.dumps(kwargs)))
        assert fp1 == fp2
        assert len(fp1["pfp"]) == 2 and all(len(h) == 8 for h in fp1["pfp"])
        assert len(fp1["pfp_lens"]) == 2 and all(n > 0 for n in fp1["pfp_lens"])
        assert fp1["pfp_tools"] and fp1["pfp_rest"]

    def test_key_order_changes_hash(self):
        # Wire-faithful: same content, different key order must NOT collide —
        # the provider prefix cache sees different bytes.
        a = turn_trace.prefix_fingerprint(
            {"messages": [{"role": "user", "content": "x"}]}
        )
        b = turn_trace.prefix_fingerprint(
            {"messages": [{"content": "x", "role": "user"}]}
        )
        assert a["pfp"] != b["pfp"]

    def test_rest_excludes_messages_and_tools(self):
        base = {"model": "m", "temperature": 0.2, "messages": [], "tools": []}
        changed_msgs = dict(base, messages=[{"role": "user", "content": "x"}])
        changed_rest = dict(base, temperature=0.9)
        assert (
            turn_trace.prefix_fingerprint(base)["pfp_rest"]
            == turn_trace.prefix_fingerprint(changed_msgs)["pfp_rest"]
        )
        assert (
            turn_trace.prefix_fingerprint(base)["pfp_rest"]
            != turn_trace.prefix_fingerprint(changed_rest)["pfp_rest"]
        )

    def test_unserializable_values_fall_back(self):
        fp = turn_trace.prefix_fingerprint(
            {"messages": [{"role": "user", "content": object()}]}
        )
        assert fp is not None and len(fp["pfp"]) == 1


class TestCacheDiff:
    def _fp(self, msgs):
        return turn_trace.prefix_fingerprint({"messages": msgs, "tools": []})

    def test_intact_prefix(self):
        from agent.turn_trace_render import diff_turn_pair

        old = [{"role": "system", "content": "s"}, {"role": "user", "content": "a"}]
        new = old + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "b"}]
        d = diff_turn_pair(self._fp(old), self._fp(new))
        assert d["prefix_intact"] and d["diverge_at"] is None
        assert d["common"] == 2 and d["system_same"] and d["tools_same"]

    def test_divergence_index_reported(self):
        from agent.turn_trace_render import diff_turn_pair

        old = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "r"},
        ]
        new = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "a-REWRITTEN"},
            {"role": "assistant", "content": "r"},
            {"role": "user", "content": "b"},
        ]
        d = diff_turn_pair(self._fp(old), self._fp(new))
        assert not d["prefix_intact"]
        assert d["diverge_at"] == 1 and d["common"] == 1 and d["system_same"]
        assert d["detail"] and d["detail"][0][0] == 1

    def test_system_prompt_change_flagged(self):
        from agent.turn_trace_render import diff_turn_pair

        old = [{"role": "system", "content": "s-v1"}, {"role": "user", "content": "a"}]
        new = [{"role": "system", "content": "s-v2"}, {"role": "user", "content": "a"}]
        d = diff_turn_pair(self._fp(old), self._fp(new))
        assert d["diverge_at"] == 0 and not d["system_same"]

    def test_responses_api_shape(self):
        # Responses payloads carry the system prompt in `instructions` and
        # history in `input`; instructions must land at pfp index 0.
        fp = turn_trace.prefix_fingerprint(
            {
                "model": "m",
                "instructions": "SYS",
                "input": [
                    {"type": "message", "role": "user", "content": "hi"},
                    {"type": "function_call", "name": "t", "arguments": "{}"},
                ],
                "store": False,
            }
        )
        assert len(fp["pfp"]) == 3
        sys_only = turn_trace.prefix_fingerprint({"instructions": "SYS", "input": []})
        assert fp["pfp"][0] == sys_only["pfp"][0]
        # instructions/input excluded from pfp_rest
        assert (
            turn_trace.prefix_fingerprint(
                {"model": "m", "instructions": "OTHER", "input": [], "store": False}
            )["pfp_rest"]
            == turn_trace.prefix_fingerprint(
                {"model": "m", "instructions": "SYS", "input": [], "store": False}
            )["pfp_rest"]
        )

    def test_chunk_hashes_locate_tail_edit(self):
        from agent.turn_trace_render import diff_turn_pair

        big = "A" * 20000
        old = [{"role": "system", "content": big + "TAIL-V1"},
               {"role": "user", "content": "u"}]
        new = [{"role": "system", "content": big + "TAIL-V2-DIFFERENT"},
               {"role": "user", "content": "u"}]
        fp_old = turn_trace.prefix_fingerprint({"messages": old, "tools": []})
        fp_new = turn_trace.prefix_fingerprint({"messages": new, "tools": []})
        assert "0" in fp_old["pfp_chunks"] and len(fp_old["pfp_chunks"]["0"]) >= 4
        d = diff_turn_pair(fp_old, fp_new)
        assert d["diverge_at"] == 0
        ci = d["chunk_info"]
        # 20k identical chars -> first differing 4KB chunk is the 5th (index 4)
        assert ci and ci["chunk"] == 4 and ci["char_offset"] == 16384
        # matched chars credit the intact head of the changed message
        assert d["matched_char_pct"] > 50.0
