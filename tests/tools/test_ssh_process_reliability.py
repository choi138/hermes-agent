"""Real remote protocol scripts against isolated local subprocesses."""
import json
import shlex
import subprocess
import time

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments.ssh_process import launch_command, observe


class ShellTransport:
    host, user, port = 'isolated', 'test', 22

    def _run_ssh(self, command, timeout, *, stdin_data=None):
        return subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=timeout, input=stdin_data)


def test_private_protocol_preserves_user_artifact_umask(tmp_path):
    import stat
    env = ShellTransport()
    artifact = tmp_path / 'user-output'
    env._run_ssh('umask 022; ' + launch_command(str(tmp_path), 'proc_umask'), 5,
                 stdin_data='printf result > ' + shlex.quote(str(artifact)))
    assert wait_exit(env, tmp_path, 'proc_umask')['exit_code'] == 0
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o644
    claim = tmp_path / 'hermes_bg_proc_umask.claim'
    assert stat.S_IMODE(claim.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in claim.iterdir())


def test_remote_protocol_runs_on_real_python38(tmp_path):
    import os, shutil
    python38 = shutil.which('python3.8')
    if python38 is None:
        pytest.skip('Python 3.8 interpreter required for remote compatibility proof')
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'python3').symlink_to(python38)
    class Python38Transport(ShellTransport):
        def _run_ssh(self, command, timeout, *, stdin_data=None):
            return subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                timeout=timeout, input=stdin_data, env=dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH']))
    env = Python38Transport()
    result = env._run_ssh(launch_command(str(tmp_path), 'proc_python38'), 5, stdin_data='printf compatible')
    assert result.returncode == 0, result.stderr
    state = wait_exit(env, tmp_path, 'proc_python38')
    assert state['exit_code'] == 0 and state['log']['bytes'] == b'compatible'


def test_failed_python_prerequisite_preserves_sanitized_cause(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.environments.base import EnvironmentConnectionError
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    detail = 'invalid PYTHONHOME; token=sk-testSensitiveTransportKey123456789'
    monkeypatch.setattr(env, '_run_ssh', lambda *a, **kw: subprocess.CompletedProcess([], 1, '', detail))
    registry = ProcessRegistry()
    with pytest.raises(EnvironmentConnectionError) as error:
        registry.spawn_via_env(env, 'touch must-not-dispatch')
    assert 'exit 1' in str(error.value) and 'invalid PYTHONHOME' in str(error.value)
    assert 'sk-testSensitiveTransportKey' not in str(error.value)
    assert not registry._running and not (tmp_path / 'processes.json').exists()


def test_numeric_login_banner_cannot_poison_launch_identity(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, '_track_started', lambda *a, **kw: None)
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, 'get_temp_dir', lambda: str(tmp_path))
    monkeypatch.setattr(env, '_run_ssh', ShellTransport()._run_ssh)
    def dispatch(command, **kwargs):
        result = ShellTransport()._run_ssh(command, 5, stdin_data=kwargs.get('stdin_data'))
        return {'returncode': result.returncode, 'output': '123\n' + result.stdout}
    monkeypatch.setattr(env, '_execute_prepared', dispatch)
    session = registry.spawn_via_env(env, 'printf complete')
    observed = wait_exit(ShellTransport(), tmp_path, session.id)
    assert session.pid == observed['identity']['pid']
    env._observe_process = lambda root, execution, offset: observe(ShellTransport(), root, execution, offset)
    poll_remote(registry, session, env)
    assert session.exited and session.exit_code == 0 and session.output_buffer == 'complete'


def test_oserror_diagnostic_keeps_errno_without_free_form_text():
    from tools.environments.ssh_process import safe_error
    assert safe_error(OSError(13, 'private unstructured payload')) == 'PermissionError: errno=13'


def wait_exit(env, root, execution):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = observe(env, str(root), execution, 0)
        if state['state'] == 'exited':
            return state
        time.sleep(.05)
    pytest.fail('remote wrapper did not publish receipt')


def test_remote_command_preserves_shell_zero(tmp_path):
    ShellTransport()._run_ssh(launch_command(str(tmp_path), 'proc_shellzero'), 5,
        stdin_data='exec "$0" -c \'printf shell-compatible\'')
    result = wait_exit(ShellTransport(), tmp_path, 'proc_shellzero')
    assert result['exit_code'] == 0
    assert bytes(result['log']['bytes']).decode() == 'shell-compatible'


def test_atomic_receipt_and_bounded_logs_and_duplicate_dispatch(tmp_path):
    env = ShellTransport()
    command = "python3 -c " + shlex.quote("import sys; print('한'*50000); sys.exit(7)")
    launch = launch_command(str(tmp_path), 'proc_test')
    env._run_ssh(launch, 5, stdin_data=command)
    state = wait_exit(env, tmp_path, 'proc_test')
    assert state['exit_code'] == 7
    assert len(state['log']['bytes']) == 65536
    assert state['log']['next'] == 65536
    identity = state['identity']
    env._run_ssh(launch, 5, stdin_data=command)
    assert wait_exit(env, tmp_path, 'proc_test')['identity'] == identity
    receipt = tmp_path / 'hermes_bg_proc_test.claim/process.receipt'
    receipt.write_text('{')
    assert observe(env, str(tmp_path), 'proc_test', 0)['state'] != 'exited'
    receipt.write_text(json.dumps({'identity': {'execution': 'other'}, 'exit_code': 0}))
    assert observe(env, str(tmp_path), 'proc_test', 0)['state'] == 'identity_mismatch'



def test_duplicate_dispatch_while_running_has_one_effect(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    env = ShellTransport()
    effect, release = tmp_path / 'effects', tmp_path / 'release'
    command = "python3 -c " + shlex.quote(
        "import pathlib,time; p=pathlib.Path(" + repr(str(effect)) + "); "
        "p.open('a').write('effect\\n'); "
        "r=pathlib.Path(" + repr(str(release)) + "); "
        "exec('while not r.exists(): time.sleep(.02)')")
    launch = launch_command(str(tmp_path), 'proc_concurrent')
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: env._run_ssh(launch, 5, stdin_data=command), range(8)))
        deadline = time.monotonic() + 5
        while not effect.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert effect.read_text().splitlines() == ['effect']
        assert not (tmp_path / 'hermes_bg_proc_concurrent.claim/process.receipt').exists()
        env._run_ssh(launch, 5, stdin_data=command)
    finally:
        release.touch()
    assert wait_exit(env, tmp_path, 'proc_concurrent')['exit_code'] == 0
    assert effect.read_text().splitlines() == ['effect']

