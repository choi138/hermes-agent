"""Real CLIs and real child pipes; SSH executable and HTTP are LOCAL FIXTURES."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agent.delegation_progress import Manifest, Progress, _atomic, _lock, set_stage

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / 'scripts/delegation_progress_bridge.py'
SERVER = ROOT / 'scripts/delegation_progress_discord_send.py'


@pytest.fixture
def lane(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'runner.py').write_text('value=1')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir(mode=0o700)
    data = dict(run_id='fixture-run', worktree=str(repo), approved_root=str(repo), artifact_root=str(artifacts),
                event_path=str(artifacts / 'events.jsonl'), receipt_path=str(artifacts / 'status.json'),
                thread_id='123456789', task_label='fixture')
    (artifacts / 'events.jsonl').write_text('')
    manifest = tmp_path / 'manifest.json'
    _atomic(manifest, data)
    state = tmp_path / 'state'
    p = Progress(Manifest.load(manifest), state)
    p.tick(now=0)
    return p, manifest, artifacts


def bridge_args(p, manifest):
    return ['--manifest', str(manifest), '--state-dir', str(p.root), '--ssh-host', 'fixture-host',
            '--remote-python', '/usr/bin/python3', '--runtime-root', '/srv/fixture',
            '--helper-path', '/srv/helper.py', '--allow-thread', '123456789', '--poll-interval', '.05']


def bootstrap(tmp_path, body, *, timeout=None):
    fake = tmp_path / 'fake_ssh.py'
    fake.write_text(body)
    boot = tmp_path / 'bootstrap.py'
    boot.write_text('''import runpy, sys, subprocess
sys.path.insert(0, ''' + repr(str(ROOT)) + ''')
import agent.delegation_progress_delivery as delivery
original = delivery.SSHSender.__init__
def popen(argv, **kwargs):
    assert argv[0] == '/usr/bin/ssh' and kwargs['shell'] is False
    return subprocess.Popen([sys.executable, ''' + repr(str(fake)) + ''', *argv[1:]], **kwargs)
def transport(argv, data, timeout):
    return delivery.ssh_transport(argv, data, ''' + (repr(timeout) if timeout else 'timeout') + ''', popen=popen)
def init(self, *args, **kwargs):
    original(self, *args, **kwargs, transport=transport)
delivery.SSHSender.__init__ = init
sys.argv = [''' + repr(str(BRIDGE)) + ''', *sys.argv[1:]]
runpy.run_path(''' + repr(str(BRIDGE)) + ''', run_name='__main__')
''')
    return boot


GOOD = '''import json, sys
v = json.load(sys.stdin)
assert set(v) == {'run_id','sequence','thread_id','content','content_digest'}
assert 'PRIVATE_PROMPT' not in json.dumps(v)
print(json.dumps(dict(status='verified', run_id=v['run_id'], sequence=v['sequence'],
    thread_id=v['thread_id'], content_digest=v['content_digest'], message_id=str(987654321+v['sequence']))))
'''


def test_actual_bridge_final_pending_ack_then_exit(lane, tmp_path):
    p, manifest, _ = lane
    p.tick(now=300)
    set_stage(manifest, 'final_verified')
    result = subprocess.run([sys.executable, str(bootstrap(tmp_path, GOOD)), *bridge_args(p, manifest)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['status'] == 'stopped_and_delivered'
    assert p.peek() is None
    assert len(json.loads((p.directory / 'delivery.json').read_text())['records']) == 2


@pytest.mark.parametrize('body', ['import sys; sys.stdin.read()',
    'import sys; sys.stdin.read(); print("{bad json")',
    'import sys; sys.stdin.read(); print(\'{"skipped":true}\')'])
def test_actual_ssh_empty_malformed_exit0_stays_pending(lane, tmp_path, body):
    p, manifest, _ = lane
    result = subprocess.run([sys.executable, str(bootstrap(tmp_path, body)), *bridge_args(p, manifest)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 75
    assert p.peek() is not None
    # Restart uses persisted uncertainty and must not launch even this fixture again.
    fake = tmp_path / 'fake_ssh.py'
    fake.write_text('raise RuntimeError("FIXTURE_SHOULD_NOT_RUN")')
    retry = subprocess.run([sys.executable, str(tmp_path / 'bootstrap.py'), *bridge_args(p, manifest)],
                           capture_output=True, text=True, timeout=15)
    assert retry.returncode == 75 and 'FIXTURE_SHOULD_NOT_RUN' not in retry.stderr


def test_actual_sender_timeout_is_bounded_safe_and_pending(lane, tmp_path):
    p, manifest, _ = lane
    body = 'import sys,time; sys.stdin.read(); print("PRIVATE_REMOTE_ERROR",file=sys.stderr,flush=True); time.sleep(3)'
    start = time.monotonic()
    result = subprocess.run([sys.executable, str(bootstrap(tmp_path, body, timeout=.1)), *bridge_args(p, manifest)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 75 and time.monotonic() - start < 5
    assert 'PRIVATE_REMOTE_ERROR' not in result.stdout + result.stderr
    assert p.peek()


def wait_for(predicate):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail('local fixture deadline')


def test_cli_exit_immediate_continues_review_and_concurrent_bridge_fenced(lane, tmp_path):
    p, manifest, artifacts = lane
    # Reset only the fixture clock to current time: periodic report is NOT due.
    state = p._load()
    state['last_queued_at'] = time.time()
    _atomic(p.path, state)
    boot = bootstrap(tmp_path, GOOD)
    args = [sys.executable, str(boot), *bridge_args(p, manifest), '--max-runtime', '15']
    child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        def child_owns_watch_lock():
            assert child.poll() is None, 'fixture bridge exited before acquiring its fence'
            try:
                with _lock(p.directory / 'watch.lock'):
                    return False
            except ValueError as exc:
                if str(exc) != 'run_locked':
                    raise
                return True

        # Baseline collection already created the lock file. Its existence
        # cannot establish that this child won the lifetime fence.
        wait_for(child_owns_watch_lock)
        duplicate = subprocess.run(args, capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 74
        (artifacts / 'status.json').write_text('{"status":"cli_completed","exit_code":0}')
        wait_for(lambda: p._load()['delivered'] is not None)
        assert child.poll() is None
        set_stage(manifest, 'final_verified')
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert len([v for v in stdout.splitlines() if json.loads(v)['status'] == 'verified']) == 2
        assert p.peek() is None
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=2)


def test_all_help_and_dry_run_surfaces_do_not_mutate(lane, tmp_path):
    p, manifest, _ = lane
    for script in [BRIDGE, SERVER, ROOT / 'scripts/run_codex_task.py', ROOT / 'scripts/delegation_progress.py']:
        result = subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and '--dry-run' in result.stdout
    before = {f: f.read_bytes() for f in tmp_path.rglob('*') if f.is_file()}
    result = subprocess.run([sys.executable, str(BRIDGE), *bridge_args(p, manifest), '--dry-run'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    content = '작업 진행 상황을 전해드려요.\n• runner.py: 파일 내용 변경.'
    value = dict(run_id='fixture', sequence=1, thread_id='123456789', content=content,
                 content_digest=hashlib.sha256(content.encode()).hexdigest())
    result = subprocess.run([sys.executable, str(SERVER), '--runtime-root', '/does/not/exist',
        '--allow-thread', '123456789', '--dry-run'], input=json.dumps(value), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'validated'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/delegation_progress.py'), 'set-stage',
        '--manifest', str(manifest), '--state-dir', str(p.root), '--stage', 'stopped', '--dry-run'],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert before == {f: f.read_bytes() for f in tmp_path.rglob('*') if f.is_file()}


def test_server_cli_dry_run_rejects_cross_thread_and_duplicate_keys(tmp_path):
    for value in ['{"run_id":"x","run_id":"y"}', '[]', 'x' * 17000]:
        result = subprocess.run([sys.executable, str(SERVER), '--runtime-root', '/does/not/exist',
            '--allow-thread', '123456789', '--dry-run'], input=value, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0 and 'Traceback' not in result.stderr
