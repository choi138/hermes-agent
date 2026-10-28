"""Native lifecycle subprocess/check receipts, aggregated into a parent objective.

The harmless Python worker and coverage adapter exercise receipt plumbing; this
is not evidence of a live LLM reviewer or a deployed gateway integration.
"""
from pathlib import Path
from dataclasses import replace
import json
import subprocess
import sys

import pytest

from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle.intake import create_grant
from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.parent_verification import run_coverage
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle import workflow
from agent.task_lifecycle.verification import verify
from agent.task_lifecycle.types import LifecycleError
from tests.agent.task_lifecycle.test_workflow import case as workflow_case
from tests.waiting import wait_until

case = workflow_case


def bind_native_grant(ledger, parent, grant):
    """Bind trusted fixture grant before coverage, without running a worker."""
    data, contract, _, _ = workflow.load_grant(grant)
    envelope = dict(version=1, **{name: getattr(contract, name) for name in (
        'owner', 'origin', 'request_revision', 'profile', 'request_text', 'objective', 'approval_ref')},
        **{name: data[name] for name in ('request', 'checks', 'artifacts', 'spec_sha256', 'context', 'destination')},
        forbidden_actions=list(contract.forbidden_actions))
    request = data['request']
    return ledger.bind_plan(parent['parent_id'], envelope=envelope,
        spec_text=Path(request['spec']).read_text(),
        policy=dict(lane=request['cli'], workdir=request['workdir'], timeout_seconds=request['timeout'],
                    permissions='read_only' if request['sandbox'] == 'read-only' else 'workspace_write',
                    approval='required' if contract.approval_ref else 'not_required'),
        check_files=data['check_files'])


@pytest.mark.platforms('macos')
@pytest.mark.parametrize('changed_field,error', [('origin', 'origin'), ('workdir', 'repo/workdir'),
    ('execution_digest', 'execution digest'), ('destination', 'session')])
def test_same_text_child_from_different_source_plan_or_session_is_refused(case, changed_field, error):
    data, contract, _, _ = workflow.load_grant(case['grant'])
    registry = Registry()
    try:
        ledger = ParentLedger(registry)
        parent = ledger.begin(session_key=data['destination']['session_key'], profile=contract.profile,
            owner=contract.owner, origin=contract.origin, original_request=contract.request_text,
            required_outcomes=[dict(id='runtime', description=contract.request_text,
                checks=[dict(kind='boundary', name='json-boundary')])])
        parent = bind_native_grant(ledger, parent, case['grant'])
        ledger.record_execution(execution_id='raw', session_key=parent['session_key'],
            profile=contract.profile, owner=contract.owner, kind='codex', parent_id=parent['parent_id'])
        alternate = case['repo'] / 'different-workdir'
        alternate.mkdir()
        changes = dict(origin='different-message-same-text', workdir=str(alternate),
                       execution_digest='0' * 64)
        child = registry.submit(replace(contract, **{changed_field: changes[changed_field]})
            if changed_field != 'destination' else contract)
        payload = {key: data[key] for key in ('request', 'prompt', 'checks', 'artifacts',
            'context', 'destination', 'spec_sha256', 'check_files')}
        if changed_field == 'destination':
            payload['destination'] = dict(payload['destination'], session_key='different-session')
        registry.prepare_job(child.run_id, payload)
        with pytest.raises(LifecycleError, match=error):
            ledger.bind_execution('raw', parent_id=parent['parent_id'],
                                  run_id=child.run_id, outcome_ids=['runtime'])
        assert not ledger.decision(parent['parent_id'])['accepted']
    finally:
        registry.close()