def test_status_construction_and_cleanup_do_not_sync_or_close_shared_master(monkeypatch, tmp_path):
    from tools.environments import ssh
    from hermes_constants import get_hermes_home
    monkeypatch.setattr(ssh.tempfile, 'gettempdir', lambda: str(tmp_path))
    monkeypatch.setattr(ssh, 'FileSyncManager', lambda **kw: pytest.fail('status constructed sync manager'))
    monkeypatch.setattr(ssh.SSHEnvironment, '_establish_connection', lambda self: pytest.fail('status connects during construction'))
    first = SSHEnvironment('localhost', 'test', _status_only=True)
    second = SSHEnvironment('localhost', 'test', _status_only=True)
    assert first.control_socket == second.control_socket
    first.control_socket.touch()
    sibling = first._control_socket_for(('FOO',))
    sibling.touch()
    second.cleanup()
    assert first.control_socket.exists() and sibling.exists()
    assert SSHEnvironment('localhost', 'test', key_path='/other-key', _status_only=True).control_socket != first.control_socket
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other-profile'))
    other = SSHEnvironment('localhost', 'test', _status_only=True)
    assert other.control_socket != first.control_socket


def test_remote_cancel_receipt_is_confirmed(tmp_path):
    from tools.environments.ssh_process import request_cancel
    env = ShellTransport()
    child_file = tmp_path / 'child.pid'
    env._run_ssh(launch_command(str(tmp_path), 'proc_cancel'), 5,
                 stdin_data='echo $$ > ' + shlex.quote(str(child_file)) + '; exec sleep 60')
    deadline = time.monotonic() + 5
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    import psutil
    child = psutil.Process(int(child_file.read_text()))
    assert child.is_running()
    request_cancel(env, str(tmp_path), 'proc_cancel')
    state = wait_exit(env, tmp_path, 'proc_cancel')
    assert state['exit_code'] != 0
    assert state['cancel_confirmed'] is True
    assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE


def test_recover_remote_without_sync_under_locked_home(monkeypatch, tmp_path):
    import fcntl
    from tools import process_registry as pr
    from tools.process_registry_remote import recover_remote
    from hermes_constants import get_hermes_home
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    monkeypatch.setattr(SSHEnvironment, '_run_ssh', lambda self, cmd, timeout: ShellTransport()._run_ssh(cmd, timeout))
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    env._sync_manager = type('NoSync', (), {'sync': lambda *a, **kw: pytest.fail('sync called')})()
    # Prevent the test-only manager from participating in object finalization.
    env.cleanup = lambda: None
    ShellTransport()._run_ssh(launch_command(str(tmp_path), 'proc_recover'), 5, stdin_data="printf recovered; exit 4")
    wait_exit(ShellTransport(), tmp_path, 'proc_recover')
    registry = pr.ProcessRegistry()
    entry = {'session_id': 'proc_recover', 'pid_scope': 'sandbox', 'remote_root': str(tmp_path),
             'remote_connection': dict(host='localhost', user='test', port=22, key_path='',
                                     profile_home=str(get_hermes_home()), identity=env._connection_identity)}
    with open(tmp_path / '.sync.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session = recover_remote(registry, entry)
        assert session._completion_event.wait(10)
        assert session.exit_code == 4
        assert session.output_buffer == 'recovered'


def test_sync_upload_failure_blocks_dependent_execution(tmp_path):
    from tools.environments.file_sync import FileSyncManager
    source = tmp_path / 'needed.txt'
    source.write_text('new content')
    def fail(*args):
        raise OSError('upload failed')
    manager = FileSyncManager(lambda: [(str(source), '/remote/needed.txt')], fail, lambda _: None,
                              fail_closed=True)
    env = SSHEnvironment.__new__(SSHEnvironment)
    env._sync_manager = manager
    env.cleanup = lambda: None
    dispatched = []
    def execute_prepared(*args, **kwargs):
        dispatched.append(args)
        return {'returncode': 0, 'output': 'must-not-run'}
    env._execute_prepared = execute_prepared
    with pytest.raises(OSError, match='upload failed'):
        env.execute('printf must-not-run')
    assert manager._synced_files == {}
    assert dispatched == []


def test_failed_receipt_write_keeps_remote_execution_tracked(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools import process_registry_results as results
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_disk', command='true', remote_root='/tmp/test')
    registry._running[session.id] = session
    def fail(*a, **kw):
        raise OSError('disk unavailable')
    monkeypatch.setattr(results, 'atomic_json_write', fail)
    with pytest.raises(OSError, match='disk unavailable'):
        registry._finish_exited(session, 0)
    assert registry._running[session.id] is session
    assert session.id not in registry._finished
    assert not session.exited
    assert registry.poll(session.id)['status'] != 'exited'


def test_log_replacement_identity_and_utf8_terminal_backlog(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env = ShellTransport()
    env._run_ssh(launch_command(str(tmp_path), 'proc_tail'), 5,
                 stdin_data="python3 -c " + shlex.quote("print('한'*100000)"))
    state = wait_exit(env, tmp_path, 'proc_tail')
    log = tmp_path / 'hermes_bg_proc_tail.claim/process.log'
    count = []
    def observation(root, execution, offset):
        count.append(offset)
        if len(count) == 2:
            replacement = tmp_path / 'replacement'
            replacement.write_text('REPLACED\n' + '한' * 100000 + '\n')
            replacement.chmod(0o600)
            replacement.replace(log)
        return observe(env, root, execution, offset)
    env._observe_process = observation
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_tail', command='test', remote_root=str(tmp_path))
    registry._running[session.id] = session
    poll_remote(registry, session, env)
    assert session.exited and session.exit_code == 0
    assert '\ufffd' not in session.output_buffer
    assert session.output_buffer.endswith('한\n')
    assert '[remote log replaced; reading new file]' in session.output_buffer
    assert 0 in count[2:]


def test_repeated_transport_failure_never_finishes_or_relaunches(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    from gateway.run import _drain_gateway_watch_events, _format_gateway_process_notification
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env = ShellTransport()
    effect = tmp_path / 'effects'
    command = "printf '%s\\n' effect >> " + shlex.quote(str(effect)) + '; printf survived'
    env._run_ssh(launch_command(str(tmp_path), 'proc_outage'), 5, stdin_data=command)
    wait_exit(env, tmp_path, 'proc_outage')
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_outage', command=command, remote_root=str(tmp_path),
                             notify_on_complete=True)
    registry._running[session.id] = session
    waits = []
    def wait(delay):
        assert not session.exited
        assert session.id in registry._running
        waits.append(delay)
    session._observation_wake = SimpleNamespace(wait=wait, clear=lambda: None, set=lambda: None)
    attempts = []
    def observation(root, execution, offset):
        attempts.append(offset)
        if len(attempts) <= 6:
            raise TimeoutError('injected transport timeout')
        return observe(env, root, execution, offset)
    env._observe_process = observation
    poll_remote(registry, session, env)
    assert session.exit_code == 0 and session.output_buffer == 'survived'
    assert effect.read_text().splitlines() == ['effect']
    assert len(waits) == 6 and max(waits) <= 72
    notices = _drain_gateway_watch_events(registry.completion_queue)
    assert len(notices) == 1
    assert 'remains tracked' in _format_gateway_process_notification(notices[0])


@pytest.mark.parametrize('response', ['timeout_result', 'transport_exception'])
def test_dispatch_intent_contains_notification_route_before_external_effect(monkeypatch, tmp_path, response):
    from tools import process_registry as pr
    from gateway import session_context
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', tmp_path / 'processes.json')
    routing = {'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_CHAT_ID': '4242',
               'HERMES_SESSION_ID': 'parent'}
    monkeypatch.setattr(session_context, 'async_delivery_supported', lambda: True)
    monkeypatch.setattr(session_context, 'get_session_env', lambda name, default='': routing.get(name, default))
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, 'get_temp_dir', lambda: str(tmp_path))
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(registry, '_track_started', lambda *a, **kw: None)
    intents = []
    def dispatch(command, **kwargs):
        intents.append(json.loads((tmp_path / 'processes.json').read_text())[0])
        from tools.environments.base_output import _popen_bash
        return _popen_bash(['bash', '-c', command], kwargs.get('stdin_data'))
    wait = env._wait_for_process
    def lose_ack(proc, **kwargs):
        wait(proc, **kwargs)
        if response == 'transport_exception':
            raise TimeoutError('launch acknowledgement lost')
        return {'returncode': 124, 'output': ''}
    monkeypatch.setattr(env, '_run_ssh', ShellTransport()._run_ssh)
    monkeypatch.setattr(env, '_run_bash', dispatch)
    monkeypatch.setattr(env, '_wait_for_process', lose_ack)
    marker = tmp_path / 'effect'
    from tools.process_registry_followups import task_request_scope
    with task_request_scope('Create effect exactly once, verify its contents, then report.'):
        session = registry.spawn_via_env(env, f'printf once >> {shlex.quote(str(marker))}; pwd > {shlex.quote(str(tmp_path / "actual-cwd"))}', notify_on_complete=True, cwd=str(tmp_path))
    assert not session.exited and session.id in registry._running
    assert len(intents) == 1
    assert intents[0]['notify_on_complete'] is True
    assert intents[0]['watcher_chat_id'] == '4242'
    assert intents[0]['parent_session_id'] == 'parent'
    scope = intents[0]['verification_scope']
    assert scope['execution_id'] == session.id
    assert scope['parent_session_id'] == 'parent'
    assert scope['automatic_repair_budget'] == 0
    assert scope['task_request'] == 'Create effect exactly once, verify its contents, then report.'
    assert scope == session.verification_scope
    assert wait_exit(ShellTransport(), tmp_path, session.id)['exit_code'] == 0
    assert (tmp_path / 'actual-cwd').read_text().strip() == str(tmp_path)
    assert marker.read_text() == 'once'


@pytest.mark.parametrize('failure', [TimeoutError('sync-back deadline exceeded'), OSError('upload failed')])
def test_sync_failure_cannot_admit_background_execution(monkeypatch, tmp_path, failure):
    from tools import process_registry as pr
    from tools.environments.file_sync import FileSyncManager
    from tools.terminal_tool_background import spawn_background_process

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    checkpoint = tmp_path / 'processes.json'
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', checkpoint)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    monkeypatch.setattr(registry, '_track_started', lambda *a, **kw: None)
    source = tmp_path / 'needed.txt'
    source.write_text('needed input')
    def upload(*args):
        raise failure
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    env._sync_manager = FileSyncManager(lambda: [(str(source), '/remote/needed.txt')],
                                      upload, lambda _: None, fail_closed=True)
    env.cleanup = lambda: None
    monkeypatch.setattr(env, '_run_bash', lambda *a, **kw: pytest.fail('command dispatched'))
    result = json.loads(spawn_background_process(
        env=env, env_type='ssh', command='true', effective_task_id='sync-test', task_id='sync-test',
        session_key='', workdir=None, cwd='/tmp', effective_pty=False, notify_on_complete=False,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None))
    assert result['exit_code'] == -1 and result['error']
    assert 'session_id' not in result
    assert not registry._running
    assert not checkpoint.exists() or json.loads(checkpoint.read_text()) == []


@pytest.mark.parametrize('after_dispatch', [False, True])
def test_checkpoint_failure_preserves_existing_execution_only(monkeypatch, tmp_path, after_dispatch):
    from tools import process_registry as pr
    from tools.terminal_tool_background import spawn_background_process

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    checkpoint = tmp_path / 'processes.json'
    monkeypatch.setattr(pr, 'CHECKPOINT_PATH', checkpoint)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    monkeypatch.setattr(registry, '_track_started', lambda *a, **kw: None)
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, 'get_temp_dir', lambda: str(tmp_path))
    dispatched = []
    def dispatch(command, **kwargs):
        dispatched.append(command)
        from tools.environments.base_output import _popen_bash
        return _popen_bash(['bash', '-c', command], kwargs.get('stdin_data'))
    monkeypatch.setattr(env, '_run_ssh', ShellTransport()._run_ssh)
    monkeypatch.setattr(env, '_run_bash', dispatch)
    write = registry._write_checkpoint
    def fail_checkpoint(*args, **kwargs):
        if not after_dispatch or dispatched:
            raise OSError('checkpoint disk unavailable')
        return write(*args, **kwargs)
    monkeypatch.setattr(registry, '_write_checkpoint', fail_checkpoint)
    marker = tmp_path / 'effect'
    result = json.loads(spawn_background_process(
        env=env, env_type='ssh', command=f'printf once >> {shlex.quote(str(marker))}',
        effective_task_id='checkpoint-test', task_id='checkpoint-test', session_key='',
        workdir=None, cwd='/tmp', effective_pty=False, notify_on_complete=False,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None))
    if not after_dispatch:
        assert result['exit_code'] == -1 and result['error']
        assert 'session_id' not in result and not dispatched and not registry._running
        return
    execution = next(iter(registry._running))
    assert wait_exit(ShellTransport(), tmp_path, execution)['exit_code'] == 0
    assert marker.read_text() == 'once' and len(dispatched) == 1
    assert result['session_id'] == execution
    assert result['exit_code'] == 0 and result['error'] is None
    assert result['setup_incomplete'] is True and result['warning']
    assert 'Do not rerun' in result['output']
    assert json.loads(checkpoint.read_text())[0]['session_id'] == execution


def test_failed_cancel_checkpoint_never_exposes_remote_intent(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.environments import ssh_process
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_cancel_disk', command='sleep', remote_root=str(tmp_path),
                             env_ref=ShellTransport())
    registry._running[session.id] = session
    def fail(**kwargs):
        raise OSError('disk unavailable')
    monkeypatch.setattr(registry, '_write_checkpoint', fail)
    monkeypatch.setattr(ssh_process, 'request_cancel', lambda *a: pytest.fail('cancel sent before persistence'))
    result = registry.kill_process(session.id)
    assert result['status'] == 'error'
    assert not session.cancel_requested


def test_late_cancel_does_not_relabel_success(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools import process_registry_followups as ledger
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_late_success', command='true', remote_root=str(tmp_path),
                             cancel_requested=True, notify_on_complete=True, watcher_platform='telegram')
    registry._running[session.id] = session
    registry._finish_exited(session, 0)
    assert session.completion_reason == 'exited'
    assert ledger.get_state(session.id)['phase'] == 'pending'


@pytest.mark.parametrize('resolve_config', [True, False])
def test_recovery_identity_survives_agent_socket_rotation(monkeypatch, tmp_path, resolve_config):
    from tools.environments import ssh
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if not resolve_config:
        monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 1, '', ''))
    monkeypatch.setenv('SSH_AUTH_SOCK', '/old-agent')
    first = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setenv('SSH_AUTH_SOCK', '/new-agent')
    second = SSHEnvironment('localhost', 'test', _status_only=True)
    assert first._connection_identity == second._connection_identity
    assert first.control_socket != second.control_socket
    other = SSHEnvironment('other-host', 'test', _status_only=True)
    if resolve_config:
        assert other._connection_identity != first._connection_identity
    else:
        assert not first._connection_identity and not other._connection_identity


