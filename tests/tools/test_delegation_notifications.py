"""Transport cancellation must not allow a final report to overtake progress."""
import asyncio
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from gateway import delivery_ledger as delivery
from tools import process_registry_followups as followups
from tools import delegation_notifications as notifications
from tools.process_registry import ProcessSession


@pytest.fixture(autouse=True)
def fixed_progress_text(monkeypatch):
    # Transport contracts stay independent of model availability; rewrite contracts have their own suite.
    from tools import delegation_progress_rewrite
    monkeypatch.setattr(delegation_progress_rewrite, 'rewrite_progress',
        AsyncMock(side_effect=lambda facts, event, **kwargs: facts))


@pytest.fixture
def multiplexed_progress(monkeypatch, tmp_path):
    from hermes_constants import (
        get_hermes_home, get_process_hermes_home,
        reset_hermes_home_override, set_hermes_home_override,
    )
    launch = tmp_path / 'launch'
    profile = launch / 'profiles' / 'koharu'
    monkeypatch.setenv('HERMES_HOME', str(launch))
    token = set_hermes_home_override(profile)
    session = ProcessSession(id='proc_multiplexed', command='worker', session_key='owner',
        watcher_platform='discord', watcher_chat_id='4242', watcher_thread_id='123',
        remote_connection={'profile_home': str(profile)}, delegation_progress={'thread': '123'})
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    send = AsyncMock(return_value=SimpleNamespace(success=True, message_id='profile-receipt'))
    adapter = SimpleNamespace(send=send, _owner_profile='koharu')
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source,
        _delivery_adapter_for=lambda source: adapter)
    observe = Mock(side_effect=lambda *args, **kwargs: {'message': {'id': kwargs.get('admit')}})
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    message = {'id': 'run:1', 'content': 'progress'}
    try:
        assert get_process_hermes_home() == launch
        assert get_hermes_home() == profile
        followups.reserve(session)
        with delivery._transaction():
            pass
        yield SimpleNamespace(launch=launch, profile=profile, session=session, runner=runner,
            send=send, observe=observe, message=message,
            oid=f'delegation-progress:{session.id}:{message["id"]}')
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_multiplexed_progress_delivers_in_profile_db(multiplexed_progress):
    case = multiplexed_progress
    await notifications.send_progress(case.runner, case.session, case.message)

    case.send.assert_awaited_once()
    with followups._db(read_only=True) as db:
        row = db.execute('SELECT state,adapter_profile FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()
        assert tuple(row) == ('delivered', 'koharu')
        assert db.execute('SELECT message_id FROM delivery_obligation_receipts WHERE obligation_id=?',
            (case.oid,)).fetchone()[0] == 'profile-receipt'
    with delivery._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM delivery_obligations WHERE obligation_id LIKE 'delegation-progress:%'").fetchone()[0] == 0
    assert case.observe.call_args_list == [call(case.session, admit='run:1'), call(case.session, ack='run:1')]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['superseded', 'transport'])
