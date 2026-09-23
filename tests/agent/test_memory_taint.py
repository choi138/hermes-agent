"""Tests for agent/memory_taint.py — origin-taint machinery (ADR-004 §①, Phase 2).

Covers: injected-span registry (prefetch + memory_search results, JSON
unescaping, per-session sha dedup), WAL/mirror span tagging (assistant
paraphrase incl. Korean; user spans never tainted; explicit clean tags;
literal fence echoes; proposal records), pipeline quote-admissibility
enforcement (check='taint', user-span quotes unaffected, kill switch, the
quote self-check that closes the dilution bypass, the as_of time bound that
prevents retroactive false taint), threshold boundary semantics, registry
TTL/GC and session-end eviction, and the fail-CLOSED posture on registry
corruption.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

import pytest

import agent.memory_taint as mt
from agent.memory_journal import L0Mirror, PendingTurnWAL
from agent.memory_pipeline import MemoryWritePipeline
from agent.memory_taint import TaintRegistry

SESSION = "sess-taint-1"

# Korean memory content as prefetch would inject it, and an assistant reply
# that paraphrases it: the frame is rewritten (인사말, 어미, 연결어 모두 변경)
# but the borrowed noun phrases — entities, numbers, the actual FACT — stay
# verbatim, which is exactly what paraphrase looks like in practice.
INJECTED_KO = (
    "codex-lb는 10.0.0.113에서 구성되어 있고, postgres memcg OOM은 "
    "버스트 인덱스 최적화로 조치 완료했다. retention 정책은 아직 미해결."
)
PARAPHRASE_KO = (
    "응, 기억나 — codex-lb는 10.0.0.113에서 구성되어 있고 postgres memcg "
    "OOM은 버스트 인덱스 최적화로 조치했었지. retention 쪽만 남았어."
)
# Independent same-topic prose: shares vocabulary (postgres, OOM) but no
# borrowed 8-char phrase runs.
INDEPENDENT_KO = (
    "새 서버의 postgres 설정을 처음부터 다시 검토하자. 메모리 파라미터를 "
    "보수적으로 잡아야 OOM 걱정 없이 안정적으로 돌아갈 거야."
)


@pytest.fixture()
def registry(tmp_path):
    reg = TaintRegistry(base_dir=tmp_path / "state" / "memory-pending" / "taint")
    mt.set_registry(reg)
    yield reg
    mt.set_registry(None)


def _wal(tmp_path) -> PendingTurnWAL:
    return PendingTurnWAL(base_dir=tmp_path / "state" / "memory-pending")


def _records(path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Registry recording + WAL span tagging
# ---------------------------------------------------------------------------

class TestRegistryAndTagging:
    def test_assistant_paraphrase_of_injected_korean_is_tainted(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        wal = _wal(tmp_path)
        wal.append_turn(SESSION, "어제 codex-lb 어떻게 됐지?", PARAPHRASE_KO)

        recs = _records(
            tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        assert len(recs) == 1
        user_rec, assistant_rec = recs[0]["records"]
        assert user_rec["role"] == "user"
        assert "taint" not in user_rec  # user spans are never tainted
        assert assistant_rec["role"] == "assistant"
        taint = assistant_rec["taint"]
        assert taint["tainted"] is True
        assert taint["registry"] == "ok"
        assert taint["spans"], "tainted segments must carry offsets"
        assert taint["score"] >= mt.containment_threshold()

    def test_independent_same_topic_text_gets_explicit_clean_tag(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        wal = _wal(tmp_path)
        wal.append_turn(SESSION, "postgres 새로 세팅하자", INDEPENDENT_KO)

        recs = _records(
            tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        assistant_rec = recs[0]["records"][1]
        # Always-stamped: the write-time CLEAN verdict is recorded explicitly
        # so enforcement never recomputes this span against a registry that
        # has since grown (retroactive-taint guard).
        taint = assistant_rec["taint"]
        assert taint["tainted"] is False
        assert taint["spans"] == []
        assert taint["registry"] == "ok"

    def test_user_text_identical_to_injection_is_never_tainted(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        wal = _wal(tmp_path)
        # The user literally repeats the injected fact — still user-origin.
        wal.append_turn(SESSION, INJECTED_KO, "네, 맞아요.")

        recs = _records(
            tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        assert "taint" not in recs[0]["records"][0]

    def test_fence_echo_is_tainted_even_with_empty_registry(self, registry):
        taint = registry.assistant_taint(
            SESSION, "sure!\n<memory-context>\nleaked stuff\n</memory-context>"
        )
        assert taint["tainted"] is True
        assert taint["reason"] == "memory-context-fence"
        assert taint["spans"] == [[0, len(
            "sure!\n<memory-context>\nleaked stuff\n</memory-context>"
        )]]

    def test_memory_search_result_json_is_unescaped_before_registration(
        self, registry, tmp_path
    ):
        # ensure_ascii=True (the provider default) \u-escapes Korean — the
        # registry must register the decoded text the model actually read.
        result = json.dumps(
            {"status": "success", "results": [{"fact": INJECTED_KO}]}
        )
        assert "\\u" in result  # escaped on the wire
        mt.record_injected_tool_result(SESSION, result, source="memory_search")

        taint = registry.assistant_taint(SESSION, PARAPHRASE_KO)
        assert taint["tainted"] is True

    def test_proposal_record_is_tagged_like_assistant(self, registry, tmp_path):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        wal = _wal(tmp_path)
        wal.append_proposal(SESSION, PARAPHRASE_KO, kind_hint="fact")

        recs = _records(
            tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        assert recs[0]["type"] == "proposal"
        assert recs[0]["taint"]["tainted"] is True

    def test_l0_mirror_assistant_body_is_tagged(self, registry, tmp_path):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        mirror = L0Mirror(base_dir=tmp_path / "memory" / "l0-mirror")
        mirror.append_turn(SESSION, "질문", PARAPHRASE_KO, wal_entry_id="e1")

        month = time.strftime("%Y-%m")
        recs = _records(tmp_path / "memory" / "l0-mirror" / f"{month}.jsonl")
        assert recs[0]["taint"]["assistant"]["tainted"] is True

    def test_recording_survives_to_sidecar_and_reloads(self, registry, tmp_path):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        registry.drain_io()
        # Fresh registry over the same dir (simulates process restart).
        fresh = TaintRegistry(
            base_dir=tmp_path / "state" / "memory-pending" / "taint"
        )
        taint = fresh.assistant_taint(SESSION, PARAPHRASE_KO)
        assert taint["tainted"] is True
        assert taint["registry"] == "ok"

    def test_identical_injection_registers_once_per_session(
        self, registry, tmp_path
    ):
        """Per-session sha dedup: the prefetch block re-injected every turn
        must not grow the entry list or the sidecar unboundedly."""
        for _ in range(5):
            mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        registry.drain_io()

        digest = registry.session_injected_digest(SESSION)
        assert digest["count"] == 1
        sidecar = (
            tmp_path / "state" / "memory-pending" / "taint" / f"{SESSION}.jsonl"
        )
        lines = [ln for ln in sidecar.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        assert len(lines) == 1
        # Taint coverage is unaffected by dedup.
        assert registry.assistant_taint(SESSION, PARAPHRASE_KO)["tainted"] is True

    def test_pre_dedup_sidecar_with_duplicates_loads_deduped(
        self, registry, tmp_path
    ):
        """Sidecars written before the dedup fix may hold repeats — reload
        collapses them (earliest ts wins) instead of double-counting."""
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        registry.drain_io()
        sidecar = (
            tmp_path / "state" / "memory-pending" / "taint" / f"{SESSION}.jsonl"
        )
        line = sidecar.read_text(encoding="utf-8").strip()
        sidecar.write_text(line + "\n" + line + "\n" + line + "\n",
                           encoding="utf-8")
        fresh = TaintRegistry(
            base_dir=tmp_path / "state" / "memory-pending" / "taint"
        )
        assert fresh.session_injected_digest(SESSION)["count"] == 1
        assert fresh.assistant_taint(SESSION, PARAPHRASE_KO)["tainted"] is True


# ---------------------------------------------------------------------------
# Threshold boundary semantics
# ---------------------------------------------------------------------------

class TestThresholdBoundary:
    def test_taint_fires_at_and_above_threshold_not_below(
        self, registry, monkeypatch
    ):
        injected = "the codex load balancer runs at ten dot zero dot zero"
        mt.record_injected_text(SESSION, injected, source="prefetch")

        full_copy = injected  # containment exactly 1.0
        near_copy = injected[: len(injected) - 6] + " nope!!"  # < 1.0

        # Compute the actual scores through the same primitives the module
        # uses, so the assertions test the >= boundary, not string luck.
        corpus = mt._shingles(injected)
        full_score = mt._containment(mt._shingles(full_copy), corpus)
        near_score = mt._containment(mt._shingles(near_copy), corpus)
        assert full_score == 1.0
        assert near_score < 1.0

        monkeypatch.setattr(mt, "containment_threshold", lambda: 1.0)
        assert registry.assistant_taint(SESSION, full_copy)["tainted"] is True
        assert registry.assistant_taint(SESSION, near_copy)["tainted"] is False

        # And just under the near_copy score, it flips back to tainted.
        monkeypatch.setattr(
            mt, "containment_threshold", lambda: near_score - 0.001
        )
        assert registry.assistant_taint(SESSION, near_copy)["tainted"] is True

    def test_tiny_segments_do_not_taint(self, registry):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        # Below one shingle worth of content: no verdict possible.
        assert registry.assistant_taint(SESSION, "응.")["tainted"] is False

    def test_tiny_segment_fully_contained_in_corpus_is_tainted(self, registry):
        mt.record_injected_text(
            SESSION, "graphiti 데몬 포트는 9876이다. neo4j는 7687.",
            source="prefetch",
        )
        # A short verbatim echo: few shingles, but every one is borrowed —
        # and at ~8 Hangul chars it would pass quote admissibility, so it
        # must not be taint-blind.
        taint = registry.assistant_taint(SESSION, "포트는 9876이다.")
        assert taint["tainted"] is True

    def test_env_threshold_override_is_used(self, registry, monkeypatch):
        monkeypatch.setenv("HERMES_MEMORY_TAINT_THRESHOLD", "0.9")
        assert mt.containment_threshold() == 0.9
        monkeypatch.setenv("HERMES_MEMORY_TAINT_THRESHOLD", "not-a-float")
        assert mt.containment_threshold() == mt.DEFAULT_CONTAINMENT_THRESHOLD
        monkeypatch.delenv("HERMES_MEMORY_TAINT_THRESHOLD")
        assert mt.containment_threshold() == mt.DEFAULT_CONTAINMENT_THRESHOLD


# ---------------------------------------------------------------------------
# Pipeline enforcement (check='taint')
# ---------------------------------------------------------------------------

class TestPipelineEnforcement:
    def _grounded_confirm(self, tmp_path, quote: str) -> Dict[str, Any]:
        """propose→confirm an ADD citing a WAL quote; return confirm result."""
        pipeline = MemoryWritePipeline(hermes_home=tmp_path)
        wal = _wal(tmp_path)
        entry_id = wal.append_turn(
            SESSION,
            "빌드 서버 접속 계정은 builder02로 바꿨어. 기억해 줘.",
            PARAPHRASE_KO,
        )
        ref = {
            "type": "wal",
            "session_id": SESSION,
            "entry_id": entry_id,
            "quote": quote,
        }
        proposed = pipeline.propose(
            "codex-lb 인프라 사실",
            kind_hint="fact",
            evidence_refs=[ref],
            session_id=SESSION,
        )
        assert proposed["success"], proposed
        return pipeline.confirm(
            proposed["token"], "ADD",
            topic_key="infra.codexlb.fact",
            session_id=SESSION,
        )

    def test_quote_from_tainted_assistant_span_is_rejected(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        result = self._grounded_confirm(
            tmp_path, "codex-lb는 10.0.0.113에서 구성되어 있고"
        )
        assert result["success"] is False
        checks = result["grounding"]
        assert checks[0]["ok"] is False
        assert checks[0]["checked"] == "taint"
        assert "origin-taint" in checks[0]["detail"]

    def test_quote_from_user_span_is_unaffected(self, registry, tmp_path):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        result = self._grounded_confirm(
            tmp_path, "빌드 서버 접속 계정은 builder02로 바꿨어"
        )
        assert result["success"] is True, result

    def test_untainted_assistant_quote_still_grounds(self, registry, tmp_path):
        # No injections this session: assistant text is clean evidence.
        pipeline = MemoryWritePipeline(hermes_home=tmp_path)
        wal = _wal(tmp_path)
        entry_id = wal.append_turn(SESSION, "서버 상태 어때?", INDEPENDENT_KO)
        ref = {
            "type": "wal",
            "session_id": SESSION,
            "entry_id": entry_id,
            "quote": "postgres 설정을 처음부터 다시 검토하자",
        }
        proposed = pipeline.propose(
            "postgres 재검토 결정", kind_hint="fact",
            evidence_refs=[ref], session_id=SESSION,
        )
        result = pipeline.confirm(
            proposed["token"], "ADD",
            topic_key="infra.postgres.review", session_id=SESSION,
        )
        assert result["success"] is True, result

    def test_kill_switch_disables_enforcement(
        self, registry, tmp_path, monkeypatch
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        monkeypatch.setattr(
            mt, "_config_memory", lambda: {"taint_enforce": False}
        )
        result = self._grounded_confirm(
            tmp_path, "codex-lb는 10.0.0.113에서 구성되어 있고"
        )
        assert result["success"] is True, result

    def test_l0_quote_from_tainted_assistant_body_is_rejected(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        pipeline = MemoryWritePipeline(hermes_home=tmp_path)
        mirror = L0Mirror(base_dir=tmp_path / "memory" / "l0-mirror")
        mirror.append_turn(SESSION, "질문했다", PARAPHRASE_KO, wal_entry_id="e9")
        month = time.strftime("%Y-%m")
        ref = {
            "type": "l0",
            "month": month,
            "wal_entry_id": "e9",
            "quote": "codex-lb는 10.0.0.113에서 구성되어 있고",
        }
        check = pipeline._ground_ref(ref)
        assert check["ok"] is False
        assert check["checked"] == "taint"


# ---------------------------------------------------------------------------
# Dilution bypass (quote self-check) — must-fix #1
# ---------------------------------------------------------------------------

# A verbatim injected fact embedded inside a longer ORIGINAL assistant
# sentence: the segment's containment stays below threshold (diluted), but a
# quote of just the fact is 100% injected text. Single segment on purpose —
# no sentence-final punctuation + whitespace inside.
INJECTED_FACT_KO = "codex-lb 프록시 포트는 10.0.0.113:8443이다"
DILUTED_KO = (
    "오늘 오후에 확인해 본 내용을 정리하면, 게이트웨이 라우팅 경로와 백엔드 "
    "헬스체크 설정은 전부 문제없이 잘 동작하고 있었고, "
    + INJECTED_FACT_KO
    + "라는 부분만 새로 기록해 두면 될 것 같아"
)


def _confirm_wal_quote(tmp_path, quote: str, *, entry_id: str,
                       title: str = "타당성 검증", topic_key: str = "t.k"):
    """propose→confirm an ADD citing an existing WAL entry; return result."""
    pipeline = MemoryWritePipeline(hermes_home=tmp_path)
    ref = {
        "type": "wal",
        "session_id": SESSION,
        "entry_id": entry_id,
        "quote": quote,
    }
    proposed = pipeline.propose(
        title, kind_hint="fact", evidence_refs=[ref], session_id=SESSION
    )
    assert proposed["success"], proposed
    return pipeline.confirm(
        proposed["token"], "ADD", topic_key=topic_key, session_id=SESSION
    )


class TestDilutionBypass:
    def _precondition_scores(self):
        """The scenario is only a bypass while the segment dilutes below
        threshold AND the quote itself is fully contained — assert both
        through the module's own primitives, not string luck."""
        corpus = mt._shingles(INJECTED_FACT_KO)
        seg_score = mt._containment(mt._shingles(DILUTED_KO), corpus)
        quote_score = mt._containment(mt._shingles(INJECTED_FACT_KO), corpus)
        assert seg_score < mt.containment_threshold(), seg_score
        assert quote_score == 1.0
        return seg_score

    def test_diluted_segment_gets_clean_write_time_tag(
        self, registry, tmp_path
    ):
        self._precondition_scores()
        mt.record_injected_text(SESSION, INJECTED_FACT_KO, source="prefetch")
        wal = _wal(tmp_path)
        wal.append_turn(SESSION, "오늘 점검 결과 알려줘", DILUTED_KO)
        recs = _records(
            tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        taint = recs[0]["records"][1]["taint"]
        assert taint["tainted"] is False  # segment-level verdict IS diluted

    def test_verbatim_injected_fact_quote_is_rejected_despite_dilution(
        self, registry, tmp_path
    ):
        """must-fix #1 repro: the quote — 100% injected text — must be
        rejected even though every segment of the span computed clean."""
        self._precondition_scores()
        mt.record_injected_text(SESSION, INJECTED_FACT_KO, source="prefetch")
        wal = _wal(tmp_path)
        entry_id = wal.append_turn(SESSION, "오늘 점검 결과 알려줘", DILUTED_KO)
        result = _confirm_wal_quote(
            tmp_path, INJECTED_FACT_KO, entry_id=entry_id
        )
        assert result["success"] is False
        assert result["grounding"][0]["checked"] == "taint"
        assert "origin-taint" in result["grounding"][0]["detail"]

    def test_original_prose_quote_from_same_span_still_grounds(
        self, registry, tmp_path
    ):
        """The fix is quote-granular: original prose from the SAME diluted
        span remains admissible evidence."""
        self._precondition_scores()
        mt.record_injected_text(SESSION, INJECTED_FACT_KO, source="prefetch")
        wal = _wal(tmp_path)
        entry_id = wal.append_turn(SESSION, "오늘 점검 결과 알려줘", DILUTED_KO)
        result = _confirm_wal_quote(
            tmp_path,
            "게이트웨이 라우팅 경로와 백엔드 헬스체크 설정은 전부 문제없이 잘 동작하고",
            entry_id=entry_id,
        )
        assert result["success"] is True, result


# ---------------------------------------------------------------------------
# Retroactive false taint (as_of time bound + explicit clean tags) — must-fix #2
# ---------------------------------------------------------------------------

def _write_raw_wal_turn(tmp_path, assistant: str, ts: float,
                        *, entry_id: str, user: str = "질문") -> None:
    """Simulate a PRE-taint-patch WAL record: no ``taint`` key at all."""
    path = tmp_path / "state" / "memory-pending" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "type": "turn", "id": entry_id, "ts": round(ts, 3),
        "session_id": SESSION, "seq": 1,
        "records": [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class TestRetroactiveTaint:
    def test_later_injection_does_not_taint_stored_clean_span(
        self, registry, tmp_path
    ):
        """must-fix #2 repro: an assistant conclusion authored BEFORE any
        injection existed (clean write-time tag) stays quotable after the
        agent's own memory_search later surfaces the same content — the
        session registry growing must not mutate past admission verdicts."""
        wal = _wal(tmp_path)
        entry_id = wal.append_turn(SESSION, "서버 상태 어때?", INDEPENDENT_KO)
        time.sleep(0.005)
        # Same-session re-surfacing of the just-ingested content.
        mt.record_injected_tool_result(
            SESSION,
            json.dumps({"results": [{"fact": INDEPENDENT_KO}]}),
            source="memory_search",
        )
        result = _confirm_wal_quote(
            tmp_path, "postgres 설정을 처음부터 다시 검토하자",
            entry_id=entry_id,
        )
        assert result["success"] is True, result

    def test_untagged_pre_patch_record_recompute_is_bounded_by_record_ts(
        self, registry, tmp_path
    ):
        """Pre-patch records carry no tag — enforcement recomputes, but the
        corpus is bounded at the record's own ts, so a later injection
        cannot reach back."""
        _write_raw_wal_turn(
            tmp_path, PARAPHRASE_KO, time.time(), entry_id="preinj001"
        )
        time.sleep(0.005)
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        result = _confirm_wal_quote(
            tmp_path, "codex-lb는 10.0.0.113에서 구성되어 있고",
            entry_id="preinj001",
        )
        assert result["success"] is True, result

    def test_untagged_record_written_after_injection_is_still_rejected(
        self, registry, tmp_path
    ):
        """Control for the bound: injection BEFORE the record ts must still
        taint an untagged record — the as_of bound is not a blanket admit."""
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        time.sleep(0.005)
        _write_raw_wal_turn(
            tmp_path, PARAPHRASE_KO, time.time(), entry_id="postinj01"
        )
        result = _confirm_wal_quote(
            tmp_path, "codex-lb는 10.0.0.113에서 구성되어 있고",
            entry_id="postinj01",
        )
        assert result["success"] is False
        assert result["grounding"][0]["checked"] == "taint"


# ---------------------------------------------------------------------------
# Fail-closed on registry corruption
# ---------------------------------------------------------------------------

class TestCorruptionFailClosed:
    def _corrupt_sidecar(self, tmp_path) -> None:
        taint_dir = tmp_path / "state" / "memory-pending" / "taint"
        taint_dir.mkdir(parents=True, exist_ok=True)
        (taint_dir / f"{SESSION}.jsonl").write_text(
            '{"broken json…\n', encoding="utf-8"
        )

    def test_corrupt_registry_taints_assistant_spans(self, tmp_path):
        self._corrupt_sidecar(tmp_path)
        reg = TaintRegistry(
            base_dir=tmp_path / "state" / "memory-pending" / "taint"
        )
        mt.set_registry(reg)
        try:
            taint = reg.assistant_taint(SESSION, INDEPENDENT_KO)
            assert taint["tainted"] is True
            assert taint["registry"] == "corrupt"
        finally:
            mt.set_registry(None)

    def test_corrupt_registry_rejects_assistant_quote_but_not_user_quote(
        self, tmp_path
    ):
        self._corrupt_sidecar(tmp_path)
        reg = TaintRegistry(
            base_dir=tmp_path / "state" / "memory-pending" / "taint"
        )
        mt.set_registry(reg)
        try:
            pipeline = MemoryWritePipeline(hermes_home=tmp_path)
            wal = _wal(tmp_path)
            entry_id = wal.append_turn(
                SESSION,
                "그 인시던트 원인은 SATA 커넥터 헐거움이었어.",
                INDEPENDENT_KO,
            )
            base = {"type": "wal", "session_id": SESSION, "entry_id": entry_id}
            # Assistant quote: fail CLOSED (cannot prove clean).
            assistant_check = pipeline._ground_ref(
                {**base, "quote": "postgres 설정을 처음부터 다시 검토하자"}
            )
            assert assistant_check["ok"] is False
            assert assistant_check["checked"] == "taint"
            # User quote: user spans are never tainted, corruption or not.
            user_check = pipeline._ground_ref(
                {**base, "quote": "원인은 SATA 커넥터 헐거움이었어"}
            )
            assert user_check["ok"] is True
        finally:
            mt.set_registry(None)

    def test_missing_sidecar_is_empty_not_corrupt(self, registry):
        taint = registry.assistant_taint("never-seen-session", INDEPENDENT_KO)
        assert taint["tainted"] is False
        assert taint["registry"] == "empty"


# ---------------------------------------------------------------------------
# Lifecycle: session-end eviction, sidecar TTL GC
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_end_session_evicts_memory_but_keeps_sidecar(
        self, registry, tmp_path
    ):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        registry.drain_io()
        mt.end_session(SESSION)
        assert SESSION not in registry._sessions
        # Post-session readers (curator/dream) reload from the sidecar.
        sidecar = (
            tmp_path / "state" / "memory-pending" / "taint" / f"{SESSION}.jsonl"
        )
        assert sidecar.exists()
        assert registry.assistant_taint(SESSION, PARAPHRASE_KO)["tainted"] is True

    def test_gc_deletes_only_stale_sidecars(self, registry, tmp_path):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        mt.record_injected_text("sess-old", INJECTED_KO, source="prefetch")
        registry.drain_io()
        taint_dir = tmp_path / "state" / "memory-pending" / "taint"
        old = taint_dir / "sess-old.jsonl"
        stale_ts = time.time() - 8 * 24 * 3600
        os.utime(old, (stale_ts, stale_ts))

        assert registry.gc_stale_files() == 1
        assert not old.exists()
        assert (taint_dir / f"{SESSION}.jsonl").exists()

    def test_in_memory_entry_cap_sets_truncated_flag(self, registry):
        for i in range(mt._MAX_ENTRIES_PER_SESSION + 3):
            registry.record_injected_text(
                SESSION, f"unique injected content number {i} " + "x" * 20,
                source="prefetch",
            )
        digest = registry.session_injected_digest(SESSION)
        assert digest["truncated"] is True
        assert digest["count"] == mt._MAX_ENTRIES_PER_SESSION


# ---------------------------------------------------------------------------
# Phase-3 corroboration API + curator rendering hook
# ---------------------------------------------------------------------------

class TestPhase3AndCuratorAPI:
    def test_span_taint_semantics(self, registry):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        assert mt.span_taint(
            {"role": "user", "content": INJECTED_KO, "session_id": SESSION}
        ) is False
        assert mt.span_taint(
            {"role": "assistant", "content": PARAPHRASE_KO,
             "session_id": SESSION}
        ) is True
        # Stored tag wins over recompute.
        assert mt.span_taint(
            {"role": "assistant", "content": "whatever",
             "taint": {"tainted": True}}
        ) is True
        # Malformed span: indeterminate → tainted (must not corroborate).
        assert mt.span_taint("not-a-dict") is True

    def test_session_injected_digest_shape(self, registry):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        mt.record_injected_tool_result(
            SESSION, json.dumps({"results": [{"fact": "다른 사실 하나"}]}),
            source="memory_search",
        )
        digest = mt.session_injected_digest(SESSION)
        assert digest["count"] == 2
        assert digest["sources"] == {"prefetch": 1, "memory_search": 1}
        assert len(digest["shas"]) == 2
        assert digest["registry"] == "ok"
        assert digest["shingle_count"] > 0

    def test_curator_labels_mark_tainted_assistant_spans(self, registry):
        mt.record_injected_text(SESSION, INJECTED_KO, source="prefetch")
        records = [
            {"role": "user", "content": "질문이야"},
            {"role": "assistant", "content": PARAPHRASE_KO},
            {"role": "assistant", "content": INDEPENDENT_KO},
        ]
        annotated = mt.annotate_wal_records_for_curator(SESSION, records)
        assert [r["curator_label"] for r in annotated] == ["", "[tainted]", ""]
        # Originals are not mutated.
        assert "curator_label" not in records[0]
