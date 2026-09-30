"""Fault injection against an isolated real OpenSSH server, never the user's master."""
import getpass
import shutil
import socket
import subprocess
import time

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments.ssh_process import launch_command


@pytest.fixture
def ssh_server(tmp_path):
    sshd = shutil.which('sshd') or '/usr/sbin/sshd'
    if not shutil.which('ssh-keygen') or not __import__('os').path.exists(sshd):
        pytest.skip('OpenSSH server required')
    key = tmp_path / 'key'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = tmp_path / 'sshd_config'
    config.write_text(f'ListenAddress 127.0.0.1\nPort {port}\nHostKey {key}\nPidFile {tmp_path}/pid\n'
                      f'AuthorizedKeysFile {key}.pub\nStrictModes no\nPasswordAuthentication no\nUsePAM no\n')
    with open(tmp_path / 'sshd.log', 'w') as log:
        server = subprocess.Popen([sshd, '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
        try:
            for _ in range(40):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            assert server.poll() is None, (tmp_path / 'sshd.log').read_text()
            yield port, key
        finally:
            server.terminate()
            server.wait(timeout=5)


def test_sibling_cleanup_outage_reconnect_and_locked_status(ssh_server, monkeypatch, tmp_path):
    import fcntl
    from tools.environments import ssh
    port, key = ssh_server
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    # Short socket dir also exercises macOS sun_path bounds.
    original = SSHEnvironment._build_ssh_command
    def isolated(self, extra_args=None, send_env=()):
        return original(self, ['-F', '/dev/null', '-o', 'UserKnownHostsFile=/dev/null',
                              '-o', 'StrictHostKeyChecking=no', *(extra_args or [])], send_env)
    monkeypatch.setattr(SSHEnvironment, '_build_ssh_command', isolated)
    first = SSHEnvironment('127.0.0.1', getpass.getuser(), port=port, key_path=str(key), _status_only=True)
    sibling = SSHEnvironment('127.0.0.1', getpass.getuser(), port=port, key_path=str(key), _status_only=True)
    try:
        result = first._run_ssh(launch_command(str(tmp_path), 'proc_live'), 5, stdin_data='sleep 1; python3 -c "print(chr(120)*200000)"; printf live; exit 3' )
        assert result.returncode == 0
        sibling.cleanup()
        assert first.control_socket.exists()
        (tmp_path / 'home').mkdir(exist_ok=True)
        with open(tmp_path / 'home' / '.sync.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            locked_at = time.monotonic()
            # Close only this test's master, simulating disconnect. The observer must reconnect.
            subprocess.run(first._build_ssh_command(['-O', 'exit']), capture_output=True, timeout=5)
            observer = SSHEnvironment('127.0.0.1', getpass.getuser(), port=port, key_path=str(key), _status_only=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = observer._observe_process(str(tmp_path), 'proc_live', 0)
                if state['state'] == 'exited':
                    break
                time.sleep(.1)
            assert state['state'] == 'exited'
            assert state['exit_code'] == 3
            assert len(state['log']['bytes']) == 65536
            from tools.process_registry import ProcessRegistry, ProcessSession
            from tools.process_registry_remote import poll_remote
            registry = ProcessRegistry()
            session = ProcessSession(id='proc_live', command='large log', remote_root=str(tmp_path))
            registry._running[session.id] = session
            poll_remote(registry, session, observer)
            assert session.exited and session.exit_code == 3
            assert session.output_buffer.endswith('live')
            assert 'backlog omitted' in session.output_buffer
            # Keep the real flock held for the declared 60-second fault case.
            time.sleep(max(0, 60 - (time.monotonic() - locked_at)))
            assert time.monotonic() - locked_at >= 60
    finally:
        subprocess.run(first._build_ssh_command(['-O', 'exit']), capture_output=True, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('crash_boundary', ['none', 'after_dispatch', 'before_reserve', 'after_reserve', 'before_verification', 'after_verification', 'missing_receipt', 'corrupt_receipt', 'pid_reuse', 'storage_failure', 'verification_failed', 'approval_wait'])
async def test_real_ssh_completion_reaches_gateway_report_and_survives_restart(ssh_server, monkeypatch, tmp_path, crash_boundary):
    """Real SSH, registry, adapter dispatch and delivery ledger; only LLM/transport replaced."""
    import asyncio
    import json
    from pathlib import Path
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.session_context import set_session_vars, clear_session_vars
    from gateway.session import SessionSource
    from tools.process_registry import ProcessRegistry
    from tools import process_registry_followups as ledger
    from gateway.process_followups import reconcile
    # Reuse the existing fake transport/model bootstrap; all gateway methods remain real.
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'gateway'))
    from test_queued_followup_processing_hooks import HookRecordingAdapter, _install_fake_agent
    marker = tmp_path / 'effect'
    calls = []
    class Verifier:
        def __init__(self, **kwargs):
            self.tools = []
        def run_conversation(self, message, **kwargs):
            calls.append(message)
            registry.read_log(session.id)
            assert marker.read_text() == 'once'
            assert 'process_verification' in message
            return {'final_response': 'Checked artifact.\n```process_verification\n' + json.dumps({
                'outcome': crash_boundary if crash_boundary in {'verification_failed','approval_wait'} else 'verified',
                'evidence': ['artifact contains once'], 'next_action': 'Inspect remaining artifact requirement' if crash_boundary == 'verification_failed' else ('Await user authorization for remaining action' if crash_boundary == 'approval_wait' else 'none'),
                'repair_attempts': 0}) + '\n```', 'messages': [], 'api_calls': 1}
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'tester')
    _install_fake_agent(monkeypatch, tmp_path / 'home', Verifier)
    from gateway.run import GatewayRunner
    runner = GatewayRunner(config=GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token='x')}))
    adapter = HookRecordingAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    adapter.set_message_handler(runner._handle_message)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='4242', chat_type='dm', user_id='tester')
    entry = runner.session_store.get_or_create_session(source)
    port, key = ssh_server
    original = SSHEnvironment._build_ssh_command
    def isolated(self, extra_args=None, send_env=()):
        return original(self, ['-F', '/dev/null', '-o', 'UserKnownHostsFile=/dev/null',
                              '-o', 'StrictHostKeyChecking=no', *(extra_args or [])], send_env)
    monkeypatch.setattr(SSHEnvironment, '_build_ssh_command', isolated)
    env = SSHEnvironment('127.0.0.1', getpass.getuser(), port=port, key_path=str(key), _status_only=True)
    monkeypatch.setattr(env, 'get_temp_dir', lambda: str(tmp_path))
    import shlex
    tokens = set_session_vars(platform='telegram', chat_id='4242', user_id='tester',
                              session_key=entry.session_key, session_id=entry.session_id)
    try:
        registry = ProcessRegistry()
        command = f'printf once >> {shlex.quote(str(marker))} # inline-test-secret'
        original_bash = env._run_bash
        def checked_bash(cmd, **kwargs):
            import base64
            assert 'inline-test-secret' not in cmd
            assert base64.b64encode(command.encode()).decode() not in cmd
            return original_bash(cmd, **kwargs)
        monkeypatch.setattr(env, '_run_bash', checked_bash)
        storage_path = tmp_path / 'home/logs/process-results'
        if crash_boundary == 'storage_failure':
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_text('not a directory')
        if crash_boundary in {'none','storage_failure','verification_failed','approval_wait'}:
            session = registry.spawn_via_env(env, command, session_key=entry.session_key, notify_on_complete=True)
        else:
            import sys
            child = subprocess.run([sys.executable, '-c', _CRASH_WORKER], input=json.dumps(dict(
                home=str(tmp_path / 'home'), root=str(tmp_path), user=getpass.getuser(), port=port,
                key=str(key), session_key=entry.session_key, session_id=entry.session_id,
                command=command, boundary='after_dispatch' if crash_boundary in {'missing_receipt','corrupt_receipt','pid_reuse'} else crash_boundary)), text=True, capture_output=True, timeout=20)
            assert child.returncode == 42, child.stderr
            saved_receipt = None
            if crash_boundary in {'missing_receipt','corrupt_receipt','pid_reuse'}:
                rows=json.loads((tmp_path/'home/processes.json.ssh-v2').read_text())
                execution=rows[0]['session_id']
                receipt=tmp_path/f'hermes_bg_{execution}.claim/process.receipt'
                for _ in range(100):
                    if receipt.exists():break
                    await asyncio.sleep(.05)
                saved_receipt=receipt.read_text()
                if crash_boundary=='missing_receipt':receipt.unlink()
                elif crash_boundary=='corrupt_receipt':receipt.write_text('{')
                else:
                    altered=json.loads(saved_receipt);altered['identity']['pid']+=1
                    receipt.write_text(json.dumps(altered))
                observed=env._observe_process(str(tmp_path),execution,0)
                assert observed['state']!='exited'
            recovered = registry.recover_from_checkpoint()
            if crash_boundary in {'before_verification', 'after_verification'}:
                await reconcile(runner)
                state = ledger.get_state(json.loads((tmp_path / 'verification-execution').read_text()))
                assert state['phase'] == 'needs_reconciliation_reported'
                await reconcile(runner)
                assert calls == [] and marker.read_text() == 'once'
                effect = tmp_path / 'verification-effect'
                assert effect.exists() == (crash_boundary == 'after_verification')
                if effect.exists():
                    assert effect.read_text() == 'once'
                return
            assert recovered == 1
            session = next(iter({**registry._running, **registry._finished}.values()))
        if crash_boundary in {'missing_receipt','corrupt_receipt','pid_reuse','storage_failure'}:
            for _ in range(100):
                if session.observation_state in {'unavailable','identity_mismatch'}:break
                await asyncio.sleep(.05)
            assert not session.exited and calls==[]
            assert marker.read_text()=='once'
            if crash_boundary=='storage_failure':
                storage_path.unlink(); storage_path.mkdir()
            else:
                receipt.write_text(saved_receipt)
                receipt.chmod(0o600)
            session._observation_wake.set()
        assert await asyncio.to_thread(session._completion_event.wait, 15)
        assert ledger.get_state(session.id)['phase'] == 'pending'
        await reconcile(runner)
        deadline = time.monotonic() + 15
        while ledger.get_state(session.id)['phase'] != 'turn_finished' and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        assert ledger.get_state(session.id)['phase'] == 'turn_finished'
        from gateway.delivery_ledger import turn_delivery_state
        with ledger._db() as db:
            payload = json.loads(db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (session.id,)).fetchone()[0])
        while turn_delivery_state(entry.session_key, payload['followup_turn_id']) != 'delivered' and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        assert turn_delivery_state(entry.session_key, payload['followup_turn_id']) == 'delivered'
        with ledger._db() as db:
            db.execute('UPDATE process_followups SET next_attempt=0')
        # New runner reopens the real durable stores; it must settle delivery without a second turn.
        restarted = GatewayRunner(config=runner.config)
        restarted.adapters = runner.adapters
        await reconcile(restarted)
        expected=crash_boundary if crash_boundary in {'verification_failed','approval_wait'} else 'reported'
        assert ledger.get_state(session.id)['phase'] == expected
        timings = ledger.get_state(session.id)['timings']
        assert timings['execution_seconds'] >= 0
        assert timings['verification_wait_seconds'] >= 0
        assert timings['verification_turn_seconds'] >= timings['approval_wait_seconds']
        assert len(calls) == 1 and marker.read_text() == 'once'
    finally:
        clear_session_vars(tokens)
        subprocess.run(env._build_ssh_command(['-O', 'exit']), capture_output=True, timeout=5)


