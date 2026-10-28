"""Existing watcher resumes admitted work without a synthetic user request."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError
from gateway.task_completion_managed import _schema
from gateway.task_completion_recovery import reconcile_committed


def test_support_recovery_cannot_publish_existing_acceptance(tmp_path, monkeypatch):
    from gateway.task_completion_modes import remember_parent_mode
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = Registry()
    parent = ParentLedger(registry).begin(session_key='session', profile='default', owner='owner',
        origin='original', original_request='implement feature')
    remember_parent_mode(registry, parent['parent_id'], 'support')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
        ('submission', parent['parent_id'], '{}'))
    registry._conn.commit()
    registry.close()
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr(ParentLedger, 'decision', lambda self, identity: dict(accepted=True, missing=[]))
    results = reconcile_committed()
    assert results == []


@pytest.mark.parametrize('failure', [False, True])
def test_recovery_resumes_work_but_failed_checks_need_explicit_retry(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = Registry()
    parent = ParentLedger(registry).begin(session_key='session', profile='default', owner='owner',
        origin='original', original_request='implement feature')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
        ('submission', parent['parent_id'], json.dumps({'original': 'saved envelope'})))
    registry._conn.commit()
    registry.close()
    calls = []
    def reconcile(scope, identity, encoded, *, allow_verification=True):
        calls.append((identity, json.loads(encoded)))
        if failure:
            raise LifecycleError('boundary is not ready')
        return dict(phase='running' if len(calls) == 1 else 'verified')
    monkeypatch.setattr('gateway.task_completion_managed._reconcile', reconcile)
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: {'completion_gate': {'mode': 'enforce'}})
    for index in range(3):
        results = reconcile_committed()
        assert len(results) == (0 if failure and index else 1)
        if results:
            assert results[0][1]['accepted'] is False  # Polling does not mint acceptance receipts.
    assert len(calls) == (1 if failure else 2)
    assert calls[0] == ('submission', {'original': 'saved envelope'})
    registry = Registry()
    try:
        state = registry._conn.execute('SELECT phase FROM lifecycle_gateway_recovery').fetchone()[0]
        assert state == ('needs_attention' if failure else 'awaiting_publication')
    finally:
        registry.close()


@pytest.mark.parametrize('interrupted', [False, True])
def test_recovery_fairly_polls_running_jobs_and_resumes_crashed_status(tmp_path, monkeypatch, interrupted):
    from gateway.task_completion_recovery import _schema as recovery_schema
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', lambda: {'completion_gate': {'mode': 'enforce'}})
    registry = Registry()
    parent = ParentLedger(registry).begin(session_key='session', profile='default', owner='owner',
        origin='original', original_request='implement feature')
    _schema(registry)
    recovery_schema(registry)
    with registry._conn:
        for index in range(9):
            identity = str(index)
            registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                (identity, parent['parent_id'], '{}'))
            if interrupted:
                registry._conn.execute('INSERT INTO lifecycle_gateway_recovery VALUES(?,?,NULL,0)',
                    (identity, 'checking'))
    registry.close()
    calls = []
    def reconcile(scope, identity, encoded, *, allow_verification):
        calls.append((identity, allow_verification))
        return {'phase': 'running'}
    monkeypatch.setattr('gateway.task_completion_managed._reconcile', reconcile)
    reconcile_committed()
    assert len(calls) == 8
    reconcile_committed()
    assert any(identity == '8' for identity, _ in calls)
    assert calls[:8] == [(str(index), not interrupted) for index in range(8)]


@pytest.mark.asyncio
async def test_existing_profile_watcher_schedules_one_supervised_recovery(monkeypatch, tmp_path):
    from gateway.run_profile_reconcile import GatewayProfileReconcileMixin
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    from gateway.task_completion_recovery import schedule_recovery
    monkeypatch.setattr('hermes_constants.get_process_hermes_home', lambda: tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def recover(runner, home):
        assert home == tmp_path
        entered.set()
        await release.wait()
    monkeypatch.setattr('gateway.task_completion_recovery.recover_profile', recover)
    runner = SimpleNamespace(_running=True, _served_profile_homes={}, _background_tasks=set(),
        _retain_background_task=None, _multiplex_on=lambda: False,
        _multiplex_for_added_profile=AsyncMock(return_value=False))
    runner._retain_background_task = lambda task: GatewayAdapterLifecycleMixin._retain_background_task(runner, task)
    async def sync():
        runner._running = False
    runner._sync_hooks_on_consent_change = sync
    await GatewayProfileReconcileMixin._profile_reconcile_watcher(runner, interval=0)
    await asyncio.wait_for(entered.wait(), timeout=5)
    schedule_recovery(runner)
    assert len(runner._background_tasks) == 1
    release.set()
    await asyncio.gather(*runner._background_tasks)