async def test_multiplexed_progress_failure_stays_in_profile_db(multiplexed_progress, failure):
    case = multiplexed_progress
    if failure == 'superseded':
        case.observe.side_effect = None
        case.observe.return_value = {'message': {'id': 'run:2'}}
        await notifications.send_progress(case.runner, case.session, case.message)
        case.send.assert_not_awaited()
    else:
        case.send.side_effect = RuntimeError('transport interrupted')
        with pytest.raises(RuntimeError, match='transport interrupted'):
            await notifications.send_progress(case.runner, case.session, case.message)
        case.send.assert_awaited_once()
    with followups._db(read_only=True) as db:
        row = db.execute('SELECT state,attempts FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()
        assert tuple(row) == ('failed' if failure == 'superseded' else 'uncertain', 0)
        assert db.execute('SELECT COUNT(*) FROM delivery_obligation_receipts').fetchone()[0] == 0
    assert case.observe.call_args_list == [call(case.session, admit='run:1')]


@pytest.mark.asyncio
async def test_multiplexed_progress_recovers_dead_owner_in_profile_db(multiplexed_progress, monkeypatch):
    case = multiplexed_progress
    case.observe.side_effect = None
    case.observe.return_value = {'message': {'id': 'run:2'}}
    await notifications.send_progress(case.runner, case.session, case.message)
    assert notifications.claim_progress(case.session.id, case.oid)
    with followups._db() as db:
        db.execute('UPDATE delivery_obligations SET owner_pid=?,owner_started_at=? WHERE obligation_id=?',
            (999999, 1, case.oid))
    monkeypatch.setattr(delivery, '_owner_alive', lambda *args: False)

    notifications.recover_progress(case.session.id)

    with followups._db(read_only=True) as db:
        row = db.execute('SELECT state,last_error FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()
        assert tuple(row) == ('uncertain', 'progress_sender_exited_receipt_unknown')
    case.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiplexed_progress_fences_followup_begin(multiplexed_progress):
    case = multiplexed_progress
    token = followups.pending()[0]['token']
    assert followups.admission(case.session.id, token)
    async def send(*args, **kwargs):
        with followups._db(read_only=True) as db:
            assert followups.progress_inflight(db, case.session.id)
        assert not followups.begin(case.session.id, token)
        assert followups.get_state(case.session.id)['phase'] == 'pending'
        return SimpleNamespace(success=True, message_id='profile-receipt')
    case.send.side_effect = send

    await notifications.send_progress(case.runner, case.session, case.message)

    case.send.assert_awaited_once()
    assert followups.get_state(case.session.id)['phase'] == 'pending'


@pytest.mark.asyncio
async def test_multiplexed_running_followup_abandons_progress(multiplexed_progress):
    case = multiplexed_progress
    token = followups.pending()[0]['token']
    assert followups.admission(case.session.id, token)
    assert followups.begin(case.session.id, token)

    await notifications.send_progress(case.runner, case.session, case.message)

    assert not notifications.claim_progress(case.session.id, case.oid)
    with followups._db(read_only=True) as db:
        assert db.execute('SELECT state FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()[0] == 'abandoned'
    assert followups.get_state(case.session.id)['phase'] == 'running'
    case.send.assert_not_awaited()
    case.observe.assert_not_called()


def test_profile_progress_record_preserves_payload_and_claim(multiplexed_progress):
    case = multiplexed_progress
    payload = dict(obligation_id=case.oid, session_key='owner', platform='discord',
        chat_id='4242', thread_id='123', content='progress', adapter_profile='koharu')
    notifications._record_progress(**payload)
    with followups._db() as db:
        db.execute("UPDATE delivery_obligations SET state='uncertain',attempts=3,"
            "owner_pid=999999,owner_started_at=1 WHERE obligation_id=?", (case.oid,))
        before = tuple(db.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone())

    notifications._record_progress(**payload)
    with pytest.raises(ValueError, match='Delivery obligation payload conflict'):
        notifications._record_progress(**{**payload, 'content': 'changed'})

    with followups._db(read_only=True) as db:
        assert tuple(db.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()) == before


@pytest.mark.parametrize('state,foreign_owner', [
    ('attempting', True), ('delivered', False), ('abandoned', False),
])
def test_profile_progress_transitions_preserve_foreign_or_terminal_claim(
    multiplexed_progress, state, foreign_owner,
):
    case = multiplexed_progress
    notifications._record_progress(obligation_id=case.oid, session_key='owner', platform='discord',
        chat_id='4242', thread_id='123', content='progress', adapter_profile='koharu')
    with followups._db() as db:
        db.execute('UPDATE delivery_obligations SET state=?,attempts=3 WHERE obligation_id=?',
            (state, case.oid))
        if foreign_owner:
            db.execute('UPDATE delivery_obligations SET owner_pid=999999,owner_started_at=1 '
                'WHERE obligation_id=?', (case.oid,))
        before = tuple(db.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone())

    assert not notifications._release_progress_claim(case.oid, 'unsent')
    notifications._mark_progress_uncertain(case.oid, 'interrupted')
    notifications._mark_progress_delivered(case.oid, message_id='wrong-receipt')

    with followups._db(read_only=True) as db:
        assert tuple(db.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone()) == before
        assert db.execute('SELECT COUNT(*) FROM delivery_obligation_receipts').fetchone()[0] == 0


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
    resolve_root = Mock(return_value=str(tmp_path.resolve()))
    tracked, binding = notifications.prepare_launch(shlex.join(argv), session, str(tmp_path),
        resolve_root=resolve_root)
    resolve_root.assert_called_once_with(str(tmp_path))
    assert binding and shlex.join(argv) in tracked


@pytest.mark.parametrize('reserved', [
    '--progress-owner=manual', '--progress-manifest', '--progress-thread',
    '--progress-manifest=/fake/manifest', '--progress-thread=123',
    '--progress-state-dir', '--progress-label', '--progress-unknown',
])
def test_gateway_launch_rejects_reserved_progress_flags(tmp_path, reserved):
    session = ProcessSession(id='proc_reserved', command='', watcher_platform='discord', watcher_thread_id='456')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi ' + reserved
    resolve_root = Mock(return_value=str(tmp_path.resolve()))

    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})
    resolve_root.assert_not_called()


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


@pytest.mark.parametrize('value,category', [
    ('', 'empty'), ('ROOT_PATH_SENTINEL/relative', 'relative'),
    ('/ROOT_PATH_SENTINEL\n/STDOUT_SENTINEL', 'multiline'), ('/ROOT_PATH_SENTINEL/../tmp', 'dotdot'),
    ('/ROOT_PATH_SENTINEL/secret value', 'chars'), ('/ROOT_PATH_SENTINEL/$(secret)', 'chars'),
    ('/ROOT_PATH_SENTINEL/secret\tvalue', 'chars'), ('/ROOT_PATH_SENTINEL/secret\rvalue', 'chars'),
    ('/ROOT_PATH_SENTINEL/secret\x00value', 'chars'), ('/ROOT_PATH_SENTINEL/secret\x7fvalue', 'chars'),
    ('/ROOT_PATH_SENTINEL/비밀', 'chars'), ('/ROOT_PATH_SENTINEL\n\n', 'multiline'),
    ('\n', 'empty'), (None, 'chars'), (b'/ROOT_PATH_SENTINEL', 'chars'),
])
def test_invalid_resolved_temp_root_blocks_launch(tmp_path, caplog, value, category):
    session = ProcessSession(id='proc_invalid_root', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(return_value=value)

    with pytest.raises(notifications.ProgressRootUnavailable) as failure:
        notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root)

    resolve_root.assert_called_once_with(str(tmp_path))
    reason = 'invalid_root:' + category
    assert failure.value.reason == str(failure.value) == reason
    assert [record.getMessage() for record in caplog.records] == [reason]
    assert all(record.levelname == 'WARNING' for record in caplog.records)
    diagnostic = str(failure.value) + caplog.text
    for secret in ['ROOT_PATH_SENTINEL', 'STDOUT_SENTINEL', str(tmp_path), command]:
        assert secret not in diagnostic
    if isinstance(value, str) and value.strip():
        assert value not in diagnostic


@pytest.mark.parametrize('error,reason', [
    (RuntimeError('EXCEPTION_MESSAGE_SENTINEL'), 'resolver_exception:RuntimeError'),
    (TimeoutExpired('COMMAND_SENTINEL', 10, output='STDOUT_SENTINEL', stderr='STDERR_SENTINEL'),
        'resolver_exception:TimeoutExpired'),
    (TypeError('EXCEPTION_MESSAGE_SENTINEL'), 'resolver_exception:TypeError'),
    (CalledProcessError(1, '/ROOT_PATH_SENTINEL/COMMAND_SENTINEL', output='STDOUT_SENTINEL',
        stderr='STDERR_SENTINEL'), 'resolver_exit:1'),
    (CalledProcessError(255, '/ROOT_PATH_SENTINEL/COMMAND_SENTINEL', output='STDOUT_SENTINEL',
        stderr='STDERR_SENTINEL'), 'resolver_exit:255'),
])
def test_temp_root_resolution_failure_blocks_launch(tmp_path, caplog, error, reason):
    session = ProcessSession(id='proc_failed_root', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(side_effect=error)

    with pytest.raises(notifications.ProgressRootUnavailable) as failure:
        notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root)

    resolve_root.assert_called_once_with(str(tmp_path))
    assert failure.value.reason == str(failure.value) == reason
    assert [record.getMessage() for record in caplog.records] == [reason]
    assert all(record.levelname == 'WARNING' for record in caplog.records)
    diagnostic = str(failure.value) + caplog.text
    for secret in ['EXCEPTION_MESSAGE_SENTINEL', 'COMMAND_SENTINEL', 'STDOUT_SENTINEL',
                   'STDERR_SENTINEL', 'ROOT_PATH_SENTINEL', str(error), str(tmp_path), command]:
        assert secret not in diagnostic


@pytest.mark.parametrize('root', ['/private/tmp\n', '/'])
def test_temp_root_accepts_pwd_output_and_filesystem_root(root):
    session = ProcessSession(id='proc_pwd', command='', watcher_platform='discord', watcher_thread_id='123')
    command = '/usr/bin/python3 /release/scripts/run_codex_task.py --prompt hi'
    resolve_root = Mock(return_value=root)

    tracked, binding = notifications.prepare_launch(command, session, '/tmp', resolve_root=resolve_root)

    resolve_root.assert_called_once_with('/tmp')
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
    resolve_root = Mock(return_value=str(tmp_path.resolve()))

    assert notifications.prepare_launch(command, session, str(tmp_path), resolve_root=resolve_root) == (command, {})
    resolve_root.assert_not_called()


def test_ssh_spawn_resolves_only_progress_paths(monkeypatch, tmp_path):
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
    env._run_ssh.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout='/private/tmp\n')]
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
    resolve_root = Mock(return_value=str(tmp_path.resolve()))
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