@pytest.mark.platforms('macos')
@pytest.mark.parametrize('race', [None, 'execution', 'link', 'pending', 'admission'])
def test_actual_child_receipts_complete_only_current_parent_artifact(tmp_path, monkeypatch, race):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    output = tmp_path / 'receipts'
    output.mkdir()
    original = 'Keep the required runtime boundary and check it before completion'
    spec = repo / 'SPEC.md'
    spec.write_text(original)
    (repo / 'answer.txt').write_text('correct')
    check = dict(kind='boundary', name='actual-output', argv=[sys.executable, '-c',
        'from pathlib import Path; assert Path("answer.txt").read_text() == "correct"'])
    registry = Registry()
    try:
        ledger = ParentLedger(registry)
        parent = ledger.begin(session_key='session', profile='canary', owner='human',
            origin='message:1', original_request=original, required_outcomes=[dict(
                id='runtime', description=original, checks=[dict(kind=check['kind'], name=check['name'])])])
        adapter = tmp_path / 'coverage.py'
        adapter.write_text('''import json,sys
p=json.load(sys.stdin)
print(json.dumps(dict(p['binding'], approved=True, missing=[], covered_outcomes=['runtime'])))
''')
        request = TaskRequest(spec, repo, repo, output, timeout=15)
        destination = dict(session_key='session', platform='discord', channel_id='channel',
                           thread_id='thread', adapter_profile='canary')
        grant = create_grant(request=request, request_text=original, objective=original,
            owner='human', origin='message:1', request_revision='1', profile='canary',
            checks=[check], artifacts=['answer.txt'], destination=destination)
        parent = bind_native_grant(ledger, parent, grant)
        run_coverage(ledger, parent['parent_id'], argv=[sys.executable, str(adapter)],
            reviewer='claude', implementation_cli='codex', worker_root=str(repo))
        bootstrap = output / 'harmless_worker.py'
        source = Path(__file__).resolve().parents[3]
        bootstrap.write_text('''import sys,subprocess
sys.path.insert(0,sys.argv[1])
from agent.task_lifecycle.workflow import worker
def popen(argv,**kwargs):
    marker=argv.index('--')
    return subprocess.Popen([*argv[:marker+1],sys.executable,'-c',
        'import sys; sys.stdin.read(); print("worker finished")'],**kwargs)
raise SystemExit(worker(sys.argv[2],popen=popen))
''')
        def spawn(argv, **kwargs):
            return subprocess.Popen([sys.executable, str(bootstrap), str(source), argv[-1]], **kwargs)
        run_id = workflow.submit(grant, popen=spawn)['run_id']
        ledger.record_execution(execution_id='managed-child', session_key='session',
            profile='canary', owner='human', kind='codex', parent_id=parent['parent_id'],
            run_id=None)
        ledger.bind_execution('managed-child', parent_id=parent['parent_id'],
                              run_id=run_id, outcome_ids=['runtime'])
        if race == 'link':
            ledger.record_execution(execution_id='direct-child', session_key='session',
                profile='canary', owner='human', kind='codex', parent_id=parent['parent_id'],
                run_id=run_id, outcome_ids=['runtime'])
        assert wait_until(lambda: bool(workflow.status(run_id)['execution']), timeout=15)
        assert not ledger.decision(parent['parent_id'])['accepted']
        assert verify(run_id)['accepted']
        completed = ledger.decision(parent['parent_id'])
        assert completed['accepted'], completed
        assert ledger.authoritative(parent['parent_id'], completed['decision_id'])
        independent = Registry()
        try:
            assert ParentLedger(independent).decision(parent['parent_id'])['decision_id'] == completed['decision_id']
        finally:
            independent.close()
        if race == 'admission':
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier
            rendezvous = Barrier(2)
            def admit(origin):
                registry = Registry()
                try:
                    independent = ParentLedger(registry)
                    rendezvous.wait(timeout=10)
                    return independent.intake_requirement(session_key='session', profile='canary',
                        owner='human', origin=origin, source_text='Next requirement ' + origin,
                        previous_accepted=True)
                finally:
                    registry.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(admit, origin) for origin in ('message:2', 'message:3')]
                admitted = [future.result(timeout=15) for future in futures]
            assert admitted[0]['parent_id'] == admitted[1]['parent_id']
            assert admitted[0]['parent_id'] != parent['parent_id']
            current = ledger.current(session_key='session', profile='canary', owner='human')
            assert {source['origin'] for source in current['contract']['sources']} == {'message:2', 'message:3'}
            return
        if race:
            from agent.task_lifecycle import parent_verification
            from gateway.task_completion_managed import _schema
            _schema(registry)
            other = Registry()
            other_ledger = ParentLedger(other)
            real_receipt = parent_verification._child_receipt
            injected = False
            def read_then_commit(*args):
                nonlocal injected
                result = real_receipt(*args)
                if not injected:
                    injected = True
                    if race == 'execution':
                        other_ledger.record_execution(execution_id='racing-tool',
                            session_key='session', profile='canary', owner='human', kind='terminal',
                            parent_id=parent['parent_id'])
                    elif race == 'link':
                        other_ledger.bind_execution('direct-child', parent_id=parent['parent_id'],
                                                   run_id=run_id, outcome_ids=['runtime'])
                    else:
                        with other._conn:
                            other._conn.execute('INSERT INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                                ('racing-submission', parent['parent_id'],
                                 json.dumps(dict(outcome_ids=['runtime']))))
                return result
            monkeypatch.setattr(parent_verification, '_child_receipt', read_then_commit)
            try:
                raced = ledger.decision(parent['parent_id'])
                assert injected
                assert not raced['accepted'], raced
                assert 'Completion inputs changed during aggregation' in raced['missing']
                current = ledger.decision(parent['parent_id'])
                if race == 'link':
                    assert current['accepted'], current
                else:
                    assert not current['accepted']
                    assert any(('unverified execution:racing-tool' if race == 'execution'
                                else 'pending managed submission:racing-submission') == value
                               for value in current['missing'])
            finally:
                other.close()
            return
        # A new raw execution invalidates a prior success even when its child
        # is still VERIFIED. The authoritative query never trusts cached PASS.
        ledger.record_execution(execution_id='late-raw', session_key='session',
            profile='canary', owner='human', kind='terminal', parent_id=parent['parent_id'])
        assert not ledger.authoritative(parent['parent_id'], completed['decision_id'])
        ledger.bind_execution('late-raw', parent_id=parent['parent_id'],
                              run_id=run_id, outcome_ids=['runtime'])
        changed = ledger.decision(parent['parent_id'])
        assert not changed['accepted']
        assert any('predate' in value for value in ledger.decision(parent['parent_id'])['missing'])
        (repo / 'answer.txt').write_text('regression after pass')
        assert not ledger.authoritative(parent['parent_id'], completed['decision_id'])
        changed = ledger.decision(parent['parent_id'])
        assert not changed['accepted']
        assert any('Child artifacts changed' in value for value in changed['missing'])
    finally:
        registry.close()
