"""Acceptance regressions (baseline evidence in .hermes/verification/progress-v2).

Quiet polling is silent until the approved 20-minute no-evidence warning.
"""
from dataclasses import replace
import subprocess

from agent.delegation_progress import Manifest, Progress, render


def lane(tmp_path):
    repo, artifacts = tmp_path / 'repo', tmp_path / 'artifacts'
    repo.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'a.py').write_text('value = 1\n')
    return Progress(Manifest('red', repo, tmp_path, artifacts, '123', '진행 보고'), tmp_path / 'state')


def test_compact_format():
    text = render({'task_label': '진행 보고', 'available': True, 'changes': [],
                   'tests': {'status': 'unknown'}, 'coordinator_stage': 'working'})
    lines = text.splitlines()
    assert 1 <= len(lines) <= 2 and lines[0].startswith('진행 보고: ')
    assert '남아 있어' in text and '변경:' not in text and '•' not in text
    assert len(text) <= 500


def test_no_change_periodic_silent(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    while p.peek():
        p.ack(p.peek()['id'])
    assert p.tick(now=1199)['queued'] == []
    assert [m['event'] for m in p.tick(now=1200)['queued'] if m['operation'] == 'NOTICE'] == ['stale']
    while p.peek():
        p.ack(p.peek()['id'])
    assert p.tick(now=1500)['queued'] == []


def test_bare_final_flag_is_not_verification(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    p.manifest = replace(p.manifest, coordinator_stage='final_verified')
    result = p.tick(now=10)
    assert not result['stopped']
    assert '최종 검증 완료' not in render(result['snapshot'])
