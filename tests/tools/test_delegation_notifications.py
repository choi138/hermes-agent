"""Transport cancellation must not allow a final report to overtake progress."""
import asyncio
from pathlib import Path
from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from gateway import delivery_ledger as delivery
from tools import process_registry_followups as followups
from tools import delegation_notifications as notifications
from tools.process_registry import ProcessSession


@pytest.mark.asyncio
@pytest.mark.parametrize('late_success', [True, False])
async def test_cancelled_observer_keeps_terminal_fenced_until_transport_settles(monkeypatch, tmp_path, late_success):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_slow_send', command='worker', session_key='owner',
        watcher_platform='discord', watcher_chat_id='4242', watcher_thread_id='123',
        delegation_progress={'thread': '123'})
    followups.reserve(session)
    entered, release = asyncio.Event(), asyncio.Event()
    sends = []
    async def send(*args, **kwargs):
        sends.append((args, kwargs))
        entered.set()
        await release.wait()
        return SimpleNamespace(success=late_success, message_id='receipt-1')
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source,
        _delivery_adapter_for=lambda source: SimpleNamespace(send=send, _owner_profile='koharu'))
    monkeypatch.setattr(notifications, 'remote_observe', lambda *args, **kwargs: {'message': {'id': kwargs.get('admit')}})
    message = {'id':'run:1', 'run_id':'run', 'sequence':1, 'content':'진행 상황 @everyone'}
    task = asyncio.create_task(notifications.send_progress(runner, session, message))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with followups._db(read_only=True) as db:
        assert followups.progress_inflight(db, session.id)
    row = followups.pending()[0]
    assert not followups.admission(session.id, row['token'])
    release.set()
    await asyncio.wait_for(asyncio.gather(*tuple(notifications._sends)), 5)
    with followups._db(read_only=True) as db:
        assert not followups.progress_inflight(db, session.id)
        saved = db.execute('SELECT state FROM delivery_obligations').fetchone()[0]
        assert saved == ('delivered' if late_success else 'uncertain')
        if late_success:
            assert db.execute('SELECT message_id FROM delivery_obligation_receipts').fetchone()[0] == 'receipt-1'
    assert followups.admission(session.id, row['token'])
    assert len(sends) == 1 and '@\u200beveryone' in sends[0][0][1]


def test_gateway_registration_preserves_v3_policy_scope_and_identity(tmp_path):
    import shlex
    argv = ['/usr/bin/python3', '/release/scripts/run_codex_task.py', '--cli', 'codex',
        '--task-class', 'integration', '--risk', 'high', '--ambiguity', 'low',
        '--progress-code-scope', '/repo/app', '--progress-sender-profile', 'koharu',
        '--progress-expected-bot-id=123', '--progress-login-shell', 'zsh']
    session = ProcessSession(id='proc_v3', command='', watcher_platform='discord', watcher_thread_id='456')
    tracked, binding = notifications.prepare_launch(shlex.join(argv), session, str(tmp_path),
        resolve_root=lambda root: str(tmp_path.resolve()))
    assert binding and shlex.join(argv) in tracked
    for reserved in ['--progress-owner=manual', '--progress-manifest', '--progress-thread']:
        assert notifications.prepare_launch(shlex.join(argv+[reserved]), session, str(tmp_path))[1] == {}