@pytest.mark.parametrize('broken', ['receipt', 'log'])
def test_observation_preserves_artifact_failure(monkeypatch, tmp_path, broken):
    env = ShellTransport()
    env._run_ssh(launch_command(str(tmp_path), 'proc_artifact'), 5, stdin_data='printf done')
    wait_exit(env, tmp_path, 'proc_artifact')
    path = tmp_path / ('hermes_bg_proc_artifact.claim/process.' + broken)
    if broken == 'receipt':
        path.write_text('{')
    else:
        path.unlink()
    state = observe(env, str(tmp_path), 'proc_artifact', 0)
    assert state['state'] == ('unavailable' if broken == 'receipt' else 'exited')
    assert state['error'] == ('invalid_receipt: JSONDecodeError' if broken == 'receipt' else 'log_unavailable: errno=2')


def test_detached_wrapper_and_shell_do_not_retain_command_in_argv(tmp_path):
    import base64
    import psutil
    from urllib.parse import quote
    env = ShellTransport()
    secret = 'inline-sensitive-test-token'
    command = 'sleep 60; printf done # ' + secret
    env._run_ssh(launch_command(str(tmp_path), 'proc_argv'), 5,
                 stdin_data=command)
    try:
        deadline = time.monotonic() + 5
        processes = []
        while time.monotonic() < deadline:
            state = observe(env, str(tmp_path), 'proc_argv', 0)
            if state.get('identity'):
                wrapper = psutil.Process(state['identity']['pid'])
                processes = [wrapper, *wrapper.children(recursive=True)]
                if any(p.name() == 'bash' for p in processes[1:]):
                    break
            time.sleep(.02)
        assert any(p.name() == 'bash' for p in processes[1:]), 'command shell never became observable'
        # User payload belongs on stdin; only execution metadata may follow the bootstrap.
        assert wrapper.cmdline()[3:] == [str(tmp_path), 'proc_argv', '']
        forbidden = [secret, command]
        for payload in (secret, command):
            forbidden.extend((base64.b64encode(payload.encode()).decode(),
                              payload.encode().hex(), quote(payload, safe='')))
        for process in processes:
            argv = ' '.join(process.cmdline())
            assert all(payload not in argv for payload in forbidden), 'recoverable command exposed in argv'
    finally:
        from tools.environments.ssh_process import request_cancel
        request_cancel(env, str(tmp_path), 'proc_argv')
        wait_exit(env, tmp_path, 'proc_argv')


def test_unconfirmed_launch_is_pollable_and_cancel_request_is_counted(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools import process_registry as pr
    from tools.terminal_tool_background import spawn_background_process
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    monkeypatch.setattr(registry, '_track_started', lambda *a, **kw: None)
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, 'get_temp_dir', lambda: str(tmp_path))
    monkeypatch.setattr(env, '_run_ssh', lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(env, '_execute_prepared', lambda *a, **kw: {'returncode': 124, 'output': ''})
    result = json.loads(spawn_background_process(
        env=env, env_type='ssh', command='true', effective_task_id='t', task_id='t', session_key='',
        workdir=None, cwd='/tmp', effective_pty=False, notify_on_complete=False,
        watch_patterns=None, approval_note=None, pty_disabled_reason=None))
    assert result['dispatch_unconfirmed'] is True and 'Do not rerun' in result['output']
    assert result['session_id'] in registry._running
    assert registry.poll(result['session_id'])['status'] == 'unknown'
    monkeypatch.setattr(env, '_run_ssh', lambda *a, **kw: SimpleNamespace(returncode=0))
    pending_session = registry.get(result['session_id'])
    assert not pending_session._observation_wake.is_set()
    assert registry.kill_all() == 1
    assert pending_session._observation_wake.is_set()
    assert not pending_session._completion_event.is_set()
    assert registry.get(result['session_id']).cancel_requested
    assert not registry.get(result['session_id']).exited


def test_observation_timeout_never_exposes_command_arguments(monkeypatch, tmp_path, caplog):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    from types import SimpleNamespace
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_secret_timeout', command='true', remote_root=str(tmp_path))
    secret = 'PRIVATE_SENTINEL_OBSERVER_ARG'
    def observe_failure(*args):
        raise subprocess.TimeoutExpired(['ssh', secret], 10)
    session._observation_wake.wait = lambda delay: setattr(session, 'exited', True)
    poll_remote(registry, session, SimpleNamespace(_observe_process=observe_failure))
    assert secret not in session.observation_error and secret not in caplog.text
    assert 'TimeoutExpired' in session.observation_error


def test_missing_remote_python_fails_before_intent(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.environments.base import EnvironmentConnectionError
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, '_run_ssh', lambda *a, **k: subprocess.CompletedProcess([], 127, '', 'python3 missing'))
    monkeypatch.setattr(env, '_execute_prepared', lambda *a, **k: pytest.fail('dispatched without prerequisite'))
    registry = ProcessRegistry()
    with pytest.raises(EnvironmentConnectionError, match='Python 3'):
        registry.spawn_via_env(env, 'touch should-not-exist')
    assert not registry._running
    assert not (tmp_path / 'processes.json').exists()


def test_resolved_proxy_change_fences_recovery_and_master(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.environments import ssh
    config = ['hostname original\nproxycommand proxy-one\nidentityagent /old\n']
    monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 0, config[0], ''))
    first = SSHEnvironment('alias', 'test', _status_only=True)
    config[0] = config[0].replace('/old', '/new')
    rotated = SSHEnvironment('alias', 'test', _status_only=True)
    assert first._connection_identity == rotated._connection_identity
    assert first.control_socket != rotated.control_socket
    config[0] = config[0].replace('proxy-one', 'proxy-two')
    changed = SSHEnvironment('alias', 'test', _status_only=True)
    assert changed._connection_identity != first._connection_identity
    assert changed.control_socket != first.control_socket

    from tools.process_registry_remote import recover_remote
    from tools.process_registry import ProcessRegistry
    connection = dict(host='alias', user='test', port=22, key_path='',
                      profile_home=str(tmp_path), identity=first._connection_identity)
    with pytest.raises(ValueError, match='identity changed'):
        recover_remote(ProcessRegistry(), dict(session_id='proc_proxy', remote_root='/tmp/isolated',
                                               remote_connection=connection))