def _observed_registry(monkeypatch, tmp_path, session_id):
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    registry._running[session_id] = ProcessSession(id=session_id, command='', session_key='owner',
        remote_connection={'profile_home': str(tmp_path)}, delegation_progress={'thread': '123'})
    clock = [1000.0]
    import time
    monkeypatch.setattr(notifications, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(notifications, '_last_poll', {})
    monkeypatch.setattr(notifications, '_observe_failures', {})
    return registry, clock


def _observation_warnings(caplog, session_id):
    return [record for record in caplog.records
            if record.levelname == 'WARNING' and session_id in record.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['running', 'completed', 'failed'])
async def test_finished_session_stop_failure_is_not_retried(monkeypatch, tmp_path, caplog, phase):
    registry, clock = _observed_registry(monkeypatch, tmp_path, 'proc_done')
    monkeypatch.setattr(followups, 'get_state', lambda session_id: {'phase': phase})
    calls = []
    def observe(session, **kwargs):
        calls.append(kwargs)
        raise ValueError('progress_remote_observation_unavailable')
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    for _ in range(20):
        await notifications.reconcile_progress(SimpleNamespace(), registry)
        clock[0] += 61
    assert calls == [{'stop': True}]
    warnings = _observation_warnings(caplog, 'proc_done')
    assert len(warnings) == 1
    assert 'progress_remote_observation_unavailable' in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_cancelled_session_stop_failure_is_not_retried(monkeypatch, tmp_path, caplog):
    registry, clock = _observed_registry(monkeypatch, tmp_path, 'proc_cancelled')
    monkeypatch.setattr(followups, 'get_state', lambda session_id: {'phase': 'pending'})
    monkeypatch.setattr(notifications, '_cancelled', lambda db, session: True)
    observe = Mock(side_effect=TimeoutExpired('ssh', 20))
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    for _ in range(5):
        await notifications.reconcile_progress(SimpleNamespace(), registry)
        clock[0] += 61
    observe.assert_called_once()
    assert observe.call_args.kwargs == {'stop': True}
    assert len(_observation_warnings(caplog, 'proc_cancelled')) == 1


@pytest.mark.asyncio
async def test_finished_session_successful_stop_is_not_retried(monkeypatch, tmp_path, caplog):
    registry, clock = _observed_registry(monkeypatch, tmp_path, 'proc_stopped')
    monkeypatch.setattr(followups, 'get_state', lambda session_id: {'phase': 'completed'})
    observe = Mock(return_value={'status': 'stopped'})
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    for _ in range(5):
        await notifications.reconcile_progress(SimpleNamespace(), registry)
        clock[0] += 61
    observe.assert_called_once()
    assert not _observation_warnings(caplog, 'proc_stopped')


@pytest.mark.asyncio
async def test_running_session_failures_back_off_and_warn_on_state_change(monkeypatch, tmp_path, caplog):
    caplog.set_level('INFO', logger=notifications.logger.name)
    registry, clock = _observed_registry(monkeypatch, tmp_path, 'proc_live')
    monkeypatch.setattr(followups, 'get_state', lambda session_id: {'phase': 'queued'})
    outcome = {'error': ValueError('progress_remote_observation_unavailable')}
    polled = []
    def observe(session, **kwargs):
        assert 'stop' not in kwargs
        polled.append(clock[0] - 1000)
        if outcome['error']:
            raise outcome['error']
        return {'message': None}
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    async def tick_until(end):
        while clock[0] - 1000 <= end:
            await notifications.reconcile_progress(SimpleNamespace(), registry)
            clock[0] += 1
    await tick_until(400)
    assert polled == [0, 10, 30, 70, 130, 190, 250, 310, 370]
    assert len(_observation_warnings(caplog, 'proc_live')) == 1

    outcome['error'] = None
    polled.clear()
    await tick_until(441)
    assert polled == [430, 435, 440]
    assert 'Delegation observation recovered for proc_live' in caplog.text

    outcome['error'] = TimeoutExpired('ssh', 20)
    polled.clear()
    await tick_until(500)
    assert polled == [445, 455, 475]
    warnings = _observation_warnings(caplog, 'proc_live')
    assert len(warnings) == 2
    assert 'TimeoutExpired' in warnings[1].getMessage()
    assert notifications._last_poll[(str(tmp_path), 'proc_live')] != float('inf')
