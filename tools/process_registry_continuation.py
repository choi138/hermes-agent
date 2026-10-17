"""Launch-time authority, stop receipts and durable process continuation ownership."""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from hermes_constants import get_hermes_home
from tools.process_registry_followups import _db, _owner_alive, _owner_for_pid

_turn = ContextVar('process_continuation_turn', default=None)
_requested = ContextVar('process_continuation_requested', default=False)
_FINISHED = {'turn_finished', 'reported', 'approval_wait', 'verification_failed'}
_stops = {}
_failures = {}
_fence_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _agent_config():
    from hermes_cli.config_effective import load_user_config_effective
    return load_user_config_effective(fail_closed=True).get('agent', {})


def settings():
    cfg = _agent_config()
    return (cfg.get('process_continue_on_complete') is True,
            max(0, int(cfg.get('process_continue_max_chain', 10))),
            max(0, int(cfg.get('process_continue_max_age_seconds', 3600))))


def record_user_event(key, event):
    """Preserve receipt time through queue, steer and resend without revoking authority."""
    if event.internal or getattr(event, '_process_user_recorded', False):
        return
    now = getattr(event, '_process_user_received_at', None)
    if now is None:
        now = time.time()
    event._process_user_received_at = now
    event._process_user_recorded = True


def receive_stop(key):
    """Quarantine immediately, before yielding to the persistence worker."""
    identity = (str(get_hermes_home()), key)
    received_at = time.time()
    with _fence_lock:
        _stops[identity] = max(received_at, _stops.get(identity, 0))
    return received_at


def record_stop(key, received_at=None):
    """Durable SQLite-independent stop receipt. Gateway callers run this off-loop."""
    home = get_hermes_home()
    if received_at is None:
        received_at = receive_stop(key)
    try:
        if _agent_config().get('process_continue_on_complete') is not True:
            return True
        path = _stop_path(key)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(received_at) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        from utils import fsync_directory
        fsync_directory(home)
        return True
    except Exception:
        logger.warning('Could not persist continuation stop for %s; authority quarantined', key, exc_info=True)
        return False


def stop_write_failures(key, received_at):
    with _db(read_only=True) as db:
        rows = db.execute("""SELECT execution_id,payload FROM process_followups
            WHERE json_extract(payload,'$.session_key')=? AND json_extract(payload,'$.continue_on_complete')=1
            AND json_extract(payload,'$.continuation.origin_received_at')<=?
            AND COALESCE(json_extract(payload,'$.continuation_status.phase'),'ready') NOT IN ('done','skipped','uncertain')
            AND COALESCE(json_extract(payload,'$.continuation_status.notice_delivered'),0)=0""",
            (key, received_at)).fetchall()
    return [(row['execution_id'], json.loads(row['payload'])) for row in rows]


def _stop_path(key):
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return get_hermes_home() / f'continuation-stop-{digest}.jsonl'


def stopped_after(key, origin):
    with _fence_lock:
        if _stops.get((str(get_hermes_home()), key), 0) >= origin:
            return True
    try:
        with _stop_path(key).open(encoding='utf-8') as stream:
            return any(float(json.loads(line)) >= origin for line in stream)
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


@contextmanager
def turn_scope(event, key, continuation=None):
    real_user = event is not None and not event.internal
    metadata = (getattr(event, 'metadata', None) or {})
    received_at = getattr(event, '_process_user_received_at', None)
    authority = continuation if continuation is not None else (
        {'chain': 0, 'chain_id': uuid.uuid4().hex,
         'origin_received_at': received_at if received_at is not None else event.timestamp.timestamp()}
        if real_user and not metadata.get('process_followup') else None)
    token = _turn.set({**authority, 'session_key': key} if authority is not None else None)
    try:
        yield
    finally:
        _turn.reset(token)


@contextmanager
def launch_scope(requested):
    token = _requested.set(requested)
    try:
        yield
    finally:
        _requested.reset(token)


