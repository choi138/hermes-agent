"""Durable local journals for the external-memory pipeline (ADR-004 Phase 0).

Two append-only JSONL journals, written from the mem-sync worker just before
a turn is dispatched to external memory providers:

* **pending/ WAL** (``~/.hermes/state/memory-pending/{session_id}.jsonl``,
  ADR-004 §4.2) — per-turn write-ahead log. Each turn is appended BEFORE
  provider ingest and ack-marked after every provider's ``sync_turn``
  returned without raising. Entries that never receive an ack survive
  process restarts (patch-cadence restarts used to lose 10–20 buffered
  turns per deploy) and are the Phase-2 curator's replay input. Phase 0 is
  durability only: the startup scan counts unconsumed entries (the §⑩
  buffer-hit metric) and GCs fully-acked files, but never replays.

* **L0-mirror** (``~/.hermes/memory/l0-mirror/{YYYY-MM}.jsonl``, ADR-004 §②)
  — local co-primary evidence journal. Every per-turn sync payload that
  leaves for the graph is mirrored (full scrubbed content) to a monthly file
  on this host, so a total graph loss can be rebuilt from local disk; the
  boundary extraction inputs (end-of-session, pre-compression) re-send
  content already mirrored per-turn, so they are recorded as compact MARKER
  records (role sequence + tool-call names, no content) instead of
  re-mirroring the whole transcript on every compaction. Zero LLM calls,
  disk only.

Contract (both journals):

* **Fail-open** — no public method may raise or block; any exception is
  swallowed and logged at debug. A broken disk must never fail a turn.
* **Scrubbed** — all persisted conversation content passes
  ``agent.redact.redact_sensitive_text(..., force=True)`` (ADR-004 §4.2
  scrub site (a): secrets must not sit in the WAL, which gains additional
  readers in later phases). ``force=True`` because a durable content store
  is a safety boundary, not a log — the user's logging redaction preference
  does not apply.
* **Crash-tolerant** — a truncated trailing line (crash mid-append) is
  skipped by the scanner, never fatal.

Kill switch: set ``HERMES_MEMORY_JOURNAL_DISABLED=1`` to no-op both journals
(checked per call, so tests and operators can flip it without a restart).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_DISABLE_ENV = "HERMES_MEMORY_JOURNAL_DISABLED"

# Session ids become file names; anything outside this set is collapsed so a
# hostile/odd session id (gateway keys can carry platform separators) can't
# escape the journal directory.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# How long a fully-acked pending file must be untouched before the startup
# scan deletes it (ADR-004 §4.2: consumed entries are kept a week for
# forensic replay, then GC'd).
_PENDING_GC_MAX_AGE_S = 7 * 24 * 3600


def journals_disabled() -> bool:
    """Return True when the operator disabled the memory journals."""
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _scrub(text: Any) -> str:
    """Deterministic secret scrub for journal content (never raises)."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    try:
        return redact_sensitive_text(text, force=True)
    except Exception:
        # Redaction failing must not lose the journal write, but persisting
        # UNSCRUBBED content on a redactor bug is the worse failure — drop
        # the content and keep the record shape instead.
        logger.debug("journal scrub failed; content omitted", exc_info=True)
        return "«scrub-failed: content omitted»"


def _safe_session_filename(session_id: str) -> str:
    name = _UNSAFE_FILENAME_CHARS.sub("_", session_id or "")
    return (name or "_no_session") + ".jsonl"


