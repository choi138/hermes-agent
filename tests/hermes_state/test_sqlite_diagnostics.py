"""Real connections identify the holder separately from a waiter without values."""
import json
import logging
import sqlite3
import threading

import pytest

from hermes_cli.sqlite_util import open_db
from hermes_state import SessionDB
import hermes_state_diagnostics as diagnostics


class Capture(logging.Handler):
    def __init__(self, predicate):
        super().__init__()
        self.records = []
        self.ready = threading.Event()
        self.predicate = predicate

    def emit(self, record):
        data = json.loads(record.getMessage().removeprefix('sqlite_diagnostic '))
        self.records.append(data)
        if self.predicate(data):
            self.ready.set()


@pytest.mark.parametrize('source', ['sessiondb', 'small_store'])
def test_live_holder_waiter_and_commit_are_attributed_without_values(tmp_path, monkeypatch, source):
    path = tmp_path / 'state.db'
    store = SessionDB(path) if source == 'sessiondb' else None
    owner = store._conn if store else open_db(path, db_label='test')
    waiter = None
    capture = Capture(lambda r: r['event'] == 'ongoing' and r['phase'] == 'transaction_idle'
                      and 'UPDATE sample' in r.get('last_sql', ''))
    diagnostics.logger.addHandler(capture)
    try:
        owner.execute('CREATE TABLE sample(value)')
        owner.execute('INSERT INTO sample VALUES (?)', ('private literal',))
        owner.commit()
        waiter = open_db(path, db_label='test', busy_timeout_ms=50)
        monkeypatch.setattr(diagnostics, '_SLOW_SECONDS', 0.01)
        monkeypatch.setattr(diagnostics, '_ONGOING_SECONDS', 0.02)
        cursor = owner.cursor()
        cursor.execute('BEGIN IMMEDIATE')
        cursor.execute("UPDATE sample SET value=? WHERE value='private literal' /*private comment*/",
                       ('private parameter',))
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            waiter.execute('BEGIN IMMEDIATE')
        assert capture.ready.wait(3), capture.records
        holders = [r for r in capture.records if r['event'] == 'ongoing' and r['held_ms'] is not None]
        blocked = [r for r in capture.records if r['event'] == 'failed' and r['phase'] == 'begin_wait']
        assert holders and blocked
        holder, blocked = holders[-1], blocked[-1]
        assert holder['db'] == blocked['db'] == str(path)
        assert holder['connection'] != blocked['connection']
        assert holder['transaction'] != blocked['transaction']
        assert holder['idle_ms'] >= 0 and holder['thread_cpu_ms'] >= 0
        assert holder['acquisition_sql'] == 'BEGIN IMMEDIATE'
        import sys
        if sys.platform == 'linux':
            assert holder['host']['cpu_ticks']['total'] > 0
            assert holder['host']['cpu_ticks']['iowait'] >= 0
            assert holder['host']['process_io_bytes']['read_bytes'] >= 0
        assert blocked['held_ms'] is None and blocked['begin_wait_ms'] >= 0
        assert 'test_live_holder_waiter' in holder['last_caller']
        capture.ready.clear()
        capture.predicate = lambda r: r['phase'] == 'commit' and r['connection'] == holder['connection']
        owner.commit()
        assert capture.ready.wait(3), capture.records
        committed = next(r for r in reversed(capture.records) if capture.predicate(r))
        assert committed['transaction'] == holder['transaction']
        assert committed['held_ms'] >= holder['held_ms']
        assert 'UPDATE sample' in committed['last_sql']
        output = json.dumps(capture.records)
        assert all(secret not in output for secret in ['private literal', 'private parameter', 'private comment'])
    finally:
        diagnostics.logger.removeHandler(capture)
        if waiter:
            waiter.close()
        if store:
            store.close()
        else:
            owner.close()


