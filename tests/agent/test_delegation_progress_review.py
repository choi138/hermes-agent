"""Reviewer counterexamples through persisted observations and the actual CLI."""
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from agent.delegation_progress import Manifest, _atomic
from agent.delegation_progress_gateway import GatewayProgress
from tests.agent.test_delegation_progress import lane, event


JEST_PASS = 'Test Suites: 1 passed, 1 total\nTests: 1 passed, 1 total\n'
VITEST_PASS = 'Test Files 1 passed (1)\nTests 1 passed (1)\n'


def progress(lane, **extra):
    _, artifacts, data, state = lane
    manifest = Manifest.from_dict(dict(data, delivery_owner='gateway', **extra))
    path = artifacts / 'manifest.json'
    _atomic(path, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(manifest).items()})
    p = GatewayProgress(manifest, state)
    return p, path


def wire(p, path, now):
    snapshot = p.tick(now=now)['snapshot']
    runner = Path(__file__).resolve().parents[2] / 'scripts/run_codex_task.py'
    out = subprocess.run([sys.executable, str(runner), 'progress', '--manifest', str(path),
                          '--state-dir', str(p.root), '--thread', p.manifest.thread_id],
                         capture_output=True, text=True, check=True)
    message = json.loads(out.stdout)['message']
    if message is None:
        return snapshot, ''
    assert message['allowed_mentions'] == {'parse': [], 'replied_user': False}
    p.ack(message['id'])
    while p.peek():
        p.ack(p.peek()['id'])
    return snapshot, message['content']


@pytest.mark.parametrize('command', ['pnpm jest', 'npm jest', 'yarn jest', 'pnpm vitest run', 'yarn vitest run'])
def test_review_script_dispatch_cannot_authorize_pass(lane, command):
    repo, artifacts, _, _ = lane
    (repo / 'package.json').write_text(json.dumps({'scripts': {'jest': "printf 'Test Suites: 1 passed, 1 total\\nTests: 1 passed, 1 total\\n'", 'vitest': "printf 'Test Files 1 passed (1)\\nTests 1 passed (1)\\n'"}}))
    p, path = progress(lane)
    wire(p, path, 0)
    event(artifacts, 'item.started', command, item_id='script')
    event(artifacts, 'item.completed', command, item_id='script', output=VITEST_PASS if 'vitest' in command else JEST_PASS)
    snapshot, text = wire(p, path, 301)
    assert snapshot['tests']['status'] != 'passed'
    assert '테스트 실행 통과' not in text


@pytest.mark.parametrize('restored', [False, True])
def test_review_missing_requested_path_stays_unknown_across_polls_and_restart(lane, restored):
    repo, artifacts, _, state = lane
    p, path = progress(lane, code_scope=('missing.py',))
    first, _ = wire(p, path, 0)
    assert not first['files_available'] and first['code_fingerprint'] is None
    if restored:
        p = GatewayProgress(p.manifest, state)
    second, _ = wire(p, path, 301)
    assert not second['files_available'] and second['code_fingerprint'] is None
    event(artifacts, 'item.started', 'pnpm exec jest', item_id='missing-test')
    event(artifacts, 'item.completed', 'pnpm exec jest', item_id='missing-test', output=JEST_PASS)
    third, _ = wire(p, path, 602)
    assert third['tests']['status'] == 'passed'
    assert not third['tests']['applicable']
    (repo / 'missing.py').write_text('value = 1\n')
    fourth, _ = wire(p, path, 903)
    assert fourth['files_available'] and fourth['code_fingerprint'] is not None
    assert not fourth['tests']['applicable']


@pytest.mark.parametrize(('command', 'output'), [
    ('pnpm exec jest', JEST_PASS + 'Test suite failed to run\n'),
    ('pnpm exec jest', JEST_PASS + '  ● Test suite failed to run\n'),
    ('pnpm exec vitest run', VITEST_PASS + '⎯⎯⎯⎯ Unhandled Errors ⎯⎯⎯⎯\n'),
    ('pnpm exec vitest run', VITEST_PASS + '\x1b[31m⎯⎯ Unhandled Error ⎯⎯\x1b[0m\n'),
    ('pnpm exec jest', JEST_PASS + 'Snapshots: 1 passed, 2 total\n'),
    ('pnpm exec jest', JEST_PASS + 'Snapshots: 1 passed, 1 passed, 2 total\n'),
])
def test_review_error_and_contradictory_snapshot_cannot_pass_cli(lane, command, output):
    _, artifacts, _, _ = lane
    p, path = progress(lane)
    wire(p, path, 0)
    event(artifacts, 'item.started', command, item_id='error')
    event(artifacts, 'item.completed', command, item_id='error', output=output)
    snapshot, text = wire(p, path, 301)
    assert snapshot['tests']['status'] != 'passed'
    assert '테스트 실행 통과' not in text


@pytest.mark.parametrize('separator', ['=' * 64, '⎯' * 256, ('= ' * 1024)])
def test_review_error_header_scan_is_bounded(lane, separator):
    program = ('import json,sys; sys.path.insert(0,sys.argv[1]); '
               'from agent.delegation_progress import _js_summary; '
               'print(json.dumps(_js_summary(sys.stdin.read(),0,"jest")))')
    result = subprocess.run([sys.executable, '-c', program,
                             str(Path(__file__).resolve().parents[2])],
                            input=JEST_PASS + separator + '\n', capture_output=True,
                            text=True, check=True, timeout=3)
    assert json.loads(result.stdout)['status'] == 'passed'


@pytest.mark.parametrize(('command', 'output'), [
    ('pnpm exec jest', JEST_PASS + '=' * 64 + '\n'),
    ('pnpm exec jest', JEST_PASS + 'Snapshots: 1 passed, 1 total\n'),
    ('npm exec jest', JEST_PASS + 'Snapshots: 0 total\n'),
    ('npx jest', JEST_PASS),
    ('yarn exec jest', JEST_PASS),
    ('pnpm exec vitest run', VITEST_PASS),
])
def test_review_unambiguous_runner_pass_controls(lane, command, output):
    _, artifacts, _, _ = lane
    p, path = progress(lane)
    wire(p, path, 0)
    event(artifacts, 'item.started', command, item_id='valid')
    event(artifacts, 'item.completed', command, item_id='valid', output=output)
    snapshot, text = wire(p, path, 301)
    assert snapshot['tests']['status'] == 'passed'
    assert '테스트 실행 통과' in text
