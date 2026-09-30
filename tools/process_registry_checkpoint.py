"""Running-process checkpoint persistence and PID-safe recovery."""

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.process_registry")


def _read_checkpoint(path):
    """Versioned SSH rows survive an older reader rewriting processes.json."""
    entries = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError('Invalid process checkpoint')
    sidecar = path.with_name(path.name + '.ssh-v2')
    if sidecar.exists():
        remote = json.loads(sidecar.read_text())
        if not isinstance(remote, list) or any(not isinstance(row, dict) for row in remote):
            raise ValueError('Invalid SSH checkpoint')
        entries = [row for row in entries if not row.get('remote_root')] + remote
    return entries


def _write_checkpoint_files(path, entries):
    from utils import atomic_json_write
    # Authoritative for SSH rows, including the empty set after completion.
    sidecar = path.with_name(path.name + '.ssh-v2')
    previous = [row for row in _read_checkpoint(path) if row.get('remote_root')]
    atomic_json_write(sidecar, [row for row in entries if row.get('remote_root')])
    try:
        atomic_json_write(path, entries)
    except Exception:
        # A known failed pre-dispatch intent must not survive in the authoritative sidecar.
        atomic_json_write(sidecar, previous)
        raise


def _legacy_profile_home(registry, entry):
    """Adopt only a same-start local PID whose inherited execution home is known.

    Old global writers could copy rows into several profile checkpoints: the file's
    location alone is not ownership. The live child's execution environment is.
    """
    if entry.get('pid_scope', 'host') != 'host' or not entry.get('pid'):
        return None
    pid = entry['pid']
    if not registry._host_pid_is_ours(pid, entry.get('host_start_time')):
        return None
    try:
        import psutil
        from pathlib import Path
        home = psutil.Process(pid).environ().get('HERMES_HOME')
        if home:
            resolved = Path(os.path.expandvars(home)).expanduser().resolve()
            # A named runtime can inherit the default gateway environment. Never
            # adopt its row into that default home just because the PID is alive.
            key = entry.get('session_key') or ''
            if key.startswith('agent:'):
                from gateway.session import profile_from_session_key_namespace
                from hermes_constants import profile_name_for_home
                expected = profile_from_session_key_namespace(key.split(':', 2)[1])
                actual = profile_name_for_home(resolved) or 'default'
                if expected != actual:
                    return None
            return str(resolved)
    except (OSError, psutil.Error) as exc:
        from tools.environments.ssh_process import safe_error
        logger.warning('Legacy process ownership probe failed for PID %s: %s', pid, safe_error(exc))
    return None


