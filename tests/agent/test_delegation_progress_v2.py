"""V2 timelines and evidence contracts; isolated git repositories and fake clock."""
from dataclasses import replace
import json

import pytest

from agent.delegation_progress import Progress, _atomic, render
from agent.delegation_progress_evidence import begin, record, migrate
from tests.agent.test_delegation_progress_v2_red import lane


def drain(p):
    messages = []
    while p.peek():
        messages.append(p.peek())
        p.ack(p.peek()['id'])
    return messages


def notices(result):
    return [m for m in result['queued'] if m['operation'] == 'NOTICE']


def test_timeline_cooldown_saves_retries_and_coalesced_card(tmp_path):
    p = lane(tmp_path)
    assert p.interval == 1200
    assert p.tick(now=0)['queued'][0]['operation'] == 'CARD_CREATE'
    drain(p)
    assert not p.tick(now=300)['queued']
    for now in range(310, 350):
        (p.manifest.worktree / 'a.py').write_text(f'value = {now}\n')
        assert not notices(p.tick(now=now))
        drain(p)
    p.manifest = replace(p.manifest, coordinator_stage='verifying')
    assert [m['event'] for m in notices(p.tick(now=400))] == ['verifying']
    drain(p)
    assert not notices(p.tick(now=1199))
    assert not notices(p.tick(now=1200))
    drain(p)
    assert not p.tick(now=1201)['queued']
    assert all(m['event'] == 'stale' for m in notices(p.tick(now=2400)))


