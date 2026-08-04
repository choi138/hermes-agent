"""Relay-independent recorder for Hermes local observation samples (R3-b).

Shared metrics never required the optional ``nemo_relay`` plugin, but durable
recording did: ``relay_shared_metrics.observe_lifecycle`` routes through
``_get_runtime()``, and ``_Runtime.__init__`` raises "Hermes core Relay runtime
is unavailable" when ``relay_runtime.get_runtime()`` yields None — which
``_get_runtime`` swallows into a per-profile ``_RUNTIME_FAILED`` sentinel. On a
host with no importable Relay wheel, every metric therefore stopped at that
sentinel. This module writes to the same local SQLite store WITHOUT any Relay
host, so R3's raw samples land on exactly the machines that run baselines.

It owns:
  * ``hermes.turn.first_useful_result_ms``
  * the ``hermes.model_call.ttft_ms`` / ``.duration_ms`` flush, drained from
    ``agent.model_call_timing`` on the EXISTING ``post_api_request`` and
    ``api_request_error`` payloads — so no new hook kwarg is introduced (a new
    kwarg would be forwarded verbatim into any outbound webhook subscribed to
    that hook, shipping the R3 latency data off the machine);
  * ``hermes.model_call.retry_attempt`` for the classified-error path;
  * the Relay-independent retention trigger.

Rows are BUFFERED per turn and flushed with ONE ``record_observations()`` call,
because ``post_api_request`` fires inside the multi-round agentic loop and each
flush is a fresh connect + BEGIN + INSERT + COMMIT + close against a
rollback-journal database.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import Any

from .shared_metrics_contract import (
    COMPRESSION_AUX_DURATION_METRIC,
    COMPRESSION_DURATION_METRIC,
    COMPRESSION_TOKENS_AFTER_METRIC,
    COMPRESSION_TOKENS_BEFORE_METRIC,
    FALLBACK_ACTIVATION_METRIC,
    FIRST_USEFUL_RESULT_METRIC,
    MODEL_CALL_DURATION_METRIC,
    RETRY_ATTEMPT_METRIC,
    TTFT_METRIC,
    api_mode_family,
    execution_surface,
    first_result_kind,
    model_family,
    observation_call_role,
    provider_family,
    retry_reason,
    stream_mode,
    work_lane,
)

logger = logging.getLogger(__name__)

HANDLED_HOOKS = frozenset({
    "api_request_error",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "post_api_request",
    "post_tool_call",
    "pre_api_request",
    "pre_llm_call",
})

_MAX_TURN_SLOTS = 32
_MAX_BUFFERED_ROWS = 64

_STATE_LOCK = threading.RLock()
_STORE_LOCK = threading.RLock()
_STORES: dict[str, Any] = {}
_PRUNED_THIS_PROCESS = False


@dataclass
class _TurnSlot:
    session_id: str
    turn_id: str
    t0_ns: int
    t0_source: str
    lane: str = "unknown"
    surface: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"
    # Last known call_role for the turn, learned from drained wire-attempt
    # records. The api_request_error hook payload carries neither is_subagent
    # nor fallback_index, so this is the only in-process source that can tell a
    # fallback or delegated retry apart from a primary one WITHOUT widening the
    # published hook payload (which would let the values leave the machine).
    call_role: str = "unknown"
    first_result_recorded: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)


# (session_id, turn_id) -> slot. Insertion-ordered, LRU-evicted.
_TURNS: dict[tuple[str, str], _TurnSlot] = {}
# session_id -> rows with no turn context (fallback activations, compression).
_LOOSE: dict[str, list[dict[str, Any]]] = {}


# ── gating and store resolution ─────────────────────────────────────────


def enabled() -> bool:
    """Return whether raw local observation recording is permitted.

    Deliberately does NOT call ``relay_shared_metrics.enabled()``: that
    function pops and deactivates the profile's Relay runtime when it returns
    False, so using it as a gate here would have a side effect on the Relay
    path.
    """
    try:
        from .metrics_policy import local_observations_enabled

        return local_observations_enabled()
    except Exception:
        logger.debug("Unable to read the local-observations policy", exc_info=True)
        return False


def _store() -> Any:
    """Return a SharedMetricsStore cached per HERMES_HOME.

    The store location is derived from ``get_hermes_home()``, which can change
    through the context-local override, so the home is the correct cache key.
    """
    from hermes_constants import get_hermes_home

    key = str(get_hermes_home())
    with _STORE_LOCK:
        store = _STORES.get(key)
        if store is None:
            from .shared_metrics import SharedMetricsStore

            store = SharedMetricsStore()
            _STORES[key] = store
        return store


def _hermes_version() -> str:
    try:
        from hermes_cli import __version__

        return str(__version__ or "unknown")
    except Exception:
        return "unknown"


# ── dimension builders ──────────────────────────────────────────────────


def model_call_dimensions(
    *,
    lane: str,
    surface: str,
    provider: str,
    model: str,
    api_mode: str,
    stream: str,
    call_role: str,
    outcome: str,
) -> dict[str, str]:
    """Build the closed dimension set shared by the per-attempt model metrics."""
    from .shared_metrics_contract import MODEL_OUTCOMES

    resolved_outcome = str(outcome or "").strip().lower()
    return {
        "api_mode_family": api_mode_family(api_mode),
        "attempt_outcome": (
            resolved_outcome if resolved_outcome in MODEL_OUTCOMES else "failed"
        ),
        "call_role": observation_call_role(call_role),
        "execution_surface": execution_surface({"platform": surface}),
        "model_family": model_family({"model": model}),
        "provider_family": provider_family({"provider": provider}),
        "stream_mode": stream_mode(stream),
        "work_lane": work_lane(lane),
    }


def _call_role(kwargs: dict[str, Any]) -> str:
    if kwargs.get("is_subagent"):
        return "delegated"
    try:
        if int(kwargs.get("fallback_index") or 0) > 0:
            return "fallback"
    except Exception:
        pass
    return "primary"


# ── buffering ───────────────────────────────────────────────────────────


def buffer_rows(session_id: Any, turn_id: Any, rows: list[dict[str, Any]]) -> None:
    """Append rows to a turn's buffer, force-flushing an oversized buffer."""
    if not rows:
        return
    try:
        session = str(session_id or "")
        turn = str(turn_id or "")
        with _STATE_LOCK:
            slot = _TURNS.get((session, turn))
            if slot is not None:
                slot.rows.extend(rows)
                if len(slot.rows) <= _MAX_BUFFERED_ROWS:
                    return
                pending = slot.rows
                slot.rows = []
            else:
                loose = _LOOSE.setdefault(session, [])
                loose.extend(rows)
                if len(loose) <= _MAX_BUFFERED_ROWS:
                    return
                pending = loose
                _LOOSE[session] = []
        _write(pending)
    except Exception:
        logger.debug("Unable to buffer observation rows", exc_info=True)


