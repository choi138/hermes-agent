"""Durable SSH completion admission in the existing profile state database.

Only the existing parent agent executes verification. An uncertain running turn is
never automatically replayed: it remains visible for reconciliation.
"""
import json
import logging
import os
import re
import psutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_verification_execution = ContextVar("verification_execution", default=None)
_task_request = ContextVar("process_task_request", default='')


@contextmanager
def task_request_scope(request):
    token = _task_request.set(scrub_payload(request))
    try:
        yield
    finally:
        _task_request.reset(token)


def task_request():
    return _task_request.get()

def verification_tool_block(name, args):
    execution = _verification_execution.get()
    if execution is None:
        return None
    if name in {"read_file", "search_files"}:
        return None
    if (name in {"process", "process_manage"} and args.get("action") in {"poll", "log"}
            and args.get("session_id") == execution):
        return None
    if name in {"process", "process_manage"} and args.get("action") in {"poll", "log"}:
        return f'Wrong session_id: use session_id={execution!r} for this verification; do not inspect another execution.'
    return ("Automatic verification is read-only: this tool is blocked. "
            "Use read_file/search_files or poll/log this execution. "
            "Report approval_wait if verification requires executing commands; do not repair.")

from functools import lru_cache

from hermes_constants import get_hermes_home


@contextmanager
def _db():
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    from hermes_cli.sqlite_util import open_db
    conn = open_db(home / 'state.db', db_label="state.db (process_followups)", busy_timeout_ms=5000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS process_followups (
            execution_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
            payload TEXT NOT NULL, phase TEXT NOT NULL, token TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
            next_attempt REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '')''')
        conn.execute('CREATE TABLE IF NOT EXISTS process_followup_cancellations (session_key TEXT PRIMARY KEY, cancelled_at REAL NOT NULL)')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
    finally:
        conn.close()


def scrub_payload(value):
    from agent.redact import redact_for_egress
    if isinstance(value, str):
        return redact_for_egress(value)
    if isinstance(value, dict):
        return {key: scrub_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    return value


def reserve(session):
    from agent.redact import redact_sensitive_text, redact_terminal_output
    payload = {name: getattr(session, name) for name in (
        'session_key', 'parent_session_id', 'started_at', 'exit_code', 'completion_reason',
        'termination_source', 'task_id', 'owner_task_id', 'cancel_requested', 'cancel_confirmed', 'observation_error', 'verification_scope')}
    from tools.process_registry import ProcessRegistry, _completion_output
    payload.update(ProcessRegistry._exit_fields(session))
    completion = _completion_output(session)
    payload.update(type='completion', session_id=session.id,
                   command=redact_sensitive_text(session.command, code_file=True, force=True),
                   output=redact_terminal_output(completion['output'], session.command, force=True),
                   output_cut=completion.get('output_cut', 0),
                   notify_on_complete=True, durable_process=True)
    for name in ('platform', 'chat_id', 'user_id', 'user_name', 'thread_id', 'message_id'):
        payload[name] = getattr(session, 'watcher_' + name)
    now = time.time()
    payload['timings'] = {'completion_observed_at': now, 'execution_seconds': session.execution_seconds,
                          'execution_observed_seconds': max(0, now - session.started_at) if session.started_at else None}
    payload = scrub_payload(payload)
    phase = 'pending'
    with _db() as db:
        cancellation = db.execute('SELECT cancelled_at FROM process_followup_cancellations WHERE session_key=?',
                                  (session.session_key,)).fetchone()
        if cancellation and cancellation[0] >= session.started_at:
            phase = 'cancelled'
        db.execute('''INSERT OR IGNORE INTO process_followups
            (execution_id,parent_session_id,payload,phase,token,updated_at) VALUES (?,?,?,?,?,?)''',
            (session.id, session.parent_session_id, json.dumps(payload), phase, uuid.uuid4().hex, time.time()))


def pending():
    # Never run host probes while holding SQLite's writer lock.
    with _db() as db:
        running = [dict(row) for row in db.execute(
            "SELECT execution_id,token,owner,updated_at FROM process_followups WHERE phase IN ('running','dispatching','cancel_requested')")]
    dead = {row['owner'] for row in running if row['owner']}
    dead = {owner for owner in dead if _owner_alive(owner) is not True}
    with _db() as db:
        for row in running:
            if row['owner'] in dead or time.time() - row['updated_at'] > 900:
                db.execute("UPDATE process_followups SET phase=CASE WHEN phase='cancel_requested' THEN 'cancelled' ELSE 'needs_reconciliation' END,reason=?,updated_at=?,next_attempt=0 WHERE execution_id=? AND token=? AND owner=? AND updated_at=? AND phase IN ('running','dispatching','cancel_requested')",
                           ('Verification owner unavailable or 15-minute progress deadline exceeded; inspect effects before retrying',
                            time.time(), row['execution_id'], row['token'], row['owner'], row['updated_at']))
        rows = db.execute("SELECT * FROM process_followups WHERE phase IN ('pending','queued','needs_reconciliation','turn_finished','failed') AND next_attempt<=? ORDER BY next_attempt,updated_at LIMIT 32",
                          (time.time(),)).fetchall()
    return [dict(row) for row in rows]


def admission(execution, token):
    """Reserve a bounded retry window, with a token that also fences actual model execution."""
    now = time.time()
    with _db() as db:
        return db.execute('''UPDATE process_followups SET phase='queued',next_attempt=?,updated_at=?
            WHERE execution_id=? AND token=? AND phase IN ('pending','queued') AND next_attempt<=?''',
            (now + 30, now, execution, token, now)).rowcount == 1


def begin(execution, token):
    """Final cancellation/duplicate guard immediately before entering the model turn."""
    try:
        owner = _owner_for_pid(os.getpid())
    except (psutil.Error, OSError, ValueError) as exc:
        from tools.environments.ssh_process import safe_error
        with _db() as db:
            db.execute("UPDATE process_followups SET phase='needs_reconciliation',reason=?,updated_at=?,next_attempt=0 WHERE execution_id=? AND token=? AND phase='queued'",
                       ('Cannot establish verification owner identity; no model dispatched: ' + safe_error(exc), time.time(), execution, token))
        return False
    with _db() as db:
        return db.execute('''UPDATE process_followups SET phase='running',version=version+1,reason='Verification turn started; reconcile uncertain outcomes before retry',owner=?,updated_at=?
            WHERE execution_id=? AND token=? AND phase='queued' ''',
            (owner, time.time(), execution, token)).rowcount == 1


def finish(execution, token, result, turn_id=None):
    phase = 'failed' if result.get('failed') or result.get('interrupted') else 'turn_finished'
    reason = scrub_payload(str(result.get('failure_reason') or result.get('interrupt_message') or ''))
    with _db() as db:
        row = db.execute("SELECT payload,phase FROM process_followups WHERE execution_id=? AND token=? AND phase IN ('running','dispatching','cancel_requested','needs_reconciliation','needs_reconciliation_reported')",
                         (execution, token)).fetchone()
        if not row:
            return False
        payload = json.loads(row['payload'])
        payload['followup_turn_id'] = turn_id
        payload['verification_failure_reason'] = reason
        payload['verification'] = scrub_payload(verification_outcome(result))
        if row['phase'] == 'cancel_requested':
            phase = 'cancelled'
            reason = 'Cancelled verification result retained for inspection; no follow-up will be dispatched'
        elif row['phase'] != 'running' and row['phase'] != 'dispatching':
            phase = 'needs_reconciliation'
            reason = 'Cancellation or attention raced an admitted verification; inspect its result and effects'
        return db.execute("""UPDATE process_followups SET phase=?,reason=?,payload=?,updated_at=?,next_attempt=?
            WHERE execution_id=? AND token=? AND phase IN ('running','dispatching','cancel_requested','needs_reconciliation','needs_reconciliation_reported') """,
            (phase, reason, json.dumps(payload), time.time(), time.time() + 30, execution, token)).rowcount == 1


def report_state(execution, token, *, delivered=False, reason=''):
    with _db() as db:
        row = db.execute("SELECT payload,reason FROM process_followups WHERE execution_id=? AND token=? AND phase IN ('turn_finished','failed')", (execution, token)).fetchone()
        if not row:
            return
        payload = json.loads(row['payload'])
        outcome = payload.get('verification', {}).get('outcome', 'unverified')
        payload['report_delivered'] = delivered
        phase = 'needs_reconciliation'
        if delivered and outcome in {'verified', 'verification_failed', 'approval_wait'}:
            phase = 'reported' if outcome == 'verified' else outcome
        if phase == 'needs_reconciliation' and payload.get('followup_cancelled'):
            phase = 'cancelled'
        if phase == 'needs_reconciliation':
            failure = payload.get('verification_failure_reason') or row['reason']
            guidance = reason or 'Verification outcome missing or report undelivered; inspect evidence before retrying'
            reason = guidance if not failure or failure in guidance else failure + '; ' + guidance
        db.execute("UPDATE process_followups SET phase=?,reason=?,payload=?,updated_at=?,next_attempt=0 WHERE execution_id=? AND token=?",
                   (phase, reason, json.dumps(payload), time.time(), execution, token))


def cancel(execution):
    with _db() as db:
        db.execute("UPDATE process_followups SET payload=json_set(payload,'$.followup_cancelled',1),phase=CASE WHEN phase IN ('dispatching','cancel_requested') THEN 'cancel_requested' WHEN phase IN ('turn_finished','failed') THEN phase ELSE 'cancelled' END,version=version+1,token=CASE WHEN phase IN ('dispatching','cancel_requested','turn_finished','failed') THEN token ELSE ? END,updated_at=? WHERE execution_id=?",
                   (uuid.uuid4().hex, time.time(), execution))


def defer(execution, token, reason):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=? AND token=?', (execution, token)).fetchone()
        if row:
            failure = json.loads(row['payload']).get('verification_failure_reason')
            if failure and failure not in reason:
                reason = failure + '; ' + reason
        db.execute("UPDATE process_followups SET reason=?,updated_at=?,next_attempt=? WHERE execution_id=? AND token=? AND phase IN ('pending','queued','needs_reconciliation','turn_finished','failed')",
                   (scrub_payload(reason), time.time(), time.time() + 30, execution, token))


def cancel_for_session(session_key):
    with _db() as db:
        now = time.time()
        db.execute('INSERT OR REPLACE INTO process_followup_cancellations VALUES (?,?)', (session_key, now))
        rows = db.execute("SELECT execution_id,payload,token FROM process_followups WHERE phase IN ('pending','queued','running','dispatching','cancel_requested','turn_finished','failed','needs_reconciliation','needs_reconciliation_reported')").fetchall()
        for row in rows:
            if json.loads(row['payload']).get('session_key') == session_key:
                db.execute("UPDATE process_followups SET payload=json_set(payload,'$.followup_cancelled',1),phase=CASE WHEN phase IN ('dispatching','cancel_requested') THEN 'cancel_requested' WHEN phase IN ('turn_finished','failed') THEN phase ELSE 'cancelled' END,version=version+1,token=CASE WHEN phase IN ('dispatching','cancel_requested','turn_finished','failed') THEN token ELSE ? END,updated_at=? WHERE execution_id=?",
                           (uuid.uuid4().hex, now, row['execution_id']))
    from gateway.delivery_ledger import abandon_obligation
    for row in rows:
        if json.loads(row['payload']).get('session_key') == session_key:
            abandon_obligation('process-attention:' + row['execution_id'] + ':' + row['token'])


def get_state(execution):
    with _db() as db:
        row = db.execute('SELECT phase,reason,updated_at,payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
    if not row:
        return None
    state = dict(row)
    payload = json.loads(state.pop('payload'))
    state.update({key: payload[key] for key in ('verification', 'timings', 'report_delivered') if key in payload})
    return state


def enabled(session):
    root = getattr(session, 'remote_root', '')
    platform = getattr(session, 'watcher_platform', '')
    return bool(isinstance(root, str) and root and session.notify_on_complete
                and isinstance(platform, str) and platform and platform != 'api_server')


def authorized(execution, token):
    with _db() as db:
        return db.execute("UPDATE process_followups SET phase='dispatching' WHERE execution_id=? AND token=? AND phase='running'",
                          (execution, token)).rowcount == 1


def require_reconciliation(execution, token, *, reason='Gateway restarted during verification; inspect previous effects before retrying'):
    with _db() as db:
        db.execute("UPDATE process_followups SET phase=CASE WHEN phase='cancel_requested' THEN 'cancelled' ELSE 'needs_reconciliation' END,reason=?,updated_at=? WHERE execution_id=? AND token=? AND phase IN ('running','dispatching','cancel_requested')",
                   (scrub_payload(reason), time.time(), execution, token))


@lru_cache(maxsize=4)
def _owner_for_pid(pid):
    return json.dumps([pid, psutil.Process(pid).create_time()])


def _owner_alive(owner):
    try:
        pid, start = json.loads(owner)
        return psutil.Process(pid).create_time() == start
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError, ValueError) as exc:
        from tools.environments.ssh_process import safe_error
        logging.getLogger(__name__).warning('Verification owner probe failed: %s', safe_error(exc))
        return None  # Fence execution and surface attention; never silently assume alive.


def attention_authorized(execution, token):
    with _db() as db:
        return db.execute("SELECT 1 FROM process_followups WHERE execution_id=? AND token=? AND phase='needs_reconciliation'",
                          (execution, token)).fetchone() is not None


def acknowledge_attention(execution, token, *, delivered=True):
    with _db() as db:
        db.execute("UPDATE process_followups SET phase=?,updated_at=? WHERE execution_id=? AND token=? AND phase='needs_reconciliation'",
                   ('needs_reconciliation_reported' if delivered else 'needs_reconciliation_delivery_failed', time.time(), execution, token))


VERIFICATION_INSTRUCTION = """
Verify the completed execution against the user's requested result; exit code alone is not verification.
Do not rerun the completed command or start an automatic repair in this follow-up.
Allowed checks are read_file, search_files, and process/process_manage poll or log for this execution only.
Terminal commands and all other tools are blocked. If required evidence needs another tool or command, return approval_wait with the exact check that needs approval.
On failed verification, report the reason and concrete remaining work. Never loop repairs or bypass approval.
End the response with a single ```process_verification JSON block containing:
{"outcome":"verified","evidence":["specific checks and observed results"], "next_action":"none"}
Choose exactly one outcome: verified, verification_failed, or approval_wait.
For either non-verified outcome, replace next_action with the concrete remaining or approval action.
The automatic repair budget is zero and is recorded by the system. Only use verified with actual successful checks.
This is your recorded assessment, not an independent automated certificate.
"""


def verification_outcome(result):
    """Fail closed on missing/malformed model assessment; delivery is a separate fact."""
    fallback = dict(outcome='unverified', evidence=[], next_action='Inspect the turn and verify its effects', repair_attempts=0)
    if result.get('failed') or result.get('interrupted'):
        return fallback
    matches = re.findall(r'^```process_verification[^\S\n]*\n(.*?)\n```[^\S\n]*(?=\n|$)', result.get('final_response') or '', re.S | re.M)
    if len(matches) != 1 or not re.search(r'^```process_verification[^\S\n]*\n.*?\n```\s*\Z', result.get('final_response') or '', re.S | re.M):
        return fallback
    # A terminal-looking block inside an open Markdown fence is still a quoted example.
    text = result.get('final_response') or ''
    start = re.search(r'^```process_verification[^\S\n]*\n', text, re.M).start()
    fence = None
    for line in text[:start].splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if not marker:
            continue
        run, suffix = marker.groups()
        if fence is None:
            if run[0] != '`' or '`' not in suffix:
                fence = run
        elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
            fence = None
    if fence is not None:
        return fallback
    try:
        value = json.loads(matches[0])
        if (not isinstance(value, dict)
                or value.get('outcome') not in {'verified', 'verification_failed', 'approval_wait'}
                or not isinstance(value.get('evidence'), list) or not value['evidence']
                or not all(isinstance(item, str) and item.strip() for item in value['evidence'])
                or not isinstance(value.get('next_action'), str) or not value['next_action'].strip()
                or (value['outcome'] != 'verified' and value['next_action'].strip().lower() in {'none', 'n/a', 'null', '-', '없음'})):
            return fallback
        value = {key: value[key] for key in ('outcome', 'evidence', 'next_action')}
        value['repair_attempts'] = 0
        from agent.redact import redact_sensitive_text
        return json.loads(redact_sensitive_text(json.dumps(value), code_file=True, force=True))
    except (ValueError, TypeError):
        return fallback


def record_timings(execution, token, **values):
    """Persist measured phases without changing admission, cancellation or report state."""
    with _db() as db:
        row = db.execute("SELECT payload FROM process_followups WHERE execution_id=? AND token=?", (execution, token)).fetchone()
        if row:
            payload = json.loads(row['payload'])
            timings = payload.setdefault('timings', {})
            if 'verification_started_at' in values:
                values['verification_wait_seconds'] = max(0, values['verification_started_at'] - timings.get('completion_observed_at', values['verification_started_at']))
            timings.update(values)
            db.execute("UPDATE process_followups SET payload=? WHERE execution_id=? AND token=?", (json.dumps(payload), execution, token))


@contextmanager
def verification_lease(execution, token, interval=30):
    """Keep a live model/approval wait distinct from an abandoned verification."""
    import logging
    import threading
    from agent.memory_provider import spawn_context_thread
    stopped = threading.Event()
    owner_thread = threading.current_thread()
    def refresh():
        with _db() as db:
            db.execute("UPDATE process_followups SET updated_at=? WHERE execution_id=? AND token=? AND phase IN ('running','dispatching','cancel_requested')",
                       (time.time(), execution, token))
    def heartbeat():
        while not stopped.wait(interval) and owner_thread.is_alive():
            try:
                refresh()
            except Exception as exc:
                from tools.environments.ssh_process import safe_error
                logging.getLogger(__name__).warning('Verification lease refresh failed: %s: %s', execution, safe_error(exc))
    refresh()
    worker = spawn_context_thread(heartbeat, name='verification-lease-' + execution)
    worker.start()
    policy_token = _verification_execution.set(execution)
    try:
        yield
    finally:
        _verification_execution.reset(policy_token)
        stopped.set()
        worker.join(timeout=1)