def test_gateway_launch_uses_canonical_temp_root(tmp_path):
    import shlex
    from agent.delegation_progress import _path
    real = tmp_path.resolve() / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    session = ProcessSession(id='proc_canonical', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(return_value=str(real))

    tracked, binding = notifications.prepare_launch(command, session, str(link), resolve_root=resolve_root)

    resolve_root.assert_called_once_with(str(link))
    directory = real / f'delegation-{session.id}'
    assert binding['manifest'] == str(directory / 'manifest.json')
    assert binding['state_dir'] == str(directory / 'state')
    assert tracked.startswith(f'mkdir -m 700 -- {directory} && exec ')
    argv = shlex.split(tracked)
    assert argv[argv.index('--progress-manifest') + 1] == binding['manifest']
    assert argv[argv.index('--progress-state-dir') + 1] == binding['state_dir']
    assert str(link) not in tracked
    directory.mkdir(mode=0o700)
    assert _path(Path(binding['manifest']).parent) == directory.resolve()


@pytest.mark.parametrize('value', [
    '', 'relative/tmp', '/private/tmp\n/private/other', '/private/../tmp',
    '/private/secret value', '/private/$(secret)', '/private/secret\tvalue',
    '/private/secret\rvalue', '/private/secret\x00value', '/private/secret\x7fvalue',
    '/private/비밀', '/private/tmp\n\n', '\n', None,
])
def test_invalid_resolved_temp_root_leaves_launch_untracked(tmp_path, caplog, value):
    session = ProcessSession(id='proc_invalid_root', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(return_value=value)

    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})

    resolve_root.assert_called_once_with(str(tmp_path))
    assert [record.getMessage() for record in caplog.records] == ['progress_temp_root_invalid']


@pytest.mark.parametrize('error', [RuntimeError('private secret'), TimeoutExpired('private secret', 10)])
def test_temp_root_resolution_failure_leaves_launch_untracked(tmp_path, caplog, error):
    session = ProcessSession(id='proc_failed_root', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(side_effect=error)

    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})

    resolve_root.assert_called_once_with(str(tmp_path))
    assert [record.getMessage() for record in caplog.records] == ['progress_temp_root_resolution_failed']


def test_missing_temp_root_resolver_leaves_launch_untracked(tmp_path, caplog):
    session = ProcessSession(id='proc_no_resolver', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'

    assert notifications.prepare_launch(command, session, str(tmp_path)) == (command, {})
    assert [record.getMessage() for record in caplog.records] == ['progress_temp_root_resolution_failed']


@pytest.mark.parametrize('root', ['/private/tmp\n', '/'])
def test_temp_root_accepts_pwd_output_and_filesystem_root(root):
    session = ProcessSession(id='proc_pwd', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'

    tracked, binding = notifications.prepare_launch(command, session, '/tmp', resolve_root=lambda temp_dir: root)

    directory = root.removesuffix('\n').rstrip('/') + '/delegation-proc_pwd'
    assert binding['manifest'] == directory + '/manifest.json'
    assert binding['state_dir'] == directory + '/state'
    assert tracked.startswith(f'mkdir -m 700 -- {directory} && exec ')


@pytest.mark.parametrize('command,platform,thread', [
    ('echo hello', 'discord', '123'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi --dry-run', 'discord', '123'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi', 'telegram', '123'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi', 'discord', 'invalid'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py progress', 'discord', '123'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py lifecycle', 'discord', '123'),
    ('/usr/bin/python3 /release/scripts/run_codex_task.py --progress-owner=manual', 'discord', '123'),
])
def test_ineligible_launch_does_not_resolve_temp_root(tmp_path, command, platform, thread):
    session = ProcessSession(id='proc_ineligible', command='', watcher_platform=platform, watcher_thread_id=thread)
    resolve_root = Mock()

    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})
    resolve_root.assert_not_called()


@pytest.mark.parametrize('resolution,tracked', [
    (SimpleNamespace(returncode=0, stdout='/private/tmp\n'), True),
    (SimpleNamespace(returncode=1, stdout='/private/tmp\n', stderr='private secret'), False),
    (SimpleNamespace(returncode=0, stdout=''), False),
    (TimeoutExpired('private secret', 10), False),
])
def test_ssh_spawn_resolves_only_progress_paths(monkeypatch, tmp_path, resolution, tracked):
    import shlex
    from tools.environments.ssh import SSHEnvironment
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    route = {'HERMES_SESSION_PLATFORM': 'discord', 'HERMES_SESSION_THREAD_ID': '123'}
    monkeypatch.setattr('gateway.session_context.get_session_env', lambda key, default='': route.get(key, default))
    monkeypatch.setattr('gateway.session_context.async_delivery_supported', lambda: True)
    env = Mock(spec=SSHEnvironment)
    env.host, env.user, env.port, env.key_path = 'example.invalid', 'worker', 22, ''
    env.cwd, env._connection_identity = '/repo', 'bound'
    env.get_temp_dir.return_value = '/tmp'
    env._run_ssh.side_effect = [SimpleNamespace(returncode=0), resolution]
    env._execute_prepared.return_value = {'returncode': 0, 'output': 'HERMES_PROCESS_LAUNCH_V1:123\n'}
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, '_write_checkpoint', Mock())
    poller = Mock()
    monkeypatch.setattr(registry, '_track_started', poller)
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'

    session = registry.spawn_via_env(env, command, notify_on_complete=True)

    assert env._run_ssh.call_args_list[1] == call('cd -P -- /tmp && pwd -P', timeout=10)
    assert session.remote_root == '/tmp'
    assert poller.call_args.args[3] == (env, f'/tmp/hermes_bg_{session.id}.log',
        f'/tmp/hermes_bg_{session.id}.pid', f'/tmp/hermes_bg_{session.id}.exit')
    launched = env._execute_prepared.call_args.kwargs['stdin_data']
    if not tracked:
        assert launched == command and session.delegation_progress == {}
        return
    binding = session.delegation_progress
    assert binding['manifest'] == f'/private/tmp/delegation-{session.id}/manifest.json'
    assert binding['state_dir'] == f'/private/tmp/delegation-{session.id}/state'
    assert binding['manifest'] in launched and binding['state_dir'] in launched
    env._run_ssh.side_effect = None
    env._run_ssh.return_value = SimpleNamespace(returncode=0, stdout='{"status":"observed"}')

    assert notifications.remote_observe(session) == {'status': 'observed'}

    observed = shlex.split(env._run_ssh.call_args.args[0])
    assert observed[observed.index('--manifest') + 1] == binding['manifest']
    assert observed[observed.index('--state-dir') + 1] == binding['state_dir']