class ProcessCheckpointMixin:
    # ----- Checkpoint (crash recovery) -----

    def retry_checkpoint_recovery(self):
        """Revisit owners skipped during an overlapping restart, without resuming commands."""
        from tools.process_registry import _checkpoint_path
        path = str(_checkpoint_path())
        waiting = getattr(self, '_checkpoint_recovery_waiting', {})
        if not waiting.get(path):
            return 0
        retry_at = getattr(self, '_checkpoint_recovery_retry_at', {})
        if time.monotonic() < retry_at.get(path, 0):
            return 0
        retry_at[path] = time.monotonic() + 30
        self._checkpoint_recovery_retry_at = retry_at
        return self.recover_from_checkpoint()

    def _write_checkpoint(self, extra_entries: Optional[List[Dict[str, Any]]] = None, *, strict=False):
        """Serialize snapshot plus replacement so an older writer cannot erase newer state."""
        with self._checkpoint_write_lock:
            self._write_checkpoint_serialized(extra_entries, strict=strict)

    def _write_checkpoint_serialized(self, extra_entries=None, *, strict=False):
        from tools.process_registry import _checkpoint_path, _CHECKPOINT_FIELDS

        try:
            from hermes_constants import get_hermes_home
            home = str(get_hermes_home())
            path = _checkpoint_path()
            with self._lock:
                entries = []
                for s in self._running.values():
                    if not s.profile_home:
                        s.profile_home = s.remote_connection.get('profile_home') or ''
                        if not s.profile_home:
                            logger.warning('Process %s has no profile ownership; not checkpointing under %s', s.id, home)
                            continue
                    if s.profile_home != home:
                        continue
                    if s.exited:
                        continue
                    # Backfill the start time so recovery can detect PID recycling
                    # even for sessions spawned before this field existed.
                    if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                        s.host_start_time = self._safe_host_start_time(s.pid)
                    entry = {"session_id": s.id, **{f: getattr(s, f) for f in _CHECKPOINT_FIELDS}}
                    entry['registry_owner'] = [os.getpid(), self._safe_host_start_time(os.getpid())]
                    # Redact inline credentials before persisting (~/.hermes/processes.json).
                    # Recovery uses command only for display (adoption re-validates the
                    # PID, never re-runs it), so masking is lossless.
                    # See #77484.
                    entry["command"] = redact_sensitive_text(s.command, code_file=True)
                    entry["owner_task_id"] = s.owner_task_id or s.task_id
                    from tools.process_registry_followups import scrub_payload
                    entries.append(scrub_payload(entry))
                retained = getattr(self, "_unresolved_remote_entries", {}).get(str(_checkpoint_path()), [])
                extra_entries = [*retained, *(extra_entries or [])]
                if extra_entries:
                    tracked_ids = {item.get("session_id") for item in entries}
                    for item in extra_entries:
                        if item.get("session_id") not in tracked_ids:
                            entries.append(item)
                            tracked_ids.add(item.get("session_id"))
            from utils import atomic_json_write
            # SQLite provides a bounded, cross-platform inter-process write lock.
            # The JSON remains the recovery format; never replace another writer's rows.
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path) + '.lock.db', timeout=5)
            try:
                conn.execute('BEGIN IMMEDIATE')
                existing = _read_checkpoint(path)
                if not isinstance(existing, list):
                    raise ValueError('Invalid process checkpoint')
                known_by_path = getattr(self, '_checkpoint_known_ids', {})
                known = known_by_path.get(str(path), set())
                current = {entry['session_id']: entry for entry in entries}
                merged = {entry['session_id']: entry for entry in existing
                          if entry['session_id'] not in known}
                merged.update(current)
                _write_checkpoint_files(path, list(merged.values()))
                known_by_path[str(path)] = set(current)
                self._checkpoint_known_ids = known_by_path
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to write checkpoint file: %s", e, exc_info=True)
            if strict:
                raise

    def recover_from_checkpoint(self) -> int:
        """On gateway startup, probe PIDs from the checkpoint file; returns how many
        were recovered as detached sessions."""
        from tools.process_registry import (
            ProcessSession, _CHECKPOINT_FIELDS, _checkpoint_path,
            _CHECKPOINT_DEFAULTS, _WATCHER_ROUTE_KEYS, _stop_systemd_unit,
        )

        checkpoint_path = _checkpoint_path()
        if not checkpoint_path.exists() and not checkpoint_path.with_name(checkpoint_path.name + ".ssh-v2").exists():
            return 0
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
        # Claim recovery ownership under the same inter-process lock as writers.
        # Release before starting pollers (their checkpoint writes take this lock).
        conn = None
        owned = set()
        waiting = False
        try:
            conn = sqlite3.connect(str(checkpoint_path) + '.lock.db', timeout=5)
            conn.execute('BEGIN IMMEDIATE')
            entries = _read_checkpoint(checkpoint_path)
            for entry in entries:
                entry_home = entry.get('profile_home') or (entry.get('remote_connection') or {}).get('profile_home')
                if not entry_home:
                    entry_home = _legacy_profile_home(self, entry)
                if not entry_home:
                    logger.warning('Legacy process %s has unknown profile ownership; retained without adoption in %s for manual reconciliation', entry.get('session_id'), checkpoint_path)
                    continue
                if entry_home != home:
                    continue
                owner = entry.get('registry_owner')
                if owner and owner[0] != os.getpid() and self._host_pid_is_ours(*owner):
                    waiting = True
                    continue
                owned.add(entry['session_id'])
                entry['registry_owner'] = [os.getpid(), self._safe_host_start_time(os.getpid())]
                entry['profile_home'] = home
            from utils import atomic_json_write
            _write_checkpoint_files(checkpoint_path, entries)
            conn.commit()
        except Exception:
            logger.warning("Cannot claim process checkpoint for recovery", exc_info=True)
            pending = getattr(self, '_checkpoint_recovery_waiting', {})
            pending[str(checkpoint_path)] = True
            self._checkpoint_recovery_waiting = pending
            return 0
        finally:
            if conn is not None:
                conn.close()
        if not hasattr(self, "_unresolved_remote_entries"):
            self._unresolved_remote_entries = {}
        self._unresolved_remote_entries[str(checkpoint_path)] = [
            entry for entry in entries if entry['session_id'] in owned and entry.get("pid_scope", "host") != "host"]
        recovered = 0
        unresolved_scope_entries: List[Dict[str, Any]] = []
        known = getattr(self, '_checkpoint_known_ids', {})
        known[str(checkpoint_path)] = owned
        self._checkpoint_known_ids = known
        for entry in entries:
            if entry['session_id'] not in owned:
                continue
            from hermes_constants import get_hermes_home
            if entry.get('profile_home', str(get_hermes_home())) != str(get_hermes_home()):
                continue
            pid, pid_scope = entry.get("pid"), entry.get("pid_scope", "host")
            if not pid and not entry.get("remote_root"):
                continue
            # A multiplexer may revisit a home; adopt each owned execution once.
            with self._lock:
                tracked = self._running.get(entry.get("session_id"))
                already_tracked = tracked is not None and tracked.observation_operation != "ssh_recovery"
            if already_tracked:
                continue
            if pid_scope != "host":
                if not entry.get("remote_root") or not entry.get("remote_connection"):
                    unresolved_scope_entries.append(entry)
                    waiting = True
                    continue  # Retain incomplete SSH/legacy records without guessing a transport.
                from tools.process_registry_remote import recover_remote
                try:
                    session = recover_remote(self, entry)
                except Exception as exc:
                    logger.warning("Remote checkpoint retained for reconciliation: %s", entry.get("session_id"), exc_info=True)
                    from tools.environments.ssh_process import safe_error
                    fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
                    fields.update(command=entry.get('command', 'unknown'),
                                  observation_operation='ssh_recovery',
                                  observation_state='identity_mismatch' if isinstance(exc, ValueError) else 'unavailable',
                                  observation_error=safe_error(exc))
                    with self._lock:
                        placeholder = self._running.get(entry['session_id'])
                    if placeholder is not None and placeholder.observation_operation == 'ssh_recovery':
                        # A failed reconstruction must retain cancellation accepted
                        # on the live placeholder just as a successful one does.
                        with placeholder._finalize_lock:
                            placeholder.observation_state = fields['observation_state']
                            placeholder.observation_error = fields['observation_error']
                    else:
                        placeholder = ProcessSession(id=entry['session_id'], detached=True, **fields)
                        with self._lock:
                            self._running[placeholder.id] = placeholder
                    session = None
                if session is None:
                    waiting = True
                    unresolved_scope_entries.append(entry)
                    continue
                recovered += 1
                if session.watcher_interval > 0:
                    self.pending_watchers.append({
                        "session_id": session.id, "check_interval": session.watcher_interval,
                        "session_key": session.session_key,
                        **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                        "notify_on_complete": session.notify_on_complete,
                        "parent_session_id": session.parent_session_id,
                    })
                continue
            # Alive AND the same process: across a restart the kernel may have
            # recycled the PID onto a stranger, and adopting it would let a later
            # kill tree-kill e.g. a browser.
            if not self._host_pid_is_ours(pid, entry.get("host_start_time")):
                if self._is_host_pid_alive(pid):
                    logger.info(
                        "Not recovering session %s: pid %d is alive but its "
                        "start time no longer matches — PID was recycled onto "
                        "an unrelated process; refusing to adopt it.",
                        entry.get("session_id", "?"), pid)
                systemd_unit = entry.get("systemd_unit", "")
                if systemd_unit and not _stop_systemd_unit(systemd_unit):
                    logger.warning(
                        "Could not reap persisted scope %s for dead wrapper pid %s; "
                        "retaining checkpoint entry for the next startup",
                        systemd_unit, pid)
                    unresolved_scope_entries.append(entry)
                continue
            fields = {f: entry.get(f, _CHECKPOINT_DEFAULTS[f]) for f in _CHECKPOINT_FIELDS}
            fields.update(
                command=entry.get("command", "unknown"),
                owner_task_id=entry.get("owner_task_id", "") or entry.get("task_id", ""),
                started_at=entry.get("started_at", time.time()))
            # detached: can't read output, but can report status + kill
            session = ProcessSession(id=entry["session_id"], detached=True, **fields)
            with self._lock:
                self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)
            # Re-enqueue watcher so gateway can resume notifications
            if session.watcher_interval > 0:
                self.pending_watchers.append({
                    "session_id": session.id,
                    "check_interval": session.watcher_interval,
                    "session_key": session.session_key,
                    **{key: getattr(session, f"watcher_{key}") for key in _WATCHER_ROUTE_KEYS},
                    "notify_on_complete": session.notify_on_complete,
                    "parent_session_id": session.parent_session_id,
                })
        if not hasattr(self, "_unresolved_remote_entries"):
            self._unresolved_remote_entries = {}
        self._unresolved_remote_entries[str(checkpoint_path)] = [
            entry for entry in unresolved_scope_entries if entry.get("pid_scope", "host") != "host"]
        self._write_checkpoint(extra_entries=unresolved_scope_entries)
        pending = getattr(self, '_checkpoint_recovery_waiting', {})
        pending[str(checkpoint_path)] = waiting
        self._checkpoint_recovery_waiting = pending
        return recovered