def arming_reason():
    if not settings()[0]:
        return 'disabled by agent.process_continue_on_complete (accepted, not armed)'
    from tools.process_registry_followups import _verification_execution
    if _verification_execution.get() is not None:
        return 'not armed: requires a normal user-originated turn or authorized continuation'
    from gateway.session_context import async_delivery_supported, get_session_env
    surface = get_session_env('HERMES_SESSION_PLATFORM', '') or get_session_env('HERMES_SESSION_SOURCE', '') or 'CLI'
    if _turn.get() is None:
        return f'not armed: gateway-only continuation; {surface} session has no authorized gateway turn'
    with _db(read_only=True) as db:
        used = chain_count(db, _turn.get().get('chain_id'))
    if max(_turn.get()['chain'], used) >= settings()[1]:
        return 'not armed: process_continue_max_chain reached'
    from agent.delegation_context import is_delegated_child_context
    if not async_delivery_supported():
        return f'not armed: gateway-only continuation; {surface} session cannot receive independent delivery'
    if is_delegated_child_context():
        return 'not armed: delegated child session cannot receive an independent continuation'
    return ''


def launch_result(session, requested):
    if not requested:
        return {}
    return {'continue_on_complete': session.continue_on_complete,
            'continuation_note': 'armed at launch' if session.continue_on_complete else arming_reason()}


def durable_launch(session):
    return bool(session.remote_root or session.continue_on_complete or session.continuation.get('refused_reason'))


def arm_session(session):
    """Called by the registry BEFORE dispatch, including fast local processes."""
    if not _requested.get():
        return
    reason = arming_reason()
    refused = reason == 'not armed: process_continue_max_chain reached'
    if reason and not refused:
        return
    authority = _turn.get()
    if authority['session_key'] != session.session_key:
        return
    session.continue_on_complete = not refused
    session.continuation = {**authority, 'launched_at': session.started_at}
    session.notify_on_complete = True
    from gateway.session_context import get_session_env
    from tools.terminal_tool_background import _stamp_gateway_routing
    from tools.process_registry_followups import task_request
    _stamp_gateway_routing(session, get_session_env)
    session.verification_scope = {'cwd': session.cwd, 'task_request': task_request()}
    if refused:
        session.continuation['refused_reason'] = 'chain cap reached'
        from tools.process_registry_followups import reserve
        reserve(session)


def _skip_reason(db, row, payload):
    gate, cap, _age = settings()
    auth = payload.get('continuation') or {}
    if not gate:
        return 'config gate disabled'
    reason = _stop_reason(db, payload)
    if reason:
        return reason
    if row['phase'] not in _FINISHED or payload.get('followup_cancelled'):
        return 'follow-up cancelled, failed, or needs reconciliation'
    if payload.get('verification', {}).get('outcome') not in {'verified', 'verification_failed', 'approval_wait'}:
        return 'verification needs reconciliation'
    if auth.get('chain', cap) >= cap:
        return 'chain cap reached'
    if chain_count(db, auth.get('chain_id'), excluding=row['execution_id']) >= cap:
        return 'chain cap reached'
    return ''


def _freshness_reason(payload):
    status = payload.get('continuation_status', {})
    if status.get('resume_count'):
        from gateway.run import _auto_continue_freshness_window, _is_fresh_gateway_interruption
        fresh = _is_fresh_gateway_interruption(status.get('interrupted_at'), now=time.time(),
            window_secs=_auto_continue_freshness_window())
        reason = '게이트웨이가 너무 오래 멈춰 있어서 자동 재개를 멈췄어'
    else:
        completed = payload.get('timings', {}).get('completion_observed_at', 0)
        fresh = time.time() - completed <= settings()[2]
        reason = 'completion too old'
    if fresh:
        return ''
    return '게이트웨이가 채팅에 연결되지 않아 이어 가기를 시작하지 못했어' if status.get('admission_retry') else reason


