"""Local integration fixtures; never use live Codex, SSH or Discord."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent.delegation_progress import Manifest, Progress, render

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lane(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'runner.py').write_text('value=1\n')
    (repo / 'task.md').write_text('PRIVATE_PROMPT_SENTINEL')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir(mode=0o700)
    return repo, artifacts, tmp_path / 'manifest.json', tmp_path / 'state'


def launch_args(lane):
    repo, artifacts, manifest, state = lane
    return [str(ROOT / 'scripts/run_codex_task.py'), '--spec', str(repo / 'task.md'),
            '--workdir', str(repo), '--allowed-root', str(repo), '--output-dir', str(artifacts),
            '--progress-manifest', str(manifest), '--progress-state-dir', str(state),
            '--progress-thread', '123456789', '--progress-label', '실행기 통합']


def test_actual_cli_registers_before_fast_worker(lane, tmp_path):
    import os
    repo, artifacts, manifest, state = lane
    binary = tmp_path / 'bin'
    binary.mkdir()
    fake = binary / 'codex'
    fake.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys
sys.stdin.read()
m = json.loads(pathlib.Path(''' + repr(str(manifest)) + ''').read_text())
s = json.loads((pathlib.Path(''' + repr(str(state)) + ''') / m['run_id'] / 'state.json').read_text())
assert s['previous']['files']['runner.py']['known']
assert s['cursor']['offset'] == 0
pathlib.Path('runner.py').write_text('value=2\\n')
pathlib.Path('tests').mkdir()
pathlib.Path('tests/test_pin.py').write_text('def test_pin(): pass\\n')
print(json.dumps({'type':'item.completed','item':{'id':'first','type':'command_execution','status':'completed','command':'python -m pytest tests/test_pin.py -q','exit_code':0,'aggregated_output':'1 passed in 0.01s'}}))
''')
    fake.chmod(0o700)
    result = subprocess.run([sys.executable, *launch_args(lane)],
                            env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH']),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    outputs = [json.loads(line) for line in result.stdout.splitlines()]
    assert outputs[0]['status'] == 'progress_registered'
    assert 'PRIVATE_PROMPT_SENTINEL' not in result.stdout
    p = Progress(Manifest.load(manifest), state)
    baseline_time = p._load()['last_queued_at']
    tick = p.tick(now=baseline_time + 1)
    assert tick['queued'][0]['event'] == 'cli_completed'
    assert tick['snapshot']['tests']['status'] == 'passed'
    assert {c['path'] for c in tick['snapshot']['changes']} == {'runner.py', 'tests/test_pin.py'}
    assert 'runner.py' in tick['queued'][0]['content']
    assert '테스트 파일 추가' in tick['queued'][0]['content']
    # Complete this same real runner's outbox through the actual bridge CLI,
    # replacing only its SSH child with the explicit local executable fixture.
    import runpy
    from agent.delegation_progress import set_stage
    fixture = runpy.run_path(str(ROOT / 'tests/scripts/test_progress_bridge_cli.py'))
    set_stage(manifest, 'final_verified')
    boot = fixture['bootstrap'](tmp_path, fixture['GOOD'])
    delivered = subprocess.run([sys.executable, str(boot), *fixture['bridge_args'](p, manifest)],
                               capture_output=True, text=True, timeout=15)
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert p.peek() is None
    assert json.loads(delivered.stdout.splitlines()[-1])['status'] == 'stopped_and_delivered'


def test_requested_registration_failure_does_not_spawn(lane):
    from agent.codex_task_runner import TaskRequest, run_task
    repo, artifacts, _, _ = lane
    called = []
    def broken(*_):
        raise ValueError('PRIVATE_REGISTRATION_ERROR')
    result = run_task(TaskRequest(repo / 'task.md', repo, repo, artifacts),
                      before_spawn=broken, popen=lambda *a, **k: called.append(True))
    assert not called
    assert result['status'] == 'registration_failed' and result['exit_code'] != 0
    assert 'PRIVATE' not in json.dumps(result)


def test_named_changes_and_safe_fallback():
    snapshot = dict(available=True, changes=[
        dict(path='agent/runner.py', **{'class':'source'}, kinds=['content']),
        dict(path='tests/test_pin.py', **{'class':'tests'}, kinds=['added']),
        *[dict(path=name, **{'class':'source'}, kinds=['added']) for name in
          ['@everyone.py', 'MEDIA:evil.py', 'https://evil.py', 'sk-' + 'a'*45 + '.py', 'x\n<@123>.py']]],
        tests={'status':'passed'}, coordinator_stage='working')
    text = render(snapshot)
    assert 'agent/runner.py' in text and r'tests/test\_pin.py' in text
    assert '테스트 파일 추가' in text and '파일 내용 변경' in text
    assert '안전한 이름 표시 불가' in text
    assert all(s not in text for s in ['@', 'MEDIA:', 'https:', 'sk-', '<@', 'a'*45])
    assert len(text) <= 1200 and '생략' in text


def test_canonical_test_command_completion():
    from agent.delegation_progress import _pytest_identity, _test_summary
    assert _pytest_identity('bash scripts/run_tests.sh -j 2 tests/agent/test_fixture.py')
    assert not _pytest_identity('echo bash scripts/run_tests.sh -j 2 tests/a.py')
    assert not _pytest_identity("bash -c 'echo 10 passed in 1.0s'")
    summary = _test_summary('10 passed, 2 skipped in 1.0s', 0)
    assert summary['passed'] == 10 and summary['skipped'] == 2


def test_bounded_baseline_registration_is_durable_and_explicit(lane, monkeypatch):
    import agent.delegation_progress as module
    from agent.codex_task_runner import TaskRequest
    repo, artifacts, manifest, state = lane
    run_dir = artifacts / 'unique-run'
    run_dir.mkdir(mode=0o700)
    (run_dir / 'events.jsonl').write_text('')
    original = module.collect
    monkeypatch.setattr(module, 'collect', lambda m: original(m, max_files=1))
    request = TaskRequest(repo / 'task.md', repo, repo, artifacts)
    registered = module.register_run(request, run_dir, str(manifest), str(state), '123456789', 'fixture')
    progress = Progress(registered, state)
    assert progress._load()['previous'] is not None
    assert 'files_truncated' in progress._load()['observation_errors']
    assert '상태 확인 불가' in render(progress.snapshot())


def test_canonical_summary_requires_exact_command_identity_and_complete_summary():
    from agent.delegation_progress import _event
    state = dict(seen_events=[], tests={'status':'unknown'})
    def observe(command, output, code=0):
        _event({'type':'item.completed', 'item':dict(id=str(len(state['seen_events'])),
            type='command_execution', status='completed', command=command,
            aggregated_output=output, exit_code=code)}, state, 1)
    complete = '=== Summary: 4 files, 72 tests passed, 0 failed, 2 skipped (100% complete) in 22.7s (2 workers) ===\n  Durations cached to test_durations.json (4 files)'
    observe('bash scripts/run_tests.sh -j 2 tests/agent/test_fixture.py', complete)
    assert state['tests']['scope'] == 'canonical_command' and state['tests']['skipped'] == 2
    observe('bash scripts/run_tests.sh -j 2 tests/agent/test_fixture.py', complete.replace('100%', '90%'))
    assert state['tests']['status'] == 'unknown'
    observe('python -m pytest scripts/run_tests.sh', complete)
    assert state['tests']['status'] == 'unknown'
    for command in ["echo 'bash scripts/run_tests.sh'", 'bash scripts/run_tests.sh -j 2 tests/a.py; echo ok']:
        observe(command, complete)
        assert state['tests']['status'] == 'unknown'


def test_progress_launch_dry_run_no_files(lane):
    repo, artifacts, manifest, state = lane
    before = {p: p.read_bytes() for p in repo.parent.rglob('*') if p.is_file()}
    result = subprocess.run([sys.executable, *launch_args(lane), '--dry-run'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'planned'
    assert before == {p: p.read_bytes() for p in repo.parent.rglob('*') if p.is_file()}
    assert not manifest.exists() and not state.exists() and list(artifacts.iterdir()) == []


def test_known_manifest_and_short_first_event_visible_while_cli_running(lane, tmp_path):
    import os
    import time
    repo, artifacts, manifest, state = lane
    binary = tmp_path / 'bin'
    binary.mkdir()
    release = tmp_path / 'release'
    fake = binary / 'codex'
    fake.write_text('#!' + sys.executable + '\n' + '''import json, pathlib, sys, time
sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'id':'first','type':'file_change','status':'completed'}}), flush=True)
deadline=time.monotonic()+8
while not pathlib.Path(''' + repr(str(release)) + ''').exists() and time.monotonic()<deadline:
    time.sleep(.02)
''')
    fake.chmod(0o700)
    child = subprocess.Popen([sys.executable, *launch_args(lane)],
        env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH']),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if manifest.exists():
                p = Progress(Manifest.load(manifest), state)
                if p.snapshot()['execution']['phase'] == 'file_change_completed':
                    break
            time.sleep(.03)
        else:
            pytest.fail('live short event was not flushed')
        assert child.poll() is None and not p.manifest.receipt_path.exists()
    finally:
        release.touch()
        stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert json.loads(stdout.splitlines()[0])['baseline_complete'] is True


def test_registration_refuses_existing_manifest_and_bad_git(lane):
    from agent.codex_task_runner import TaskRequest
    from agent.delegation_progress import register_run
    repo, artifacts, manifest, state = lane
    run_dir = artifacts / 'unique'
    run_dir.mkdir(mode=0o700)
    (run_dir / 'events.jsonl').write_text('')
    request = TaskRequest(repo / 'task.md', repo, repo, artifacts)
    manifest.write_text('DO_NOT_REPLACE')
    with pytest.raises(ValueError):
        register_run(request, run_dir, str(manifest), str(state), '123456789', 'fixture')
    assert manifest.read_text() == 'DO_NOT_REPLACE'
    manifest.unlink()
    (repo / '.git').rename(repo / '.git-disabled')
    with pytest.raises(ValueError, match='registration_baseline_unavailable'):
        register_run(request, run_dir, str(manifest), str(state), '123456789', 'fixture')
    assert not manifest.exists()
