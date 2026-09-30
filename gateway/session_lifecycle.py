"""SessionStore explicit suspension, crash-recovery markers, pruning and shared clock/id helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional

from hermes_state_ids import new_session_id

if TYPE_CHECKING:
    from gateway.session import SessionEntry, SessionSource

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


def _new_session_id(now: datetime) -> str:
    return new_session_id(now, hex_len=8)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(value) -> Optional[datetime]:
    """``datetime.fromisoformat`` that returns None for empty/malformed input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# Auto-continue freshness window (1 hour) after the ``resume_pending`` mark; ``gateway/run.py``
# bridges config.yaml ``agent.gateway_auto_continue_freshness`` into the env var at startup.
_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT = 60 * 60


def auto_continue_freshness_window() -> float:
    """Resume-scheduler freshness window; stale automation never discards the transcript."""
    raw = os.environ.get("HERMES_AUTO_CONTINUE_FRESHNESS")
    try:
        return float(raw) if raw else float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)
    except (TypeError, ValueError):
        return float(_AUTO_CONTINUE_FRESHNESS_SECS_DEFAULT)


class SessionLifecycleMixin:
    """SessionStore explicit boundaries and crash-recovery markers."""

    def _is_session_ended_in_db(self, session_id: str) -> bool:
        """True iff state.db has this session with a non-null end_reason (same staleness test as
        ``_prune_stale_sessions_locked``; no DB/row or DB error -> False). Lets routing self-heal a
        session ended while the gateway stays alive. Store resolved from the owning profile.

        Used by ``get_or_create_session`` to self-heal at routing time: ``_prune_stale_sessions_locked``
        only runs at startup, so a session ended in the DB while the gateway stays alive (any path that
        finalizes the row without clearing sessions.json) would otherwise be reused as a live routing key
        and silently swallow every subsequent message until the next restart (#54878 — the live-gateway
        variant of #52804/FM9). DB errors are non-fatal — never block routing on a failed lookup.
        The store is resolved from the row's owning profile rather than the ambient scope: an unscoped
        background writer keeps its own copy of the same session, and comparing against that copy reports a
        live session as ended (#66887).
        """
        db = self._db_for_session_id(session_id)
        if not db or not session_id:
            return False
        try:
            row = db.get_session(session_id)
        except Exception:
            return False
        return bool(row is not None and row.get("end_reason") is not None)

    def _route_reset_reason(self, entry: SessionEntry) -> Optional[str]:
        """Only explicit suspension replaces a routed conversation; time never does."""
        return "suspended" if entry.suspended else None

    def _update_entry(self, session_key: str, mutate) -> bool:
        """Apply ``mutate(entry)`` under ``_lock`` and full-save; False when the entry is missing
        or *mutate* returned False (nothing to persist)."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or mutate(entry) is False:
                return False
            self._save()
            return True

    def _update_all_entries_locked(self, mutate) -> int:
        """Apply ``mutate(entry) -> bool`` to every entry under ``_lock``; save once if any
        returned True. Returns the count that did."""
        with self._lock:
            self._ensure_loaded_locked()
            changed = sum(1 for entry in self._entries.values() if mutate(entry))
            if changed:
                self._save()
        return changed

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session suspended so it auto-resets on next access (/stop). True if it existed.

        Used by ``/stop`` to prevent stuck sessions from being resumed after a gateway restart (#7536).
        """
        return self._update_entry(session_key, lambda e: setattr(e, "suspended", True))

    def _set_turn_marker_locked(self, session_key: str, entry: SessionEntry, token, started_at) -> None:
        """Persist the active-turn pair BEFORE publishing it in memory, so a failed write can
        neither leak an unowned token nor drop a live one. Lock held."""
        candidate = entry.to_dict()
        candidate["active_turn_token"] = token
        candidate["active_turn_started_at"] = _iso(started_at)
        if started_at is not None:
            # Keeps the legacy 120s startup heuristic working for an older binary during a rolling
            # downgrade/upgrade window.
            candidate["updated_at"] = started_at.isoformat()
        self._save_entry(session_key, entry_data=candidate, lock_held=True)
        entry.active_turn_token = token
        entry.active_turn_started_at = started_at
        if started_at is not None:
            entry.updated_at = started_at

    def mark_turn_active(self, session_key: str) -> Optional[str]:
        """Persist exact ownership of the running agent turn; returns the opaque token for
        :meth:`clear_turn_active`. Re-marking replaces the previous token so a stale asynchronous
        unwind cannot clear a newer turn."""
        token = uuid.uuid4().hex
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None:
                return None
            self._set_turn_marker_locked(session_key, entry, token, _now())
        return token

    def clear_turn_active(self, session_key: str, token: str) -> bool:
        """Compare-and-swap clear an active-turn marker; ``False`` when the entry disappeared or a
        newer turn owns it."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or entry.active_turn_token != token:
                return False
            self._set_turn_marker_locked(session_key, entry, None, None)
        return True

    def _begin_active_turn_locked(
        self, session_key: str, entry: SessionEntry, turn_id: str, boot_id: str,
        resume_count: int, *, origin_session_id: Optional[str] = None,
        origin_owner: Optional[str] = None, process_followup: Optional[dict] = None,
    ) -> None:
        """Persist one active-turn record after the caller establishes ownership. Lock held."""
        record = {
            "turn_id": turn_id,
            "boot_id": boot_id,
            "status": "resuming" if resume_count else "running",
            "started_at": _now().isoformat(),
            "resume_count": resume_count,
        }
        if origin_session_id and origin_owner:
            record.update({
                "recovery_version": 1,
                "origin_session_id": origin_session_id,
                "execution_session_id": entry.session_id,
                "origin_owner": origin_owner,
                "origin_row_id": None,
            })
        if process_followup:
            record["process_followup"] = dict(process_followup)
        prior = entry.active_turn
        if resume_count and prior and prior.get("turn_id") == turn_id:
            for key in (
                "recovery_version", "origin_session_id", "execution_session_id",
                "origin_owner", "origin_row_id", "checkpoint",
                "failure_retryable", "failure_reason",
            ):
                if key in prior:
                    record[key] = prior[key]
        if (resume_count and prior and prior.get("turn_id") == turn_id
                and prior.get("failure_retryable") is True):
            # A retry can itself crash after executing tools. Retain its lineage
            # so startup checks that attempt's transcript before another replay.
            record["failure_retryable"] = True
            record["failure_reason"] = prior.get("failure_reason")
        candidate = entry.to_dict()
        candidate["active_turn"] = record
        self._save_entry(session_key, entry_data=candidate, lock_held=True)
        entry.active_turn = record

    def begin_active_turn(
        self, session_key: str, turn_id: str, boot_id: str, resume_count: int = 0,
        *, process_followup: Optional[dict] = None,
        origin_session_id: Optional[str] = None, origin_owner: Optional[str] = None,
    ) -> bool:
        """Persist the agent turn identity before dispatch without publishing a failed write."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None:
                return False
            self._begin_active_turn_locked(
                session_key, entry, turn_id, boot_id, resume_count,
                origin_session_id=origin_session_id, origin_owner=origin_owner,
                process_followup=process_followup,
            )
            return True

    def claim_resume_active_turn(
        self,
        session_key: str,
        turn_id: str,
        boot_id: str,
        resume_count: int,
        *,
        expected_session_id: str,
        expected_turn_id: Optional[str],
        expected_resume_count: Optional[int],
        expected_status: Optional[str],
        expected_identity: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Atomically claim an unchanged, unsuspended restart-resume candidate.

        ``expected_turn_id=None`` denotes the legacy ``resume_pending`` route and requires the
        active-turn record to remain exactly absent. Normal inbound turns intentionally continue
        to use unconditional :meth:`begin_active_turn`.
        """
        with self._lock:
            entry = self._entry_locked(session_key)
            if (
                entry is None
                or entry.suspended
                or entry.session_id != expected_session_id
            ):
                return False
            prior = entry.active_turn
            if isinstance(prior, dict) and self.recovery_is_quarantined(session_key, prior):
                return False
            if expected_turn_id is None:
                if prior is not None or not entry.resume_pending:
                    return False
            elif (
                not isinstance(prior, dict)
                or prior.get("turn_id") != expected_turn_id
                or prior.get("resume_count") != expected_resume_count
                or prior.get("status") != expected_status
            ):
                return False
            if expected_identity is not None and not self._publication_matches(
                session_key, entry, expected_identity,
            ):
                return False
            now = _now()
            record = {
                **(prior or {}),
                "turn_id": turn_id,
                "boot_id": boot_id,
                "status": "resuming",
                "started_at": now.isoformat(),
                "resume_count": resume_count,
                "execution_session_id": entry.session_id,
                "dispatch_token": uuid.uuid4().hex,
                "dispatch_state": "queued",
            }
            record.pop("retry_not_before", None)
            candidate = entry.to_dict()
            candidate["active_turn"] = record
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = record
            return True

    @staticmethod
    def _attempt_identity(record):
        return tuple(record.get(key) for key in (
            "recovery_version", "origin_session_id", "execution_session_id", "origin_owner",
            "turn_id", "resume_count", "boot_id", "dispatch_token",
        ))

    def _recovery_fence_path(self, session_key: str, record: Dict[str, Any]):
        # Keep the fence scoped to the exact attempt and the owning SessionStore.
        payload = json.dumps([session_key, self._attempt_identity(record)], separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self.sessions_dir / "recovery_cancellations" / f"{digest}.json"

    def recovery_is_quarantined(self, session_key: str, record: Dict[str, Any]) -> bool:
        if (session_key, self._attempt_identity(record)) in getattr(self, "_recovery_quarantine", set()):
            return True
        try:
            self._recovery_fence_path(session_key, record).stat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True  # An unreadable cancellation store cannot authorize replay.

    def _publication_matches(self, session_key, entry, expected_identity):
        record = entry.active_turn
        if self.recovery_is_quarantined(session_key, record):
            return False
        if expected_identity is None:
            return True  # Legacy callers; production settlement supplies the complete receipt.
        return bool(
            not entry.suspended
            and ("recovery_version" not in expected_identity
                 or entry.session_id == expected_identity.get("execution_session_id"))
            and self._attempt_identity(record) == self._attempt_identity(expected_identity)
            and record.get("status") == expected_identity.get("status")
            and record.get("dispatch_state") == expected_identity.get("dispatch_state")
        )

    @staticmethod
    def _resume_marker_matches_record(
        entry: SessionEntry, marker: Any, *, phase: str,
    ) -> bool:
        """Strict attempt-identity predicate shared by every dispatch boundary."""
        if not isinstance(marker, dict) or entry.suspended:
            return False
        record = entry.active_turn
        if not isinstance(record, dict) or (type(record.get("recovery_version")) is not int or record.get("recovery_version") != 1):
            return False
        required_strings = (
            "turn_id", "origin_session_id", "execution_session_id", "origin_owner",
            "boot_id", "dispatch_token",
        )
        if any(not isinstance(marker.get(k), str) or not marker[k] for k in required_strings):
            return False
        if type(marker.get("resume_count")) is not int:
            return False
        if (type(marker.get("recovery_version")) is not int or marker.get("recovery_version") != 1):
            return False
        expected = {
            "turn_id": record.get("turn_id"),
            "origin_session_id": record.get("origin_session_id"),
            "execution_session_id": record.get("execution_session_id"),
            "origin_owner": record.get("origin_owner"),
            "boot_id": record.get("boot_id"),
            "dispatch_token": record.get("dispatch_token"),
            "resume_count": record.get("resume_count"),
        }
        if any(marker.get(k) != value for k, value in expected.items()):
            return False
        if entry.session_id != marker["execution_session_id"]:
            return False
        if phase == "queued":
            return record.get("status") == "resuming" and record.get("dispatch_state") == "queued"
        if phase == "executing":
            return record.get("status") == "resuming" and record.get("dispatch_state") == "executing"
        raise ValueError(f"invalid resume validation phase: {phase}")

    def resume_owner_matches(self, session_key: str, marker: Any, *, phase: str = "queued") -> bool:
        """Read-only validation of one recovery event; never creates or heals a route."""
        with self._lock:
            entry = self._entry_locked(session_key)
            return bool(entry and isinstance(entry.active_turn, dict)
                        and not self.recovery_is_quarantined(session_key, entry.active_turn)
                        and self._resume_marker_matches_record(entry, marker, phase=phase))

    def consume_resume_dispatch(self, session_key: str, marker: Any) -> bool:
        """Durably consume one queued recovery ticket immediately before model entry."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not self._resume_marker_matches_record(entry, marker, phase="queued"):
                return False
            if self.recovery_is_quarantined(session_key, entry.active_turn):
                return False
            record = {**entry.active_turn, "dispatch_state": "executing"}
            candidate = entry.to_dict()
            candidate["active_turn"] = record
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = record
            return True

    def cancel_active_turn_recovery(
        self, session_key: str, *, expected_session_id: Optional[str], reason: str,
        expected_identity: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Persist sticky cancellation for pending or executing same-turn recovery."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not isinstance(entry.active_turn, dict):
                return False
            if expected_session_id is not None and entry.session_id != expected_session_id:
                return False
            record = entry.active_turn
            if (expected_identity is not None
                    and self._attempt_identity(record) != self._attempt_identity(expected_identity)):
                return False
            if record.get("status") not in {"running", "interrupted", "resuming", "retry_wait"}:
                return False
            record = {
                **record,
                "status": "blocked",
                "blocked_reason": "user_cancelled",
                "cancel_reason": reason,
                "cancelled_at": _now().isoformat(),
            }
            record.pop("retry_not_before", None)
            record.pop("dispatch_token", None)
            record.pop("dispatch_state", None)
            candidate = entry.to_dict()
            candidate["active_turn"] = record
            candidate["resume_pending"] = False
            candidate["resume_reason"] = None
            candidate["last_resume_marked_at"] = None
            try:
                from utils import atomic_json_write
                fence = self._recovery_fence_path(session_key, entry.active_turn)
                try:
                    fence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    atomic_json_write(fence, {"cancel_reason": reason}, mode=0o600, fsync_dir=True)
                except OSError:
                    # An index write can still persist cancellation when the sidecar is unavailable.
                    logger.warning("Cannot write recovery cancellation fence for %s", session_key, exc_info=True)
                self._save_entry(session_key, entry_data=candidate, lock_held=True)
            except Exception:
                # Durable state remains unchanged. Fence the exact pending ticket locally,
                # including copies already admitted to adapter queues, before propagating.
                if not hasattr(self, "_recovery_quarantine"):
                    self._recovery_quarantine = set()
                self._recovery_quarantine.add((session_key, self._attempt_identity(entry.active_turn)))
                raise
            entry.active_turn = record
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            return True

    def seal_active_turn_evidence(
        self, session_key: str, turn_id: str, *, expected_resume_count: int,
        origin_row_id: int, checkpoint: Dict[str, Any],
        expected_dispatch_token: Optional[str] = None,
        expected_identity: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Publish a raw-evidence checkpoint only while the exact attempt still owns the turn."""
        with self._lock:
            entry = self._entry_locked(session_key)
            record = entry.active_turn if entry is not None else None
            if not isinstance(record, dict):
                return False
            if not self._publication_matches(session_key, entry, expected_identity):
                return False
            if record.get("turn_id") != turn_id or record.get("resume_count") != expected_resume_count:
                return False
            if expected_dispatch_token is not None and record.get("dispatch_token") != expected_dispatch_token:
                return False
            if record.get("status") == "blocked":
                return False
            updated = {**record, "origin_row_id": origin_row_id, "checkpoint": dict(checkpoint)}
            candidate = entry.to_dict()
            candidate["active_turn"] = updated
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = updated
            return True

    def mark_active_turn_interrupted(self, session_key: str, reason: str) -> bool:
        """Keep a gateway-interrupted turn eligible across a cooperative shutdown."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not entry.active_turn:
                return False
            # Shutdown may observe a worker after its recovery state has settled.
            # Queued tickets have not executed; preserve their restart replacement semantics.
            if entry.active_turn.get("status") not in {"running", "interrupted", "resuming"}:
                return False
            if entry.active_turn.get("status") == "resuming" and entry.active_turn.get("dispatch_state") == "queued":
                return False
            if self.recovery_is_quarantined(session_key, entry.active_turn):
                return False
            record = {**entry.active_turn, "status": "interrupted",
                      "interrupted_reason": reason, "interrupted_at": _now().isoformat()}
            candidate = entry.to_dict()
            candidate["active_turn"] = record
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = record
            return True

    def mark_active_turn_recovery(
        self, session_key: str, turn_id: str, *, expected_resume_count: int,
        status: str, failure_reason: str, retry_delay: float = 0.0,
        blocked_reason: Optional[str] = None,
        expected_dispatch_token: Optional[str] = None,
        expected_identity: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """CAS a structured failed attempt into ``retry_wait`` or ``blocked`` state."""
        if status not in {"retry_wait", "blocked"}:
            raise ValueError(f"invalid active-turn recovery status: {status}")
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not entry.active_turn:
                return False
            if not self._publication_matches(session_key, entry, expected_identity):
                return False
            if entry.active_turn.get("turn_id") != turn_id:
                return False
            if entry.active_turn.get("resume_count") != expected_resume_count:
                return False
            if (
                expected_dispatch_token is not None
                and entry.active_turn.get("dispatch_token") != expected_dispatch_token
            ):
                return False
            if entry.active_turn.get("status") == "blocked":
                return False
            now = _now()
            record = {
                **entry.active_turn,
                "status": status,
                "failure_reason": failure_reason,
                "failure_retryable": True,
                "failed_at": now.isoformat(),
            }
            record.pop("retry_not_before", None)
            record.pop("blocked_reason", None)
            record.pop("dispatch_token", None)
            record.pop("dispatch_state", None)
            if status == "retry_wait":
                record["retry_not_before"] = (
                    now + timedelta(seconds=max(0.0, float(retry_delay)))
                ).isoformat()
            else:
                record["blocked_reason"] = blocked_reason or "recovery_blocked"
            candidate = entry.to_dict()
            candidate["active_turn"] = record
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = record
            return True

    def finish_active_turn(
        self, session_key: str, turn_id: Optional[str] = None, *,
        force: bool = False, turn_interrupted: bool = False,
        expected_resume_count: Optional[int] = None,
        expected_dispatch_token: Optional[str] = None,
    ) -> bool:
        """CAS-retire a turn; preserve a gateway-interrupted one for the next boot."""
        with self._lock:
            entry = self._entry_locked(session_key)
            if entry is None or not entry.active_turn:
                return False
            if turn_id is not None and entry.active_turn.get("turn_id") != turn_id:
                return False
            if (
                expected_resume_count is not None
                and entry.active_turn.get("resume_count") != expected_resume_count
            ):
                return False
            if (
                expected_dispatch_token is not None
                and entry.active_turn.get("dispatch_token") != expected_dispatch_token
            ):
                return False
            if entry.active_turn.get("status") == "blocked" and not force:
                return False
            if not force and turn_interrupted and entry.active_turn.get("status") == "interrupted":
                return False
            candidate = entry.to_dict()
            candidate.pop("active_turn", None)
            self._save_entry(session_key, entry_data=candidate, lock_held=True)
            entry.active_turn = None
            return True

    def recover_interrupted_turns(self, max_age_seconds: int = 60 * 60) -> int:
        """Promote crash-left turn markers into ``resume_pending`` (unclean startup only).
        Old/invalid markers are cleared without resuming; suspended sessions are never re-armed.
        Returns the number of newly promoted sessions."""
        now = _now()
        max_age = timedelta(seconds=max(0, max_age_seconds))
        promoted = 0

        def _promote(entry: SessionEntry) -> bool:
            nonlocal promoted
            if not entry.active_turn_token:
                return False
            started_at = entry.active_turn_started_at
            try:
                marker_is_stale = started_at is None or (
                    max_age_seconds > 0 and now - started_at > max_age
                )
            except TypeError:
                # Mixed aware/naive timestamps: clear rather than risk an unsafe old resume.
                marker_is_stale = True
            if not marker_is_stale and not entry.suspended:
                if entry.resume_pending:
                    # A drain-timeout marker is more specific; keep it.
                    if entry.last_resume_marked_at is None:
                        entry.last_resume_marked_at = now
                else:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.last_resume_marked_at = now  # freshness starts at discovery
                    promoted += 1
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True

        self._update_all_entries_locked(_promote)
        return promoted

    def discard_active_turn_markers(self) -> int:
        """Clear orphan turn markers after a verified clean shutdown."""
        def _discard(entry: SessionEntry) -> bool:
            if not entry.active_turn_token and entry.active_turn_started_at is None:
                return False
            entry.active_turn_token = None
            entry.active_turn_started_at = None
            return True
        return self._update_all_entries_locked(_discard)

    def mark_resume_pending(self, session_key: str, reason: str = "restart_timeout") -> bool:
        """Mark a session resumable after a restart interruption (keeps the session_id/transcript,
        unlike ``suspend_session``). True if marked."""
        def _apply(entry: SessionEntry):
            if entry.suspended:  # never override an explicit ``suspended`` (hard forced-wipe)
                return False
            entry.resume_pending = True
            entry.resume_reason = reason
            entry.last_resume_marked_at = _now()
        return self._update_entry(session_key, _apply)

    def clear_resume_pending(self, session_key: str, *, expected_turn_id: Optional[str] = None) -> bool:
        """Clear the resume-pending flag after a successful resumed turn; True if cleared."""
        def _apply(entry: SessionEntry):
            if (expected_turn_id is not None and entry.active_turn
                    and entry.active_turn.get("turn_id") != expected_turn_id):
                return False
            if not entry.resume_pending:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
        return self._update_entry(session_key, _apply)

    def prune_old_entries(self, max_age_days: int) -> int:
        """Drop routing entries idle (by ``updated_at``) for more than max_age_days; suspended
        entries and entries with active background processes are kept. Only the key -> session_id
        mapping is dropped (the transcript stays). ``max_age_days <= 0`` disables. Returns count."""
        if max_age_days is None or max_age_days <= 0:
            return 0
        cutoff = _now() - timedelta(days=max_age_days)
        with self._lock:
            self._ensure_loaded_locked()
            removed_keys = [
                key for key, entry in list(self._entries.items())
                if not entry.suspended
                # The callback is keyed by session_key, NOT session_id.
                and not self._has_active_processes_safe(entry.session_key, context="prune")
                and entry.updated_at < cutoff
            ]
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()
        if removed_keys:
            logger.info("SessionStore pruned %d entries older than %d days",
                        len(removed_keys), max_age_days)
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """Mark sessions active within *max_age_seconds* as ``resume_pending`` after a crash/fast
        restart (already-pending and suspended entries are skipped). Returns the number marked.

        Called on gateway startup after a crash or fast restart to preserve in-flight sessions instead of
        destroying their conversation history (#7536). Only marks sessions updated within *max_age_seconds*
        to avoid touching long-idle sessions. Sets ``resume_pending=True`` so the next incoming message on
        the same session_key auto-resumes from the existing transcript.
        """
        cutoff = _now() - timedelta(seconds=max_age_seconds)

        def _mark(entry: SessionEntry) -> bool:
            if entry.resume_pending or entry.suspended or entry.updated_at < cutoff:
                return False
            entry.resume_pending = True
            entry.resume_reason = "restart_interrupted"
            entry.last_resume_marked_at = _now()
            return True
        return self._update_all_entries_locked(_mark)
