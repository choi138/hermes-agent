"""Owned SSH CLI launch and progress delivery on the existing reconciliation tick."""
import asyncio
import json
import logging
import re
import shlex
import time
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)

def _cause(exc):
    value = str(exc)
    return value if re.fullmatch(r'progress_[a-z_]+', value) else type(exc).__name__

_last_poll = {}
_sends = set()
_poll_tasks = {}


def _canonical_temp_root(temp_dir, resolve_root):
    try:
        root = resolve_root(temp_dir)
    except Exception:
        logger.warning('progress_temp_root_resolution_failed')
        return None
    if isinstance(root, str):
        root = root.removesuffix('\n')
    if (not isinstance(root, str) or not re.fullmatch(r'/[A-Za-z0-9._/-]*', root)
            or '..' in root.split('/')):
        logger.warning('progress_temp_root_invalid')
        return None
    return root


def prepare_launch(command, session, temp_dir, *, resolve_root=None):
    # Accept one argv, never parse/rewrite arbitrary shell programs or monitor jobs.
    try:
        if any(char in command for char in '$`\n\r~'):
            return command, {}
        quote, escaped = None, False
        for char in command:
            if escaped:
                escaped = False
            elif char == '\\' and quote != "'":
                escaped = True
            elif char == quote:
                quote = None
            elif quote is None and char in "\"'":
                quote = char
            elif quote is None and char in '*?[{()}#':
                return command, {}
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>\n')
        lexer.whitespace_split = True
        lexer.commenters = ''
        argv = list(lexer)
        if any(word and all(char in ';&|<>' for char in word) for word in argv):
            return command, {}
        if (len(argv) < 3 or not re.fullmatch(r'python(?:3(?:\.\d+)?)?', PurePosixPath(argv[0]).name)
                or not argv[1].startswith('/') or not argv[1].endswith('/scripts/run_codex_task.py')
                or any(word.startswith('--progress-') and word.split('=', 1)[0] not in
                       {'--progress-code-scope', '--progress-sender-profile', '--progress-expected-bot-id', '--progress-login-shell'}
                       for word in argv[2:])
                or '--dry-run' in argv or argv[2] in {'lifecycle', 'progress'}
                or session.watcher_platform != 'discord'):
            return command, {}
        thread = session.watcher_thread_id or session.watcher_chat_id
        if not re.fullmatch(r'[0-9]{1,22}', thread):
            return command, {}
    except ValueError:
        return command, {}
    root = _canonical_temp_root(temp_dir, resolve_root)
    if root is None:
        return command, {}
    directory = f'{root.rstrip("/")}/delegation-{session.id}'
    binding = {'python': argv[0], 'runner': argv[1], 'manifest': directory+'/manifest.json',
               'state_dir': directory+'/state', 'thread': thread}
    argv += ['--progress-manifest', binding['manifest'], '--progress-state-dir', binding['state_dir'],
             '--progress-thread', thread, '--progress-label', 'gateway-owned delegation', '--progress-owner', 'gateway']
    # The directory is exclusively created; registration refuses preexisting manifests.
    return f'mkdir -m 700 -- {shlex.quote(directory)} && exec {shlex.join(argv)}', binding


