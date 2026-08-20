"""Origin-taint machinery for the memory pipeline (ADR-004 §①, Phase 2).

Echo-chamber neutralization: quote-grounding only proves "this text exists in
the transcript", not "this text originated outside the memory system". Text
the system itself injected — the ``<memory-context>`` prefetch fence and
``memory_search`` tool results — re-enters the transcript every session, so
an assistant paraphrase of it would otherwise count as fresh evidence and
corroborate facts for free, forever. This module closes that loop:

* **Injected-span registry** — every piece of memory-derived text shown to
  the agent in a session is registered here (scrubbed text + char-shingle
  set), in memory for same-process readers and in a durable sidecar JSONL
  under the WAL directory (``state/memory-pending/taint/{session}.jsonl``)
  for post-restart readers (curator, dream).

* **WAL span tainting** — when the mem-sync worker journals a turn, the
  journal calls :func:`tag_wal_turn_records` /:func:`tag_wal_proposal_record`
  to stamp taint metadata onto the record. User spans are NEVER tainted
  (user words are user-origin by definition — the one origin corroboration
  is allowed to trust). Assistant spans are tainted per-segment by char
  n-gram containment against the session's injected registry; literal
  ``<memory-context>`` fence material is tainted unconditionally. Assistant
  spans are ALWAYS stamped — an explicit clean verdict is stamped too — so
  the write-time verdict (computed against the injections that existed when
  the span was authored) is authoritative and enforcement never needs to
  recompute a post-patch span against a registry that has since grown.
  Registrations carry timestamps; every recompute path is bounded by the
  record's own ``ts`` (``as_of``), so an injection registered AFTER a span
  was authored can never retroactively taint it.

* **Enforcement** — ``memory_pipeline`` grounding consults
  :func:`matched_quote_taint`: a quote is rejected (check='taint') when
  every span it grounds in fails EITHER the span check (all occurrences lie
  inside tainted segments) OR the quote self-check (the quote's OWN shingle
  containment against injections registered before that span's ``ts`` is at
  or above threshold — this closes the dilution bypass where a verbatim
  injected fact hides inside a longer original sentence whose segment score
  stays below threshold). The curator (lane A) renders ``[tainted]`` span
  markers via :func:`curator_taint_label` /
  :func:`annotate_wal_records_for_curator` (§4.6 prompt contract).

* **Phase-3 corroboration API** — :func:`span_taint` and
  :func:`session_injected_digest` are the stable interface dream promotion
  calls to exclude tainted re-occurrences from spacing-gate counts (§⑤).

Failure posture (deliberately asymmetric, ADR-004 §①):

* Registry **writes** are fail-open — a broken disk must never block a turn
  (recording degrades; in-memory registry still covers the live process).
* Registry **reads for admission** are fail-CLOSED for assistant spans — a
  corrupt registry means we can no longer prove an assistant span is clean,
  and taint protects corroboration integrity: admitting an unverifiable
  assistant quote re-opens the echo-chamber, while rejecting it merely
  forces the writer to cite the user/tool span the fact actually came from.
  Quotes grounded in user spans are unaffected by registry corruption.

Kill switches: recording honors ``HERMES_MEMORY_JOURNAL_DISABLED`` for its
disk sidecar (in-memory registration continues); enforcement honors config
``memory.taint_enforce`` (default ON — flip to false to fall back to the
Phase-1 caller-marked-only behavior).

Known seams (documented, deliberately out of this lane's scope):

* **L1/MEMORY.md system-prompt injection is NOT registered.** ADR-004 §①
  covers the ``<memory-context>`` fence and memory-provider tool results;
  the built-in MEMORY.md block compiled into the system prompt (and the
  Phase-3 L1 compilation that will feed it) is a memory-derived injection
  too — an assistant restatement of a MEMORY.md line currently yields a
  clean quotable span. The Phase-3 dream/promotion lane must register the
  compiled L1 text at injection time (``record_injected_text(...,
  source="l1")``) before promotion starts counting corroborations.
* **Cross-process registration visibility.** A session's sidecar is loaded
  once per process (on first touch) and only in-process registrations are
  applied after that; injections appended by ANOTHER process later in the
  same session become visible only after LRU eviction + reload. Sessions
  are single-process in this architecture, so this only under-taints in
  exotic multi-process replays — and under-taint at recompute time is
  bounded anyway by the always-stamped write-time verdicts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_journal import (
    _append_jsonl,
    _safe_session_filename,
    _scrub,
    journals_disabled,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Char-level shingle size. Char-level (not word-level) because the primary
# corpus is Korean: agglutinative morphology mutates word surface forms
# (조사/어미) so word-token shingles miss paraphrases, while Hangul syllable
# blocks pack ~2x the information per char — 8 chars is roughly two Korean
# content words or one distinctive token fragment (UUID, IP, path). Shorter
# shingles (4–5) start matching on particle/spacing patterns shared by ALL
# Korean prose (false positives); much longer (12+) only catch near-verbatim
# reuse and miss entity-preserving paraphrases.
SHINGLE_SIZE = 8

# Containment threshold: a segment is tainted when
#   |shingles(segment) ∩ shingles(session injections)| / |shingles(segment)|
# >= this value. Containment, not Jaccard: the injected registry is orders
# of magnitude larger than one assistant sentence, so the Jaccard union
# denominator would push every short paraphrase toward 0 regardless of how
# much of it was borrowed. 0.35 rather than 0.5: a genuine paraphrase keeps
# the borrowed noun phrases / numbers / identifiers verbatim while rewriting
# the frame around them — in Korean that reliably covers ~40–70% of the
# segment's shingles, but a restructured sentence with new endings and
# connectives can dip below 0.5 while still being memory-derived.
# Independent same-topic prose shares only isolated entity names, which at
# 8-char shingle granularity stays well under ~0.2 (contiguous 8-char matches
# require borrowed *phrases*, not shared vocabulary). Erring toward taint is
# the cheap direction: a false positive only forces citing the user's own
# span instead; a false negative re-opens the echo-chamber loop.
DEFAULT_CONTAINMENT_THRESHOLD = 0.35

# Segments with fewer shingles than this can't produce a meaningful
# containment ratio (and are far below quote admissibility length anyway).
_MIN_SEGMENT_SHINGLES = 4

# Sidecar TTL matches the WAL's fully-acked GC window: after 7 idle days the
# session's WAL is (if consumed) deleted and the taint spans stored ON the
# WAL/mirror records themselves are the durable signal.
_SIDECAR_GC_MAX_AGE_S = 7 * 24 * 3600

# In-memory bounds. Sessions are LRU-evicted (re-loadable from the sidecar);
# the per-session entry LIST (digest telemetry) is FIFO-capped with a
# `truncated` flag, while the shingle UNION — what taint checks run against —
# is never trimmed, so the cap can only lose digest detail, never taint
# coverage. A 512-injection session (~250+ turns) is far past every boundary
# trigger anyway.
_MAX_SESSIONS = 64
_MAX_ENTRIES_PER_SESSION = 512

_TAINT_METHOD = f"char{SHINGLE_SIZE}-shingle-containment"

# Fence tags are machine vocabulary: assistant text carrying them IS leaked
# injected context (the streaming scrubber strips them from display, but the
# stored final_response can still carry an echo). Same pattern as
# memory_manager._FENCE_TAG_RE, duplicated to keep this module import-light.
_FENCE_TAG_RE = re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE)

# Segment breaks: newlines always; sentence-final punctuation followed by
# whitespace. Korean sentences end in '다.'/'요.'/'까?' etc., so the Latin
# enders plus ellipsis/ideographic stop cover the practical cases; a missed
# break only makes segments coarser (more conservative tainting of mixed
# segments, never less).
_SEGMENT_BREAK_RE = re.compile(r"\n+|(?<=[.!?…。])\s+")


def _config_memory() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return (load_config() or {}).get("memory", {}) or {}
    except Exception:
        return {}


def taint_enforce_enabled() -> bool:
    """Config gate for quote-admissibility enforcement (default ON).

    ``memory.taint_enforce: false`` is the kill switch — it reverts quote
    grounding to the Phase-1 behavior (caller-marked ``tainted: true`` refs
    are still refused at propose; mechanical span checks are skipped).
    Registry recording and WAL tagging are NOT gated: they are inert
    metadata whose absence would leave a permanent hole in the record.
    """
    try:
        return bool(_config_memory().get("taint_enforce", True))
    except Exception:
        return True


def containment_threshold() -> float:
    """Effective containment threshold.

    Env override ``HERMES_MEMORY_TAINT_THRESHOLD`` (ops recalibration knob),
    NOT a config key: this runs on the WAL tagging path for every journaled
    turn, and ``load_config()`` both scaffolds a HERMES_HOME as a side
    effect and re-reads config.yaml — neither belongs on a journal append.
    Env reads are side-effect-free and per-call (flippable without restart).
    """
    raw = os.environ.get("HERMES_MEMORY_TAINT_THRESHOLD", "").strip()
    if raw:
        try:
            value = float(raw)
            if 0.0 < value <= 1.0:
                return value
        except ValueError:
            pass
    return DEFAULT_CONTAINMENT_THRESHOLD


# ---------------------------------------------------------------------------
# Shingling / segmentation primitives
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def _shingles(text: str) -> set:
    norm = _normalize(text)
    if len(norm) < SHINGLE_SIZE:
        return set()
    return {norm[i : i + SHINGLE_SIZE] for i in range(len(norm) - SHINGLE_SIZE + 1)}


def _segments(content: str) -> List[Tuple[int, int]]:
    """Split content into (start, end) segment offsets in ORIGINAL coords."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    for m in _SEGMENT_BREAK_RE.finditer(content):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(content):
        spans.append((pos, len(content)))
    return spans


