"""A committed managed execution precedes remote work and binds on import."""
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError
from gateway.task_completion_managed import _schema, _reconcile, remote_snapshot_reader


def _payload():
    return dict(envelope=dict(owner='human', origin='message', profile='default',
        request_text='implement', request_revision='1'), transport=dict(host='operator-mac',
        python='/usr/bin/python3', script='/operator/runner', profile_home='/operator/profile',
        executable_path='/usr/bin:/bin'), outcome_ids=['original_request'])


def test_restart_reuses_original_remote_run_and_binds_existing_execution(tmp_path, monkeypatch):
    from agent.task_lifecycle.transport import SSHExecutor
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    calls, bound = [], []
    payload = _payload()
    scope = SimpleNamespace(registry=registry, parent={'parent_id': 'parent'},
        identity=dict(session_key='session', profile='default', owner='human'),
        ledger=SimpleNamespace(bind_execution=lambda *a, **k: bound.append((a, k))))
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                          ('submission', 'parent', json.dumps(payload)))
    registry._conn.commit()
    def submit(self, envelope):
        calls.append(('submit', envelope))
        return {'run_id': 'remote-run'}
    def call(self, action, *args, **kwargs):
        calls.append((action, args))
        return {'phase': 'verified'}
    def receive(self, run_id, **kwargs):
        calls.append(('receive', run_id))
    monkeypatch.setattr(SSHExecutor, 'submit', submit)
    monkeypatch.setattr(SSHExecutor, 'call', call)
    monkeypatch.setattr(SSHExecutor, 'receive', receive)
    try:
        first = _reconcile(scope, 'submission', json.dumps(payload))
        second = _reconcile(scope, 'submission', json.dumps(payload))
        assert first == second == {'submission_id': 'submission', 'run_id': 'remote-run', 'phase': 'verified'}
        assert [v[0] for v in calls].count('submit') == 1
        assert len(bound) == 2
        assert bound[0][0] == ('submission',)
        assert bound[0][1]['run_id'] == 'remote-run'
    finally:
        registry.close()


def test_remote_current_snapshot_must_match_committed_host_and_contract(tmp_path, monkeypatch):
    from agent.task_lifecycle.transport import SSHExecutor
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    payload = _payload()
    # dataclasses.asdict production contract serialization is exercised by a small real dataclass.
    from dataclasses import make_dataclass
    cls = make_dataclass('ReadbackContract', [(k, str) for k in payload['envelope']])
    contract = cls(**payload['envelope'])
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                          ('s', 'parent', json.dumps(payload)))
    registry._conn.execute('INSERT INTO lifecycle_gateway_submission_receipts VALUES(?,?)', ('s', 'run'))
    registry._conn.commit()
    package = dict(run_id='run', host={'host': 'expected'}, contract=asdict(contract),
                   verification={'artifact': {'revision': 'current'}})
    reads = []
    def call(self, action, *args, **kwargs):
        reads.append((self.host, action, args))
        return package
    monkeypatch.setattr(SSHExecutor, 'call', call)
    try:
        reader = remote_snapshot_reader(registry)
        assert reader('run', contract, {'host': 'expected'}) == {'revision': 'current'}
        assert reads == [('operator-mac', 'export-result', ('run',))]
        package['host'] = {'host': 'replaced'}
        with pytest.raises(LifecycleError, match='identity mismatch'):
            reader('run', contract, {'host': 'expected'})
    finally:
        registry.close()


def test_later_human_followup_resumes_original_without_planning_replacement(tmp_path, monkeypatch):
    from gateway.task_completion_managed import managed_turn
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                          ('submission', 'parent', json.dumps(_payload())))
    registry._conn.commit()
    scope = SimpleNamespace(registry=registry, error=None, mode='enforce',
        parent={'parent_id': 'parent', 'origin': 'original-message', 'contract_digest': 'current'}, ledger=object(),
        request=SimpleNamespace(origin='later-status-message', text='status please'))
    def replacement(*args):
        raise AssertionError('A status/followup cannot replace original execution input')
    monkeypatch.setattr('gateway.task_completion_managed.plan_path', replacement)
    monkeypatch.setattr('gateway.task_completion_managed._reconcile',
        lambda actual, identity, payload, **kw: {'submission_id': identity, 'phase': 'verified'})
    try:
        assert managed_turn(scope) == [{'submission_id': 'submission', 'phase': 'verified'}]
    finally:
        registry.close()