_CRASH_WORKER = r'''
import json, os, sys
args = json.load(sys.stdin)
os.environ['HERMES_HOME'] = args['home']
from tools.environments.ssh import SSHEnvironment
from tools.process_registry import ProcessRegistry
from tools import process_registry_followups as ledger
from gateway.session_context import set_session_vars
original = SSHEnvironment._build_ssh_command
def isolated(self, extra_args=None, send_env=()):
    return original(self, ['-F', '/dev/null', '-o', 'UserKnownHostsFile=/dev/null',
                          '-o', 'StrictHostKeyChecking=no', *(extra_args or [])], send_env)
SSHEnvironment._build_ssh_command = isolated
env = SSHEnvironment('127.0.0.1', args['user'], port=args['port'], key_path=args['key'], _status_only=True)
env.get_temp_dir = lambda: args['root']
set_session_vars(platform='telegram', chat_id='4242', user_id='tester',
                 session_key=args['session_key'], session_id=args['session_id'])
registry = ProcessRegistry()
if args['boundary'] == 'after_dispatch':
    registry._track_started = lambda *a, **kw: os._exit(42)
elif args['boundary'] in {'before_reserve', 'after_reserve'}:
    reserve = ledger.reserve
    def crash_reserve(session):
        if args['boundary'] == 'after_reserve':
            reserve(session)
        os._exit(42)
    ledger.reserve = crash_reserve
session = registry.spawn_via_env(env, args['command'], session_key=args['session_key'], notify_on_complete=True)
assert session._completion_event.wait(15)
if args['boundary'] in {'before_verification', 'after_verification'}:
    from pathlib import Path
    from types import SimpleNamespace
    sys.path.insert(0, str(Path.cwd() / 'tests/gateway'))
    from test_durable_process_followups import Runner
    from gateway.platforms.base import MessageEvent
    from gateway.turn_context import TurnContext
    from gateway.run_turn_runner import TurnRunner
    runner = Runner(Path(args['home']))
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token'])
    Path(args['root'], 'verification-execution').write_text(json.dumps(session.id))
    def run(message, **kwargs):
        if args['boundary'] == 'before_verification':
            os._exit(42)
        Path(args['root'], 'verification-effect').write_text('once')
        return {'final_response': 'checked artifact', 'messages': []}
    ledger.finish = lambda *a, **kw: os._exit(42)
    event = MessageEvent(text='Verify the execution', source=runner.source,
                        metadata={'process_followup': {'execution_id': session.id, 'token': row['token']}})
    context = TurnContext(event=event, source=runner.source, session_key=args['session_key'],
                          session_id=args['session_id'], message=event.text)
    TurnRunner(runner, context)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
sys.exit(99)
'''


