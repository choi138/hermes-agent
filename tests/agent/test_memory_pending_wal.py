"""Tests for the pending/ WAL — the durable per-turn memory buffer (ADR-004 §4.2).

Turns are journaled to ``state/memory-pending/{session_id}.jsonl`` BEFORE
provider ingest and ack-marked after every provider's ``sync_turn`` returned
without raising; unacked entries survive restarts for the Phase-2 curator.
Phase 0 is durability only: the startup scan counts unconsumed entries (the
ADR §⑩ buffer-hit metric) and GCs fully-acked files older than 7 days, but
never replays into ingest.

Covers: append/ack/GC semantics, crash-mid-write tolerance (a truncated last
line must not break the scanner), the secret scrub on journaled content, the
fail-open contract, and the MemoryManager integration (WAL entry lands before
provider dispatch; failed ingest leaves the entry unacked).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent.memory_journal import (
    PendingTurnWAL,
    _PENDING_GC_MAX_AGE_S,
    journals_disabled,
)
from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


def _records(path: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture()
def wal(tmp_path) -> PendingTurnWAL:
    return PendingTurnWAL(base_dir=tmp_path / "memory-pending")


# ---------------------------------------------------------------------------
# Append / ack semantics
# ---------------------------------------------------------------------------

class TestAppendAck:
    def test_append_writes_role_tagged_turn_record(self, wal, tmp_path):
        entry_id = wal.append_turn("sess-1", "user says hi", "assistant replies")

        assert entry_id
        recs = _records(tmp_path / "memory-pending" / "sess-1.jsonl")
        assert len(recs) == 1
        rec = recs[0]
        assert rec["type"] == "turn"
        assert rec["id"] == entry_id
        assert rec["session_id"] == "sess-1"
        assert rec["seq"] == 1
        assert rec["records"][0] == {"role": "user", "content": "user says hi"}
        assistant_rec = rec["records"][1]
        assert assistant_rec["role"] == "assistant"
        assert assistant_rec["content"] == "assistant replies"
        # ADR-004 §① (Phase 2): assistant spans always carry an explicit
        # write-time taint verdict — clean here (no injections registered).
        assert assistant_rec["taint"]["tainted"] is False
        assert isinstance(rec["ts"], float)

    def test_ack_marks_entry_consumed(self, wal, tmp_path):
        entry_id = wal.append_turn("sess-1", "u", "a")
        wal.ack("sess-1", entry_id)

        recs = _records(tmp_path / "memory-pending" / "sess-1.jsonl")
        assert [r["type"] for r in recs] == ["turn", "ack"]
        assert recs[1]["id"] == entry_id

        stats = wal.scan_and_gc()
        assert stats == {"files": 1, "unconsumed_entries": 0, "gc_deleted_files": 0}

    def test_unacked_entries_are_counted_unconsumed(self, wal):
        first = wal.append_turn("sess-1", "u1", "a1")
        wal.append_turn("sess-1", "u2", "a2")  # never acked
        wal.append_turn("sess-2", "u3", "a3")  # never acked
        wal.ack("sess-1", first)

        stats = wal.scan_and_gc()
        assert stats["files"] == 2
        assert stats["unconsumed_entries"] == 2

    def test_seq_continues_across_wal_instances(self, wal, tmp_path):
        """A restart (fresh WAL instance) must extend the sequence, not
        restart it — seq is seeded from the existing file."""
        wal.append_turn("sess-1", "u1", "a1")
        wal.append_turn("sess-1", "u2", "a2")

        restarted = PendingTurnWAL(base_dir=tmp_path / "memory-pending")
        restarted.append_turn("sess-1", "u3", "a3")

        recs = _records(tmp_path / "memory-pending" / "sess-1.jsonl")
        assert [r["seq"] for r in recs if r["type"] == "turn"] == [1, 2, 3]

    def test_session_id_is_sanitized_for_filenames(self, wal, tmp_path):
        wal.append_turn("../weird:session/id", "u", "a")

        wal_dir = tmp_path / "memory-pending"
        names = [p.name for p in wal_dir.iterdir()]
        assert len(names) == 1
        assert "/" not in names[0].replace(".jsonl", "")
        assert ".." not in names[0].split(".jsonl")[0].replace("._", "")
        # Nothing escaped the journal directory.
        assert list(wal_dir.parent.glob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# Crash tolerance + scrub
# ---------------------------------------------------------------------------

class TestCrashToleranceAndScrub:
    def test_truncated_last_line_does_not_break_scanner(self, wal, tmp_path):
        """A crash mid-append leaves a partial JSON line; the scanner must
        skip it and still count the intact entries."""
        entry_id = wal.append_turn("sess-1", "u1", "a1")
        wal.append_turn("sess-1", "u2", "a2")
        path = tmp_path / "memory-pending" / "sess-1.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type": "turn", "id": "trunc')  # no newline, cut mid-record

        stats = wal.scan_and_gc()
        assert stats["files"] == 1
        assert stats["unconsumed_entries"] == 2

        # And appends after the crash still parse: the writer heals an
        # unterminated tail (starts on a fresh line), so only the single
        # corrupt fragment is lost.
        wal.ack("sess-1", entry_id)
        from agent.memory_journal import _iter_jsonl_records
        recs = list(_iter_jsonl_records(path))
        assert recs[-1] == {"type": "ack", "id": entry_id, "ts": recs[-1]["ts"]}
        stats = wal.scan_and_gc()
        assert stats["unconsumed_entries"] == 1  # u2 still unconsumed

    def test_secrets_are_scrubbed_at_write_time(self, wal, tmp_path):
        """ADR-004 §4.2 scrub site (a): secrets must never sit in the WAL."""
        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        wal.append_turn("sess-1", f"my key is {secret}", f"noted: {secret}")

        raw = (tmp_path / "memory-pending" / "sess-1.jsonl").read_text(encoding="utf-8")
        assert secret not in raw
        # The record shape survives; only the token is masked.
        rec = _records(tmp_path / "memory-pending" / "sess-1.jsonl")[0]
        assert rec["records"][0]["role"] == "user"
        assert "my key is" in rec["records"][0]["content"]


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------

class TestGC:
    def _age(self, path: Path, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_fully_acked_old_file_is_deleted(self, wal, tmp_path):
        entry_id = wal.append_turn("sess-old", "u", "a")
        wal.ack("sess-old", entry_id)
        path = tmp_path / "memory-pending" / "sess-old.jsonl"
        self._age(path, _PENDING_GC_MAX_AGE_S + 60)

        stats = wal.scan_and_gc()
        assert stats["gc_deleted_files"] == 1
        assert not path.exists()

    def test_unacked_old_file_is_kept(self, wal, tmp_path):
        """Durability beats tidiness: an unconsumed entry is exactly what the
        WAL exists to preserve, however old."""
        wal.append_turn("sess-old", "u", "a")  # no ack
        path = tmp_path / "memory-pending" / "sess-old.jsonl"
        self._age(path, _PENDING_GC_MAX_AGE_S + 60)

        stats = wal.scan_and_gc()
        assert stats["gc_deleted_files"] == 0
        assert stats["unconsumed_entries"] == 1
        assert path.exists()

    def test_fresh_fully_acked_file_is_kept(self, wal, tmp_path):
        entry_id = wal.append_turn("sess-new", "u", "a")
        wal.ack("sess-new", entry_id)

        stats = wal.scan_and_gc()
        assert stats["gc_deleted_files"] == 0
        assert (tmp_path / "memory-pending" / "sess-new.jsonl").exists()

    def test_scan_on_missing_directory_is_zero(self, tmp_path):
        wal = PendingTurnWAL(base_dir=tmp_path / "never-created")
        assert wal.scan_and_gc() == {
            "files": 0, "unconsumed_entries": 0, "gc_deleted_files": 0,
        }

    def test_startup_scan_runs_once_per_directory_not_per_process(self, tmp_path, monkeypatch):
        """A multi-profile process constructs managers over several
        HERMES_HOMEs; a process-global once flag would pin scan+GC to
        whichever profile constructed first and never sweep the others."""
        import agent.memory_journal as mj

        scanned: List[str] = []
        monkeypatch.setattr(
            PendingTurnWAL, "scan_and_gc",
            lambda self: scanned.append(str(self._dir())),
        )
        monkeypatch.setattr(mj, "_scanned_pending_dirs", set())

        dir_a, dir_b = tmp_path / "home-a", tmp_path / "home-b"
        mj.run_pending_startup_scan_once(PendingTurnWAL(base_dir=dir_a))
        mj.run_pending_startup_scan_once(PendingTurnWAL(base_dir=dir_a))  # dedup
        mj.run_pending_startup_scan_once(PendingTurnWAL(base_dir=dir_b))

        assert scanned == [str(dir_a), str(dir_b)]


# ---------------------------------------------------------------------------
# Fail-open contract + kill switch
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_append_never_raises_on_unwritable_dir(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")  # mkdir(parents=True) will fail
        wal = PendingTurnWAL(base_dir=blocker / "memory-pending")

        assert wal.append_turn("sess-1", "u", "a") is None
        wal.ack("sess-1", "whatever")  # must not raise either

    def test_kill_switch_disables_both_directions(self, wal, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_MEMORY_JOURNAL_DISABLED", "1")
        assert journals_disabled() is True
        assert wal.append_turn("sess-1", "u", "a") is None
        assert wal.scan_and_gc() is None
        assert not (tmp_path / "memory-pending").exists()

    def test_journal_dir_pinned_at_construction(self, tmp_path, monkeypatch):
        """The journal directory is resolved EAGERLY on the constructing
        thread. Appends run later on the mem-sync worker — if they re-resolved
        HERMES_HOME at write time, a queued write could land in whatever home
        the environment points at by then (observed: delayed background syncs
        writing into the real ~/.hermes after test teardown restored env)."""
        from agent.memory_journal import L0Mirror

        home_a = tmp_path / "home-a"
        home_b = tmp_path / "home-b"
        monkeypatch.setenv("HERMES_HOME", str(home_a))
        wal = PendingTurnWAL()
        mirror = L0Mirror()

        # Env changes AFTER construction (teardown, profile switch) …
        monkeypatch.setenv("HERMES_HOME", str(home_b))
        wal.append_turn("sess-1", "u", "a")
        mirror.append_turn("sess-1", "u", "a")

        # … but writes stay pinned to the home active at construction.
        assert (home_a / "state" / "memory-pending" / "sess-1.jsonl").exists()
        assert list((home_a / "memory" / "l0-mirror").glob("*.jsonl"))
        assert not home_b.exists()


# ---------------------------------------------------------------------------
# MemoryManager integration
# ---------------------------------------------------------------------------

class _RecordingProvider(MemoryProvider):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.synced: List[str] = []
        self.wal_records_at_sync: List[int] = []
        self._wal_path: Path | None = None

    @property
    def name(self) -> str:
        return "recorder"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        # Prove write-ahead ordering: the WAL entry must already be on disk
        # when the provider ingest runs.
        if self._wal_path is not None and self._wal_path.exists():
            self.wal_records_at_sync.append(len(_records(self._wal_path)))
        else:
            self.wal_records_at_sync.append(0)
        if self.fail:
            raise RuntimeError("simulated ingest failure")
        self.synced.append(user_content)


def _manager_with_wal(tmp_path, provider) -> MemoryManager:
    mm = MemoryManager()
    mm._pending_wal = PendingTurnWAL(base_dir=tmp_path / "memory-pending")
    provider._wal_path = tmp_path / "memory-pending" / "sess-1.jsonl"
    mm.add_provider(provider)
    return mm


class TestManagerIntegration:
    def test_turn_journaled_before_ingest_and_acked_after(self, tmp_path):
        provider = _RecordingProvider()
        mm = _manager_with_wal(tmp_path, provider)

        mm.sync_all("hello", "world", session_id="sess-1")
        assert mm.flush_pending(timeout=5)

        assert provider.synced == ["hello"]
        # Write-ahead: exactly the turn record existed at sync time.
        assert provider.wal_records_at_sync == [1]
        recs = _records(tmp_path / "memory-pending" / "sess-1.jsonl")
        assert [r["type"] for r in recs] == ["turn", "ack"]
        assert recs[1]["id"] == recs[0]["id"]

    def test_failed_ingest_leaves_entry_unacked(self, tmp_path):
        provider = _RecordingProvider(fail=True)
        mm = _manager_with_wal(tmp_path, provider)

        mm.sync_all("hello", "world", session_id="sess-1")
        assert mm.flush_pending(timeout=5)

        recs = _records(tmp_path / "memory-pending" / "sess-1.jsonl")
        assert [r["type"] for r in recs] == ["turn"]
        stats = mm._pending_wal.scan_and_gc()
        assert stats["unconsumed_entries"] == 1

    def test_broken_wal_never_blocks_sync(self, tmp_path):
        provider = _RecordingProvider()
        mm = MemoryManager()
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        mm._pending_wal = PendingTurnWAL(base_dir=blocker / "memory-pending")
        mm.add_provider(provider)

        mm.sync_all("hello", "world", session_id="sess-1")
        assert mm.flush_pending(timeout=5)

        # Ingest proceeded even though the journal could not be written.
        assert provider.synced == ["hello"]