def _containment(
    candidate: set, corpus, as_of: Optional[float] = None
) -> float:
    """Containment of ``candidate`` shingles in ``corpus``.

    ``corpus`` is either a plain set (legacy/tests) or the registry's
    ``{shingle: earliest_registration_ts}`` map. With ``as_of`` set, only
    shingles first registered at or before that timestamp count — the
    origin-taint time bound: an injection registered AFTER a span was
    authored cannot have caused the span (ADR-004 §①, retroactive-taint
    guard). ``as_of=None`` means "the full corpus" (no bound).
    """
    if not candidate:
        return 0.0
    if as_of is None or not isinstance(corpus, dict):
        hits = sum(1 for s in candidate if s in corpus)
    else:
        hits = sum(
            1 for s in candidate if corpus.get(s, float("inf")) <= as_of
        )
    return hits / len(candidate)


def _merge_shingles(dst: Dict[str, float], shingles: set, ts: float) -> None:
    """Merge ``shingles`` into the earliest-ts map (keeps the earliest ts)."""
    for s in shingles:
        prev = dst.get(s)
        if prev is None or ts < prev:
            dst[s] = ts


def _flatten_json_strings(value: Any, out: List[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _flatten_json_strings(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _flatten_json_strings(v, out)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class _SessionTaint:
    __slots__ = ("shingles", "entries", "shas", "corrupt", "truncated")

    def __init__(self) -> None:
        # Union of every injected span's shingles for the session, mapped to
        # the EARLIEST registration timestamp per shingle. The union (not
        # per-entry sets) is what containment checks against: a paraphrase
        # may stitch material from several injections. The per-shingle ts
        # lets recompute paths bound the corpus at a record's own ts
        # (``as_of``) so later injections cannot retroactively taint.
        self.shingles: Dict[str, float] = {}
        self.entries: List[Dict[str, Any]] = []
        # Shas already registered this session — dedup guard so the same
        # prefetch block re-injected every turn lands once in memory and
        # once in the sidecar (unbounded growth otherwise).
        self.shas: set = set()
        # True when the sidecar held undecodable material — reads for
        # assistant-span admission then fail closed (see module docstring).
        self.corrupt: bool = False
        self.truncated: bool = False


class TaintRegistry:
    """Per-session injected-span registry with a durable JSONL sidecar.

    Same construction contract as ``PendingTurnWAL``: the base directory is
    pinned eagerly on the constructing thread so worker-thread appends never
    chase a changed HERMES_HOME. All public methods are fail-open for writes
    and fail-closed (via the ``corrupt`` flag) for admission reads.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is not None:
            self._base_dir = Path(base_dir)
        else:
            from hermes_constants import get_hermes_home

            self._base_dir = (
                get_hermes_home() / "state" / "memory-pending" / "taint"
            )
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, _SessionTaint]" = OrderedDict()
        self._io_executor = None
        self._io_lock = threading.Lock()

    # -- paths / io -----------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        return self._base_dir / _safe_session_filename(session_id)

    def _submit_io(self, fn) -> None:
        """Run a sidecar append off-thread (single daemon worker, fail-open).

        The prefetch registration site is on the turn prologue hot path —
        disk I/O must not ride it. Inline fallback if the executor is gone:
        losing async beats losing the record.
        """
        executor = self._io_executor
        if executor is None:
            with self._io_lock:
                if self._io_executor is None:
                    try:
                        from tools.daemon_pool import DaemonThreadPoolExecutor

                        self._io_executor = DaemonThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="mem-taint"
                        )
                    except Exception:
                        self._io_executor = None
                executor = self._io_executor
        if executor is None:
            try:
                fn()
            except Exception:
                logger.debug("taint sidecar inline append failed", exc_info=True)
            return
        try:
            executor.submit(fn)
        except RuntimeError:
            try:
                fn()
            except Exception:
                logger.debug("taint sidecar inline append failed", exc_info=True)

    # -- session state ----------------------------------------------------------

    def _session_locked(self, session_id: str) -> _SessionTaint:
        """Get (loading from the sidecar if needed) the session state.

        Caller holds ``self._lock``.
        """
        state = self._sessions.get(session_id)
        if state is not None:
            self._sessions.move_to_end(session_id)
            return state
        state = _SessionTaint()
        path = self._path_for(session_id)
        try:
            if path.exists():
                parsed = 0
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            # ANY undecodable sidecar line means injections
                            # may be missing from the registry — admission
                            # reads must fail closed for assistant spans.
                            state.corrupt = True
                            continue
                        if not isinstance(rec, dict):
                            state.corrupt = True
                            continue
                        text = str(rec.get("text") or "")
                        sha = str(rec.get("sha") or "")
                        if sha and sha in state.shas:
                            # Pre-dedup sidecars can hold repeats: shingles
                            # (earliest ts) are already merged, skip the dup.
                            continue
                        try:
                            ts_val = float(rec.get("ts") or 0.0)
                        except (TypeError, ValueError):
                            # Unparseable ts: treat as "always present"
                            # (epoch), the over-taint direction.
                            ts_val = 0.0
                        _merge_shingles(state.shingles, _shingles(text), ts_val)
                        if sha:
                            state.shas.add(sha)
                        state.entries.append({
                            "sha": sha,
                            "source": rec.get("source") or "",
                            "ts": rec.get("ts"),
                            "chars": len(text),
                        })
                        parsed += 1
                if parsed > _MAX_ENTRIES_PER_SESSION:
                    state.truncated = True
        except Exception:
            # Unreadable sidecar == corrupt registry: fail closed on reads.
            logger.debug("taint sidecar load failed (fail-closed)", exc_info=True)
            state.corrupt = True
        self._sessions[session_id] = state
        while len(self._sessions) > _MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return state

    # -- recording ----------------------------------------------------------------

    def record_injected_text(
        self, session_id: str, text: str, *, source: str = "prefetch"
    ) -> None:
        """Register memory-derived text that was shown to the agent.

        Scrubs, shingles, updates the in-memory union synchronously (so the
        same process's WAL tagger sees it immediately) and appends the
        scrubbed text to the sidecar asynchronously. Identical text (by sha
        of the scrubbed content) is registered ONCE per session — the
        prefetch block re-injected every turn must not grow the sidecar or
        the entry list unboundedly (its shingles are already in the union,
        stamped with the FIRST injection's ts). Never raises, never blocks
        on disk.
        """
        try:
            text = _scrub(text or "")
            if not text.strip():
                return
            session_id = session_id or ""
            shingles = _shingles(text)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            # Floor, don't round: a half-up rounding can stamp the injection
            # a fraction of a millisecond into the FUTURE, and a proposal/WAL
            # record written inside that window would exclude it from its
            # as_of-bounded corpus (tainted=False flake). Floored ts is always
            # <= the actual registration time, preserving the as_of guard.
            ts = int(time.time() * 1000) / 1000
            with self._lock:
                state = self._session_locked(session_id)
                if sha in state.shas:
                    return  # dedup: already registered this session
                state.shas.add(sha)
                _merge_shingles(state.shingles, shingles, ts)
                state.entries.append(
                    {"sha": sha, "source": source, "ts": ts, "chars": len(text)}
                )
                if len(state.entries) > _MAX_ENTRIES_PER_SESSION:
                    # FIFO cap: dropping oldest can only under-taint; flag it
                    # so the digest consumers know the registry is partial.
                    state.entries.pop(0)
                    state.truncated = True
            if journals_disabled():
                return
            path = self._path_for(session_id)
            record = {
                "ts": ts,
                "session_id": session_id,
                "source": source,
                "sha": sha,
                # Scrubbed full text: shingles are recomputed at load time,
                # which keeps the sidecar smaller than persisted shingle
                # sets and lets threshold/shingle-size changes apply
                # retroactively.
                "text": text,
            }
            self._submit_io(lambda: _append_jsonl(path, record))
        except Exception:
            logger.debug("taint registry record failed (fail-open)", exc_info=True)

    # -- taint computation -----------------------------------------------------------

    def assistant_taint(
        self,
        session_id: str,
        content: str,
        as_of: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute the taint block for an assistant-authored span.

        Returns a dict (never raises)::

            {"tainted": bool, "spans": [[start, end], ...], "score": float,
             "reason": str, "registry": "ok"|"corrupt"|"empty",
             "method": ..., "threshold": float}

        ``spans`` are original-coordinate character ranges of the tainted
        segments. ``as_of`` bounds the corpus to injections first registered
        at or before that timestamp — pass the span's authoring/journal ts
        so injections that postdate the span cannot taint it (they cannot
        have caused it). On registry corruption the whole span is tainted
        (fail-closed — see module docstring).
        """
        try:
            content = content or ""
            threshold = containment_threshold()
            base = {
                "method": _TAINT_METHOD,
                "threshold": threshold,
            }
            # Literal fence material is memory-derived at record time, no
            # registry needed: those tags only ever enter the transcript via
            # injection (and an assistant echoing them is the leak case).
            if _FENCE_TAG_RE.search(content):
                return {
                    **base,
                    "tainted": True,
                    "spans": [[0, len(content)]],
                    "score": 1.0,
                    "reason": "memory-context-fence",
                    "registry": "ok",
                }
            with self._lock:
                state = self._session_locked(session_id or "")
                corrupt = state.corrupt
                # Copy under the lock: record_injected_text mutates the
                # union in place, and dict iteration during mutation raises.
                corpus = dict(state.shingles)
            if corrupt:
                return {
                    **base,
                    "tainted": True,
                    "spans": [[0, len(content)]],
                    "score": 1.0,
                    "reason": "registry-corrupt (fail-closed for assistant spans)",
                    "registry": "corrupt",
                }
            if not corpus:
                return {
                    **base,
                    "tainted": False,
                    "spans": [],
                    "score": 0.0,
                    "reason": "no injections registered this session",
                    "registry": "empty",
                }
            spans: List[List[int]] = []
            max_score = 0.0
            for start, end in _segments(content):
                seg_shingles = _shingles(content[start:end])
                if not seg_shingles:
                    continue  # < SHINGLE_SIZE normalized chars: no verdict
                score = _containment(seg_shingles, corpus, as_of)
                if len(seg_shingles) < _MIN_SEGMENT_SHINGLES and score < 1.0:
                    # Too few shingles for a meaningful ratio — but a tiny
                    # segment FULLY contained in the corpus is still an echo
                    # (a short Korean fact can pass quote admissibility at
                    # ~8 Hangul chars, which is a 1-shingle segment; skipping
                    # it entirely would leave those quotes taint-blind).
                    continue
                max_score = max(max_score, score)
                if score >= threshold:
                    # Merge with the previous span when contiguous-ish (the
                    # break between them was just whitespace/punctuation).
                    if spans and start - spans[-1][1] <= 2:
                        spans[-1][1] = end
                    else:
                        spans.append([start, end])
            return {
                **base,
                "tainted": bool(spans),
                "spans": spans,
                "score": round(max_score, 3),
                "reason": "shingle-containment" if spans else "below threshold",
                "registry": "ok",
            }
        except Exception:
            # Computation failure == we cannot prove the span clean:
            # fail closed, same as corruption.
            logger.debug("assistant taint computation failed (fail-closed)",
                         exc_info=True)
            return {
                "method": _TAINT_METHOD,
                "threshold": DEFAULT_CONTAINMENT_THRESHOLD,
                "tainted": True,
                "spans": [[0, len(content or "")]],
                "score": 1.0,
                "reason": "taint computation failed (fail-closed)",
                "registry": "corrupt",
            }

    def quote_taint(
        self,
        session_id: str,
        quote: str,
        as_of: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Taint verdict for a QUOTE string itself (ADR-004 §① admission).

        Segment-level tainting can be diluted: a verbatim injected fact
        embedded inside a longer original assistant sentence drags the
        segment's containment below threshold, yet a quote of just the fact
        is 100% injected text. Admission therefore also shingles the quote
        string directly against the session's injected corpus — bounded by
        ``as_of`` (the ts of the journal span the quote grounds in) so
        injections that postdate the span cannot retroactively reject it.

        Same threshold/tiny-candidate semantics as segments: below
        ``_MIN_SEGMENT_SHINGLES`` shingles only full containment (1.0)
        taints; at or above, the containment threshold applies. Fail-closed
        on registry corruption or internal error (assistant-span quotes
        only reach here — user-span grounding admits earlier). Never raises.
        """
        try:
            quote = quote or ""
            threshold = containment_threshold()
            base = {"method": _TAINT_METHOD, "threshold": threshold}
            if _FENCE_TAG_RE.search(quote):
                return {**base, "tainted": True, "score": 1.0,
                        "reason": "memory-context-fence", "registry": "ok"}
            with self._lock:
                state = self._session_locked(session_id or "")
                corrupt = state.corrupt
                corpus = dict(state.shingles)
            if corrupt:
                return {**base, "tainted": True, "score": 1.0,
                        "reason": "registry-corrupt (fail-closed for "
                                  "assistant spans)",
                        "registry": "corrupt"}
            if not corpus:
                return {**base, "tainted": False, "score": 0.0,
                        "reason": "no injections registered this session",
                        "registry": "empty"}
            quote_shingles = _shingles(quote)
            if not quote_shingles:
                # < SHINGLE_SIZE normalized chars: no verdict possible (and
                # below any useful quote admissibility length).
                return {**base, "tainted": False, "score": 0.0,
                        "reason": "quote below shingle size",
                        "registry": "ok"}
            score = _containment(quote_shingles, corpus, as_of)
            if len(quote_shingles) < _MIN_SEGMENT_SHINGLES:
                tainted = score >= 1.0
            else:
                tainted = score >= threshold
            return {**base, "tainted": tainted, "score": round(score, 3),
                    "reason": ("quote-shingle-containment" if tainted
                               else "below threshold"),
                    "registry": "ok"}
        except Exception:
            logger.debug("quote taint computation failed (fail-closed)",
                         exc_info=True)
            return {
                "method": _TAINT_METHOD,
                "threshold": DEFAULT_CONTAINMENT_THRESHOLD,
                "tainted": True,
                "score": 1.0,
                "reason": "quote taint computation failed (fail-closed)",
                "registry": "corrupt",
            }

    # -- digest / lifecycle -----------------------------------------------------------

    def session_injected_digest(self, session_id: str) -> Dict[str, Any]:
        """Summary of everything injected into a session (Phase-3 API, §⑤).

        Dream promotion calls this to exclude tainted re-occurrences from
        spacing-gate corroboration counts: a fact that "re-appeared" in a
        session whose digest already contains it arrived via prefetch, not
        via independent user/external evidence. Fail-open: on any internal
        error returns a digest with ``registry: "corrupt"`` (callers must
        then treat the session's assistant spans as taint-unknown → excluded).
        """
        try:
            with self._lock:
                state = self._session_locked(session_id or "")
                sources: Dict[str, int] = {}
                for e in state.entries:
                    src = str(e.get("source") or "?")
                    sources[src] = sources.get(src, 0) + 1
                return {
                    "session_id": session_id or "",
                    "count": len(state.entries),
                    "sources": sources,
                    "shas": [e.get("sha") for e in state.entries],
                    "shingle_count": len(state.shingles),
                    "truncated": state.truncated,
                    "registry": "corrupt" if state.corrupt else (
                        "ok" if state.entries else "empty"
                    ),
                }
        except Exception:
            logger.debug("taint digest failed", exc_info=True)
            return {
                "session_id": session_id or "",
                "count": 0,
                "sources": {},
                "shas": [],
                "shingle_count": 0,
                "truncated": False,
                "registry": "corrupt",
            }

    def end_session(self, session_id: str) -> None:
        """Evict a session's in-memory registry (session-end hook).

        The sidecar file is deliberately KEPT: the shadow curator and dream
        run after session end and need the registry to compute taint for
        spans they replay. Disk lifecycle is :meth:`gc_stale_files` (7-day
        TTL, matching the WAL's fully-acked GC window).
        """
        try:
            with self._lock:
                self._sessions.pop(session_id or "", None)
        except Exception:
            pass

    def drain_io(self, timeout: float = 5.0) -> None:
        """Block until queued sidecar appends have landed (tests/shutdown).

        Submits a barrier task to the single IO worker and waits for it;
        fail-open like everything else here.
        """
        executor = self._io_executor
        if executor is None:
            return
        try:
            executor.submit(lambda: None).result(timeout=timeout)
        except Exception:
            pass

    def gc_stale_files(self) -> int:
        """Delete sidecar files idle past the TTL. Returns count deleted."""
        deleted = 0
        try:
            if not self._base_dir.is_dir():
                return 0
            now = time.time()
            for path in self._base_dir.glob("*.jsonl"):
                try:
                    if now - path.stat().st_mtime > _SIDECAR_GC_MAX_AGE_S:
                        path.unlink()
                        deleted += 1
                except FileNotFoundError:
                    continue
            if deleted:
                logger.info("taint sidecar GC: %d stale file(s) deleted", deleted)
        except Exception:
            logger.debug("taint sidecar GC failed (fail-open)", exc_info=True)
        return deleted


# ---------------------------------------------------------------------------
# Module-level default registry (process singleton)
# ---------------------------------------------------------------------------

_default_registry: Optional[TaintRegistry] = None
_default_registry_lock = threading.Lock()
_gc_ran = False


def get_registry() -> TaintRegistry:
    """Process-default registry, lazily pinned to the active HERMES_HOME."""
    global _default_registry, _gc_ran
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = TaintRegistry()
    if not _gc_ran:
        with _default_registry_lock:
            if not _gc_ran:
                _gc_ran = True
                try:
                    _default_registry.gc_stale_files()
                except Exception:
                    pass
    return _default_registry


def set_registry(registry: Optional[TaintRegistry]) -> None:
    """Swap the process-default registry (tests / multi-profile setups)."""
    global _default_registry, _gc_ran
    with _default_registry_lock:
        _default_registry = registry
        _gc_ran = registry is not None  # a supplied registry manages its own GC


# ---------------------------------------------------------------------------
# Recording entry points (touch-point API)
# ---------------------------------------------------------------------------

def record_injected_text(
    session_id: str, text: str, *, source: str = "prefetch"
) -> None:
    """Register injected memory text (prefetch fence content). Never raises."""
    try:
        get_registry().record_injected_text(session_id, text, source=source)
    except Exception:
        logger.debug("record_injected_text failed (fail-open)", exc_info=True)


def record_injected_tool_result(
    session_id: str, result_text: str, *, source: str = "memory_search"
) -> None:
    """Register a memory-provider tool result returned to the agent.

    Tool results are JSON envelopes serialized with ``ensure_ascii=True``
    (Korean text arrives as ``\\uXXXX`` escapes), so the decoded string
    values — the text the model actually reads and paraphrases — are what
    gets registered; the raw envelope is registered only when it doesn't
    parse. Never raises.
    """
    try:
        text = result_text or ""
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            flat: List[str] = []
            _flatten_json_strings(parsed, flat)
            text = "\n".join(flat)
        record_injected_text(session_id, text, source=source)
    except Exception:
        logger.debug("record_injected_tool_result failed (fail-open)",
                     exc_info=True)


# ---------------------------------------------------------------------------
# WAL tagging (called by memory_journal at record time)
# ---------------------------------------------------------------------------

def tag_wal_turn_records(
    session_id: str,
    records: List[Dict[str, Any]],
    as_of: Optional[float] = None,
) -> None:
    """Stamp taint metadata onto a WAL turn's role records, in place.

    * ``role == "user"`` — never tainted, never stamped: user words are
      user-origin by definition (ADR-004 §①: corroboration means "user
      utterance or external source origin"). A user pasting fence-looking
      text is still user-authored input — injection fences never ride the
      user row in this architecture (they live only in the api_content
      sidecar), so there is nothing machine-injected to protect against.
    * ``role == "assistant"`` — tainted iff shingle containment against the
      session's injected registry crosses the threshold (or the content
      carries literal fence tags / the registry is corrupt → fail-closed).

    Assistant records are ALWAYS stamped — a clean verdict is stamped
    explicitly. The write-time verdict is authoritative: it was computed
    against exactly the injections that existed when the span was authored
    (``as_of`` = the turn-boundary ts captured on the foreground thread, so
    a backed-up mem-sync worker journaling late cannot pick up later turns'
    registrations). Absence of the key therefore means only "pre-taint-patch
    record"; enforcement recomputes those bounded by the record's own ts,
    which can never taint a span with injections that postdate it. Never
    raises; on failure records stay untagged (recompute path applies).
    """
    try:
        registry = get_registry()
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            if (rec.get("role") or "") != "assistant":
                continue
            rec["taint"] = registry.assistant_taint(
                session_id, str(rec.get("content") or ""), as_of=as_of
            )
    except Exception:
        logger.debug("WAL turn taint tagging failed (fail-open)", exc_info=True)


def tag_wal_proposal_record(
    session_id: str,
    record: Dict[str, Any],
    as_of: Optional[float] = None,
) -> None:
    """Stamp taint metadata onto a WAL proposal record, in place.

    ``memory_propose`` content is agent-authored text (the tool call's
    arguments are written by the assistant, whatever ``origin`` claims), so
    it gets the assistant treatment: a proposal that paraphrases injected
    memory must not later count as independent evidence. Always-stamped +
    fail-open, same contract as :func:`tag_wal_turn_records`.
    """
    try:
        if not isinstance(record, dict):
            return
        record["taint"] = get_registry().assistant_taint(
            session_id, str(record.get("content") or ""), as_of=as_of
        )
    except Exception:
        logger.debug("WAL proposal taint tagging failed (fail-open)",
                     exc_info=True)


# ---------------------------------------------------------------------------
# Enforcement (called by memory_pipeline grounding)
# ---------------------------------------------------------------------------

def _occurrences_all_tainted(
    content: str, quote: str, spans: List[List[int]]
) -> bool:
    """True when every occurrence of ``quote`` in ``content`` overlaps a
    tainted span. A single occurrence fully outside the tainted spans means
    the quote is grounded in clean material (partial overlap counts as
    tainted — conservative)."""
    if not spans:
        return False
    found = False
    start = 0
    while True:
        i = content.find(quote, start)
        if i == -1:
            break
        found = True
        occ_start, occ_end = i, i + len(quote)
        overlaps = any(s < occ_end and occ_start < e for s, e in spans)
        if not overlaps:
            return False
        start = i + 1
    return found


def _span_quote_tainted(
    session_id: str,
    role: str,
    content: str,
    quote: str,
    stored_taint: Optional[Dict[str, Any]],
    as_of: Optional[float] = None,
) -> bool:
    """Taint verdict for one quote occurrence-set within one journal span."""
    if role == "user":
        return False
    taint = stored_taint
    if not isinstance(taint, dict):
        # No stored tag: pre-taint-patch record. Live-recompute, bounded by
        # the record's own ts (``as_of``) so injections registered AFTER the
        # span was authored — the agent's own memory_search surfacing this
        # session's per-turn-ingested content, a shared-session fork's reads
        # — can never retroactively taint it. Post-patch records always
        # carry an explicit (possibly clean) write-time tag, which wins.
        taint = get_registry().assistant_taint(session_id, content, as_of=as_of)
    if not taint.get("tainted"):
        return False
    spans: List[List[int]] = []
    for pair in taint.get("spans") or []:
        try:
            s, e = pair
            spans.append([int(s), int(e)])
        except Exception:
            continue
    if not spans:
        # Tainted but span-less (corrupt registry fail-closed shape without
        # spans): treat the whole span as tainted.
        return True
    return _occurrences_all_tainted(content, quote, spans)


def matched_quote_taint(
    session_id: str,
    matched: List[Tuple[str, str, Optional[Dict[str, Any]], Optional[float]]],
    quote: str,
) -> Optional[str]:
    """Admission check for a quote that already substring-matched journal
    content. ``matched`` is ``[(role, content, stored_taint, ts), ...]`` —
    every journal span the quote was found in (``ts`` is the journal
    record's timestamp, used to bound registry recomputes and the quote
    self-check so later injections cannot retroactively reject the quote).
    Returns a rejection detail string when the quote is taint-ineligible,
    else None.

    A quote grounded in ANY user span is always admissible (user words are
    the origin corroboration trusts). Otherwise a span admits the quote only
    when BOTH hold:

    * the span check passes — at least one occurrence of the quote in the
      span lies outside the span's tainted segments, and
    * the quote self-check passes — the quote's OWN shingle containment
      against injections registered at or before the span's ts stays below
      threshold. Segment scores dilute (a verbatim injected fact inside a
      longer original sentence keeps the segment clean), so the span check
      alone would admit a quote that is 100% injected text.

    Fail-closed: an internal error while checking yields a rejection, not
    an admission.
    """
    if not taint_enforce_enabled():
        return None
    try:
        if any(m[0] == "user" for m in matched):
            return None
        if not matched:
            return None
        registry = get_registry()
        for role, content, stored_taint, ts in matched:
            try:
                as_of = float(ts) if ts else None
            except (TypeError, ValueError):
                as_of = None
            if _span_quote_tainted(
                session_id, role, content, quote, stored_taint, as_of
            ):
                continue
            if registry.quote_taint(session_id, quote, as_of=as_of).get(
                "tainted"
            ):
                continue
            return None
        return (
            "quote matches only memory-tainted assistant spans "
            "(ADR-004 §① origin-taint): the text overlaps this session's "
            "injected <memory-context>/memory_search content, so it cannot "
            "serve as evidence — memory citing its own echo is not "
            "corroboration. Cite the user/tool span the fact actually "
            "came from."
        )
    except Exception:
        logger.debug("quote taint check failed (fail-closed)", exc_info=True)
        return (
            "quote taint check failed — failing closed for assistant-span "
            "quotes (taint protects corroboration integrity); cite a user "
            "span instead"
        )


# ---------------------------------------------------------------------------
# Curator rendering hook (lane A consumes these if present — §4.6)
# ---------------------------------------------------------------------------

def curator_taint_label(
    session_id: str,
    role: str,
    content: str,
    stored_taint: Optional[Dict[str, Any]] = None,
) -> str:
    """``"[tainted]"`` marker for the curator's input rendering, else ``""``.

    Per the §4.6 prompt contract, spans carrying this marker are
    quote-ineligible in the curator's verdict output. User spans never get
    the marker. Fail-closed like admission: if taint can't be determined for
    an assistant span, it is rendered ``[tainted]``.
    """
    try:
        if role == "user":
            return ""
        taint = stored_taint
        if not isinstance(taint, dict):
            taint = get_registry().assistant_taint(session_id, content or "")
        return "[tainted]" if taint.get("tainted") else ""
    except Exception:
        return "[tainted]"


def annotate_wal_records_for_curator(
    session_id: str, records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Shallow-copied WAL role records with ``curator_label`` set per §4.6.

    Convenience wrapper for the curator context assembly (lane A): each
    returned record carries ``curator_label: "[tainted]"|""``. Original
    records are not mutated. Fail-open: on error records come back with
    empty labels for user spans and ``[tainted]`` for the rest.
    """
    out: List[Dict[str, Any]] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        copy = dict(rec)
        copy["curator_label"] = curator_taint_label(
            session_id,
            str(rec.get("role") or ""),
            str(rec.get("content") or ""),
            rec.get("taint") if isinstance(rec.get("taint"), dict) else None,
        )
        out.append(copy)
    return out


# ---------------------------------------------------------------------------
# Phase-3 corroboration API (§⑤ dream promotion)
# ---------------------------------------------------------------------------

def span_taint(span: Dict[str, Any]) -> bool:
    """Is this journal span memory-tainted? (Phase-3 corroboration API.)

    ``span`` is a WAL-shaped role record — ``{"role": ..., "content": ...,
    "session_id": ..., "taint": ...?}`` (``session_id`` may live on the
    enclosing turn record; pass it through). Dream promotion (§⑤) must call
    this per re-occurrence and EXCLUDE tainted spans from spacing-gate
    corroboration counts and usage/mention aggregation: only user-origin or
    external-source evidence corroborates.

    Semantics: user spans are never tainted; assistant/proposal spans use
    the stored record tag when present, else a live registry recompute
    bounded by the span's own ``ts`` when it carries one (injections that
    postdate the span cannot have caused it); indeterminate (corrupt
    registry / internal error) counts as TAINTED — an unverifiable
    re-occurrence must not promote a fact.
    """
    try:
        if not isinstance(span, dict):
            return True
        role = str(span.get("role") or "")
        if role == "user":
            return False
        stored = span.get("taint") if isinstance(span.get("taint"), dict) else None
        if stored is not None:
            return bool(stored.get("tainted"))
        try:
            as_of: Optional[float] = float(span.get("ts")) if span.get("ts") else None
        except (TypeError, ValueError):
            as_of = None
        taint = get_registry().assistant_taint(
            str(span.get("session_id") or ""),
            str(span.get("content") or ""),
            as_of=as_of,
        )
        return bool(taint.get("tainted"))
    except Exception:
        return True


def session_injected_digest(session_id: str) -> Dict[str, Any]:
    """Digest of a session's injected memory content (Phase-3 API, §⑤).

    See :meth:`TaintRegistry.session_injected_digest` — exposed at module
    level so dream promotion can call it without holding a registry handle.
    """
    try:
        return get_registry().session_injected_digest(session_id)
    except Exception:
        return {
            "session_id": session_id or "",
            "count": 0,
            "sources": {},
            "shas": [],
            "shingle_count": 0,
            "truncated": False,
            "registry": "corrupt",
        }


def end_session(session_id: str) -> None:
    """Evict a session's in-memory registry (its sidecar file remains)."""
    try:
        get_registry().end_session(session_id)
    except Exception:
        pass


__all__ = [
    "SHINGLE_SIZE",
    "DEFAULT_CONTAINMENT_THRESHOLD",
    "TaintRegistry",
    "get_registry",
    "set_registry",
    "taint_enforce_enabled",
    "containment_threshold",
    "record_injected_text",
    "record_injected_tool_result",
    "tag_wal_turn_records",
    "tag_wal_proposal_record",
    "matched_quote_taint",
    "curator_taint_label",
    "annotate_wal_records_for_curator",
    "span_taint",
    "session_injected_digest",
    "end_session",
]
