"""Local SQLite timing/owner evidence without SQL values or database probes.

The watchdog only reads Python metadata, never signals a process, opens database
files, or queries a connection. Logging runs off the writer thread: a slow disk
log handler must not extend the transaction it is diagnosing.
"""
from __future__ import annotations

from collections import deque
import hashlib
import itertools
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import weakref
from urllib.parse import unquote

logger = logging.getLogger(__name__)
_SLOW_SECONDS = 1.0
_ONGOING_SECONDS = 5.0
_POLL_SECONDS = 1.0
_ids = itertools.count(1)
_lock = threading.RLock()
_active: dict[str, dict] = {}
_finished = deque(maxlen=256)
_wake = threading.Event()
_watcher = None
_factories: dict[type, type] = {}
# Strip literals AND comments before retaining any SQL. Never use SQLite's
# trace callback: it expands bound parameters into the statement.
_values = re.compile(
    # SQLite parameter tokens can contain namespace/suffix text, including
    # quotes. Consume them before interpreting quotes as string delimiters.
    # '$' can be inside an identifier; ':'/'@' always start a new token,
    # including immediately after a keyword (SELECT:name / SELECT@name).
    r"(?:(?<![\w$\u0080-\U0010ffff])\$|[:@])[\w$\u0080-\U0010ffff]+"
    r"(?:::[\w$\u0080-\U0010ffff]*)*(?:\([^)]*(?:\)|$))?"
    r"|'(?:''|[^'])*(?:'|$)|\"(?:\"\"|[^\"])*(?:\"|$)|`(?:``|[^`])*(?:`|$)|\[[^\]]*(?:\]|$)"
    r"|--[^\n]*(?:\n|$)|/\*.*?(?:\*/|$)|\b0[xX][\da-fA-F](?:_?[\da-fA-F])*\b"
    r"|\b\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?(?:[eE][+-]?\d(?:_?\d)*)?\b",
    re.DOTALL,
)


def _sql_metadata(sql: str) -> dict:
    shape = ' '.join(_values.sub('?', sql).split())
    return {'sql': shape[:512], 'sql_id': hashlib.sha256(shape.encode()).hexdigest()[:16]}


def _caller() -> str:
    frame = sys._getframe(1)
    sites = []
    try:
        while frame is not None and len(sites) < 6:
            module = frame.f_globals.get('__name__', '?')
            if module != __name__:
                sites.append(f'{module}.{frame.f_code.co_name}:{frame.f_lineno}')
            frame = frame.f_back
        return ' <- '.join(sites)
    finally:
        del frame


def _record(state: dict, now: float, event: str, error: str | None = None) -> dict:
    result = {k: v for k, v in state.items() if not k.startswith('_')}
    result.update(event=event, error=error,
                  operation_ms=round((now - state['_operation_started']) * 1000, 3),
                  held_ms=(round((now - state['_acquired']) * 1000, 3)
                           if state.get('_acquired') is not None else None))
    # For implicit transactions this is a lower bound measured after the first
    # successful write. Waiting and execution cannot be separated inside SQLite.
    result['held_time_basis'] = 'after_successful_acquisition_or_write'
    if state['phase'] == 'transaction_idle':
        result['idle_ms'] = round((now - state['_operation_ended']) * 1000, 3)
    return result


def _poll() -> None:
    now = time.monotonic()
    with _lock:
        records = list(_finished)
        _finished.clear()
        for state in _active.values():
            age = now - (state.get('_acquired') or state['_operation_started'])
            if age >= _ONGOING_SECONDS and now - state.get('_reported', 0) >= _ONGOING_SECONDS:
                records.append(_record(state, now, 'ongoing'))
                state['_reported'] = now
    if records:
        from hermes_state_host_metrics import snapshot
        host = snapshot()
        for record in records:
            record['host'] = host
            if logger.isEnabledFor(logging.WARNING):
                log = logger.makeRecord(logger.name, logging.WARNING, __file__, 0,
                                        'sqlite_diagnostic %s',
                                        (json.dumps(record, ensure_ascii=True),), None)
                # The normal record factory runs on this watchdog's default
                # profile. Restore the connection's owner before queue routing.
                log.hermes_home = record['profile_home']
                logger.handle(log)


def _watch() -> None:
    while True:
        _wake.wait(_POLL_SECONDS)
        _wake.clear()
        _poll()


def _start_watcher() -> None:
    global _watcher
    # Caller holds _lock. The thread consumes explicit path metadata only; it
    # does not resolve launch-profile config or credentials.
    if _watcher is None:
        _watcher = threading.Thread(target=_watch, name='sqlite-diagnostics', daemon=True)
        _watcher.start()


def _after_fork() -> None:
    # The child's only surviving thread must not inherit a locked mutex or a
    # watcher object that refers to a thread which no longer exists.
    global _lock, _active, _finished, _wake, _watcher
    _lock = threading.RLock()
    _active = {}
    _finished = deque(maxlen=256)
    _wake = threading.Event()
    _watcher = None


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _forget(connection_id: str) -> None:
    with _lock:
        _active.pop(connection_id, None)


