"""V3 behavioral acceptance: fake clock, real temp files, no external messages."""
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from agent.delegation_progress import Progress, _atomic, render
from tests.agent.test_delegation_progress_v2_red import lane
from tests.agent.test_delegation_progress_v2 import drain, notices


def test_worker_exit_and_adjacent_verification_share_notice(tmp_path):
    p = lane(tmp_path)
    receipt = p.manifest.artifact_root / 'status.json'
    p.manifest = replace(p.manifest, receipt_path=receipt)
    p.tick(now=0)
    drain(p)
    receipt.write_text('{"status":"cli_completed","exit_code":0}')
    ended = p.tick(now=1)
    assert [m['event'] for m in notices(ended)] == ['cli_completed']
    assert '아직 시작 전' in render(ended['snapshot'])
    drain(p)
    p.manifest = replace(p.manifest, coordinator_stage='verifying')
    reviewing = p.tick(now=2)
    assert not notices(reviewing)
    assert reviewing['queued'][0]['operation'] == 'CARD_PATCH'
    assert '결과를 검증하고 있어' in reviewing['queued'][0]['content']
    drain(p)
    assert not notices(Progress(p.manifest, p.root).tick(now=3))


def test_nonadjacent_verification_is_immediate(tmp_path):
    p = lane(tmp_path)
    receipt = p.manifest.artifact_root / 'status.json'
    p.manifest = replace(p.manifest, receipt_path=receipt)
    p.tick(now=0)
    drain(p)
    receipt.write_text('{"status":"cli_completed","exit_code":0}')
    p.tick(now=1)
    drain(p)
    p.manifest = replace(p.manifest, coordinator_stage='verifying')
    assert [m['event'] for m in notices(p.tick(now=32))] == ['verifying']


def test_stale_boundary_loss_and_recovery_are_distinct(tmp_path):
    p = lane(tmp_path)
    p.tick(now=0)
    drain(p)
    assert not notices(p.tick(now=1199))
    assert [m['event'] for m in notices(p.tick(now=1200))] == ['stale']
    drain(p)
    p.manifest_error = True
    lost = p.tick(now=1201)
    assert [m['event'] for m in notices(lost)] == ['lost']
    assert '작업 실패인지는 아직 알 수 없어' in render(lost['snapshot'])
    drain(p)
    p.manifest_error = False
    assert not notices(p.tick(now=1202))
    drain(p)
    assert [m['event'] for m in notices(p.tick(now=2402))] == ['stale']


def test_claude_activity_is_safe_and_never_test_evidence(tmp_path):
    p = lane(tmp_path)
    events = p.manifest.artifact_root / 'events.jsonl'
    events.touch()
    p.manifest = replace(p.manifest, worker_cli='claude', event_path=events)
    p.tick(now=0)
    drain(p)
    event = {'type': 'assistant', 'message': {'id': 'msg-one', 'content': [
        {'type': 'tool_use', 'id': 'tool-one', 'name': 'Read', 'input': {'file_path': 'PRIVATE'}},
        {'type': 'text', 'text': 'PRIVATE all 99 tests passed'}]}}
    events.write_text(json.dumps(event) + '\n')
    result = p.tick(now=1199)
    assert result['snapshot']['execution']['phase'] == 'response_observed'
    assert result['snapshot']['tests']['status'] == 'unknown'
    assert not notices(result)
    assert 'Claude' in render(result['snapshot']) and 'Codex' not in render(result['snapshot'])
    assert 'PRIVATE' not in p.path.read_text()
    assert not notices(p.tick(now=1200))
    events.write_text(events.read_text() + json.dumps(event) + '\n')
    assert [m['event'] for m in notices(p.tick(now=2399))] == ['stale']