# One lock per journal file, module-level so it is shared across every
# WAL/mirror INSTANCE in the process: the gateway builds many MemoryManagers
# whose mem-sync workers all append to the same monthly mirror file, and each
# instance holding its own lock would serialize nothing. Keyed by str(path);
# journal paths are a small bounded set (one per session + one per month), so
# the map never needs eviction.
_path_locks: Dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Append one JSON line atomically with respect to concurrent appenders.

    The complete line is built first and written with a single ``os.write``
    on a raw ``O_APPEND`` fd. A buffered handle is NOT safe here: it may
    flush the heal-newline and the record as separate write() syscalls, and a
    record larger than the io buffer is flushed as several raw writes — a
    concurrent writer then interleaves mid-record and both lines come out
    corrupt (which the scanner silently skips: evidence loss). The per-path
    module lock additionally serializes the tail-heal check-then-act among
    in-process writers; O_APPEND positioning covers the write itself even
    against out-of-process appenders.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with _lock_for(path):
        fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            # Heal a truncated tail left by a crash mid-append: if the file
            # does not end with a newline, start this record on a fresh line
            # so the corrupt fragment stays confined to its own
            # (scanner-skipped) line instead of merging with — and
            # corrupting — this record too.
            if os.fstat(fd).st_size > 0:
                with open(path, "rb") as probe:
                    probe.seek(-1, os.SEEK_END)
                    if probe.read(1) != b"\n":
                        os.write(fd, b"\n")
            # Single syscall in the overwhelmingly common case; the loop only
            # exists for the (regular-file-rare) partial-write return, where
            # finishing the line beats leaving a truncated record.
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# pending/ WAL
# ---------------------------------------------------------------------------

class PendingTurnWAL:
    """Durable per-turn buffer for the external-memory sync path.

    One JSONL file per session under ``state/memory-pending/``. Two record
    shapes::

        {"type": "turn", "id": ..., "ts": ..., "session_id": ..., "seq": n,
         "records": [{"role": "user", "content": ...},
                     {"role": "assistant", "content": ...}]}
        {"type": "ack", "id": <turn entry id>, "ts": ...}

    A turn whose id has a matching ack was successfully ingested; anything
    else is unconsumed and survives restarts for the Phase-2 curator.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        # Resolve the journal directory EAGERLY, on the constructing thread.
        # Appends run later on the mem-sync worker; resolving get_hermes_home()
        # there would chase whatever HERMES_HOME / profile context the worker
        # sees at write time (a context-local override does not reliably
        # propagate to pool threads), so a queued write could land in the
        # wrong profile's home. The home active at manager construction is
        # authoritative for everything this manager journals.
        if base_dir is not None:
            self._base_dir = Path(base_dir)
        else:
            from hermes_constants import get_hermes_home
            self._base_dir = get_hermes_home() / "state" / "memory-pending"
        self._lock = threading.Lock()
        # Per-session turn sequence, lazily seeded from the existing file so
        # restarts continue the sequence instead of restarting at 0.
        self._seq: Dict[str, int] = {}

    def _dir(self) -> Path:
        return self._base_dir

    def _path_for(self, session_id: str) -> Path:
        return self._dir() / _safe_session_filename(session_id)

    def _next_seq(self, session_id: str, path: Path) -> int:
        with self._lock:
            if session_id not in self._seq:
                count = 0
                try:
                    if path.exists():
                        for rec in _iter_jsonl_records(path):
                            if rec.get("type") == "turn":
                                count += 1
                except Exception:
                    count = 0
                self._seq[session_id] = count
            self._seq[session_id] += 1
            return self._seq[session_id]

    # -- writes ---------------------------------------------------------------

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> Optional[str]:
        """Durably record a turn BEFORE ingest dispatch. Returns the entry id,
        or None when disabled/failed (callers treat None as "no ack needed")."""
        if journals_disabled():
            return None
        try:
            path = self._path_for(session_id)
            entry_id = uuid.uuid4().hex[:12]
            record = {
                "type": "turn",
                "id": entry_id,
                "ts": round(time.time(), 3),
                "session_id": session_id or "",
                "seq": self._next_seq(session_id, path),
                "records": [
                    {"role": "user", "content": _scrub(user_content)},
                    {"role": "assistant", "content": _scrub(assistant_content)},
                ],
            }
            _append_jsonl(path, record)
            return entry_id
        except Exception:
            logger.debug("memory-pending WAL append failed (fail-open)", exc_info=True)
            return None

    def ack(self, session_id: str, entry_id: str) -> None:
        """Mark a turn entry as consumed (all provider ingests succeeded)."""
        if journals_disabled() or not entry_id:
            return
        try:
            _append_jsonl(
                self._path_for(session_id),
                {"type": "ack", "id": entry_id, "ts": round(time.time(), 3)},
            )
        except Exception:
            logger.debug("memory-pending WAL ack failed (fail-open)", exc_info=True)

    # -- startup scan / GC ------------------------------------------------------

    def scan_and_gc(self) -> Optional[Dict[str, int]]:
        """Count unconsumed entries and GC old fully-acked files.

        Returns ``{"files", "unconsumed_entries", "gc_deleted_files"}`` (or
        None when disabled/failed). Logged as the ADR-004 §⑩ buffer-hit
        metric. Phase 0 deliberately does NOT replay unconsumed entries into
        ingest — that is Phase-2 curator behavior.
        """
        if journals_disabled():
            return None
        try:
            stats = {"files": 0, "unconsumed_entries": 0, "gc_deleted_files": 0}
            wal_dir = self._dir()
            if not wal_dir.is_dir():
                return stats
            now = time.time()
            for path in sorted(wal_dir.glob("*.jsonl")):
                try:
                    turn_ids: set = set()
                    acked_ids: set = set()
                    for rec in _iter_jsonl_records(path):
                        rec_type = rec.get("type")
                        rec_id = rec.get("id")
                        if not rec_id:
                            continue
                        if rec_type == "turn":
                            turn_ids.add(rec_id)
                        elif rec_type == "ack":
                            acked_ids.add(rec_id)
                    unconsumed = len(turn_ids - acked_ids)
                    fully_acked = not (turn_ids - acked_ids)
                    age = now - path.stat().st_mtime
                    if fully_acked and age > _PENDING_GC_MAX_AGE_S:
                        path.unlink()
                        stats["gc_deleted_files"] += 1
                        continue
                    stats["files"] += 1
                    stats["unconsumed_entries"] += unconsumed
                except FileNotFoundError:
                    continue  # concurrent GC / operator cleanup
            logger.info(
                "memory-pending scan: %d file(s), %d unconsumed turn entry(ies) "
                "[ADR-004 buffer-hit metric], %d fully-acked file(s) GC'd",
                stats["files"], stats["unconsumed_entries"], stats["gc_deleted_files"],
            )
            return stats
        except Exception:
            logger.debug("memory-pending WAL scan failed (fail-open)", exc_info=True)
            return None


def _iter_jsonl_records(path: Path) -> Iterable[Dict[str, Any]]:
    """Yield parsed dict records, skipping blank/truncated/corrupt lines.

    A crash mid-append leaves a truncated final line — that must degrade to
    "one unparseable line skipped", never an exception (the entry it belonged
    to simply stays unacked / uncounted).
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                yield rec