def test_context_commit_rollback_and_closed_errors_preserve_sqlite_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, '_SLOW_SECONDS', 0)
    conn = open_db(tmp_path / 'state.db', db_label='test')
    capture = Capture(lambda r: r['phase'] == 'rollback')
    diagnostics.logger.addHandler(capture)
    try:
        conn.execute('CREATE TABLE sample(value)')
        with conn:
            conn.executemany('INSERT INTO sample VALUES (?)', [('kept',)])
        with pytest.raises(RuntimeError, match='original failure'):
            with conn:
                conn.execute('INSERT INTO sample VALUES (?)', ('discarded',))
                raise RuntimeError('original failure')
        assert [tuple(row) for row in conn.execute('SELECT value FROM sample')] == [('kept',)]
        assert capture.ready.wait(3)
        assert any(r['phase'] == 'commit' for r in capture.records)
        assert any(r['phase'] == 'rollback' for r in capture.records)
        conn.close()
        conn.close()
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')
    finally:
        diagnostics.logger.removeHandler(capture)
        conn.close()


def test_custom_connection_cursor_behavior_survives_tracking_and_diagnostics(tmp_path):
    from hermes_state_dbfile import _connect_tracked_db

    class CustomCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            if sql == 'SELECT special':
                sql = 'SELECT 42'
            return super().execute(sql, parameters)

    class CustomConnection(sqlite3.Connection):
        def cursor(self, factory=None):
            return super().cursor(factory or CustomCursor)

    conn = _connect_tracked_db(tmp_path / 'custom.db', factory=CustomConnection)
    try:
        assert isinstance(conn, CustomConnection)
        assert conn.cursor().execute('SELECT special').fetchone()[0] == 42
        assert type(conn.cursor(sqlite3.Cursor)) is sqlite3.Cursor
    finally:
        conn.close()


def test_collected_connection_is_not_reported_as_a_live_holder(tmp_path, monkeypatch):
    import gc
    import weakref

    monkeypatch.setattr(diagnostics, '_ONGOING_SECONDS', 0)
    conn = open_db(tmp_path / 'collected.db', db_label='test')
    conn.execute('BEGIN IMMEDIATE')
    key = conn._diag_id
    capture = Capture(lambda r: r['event'] == 'ongoing' and r['connection'] == key)
    diagnostics.logger.addHandler(capture)
    try:
        diagnostics._poll()
        assert capture.ready.wait(3)
        reference = weakref.ref(conn)
        del conn
        gc.collect()
        assert reference() is None
        checkpoint = len(capture.records)
        diagnostics._poll()
        assert not any(r['connection'] == key for r in capture.records[checkpoint:])
        # SQLite really released its writer, not just the diagnostic metadata.
        with sqlite3.connect(tmp_path / 'collected.db', timeout=0.1) as waiter:
            waiter.execute('BEGIN IMMEDIATE')
    finally:
        diagnostics.logger.removeHandler(capture)


