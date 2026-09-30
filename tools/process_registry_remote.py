"""SSH observation lifecycle, separate from command execution and file synchronization."""
import codecs
import logging
import random
import time

logger = logging.getLogger('tools.process_registry')


def poll_remote(registry, session, env):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    failures = 0
    previous = ''
    log_identity = None
    skipped = False
    outage_notified = False
    cancel_error = None
    persisted_at = 0.0
    persisted_state = None
    terminal_reads = 0
    terminal_size = None
    from tools.environments.ssh_process import ObservationCapacityBusy
    while not session.exited:
        session._observation_wake.clear()
        observation = {}
        failure_operation = 'observe'
        try:
            with session._finalize_lock:
                cancel_requested = session.cancel_requested
            if cancel_requested:
                from tools.environments.ssh_process import request_cancel
                try:
                    request_cancel(env, session.remote_root, session.id)
                    cancel_error = None
                except Exception as exc:
                    # A failed cancellation write must not prevent reading an existing receipt.
                    from tools.environments.ssh_process import safe_error
                    detail = safe_error(exc)
                    if cancel_error != detail:
                        logger.warning('Remote cancellation unconfirmed: %s: %s', session.id, detail)
                    cancel_error = detail
            session.observation_operation = 'ssh_observe'
            observation = env._observe_process(session.remote_root, session.id, session.remote_offset)
            operation = observation.get('operation')
            if operation in {'identity', 'receipt', 'process_identity', 'log', 'wrapper'}:
                session.observation_operation = operation
            state = observation['state']
            identity = observation.get('identity')
            if identity:
                if session.remote_identity and session.remote_identity != identity:
                    state = 'identity_mismatch'
                elif session.pid is not None and session.pid != identity['pid']:
                    state = 'identity_mismatch'
                elif not session.remote_identity:
                    session.remote_identity = identity
                    session.pid = identity['pid']
                    failure_operation = 'checkpoint'
                    registry._write_checkpoint()
                    failure_operation = 'observe'
            session.observation_state = state
            if state != previous:
                logger.info('Remote observation %s: %s -> %s', session.id, previous, state)
                previous = state
            if state in {'unavailable', 'identity_mismatch'}:
                raise ConnectionError(observation.get('error', state))
            failures = 0
            outage_notified = False
            session.observation_error = observation.get('error', '') if observation.get('error', '').startswith('log_unavailable') else ''
            if cancel_error and not observation.get('cancel_confirmed'):
                session.observation_error = '; '.join(filter(None,
                    (session.observation_error, 'cancel write unconfirmed: ' + cancel_error)))[:500]
            session.last_observed_at = time.time()
            chunk = observation.get('log')
            if state == 'exited':
                terminal_reads += 1
                if terminal_size is None and chunk:
                    terminal_size = chunk['size']
            if chunk:
                new_identity = chunk.get('identity')
                if log_identity is not None and new_identity != log_identity and terminal_reads < 4:
                    session.remote_offset = 0
                    log_identity = new_identity
                    decoder.reset()
                    terminal_size = chunk['size'] if state == 'exited' else None
                    registry._ingest_output(session, '\n[remote log replaced; reading new file]\n')
                    continue
                log_identity = new_identity
                if chunk['offset'] < session.remote_offset:
                    decoder.reset()
                    with session._lock:
                        session.output_buffer = ''
                raw = chunk['bytes']
                if skipped:
                    while raw and raw[0] & 0xc0 == 0x80:
                        raw = raw[1:]
                    skipped = False
                text = decoder.decode(raw)
                if text:
                    registry._ingest_output(session, text)
                session.remote_offset = chunk['next']
            if state == 'exited':
                session.cancel_confirmed = observation.get('cancel_confirmed') is True
                elapsed = observation.get('execution_seconds')
                import math
                session.execution_seconds = elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None
                # Retain the bounded tail without allowing a large backlog to hold exit hostage.
                if chunk and chunk['next'] < terminal_size and terminal_reads < 4:
                    session.remote_offset = max(chunk['next'], terminal_size - 65536)
                    if session.remote_offset > chunk['next']:
                        registry._ingest_output(session, '\n[remote log backlog omitted; showing final 64 KiB]\n')
                        skipped = True
                        decoder.reset()
                    continue
                if observation.get('startup_error'):
                    from tools.environments.ssh_process import safe_error
                    registry._ingest_output(session, '\n[Remote child failed to start: ' + safe_error(RuntimeError(str(observation['startup_error']))) + ']\n')
                tail = decoder.decode(b'', final=True)
                if tail:
                    registry._ingest_output(session, tail)
                try:
                    failure_operation = 'finalize'
                    registry._finish_exited(session, observation['exit_code'])
                except Exception:
                    # Do not let a failed receipt/outbox write erase the recoverable execution.
                    session.exited = False
                    decoder.reset()
                    raise
                return
        except ObservationCapacityBusy:
            # Local scheduling is not evidence of a remote outage.
            session._observation_wake.wait(.5)
            continue
        except Exception as exc:
            failures += 1
            category = observation.get('error')
            if not isinstance(category, str) or not category.startswith(('invalid_receipt', 'invalid_identity_or_receipt', 'log_unavailable')):
                category = None
            from tools.environments.ssh_process import safe_error
            detail = f"{failure_operation}: {safe_error(exc)}; retry scheduled"
            if category:
                detail += '; remote observation: ' + safe_error(RuntimeError(category))
            session.observation_error = detail[:500]
            if session.observation_state != 'identity_mismatch':
                session.observation_state = 'unavailable'
            if failures >= 5 and not outage_notified and session.notify_on_complete:
                registry.completion_queue.put({
                    'type': 'observation_unavailable', 'session_id': session.id,
                    'session_key': session.session_key, 'parent_session_id': session.parent_session_id,
                    **{key: getattr(session, 'watcher_' + key) for key in
                       ('platform', 'chat_id', 'user_id', 'user_name', 'thread_id', 'message_id')},
                    'message': f'Cannot observe remote process {session.id}; state is {session.observation_state}. '
                               'It remains tracked and observation will retry automatically. '
                               'Report the waiting state; do not repeat the original command or claim it failed.'})
                outage_notified = True
            if failures == 1 or failures % 10 == 0:
                logger.warning('Remote observation unavailable: execution=%s failures=%s last_ok=%s error=%s',
                               session.id, failures, session.last_observed_at, session.observation_error)
        delay = min(60, 2 ** min(failures + 1, 6)) if failures else 2
        # A cancellation wakes observation without falsely signalling completion.
        delay *= random.uniform(.8, 1.2)
        session.observation_retry_at = time.time() + delay
        state_key = (session.observation_state, session.observation_error, session.observation_operation)
        now = time.monotonic()
        if state_key != persisted_state or now - persisted_at >= 30:
            registry._write_checkpoint()
            persisted_state, persisted_at = state_key, now
        session._observation_wake.wait(delay)