def test_new_progress_generation_waits_for_previous_transport(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_serial', command='worker', session_key='owner')
    followups.reserve(session)
    for oid in ['delegation-progress:proc_serial:run:1', 'delegation-progress:proc_serial:run:2']:
        delivery.record_obligation(obligation_id=oid, session_key='owner', platform='discord',
            chat_id='123', thread_id='456', content=oid, response_kind='delegation_progress')
    assert notifications.claim_progress(session.id, 'delegation-progress:proc_serial:run:1')
    assert not notifications.claim_progress(session.id, 'delegation-progress:proc_serial:run:2')
    delivery.mark_delivered('delegation-progress:proc_serial:run:1', message_id='receipt')
    assert notifications.claim_progress(session.id, 'delegation-progress:proc_serial:run:2')


@pytest.mark.parametrize('block', [
    '```process_reports\ninvalid\n```\n다른 설명',
    '```process_reports\nunfinished',
    '```process_reports\n{}\n```\n```process_reports\n{}\n```',
])
def test_invalid_internal_reports_are_not_published(block):
    public, reports = followups.foreground_reports({'final_response': '부모 작업 설명.\n' + block})
    assert public.startswith('부모 작업 설명.') and not reports
    assert 'process_reports' not in public and 'unfinished' not in public and 'invalid' not in public


@pytest.mark.asyncio
async def test_gateway_rejects_wrong_registered_sender_before_transport(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_identity', command='worker', session_key='owner',
        watcher_platform='discord', delegation_progress={'thread': '123'})
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    from unittest.mock import AsyncMock
    send = AsyncMock()
    adapter = SimpleNamespace(send=send, _owner_profile='koharu', _client=SimpleNamespace(user=SimpleNamespace(id=10)))
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source, _delivery_adapter_for=lambda source: adapter)
    with pytest.raises(ValueError, match='progress_sender_identity'):
        await notifications.send_progress(runner, session, {'id':'run:1', 'content':'safe',
            'sender_profile':'koharu', 'expected_bot_id':'11'})
    send.assert_not_called()