def test_plan_binding_precedes_coverage_and_execution_audit_precedes_ssh(tmp_path, monkeypatch):
    from gateway.task_completion_managed import _prepare
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    calls = []
    identity = dict(session_key='session', profile='default', owner='human')
    parent = dict(**identity, parent_id='parent', origin='message', original_request='implement original', contract={'revision': 1, 'sources': [dict(origin='message', text='implement original')]}, contract_digest='d1')
    request = SimpleNamespace(origin='message', text='implement original')
    def bind_checks(*args, **kwargs):
        calls.append('checks')
        return parent
    def bind_plan(*args, **kwargs):
        calls.append(('plan', kwargs))
        return parent | {'contract_digest': 'bound-plan', 'contract': {'revision': 2,
            'plan': {'envelope': kwargs['envelope']}}}
    ledger = SimpleNamespace(bind_checks=bind_checks, bind_plan=bind_plan,
        get=lambda parent_id: parent | {'contract_digest': 'bound-plan', 'contract': {'revision': 2, 'plan': {'envelope': envelope}}},
        record_execution=lambda **kw: calls.append(('record', kw)))
    scope = SimpleNamespace(registry=registry, ledger=ledger, parent=parent, request=request,
        identity=identity, ctx=SimpleNamespace(source=object()))
    envelope = dict(owner='human', origin='message', profile='default')
    monkeypatch.setattr('agent.task_lifecycle.intake.gateway_envelope', lambda **kw: envelope)
    monkeypatch.setattr('agent.task_lifecycle.parent_verification._coverage',
        lambda *a: (_ for _ in ()).throw(LifecycleError('coverage not run')))
    monkeypatch.setattr('agent.task_lifecycle.parent_verification.run_coverage',
        lambda *a, **kw: calls.append('coverage'))
    plan = dict(version=1, binding=identity | {'origin': 'message', 'request_text': 'implement original'},
        outcomes=[], outcome_ids=['original_request'], policy=dict(lane='codex', workdir='/repo', timeout_seconds=60),
        transport=_payload()['transport'], request={'cli': 'codex'}, objective='implement', checks=[], artifacts=[],
        spec_sha256='a' * 64, spec_text='actual spec', check_files={'/operator/check': 'b' * 64},
        coverage=dict(argv=['/operator/reviewer'], reviewer='claude', worker_root='/repo'))
    try:
        submission = _prepare(scope, plan)
        assert calls[0] == 'checks'
        assert calls[1] == ('plan', dict(envelope=envelope, spec_text='actual spec',
            policy=plan['policy'], check_files=plan['check_files']))
        assert calls[2] == 'coverage'
        assert calls[3][0] == 'record'
        assert calls[3][1]['execution_id'] == submission
        assert registry._conn.execute('SELECT count(*) FROM lifecycle_gateway_submissions').fetchone()[0] == 1
    finally:
        registry.close()


def test_explicit_resume_retries_failed_verification_on_same_remote_run(tmp_path, monkeypatch):
    from agent.task_lifecycle.transport import SSHExecutor
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submission_receipts VALUES(?,?)', ('s', 'same-run'))
    registry._conn.commit()
    calls = []
    scope = SimpleNamespace(registry=registry, parent={'parent_id': 'p'},
        ledger=SimpleNamespace(bind_execution=lambda *a, **k: calls.append(('bind', k['run_id']))))
    status = {'phase': 'verifying'}
    def call(self, action, run_id):
        assert run_id == 'same-run'
        calls.append((action, run_id))
        if action == 'verify':
            status['phase'] = 'verified'
        return dict(status)
    monkeypatch.setattr(SSHExecutor, 'call', call)
    monkeypatch.setattr(SSHExecutor, 'submit', lambda *a: (_ for _ in ()).throw(AssertionError('must reuse run')))
    monkeypatch.setattr(SSHExecutor, 'receive', lambda self, run_id, **kw: calls.append(('receive', run_id)))
    try:
        result = _reconcile(scope, 's', json.dumps(_payload()))
        assert result['phase'] == 'verified'
        assert calls == [('status', 'same-run'), ('verify', 'same-run'), ('status', 'same-run'),
                         ('receive', 'same-run'), ('bind', 'same-run')]
    finally:
        registry.close()


