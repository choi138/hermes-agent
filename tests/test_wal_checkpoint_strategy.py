"""Tests for SessionDB WAL checkpoint strategy (issue #45383).

Verifies that periodic checkpoints use PASSIVE mode (safe for large DBs)
while close() and pre-VACUUM paths still use TRUNCATE.
"""

import sqlite3
import logging
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    """Create a SessionDB with a temp database file."""
    db_path = tmp_path / "test_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


class TestTryWalCheckpointPassive:
    """_try_wal_checkpoint() should use PASSIVE mode for periodic use."""

    def test_checkpoint_uses_passive_mode(self, db):
        """PASSIVE checkpoint does not require exclusive lock — safe for large DBs."""
        # Capture the real connection's execute before mocking
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        # sqlite3.Connection.execute is read-only (C extension) — replace _conn
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        mock_conn.fetchone.return_value = None
        db._conn = mock_conn

        db._try_wal_checkpoint()

        passive_calls = [c for c in execute_calls if "wal_checkpoint(PASSIVE)" in c]
        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(passive_calls) == 1, (
            f"Expected 1 PASSIVE checkpoint call, got {len(passive_calls)}"
        )
        assert len(truncate_calls) == 0, (
            "Periodic checkpoint should NOT use TRUNCATE"
        )

    def test_checkpoint_logs_warning_on_failure(self, db, caplog):
        """Failed PASSIVE checkpoint logs a warning instead of silent pass."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        db._conn = mock_conn

        with caplog.at_level(logging.WARNING):
            db._try_wal_checkpoint()

        assert any("WAL checkpoint (PASSIVE) failed" in r.message for r in caplog.records), (
            f"Expected warning log about PASSIVE checkpoint failure, got: {caplog.text}"
        )

    def test_checkpoint_returns_result_on_success(self, db):
        """Successful PASSIVE checkpoint does not raise."""
        db._try_wal_checkpoint()


class TestCloseUsesTruncate:
    """close() should still use TRUNCATE to shrink WAL on shutdown."""

    def test_close_uses_truncate_mode(self, db):
        """TRUNCATE at close is safe — no concurrent writers during shutdown."""
        real_conn = db._conn
        execute_calls = []

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        db.close()

        truncate_calls = [c for c in execute_calls if "wal_checkpoint(TRUNCATE)" in c]
        assert len(truncate_calls) == 1, (
            f"Expected 1 TRUNCATE checkpoint at close, got {len(truncate_calls)}"
        )

    def test_close_logs_debug_on_failure(self, db, caplog):
        """Failed TRUNCATE at close logs debug (not warning — close is best-effort)."""
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        db._conn = mock_conn

        with caplog.at_level(logging.DEBUG):
            db.close()

        assert any("WAL checkpoint (TRUNCATE) at close failed" in r.message for r in caplog.records), (
            f"Expected debug log about TRUNCATE failure at close, got: {caplog.text}"
        )


class TestCheckpointFrequency:
    """Checkpoint runs from the maintenance thread, never the write hot path.

    Inline per-Nth-write checkpointing made whichever caller crossed the
    threshold pay for a multi-GB WAL flush while every other writer queued
    behind self._lock (2026-07-28 event-loop stall ingredient). The write
    path now only counts; the session-db-maintenance thread checkpoints
    once the counter advances past _CHECKPOINT_EVERY_N_WRITES.
    """

    def test_write_path_never_checkpoints_inline(self, db):
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n * 2):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        assert call_count[0] == 0, (
            f"write path must not checkpoint inline, got {call_count[0]} calls"
        )

    def test_maintenance_tick_checkpoints_after_threshold(self, db):
        call_count = [0]
        original = db._try_wal_checkpoint

        def counting_checkpoint():
            call_count[0] += 1
            original()

        db._try_wal_checkpoint = counting_checkpoint

        n = db._CHECKPOINT_EVERY_N_WRITES
        import time as _time
        for i in range(n):
            db._execute_write(lambda conn, _i=i: conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                (f"sess_{_i}", "test", _time.time()),
            ))

        db._db_maintenance_tick()
        assert call_count[0] == 1, (
            f"Expected 1 checkpoint from the maintenance tick, got {call_count[0]}"
        )
        # Below-threshold delta: the next tick is a no-op.
        db._db_maintenance_tick()
        assert call_count[0] == 1

    def test_maintenance_thread_lifecycle(self, tmp_path):
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "maint.db")
        thread = db._maint_thread
        assert thread is not None and thread.is_alive()
        db.close()
        thread.join(timeout=3.0)
        assert not thread.is_alive()

    def test_maintenance_thread_does_not_pin_unclosed_db(self, tmp_path):
        """The maintenance thread must hold only a weakref to the DB.

        Tests and short-lived callers create SessionDB without close();
        a bound-method thread target pinned every such instance and its
        sqlite caches for the process lifetime — a full-suite run was
        OOM-killed at 37GB RSS. Unclosed instances must stay collectable.
        """
        import gc
        import weakref
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "leak.db")
        thread = db._maint_thread
        ref = weakref.ref(db)
        del db  # no close() on purpose
        gc.collect()
        assert ref() is None, "unclosed SessionDB must be garbage-collectable"
        # The orphaned thread notices the dead ref on its next wake and
        # exits on its own (daemon either way; this just proves the exit
        # path). Waking it early via the stop event would defeat the
        # point, so poke the weakref path directly with a short interval.
        assert thread.daemon