def test_simultaneous_profiles_keep_remote_receipts_separate(ssh_server, tmp_path):
    import json
    import sys
    import shlex
    port, key = ssh_server
    children = []
    release = tmp_path / 'release'
    worker = _CRASH_WORKER.replace("sys.exit(99)", """
if args['boundary'] == 'profile':
    print(json.dumps({'execution': session.id, 'followups': [r['execution_id'] for r in ledger.pending()],
                      'identity': env._connection_identity}))
    sys.exit(0)
sys.exit(99)
""")
    try:
        for name in ['a', 'b']:
            root = tmp_path / name
            root.mkdir()
            command = f'touch {shlex.quote(str(root / "ready"))}; while ! test -f {shlex.quote(str(release))}; do sleep .05; done; printf {name}'
            args = dict(home=str(root / 'home'), root=str(root), user=getpass.getuser(), port=port,
                        key=str(key), session_key='agent:main:telegram:dm:4242', session_id=name,
                        command=command, boundary='profile')
            proc = subprocess.Popen([sys.executable, '-c', worker], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.stdin.write(json.dumps(args))
            proc.stdin.close()
            proc.stdin = None
            children.append(proc)
        deadline = time.monotonic() + 12
        while not all((tmp_path / n / 'ready').exists() for n in ['a', 'b']) and time.monotonic() < deadline:
            time.sleep(.05)
        assert all((tmp_path / n / 'ready').exists() for n in ['a', 'b'])
        release.touch()
        results = []
        for proc in children:
            stdout, stderr = proc.communicate(timeout=15)
            assert proc.returncode == 0, stderr
            results.append(json.loads(stdout.splitlines()[-1]))
        assert results[0]['identity'] != results[1]['identity']
        assert results[0]['execution'] != results[1]['execution']
        for result in results:
            assert result['followups'] == [result['execution']]
    finally:
        release.touch()
        for proc in children:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