def recover_remote(registry, entry):
    from hermes_constants import get_hermes_home
    from tools.environments.ssh import SSHEnvironment
    from tools.process_registry import ProcessSession, _CHECKPOINT_FIELDS, _CHECKPOINT_DEFAULTS
    connection = entry.get('remote_connection') or {}
    if not entry.get('remote_root') or connection.get('profile_home') != str(get_hermes_home()):
        return None
    env = SSHEnvironment(**{name: connection[name] for name in ('host', 'user', 'port', 'key_path')},
                         _status_only=True)
    if not env._connection_identity:
        raise ConnectionError('SSH configuration resolution unavailable; recovery deferred: ' + env._configuration_error)
    if env._connection_identity != connection.get('identity'):
        raise ValueError('SSH configuration identity changed; manual reconciliation required')
    fields = {name: entry.get(name, _CHECKPOINT_DEFAULTS[name]) for name in _CHECKPOINT_FIELDS}
    # Re-read logs from zero after restart: incremental decoder state is not checkpointed.
    fields['remote_offset'] = 0
    fields['observation_operation'] = 'ssh_observe'
    fields['observation_state'] = 'unavailable'
    fields['command'] = entry.get('command', 'unknown')
    with registry._lock:
        previous = registry._running.get(entry['session_id'])
    if previous is not None and previous.observation_operation == 'ssh_recovery':
        # Cancellation can finish while SSH configuration is reconstructed. Keep
        # the live placeholder itself: a concurrent killer may already hold it.
        # Copying a flag into a replacement would still lose a later accepted write.
        session = previous
        with session._finalize_lock:
            session.env_ref = env
            session.detached = False
            session.remote_offset = 0
            session.observation_operation = 'ssh_observe'
            session.observation_state = 'unavailable'
    else:
        session = ProcessSession(id=entry['session_id'], env_ref=env, **fields)
    from functools import partial
    registry._track_started(session, partial(poll_remote, registry), f'proc-poller-{session.id}', (env,))
    return session