@pytest.mark.asyncio
async def test_restart_recovers_finished_registration_without_replaying_unknown_receipt(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.process_registry_results import save_completed_result
    from unittest.mock import AsyncMock
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_recovered', command='worker', session_key='owner',
        exited=True, exit_code=0, watcher_platform='discord', watcher_chat_id='4242', watcher_thread_id='123',
        remote_connection={'profile_home': str(tmp_path)}, delegation_progress={'thread': '123'})
    save_completed_result(session, strict=True)
    followups.reserve(session)
    oid = 'delegation-progress:proc_recovered:run:1'
    delivery.record_obligation(obligation_id=oid, session_key='owner', platform='discord', chat_id='4242',
        thread_id='123', content='progress', response_kind='delegation_progress')
    assert notifications.claim_progress(session.id, oid)
    monkeypatch.setattr(delivery, '_owner_alive', lambda *args: False)
    message = {'id':'run:1', 'run_id':'run', 'thread_id':'123', 'sequence':1, 'event':'periodic', 'content':'progress'}
    observed = []
    def observe(restored, **kwargs):
        observed.append(restored.id)
        return {'status':'observed', 'run_id':'run', 'message':message}
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    send = AsyncMock()
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source,
        _delivery_adapter_for=lambda source: SimpleNamespace(send=send, _owner_profile=None))
    notifications._last_poll.clear()
    await notifications.reconcile_progress(runner, ProcessRegistry())
    assert observed == [session.id, session.id]
    send.assert_not_called()
    with followups._db(read_only=True) as db:
        assert db.execute('SELECT state FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone()[0] == 'uncertain'

@pytest.mark.parametrize('suffix', [' ~/repo/SPEC.md', '\nwhoami', ' 2>&1', ' &>/tmp/out', ' || true', ' /repo/*.py', ' # comment', ' {a,b}'])
def test_shell_program_is_not_rewritten(suffix, tmp_path):
    session = ProcessSession(id='proc_shell', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi' + suffix
    resolve_root = Mock()
    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})
    resolve_root.assert_not_called()


