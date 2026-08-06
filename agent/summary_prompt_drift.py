"""R5 measurement probe: can a quiescent-point precompute ever match the
prompt the next foreground compaction actually sends?

WHAT THIS MODULE IS NOT
-----------------------
It is **not** a cache.  Nothing stored here is ever served to anybody.  There
is no field on :class:`_ParkedEntry` capable of holding a summary, a prompt or
a message — only hashes and scalars — and no code in
``agent/context_compressor.py`` reads a parked entry to decide anything.  The
auxiliary provider call in ``_generate_summary`` stays unconditional at every
gate setting.  Enabling ``compression.summary_prompt_drift_probe`` buys
knowledge and cannot make any compaction faster.

WHAT IT MEASURES, AND WHY THAT IS ENOUGH
----------------------------------------
The design this probe evaluates would key a precomputed summariser output on a
hash of the EXACT prompt that would be sent.  An exact-prompt hit therefore
requires **every** prompt component to agree.  One of those components is the
auto-derived focus block: ``ContextCompressor.compress()`` computes
``focus_topic or self._derive_auto_focus_topic(messages)`` over the FULL live
transcript (not the compression span), and that block is appended verbatim into
the prompt as the trailing ``FOCUS TOPIC:`` section.

So focus-block agreement is a *necessary condition* for the specified key to
hit.  If the block differs, the prompt differs and the key misses — regardless
of window, budget, previous summary, memory section, date or span.  Comparing
that single component is consequently sufficient to prove the key dead, and it
is the only component obtainable without cloning the compressor, spawning a
worker, reimplementing the compaction boundary pipeline or invoking the
side-effecting memory ``on_pre_compress`` hook.

``_derive_auto_focus_topic`` is a ``@classmethod`` whose sole input is the
message list: it mutates nothing, needs no compressor state, no resolved
context length, no boundary, no memory hook and no provider.  That is what
makes the park cheap and provably read-only.

TWO CALL SITES
--------------
* ``park(...)`` — called inline on the turn thread from
  ``agent/turn_finalizer.py`` at the quiescent point after the user's answer
  has been delivered.  Records the SHA-256 of the auto-focus block as it stands
  at that moment.
* ``observe(...)`` — called from ``ContextCompressor._generate_summary`` once
  every prompt component is already resident in a local variable.  Compares the
  focus hash it was handed against the parked one and records the other
  components as observations only (there is no parked counterpart for them,
  because parking them would need exactly the machinery this probe refuses to
  build).

Counters live at MODULE level, not on a compressor instance, so a single
``snapshot()`` call reads both sides.  Homing park counters on one object and
observe counters on another is how an earlier design made its own cost
accounting structurally unreadable.

The store is an LRU keyed by ``session_id`` because the gateway holds many live
sessions in one process and runs turns on executor threads; a single global slot
would be dominated by cross-session eviction and would report availability
failure as prompt drift.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Bounded so a long-lived gateway process cannot grow this without limit.
_MAX_PARKED = 16
# Bounded so the per-compaction observation list cannot grow without limit in a
# very long measurement run.  5000 observations is far more than the 913
# compactions the whole production corpus contains.
_MAX_COMPONENT_OBSERVATIONS = 5000

# Domain-separation tags.  Every digest is tagged so a hash of one component can
# never collide with a hash of another.
_TAG_FOCUS = "AF1"
_TAG_WINDOW = "WIN1"
_TAG_PREV = "PREV1"
_TAG_MEMORY = "MEM1"

# Distinguishable from any real focus block, used only so the digest input is
# well defined when the block is None.  ``focus_is_none`` is what the reporting
# actually keys on; this exists so "no block" and "empty block" cannot collide.
_NONE_FOCUS_SENTINEL = b"\x00<no-auto-focus-block>"


@dataclass
class _ParkedEntry:
    """Hashes and scalars only.

    There is deliberately no field here that can hold a summary, a prompt or a
    message, which is what makes "serve a wrong summary" unrepresentable rather
    than merely unreached.
    """

    session_id: str
    focus_hash: str
    focus_is_none: bool
    prev_summary_hash: str
    has_user_turn: Optional[bool]
    today_str: str
    msg_count: int
    turn_seq: int
    park_monotonic: float
    observe_count: int = 0


def _new_stats() -> Dict[str, Any]:
    return {
        # cost / availability.  ``park_us`` exists because the park is
        # sub-millisecond on ordinary transcripts, so an integer-ms histogram
        # would floor almost every sample to 0 and the "is the measurement
        # itself too expensive" question would be unanswerable.
        "park": 0,
        "park_failed": 0,
        "park_ms": [],
        "park_us": [],
        # observation population
        "observe": 0,
        "observe_failed": 0,
        # structural guards — never reported as prompt drift
        "observe_no_entry": 0,
        "observe_different_session": 0,
        "stale_date": 0,
        "repeat_observation": 0,
        "explicit_focus": 0,
        # the decisive comparison
        "focus_agree": 0,
        "focus_differ": 0,
        "focus_both_none": 0,
        "focus_none_mismatch": 0,
        # how far back the compared park was.  ``_generate_summary`` receives
        # only the compression span, never the live transcript, so a literal
        # "turns since park" count is not obtainable at the observe seam
        # without new plumbing.  These two are what IS obtainable: the wall
        # gap (monotonic, same process) and the park's own turn sequence
        # number, whose deltas across consecutive observations in a session
        # show how many turns elapsed.
        "park_age_ms": [],
        "park_turn_seq": [],
        # observed-only characterisation
        "window_chars": [],
        "window_bounded": 0,
        "component_observations": [],
    }


_LOCK = threading.Lock()
_PARKED: "OrderedDict[str, _ParkedEntry]" = OrderedDict()
_STATS: Dict[str, Any] = _new_stats()


def _digest(tag: str, *fields: bytes) -> str:
    """Length-prefixed SHA-256 over a tag plus an ordered list of raw fields.

    Conversation text is attacker-influenceable, so a naive delimiter join is
    forgeable across adjacent fields.  Length-prefixing every field (including
    the tag) is cheap and removes that whole class of ambiguity.
    """
    hasher = hashlib.sha256()
    raw_tag = tag.encode("utf-8")
    hasher.update(f"{len(raw_tag)}:".encode("ascii"))
    hasher.update(raw_tag)
    for raw in fields:
        hasher.update(f"{len(raw)}:".encode("ascii"))
        hasher.update(raw)
    return hasher.hexdigest()


def _text_digest(tag: str, text: Optional[str]) -> str:
    return _digest(tag, (text or "").encode("utf-8", "replace"))


def _focus_digest(focus_block: Optional[str]) -> str:
    if focus_block is None:
        return _digest(_TAG_FOCUS, _NONE_FOCUS_SENTINEL)
    return _digest(_TAG_FOCUS, focus_block.encode("utf-8", "replace"))


def _short(digest: str) -> str:
    return digest[:12]


def park(
    *,
    session_id: str,
    focus_block: Optional[str],
    prev_summary_redacted: Optional[str],
    has_user_turn: Optional[bool],
    today_str: str,
    msg_count: int,
    turn_seq: int,
    elapsed_ms: int,
    elapsed_us: int = -1,
) -> None:
    """Record the quiescent-point fingerprint for *session_id*.

    Overwrites any previous park for the same session: only the most recent
    quiescent point is a candidate for the next compaction.
    """
    try:
        entry = _ParkedEntry(
            session_id=str(session_id or ""),
            focus_hash=_focus_digest(focus_block),
            focus_is_none=focus_block is None,
            prev_summary_hash=_text_digest(_TAG_PREV, prev_summary_redacted),
            has_user_turn=has_user_turn,
            today_str=str(today_str or ""),
            msg_count=int(msg_count or 0),
            turn_seq=int(turn_seq or 0),
            park_monotonic=time.monotonic(),
            observe_count=0,
        )
        with _LOCK:
            _PARKED.pop(entry.session_id, None)
            _PARKED[entry.session_id] = entry
            while len(_PARKED) > _MAX_PARKED:
                _PARKED.popitem(last=False)
            _STATS["park"] += 1
            _STATS["park_ms"].append(int(elapsed_ms or 0))
            _STATS["park_us"].append(int(elapsed_us))
        logger.debug(
            "hermes.r5probe drift_park focus=%s none=%s prev=%s msgs=%d ms=%d us=%d",
            _short(entry.focus_hash),
            entry.focus_is_none,
            _short(entry.prev_summary_hash),
            entry.msg_count,
            int(elapsed_ms or 0),
            int(elapsed_us),
        )
    except Exception:  # pragma: no cover - a probe must never break a turn
        record_park_failure()


def record_park_failure() -> None:
    """Count a park that could not be built (the derivation itself raised)."""
    try:
        with _LOCK:
            _STATS["park_failed"] += 1
    except Exception:  # pragma: no cover
        pass


def observe(
    *,
    session_id: str,
    focus_topic: Optional[str],
    explicit_focus: bool,
    prev_summary: Optional[str],
    has_user_turn: Optional[bool],
    today_str: str,
    window_text: Optional[str],
    window_bounded: bool,
    budget: int,
    memory_context: Optional[str],
    span_len: int,
    msg_count: Optional[int] = None,
) -> None:
    """Compare the live summariser prompt's focus block against the parked one.

    Everything except the focus block is recorded as an observation only.  This
    function never returns a value the caller branches on and never mutates
    compressor state.
    """
    try:
        session_key = str(session_id or "")
        observed_focus_hash = _focus_digest(focus_topic)
        observed_focus_is_none = focus_topic is None
        window_hash = _text_digest(_TAG_WINDOW, window_text)
        prev_hash = _text_digest(_TAG_PREV, prev_summary)
        memory_hash = _text_digest(_TAG_MEMORY, memory_context)
        window_chars = len(window_text or "")
        observe_monotonic = time.monotonic()

        reason = ""
        agree: Optional[bool] = None
        park_age_ms = -1

        with _LOCK:
            _STATS["observe"] += 1
            _STATS["window_chars"].append(window_chars)
            if window_bounded:
                _STATS["window_bounded"] += 1
            if explicit_focus:
                _STATS["explicit_focus"] += 1
            if len(_STATS["component_observations"]) < _MAX_COMPONENT_OBSERVATIONS:
                _STATS["component_observations"].append(
                    {
                        "session_id": session_key,
                        "window_hash": window_hash,
                        "prev_summary_hash": prev_hash,
                        "memory_hash": memory_hash,
                        "has_user_turn": has_user_turn,
                        "budget": int(budget or 0),
                        "today_str": str(today_str or ""),
                        "span_len": int(span_len or 0),
                        "window_chars": window_chars,
                        "window_bounded": bool(window_bounded),
                        "explicit_focus": bool(explicit_focus),
                        "focus_hash": observed_focus_hash,
                        "focus_is_none": observed_focus_is_none,
                        "msg_count": msg_count,
                    }
                )

            entry = _PARKED.get(session_key)
            # Structural guards, checked BEFORE any hash comparison so a
            # structural mismatch can never be reported as prompt drift.
            if entry is None:
                _STATS["observe_no_entry"] += 1
                reason = "no_entry"
            elif entry.session_id != session_key:
                _STATS["observe_different_session"] += 1
                reason = "different_session"
            elif str(entry.today_str or "") != str(today_str or ""):
                entry.observe_count += 1
                _STATS["stale_date"] += 1
                reason = "stale_date"
            else:
                entry.observe_count += 1
                if entry.observe_count > 1:
                    _STATS["repeat_observation"] += 1
                    reason = "repeat_observation"
                elif explicit_focus:
                    # A user-supplied `/compress <focus>` string did not come
                    # from the auto-derivation, so comparing it to the parked
                    # auto block would manufacture a "differs".
                    reason = "explicit_focus"
                else:
                    park_age_ms = max(
                        int((observe_monotonic - entry.park_monotonic) * 1000), 0
                    )
                    _STATS["park_age_ms"].append(park_age_ms)
                    _STATS["park_turn_seq"].append(entry.turn_seq)
                    if entry.focus_is_none and observed_focus_is_none:
                        _STATS["focus_both_none"] += 1
                        reason = "both_none"
                    elif entry.focus_is_none != observed_focus_is_none:
                        # One side had a block and the other did not: a proven
                        # miss, tracked separately so it is never confused with
                        # agreement on absence.
                        _STATS["focus_none_mismatch"] += 1
                        _STATS["focus_differ"] += 1
                        agree = False
                        reason = "none_mismatch"
                    elif entry.focus_hash == observed_focus_hash:
                        _STATS["focus_agree"] += 1
                        agree = True
                        reason = "agree"
                    else:
                        _STATS["focus_differ"] += 1
                        agree = False
                        reason = "differ"

        logger.debug(
            "hermes.r5probe drift_observe focus_agree=%s reason=%s explicit=%s "
            "park_age_ms=%d win=%s win_chars=%d bounded=%s mem=%s budget=%d span=%d",
            agree,
            reason,
            bool(explicit_focus),
            park_age_ms,
            _short(window_hash),
            window_chars,
            bool(window_bounded),
            _short(memory_hash),
            int(budget or 0),
            int(span_len or 0),
        )
    except Exception:  # pragma: no cover - a probe must never break compaction
        try:
            with _LOCK:
                _STATS["observe_failed"] += 1
        except Exception:
            pass


def clear(session_id: str) -> None:
    """Drop the parked entry for *session_id* at a real session boundary."""
    try:
        with _LOCK:
            _PARKED.pop(str(session_id or ""), None)
    except Exception:  # pragma: no cover
        pass


def snapshot() -> Dict[str, Any]:
    """Return an independent copy of the counters for the measurement harness.

    The returned structure shares no mutable object with the live store, so a
    later ``park``/``observe`` cannot retroactively change a snapshot the
    orchestrator already read.
    """
    try:
        with _LOCK:
            out: Dict[str, Any] = {}
            for key, value in _STATS.items():
                if isinstance(value, list):
                    out[key] = [
                        dict(item) if isinstance(item, dict) else item
                        for item in value
                    ]
                else:
                    out[key] = value
            out["parked_sessions"] = len(_PARKED)
            out["parked_session_ids"] = list(_PARKED.keys())
            return out
    except Exception:  # pragma: no cover
        return _new_stats()


def reset() -> None:
    """Clear all probe state.  Tests and the measurement harness only."""
    global _STATS
    with _LOCK:
        _PARKED.clear()
        _STATS = _new_stats()


def parked_entry_for_test(session_id: str) -> Optional[_ParkedEntry]:
    """Read a parked entry.  Tests only — no production code calls this."""
    with _LOCK:
        return _PARKED.get(str(session_id or ""))