def test_terminal_readback_uses_committed_transport_and_exact_remote_run(tmp_path, monkeypatch):
    from agent.task_lifecycle.transport import SSHExecutor
    from gateway.task_completion_remote import remote_status_reader
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                          ('s', 'p', json.dumps(_payload())))
    registry._conn.execute('INSERT INTO lifecycle_gateway_submission_receipts VALUES(?,?)', ('s', 'run'))
    registry._conn.commit()
    status = {'run_id': 'run', 'phase': 'execution_finished', 'worker_alive': False,
              'execution': {'status': 'cli_completed', 'process_returncode': 0}}
    calls = []
    def call(self, action, run_id):
        calls.append((self.host, action, run_id))
        return status
    monkeypatch.setattr(SSHExecutor, 'call', call)
    try:
        reader = remote_status_reader(registry)
        assert reader('run') == status
        assert calls == [('operator-mac', 'status', 'run')]
        status['run_id'] = 'wrong-run'
        with pytest.raises(LifecycleError, match='status identity mismatch'):
            reader('run')
        with pytest.raises(LifecycleError, match='committed gateway transport'):
            reader('unknown')
    finally:
        registry.close()


def test_unknown_verification_recovery_only_reads_existing_remote_status(tmp_path, monkeypatch):
    from agent.task_lifecycle.transport import SSHExecutor
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submission_receipts VALUES(?,?)', ('s', 'run'))
    registry._conn.commit()
    scope = SimpleNamespace(registry=registry, parent={'parent_id': 'p'})
    calls = []
    def call(self, action, run_id):
        calls.append(action)
        assert action == 'status'
        return {'phase': 'verifying'}
    monkeypatch.setattr(SSHExecutor, 'call', call)
    try:
        assert _reconcile(scope, 's', json.dumps(_payload()), allow_verification=False)['phase'] == 'verifying'
        assert calls == ['status']
    finally:
        registry.close()


@pytest.mark.parametrize('text,may_verify', [('status please', True), ('Why is this design useful?', False), (None, False)])
def test_human_retry_rearms_watcher_but_other_turns_do_not_retry_checks(tmp_path, monkeypatch, text, may_verify):
    from gateway.task_completion_managed import managed_turn
    from gateway.task_completion_recovery import _schema as recovery_schema, _job_state
    registry = Registry(tmp_path / 'state.db')
    _schema(registry)
    recovery_schema(registry)
    registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                          ('s', 'p', json.dumps(_payload())))
    registry._conn.commit()
    _job_state(registry, 's', 'needs_attention')
    scope = SimpleNamespace(registry=registry, error=None, mode='enforce',
        parent={'parent_id': 'p', 'origin': 'original', 'contract_digest': 'current'}, ledger=object(),
        request=SimpleNamespace(origin='status', text=text) if text else None)
    calls = []
    def reconcile(scope, identity, payload, *, allow_verification):
        calls.append(allow_verification)
        return {'submission_id': identity, 'phase': 'running'}
    monkeypatch.setattr('gateway.task_completion_managed._reconcile', reconcile)
    try:
        assert managed_turn(scope) == [{'submission_id': 's', 'phase': 'running'}]
        assert calls == [may_verify]
        state = registry._conn.execute('SELECT phase FROM lifecycle_gateway_recovery WHERE submission_id=?', ('s',)).fetchone()[0]
        assert state == ('running' if may_verify else 'needs_attention')
    finally:
        registry.close()