def test_recovered_claim_refreshes_owner_and_session_cancellation_blocks_running_job(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_owner', command='', session_key='owner')
    oid = 'delegation-progress:proc_owner:run:1'
    delivery.record_obligation(obligation_id=oid, session_key='owner', platform='discord', chat_id='123', thread_id=None, content='progress')
    with followups._db() as db:
        db.execute('UPDATE delivery_obligations SET owner_pid=999999,owner_started_at=0')
    assert notifications.claim_progress(session.id, oid, session)
    with followups._db(read_only=True) as db:
        owner = db.execute('SELECT owner_pid,owner_started_at FROM delivery_obligations').fetchone()
        assert tuple(owner) == delivery._owner_stamp()
    delivery.release_runtime_claim(oid)
    followups.cancel_for_session('owner')
    assert not notifications.claim_progress(session.id, oid, session)


def test_orphan_recovery_does_not_depend_on_current_remote_head(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_old_head', command='', session_key='owner')
    oid = 'delegation-progress:proc_old_head:run:1'
    delivery.record_obligation(obligation_id=oid, session_key='owner', platform='discord', chat_id='123', thread_id=None, content='progress')
    assert notifications.claim_progress(session.id, oid)
    monkeypatch.setattr(delivery, '_owner_alive', lambda *args: False)
    notifications.recover_progress(session.id)
    with followups._db(read_only=True) as db:
        assert db.execute('SELECT state FROM delivery_obligations').fetchone()[0] == 'uncertain'


@pytest.mark.asyncio
async def test_cancelled_claim_is_released_without_transport(monkeypatch, tmp_path):
    import threading
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_claim_cancel', command='', session_key='owner', watcher_platform='discord',
                             delegation_progress={'thread': '123'})
    entered, release = threading.Event(), threading.Event()
    original = notifications.claim_progress
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(notifications, 'claim_progress', delayed)
    from unittest.mock import AsyncMock
    send = AsyncMock()
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source,
                            _delivery_adapter_for=lambda src: SimpleNamespace(send=send, _owner_profile=None))
    task = asyncio.create_task(notifications.send_progress(runner, session, {'id':'run:1', 'content':'progress'}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait_for(asyncio.gather(*tuple(notifications._sends)), 5)
    send.assert_not_called()
    with followups._db(read_only=True) as db:
        assert not followups.progress_inflight(db, session.id)
        assert db.execute('SELECT state FROM delivery_obligations').fetchone()[0] == 'failed'

@pytest.mark.asyncio
async def test_slow_ssh_observation_does_not_block_other_execution(monkeypatch, tmp_path):
    import threading
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    release, fast_seen = threading.Event(), threading.Event()
    for name in ['proc_slow', 'proc_fast']:
        registry._running[name] = ProcessSession(id=name, command='', session_key='owner',
            remote_connection={'profile_home': str(tmp_path)}, delegation_progress={'thread': '123'})
    def observe(session, **kwargs):
        if session.id == 'proc_slow':
            assert release.wait(8)
        else:
            fast_seen.set()
        return {'message': None}
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    notifications._last_poll.clear()
    task = asyncio.create_task(notifications.reconcile_progress(SimpleNamespace(), registry))
    try:
        assert await asyncio.to_thread(fast_seen.wait, 5)
        await asyncio.wait_for(task, 5)
        assert not release.is_set()
    finally:
        release.set()
        await asyncio.gather(*tuple(notifications._poll_tasks.values()))


def test_current_sender_recovery_keeps_completion_fenced(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_live_owner', command='', session_key='owner')
    followups.reserve(session)
    oid = 'delegation-progress:proc_live_owner:run:1'
    delivery.record_obligation(obligation_id=oid, session_key='owner', platform='discord', chat_id='123', thread_id=None, content='progress')
    assert notifications.claim_progress(session.id, oid, session)
    notifications.recover_progress(session.id)
    with followups._db(read_only=True) as db:
        assert followups.progress_inflight(db, session.id)
    row = followups.pending()[0]
    assert not followups.admission(session.id, row['token'])

@pytest.mark.parametrize('remote_value,code,expected', [
    ({'status':'admitted', 'message': {'id':'run:1'}}, 0, None),
    ({'status':'observation_unavailable', 'error':'gateway_thread_binding'}, 74, 'progress_remote_gateway_thread_binding'),
    ({'status':'observation_unavailable', 'error':'private remote details'}, 74, 'progress_remote_observation_unavailable'),
])
def test_remote_admission_and_allowlisted_diagnostics(monkeypatch, tmp_path, remote_value, code, expected):
    import json
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=code, stdout=json.dumps(remote_value))
    env = SimpleNamespace(_connection_identity='bound', _run_ssh=run)
    session = ProcessSession(id='proc_wire', command='', remote_connection={'profile_home':str(tmp_path), 'identity':'bound'},
        env_ref=env, delegation_progress={'python':'python3', 'runner':'/release/scripts/run_codex_task.py',
            'manifest':'/private/manifest.json', 'state_dir':'/private/state', 'thread':'123'})
    if expected:
        with pytest.raises(ValueError, match=expected):
            notifications.remote_observe(session, admit='run:1')
    else:
        assert notifications.remote_observe(session, admit='run:1') == remote_value
    assert '--admit run:1' in commands[0]