def remember_interruption(record):
    marker = record.get('process_continuation') or {}
    if not marker:
        return
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (marker['execution_id'],)).fetchone()
        if not row:
            return
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') == marker.get('token') and status.get('phase') == 'running':
            status['interrupted_at'] = record.get('failed_at') or record.get('interrupted_at') or record.get('started_at')
            _save(db, marker['execution_id'], payload)


def _stop_reason(db, payload):
    auth = payload.get('continuation') or {}
    origin = auth.get('origin_received_at')
    if not origin:
        return 'originating user turn is unknown'
    mark = db.execute('SELECT * FROM process_followup_cancellations WHERE session_key=?',
                      (payload.get('session_key'),)).fetchone()
    if stopped_after(payload['session_key'], origin) or (mark and max(mark['stopped_at'], mark['cancelled_at']) >= origin):
        return 'session stopped or cancelled after originating turn'
    return ''


def chain_count(db, chain_id, *, excluding=''):
    return db.execute("""SELECT COUNT(*) FROM process_followups
        WHERE json_extract(payload,'$.continuation.chain_id')=?
        AND execution_id!=?
        AND (json_extract(payload,'$.continuation_status.phase') IN ('running','done','uncertain')
            OR (json_extract(payload,'$.continuation_status.phase') IN ('queued','ready')
                AND json_extract(payload,'$.continuation_status.resume_count')=1))""",
        (chain_id, excluding)).fetchone()[0]


def _save(db, execution, payload):
    db.execute('UPDATE process_followups SET payload=? WHERE execution_id=?', (json.dumps(payload), execution))


def record_failure(execution, reason):
    with _fence_lock:
        _failures[(str(get_hermes_home()), execution)] = reason
    try:
        path = _failure_path(execution)
        with path.open('w', encoding='utf-8') as stream:
            json.dump(reason, stream)
            stream.flush()
            os.fsync(stream.fileno())
        from utils import fsync_directory
        fsync_directory(path.parent)
    except OSError:
        logger.warning('Could not persist continuation failure for %s', execution, exc_info=True)


def _failure_path(execution):
    digest = hashlib.sha256(execution.encode('utf-8')).hexdigest()
    return get_hermes_home() / f'continuation-failure-{digest}.json'


def _failure_reason(execution):
    with _fence_lock:
        reason = _failures.get((str(get_hermes_home()), execution))
    if reason:
        return reason
    try:
        return json.loads(_failure_path(execution).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return ''
    except (OSError, ValueError):
        return 'continuation fence persistence failed'


def candidates():
    """Reconcile pending authority and interrupted claims through their execution fence."""
    with _db(read_only=True) as db:
        rows = db.execute("""SELECT * FROM process_followups
            WHERE json_extract(payload,'$.continue_on_complete')=1
            AND COALESCE(json_extract(payload,'$.continuation_status.phase'),'ready')
                NOT IN ('done','skipped','uncertain')""").fetchall()
    return [dict(row) for row in rows]


def _recover_claim(status):
    phase = status.get('phase')
    if phase in {'done', 'skipped', 'uncertain'}:
        return False
    if phase not in {'queued', 'running'}:
        return True
    owner = status.get('owner')
    if not owner or owner == _owner_for_pid(os.getpid()) or _owner_alive(owner) is not False:
        return False
    if phase == 'running':
        if status.get('resume_count', 0):
            status.update(phase='uncertain', reason='게이트웨이 재시작이 반복돼 자동 재개를 멈췄어')
            return False
        status['resume_count'] = 1
    return True


def claim(execution):
    """Persist queue ownership before admission; duplicate events share one execution fence."""
    with _db(read_only=True) as db:
        snapshot = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
    if not snapshot:
        return None
    observed = json.loads(snapshot['payload'])
    from gateway.delivery_ledger import turn_delivery_state
    delivery_state = turn_delivery_state(observed['session_key'], observed.get('followup_turn_id'))
    with _db() as db:
        row = db.execute('SELECT * FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload'])
        status = payload.setdefault('continuation_status', {})
        failure = _failure_reason(execution)
        if failure:
            payload['continuation_status'] = {**status, 'phase': 'uncertain', 'reason': failure}
            _save(db, execution, payload)
            return None
        if status.get('phase') in {'done', 'skipped', 'uncertain'}:
            return None
        reason = _stop_reason(db, payload) if settings()[0] else ''
        if reason:
            payload['continuation_status'] = {**status, 'phase': 'skipped', 'reason': reason}
            _save(db, execution, payload)
            return None
        if not _recover_claim(status):
            if status.get('phase') == 'uncertain':
                _save(db, execution, payload)
            return None
        if row['phase'] in {'pending', 'queued', 'running', 'dispatching'}:
            return None  # verification has not finished
        reason = _failure_reason(execution) or _skip_reason(db, row, payload) or _freshness_reason(payload)
        if reason:
            payload['continuation_status'] = {**status, 'phase': 'skipped', 'reason': reason}
            _save(db, execution, payload)
            return None
        if (payload['session_key'], payload.get('followup_turn_id')) != (
                observed['session_key'], observed.get('followup_turn_id')):
            return None
        if delivery_state is None:
            status['phase'] = 'ready'
            _save(db, execution, payload)
            return None
        if delivery_state not in {'pending', 'delivered'}:
            payload['continuation_status'] = {'phase': 'skipped', 'reason': 'verification report needs reconciliation'}
            _save(db, execution, payload)
            return None
        token = uuid.uuid4().hex
        payload['continuation_status'] = {**status, 'phase': 'queued', 'token': token,
            'owner': _owner_for_pid(os.getpid()), 'resume_count': status.get('resume_count', 0)}
        _save(db, execution, payload)
        return {**payload, 'process_continuation': {'execution_id': execution, 'token': token}}