def _flatten_message_text(content: Any) -> str:
    """Flatten string/multimodal message content to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
    if content is None:
        return ""
    return str(content)


# ---------------------------------------------------------------------------
# L0-mirror
# ---------------------------------------------------------------------------

class L0Mirror:
    """Append-only local mirror of every payload sent to external memory.

    Monthly JSONL files under ``memory/l0-mirror/``. Enough is recorded to
    rebuild evidence episodes after a total graph loss: the role-tagged body,
    the session id (group hint), the WAL entry id (join key to pending/), the
    payload kind, and the destination provider names (source metadata).

    Per-turn sync payloads carry full (scrubbed) content. Boundary payloads
    (``session_end`` / ``pre_compress``) are content-free markers — see
    :meth:`build_boundary_record`.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        # Eager resolution on the constructing thread — same rationale as
        # PendingTurnWAL.__init__: mirror writes run on the mem-sync worker
        # and must not chase a changed HERMES_HOME / profile context.
        if base_dir is not None:
            self._base_dir = Path(base_dir)
        else:
            from hermes_constants import get_hermes_home
            self._base_dir = get_hermes_home() / "memory" / "l0-mirror"

    def _dir(self) -> Path:
        return self._base_dir

    def _path_for(self, ts: float) -> Path:
        return self._dir() / (time.strftime("%Y-%m", time.localtime(ts)) + ".jsonl")

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        provider_names: Iterable[str] = (),
        wal_entry_id: Optional[str] = None,
    ) -> None:
        """Mirror a per-turn sync payload just before ingest dispatch."""
        if journals_disabled():
            return
        try:
            ts = time.time()
            _append_jsonl(self._path_for(ts), {
                "ts": round(ts, 3),
                "kind": "sync_turn",
                "session_id": session_id or "",
                "wal_entry_id": wal_entry_id or "",
                "body": {
                    "user": _scrub(user_content),
                    "assistant": _scrub(assistant_content),
                },
                "meta": {"providers": list(provider_names)},
            })
        except Exception:
            logger.debug("l0-mirror append_turn failed (fail-open)", exc_info=True)

    def build_boundary_record(
        self,
        kind: str,
        messages: Optional[List[Dict[str, Any]]],
        *,
        session_id: str = "",
        provider_names: Iterable[str] = (),
    ) -> Optional[Dict[str, Any]]:
        """Derive a compact boundary MARKER record for a ``session_end`` /
        ``pre_compress`` extraction payload: the role sequence, tool-call
        names, and per-message content sizes — but no message content.

        The transcript handed to boundary extraction is content the per-turn
        mirror already holds; re-mirroring it wholesale made the mirror grow
        superlinearly under repeated compaction (every ``pre_compress``
        re-wrote the entire window). The marker keeps the evidence that a
        boundary extraction happened — including the tool-call sequence that
        per-turn records lack — at tens of bytes per message.

        Pure derivation, no I/O, never raises: safe to call inline on the
        hot path, with the returned record handed to :meth:`append_record`
        on a background worker. Returns None when disabled or on failure.
        """
        if journals_disabled():
            return None
        try:
            skeleton = []
            for msg in messages or []:
                if not isinstance(msg, dict):
                    continue
                entry: Dict[str, Any] = {
                    "role": msg.get("role") or "",
                    "chars": len(_flatten_message_text(msg.get("content"))),
                }
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    entry["tool_calls"] = [
                        (tc.get("function") or {}).get("name", "?")
                        for tc in tool_calls if isinstance(tc, dict)
                    ]
                skeleton.append(entry)
            return {
                "ts": round(time.time(), 3),
                "kind": kind,
                "session_id": session_id or "",
                "skeleton": skeleton,
                "meta": {
                    "providers": list(provider_names),
                    "message_count": len(skeleton),
                },
            }
        except Exception:
            logger.debug(
                "l0-mirror boundary record build failed (fail-open)", exc_info=True
            )
            return None

    def append_record(self, record: Optional[Dict[str, Any]]) -> None:
        """Append a pre-built record (None is a no-op). Fail-open."""
        if record is None or journals_disabled():
            return
        try:
            ts = float(record.get("ts") or time.time())
            _append_jsonl(self._path_for(ts), record)
        except Exception:
            logger.debug("l0-mirror append_record failed (fail-open)", exc_info=True)

    def append_messages(
        self,
        kind: str,
        messages: Optional[List[Dict[str, Any]]],
        *,
        session_id: str = "",
        provider_names: Iterable[str] = (),
    ) -> None:
        """Record a boundary payload (``session_end`` / ``pre_compress``) as
        a content-free marker — see :meth:`build_boundary_record`."""
        self.append_record(
            self.build_boundary_record(
                kind, messages, session_id=session_id, provider_names=provider_names
            )
        )