def test_wait_episodes_stale_recovery_and_restart(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    drain(p)
    p.manifest = replace(p.manifest, cli_status='needs_user')
    first = notices(p.tick(now=1))
    assert len(first) == 1
    drain(p)
    assert not notices(p.tick(now=2))
    p.manifest = replace(p.manifest, cli_status='running')
    p.tick(now=3)
    drain(p)
    p = Progress(replace(p.manifest, cli_status='needs_user'), p.root)
    second = notices(p.tick(now=4))
    assert len(second) == 1 and first[0]['event_id'] != second[0]['event_id']
    drain(p)
    assert len(notices(p.tick(now=1804))) == 1
    drain(p)
    assert not notices(p.tick(now=3604))
    (p.manifest.worktree / 'a.py').write_text('value = 2\n')
    assert not notices(p.tick(now=3605))
    drain(p)
    assert len(notices(p.tick(now=5405))) == 1


@pytest.mark.parametrize('errors', [['files_truncated'], ['git_unavailable', 'git_truncated']])
def test_partial_inventory_is_not_monitor_loss_and_loss_rearms(tmp_path, monkeypatch, errors):
    import agent.delegation_progress as module
    p = lane(tmp_path)
    original = module.collect
    def partial(manifest):
        value = original(manifest)
        value.update(available=False, errors=errors)
        return value
    monkeypatch.setattr(module, 'collect', partial)
    result = p.tick(now=0)
    assert result['snapshot']['monitoring'] is None
    drain(p)
    p.manifest_error = True
    assert [m['event'] for m in notices(p.tick(now=1))] == ['lost']
    drain(p)
    assert not notices(p.tick(now=2))
    p.manifest_error = False
    p.tick(now=3)
    drain(p)
    p.manifest_error = True
    assert len(notices(p.tick(now=4))) == 1


def validation(p):
    ticket = begin(p)
    (p.manifest.artifact_root / 'gate.log').write_text('3 passed, 1 skipped in 0.12s\n')
    return record(p, ticket['ticket'], 'gate.log', 0, 'pytest')


def test_parent_pass_worker_unknown_final_card_then_notice_and_drain(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    drain(p)
    validation(p)
    result = p.tick(now=1)
    assert result['snapshot']['validation']['applicable']
    assert '레나 검증 3개 통과' in render(result['snapshot'])
    assert 'Codex 테스트 결과 미수집' not in render(result['snapshot'])
    assert [m['operation'] for m in result['queued']] == ['CARD_PATCH', 'NOTICE']
    assert not result['stopped']
    p = Progress(p.manifest, p.root)
    assert not p.tick(now=9999)['queued']
    drain(p)
    assert p.tick(now=10000)['stopped']
    assert not p.tick(now=20000)['queued']


@pytest.mark.parametrize('field', ['run_id', 'scope', 'code_fingerprint', 'evidence_digest'])
def test_wrong_receipts_do_not_verify(tmp_path, field):
    p = lane(tmp_path)
    p.tick(now=0)
    validation(p)
    state = p._load()
    state['validation'][field] = 'wrong'
    _atomic(p.path, state)
    assert not p.snapshot()['validation']['applicable']


def test_dirty_edit_invalidates_ticket_and_historical_validation(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    receipt = validation(p)
    (p.manifest.worktree / 'a.py').write_text('value = 2\n')
    snapshot = p.snapshot()
    assert not snapshot['validation']['applicable']
    assert snapshot['validation']['passed'] == 3
    with pytest.raises(ValueError, match='run_code_scope'):
        record(p, receipt['ticket'], 'gate.log', 0, 'pytest')


def test_worker_pass_bound_to_start_and_partial_files_keep_known_result(tmp_path, monkeypatch):
    import agent.delegation_progress as module
    p = lane(tmp_path)
    events = p.manifest.artifact_root / 'events.jsonl'
    events.touch()
    p.manifest = replace(p.manifest, event_path=events)
    p.tick(now=0)
    def event(kind, identity='test', command='python -m pytest -q'):
        with events.open('a') as stream:
            stream.write(json.dumps({'type': kind, 'item': {'type': 'command_execution', 'id': identity,
                'status': 'completed' if kind == 'item.completed' else 'in_progress',
                'command': command, 'exit_code': 0, 'aggregated_output': '2 passed in 0.1s'}}) + '\n')
    event('item.started')
    p.tick(now=1)
    event('item.completed')
    assert p.tick(now=2)['snapshot']['tests']['applicable']
    original = module.collect
    def partial(manifest):
        value = original(manifest)
        value.update(available=False, errors=['files_truncated'])
        return value
    monkeypatch.setattr(module, 'collect', partial)
    snapshot = p.tick(now=3)['snapshot']
    assert snapshot['tests']['status'] == 'passed' and not snapshot['tests']['applicable']
    monkeypatch.setattr(module, 'collect', original)
    (p.manifest.worktree / 'a.py').write_text('value = 3\n')
    assert not p.tick(now=4)['snapshot']['tests']['applicable']
    event('item.started', 'new', 'python -m pytest -k strange')
    p.tick(now=5)
    event('item.completed', 'new', 'python -m pytest -k strange')
    assert p.tick(now=6)['snapshot']['tests']['status'] == 'unknown'
    event('item.completed')
    assert p.tick(now=7)['snapshot']['tests']['status'] == 'unknown'


@pytest.mark.parametrize('label', ['@everyone', '<@123>', 'https://evil', 'x\nhello', '**wow**', 'x\u202e', '/private/path'])
def test_injection_never_leaves_renderer(tmp_path, label):
    p = lane(tmp_path)
    p.manifest = replace(p.manifest, task_label=label)
    text = render(p.tick(now=0)['snapshot'])
    assert text.startswith('작업: ') and label not in text
    assert 1 <= len(text.splitlines()) <= 2 and len(text) <= 500


def test_legacy_migration_archives_spam_preserves_stopped_and_uncertain(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    old = p._load()
    old['schema_version'] = 1
    _atomic(p.path, old)
    with pytest.raises(ValueError, match='explicit_migrate'):
        p.tick(now=1)
    _atomic(p.directory / 'delivery.json', {'records': {'1': {'status': 'uncertain'}}})
    with pytest.raises(ValueError, match='uncertain'):
        migrate(p)
    assert json.loads(p.path.read_text()) == old
    _atomic(p.directory / 'delivery.json', {'records': {}})
    old.update(stopped=True, final_snapshot={'coordinator_stage': 'final_verified'})
    _atomic(p.path, old)
    assert migrate(p)['stopped']
    assert not p.peek() and p.tick(now=9999)['stopped']


def test_explicit_scope_fingerprints_only_approved_code_and_new_absent_file(tmp_path):
    p = lane(tmp_path)
    p.manifest = replace(p.manifest, code_scope=['a.py', 'new.py'])
    p.tick(now=0)
    receipt = validation(p)
    (p.manifest.worktree / 'outside.py').write_text('outside scope\n')
    assert p.snapshot()['validation']['applicable']
    (p.manifest.worktree / 'new.py').write_text('inside scope\n')
    assert not p.snapshot()['validation']['applicable']
    with pytest.raises(ValueError, match='run_code_scope'):
        record(p, receipt['ticket'], 'gate.log', 0, 'pytest')
    with pytest.raises(ValueError, match='code_scope'):
        replace(p.manifest, code_scope=['credentials.py'])


def test_validation_dry_run_and_old_log_cannot_accept(tmp_path):
    p = lane(tmp_path)
    begin(p, dry_run=True)
    assert not p.root.exists()
    (p.manifest.artifact_root / 'old.log').write_text('3 passed in 0.1s\n')
    p.tick(now=0)
    ticket = begin(p)
    with pytest.raises(ValueError, match='predates_ticket'):
        record(p, ticket['ticket'], 'old.log', 0, 'pytest')


@pytest.mark.parametrize('interval', [0, -1, .001, float('inf'), float('nan'), True, 86401])
def test_cooldown_override_validated(tmp_path, interval):
    p = lane(tmp_path)
    with pytest.raises(ValueError, match='interval'):
        Progress(p.manifest, p.root, interval=interval)


def test_partial_inventory_does_not_suppress_or_merge_wait_episode(tmp_path, monkeypatch):
    import agent.delegation_progress as module
    p = lane(tmp_path)
    original = module.collect
    def partial(manifest):
        value = original(manifest)
        value.update(available=False, errors=['files_truncated'])
        return value
    monkeypatch.setattr(module, 'collect', partial)
    p.tick(now=0)
    drain(p)
    p.manifest = replace(p.manifest, cli_status='needs_user')
    first = notices(p.tick(now=1))
    assert [m['event'] for m in first] == ['needs_user']
    drain(p)
    assert not notices(p.tick(now=2))
    p.manifest = replace(p.manifest, cli_status='running')
    p.tick(now=3)
    drain(p)
    p.manifest = replace(p.manifest, cli_status='needs_user')
    second = notices(p.tick(now=4))
    assert len(second) == 1 and second[0]['event_id'] != first[0]['event_id']


def test_simultaneous_wait_and_monitor_loss_keep_separate_identity(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    drain(p)
    p.manifest = replace(p.manifest, cli_status='needs_user',
                         event_path=p.manifest.artifact_root / 'missing.jsonl')
    # event_path is immutable: establish this observer before its first tick.
    p = Progress(p.manifest, tmp_path / 'separate-state')
    first = notices(p.tick(now=1))
    assert {m['event'] for m in first} == {'needs_user', 'lost'}
    assert len({m['event_id'] for m in first}) == 2
    drain(p)
    assert not notices(p.tick(now=2))


def test_unstaging_without_content_edit_is_git_only(tmp_path):
    import subprocess
    p = lane(tmp_path)
    def git(*args):
        subprocess.run(['git', '-C', str(p.manifest.worktree), *args], check=True,
                       capture_output=True)
    git('add', 'a.py')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
    (p.manifest.worktree / 'a.py').write_text('staged = 2\n')
    git('add', 'a.py')
    (p.manifest.worktree / 'a.py').write_text('value = 1\n')
    p.tick(now=0)
    drain(p)
    git('reset', '-q', 'HEAD', '--', 'a.py')
    snapshot = p.tick(now=30)['snapshot']
    assert snapshot['changes']
    assert all(not set(c['kinds']) & {'content', 'reverted', 'added', 'deleted'}
               for c in snapshot['changes'])
    assert 'git 상태만 변경' in render(snapshot)


def test_expected_test_failures_and_retries_never_become_blocking_notices(tmp_path):
    p = lane(tmp_path)
    events = p.manifest.artifact_root / 'events.jsonl'
    events.touch()
    p.manifest = replace(p.manifest, event_path=events)
    p.tick(now=0)
    drain(p)
    for attempt, now in enumerate([300, 600, 1200, 1500]):
        (p.manifest.worktree / 'a.py').write_text(f'value = {attempt}\n')
        for kind, offset in [('item.started', 0), ('item.completed', 1)]:
            with events.open('a') as stream:
                stream.write(json.dumps({'type': kind, 'item': {
                    'type': 'command_execution', 'id': f'repair-{attempt}',
                    'command': 'python -m pytest -q',
                    'status': 'in_progress' if offset == 0 else 'completed',
                    'exit_code': 0 if attempt == 3 else 1,
                    'aggregated_output': '1 passed in 0.1s' if attempt == 3 else '1 failed in 0.1s'
                }}) + '\n')
            result = p.tick(now=now + offset)
            assert not notices(result) and not result['stopped']
            assert '실행 실패' not in render(result['snapshot'])
            drain(p)
    assert result['snapshot']['tests']['status'] == 'passed'