class _DiagnosticMixin:
    def __init__(self, database, *args, **kwargs):
        from hermes_constants import get_hermes_home
        path = os.fsdecode(database)
        if path.startswith('file:'):
            path = unquote(path[5:].split('?', 1)[0])
        self._diag_path = str(Path(path).absolute()) if path != ':memory:' else path
        self._diag_id = f'{os.getpid()}:{next(_ids)}'
        self._diag_pid = os.getpid()
        self._diag_home = str(get_hermes_home())
        self._diag_state = None
        self._diag_finalizer = weakref.finalize(self, _forget, self._diag_id)
        super().__init__(database, *args, **kwargs)

    def _invoke(self, fn, sql: str, phase: str):
        if self._diag_pid != os.getpid():
            self._diag_pid = os.getpid()
            self._diag_id = f'{os.getpid()}:{next(_ids)}'
            self._diag_state = None
            self._diag_finalizer.detach()
            self._diag_finalizer = weakref.finalize(self, _forget, self._diag_id)
        started = time.monotonic()
        cpu_started = time.thread_time()
        # executescript can implicitly commit the old transaction and open
        # another one internally. Never carry an old writer's ownership across
        # those unobservable boundaries (even when the new transaction is read-only).
        state = {} if phase == 'script' else dict(self._diag_state or {})
        state.update(db=self._diag_path, profile_home=self._diag_home,
                     connection=self._diag_id, pid=os.getpid(),
                     thread=threading.get_ident(), operation=f'{self._diag_id}:{next(_ids)}',
                     phase=phase, caller=_caller(), _operation_started=started, thread_cpu_ms=None,
                     **_sql_metadata(sql))
        if 'transaction' not in state:
            state['transaction'] = state['operation']
            state['transaction_caller'] = state['caller']
        error = None
        with _lock:
            _active[self._diag_id] = state
            _start_watcher()
        try:
            return fn()
        except BaseException as exc:
            # Error codes are diagnostic; exception messages can contain values.
            error = getattr(exc, 'sqlite_errorname', type(exc).__name__)
            raise
        finally:
            ended = time.monotonic()
            # Publish a fresh snapshot: the watchdog must never iterate a dict
            # while the writer is adding acquisition/duration fields to it.
            state = dict(state)
            state['thread_cpu_ms'] = round((time.thread_time() - cpu_started) * 1000, 3)
            try:
                in_transaction = False if phase == 'close' and error is None else self.in_transaction
            except sqlite3.Error:
                in_transaction = False  # already closed: preserve the original exception
            acquired = state.get('_acquired')
            if error is None and in_transaction and acquired is None and phase != 'script':
                verb = sql.lstrip().upper()
                if verb.startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE')):
                    state['_acquired'] = ended
                    state['acquisition_sql'] = state['sql']
                    state['acquisition_caller'] = state['caller']
            if error is None and phase in {'sql', 'begin_wait', 'script'}:
                state.update(last_sql=state['sql'], last_sql_id=state['sql_id'],
                             last_caller=state['caller'], last_sql_ms=round((ended - started) * 1000, 3))
            record = _record(state, ended, 'failed' if error else 'completed', error)
            if phase == 'begin_wait':
                record['begin_wait_ms'] = record['operation_ms']
                state['begin_wait_ms'] = record['operation_ms']
            with _lock:
                if record['operation_ms'] >= _SLOW_SECONDS * 1000 or (
                    record['held_ms'] is not None and record['held_ms'] >= _SLOW_SECONDS * 1000
                    and not in_transaction
                ) or (error is not None and error.startswith(('SQLITE_BUSY', 'SQLITE_LOCKED'))):
                    _finished.append(record)
                    _wake.set()
                if in_transaction:
                    state['phase'] = 'transaction_idle'
                    state['_operation_ended'] = ended
                    self._diag_state = state
                    _active[self._diag_id] = state
                else:
                    self._diag_state = None
                    _active.pop(self._diag_id, None)

    def execute(self, sql, parameters=(), /):
        phase = 'begin_wait' if sql.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE')) else 'sql'
        return self._invoke(lambda: super(_DiagnosticMixin, self).execute(sql, parameters), sql, phase)

    def executemany(self, sql, parameters, /):
        return self._invoke(lambda: super(_DiagnosticMixin, self).executemany(sql, parameters), sql, 'sql')

    def executescript(self, script, /):
        # Scripts may contain several transactions; report whole-script time,
        # never claim a precise acquisition time for an internal BEGIN.
        return self._invoke(lambda: super(_DiagnosticMixin, self).executescript(script),
                            script, 'script')

    def cursor(self, factory=None):
        if factory is not None:
            return super().cursor(factory)
        if getattr(super().cursor, '__func__', None) is not None:
            # A caller Connection subclass may choose its own cursor (e.g. FTS
            # feature detection). Do not replace that behavioral contract.
            return super().cursor()
        return super().cursor(_DiagnosticCursor)

    def commit(self):
        return self._invoke(super().commit, 'COMMIT', 'commit')

    def rollback(self):
        return self._invoke(super().rollback, 'ROLLBACK', 'rollback')

    def close(self):
        return self._invoke(super().close, 'CLOSE', 'close')

    def __exit__(self, *exc):
        # CPython's context manager does not dispatch through overridden commit.
        phase = 'commit' if exc[0] is None else 'rollback'
        return self._invoke(lambda: super(_DiagnosticMixin, self).__exit__(*exc), phase.upper(), phase)


class _DiagnosticCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        phase = 'begin_wait' if sql.lstrip().upper().startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE')) else 'sql'
        return self.connection._invoke(lambda: super(_DiagnosticCursor, self).execute(sql, parameters), sql, phase)

    def executemany(self, sql, parameters, /):
        return self.connection._invoke(lambda: super(_DiagnosticCursor, self).executemany(sql, parameters), sql, 'sql')

    def executescript(self, script, /):
        return self.connection._invoke(lambda: super(_DiagnosticCursor, self).executescript(script), script, 'script')


def diagnostic_factory(factory=sqlite3.Connection):
    """Preserve caller Connection subclasses and the existing fd-tracking mixin."""
    if issubclass(factory, _DiagnosticMixin):
        return factory
    with _lock:
        if factory not in _factories:
            _factories[factory] = type(f'Diagnostic{factory.__name__}', (_DiagnosticMixin, factory), {})
        return _factories[factory]