# ---------------------------------------------------------------------------
# Process-level startup scan (once per pending directory)
# ---------------------------------------------------------------------------

_scanned_pending_dirs: set = set()
_startup_scan_lock = threading.Lock()


def run_pending_startup_scan_once(wal: PendingTurnWAL) -> None:
    """Run the WAL startup scan/GC once per pending DIRECTORY per process.

    MemoryManager init calls this; the gateway builds many managers over one
    HERMES_HOME and the directory only needs one sweep — but a multi-profile
    process constructs managers over SEVERAL homes, and a process-global once
    flag would pin the scan (and its buffer-hit metric + GC) to whichever
    profile happened to construct first, leaving the others unscanned
    forever. Keyed by the wal's pinned directory instead. Fail-open.

    Note: this is a startup sweep only — files fully acked while the process
    stays up are GC'd on the NEXT process start, not continuously. Fine at
    patch-cadence restart frequency; revisit if gateways ever run for months.
    """
    try:
        key = str(wal._dir())
    except Exception:  # pragma: no cover - _dir is attribute access
        return
    with _startup_scan_lock:
        if key in _scanned_pending_dirs:
            return
        _scanned_pending_dirs.add(key)
    try:
        wal.scan_and_gc()
    except Exception:  # pragma: no cover - scan_and_gc guards internally
        logger.debug("memory-pending startup scan failed (fail-open)", exc_info=True)
