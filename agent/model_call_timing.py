"""Per-wire-attempt timing slots for true model TTFT and attempt duration.

Why this is keyed on an opaque per-attempt TOKEN and not on ``api_request_id``:
``api_request_id`` is assigned ONE LINE before the conversation loop's retry
loop, so it is identical for every retry — and both streaming paths mint a
fresh physical request inside an INNER stream-retry loop
(``HERMES_STREAM_RETRIES``, default 2 => up to 3 wire requests) that never
changes it. Keying the denominator on the request id therefore mixes attempt
N's first frame with attempt N+1's issue time and can produce a NEGATIVE TTFT;
keying the denominator one-shot instead folds the failed attempt's stall plus
the backoff sleep back in, which is exactly the retry-inclusive defect R3
exists to remove. Each ``begin_wire_attempt`` mints a separate record, so an
inner stream retry produces a SEPARATE row.

An ``api_request_id`` is still carried, but only as the drain key that the
already-existing ``post_api_request`` / ``api_request_error`` hook payloads
supply, so no new hook kwarg (and therefore no new egress surface) is needed.

FAIL-OPEN CONTRACT — every public function in this module swallows all
exceptions internally and returns None / []. This is required, not defensive
style: the three provider stream factories have BARE bodies, so a raise there
lands in the managed-stream ``result["error"]`` and makes the turn take the
classified-API-error path (and can burn the fallback chain), and the bedrock
stamp additionally sits inside a ``try`` whose handler can set
``agent._disable_streaming = True`` and print a user-facing IAM warning.

KNOWN SEMANTIC ASYMMETRY: ``bedrock_converse`` TTFT is botocore-retry-INCLUSIVE
because ``agent/bedrock_adapter.py`` builds ``boto3.client("bedrock-runtime")``
with no ``Config(retries=...)``, unlike the OpenAI-wire and Anthropic clients
which pin ``max_retries=0``. Within the bedrock lane a throttled run and a
genuinely slow model are not separable, so bedrock p95 TTFT must not be
compared across load conditions. ``api_mode_family`` keeps the paths apart
rather than silently pooling them.
"""

from __future__ import annotations

import itertools
import logging
import threading
from time import monotonic_ns
from typing import Any

logger = logging.getLogger(__name__)

# Bounded so a stranded worker or a never-drained request id cannot grow
# without limit.
_MAX_RECORDS_PER_REQUEST = 8
_MAX_LIVE_REQUEST_IDS = 64

_LOCK = threading.Lock()
_TOKENS = itertools.count(1)
# api_request_id -> {token: record}. Insertion-ordered, LRU-evicted.
_SLOTS: "dict[str, dict[int, dict[str, Any]]]" = {}
# token -> api_request_id, so a stamp never has to re-read agent state.
_TOKEN_OWNERS: "dict[int, str]" = {}


def begin_wire_attempt(
    api_request_id: Any,
    *,
    api_mode_family: str = "unknown",
    stream_mode: str = "unknown",
    call_role: str = "unknown",
    work_lane: str = "unknown",
    provider: str = "",
    model: str = "",
) -> int | None:
    """Stamp the instant one PHYSICAL provider request was issued on the wire.

    Returns an opaque token to pass to ``stamp_first_frame`` /
    ``finish_wire_attempt``, or None when timing is unavailable. The token is
    captured in the caller's factory closure, which is what makes a superseded
    daemon worker unable to poison the next attempt's slot: nothing re-reads
    ``agent._current_api_request_id`` at stamp time.
    """
    try:
        request_id = str(api_request_id or "")
        if not request_id:
            return None
        issued_ns = monotonic_ns()
        with _LOCK:
            token = next(_TOKENS)
            records = _SLOTS.get(request_id)
            if records is None:
                records = {}
                _SLOTS[request_id] = records
                while len(_SLOTS) > _MAX_LIVE_REQUEST_IDS:
                    evicted, evicted_records = next(iter(_SLOTS.items()))
                    _SLOTS.pop(evicted, None)
                    for stale_token in evicted_records:
                        _TOKEN_OWNERS.pop(stale_token, None)
            # A new physical attempt under the same api_request_id means the
            # previous one is over. Close it at this attempt's issue instant so
            # a superseded record still yields a bounded duration even when its
            # own terminal never ran (the outer terminal only sees the newest
            # token).
            for prior in records.values():
                if prior.get("end_ns") is None:
                    prior["end_ns"] = issued_ns
            records[token] = {
                "token": token,
                "api_request_id": request_id,
                "issued_ns": issued_ns,
                "first_frame_ns": None,
                "end_ns": None,
                "attempt_outcome": "",
                "api_mode_family": str(api_mode_family or "unknown"),
                "stream_mode": str(stream_mode or "unknown"),
                "call_role": str(call_role or "unknown"),
                "work_lane": str(work_lane or "unknown"),
                "provider": str(provider or ""),
                "model": str(model or ""),
            }
            _TOKEN_OWNERS[token] = request_id
            while len(records) > _MAX_RECORDS_PER_REQUEST:
                oldest = next(iter(records))
                records.pop(oldest, None)
                _TOKEN_OWNERS.pop(oldest, None)
        return token
    except Exception:
        logger.debug("Unable to begin a wire-attempt timing slot", exc_info=True)
        return None


def stamp_first_frame(token: Any) -> None:
    """Record the first wire frame of THIS attempt (one-shot per token).

    A ``monotonic_ns()`` read plus one dict write behind a one-shot check. No
    SQLite, no buffering, no I/O — this runs directly in front of the first
    streamed chunk reaching the user.
    """
    try:
        if token is None:
            return
        stamped_ns = monotonic_ns()
        with _LOCK:
            request_id = _TOKEN_OWNERS.get(token)
            if request_id is None:
                return
            record = (_SLOTS.get(request_id) or {}).get(token)
            if record is None or record.get("first_frame_ns") is not None:
                return
            record["first_frame_ns"] = stamped_ns
    except Exception:
        logger.debug("Unable to stamp a first wire frame", exc_info=True)


def finish_wire_attempt(token: Any, outcome: str = "") -> None:
    """Record this attempt's terminal instant and its outcome."""
    try:
        if token is None:
            return
        ended_ns = monotonic_ns()
        with _LOCK:
            request_id = _TOKEN_OWNERS.get(token)
            if request_id is None:
                return
            record = (_SLOTS.get(request_id) or {}).get(token)
            if record is None:
                return
            if record.get("end_ns") is None:
                record["end_ns"] = ended_ns
            if outcome and not record.get("attempt_outcome"):
                record["attempt_outcome"] = str(outcome)
    except Exception:
        logger.debug("Unable to finish a wire-attempt timing slot", exc_info=True)


def drain(api_request_id: Any) -> list[dict[str, Any]]:
    """Pop and return every timing record for one api_request_id, oldest first."""
    try:
        request_id = str(api_request_id or "")
        if not request_id:
            return []
        with _LOCK:
            records = _SLOTS.pop(request_id, None) or {}
            for token in records:
                _TOKEN_OWNERS.pop(token, None)
        return [dict(record) for record in records.values()]
    except Exception:
        logger.debug("Unable to drain wire-attempt timing slots", exc_info=True)
        return []


def reset_for_tests() -> None:
    """Drop all timing state (test isolation only)."""
    with _LOCK:
        _SLOTS.clear()
        _TOKEN_OWNERS.clear()