def record_now(rows: list[dict[str, Any]]) -> None:
    """Write rows immediately in one transaction (low-frequency emit points)."""
    if not rows or not enabled():
        return
    _write(rows)


def _write(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        version = _hermes_version()
        payload = [
            {
                "metric_name": row.get("metric_name"),
                "dimensions": row.get("dimensions"),
                "value": row.get("value"),
                "hermes_version": row.get("hermes_version") or version,
            }
            for row in rows
        ]
        _store().record_observations(payload)
    except Exception:
        logger.debug("Unable to persist observation rows", exc_info=True)


def flush(session_id: Any = None) -> None:
    """Flush buffered rows for one session (or every session when None)."""
    try:
        session = str(session_id or "")
        pending: list[dict[str, Any]] = []
        with _STATE_LOCK:
            for key in list(_TURNS):
                if session and key[0] != session:
                    continue
                slot = _TURNS.pop(key)
                pending.extend(slot.rows)
            if session:
                pending.extend(_LOOSE.pop(session, []))
            else:
                for rows in _LOOSE.values():
                    pending.extend(rows)
                _LOOSE.clear()
        _write(pending)
    except Exception:
        logger.debug("Unable to flush observation rows", exc_info=True)


# ── per-turn state ──────────────────────────────────────────────────────


def _touch_turn(kwargs: dict[str, Any], source: str) -> _TurnSlot | None:
    session = str(kwargs.get("session_id") or "")
    turn = str(kwargs.get("turn_id") or "")
    if not session and not turn:
        return None
    platform = str(kwargs.get("platform") or "")
    with _STATE_LOCK:
        slot = _TURNS.get((session, turn))
        if slot is None:
            slot = _TurnSlot(
                session_id=session,
                turn_id=turn,
                t0_ns=monotonic_ns(),
                t0_source=source,
                lane=_resolve_lane(kwargs),
                surface=platform,
            )
            _TURNS[(session, turn)] = slot
            while len(_TURNS) > _MAX_TURN_SLOTS:
                evicted_key = next(iter(_TURNS))
                evicted = _TURNS.pop(evicted_key)
                if evicted.rows:
                    _LOOSE.setdefault(evicted.session_id, []).extend(evicted.rows)
        if platform and slot.surface in {"", "unknown"}:
            slot.surface = platform
        if kwargs.get("provider"):
            slot.provider = str(kwargs.get("provider"))
        if kwargs.get("model"):
            slot.model = str(kwargs.get("model"))
        return slot


def _resolve_lane(kwargs: dict[str, Any]) -> str:
    try:
        from . import work_lane as work_lane_module

        return work_lane_module.current_work_lane(
            platform=str(kwargs.get("platform") or ""),
            is_subagent=bool(kwargs.get("is_subagent")),
            parent_session_id=str(kwargs.get("parent_session_id") or ""),
        )
    except Exception:
        logger.debug("Unable to resolve the work lane", exc_info=True)
        return "unknown"


def _record_first_useful_result(slot: _TurnSlot, kind: str) -> None:
    if slot.first_result_recorded:
        return
    slot.first_result_recorded = True
    elapsed_ms = max(0.0, (monotonic_ns() - slot.t0_ns) / 1_000_000)
    slot.rows.append(
        {
            "metric_name": FIRST_USEFUL_RESULT_METRIC,
            "dimensions": {
                "execution_surface": execution_surface({"platform": slot.surface}),
                "first_result_kind": first_result_kind(kind),
                "model_family": model_family({"model": slot.model}),
                "provider_family": provider_family({"provider": slot.provider}),
                "work_lane": work_lane(slot.lane),
            },
            "value": elapsed_ms,
        }
    )


# ── model-call attempt flush ────────────────────────────────────────────


def _flush_wire_attempts(
    kwargs: dict[str, Any],
    slot: _TurnSlot | None,
    terminal_outcome: str,
) -> list[dict[str, Any]]:
    """Drain one api_request_id's wire attempts into buffered rows.

    Returns the rows that must be persisted NOW (an oversized buffer). The
    caller holds ``_STATE_LOCK``, so this function deliberately never writes:
    a SQLite connect/BEGIN/COMMIT under that lock would block every other
    concurrent turn's hook processing for up to the busy timeout.
    """
    from agent import model_call_timing

    records = model_call_timing.drain(kwargs.get("api_request_id"))
    if not records:
        return []
    lane = slot.lane if slot is not None else _resolve_lane(kwargs)
    surface = (
        slot.surface
        if slot is not None and slot.surface
        else str(kwargs.get("platform") or "")
    )
    rows: list[dict[str, Any]] = []
    # Only the LAST record can carry the terminal outcome; earlier records were
    # superseded physical attempts. Recording those too is what keeps failed and
    # superseded attempts out of the survivorship bias that would make a doubled
    # 429 rate read as no change at all.
    for index, record in enumerate(records):
        is_terminal = index == len(records) - 1
        outcome = record.get("attempt_outcome") or (
            terminal_outcome if is_terminal else "failed"
        )
        dimensions = model_call_dimensions(
            lane=record.get("work_lane") or lane,
            surface=surface,
            provider=record.get("provider") or str(kwargs.get("provider") or ""),
            model=record.get("model") or str(kwargs.get("model") or ""),
            api_mode=record.get("api_mode_family")
            or str(kwargs.get("api_mode") or ""),
            stream=record.get("stream_mode") or "unknown",
            call_role=record.get("call_role") or "unknown",
            outcome=outcome,
        )
        issued_ns = record.get("issued_ns")
        first_frame_ns = record.get("first_frame_ns")
        end_ns = record.get("end_ns")
        if isinstance(issued_ns, int) and isinstance(first_frame_ns, int):
            rows.append(
                {
                    "metric_name": TTFT_METRIC,
                    "dimensions": dimensions,
                    "value": max(0.0, (first_frame_ns - issued_ns) / 1_000_000),
                }
            )
        if isinstance(issued_ns, int):
            terminal_ns = end_ns if isinstance(end_ns, int) else monotonic_ns()
            rows.append(
                {
                    "metric_name": MODEL_CALL_DURATION_METRIC,
                    "dimensions": dimensions,
                    "value": max(0.0, (terminal_ns - issued_ns) / 1_000_000),
                }
            )
    learned_role = str(records[-1].get("call_role") or "").strip()
    if slot is not None:
        if learned_role:
            slot.call_role = learned_role
        slot.rows.extend(rows)
        if len(slot.rows) > _MAX_BUFFERED_ROWS:
            pending, slot.rows = slot.rows, []
            return pending
        return []
    # No turn slot (compression worker, post-eviction): buffer loose, mirroring
    # buffer_rows but without writing under the caller's lock.
    session = str(kwargs.get("session_id") or "")
    loose = _LOOSE.setdefault(session, [])
    loose.extend(rows)
    if len(loose) > _MAX_BUFFERED_ROWS:
        _LOOSE[session] = []
        return loose
    return []


# ── public emit helpers for non-hook call sites ─────────────────────────


def record_retry_attempt(
    *,
    session_id: Any,
    turn_id: Any,
    reason: Any,
    platform: Any = "",
    provider: Any = "",
    model: Any = "",
    api_mode: Any = "",
    call_role: Any = "primary",
    lane: Any = "",
) -> None:
    """Buffer one loop-level retry.

    Counts LOOP-level retries only: the one-shot TurnRetryState guards each
    ``continue`` without incrementing ``retry_count``, and codex stream
    recovery even refunds ``api_call_count``. Total PHYSICAL attempts is
    answered instead by the row count of hermes.model_call.duration_ms, which
    is emitted once per wire attempt including failures.
    """
    if not enabled():
        return
    try:
        resolved_lane = str(lane or "") or _resolve_lane(
            {"platform": platform, "session_id": session_id}
        )
        row = {
            "metric_name": RETRY_ATTEMPT_METRIC,
            "dimensions": {
                "api_mode_family": api_mode_family(api_mode),
                "call_role": observation_call_role(call_role),
                "execution_surface": execution_surface({"platform": platform}),
                "model_family": model_family({"model": model}),
                "provider_family": provider_family({"provider": provider}),
                "retry_reason": retry_reason(reason),
                "work_lane": work_lane(resolved_lane),
            },
            "value": 1.0,
        }
        buffer_rows(session_id, turn_id, [row])
    except Exception:
        logger.debug("Unable to record a retry attempt", exc_info=True)


def record_fallback_activation(
    *,
    session_id: Any,
    fallback_ordinal: Any,
    reason: Any = None,
    platform: Any = "",
    provider: Any = "",
    model: Any = "",
    api_mode: Any = "",
    call_role: Any = "primary",
    lane: Any = "",
) -> None:
    """Write one fallback chain advance immediately (rare, and never buffered).

    Structurally distinct from ``retry_attempt`` because every fallback call
    site resets ``retry_count = 0``, so retries alone undercount once a
    fallback fires.
    """
    if not enabled():
        return
    try:
        from .shared_metrics_contract import fallback_reason

        try:
            ordinal = float(int(fallback_ordinal))
        except (TypeError, ValueError):
            ordinal = 0.0
        resolved_lane = str(lane or "") or _resolve_lane({"platform": platform})
        record_now(
            [
                {
                    "metric_name": FALLBACK_ACTIVATION_METRIC,
                    "dimensions": {
                        "api_mode_family": api_mode_family(api_mode),
                        "call_role": observation_call_role(call_role),
                        "execution_surface": execution_surface({"platform": platform}),
                        "fallback_reason": fallback_reason(reason),
                        "model_family": model_family({"model": model}),
                        "provider_family": provider_family({"provider": provider}),
                        "work_lane": work_lane(resolved_lane),
                    },
                    "value": max(0.0, ordinal),
                }
            ]
        )
        del session_id
    except Exception:
        logger.debug("Unable to record a fallback activation", exc_info=True)


def record_compression_attempt(
    *,
    kind: Any,
    outcome: Any,
    trigger: Any,
    lane: Any = "",
    platform: Any = "",
    duration_ms: Any = None,
    aux_duration_ms: Any = None,
    tokens_before: Any = None,
    tokens_after: Any = None,
) -> None:
    """Write one compression attempt's rows in ONE transaction.

    Duration and aux duration are separate rows on purpose: on the micro path
    the auxiliary summariser call sits INSIDE the measured window and dominates
    it, so a single number cannot tell "compression got slower" apart from "the
    summariser provider got slower".
    """
    if not enabled():
        return
    try:
        from .shared_metrics_contract import compression_kind, compression_outcome
        from .shared_metrics_contract import compression_trigger

        dimensions = {
            "compression_kind": compression_kind(kind),
            "compression_outcome": compression_outcome(outcome),
            "compression_trigger": compression_trigger(trigger),
            "execution_surface": execution_surface({"platform": platform}),
            "work_lane": work_lane(lane),
        }
        rows: list[dict[str, Any]] = []
        for metric, value in (
            (COMPRESSION_DURATION_METRIC, duration_ms),
            (COMPRESSION_AUX_DURATION_METRIC, aux_duration_ms),
            (COMPRESSION_TOKENS_BEFORE_METRIC, tokens_before),
            (COMPRESSION_TOKENS_AFTER_METRIC, tokens_after),
        ):
            if value is None:
                continue
            rows.append(
                {
                    "metric_name": metric,
                    "dimensions": dict(dimensions),
                    "value": float(value),
                }
            )
        record_now(rows)
    except Exception:
        logger.debug("Unable to record compression observations", exc_info=True)


# ── lifecycle observer ──────────────────────────────────────────────────


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Record local observation samples for one Hermes lifecycle event."""
    if hook_name not in HANDLED_HOOKS or not enabled():
        return
    if hook_name == "pre_llm_call":
        _touch_turn(kwargs, "pre_llm_call")
        return
    if hook_name == "pre_api_request":
        _touch_turn(kwargs, "pre_api_request")
        return
    if hook_name == "post_tool_call":
        # Restricted to a SUCCESSFUL tool result: blocked and errored calls also
        # emit post_tool_call, so an unrestricted trigger would score a
        # policy-denied write as the turn's first useful result.
        if str(kwargs.get("status") or "").strip().lower() != "ok":
            return
        with _STATE_LOCK:
            slot = _TURNS.get(
                (
                    str(kwargs.get("session_id") or ""),
                    str(kwargs.get("turn_id") or ""),
                )
            )
            if slot is not None:
                _record_first_useful_result(slot, "tool_result")
        return
    if hook_name == "post_api_request":
        with _STATE_LOCK:
            slot = _TURNS.get(
                (
                    str(kwargs.get("session_id") or ""),
                    str(kwargs.get("turn_id") or ""),
                )
            )
            pending = _flush_wire_attempts(kwargs, slot, "success")
            if slot is not None:
                try:
                    content_chars = int(kwargs.get("assistant_content_chars") or 0)
                except (TypeError, ValueError):
                    content_chars = 0
                if content_chars > 0:
                    _record_first_useful_result(slot, "assistant_text")
        # Outside the lock on purpose: see _flush_wire_attempts.
        _write(pending)
        return
    if hook_name == "api_request_error":
        error = kwargs.get("error")
        error_type = (
            str(error.get("type") or "") if isinstance(error, dict) else ""
        )
        outcome = (
            "cancelled" if error_type == "InterruptedError" else "failed"
        )
        with _STATE_LOCK:
            slot = _TURNS.get(
                (
                    str(kwargs.get("session_id") or ""),
                    str(kwargs.get("turn_id") or ""),
                )
            )
            pending = _flush_wire_attempts(kwargs, slot, outcome)
            # Read the turn's resolved lane/call_role while still holding the
            # lock. The hook payload carries neither, and widening it would send
            # the values off-machine through any subscribed outbound webhook.
            slot_lane = slot.lane if slot is not None else ""
            slot_role = slot.call_role if slot is not None else ""
        # Outside the lock on purpose: see _flush_wire_attempts.
        _write(pending)
        if kwargs.get("retryable") is not False:
            resolved_role = slot_role if slot_role and slot_role != "unknown" else ""
            record_retry_attempt(
                session_id=kwargs.get("session_id"),
                turn_id=kwargs.get("turn_id"),
                reason=kwargs.get("reason"),
                platform=kwargs.get("platform"),
                provider=kwargs.get("provider"),
                model=kwargs.get("model"),
                api_mode=kwargs.get("api_mode"),
                call_role=resolved_role or _call_role(kwargs),
                lane=slot_lane,
            )
        return
    # on_session_end / on_session_finalize / on_session_reset
    flush(kwargs.get("session_id"))
    if hook_name in {"on_session_end", "on_session_finalize"}:
        _maybe_prune()


def _maybe_prune() -> None:
    """Trigger the Relay-independent retention pass at most once per process.

    The once-per-UTC-day ``telemetry_state`` guard inside
    ``prune_observation_samples`` handles the cross-process case. This trigger
    is mandatory rather than redundant: ``_prune_expired_history`` is only
    reachable through ``_Runtime._export()``, which does not exist on a host
    without an importable Relay wheel.
    """
    global _PRUNED_THIS_PROCESS
    with _STORE_LOCK:
        if _PRUNED_THIS_PROCESS:
            return
        # Latch only on a completed attempt: latching first would mean one
        # transient SQLITE_BUSY permanently disables retention for the process.
        # A concurrent duplicate is harmless — the once-per-UTC-day claim inside
        # prune_observation_samples is the real guard.
    try:
        _store().prune_observation_samples()
    except Exception:
        logger.debug("Unable to prune observation samples", exc_info=True)
        return
    with _STORE_LOCK:
        _PRUNED_THIS_PROCESS = True


def _reset_for_tests() -> None:
    """Drop all recorder state (test isolation only)."""
    global _PRUNED_THIS_PROCESS
    with _STATE_LOCK:
        _TURNS.clear()
        _LOOSE.clear()
    with _STORE_LOCK:
        _STORES.clear()
        _PRUNED_THIS_PROCESS = False