def test_light_poll_reuses_only_unchanged_working_observation(tmp_path, monkeypatch):
    import agent.delegation_progress as module
    p = lane(tmp_path)
    p.light_poll = True
    calls = []
    original = module.collect
    def counted(manifest):
        calls.append(True)
        return original(manifest)
    monkeypatch.setattr(module, 'collect', counted)
    with p.watcher():
        p.tick(now=0)
        drain(p)
        p.tick(now=10)
        p.tick(now=20)
        assert len(calls) == 1
        p.snapshot(now=21)
        assert len(calls) == 2  # Public snapshot is exact even inside a watcher.
        (p.manifest.worktree / 'a.py').write_text('value = 2\n')
        assert p.tick(now=30)['snapshot']['changes']
        assert len(calls) == 3
        (p.manifest.worktree / 'new.py').write_text('value = 3\n')
        assert p.tick(now=40)['snapshot']['changes']
        assert len(calls) == 4
        p.tick(now=100)
        assert len(calls) == 5
        p.manifest = replace(p.manifest, coordinator_stage='verifying')
        p.tick(now=101)
        assert len(calls) == 6
    p.snapshot(now=102)
    assert len(calls) == 7


def supervisor_config(p, tmp_path):
    manifest = tmp_path / 'manifest.json'
    _atomic(manifest, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(p.manifest).items()})
    config = p.directory / 'supervisor-config.json'
    _atomic(config, dict(binding=p.manifest.binding(), argv=['--manifest', str(manifest), '--state-dir', str(p.root)]))
    return config


def test_supervisor_renews_lifetime_and_bounds_durable_retry(tmp_path):
    from scripts.delegation_progress_supervisor import supervise
    p = lane(tmp_path)
    p.tick(now=0)
    config = supervisor_config(p, tmp_path)
    codes = iter([76, 76, 74, 75, 74, 74, 75])
    sleeps = []
    assert supervise(config, run_bridge=lambda _: next(codes), sleep=sleeps.append) == 0
    state = json.loads((p.directory / 'supervision.json').read_text())
    assert state['status'] == 'attention' and state['failures'] == 5 and state['renewals'] == 2
    assert sleeps == [10, 30, 60, 120]
    assert p.peek() is not None and not p._load()['stopped']
    assert supervise(config, run_bridge=lambda _: pytest.fail('budget reset')) == 0


def test_supervisor_crash_spends_persisted_budget_and_fences_duplicate(tmp_path):
    from scripts.delegation_progress_supervisor import supervise
    from agent.delegation_progress import _lock
    p = lane(tmp_path)
    p.tick(now=0)
    config = supervisor_config(p, tmp_path)
    class Crash(BaseException):
        pass
    def crash(_):
        with pytest.raises(ValueError, match='run_locked'):
            supervise(config)
        raise Crash()
    with pytest.raises(Crash):
        supervise(config, run_bridge=crash)
    assert json.loads((p.directory / 'supervision.json').read_text())['in_flight']
    calls = []
    supervise(config, run_bridge=lambda _: calls.append(True) or 74, sleep=lambda _: None)
    assert len(calls) == 4


def test_supervisor_success_requires_final_delivery_ack(tmp_path):
    from scripts.delegation_progress_supervisor import supervise
    p = lane(tmp_path)
    p.tick(now=0)
    config = supervisor_config(p, tmp_path)
    supervise(config, run_bridge=lambda _: 0, sleep=lambda _: None)
    assert json.loads((p.directory / 'supervision.json').read_text())['status'] == 'attention'
    assert p.peek() is not None


def test_supervisor_invalid_config_and_exhausted_launch_budget_stop(tmp_path):
    from scripts.delegation_progress_supervisor import owned_entry
    p = lane(tmp_path)
    p.tick(now=0)
    config = supervisor_config(p, tmp_path)
    config.write_text('{')
    assert owned_entry(config) == 0
    state = json.loads((p.directory / 'supervision.json').read_text())
    assert state['status'] == 'attention' and p.peek() is not None
    _atomic(p.directory / 'launch-budget.json', {'starts': 5})
    assert owned_entry(config) == 0
    state = json.loads((p.directory / 'supervision.json').read_text())
    assert state['reason'] == 'monitor_launch_budget_exhausted'