def remote_observe(session, *, ack=None, admit=None, stop=False):
    from hermes_constants import get_hermes_home
    connection = session.remote_connection
    if connection.get('profile_home') != str(get_hermes_home()):
        raise ValueError('progress_owner_profile')
    env = session.env_ref
    if env is None:
        from tools.environments.ssh import SSHEnvironment
        env = SSHEnvironment(**{key: connection[key] for key in ('host', 'user', 'port', 'key_path')}, _status_only=True)
    if env._connection_identity != connection.get('identity'):
        raise ValueError('progress_ssh_identity')
    binding = session.delegation_progress
    argv = [binding['python'], binding['runner'], 'progress', '--manifest', binding['manifest'],
            '--state-dir', binding['state_dir'], '--thread', binding['thread']]
    if ack:
        argv += ['--ack', ack]
    if admit:
        argv += ['--admit', admit]
    if stop:
        argv += ['--stop']
    result = env._run_ssh(shlex.join(argv), timeout=20)
    if len(result.stdout.encode()) > 16384:
        raise ValueError('progress_observation_unavailable')
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError('progress_observation_invalid_json') from None
    if not isinstance(value, dict):
        raise ValueError('progress_observation_invalid_json')
    if result.returncode:
        reason = value.get('error')
        allowed = {'gateway_owner_required', 'gateway_thread_binding', 'worker_incarnation',
                   'worker_incarnation_changed', 'ack_requires_exact_head_id',
                   'admit_requires_exact_head_id', 'state_identity_or_schema_mismatch',
                   'invalid_state', 'outbox_full'}
        raise ValueError('progress_remote_' + (reason if reason in allowed else 'observation_unavailable'))
    if value.get('status') not in {'observed', 'admitted', 'stopped'}:
        raise ValueError('progress_observation_unavailable')
    return value


def _cancelled(db, session):
    row = db.execute('SELECT cancelled_at FROM process_followup_cancellations WHERE session_key=?',
                     (session.session_key,)).fetchone()
    return bool(row and row[0] >= session.started_at)


