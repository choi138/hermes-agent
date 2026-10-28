"""Native process receipts prove revised acceptance preserves historical effects."""
from dataclasses import asdict
import json
import sys

import pytest

from agent.task_lifecycle import workflow
from agent.task_lifecycle.intake import create_grant
from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.parent_plan import source_context
from agent.task_lifecycle.parent_supersession import _terminal
from agent.task_lifecycle.parent_verification import run_coverage
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.transport import SSHExecutor
from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.verification import verify
from gateway.task_completion_managed import _schema
from tests.agent.task_lifecycle.test_parent_workflow import bind_native_grant
from tests.agent.task_lifecycle.test_workflow import case as workflow_case, detached_popen, await_exit
from tests.waiting import wait_until

case = workflow_case


@pytest.mark.parametrize('change', [dict(worker_alive=True), dict(execution=None), dict(run_id='other'),
    dict(phase='running'), dict(execution=dict(process_returncode=None))])
def test_terminal_claim_requires_same_exited_writer_and_actual_result(change):
    status = dict(run_id='run', worker_alive=False, phase='execution_finished',
                  execution=dict(process_returncode=0))
    with pytest.raises(LifecycleError, match='terminal'):
        _terminal(dict(status, **change), 'run')


def coverage(ledger, parent, case):
    adapter = case['output'] / 'coverage.py'
    adapter.write_text('''import json,sys
p=json.load(sys.stdin)
print(json.dumps(dict(p['binding'], approved=True, missing=[],
 covered_outcomes=[item['id'] for item in p['contract']['outcomes']])))
''')
    return run_coverage(ledger, parent['parent_id'], argv=[sys.executable, str(adapter)],
        reviewer='claude', implementation_cli='codex', worker_root=str(case['repo']))


@pytest.mark.platforms('macos')
@pytest.mark.parametrize('late_terminal', [False, True])
def test_terminal_old_submission_can_be_replaced_only_by_full_revised_receipts(case, monkeypatch, late_terminal):
    registry = Registry()
    try:
        ledger = ParentLedger(registry)
        data, contract, _, _ = workflow.load_grant(case['grant'])
        identity = dict(session_key=data['destination']['session_key'], profile=contract.profile, owner=contract.owner)
        parent = ledger.begin(**identity, origin=contract.origin, original_request=contract.request_text,
            required_outcomes=[dict(id='original_request', description=contract.request_text,
                checks=[dict(kind='boundary', name='json-boundary')])])
        parent = bind_native_grant(ledger, parent, case['grant'])
        coverage(ledger, parent, case)
        ledger.record_execution(execution_id='original', **identity, kind='managed_lifecycle',
                                parent_id=parent['parent_id'])
        spawn = detached_popen(case, 'import sys; sys.stdin.read(); print("actual worker finished")')
        old_run = workflow.submit(case['grant'], popen=spawn)['run_id']
        await_exit(old_run)
        assert verify(old_run)['accepted']
        ledger.bind_execution('original', parent_id=parent['parent_id'], run_id=old_run,
                              outcome_ids=['original_request'])
        transport = SSHExecutor(host='local-native-boundary', python=sys.executable,
            script=str(case['repo'] / 'operator-script.py'), profile_home=str(case['home']), executable_path='/usr/bin:/bin')
        _schema(registry)
        payload = dict(envelope=parent['contract']['plan']['envelope'], transport=asdict(transport),
                       outcome_ids=['original_request'])
        with registry._conn:
            registry._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                                   ('original', parent['parent_id'], json.dumps(payload)))
            registry._conn.execute('INSERT INTO lifecycle_gateway_submission_receipts VALUES(?,?)',
                                   ('original', old_run))
        assert ledger.decision(parent['parent_id'])['accepted']
        parent = ledger.append_requirement(parent['parent_id'], owner=contract.owner,
            origin='message:followup', source_text='Also preserve the caller boundary after reconnect')
        outcomes = parent['contract']['outcomes']
        outcomes[-1]['checks'] = [dict(kind='boundary', name='json-boundary')]
        parent = ledger.bind_checks(parent['parent_id'], required_outcomes=outcomes)
        all_ids = [outcome['id'] for outcome in outcomes]
        with pytest.raises(LifecycleError, match='terminal observation is required'):
            ledger.supersede_execution('original', parent_id=parent['parent_id'],
                                       replacement_run_id='not-created', outcome_ids=all_ids)
        assert wait_until(lambda: not workflow.status(old_run)['worker_alive'], timeout=15)
        old_status = workflow.status(old_run)
        live = False
        def local_status(self, action, *arguments, **kwargs):
            assert action == 'status'
            actual = workflow.status(arguments[0])
            return dict(actual, worker_alive=True) if live else actual
        # SSH boundary is replaced by a local call to real durable process status;
        # this is native integration evidence, not a live SSH/model proof.
        monkeypatch.setattr(SSHExecutor, 'call', local_status)
        live = True
        with pytest.raises(LifecycleError, match='terminal'):
            ledger.record_terminal_submission(parent['parent_id'], submission_id='original', status=old_status)
        live = False
        terminal = None
        if not late_terminal:
            terminal = ledger.record_terminal_submission(parent['parent_id'], submission_id='original', status=old_status)
            assert ledger.record_terminal_submission(parent['parent_id'], submission_id='original', status=old_status) == terminal
        new_grant = create_grant(request=case['request'], request_text=contract.request_text,
            objective='Verify both original and reconnect requirement', owner=contract.owner,
            origin='message:followup', request_revision=str(parent['contract']['revision']), profile=contract.profile,
            checks=data['checks'], artifacts=data['artifacts'], destination=data['destination'],
            context=source_context(parent))
        parent = bind_native_grant(ledger, parent, new_grant)
        coverage(ledger, parent, case)
        replacement = workflow.submit(new_grant, popen=spawn)['run_id']
        ledger.record_execution(execution_id='replacement', **identity, kind='managed_lifecycle',
            parent_id=parent['parent_id'], run_id=replacement, outcome_ids=all_ids)
        await_exit(replacement)
        assert verify(replacement)['accepted']
        assert not ledger.decision(parent['parent_id'])['accepted']
        if late_terminal:
            ledger.record_terminal_submission(parent['parent_id'], submission_id='original', status=old_status)
            with pytest.raises(LifecycleError, match='predates'):
                ledger.supersede_execution('original', parent_id=parent['parent_id'],
                    replacement_run_id=replacement, outcome_ids=all_ids)
            assert not ledger.decision(parent['parent_id'])['accepted']
            return
        audit = ledger.supersede_execution('original', parent_id=parent['parent_id'],
                                          replacement_run_id=replacement, outcome_ids=all_ids)
        assert audit['not_before'] >= terminal['observed_at']
        assert ledger.decision(parent['parent_id'])['accepted']
        assert registry._conn.execute('SELECT run_id FROM lifecycle_gateway_submission_receipts '
            'WHERE submission_id=?', ('original',)).fetchone()[0] == old_run
        assert registry._conn.execute('SELECT run_id FROM lifecycle_parent_executions '
            'WHERE execution_id=?', ('original',)).fetchone()[0] is None
        independent = Registry()
        try:
            assert ParentLedger(independent).decision(parent['parent_id'])['accepted']
        finally:
            independent.close()
        live = True
        assert not ledger.decision(parent['parent_id'])['accepted']
        live = False
        (case['repo'] / 'answer.json').write_text('{"status":"regression"}')
        stale = ledger.decision(parent['parent_id'])
        assert not stale['accepted']
        assert any('artifacts changed' in value for value in stale['missing'])
    finally:
        registry.close()
