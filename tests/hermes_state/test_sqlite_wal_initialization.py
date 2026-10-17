"""Concurrent first-open journal setup shared by SessionDB and small stores."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import hermes_state_wal
from hermes_cli.sqlite_util import open_db


def test_concurrent_first_open_enables_wal(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state_wal, 'is_sqlite_wal_reset_vulnerable', lambda: False)
    workers = threading.local()
    database_file = hermes_state_wal._connection_db_file

    def simultaneous_open(connection):
        database = database_file(connection)
        barrier = getattr(workers, 'barrier', None)
        if barrier is not None:
            workers.barrier = None
            barrier.wait(timeout=10)
        return database

    monkeypatch.setattr(hermes_state_wal, '_connection_db_file', simultaneous_open)

    def initialize(database, barrier, canonical):
        workers.barrier = barrier
        if canonical:
            connection = open_db(database, db_label='first-open', busy_timeout_ms=5000)
        else:
            connection = sqlite3.connect(database, timeout=5)
            try:
                hermes_state_wal.apply_wal_with_fallback(connection)
            except BaseException:
                connection.close()
                raise
        with closing(connection), connection:
            connection.execute('CREATE TABLE IF NOT EXISTS writers (id INTEGER PRIMARY KEY)')
            connection.execute('INSERT INTO writers DEFAULT VALUES')
            return connection.execute('PRAGMA journal_mode').fetchone()[0]

    with ThreadPoolExecutor(max_workers=8) as executor:
        for iteration in range(12):
            database = tmp_path / f'first-open-{iteration}.db'
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE writers (id INTEGER PRIMARY KEY)')
            barrier = threading.Barrier(8)
            futures = [executor.submit(initialize, database, barrier, worker % 2) for worker in range(8)]
            assert [future.result(timeout=15) for future in futures] == ['wal'] * 8
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute('SELECT count(*) FROM writers').fetchone()[0] == 8


def test_concurrent_first_open_preserves_every_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state_wal, 'is_sqlite_wal_reset_vulnerable', lambda: False)
    workers = threading.local()
    database_file = hermes_state_wal._connection_db_file

    def simultaneous_open(connection):
        database = database_file(connection)
        barrier = getattr(workers, 'barrier', None)
        if barrier is not None:
            workers.barrier = None
            barrier.wait(timeout=10)
            if workers.writer:
                assert workers.acquired.wait(10)
        return database

    monkeypatch.setattr(hermes_state_wal, '_connection_db_file', simultaneous_open)

    def write(database, barrier, writer, acquired, opened):
        workers.barrier = barrier
        workers.writer = writer
        workers.acquired = acquired
        try:
            if writer:
                connection = open_db(database, db_label='first-open', busy_timeout_ms=100)
            else:
                connection = sqlite3.connect(database, timeout=0.1)
                try:
                    assert hermes_state_wal.apply_wal_with_fallback(connection) == 'wal'
                except BaseException:
                    connection.close()
                    raise
        finally:
            if writer:
                opened.set()
        with closing(connection), connection:
            connection.execute('CREATE TABLE IF NOT EXISTS writers (id INTEGER PRIMARY KEY)')
            connection.execute('INSERT INTO writers VALUES (?)', (writer,))
            if not writer:
                acquired.set()
                assert opened.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        for iteration in range(12):
            database = tmp_path / f'state-{iteration}.db'
            barrier = threading.Barrier(2)
            acquired, opened = threading.Event(), threading.Event()
            futures = [executor.submit(write, database, barrier, writer, acquired, opened) for writer in range(2)]
            for future in futures:
                future.result(timeout=15)
            with closing(sqlite3.connect(database)) as connection:
                assert connection.execute('SELECT count(*) FROM writers').fetchone()[0] == 2
                assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