def claim_progress(execution, oid, session=None):
    """Atomic with foreground/automatic terminal claims in the same profile database."""
    from tools.process_registry_followups import _db
    with _db() as db:
        row = db.execute('SELECT phase FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        if (row and row[0] not in {'pending', 'queued'}) or (session and _cancelled(db, session)):
            db.execute("UPDATE delivery_obligations SET state='abandoned' WHERE obligation_id=? AND state!='delivered'", (oid,))
            return False
        if db.execute("SELECT 1 FROM delivery_obligations WHERE obligation_id LIKE ? AND obligation_id!=? AND state='attempting'",
                      (f'delegation-progress:{execution}:%', oid)).fetchone():
            return False
        from gateway.delivery_ledger import _owner_stamp
        pid, started = _owner_stamp()
        return db.execute("UPDATE delivery_obligations SET state='attempting',updated_at=?,owner_pid=?,owner_started_at=? WHERE obligation_id=? AND state IN ('pending','failed')", (time.time(), pid, started, oid)).rowcount == 1


def recover_progress(execution):
    """Recover orphan sends independently of whichever remote head is visible."""
    from gateway import delivery_ledger as ledger
    from tools.process_registry_followups import _db
    with _db() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'delivery_obligations' not in tables:
            return
        rows = db.execute("SELECT obligation_id,owner_pid,owner_started_at FROM delivery_obligations WHERE obligation_id LIKE ? AND state='attempting'",
                          (f'delegation-progress:{execution}:%',)).fetchall()
        for oid, pid, started in rows:
            if not ledger._owner_alive(pid, started):
                db.execute("UPDATE delivery_obligations SET state='uncertain',last_error=?,updated_at=? WHERE obligation_id=? AND state='attempting'",
                           ('progress_sender_exited_receipt_unknown', time.time(), oid))


async def reconcile_progress(runner, registry):
    from hermes_constants import get_hermes_home
    from tools.process_registry_followups import get_state
    # Registry recovery already restores checkpoint-bound SSH identities per profile.
    with registry._lock:
        sessions = {**registry._finished, **registry._running}
    from tools.process_registry_results import _result_paths
    from tools.process_registry import ProcessSession
    for path in await asyncio.to_thread(_result_paths):
        try:
            record = json.loads(path.read_text())
            if (record.get('delegation_progress') and record.get('remote_connection', {}).get('profile_home') == str(get_hermes_home())
                    and record['id'] == path.stem and record['id'] not in sessions):
                sessions[record['id']] = ProcessSession(**{k: v for k, v in record.items() if k in ProcessSession.__dataclass_fields__}, exited=True)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    async def observe_session(session):
        if not session.delegation_progress or session.remote_connection.get('profile_home') != str(get_hermes_home()):
            return
        key = (str(get_hermes_home()), session.id)
        now = time.monotonic()
        if now - _last_poll.get(key, -100) < 5:
            return
        _last_poll[key] = now
        try:
            await asyncio.to_thread(recover_progress, session.id)
            from tools.process_registry_followups import _db
            with _db(read_only=True) as db:
                cancelled = _cancelled(db, session)
            state = await asyncio.to_thread(get_state, session.id)
            if cancelled or (state and state['phase'] not in {'pending', 'queued'}):
                await asyncio.to_thread(remote_observe, session, stop=True)
                _last_poll[key] = float('inf')
                return
            observation = await asyncio.to_thread(remote_observe, session)
            message = observation.get('message')
            if not message:
                return
            binding = session.delegation_progress
            if (message.get('run_id') != observation['run_id'] or message.get('thread_id') != binding['thread']
                    or type(message.get('sequence')) is not int
                    or message.get('id') != f"{observation['run_id']}:{message['sequence']}"):
                raise ValueError('progress_outbox_binding')
            # Successful worker exit feeds the durable verification path, not a second completion sender.
            if message['event'] in {'cli_completed', 'final_verified', 'stopped'}:
                await asyncio.to_thread(remote_observe, session, ack=message['id'])
                return
            await send_progress(runner, session, message)
        except Exception as exc:
            # Never expose raw remote stderr/logs or infer worker failure from SSH loss.
            logger.warning('Delegation observation unavailable for %s: %s', session.id, _cause(exc))

    # An unrelated SSH timeout must not hold up terminal reconciliation.
    semaphore = asyncio.Semaphore(4)
    async def bounded(session):
        async with semaphore:
            await observe_session(session)
    tasks = []
    for session in sessions.values():
        key = (str(get_hermes_home()), session.id)
        existing = _poll_tasks.get(key)
        if existing and not existing.done():
            tasks.append(existing)
            continue
        task = asyncio.create_task(bounded(session))
        _poll_tasks[key] = task
        task.add_done_callback(lambda done, key=key: _poll_tasks.pop(key, None) if _poll_tasks.get(key) is done else None)
        tasks.append(task)
    if tasks:
        # Keep owned work alive across ticks instead of repeatedly cancelling slow SSH.
        await asyncio.wait(tasks, timeout=2)


async def send_progress(runner, session, message):
    from gateway import delivery_ledger as ledger
    from tools.process_registry_followups import scrub_payload, _db
    from tools.process_registry import _WATCHER_ROUTE_KEYS
    evt = {key: getattr(session, 'watcher_'+key) for key in _WATCHER_ROUTE_KEYS}
    evt.update(session_key=session.session_key, parent_session_id=session.parent_session_id)
    source = runner._build_process_event_source(evt)
    if source is None or str(source.thread_id or source.chat_id) != session.delegation_progress['thread']:
        raise ValueError('progress_destination')
    adapter = runner._delivery_adapter_for(source)
    if adapter is None or not ledger.ledger_enabled():
        return
    if message.get('sender_profile') is not None:
        actual_bot = getattr(getattr(getattr(adapter, '_client', None), 'user', None), 'id', None)
        if (message['sender_profile'] != (getattr(adapter, '_owner_profile', None) or 'default')
                or str(actual_bot) != str(message.get('expected_bot_id'))):
            raise ValueError('progress_sender_identity')
    oid = f"delegation-progress:{session.id}:{message['id']}"
    content = scrub_payload(message['content'])
    # Fixed renderer, but do not allow worker-controlled paths to notify Discord users.
    content = content.replace('@', '@\u200b')
    await asyncio.to_thread(ledger.record_obligation, obligation_id=oid, session_key=session.session_key,
        platform=evt['platform'], chat_id=source.chat_id, thread_id=source.thread_id,
        content=content, adapter_profile=getattr(adapter, '_owner_profile', None),
        response_kind='delegation_progress', preserve_existing=True)
    with _db(read_only=True) as db:
        state = db.execute('SELECT state FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone()[0]
    if state == 'attempting':
        from gateway.delivery_ledger import _owner_alive
        with _db(read_only=True) as db:
            owner = db.execute('SELECT owner_pid,owner_started_at FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone()
        if not _owner_alive(*owner):
            await asyncio.to_thread(ledger.mark_uncertain, oid, 'Progress sender exited; receipt unknown, do not replay')
        return
    if state in {'delivered', 'uncertain', 'abandoned'}:
        await asyncio.to_thread(remote_observe, session, ack=message['id'])
        return
    claim = asyncio.create_task(asyncio.to_thread(claim_progress, session.id, oid, session))
    try:
        claimed = await asyncio.shield(claim)
    except asyncio.CancelledError:
        async def release_unsent():
            if await claim:
                await asyncio.to_thread(ledger.release_runtime_claim, oid, 'progress_claim_cancelled_unsent')
        settlement = asyncio.create_task(release_unsent())
        _sends.add(settlement)
        settlement.add_done_callback(_sends.discard)
        raise
    if not claimed:
        return
    sending = False
    try:
        admission = await asyncio.to_thread(remote_observe, session, admit=message['id'])
        if admission.get('message', {}).get('id') != message['id']:
            await asyncio.to_thread(ledger.release_runtime_claim, oid, 'progress_head_superseded_unsent')
            return
        # Keep ownership until transport resolves; caller cancellation is uncertain, never blindly replayed.
        send = asyncio.create_task(adapter.send(source.chat_id, content,
            metadata={'thread_id': source.thread_id, 'allowed_mentions': {'parse': [], 'replied_user': False},
                      'non_conversational': True}))
        sending = True
        try:
            result = await asyncio.wait_for(asyncio.shield(send), timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError) as interruption:
            async def settle():
                try:
                    receipt = await send
                    if getattr(receipt, 'success', False):
                        await asyncio.to_thread(ledger.mark_delivered, oid, message_id=getattr(receipt, 'message_id', None))
                        await asyncio.to_thread(remote_observe, session, ack=message['id'])
                    else:
                        await asyncio.to_thread(ledger.mark_uncertain, oid,
                            'progress_late_receipt_unknown:' + scrub_payload(str(getattr(receipt, 'error', '') or 'unconfirmed'))[:350])
                except BaseException:
                    await asyncio.to_thread(ledger.mark_uncertain, oid, 'Late progress send interrupted')
            task = asyncio.create_task(settle())
            _sends.add(task)
            task.add_done_callback(_sends.discard)
            if isinstance(interruption, asyncio.CancelledError):
                # Cancellation must leave the durable fence up while shielded transport runs.
                raise
            return
        if not getattr(result, 'success', False):
            await asyncio.to_thread(ledger.mark_uncertain, oid,
                'progress_receipt_unknown:' + scrub_payload(str(getattr(result, 'error', '') or 'unconfirmed'))[:350])
            return
        await asyncio.to_thread(ledger.mark_delivered, oid, message_id=getattr(result, "message_id", None))
        await asyncio.to_thread(remote_observe, session, ack=message['id'])
    except asyncio.CancelledError:
        if not sending:
            await asyncio.to_thread(ledger.release_runtime_claim, oid, 'progress_admission_cancelled_unsent')
        raise
    except BaseException as exc:
        if not sending:
            await asyncio.to_thread(ledger.release_runtime_claim, oid, _cause(exc))
        else:
            await asyncio.to_thread(ledger.mark_uncertain, oid, 'progress_send_interrupted:' + _cause(exc))
        raise