def test_unavailable_ssh_resolution_defers_recovery_without_claiming_identity_change(monkeypatch, tmp_path):
    from tools.environments import ssh
    from tools.process_registry_remote import recover_remote
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 0, 'hostname original\n', ''))
    first = SSHEnvironment('alias', 'test', _status_only=True)
    connection = dict(host='alias', user='test', port=22, key_path='',
                      profile_home=str(tmp_path), identity=first._connection_identity)
    def unavailable(*args, **kwargs):
        raise OSError(11, 'private configuration secret')
    monkeypatch.setattr(ssh, 'run_capture', unavailable)
    registry = ProcessRegistry()
    with pytest.raises(ConnectionError) as failure:
        recover_remote(registry, dict(session_id='proc_resolution', remote_root='/tmp/isolated',
                                     remote_connection=connection))
    message = str(failure.value)
    assert 'resolution unavailable' in message and 'deferred' in message
    assert 'errno=11' in message
    assert 'identity changed' not in message and 'secret' not in message
    assert not registry._running


@pytest.mark.parametrize('reconstruction_result', ['success', 'unavailable', 'identity_mismatch'])
def test_recovery_preserves_cancellation_accepted_during_ssh_reconstruction(monkeypatch, tmp_path, reconstruction_result):
    from tools.environments import ssh
    from tools.environments.ssh_process import request_cancel
    from tools.process_registry import ProcessRegistry
    from tools.process_registry_results import load_completed_results
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('HERMES_SESSION_ID', 'recovery-cancel-owner')
    config = ['hostname original\n']
    monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 0, config[0], ''))
    monkeypatch.setattr(SSHEnvironment, '_run_ssh', lambda self, *a, **k: ShellTransport()._run_ssh(*a, **k))
    env = SSHEnvironment('alias', 'tester', _status_only=True)
    original = ProcessRegistry()
    session = original._new_session('sleep 60', '', '', '', None)
    session.pid_scope = 'sandbox'
    session.remote_root = str(tmp_path)
    session.remote_connection = dict(host='alias', user='tester', port=22, key_path='',
        profile_home=str(tmp_path / 'home'), identity=env._connection_identity)
    original._running[session.id] = session
    original._write_checkpoint(strict=True)
    env._run_ssh(launch_command(str(tmp_path), session.id), 5, stdin_data='sleep 60')
    recovered = ProcessRegistry()
    try:
        config[0] = 'hostname changed\n'
        assert recovered.recover_from_checkpoint() == 0
        placeholder = recovered.get(session.id)
        assert placeholder.env_ref is None
        config[0] = 'hostname original\n'
        accepted = []
        def reconstruct(*args, **kwargs):
            if not accepted:
                accepted.append(recovered.kill_process(session.id))
            if reconstruction_result == 'unavailable':
                raise OSError(11, 'temporary transport configuration failure')
            if reconstruction_result == 'identity_mismatch':
                return subprocess.CompletedProcess([], 0, 'hostname changed\n', '')
            return subprocess.CompletedProcess([], 0, config[0], '')
        monkeypatch.setattr(ssh, 'run_capture', reconstruct)
        assert recovered.retry_checkpoint_recovery() == (1 if reconstruction_result == 'success' else 0)
        assert accepted[0]['status'] == 'cancellation_requested'
        active = recovered.get(session.id)
        assert active is placeholder
        assert active.cancel_requested, 'reconstruction discarded an accepted cancellation'
        if reconstruction_result != 'success':
            assert active.observation_operation == 'ssh_recovery'
            assert active.observation_state == ('unavailable' if reconstruction_result == 'unavailable' else 'identity_mismatch')
            from tools.process_registry_checkpoint import _read_checkpoint
            row = next(row for row in _read_checkpoint(tmp_path / 'home' / 'processes.json') if row['session_id'] == session.id)
            assert row['cancel_requested'], 'failed reconstruction overwrote durable cancellation'
            monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 0, config[0], ''))
            assert recovered.recover_from_checkpoint() == 1
            assert recovered.get(session.id) is placeholder
        assert active._completion_event.wait(10)
        assert active.cancel_confirmed and active.exit_code != 0
        active._reader_thread.join(10)
        assert not active._reader_thread.is_alive()
        receipt = load_completed_results()[session.id]
        assert receipt.cancel_requested and receipt.cancel_confirmed
    finally:
        request_cancel(ShellTransport(), str(tmp_path), session.id)
        wait_exit(ShellTransport(), tmp_path, session.id)


def test_recycled_pid_without_wrapper_lock_is_never_running(tmp_path):
    import os
    root = tmp_path / 'hermes_bg_proc_recycled'
    root.with_suffix('.identity').write_text(json.dumps(dict(execution='proc_recycled',
        pid=os.getpid(), protocol=2, start='old-wrapper')))
    root.with_suffix('.alive').touch()
    root.with_suffix('.log').touch()
    assert observe(ShellTransport(), str(tmp_path), 'proc_recycled', 0)['state'] == 'unavailable'


def test_remote_log_append_after_fstat_is_bounded(tmp_path):
    from tools.environments.ssh_process import _OBSERVE, decode_observation
    base = tmp_path / 'hermes_bg_proc_growth'
    base.with_suffix('.log').write_bytes(b'a')
    base.with_suffix('.log').chmod(0o600)
    # Run the real protocol in a child interpreter; append precisely after its fstat.
    harness = "import os\noriginal=os.fstat\ndef racing(fd):\n stat=original(fd)\n with open(" + repr(str(base.with_suffix('.log'))) + ", 'ab') as f: f.write(b'growth')\n return stat\nos.fstat=racing\n"
    result = subprocess.run(['python3', '-c', harness + _OBSERVE, str(tmp_path), 'proc_growth', '0'],
                            capture_output=True, text=True, check=True)
    chunk = decode_observation(result.stdout)['log']
    assert chunk['next'] <= chunk['size']


