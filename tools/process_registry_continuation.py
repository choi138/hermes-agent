"""Launch-time authority and durable fences for bounded process continuations."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, copy_context

from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from tools.process_registry_followups import _db, _owner_alive, _owner_for_pid

_turn = ContextVar('process_continuation_turn', default=None)
_requested = ContextVar('process_continuation_requested', default=False)
_FINISHED = {'turn_finished', 'reported', 'approval_wait', 'verification_failed'}
_failed_fences = {}
_pending_fences = {}
_received_fences = {}
_live_authorities = {}
_fence_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _agent_config():
    from hermes_cli.config_effective import load_user_config_effective
    return load_user_config_effective(fail_closed=True).get('agent', {})


def settings():
    cfg = _agent_config()
    return (cfg.get('process_continue_on_complete') is True,
            max(0, int(cfg.get('process_continue_max_chain', 3))),
            max(0, int(cfg.get('process_continue_max_age_seconds', 3600))))


def record_user_event(key, event):
    """Record admission, including messages queued before they enter a transcript."""
    if event.internal or getattr(event, '_process_user_recorded', False):
        return
    now = getattr(event, '_process_user_received_at', None)
    if now is None:
        now = time.time()
    event._process_user_received_at = now
    _record_fence(key, now, user=True, stop=event.get_command() == 'stop')
    event._process_user_recorded = True


def record_stop(key):
    _record_fence(key, time.time(), stop=True)


def _record_fence(key, received_at, *, user=False, stop=False):
    identity = (str(get_hermes_home()), key)
    token = object()
    try:
        enabled = _agent_config().get('process_continue_on_complete') is True
    except Exception:
        _finish_fence(identity, token, received_at, failed=True)
        logger.warning('Could not read continuation config for %s; prior authority quarantined',
                       key, exc_info=True)
        return
    with _fence_lock:
        previous_user, previous_stop = _received_fences.get(identity, (0, 0))
        _received_fences[identity] = (max(previous_user, received_at if user else 0),
                                      max(previous_stop, received_at if stop else 0))
        if not enabled and not _live_authorities.get(identity):
            return
        _pending_fences.setdefault(identity, {})[token] = received_at
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _persist_fence(identity, token, received_at, user=user, stop=stop)
        return
    try:
        context = copy_context()
        loop.run_in_executor(None, lambda: context.run(
            _persist_fence, identity, token, received_at, user=user, stop=stop))
    except Exception:
        _finish_fence(identity, token, received_at, failed=True)
        logger.warning('Could not schedule continuation fence for %s; prior authority quarantined',
                       key, exc_info=True)


def _finish_fence(identity, token, received_at, *, failed=False):
    with _fence_lock:
        pending = _pending_fences.get(identity, {})
        pending.pop(token, None)
        if not pending:
            _pending_fences.pop(identity, None)
        if failed:
            _failed_fences[identity] = max(received_at, _failed_fences.get(identity, 0))


def _persist_fence(identity, token, received_at, *, user, stop):
    home_token = set_hermes_home_override(identity[0])
    try:
        with _db(busy_timeout_ms=100) as db:
            db.execute('''INSERT INTO process_followup_cancellations
                (session_key,cancelled_at,user_received_at,stopped_at) VALUES (?,0,?,?)
                ON CONFLICT(session_key) DO UPDATE SET
                user_received_at=MAX(user_received_at,excluded.user_received_at),
                stopped_at=MAX(stopped_at,excluded.stopped_at)''',
                (identity[1], received_at if user else 0, received_at if stop else 0))
    except Exception:
        _finish_fence(identity, token, received_at, failed=True)
        logger.warning('Could not persist continuation fence for %s; prior authority quarantined',
                       identity[1], exc_info=True)
    else:
        _finish_fence(identity, token, received_at)
    finally:
        reset_hermes_home_override(home_token)


def track_authority(execution, payload):
    identity = (str(get_hermes_home()), payload.get('session_key'))
    with _fence_lock:
        live = _live_authorities.setdefault(identity, set())
        if payload.get('continue_on_complete') and payload.get('continuation_status', {}).get('phase') not in {
                'done', 'skipped', 'uncertain'}:
            live.add(execution)
        else:
            live.discard(execution)
        if not live:
            _live_authorities.pop(identity, None)


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
    return bool(session.remote_root or session.continue_on_complete)


def arm_session(session):
    """Called by the registry BEFORE dispatch, including fast local processes."""
    if not _requested.get() or arming_reason():
        return
    authority = _turn.get()
    if authority['session_key'] != session.session_key:
        return
    session.continue_on_complete = True
    session.continuation = {**authority, 'launched_at': session.started_at}
    track_authority(session.id, {'session_key': session.session_key, 'continue_on_complete': True})
    session.notify_on_complete = True
    from gateway.session_context import get_session_env
    from tools.terminal_tool_background import _stamp_gateway_routing
    from tools.process_registry_followups import task_request
    _stamp_gateway_routing(session, get_session_env)
    session.verification_scope = {'cwd': session.cwd, 'task_request': task_request()}


def _skip_reason(db, row, payload):
    gate, cap, age = settings()
    auth = payload.get('continuation') or {}
    if not gate:
        return 'config gate disabled'
    if row['phase'] not in _FINISHED or payload.get('followup_cancelled'):
        return 'follow-up cancelled, failed, or needs reconciliation'
    if payload.get('verification', {}).get('outcome') not in {'verified', 'verification_failed', 'approval_wait'}:
        return 'verification needs reconciliation'
    if auth.get('chain', cap) >= cap:
        return 'chain cap reached'
    if chain_count(db, auth.get('chain_id')) >= cap:
        return 'chain cap reached'
    completed = payload.get('timings', {}).get('completion_observed_at', 0)
    if time.time() - completed > age:
        return 'completion too old'
    mark = db.execute('SELECT * FROM process_followup_cancellations WHERE session_key=?',
                      (payload.get('session_key'),)).fetchone()
    origin = auth.get('origin_received_at')
    if not origin:
        return 'originating user turn is unknown'
    with _fence_lock:
        identity = (str(get_hermes_home()), payload.get('session_key'))
        failed = max(_failed_fences.get(identity, 0),
                     max(_pending_fences.get(identity, {}).values(), default=0))
        received_user, received_stop = _received_fences.get(identity, (0, 0))
    if failed >= origin:
        return 'continuation fence persistence failed'
    if received_stop >= origin or (mark and max(mark['stopped_at'], mark['cancelled_at']) >= origin):
        return 'session stopped or cancelled after originating turn'
    if received_user > origin or (mark and mark['user_received_at'] > origin):
        return 'newer user message'
    return ''


def chain_count(db, chain_id):
    return db.execute("""SELECT COUNT(*) FROM process_followups
        WHERE json_extract(payload,'$.continuation.chain_id')=?
        AND json_extract(payload,'$.continuation_status.phase') IN ('running','done','uncertain')""",
        (chain_id,)).fetchone()[0]


def _save(db, execution, payload):
    db.execute('UPDATE process_followups SET payload=? WHERE execution_id=?', (json.dumps(payload), execution))
    track_authority(execution, payload)


def candidates():
    """Recover only unexecuted queue claims. Running effects are never replayed."""
    with _db(read_only=True) as db:
        rows = db.execute("""SELECT * FROM process_followups
            WHERE json_extract(payload,'$.continue_on_complete')=1
            AND COALESCE(json_extract(payload,'$.continuation_status.phase'),'ready')
                NOT IN ('running','done','skipped','uncertain')""").fetchall()
    for row in rows:
        track_authority(row['execution_id'], json.loads(row['payload']))
    return [dict(row) for row in rows]


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
        status = payload.get('continuation_status', {})
        if status.get('phase') in {'running', 'done', 'skipped', 'uncertain'}:
            return None
        if status.get('phase') == 'queued' and _owner_alive(status['owner']) is not False:
            return None
        if row['phase'] in {'pending', 'queued', 'running', 'dispatching'}:
            return None  # verification has not finished
        reason = _skip_reason(db, row, payload)
        if reason:
            payload['continuation_status'] = {'phase': 'skipped', 'reason': reason}
            _save(db, execution, payload)
            return None
        if (payload['session_key'], payload.get('followup_turn_id')) != (
                observed['session_key'], observed.get('followup_turn_id')):
            return None
        if delivery_state not in {'pending', 'delivered'}:
            return None
        token = status.get('token') or uuid.uuid4().hex
        payload['continuation_status'] = {'phase': 'queued', 'token': token, 'owner': _owner_for_pid(os.getpid())}
        _save(db, execution, payload)
        return {**payload, 'process_continuation': {'execution_id': execution, 'token': token}}


def admission_failed(execution, token, *, uncertain=False):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') == token and status.get('phase') == 'queued':
            status['phase'] = 'uncertain' if uncertain else 'ready'
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
        reason = _skip_reason(db, row, payload)
        status.update(phase='skipped' if reason else 'running', reason=reason)
        _save(db, execution, payload)
        if reason:
            return None
        auth = payload['continuation']
        return {**auth, 'chain': auth['chain'] + 1}


def finish(execution, token):
    with _db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        payload = json.loads(row['payload'])
        status = payload.get('continuation_status', {})
        if status.get('token') == token and status.get('phase') == 'running':
            status['phase'] = 'done'
            _save(db, execution, payload)
