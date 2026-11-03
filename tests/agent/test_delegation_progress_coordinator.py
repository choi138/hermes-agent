"""Coordinator-owned counterexamples; disposable Git fixtures only."""
from agent.delegation_progress import Manifest, Progress
from agent.delegation_progress_gateway import GatewayProgress
from tests.agent.test_delegation_progress import git, event

import pytest

pytest_plugins = ('tests.agent.test_delegation_progress',)


def test_r2_full_to_focused_never_invents_existing_deletion(lane):
    repo, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    for n in range(1030):
        (repo / f'source{n:04}.py').write_text('baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'grow inventory')
    snapshot = p.tick(now=301)['snapshot']
    assert not any(c['path'] == 'runner.py' and 'deleted' in c['kinds'] for c in snapshot['changes'])
    assert snapshot['code_fingerprint'] is None


@pytest.mark.parametrize(('command', 'output'), [
    ('pnpm exec jest', 'Test Suites: 1 passed, 2 total\nTests: 1 passed, 1 total'),
    ('pnpm exec jest', 'Test Suites: 1 passed, 1 failed, 1 total\nTests: 1 passed, 1 total'),
    ('pnpm exec vitest run', 'Test Files 1 passed (2)\nTests 1 passed (1)'),
    ('pnpm exec vitest run', 'Test Files 1 passed (1)\nTests 1 passed (1)\nErrors 1 error'),
])
def test_r2_contradictory_suite_summary_cannot_pass(lane, command, output):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    event(artifacts, 'item.started', command, item_id='contradiction')
    event(artifacts, 'item.completed', command, item_id='contradiction', output=output, code=0)
    assert p.tick(now=1)['snapshot']['tests']['status'] != 'passed'


def test_r2_legacy_partial_state_resumes_without_false_deletions(lane):
    repo, _, data, state = lane
    p = GatewayProgress(Manifest.from_dict(dict(data, delivery_owner='gateway')), state)
    p.tick(now=0)
    s = p._load()
    s.pop('reported_current_errors', None)
    s.pop('focused_paths', None)
    from agent.delegation_progress import _atomic
    _atomic(p.path, s)
    (repo / 'runner.py').write_text('changed\n')
    snap = p.tick(now=301)['snapshot']
    assert any(c['path'] == 'runner.py' and 'content' in c['kinds'] for c in snap['changes'])


@pytest.mark.parametrize('command', [
    "echo 'jest'; true", "printf 'Test Suites: 1 passed, 1 total'",
    "pnpm exec jest; tail -1 log", 'pnpm exec jest || true',
    'pnpm exec jest | tail -1', 'pnpm exec jest > result.log',
    "python3 -c 'print(\"jest\")'",
])
def test_d_arbitrary_output_and_composite_command_never_pass(lane, command):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    event(artifacts, 'item.started', command, item_id='unsafe')
    event(artifacts, 'item.completed', command, item_id='unsafe', output='Test Suites: 1 passed, 1 total\nTests: 1 passed, 1 total', code=0)
    assert p.tick(now=1)['snapshot']['tests']['status'] != 'passed'


@pytest.mark.parametrize(('command', 'output', 'expected'), [
    ('node /tmp/node_modules/.pnpm/vitest@4.0.18_@types+n/node_modules/vitest/vitest.mjs run --root /tmp/project --globals --no-cache', 'Test Files 1 passed (1)\nTests 1 passed | 1 skipped | 1 todo (3)', 'passed'),
    ('/tmp/node_modules/.bin/jest --config /tmp/jest.config.js --runInBand --no-cache', 'Test Suites: 1 passed, 1 total\nTests: 1 skipped, 1 todo, 1 passed, 3 total\nSnapshots: 0 total', 'passed'),
    ('pnpm exec jest', 'Test Suites: 1 passed, 1 total\nTests: 1 passed, 1 total\nTests: 1 passed, 1 total', 'unknown'),
    ('pnpm exec jest', 'Test Suites: 1 passed, 1 total\nTests: 1 passed, 1 total\nSnapshots: 1 failed, 1 total', 'failed'),
])
def test_followup_js_receipts(lane, command, output, expected):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    event(artifacts, 'item.started', command, item_id='receipt')
    event(artifacts, 'item.completed', command, item_id='receipt', output=output, code=0)
    assert p.tick(now=1)['snapshot']['tests']['status'] == expected


def test_followup_focused_real_deletion_and_restore(lane):
    repo, _, data, state = lane
    for n in range(1030):
        (repo / f'source{n:04}.py').write_text('baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'large baseline')
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    target = repo / 'runner.py'
    original = target.read_text()
    target.write_text('changed\n')
    assert any(c['path'] == 'runner.py' and 'content' in c['kinds'] for c in p.tick(now=1)['snapshot']['changes'])
    target.unlink()
    assert any(c['path'] == 'runner.py' and 'deleted' in c['kinds'] for c in p.tick(now=2)['snapshot']['changes'])
    target.write_text(original)
    assert any(c['path'] == 'runner.py' and 'content' in c['kinds'] for c in p.tick(now=3)['snapshot']['changes'])
    git(repo, 'add', '.')
    git(repo, 'commit', '--allow-empty', '-qm', 'restore')
    assert not any('deleted' in c['kinds'] for c in p.tick(now=4)['snapshot']['changes'])


def test_coordinator_bounded_churn_and_focused_to_full(lane):
    repo, _, data, state = lane
    for n in range(1030):
        (repo / f'source{n:04}.py').write_text('baseline\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'large baseline')
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    for n in range(1024):
        (repo / f'source{n:04}.py').write_text('first batch\n')
    first = p.tick(now=301)['snapshot']
    assert first['code_fingerprint'] is None
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'first batch')
    p.tick(now=602)
    for n in range(1024, 1030):
        (repo / f'source{n:04}.py').write_text('newest paths\n')
    newest = p.tick(now=903)['snapshot']
    paths = {c['path'] for c in newest['changes']}
    assert {f'source{n:04}.py' for n in range(1024, 1030)} <= paths
    assert newest['code_fingerprint'] is None
    s = p._load()
    for key in ('focused_paths', 'accumulated', 'cumulative'):
        assert len(s.get(key, [])) <= 1024
    assert len(s['previous']['files']) <= 1024
    assert 'files_truncated' in newest['current_errors']
    git(repo, 'rm', '-f', *[f'source{n:04}.py' for n in range(1030)])
    git(repo, 'commit', '-qm', 'shrink repository')
    full = p.tick(now=1204)['snapshot']
    assert full['files_available']
    assert full['code_fingerprint']
    assert not any(c['path'] == 'runner.py' for c in full['changes'])
    target = repo / 'runner.py'
    target.unlink()
    target.symlink_to(repo.parent / 'outside.py')
    partial = p.tick(now=1505)['snapshot']
    assert 'symlink_or_special' in partial['current_errors']
    assert partial['code_fingerprint'] is None
    target.unlink()
    target.write_text('value = 1\n')
    assert p.tick(now=1806)['snapshot']['code_fingerprint']


def test_coordinator_combined_malicious_cli_legacy_loss_recovery_terminal(lane):
    import json
    import subprocess
    import sys
    from pathlib import Path
    from dataclasses import asdict
    from agent.delegation_progress import _atomic
    repo, artifacts, data, state = lane
    manifest = Manifest.from_dict(dict(data, delivery_owner='gateway'))
    p = GatewayProgress(manifest, state)
    manifest_path = artifacts / 'manifest.json'
    _atomic(manifest_path, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(manifest).items()})
    p.tick(now=0)
    s = p._load()
    s.pop('reported_current_errors', None)
    _atomic(p.path, s)
    (repo / 'unsafe.py').symlink_to(repo.parent / 'outside.py')
    runner = Path(__file__).resolve().parents[2] / 'scripts/run_codex_task.py'

    def wire(now):
        snapshot = p.tick(now=now)['snapshot']
        result = subprocess.run([sys.executable, str(runner), 'progress', '--manifest', str(manifest_path),
                                 '--state-dir', str(state), '--thread', data['thread_id']],
                                capture_output=True, text=True, check=True)
        envelope = json.loads(result.stdout)
        msg = envelope['message']
        assert msg['allowed_mentions'] == {'parse': [], 'replied_user': False}
        p.ack(msg['id'])
        return snapshot, msg

    fake = 'Test Suites: 1 passed, 1 total\nTests: 1 passed, 1 total'
    event(artifacts, 'item.completed', "echo 'jest'", item_id='fake1', output=fake)
    snap, msg = wire(301)
    assert snap['tests']['status'] != 'passed'
    assert '심볼릭 링크' in msg['content']
    event(artifacts, 'item.completed', "printf 'jest'", item_id='fake2', output=fake)
    snap, msg = wire(602)
    assert snap['tests']['status'] != 'passed'
    assert '일부 관측 미수집' not in msg['content']
    events = artifacts / 'events.jsonl'
    saved = artifacts / 'saved-events.jsonl'
    events.rename(saved)
    events.symlink_to(saved)
    rejected = subprocess.run([sys.executable, str(runner), 'progress', '--manifest', str(manifest_path),
                               '--state-dir', str(state), '--thread', data['thread_id']],
                              capture_output=True, text=True, check=False)
    assert rejected.returncode == 74
    assert json.loads(rejected.stdout)['status'] == 'observation_unavailable'
    events.unlink()
    snap, msg = wire(903)
    assert 'events_unavailable' in snap['current_errors']
    assert '실행 이벤트 누락' in msg['content']
    saved.rename(events)
    (repo / 'unsafe.py').unlink()
    snap, msg = wire(1204)
    assert not snap['current_errors']
    assert '관측 오류가 해소' in msg['content']
    (artifacts / 'status.json').write_text(json.dumps({'status': 'cli_completed', 'exit_code': 0}))
    snap, msg = wire(1505)
    assert msg['event'] == 'cli_completed'
    assert '결과 검증 대기' in msg['content']
    assert not snap.get('validation', {}).get('applicable')