def admission_failed(execution, token, *, uncertain=False, retryable=False):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') == token and status.get('phase') == 'queued':
            if retryable:
                status.update(phase='ready', admission_retry=True)
            else:
                status.update(phase='uncertain' if uncertain else 'skipped', reason='continuation admission failed')
            _save(db, execution, payload)


def begin(execution, token, key):
    with _db() as db:
        row = db.execute('SELECT * FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        if not row:
            return None
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') != token or status.get('phase') != 'queued' or payload.get('session_key') != key:
            return None
        reason = _failure_reason(execution) or _skip_reason(db, row, payload)
        status.pop('admission_retry', None)
        status.update(phase='skipped' if reason else 'running', reason=reason)
        _save(db, execution, payload)
        if reason:
            return None
        auth = payload['continuation']
        return {**auth, 'chain': auth['chain'] + 1}


def finish(execution, token, *, reason=''):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') == token and status.get('phase') == 'running':
            status.update(phase='uncertain' if reason else 'done', reason=reason)
            _save(db, execution, payload)


def interrupt(execution, token, reason):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        if row:
            payload = json.loads(row['payload'])
            status = payload.get('continuation_status', {})
            if status.get('token') == token and status.get('phase') in {'queued', 'running'}:
                status.update(phase='uncertain', reason=reason)
                _save(db, execution, payload)


def stopped_executions():
    with _db(read_only=True) as db:
        rows = db.execute("""SELECT execution_id,payload FROM process_followups
            WHERE json_extract(payload,'$.continuation_status.phase') IN ('skipped','uncertain')
            AND COALESCE(json_extract(payload,'$.continuation_status.notice_delivered'),0)=0""").fetchall()
    return [(row['execution_id'], json.loads(row['payload'])) for row in rows]


def notice_pending(execution, *, connection):
    return connection.execute("""SELECT 1 FROM process_followups WHERE execution_id=?
        AND COALESCE(json_extract(payload,'$.continuation_status.notice_delivered'),0)=0""",
        (execution,)).fetchone() is not None


def acknowledge_notice(execution, *, connection):
    """Keep the delivery tombstone in the same transaction, beyond ledger retention."""
    connection.execute("""UPDATE process_followups
        SET payload=json_set(payload,'$.continuation_status.notice_delivered',1)
        WHERE execution_id=?""", (execution,))