def test_private_artifacts_ignore_shared_receipt_and_cancel_traps(tmp_path):
    env = ShellTransport()
    effect, release = tmp_path / 'effect', tmp_path / 'release'
    protected = tmp_path / 'protected'
    protected.write_text('preserve')
    base = tmp_path / 'hermes_bg_proc_private'
    base.with_suffix('.receipt.tmp').symlink_to(protected)
    base.with_suffix('.cancel').touch()
    command = 'printf started > ' + shlex.quote(str(effect)) + '; while [ ! -f ' + shlex.quote(str(release)) + ' ]; do sleep .05; done'
    try:
        env._run_ssh(launch_command(str(tmp_path), 'proc_private'), 5, stdin_data=command)
        deadline = time.monotonic() + 5
        while not effect.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert effect.exists(), 'shared cancellation marker must not stop the execution'
    finally:
        release.touch()
    assert wait_exit(env, tmp_path, 'proc_private')['exit_code'] == 0
    assert protected.read_text() == 'preserve'
    private = tmp_path / 'hermes_bg_proc_private.claim'
    assert private.stat().st_mode & 0o077 == 0
    assert (private / 'process.receipt').exists()


def test_child_start_failure_publishes_terminal_receipt(tmp_path):
    from tools.environments.ssh_process import _RUN
    harness = "import subprocess\ndef refuse(*args, **kwargs): raise OSError(11, 'process capacity exhausted')\nsubprocess.Popen=refuse\n"
    result = subprocess.run(['python3', '-c', harness + _RUN, str(tmp_path), 'proc_startfail', ''],
                            input='true', text=True, capture_output=True)
    state = observe(ShellTransport(), str(tmp_path), 'proc_startfail', 0)
    assert state['state'] == 'exited', result.stderr
    assert state['exit_code'] != 0
    assert 'errno=11' in state['startup_error']


def test_requested_workdir_applies_to_real_remote_child(tmp_path):
    target = tmp_path / 'requested'
    target.mkdir()
    env = ShellTransport()
    env._run_ssh(launch_command(str(tmp_path), 'proc_cwd', str(target)), 5, stdin_data='pwd')
    state = wait_exit(env, tmp_path, 'proc_cwd')
    assert state['log']['bytes'].decode().strip() == str(target)


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_workdir_has_field_specific_feedback_without_dispatch(tmp_path, kind):
    target = tmp_path / 'private-workdir-secret'
    if kind == 'file':
        target.write_text('not a directory')
    effect = tmp_path / 'must-not-run'
    env = ShellTransport()
    execution = 'proc_badcwd_' + kind
    env._run_ssh(launch_command(str(tmp_path), execution, str(target)), 5,
                 stdin_data='touch ' + shlex.quote(str(effect)))
    state = wait_exit(env, tmp_path, execution)
    assert state['exit_code'] != 0 and not effect.exists()
    message = state['startup_error']
    assert 'workdir' in message.lower()
    assert 'existing remote directory' in message
    assert 'omit' in message
    assert str(target) not in message and 'private-workdir-secret' not in message


def test_completed_duplicate_dispatch_never_repeats_effect(tmp_path):
    from tools.environments.ssh_process import _RUN
    import sys
    effect = tmp_path / 'effect'
    argv = [sys.executable, '-c', _RUN, str(tmp_path), 'proc_completed', str(tmp_path)]
    command = 'printf once >> ' + shlex.quote(str(effect))
    for _ in range(2):
        # Wait for the wrapper itself, not the previous receipt, before checking effects.
        subprocess.run(argv, input=command, text=True, check=True, timeout=5)
        assert effect.read_text() == 'once'
    assert observe(ShellTransport(), str(tmp_path), 'proc_completed', 0)['exit_code'] == 0