def test_watchdog_routes_real_logs_to_connection_profile_a_b_a(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_logging import _ProfileRoutingFileHandler, RotatingFileHandler

    a, b = tmp_path / 'a', tmp_path / 'b'
    for home in (a, b):
        (home / 'logs').mkdir(parents=True)
    base = RotatingFileHandler(a / 'logs' / 'events.log')
    router = _ProfileRoutingFileHandler(base, [a, b])
    capture = Capture(lambda r: r['phase'] == 'close')
    diagnostics.logger.addHandler(router)
    diagnostics.logger.addHandler(capture)
    monkeypatch.setattr(diagnostics, '_SLOW_SECONDS', 0)
    try:
        for home in (a, b, a):
            token = set_hermes_home_override(home)
            try:
                capture.ready.clear()
                conn = open_db(home / 'state.db', db_label='test')
                key = conn._diag_id
                capture.predicate = lambda r: r['phase'] == 'close' and r['connection'] == key
                conn.execute('SELECT 1')
                conn.close()
                assert capture.ready.wait(3)
            finally:
                reset_hermes_home_override(token)
        for home, expected_closes in ((a, 2), (b, 1)):
            records = [json.loads(line.removeprefix('sqlite_diagnostic '))
                       for line in (home / 'logs' / 'events.log').read_text().splitlines()]
            scoped = [r for r in records if r['db'] in {str(a / 'state.db'), str(b / 'state.db')}]
            assert all(r['db'] == str(home / 'state.db') for r in scoped)
            assert sum(r['phase'] == 'close' for r in scoped) == expected_closes
    finally:
        diagnostics.logger.removeHandler(capture)
        diagnostics.logger.removeHandler(router)
        router.close()
        base.close()


def test_sql_literals_and_eof_comments_are_redacted_before_logging_and_hashing(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, '_SLOW_SECONDS', 0)
    conn = open_db(tmp_path / 'redacted.db', db_label='test')
    capture = Capture(lambda r: r['phase'] == 'close' and r['connection'] == conn._diag_id)
    diagnostics.logger.addHandler(capture)
    try:
        underscore_supported = sqlite3.sqlite_version_info >= (3, 46, 0)
        for number in ('123_456', '987_654') if underscore_supported else ('123456', '987654'):
            conn.execute(f'SELECT {number} /*private eof comment').fetchone()
        for prefix in ('SELECT $', 'SELECT :', 'SELECT:', 'SELECT @', 'SELECT@'):
            for name in ('value', 'value😀'):
                parameter = name + "(')"
                value = conn.execute(f"{prefix}{parameter} || 'private named literal'",
                                     {parameter: 'private named parameter'}).fetchone()[0]
                assert value == 'private named parameterprivate named literal'
        for name in ('t', 't😀'):
            conn.execute(f"CREATE TABLE {name}$x(v TEXT DEFAULT ')private identifier literal')")
        conn.executescript("SELECT 'private script value'; /*private script comment")
        conn.close()
        assert capture.ready.wait(3)
        values = [r for r in capture.records if r['sql'] == 'SELECT ? ?' and r['phase'] == 'sql']
        assert len(values) == 2
        assert values[0]['sql_id'] == values[1]['sql_id']
        assert values[0]['sql'] == values[1]['sql'] == 'SELECT ? ?'
        output = json.dumps(capture.records)
        assert all(secret not in output for secret in ('private eof comment', 'private script value',
                   'private script comment', 'private named literal', 'private named parameter',
                   'private identifier literal'))
        assert any(r['phase'] == 'script' and r['sql'].startswith('SELECT') for r in capture.records)
    finally:
        diagnostics.logger.removeHandler(capture)
        conn.close()


@pytest.mark.parametrize('use_cursor', [False, True])
def test_script_transaction_change_does_not_inherit_a_previous_writer(tmp_path, monkeypatch, use_cursor):
    monkeypatch.setattr(diagnostics, '_ONGOING_SECONDS', 0)
    conn = open_db(tmp_path / 'script.db', db_label='test')
    capture = Capture(lambda r: r['event'] == 'ongoing' and r['connection'] == conn._diag_id)
    diagnostics.logger.addHandler(capture)
    try:
        conn.execute('BEGIN IMMEDIATE')
        diagnostics._poll()
        writer = next(r for r in reversed(capture.records) if capture.predicate(r))
        assert writer['held_ms'] is not None
        capture.records.clear()
        (conn.cursor() if use_cursor else conn).executescript('BEGIN DEFERRED; SELECT 1;')
        diagnostics._poll()
        reader = next(r for r in reversed(capture.records) if capture.predicate(r))
        assert reader['transaction'] != writer['transaction']
        assert reader['held_ms'] is None
        assert 'acquisition_sql' not in reader
        with sqlite3.connect(tmp_path / 'script.db', timeout=0.1) as other:
            other.execute('BEGIN IMMEDIATE')  # no longer blocked by the first writer
    finally:
        diagnostics.logger.removeHandler(capture)
        conn.close()