def test_receipt_finishes_despite_descendant_growing_log(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    env = ShellTransport()
    env._run_ssh(launch_command(str(tmp_path), 'proc_growing'), 5,
                 stdin_data="python3 -c " + shlex.quote("print('a'*200000)"))
    wait_exit(env, tmp_path, 'proc_growing')
    calls = []
    def observation(root, execution, offset):
        calls.append(offset)
        if len(calls) > 6:
            pytest.fail('receipt held hostage by a growing descendant log')
        with (tmp_path / 'hermes_bg_proc_growing.claim/process.log').open('a') as f:
            f.write('b'*70000)
        return observe(env, root, execution, offset)
    env._observe_process = observation
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_growing', command='test', remote_root=str(tmp_path))
    registry._running[session.id] = session
    poll_remote(registry, session, env)
    assert session.exited and session.exit_code == 0
    assert len(calls) <= 4


def test_completed_v3_log_path_survives_result_restore(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_results import save_completed_result, load_completed_results
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_ID', 'owner')
    session = ProcessSession(id='proc_v3restore', command='true', remote_root='/tmp/isolated',
        parent_session_id='owner', remote_identity={'protocol':3}, exited=True, exit_code=0)
    save_completed_result(session, strict=True)
    restored = load_completed_results()[session.id]
    registry = ProcessRegistry()
    registry._finished[session.id] = restored
    assert registry.read_log(session.id)['remote_log_path'] == '/tmp/isolated/hermes_bg_proc_v3restore.claim/process.log'


def test_unsafe_legacy_artifacts_cannot_forge_completion(tmp_path):
    env = ShellTransport()
    identity = {'execution':'proc_unsafelegacy', 'protocol':2, 'pid':1, 'start':'fake'}
    for suffix, data in [('identity',identity), ('receipt', {'identity':identity,'exit_code':0})]:
        path = tmp_path / ('hermes_bg_proc_unsafelegacy.' + suffix)
        path.write_text(json.dumps(data)); path.chmod(0o666)
    state = observe(env, str(tmp_path), 'proc_unsafelegacy', 0)
    assert state['state'] != 'exited'


def test_ssh_prerequisite_transport_error_is_not_python_hint(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    from tools.environments.base import EnvironmentConnectionError
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env=SSHEnvironment('localhost','test',_status_only=True)
    monkeypatch.setattr(env,'_run_ssh',lambda *a,**k:subprocess.CompletedProcess([],255,'','Permission denied; token=sk-testSensitiveTransportKey123456789'))
    registry=ProcessRegistry()
    with pytest.raises(EnvironmentConnectionError) as error:
        registry.spawn_via_env(env,'touch must-not-dispatch')
    assert 'exit 255' in str(error.value)
    assert 'sk-testSensitiveTransportKey' not in str(error.value)
    assert 'transport/authentication' in error.value.reason
    assert 'SSH connectivity/authentication' in error.value.retry_hint
    assert 'Python' not in error.value.reason + error.value.retry_hint
    assert not registry._running and not (tmp_path/'processes.json').exists()


def test_unframed_launch_retains_sanitized_transport_cause(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    env = SSHEnvironment('localhost', 'test', _status_only=True)
    monkeypatch.setattr(env, '_run_ssh', lambda *a, **kw: subprocess.CompletedProcess([], 0, '', ''))
    monkeypatch.setattr(env, '_execute_prepared', lambda *a, **kw: {
        'returncode': 255, 'output': 'connection reset; token=sk-testSensitiveLaunchKey123456789'})
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, '_track_started', lambda *a: registry._write_checkpoint(strict=True))
    session = registry.spawn_via_env(env, 'touch must-not-replay')
    assert not session.exited and session.pid is None
    row = json.loads((tmp_path / 'processes.json').read_text())[0]
    assert 'exit 255' in row['observation_error'] and 'connection reset' in row['observation_error']
    assert 'sk-testSensitiveLaunchKey' not in row['observation_error']
    assert row['observation_operation'] == 'dispatch'


def test_cancel_failure_and_missing_log_both_survive_completion(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    from tools.environments import ssh_process
    from tools import process_registry_followups as followups
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_bothdiag', command='true', remote_root='/tmp/isolated',
        cancel_requested=True, profile_home=str(tmp_path), notify_on_complete=True, watcher_platform='telegram')
    registry = ProcessRegistry(); registry._running[session.id] = session
    def fail(*args):
        raise ConnectionError('cancel connection reset')
    monkeypatch.setattr(ssh_process, 'request_cancel', fail)
    poll_remote(registry, session, SimpleNamespace(_observe_process=lambda *a: {
        'state': 'exited', 'exit_code': 0, 'error': 'log_unavailable: errno=2'}))
    assert session.exited
    payload = json.loads(followups.pending()[0]['payload'])
    assert 'cancel connection reset' in payload['observation_error']
    assert 'log_unavailable: errno=2' in payload['observation_error']


def test_failed_cancel_write_remains_visible_with_healthy_observation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    from tools.environments import ssh_process
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    session=ProcessSession(id='proc_canceldiag',command='test',remote_root='/tmp/isolated',cancel_requested=True,
        profile_home=str(tmp_path))
    registry=ProcessRegistry();registry._running[session.id]=session
    def refuse(*a):raise OSError(13,'permission denied token=sk-testSensitiveCancelKey123456789')
    monkeypatch.setattr(ssh_process,'request_cancel',refuse)
    session._observation_wake.wait=lambda delay:setattr(session,'exited',True)
    poll_remote(registry,session,SimpleNamespace(_observe_process=lambda *a:dict(state='running')))
    error=session.observation_error
    assert 'cancel write unconfirmed' in error and 'errno=13' in error
    assert 'sk-testSensitiveCancelKey' not in error
    assert json.loads((tmp_path/'processes.json').read_text())[0]['observation_error']==error


def test_log_path_is_withheld_until_protocol_is_observed(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_unobserved', command='true', remote_root='/tmp/isolated')
    registry._running[session.id] = session
    assert 'remote_log_path' not in registry.read_log(session.id)
    for protocol, expected in [(3, '.claim/process.log'), (2, '.log')]:
        session.remote_identity = {'protocol': protocol}
        assert registry.read_log(session.id)['remote_log_path'].endswith(expected)


def test_observation_ignores_login_stdout_banner(tmp_path):
    class BannerTransport(ShellTransport):
        def _run_ssh(self, *args, **kwargs):
            result = super()._run_ssh(*args, **kwargs)
            result.stdout = 'Welcome to the remote host\n' + result.stdout
            return result
    env = BannerTransport()
    env._run_ssh(launch_command(str(tmp_path), 'proc_banner'), 5, stdin_data='true')
    assert wait_exit(env, tmp_path, 'proc_banner')['exit_code'] == 0


@pytest.mark.live_system_guard_bypass  # Only signals the test-created, identity-tracked detached child.
def test_cancel_reports_direct_group_scope_when_descendant_detaches(tmp_path):
    import psutil
    from tools.environments.ssh_process import request_cancel
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_notifications import format_process_notification
    pid_file = tmp_path / 'detached.pid'
    code = ('import subprocess,time,pathlib; '
            'p=subprocess.Popen(["python3","-c","import time;time.sleep(60)"],start_new_session=True); '
            f'pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)')
    env = ShellTransport()
    descendant = None
    try:
        env._run_ssh(launch_command(str(tmp_path), 'proc_detached'), 5,
                     stdin_data='python3 -c ' + shlex.quote(code) + ' & wait')
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        descendant = psutil.Process(int(pid_file.read_text()))
        request_cancel(env, str(tmp_path), 'proc_detached')
        state = wait_exit(env, tmp_path, 'proc_detached')
        assert descendant.is_running()
        assert state['cancellation_scope'] == 'direct_process_group'
        assert state['execution_tree_termination_confirmed'] is False
        session = ProcessSession(id='proc_detached', command='test', remote_root=str(tmp_path),
                                 exited=True, cancel_confirmed=True, completion_reason='killed')
        status = ProcessRegistry._status_head(session)
        assert status['execution_tree_termination_confirmed'] is False
        event = dict(session_id=session.id, command='test', **ProcessRegistry._exit_fields(session))
        assert 'detached descendants may remain' in format_process_notification(event)
    finally:
        if descendant is not None and descendant.is_running():
            descendant.kill()
            try: descendant.wait(timeout=5)
            except psutil.TimeoutExpired: pass


def test_durable_cancel_transport_failure_returns_pending_intent(monkeypatch, tmp_path):
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.environments import ssh_process
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_pendingcancel', command='sleep', remote_root='/tmp/isolated',
                             profile_home=str(tmp_path), env_ref=ShellTransport())
    registry._running[session.id] = session
    def outage(*args): raise ConnectionError('remote unavailable')
    monkeypatch.setattr(ssh_process, 'request_cancel', outage)
    result = registry.kill_process(session.id)
    assert result['status'] == 'cancellation_requested'
    assert 'remote unavailable' in result['cancel_transport_error']
    assert result['termination_confirmed'] is False
    assert json.loads((tmp_path/'processes.json').read_text())[0]['cancel_requested'] is True


def test_completed_result_missing_exit_code_is_not_a_completion(monkeypatch, tmp_path):
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import save_completed_result, load_completed_results
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_ID', 'owner')
    session = ProcessSession(id='proc_corruptresult', command='true', parent_session_id='owner',
                             exited=True, exit_code=0)
    save_completed_result(session, strict=True)
    path = tmp_path/'logs/process-results'/f'{session.id}.json'
    record = json.loads(path.read_text())
    del record['exit_code']
    path.write_text(json.dumps(record))
    assert session.id not in load_completed_results()


@pytest.mark.parametrize('startup', ['close_descriptors', 'payload_unavailable'])
def test_login_startup_cannot_silently_discard_command(tmp_path, startup):
    from tools.environments.ssh_process import _RUN
    import sys
    effect = tmp_path / 'effect'
    # Execute real Bash with a test-controlled startup action before its bootstrap.
    harness = """import subprocess, shlex
original = subprocess.Popen
def startup_action(args, **kwargs):
    if args[0] == 'bash':
        args = list(args)
        if STARTUP == 'close_descriptors':
            args[2] = 'exec 3<&- 4<&- 5<&- 6<&- 7<&- 8<&- 9<&-; ' + args[2]
        else:
            words = shlex.split(args[2])
            payload = words[words.index('<') + 1]
            args[2] = 'rm -f -- ' + shlex.quote(payload) + '; ' + args[2]
    return original(args, **kwargs)
subprocess.Popen = startup_action
"""
    harness = 'STARTUP = ' + repr(startup) + '\n' + harness
    result = subprocess.run([sys.executable, '-c', harness + _RUN, str(tmp_path), 'proc_loginfd', ''],
        input='printf once > ' + shlex.quote(str(effect)), text=True, capture_output=True, timeout=10)
    state = observe(ShellTransport(), str(tmp_path), 'proc_loginfd', 0)
    assert state['state'] == 'exited', result.stderr
    if startup == 'close_descriptors':
        assert effect.exists() and effect.read_text() == 'once'
        assert state['exit_code'] == 0
    else:
        assert not effect.exists() and state['exit_code'] != 0


@pytest.mark.parametrize('startup', ['change_directory', 'remove_workdir'])
def test_requested_workdir_survives_login_startup(tmp_path, startup):
    from tools.environments.ssh_process import _RUN
    import sys
    target = tmp_path / "requested workdir's"
    other = tmp_path / 'login-home'
    target.mkdir(); other.mkdir()
    harness = """import subprocess, shlex
original = subprocess.Popen
def startup_action(args, **kwargs):
    if args[0] == 'bash':
        args = list(args)
        action = 'builtin cd -- ' + shlex.quote(OTHER)
        if STARTUP == 'remove_workdir':
            action += '; rmdir -- ' + shlex.quote(TARGET)
        args[2] = action + '; ' + args[2]
    return original(args, **kwargs)
subprocess.Popen = startup_action
"""
    harness = 'STARTUP = ' + repr(startup) + '\nOTHER = ' + repr(str(other)) + '\nTARGET = ' + repr(str(target)) + '\n' + harness
    result = subprocess.run([sys.executable, '-c', harness + _RUN, str(tmp_path), 'proc_logincwd', str(target)],
        input='printf result > artifact.txt; pwd', text=True, capture_output=True, timeout=10)
    state = observe(ShellTransport(), str(tmp_path), 'proc_logincwd', 0)
    assert state['state'] == 'exited', result.stderr
    assert not (other / 'artifact.txt').exists(), 'login initialization redirected the requested effect'
    if startup == 'change_directory':
        assert (target / 'artifact.txt').read_text() == 'result'
        assert state['exit_code'] == 0
        assert state['log']['bytes'].decode().strip() == str(target)
    else:
        assert state['exit_code'] != 0



@pytest.mark.parametrize('requested', ['jobs', '..'])
@pytest.mark.parametrize('startup', ['unchanged', 'change_directory'])
def test_relative_workdir_is_resolved_once_before_login(tmp_path, requested, startup):
    from tools.environments.ssh_process import _RUN
    import sys
    origin = tmp_path / 'origin' / 'nested'
    origin.mkdir(parents=True)
    (origin / 'jobs').mkdir()
    target = (origin / requested).resolve()
    other = tmp_path / 'login-home'
    other.mkdir()
    harness = """import subprocess, shlex
original = subprocess.Popen
def startup_action(args, **kwargs):
    if args[0] == 'bash':
        args = list(args)
        args[1] = '-c'
        if STARTUP == 'change_directory':
            args[2] = 'builtin cd -- ' + shlex.quote(OTHER) + '; ' + args[2]
    return original(args, **kwargs)
subprocess.Popen = startup_action
"""
    harness = 'STARTUP = ' + repr(startup) + '\nOTHER = ' + repr(str(other)) + '\n' + harness
    result = subprocess.run([sys.executable, '-c', harness + _RUN, str(tmp_path), 'proc_relativecwd', requested],
        cwd=origin, input='printf result > artifact.txt; pwd', text=True, capture_output=True, timeout=10)
    state = observe(ShellTransport(), str(tmp_path), 'proc_relativecwd', 0)
    assert state['state'] == 'exited', result.stderr
    assert state['exit_code'] == 0
    assert (target / 'artifact.txt').read_text() == 'result'
    assert state['log']['bytes'].decode().strip() == str(target)
    assert not (tmp_path / 'artifact.txt').exists()
    assert not (other / 'artifact.txt').exists()


def test_nul_command_reports_actionable_validation_without_dispatch(tmp_path):
    env = ShellTransport()
    effect = tmp_path / 'must-not-run'
    env._run_ssh(launch_command(str(tmp_path), 'proc_nul'), 5,
                 stdin_data='touch ' + shlex.quote(str(effect)) + '\0secret-command-content')
    state = wait_exit(env, tmp_path, 'proc_nul')
    assert state['exit_code'] != 0 and not effect.exists()
    assert 'command' in state['startup_error'].lower()
    assert 'NUL' in state['startup_error']
    assert 'textual shell escape' in state['startup_error']
    assert 'secret-command-content' not in state['startup_error']


def test_detached_wrapper_retains_receipt_storage_failure(tmp_path, monkeypatch):
    from tools.environments.ssh_process import _LAUNCH, _RUN
    harness = """import os
original_replace = os.replace
def fail_receipt(source, destination):
    if destination.endswith('.receipt'):
        raise OSError(28, 'private storage diagnostic token=secret')
    return original_replace(source, destination)
os.replace = fail_receipt
"""
    env = ShellTransport()
    launch = shlex.join(['python3', '-c', _LAUNCH, str(tmp_path), 'proc_receiptfail', '', harness + _RUN])
    result = env._run_ssh(launch, 5, stdin_data='printf effect-completed')
    assert result.returncode == 0
    deadline = time.monotonic() + 10
    while True:
        state = observe(env, str(tmp_path), 'proc_receiptfail', 0)
        if state['state'] != 'running' and state.get('identity'):
            break
        if time.monotonic() >= deadline:
            pytest.fail('wrapper failure did not settle')
        time.sleep(.05)
    assert state['state'] == 'unavailable' and 'exit_code' not in state
    assert 'errno=28' in state.get('error', '')
    assert 'wrapper' in state['error']
    assert 'private storage' not in state['error'] and 'secret' not in state['error']
    assert state['log']['bytes'] == b'effect-completed'
    from types import SimpleNamespace
    from tools.process_registry import ProcessRegistry, ProcessSession
    from tools.process_registry_remote import poll_remote
    home = tmp_path / 'profile'
    monkeypatch.setenv('HERMES_HOME', str(home))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_receiptfail', command='printf effect-completed',
        remote_root=str(tmp_path), remote_connection={'profile_home': str(home)},
        profile_home=str(home))
    registry._running[session.id] = session
    session._observation_wake.wait = lambda delay: setattr(session, 'exited', True)
    transport = SimpleNamespace(_observe_process=lambda root, execution, offset: observe(env, root, execution, offset))
    poll_remote(registry, session, transport)
    row = json.loads((home / 'processes.json').read_text())[0]
    assert row['observation_state'] == 'unavailable'
    assert row['observation_operation'] == 'wrapper'
    assert 'errno=28' in row['observation_error']
    assert 'secret' not in row['observation_error']
