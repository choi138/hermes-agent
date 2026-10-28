"""Parent completion cannot be inferred from a model/process verdict."""
from pathlib import Path
from hashlib import sha256
import sqlite3
import sys

import pytest

from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.parent_verification import run_coverage
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError


@pytest.fixture
def ledger(tmp_path):
    registry = Registry(tmp_path / 'state.db')
    yield ParentLedger(registry)
    registry.close()


def begin(ledger, **kw):
    return ledger.begin(session_key='discord:thread', profile='canary', owner='human',
        origin='message:1', original_request='Implement behavior and verify the actual boundary', **kw)


def reviewer(tmp_path, body=None):
    adapter = tmp_path / 'coverage_adapter.py'
    adapter.write_text(body or '''import json,sys
p=json.load(sys.stdin)
print(json.dumps(dict(p['binding'], approved=True, missing=[],
    covered_outcomes=[v['id'] for v in p['contract']['outcomes']])))
''')
    root = tmp_path / 'worker'
    root.mkdir(exist_ok=True)
    return dict(argv=[sys.executable, str(adapter)], reviewer='claude',
                implementation_cli='codex', worker_root=str(root))


def bind_test_plan(ledger, parent, tmp_path, *, checks=None):
    from agent.codex_task_runner import TaskRequest
    from agent.task_lifecycle.workflow import request_data
    from agent.task_lifecycle.verification import check_files
    root = tmp_path / 'worker'
    root.mkdir(exist_ok=True)
    spec = root / 'SPEC.md'
    spec.write_text(parent['original_request'])
    output = tmp_path / 'receipts'
    output.mkdir(exist_ok=True)
    request = request_data(TaskRequest(spec, root, root, output, timeout=15))
    checks = checks or [dict(kind='test', name='smoke', argv=[sys.executable, '-c', 'pass'])]
    envelope = dict(version=1, owner=parent['owner'], origin=parent['origin'],
        request_revision=str(parent['contract']['revision']), profile=parent['profile'],
        request_text=parent['original_request'], objective=parent['original_request'], request=request,
        checks=checks, artifacts=['SPEC.md'], spec_sha256=sha256(spec.read_bytes()).hexdigest(),
        forbidden_actions=[], context=None, approval_ref=None,
        destination=dict(session_key=parent['session_key'], platform='discord',
                         channel_id='channel', thread_id='thread', adapter_profile=parent['profile']))
    policy = dict(lane='codex', workdir=str(root), timeout_seconds=15,
                  permissions='read_only', approval='not_required')
    return ledger.bind_plan(parent['parent_id'], envelope=envelope, spec_text=spec.read_text(),
                            policy=policy, check_files=check_files(checks, root))


def test_restart_original_request_and_duplicate_are_durable(ledger):
    parent = begin(ledger)
    assert begin(ledger)['parent_id'] == parent['parent_id']
    registry = Registry(ledger.registry.db_path)
    try:
        resumed = ParentLedger(registry).current(session_key='discord:thread', profile='canary', owner='human')
        assert resumed == parent
        assert not ParentLedger(registry).decision(parent['parent_id'])['accepted']
    finally:
        registry.close()
    with pytest.raises(LifecycleError, match='conflict'):
        ledger.begin(session_key='discord:thread', profile='canary', owner='attacker',
                     origin='message:1', original_request=parent['original_request'])


def test_legacy_execution_and_unattributed_execution_block_parent(ledger):
    parent = begin(ledger)
    for identity, parent_id in [('raw-launch', parent['parent_id']), ('late-tool', None)]:
        ledger.record_execution(execution_id=identity, session_key='discord:thread',
            profile='canary', owner='human', kind='terminal', parent_id=parent_id)
    verdict = ledger.decision(parent['parent_id'])
    assert not verdict['accepted']
    assert {'unverified execution:raw-launch', 'unverified execution:late-tool'} <= set(verdict['missing'])
    with pytest.raises(LifecycleError, match='immutable'):
        ledger.record_execution(execution_id='late-tool', session_key='discord:thread',
            profile='canary', owner='human', kind='terminal', parent_id=parent['parent_id'])


def test_user_scope_revision_keeps_original_and_invalidates_coverage(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    args = reviewer(tmp_path)
    run_coverage(ledger, parent['parent_id'], **args)
    assert ledger.decision(parent['parent_id'])['coverage_digest']
    with pytest.raises(LifecycleError, match='authenticated'):
        ledger.revise(parent['parent_id'], owner='model', origin='message:2',
                      source_text='No E2E necessary', required_outcomes=parent['contract']['outcomes'])
    revised = ledger.revise(parent['parent_id'], owner='human', origin='message:2',
        source_text='Keep the original verification boundary', required_outcomes=parent['contract']['outcomes'])
    assert revised['original_request'] == parent['original_request']
    assert revised['contract']['sources'][0]['text'] == parent['original_request']
    assert revised['contract_digest'] != parent['contract_digest']
    assert ledger.decision(parent['parent_id'])['coverage_digest'] is None


def test_coverage_must_execute_independent_adapter_and_bind_source(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    args = reviewer(tmp_path, 'print(\'{"approved":true,"missing":[],"covered_outcomes":["original_request"]}\')')
    with pytest.raises(LifecycleError, match='current original request'):
        run_coverage(ledger, parent['parent_id'], **args)
    args['implementation_cli'] = 'claude'
    with pytest.raises(LifecycleError, match='independent'):
        run_coverage(ledger, parent['parent_id'], **args)


def test_coverage_cannot_approve_outcome_names_without_execution_plan(ledger, tmp_path):
    parent = begin(ledger)
    with pytest.raises(LifecycleError, match='plan must be bound'):
        run_coverage(ledger, parent['parent_id'], **reviewer(tmp_path))
    assert ledger.decision(parent['parent_id'])['coverage_digest'] is None


def test_adapter_changes_invalidate_coverage_and_decisions_are_recomputed(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    args = reviewer(tmp_path)
    run_coverage(ledger, parent['parent_id'], **args)
    verdict = ledger.decision(parent['parent_id'])
    assert verdict['coverage_digest']
    assert not ledger.authoritative(parent['parent_id'], verdict['decision_id'])
    Path(args['argv'][1]).write_text('print("tampered")')
    changed = ledger.decision(parent['parent_id'])
    assert changed['coverage_digest'] is None
    assert changed['decision_id'] != verdict['decision_id']


def test_parent_contracts_are_append_only_in_existing_registry_database(ledger):
    parent = begin(ledger)
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        ledger.registry._conn.execute('UPDATE lifecycle_parents SET original_request=? WHERE parent_id=?',
                                     ('all checks passed', parent['parent_id']))
    assert Path(ledger.registry.db_path).name == 'state.db'
    assert not list(Path(ledger.registry.db_path).parent.glob('*parent*.db'))


def test_trusted_check_binding_can_strengthen_but_not_weaken_original_outcome(ledger):
    parent = begin(ledger)
    outcomes = parent['contract']['outcomes']
    outcomes[0]['checks'] = [dict(kind='boundary', name='runtime')]
    strengthened = ledger.bind_checks(parent['parent_id'], required_outcomes=outcomes)
    assert strengthened['original_request'] == parent['original_request']
    outcomes[0]['checks'] = []
    with pytest.raises(LifecycleError, match='weaken'):
        ledger.bind_checks(parent['parent_id'], required_outcomes=outcomes)
    assert ledger.get(parent['parent_id']) == strengthened


def test_plan_binds_spec_commands_policy_and_invalidates_same_named_coverage(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    args = reviewer(tmp_path)
    receipt = run_coverage(ledger, parent['parent_id'], **args)
    assert receipt['binding']['plan_digest'] == parent['contract']['plan_digest']
    changed = bind_test_plan(ledger, parent, tmp_path, checks=[dict(kind='test', name='smoke',
        argv=[sys.executable, '-c', 'print("different implementation of same check name")'])])
    assert changed['contract_digest'] != parent['contract_digest']
    assert ledger.decision(parent['parent_id'])['coverage_digest'] is None
    plan = changed['contract']['plan']
    with pytest.raises(LifecycleError, match='SPEC bytes'):
        ledger.bind_plan(parent['parent_id'], envelope=plan['envelope'], spec_text='omitted boundary',
                         policy=plan['policy'], check_files=plan['check_files'])
    assert ledger.get(parent['parent_id']) == changed


def test_plan_replay_preserves_launch_revision_and_requires_exact_source_session(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    replayed = bind_test_plan(ledger, parent, tmp_path)
    assert replayed == parent
    plan = parent['contract']['plan']
    envelope = dict(plan['envelope'], origin='other-message-same-text')
    with pytest.raises(LifecycleError, match='source/session'):
        ledger.bind_plan(parent['parent_id'], envelope=envelope, spec_text=plan['spec_text'],
                         policy=plan['policy'], check_files=plan['check_files'])
    envelope = dict(plan['envelope'], destination=dict(plan['envelope']['destination'], session_key='other-session'))
    with pytest.raises(LifecycleError, match='source/session'):
        ledger.bind_plan(parent['parent_id'], envelope=envelope, spec_text=plan['spec_text'],
                         policy=plan['policy'], check_files=plan['check_files'])


def test_additive_requirement_preserves_sources_checks_and_invalidates_plan(ledger, tmp_path):
    parent = bind_test_plan(ledger, begin(ledger), tmp_path)
    run_coverage(ledger, parent['parent_id'], **reviewer(tmp_path))
    added = ledger.append_requirement(parent['parent_id'], owner='human', origin='message:2',
                                       source_text='Also verify reconnect recovery')
    assert added['contract']['outcomes'][:-1] == parent['contract']['outcomes']
    assert added['contract']['sources'][:-1] == parent['contract']['sources']
    assert added['contract']['outcomes'][-1] == dict(
        id='requirement_' + sha256(b'message:2').hexdigest()[:24],
        description='Also verify reconnect recovery', checks=[])
    assert added['original_request'] == parent['original_request']
    assert 'plan' not in added['contract'] and 'plan_digest' not in added['contract']
    assert ledger.decision(parent['parent_id'])['coverage_digest'] is None
    from agent.task_lifecycle.parent_plan import source_context
    old = parent['contract']['plan']
    material = dict(envelope=old['envelope'], spec_text=old['spec_text'],
                    policy=old['policy'], check_files=old['check_files'])
    with pytest.raises(LifecycleError, match='omits authenticated'):
        ledger.bind_plan(parent['parent_id'], **material)
    context = source_context(added, old['envelope']['context'])
    assert source_context(added, context) == context
    material['envelope'] = dict(old['envelope'], context=context)
    rebound = ledger.bind_plan(parent['parent_id'], **material)
    assert rebound['contract']['plan']['envelope']['context']['manifest']['parent_sources'] == added['contract']['sources']
    assert ledger.append_requirement(parent['parent_id'], owner='human', origin='message:2',
        source_text='Also verify reconnect recovery') == rebound
    with pytest.raises(LifecycleError, match='conflict'):
        ledger.append_requirement(parent['parent_id'], owner='human', origin='message:2', source_text='different')
    with pytest.raises(LifecycleError, match='authenticated'):
        ledger.append_requirement(parent['parent_id'], owner='model', origin='message:3', source_text='skip QA')


def test_concurrent_additive_requirements_keep_both_sources(ledger):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    parent = begin(ledger)
    rendezvous = Barrier(2)
    def add(origin):
        registry = Registry(ledger.registry.db_path)
        try:
            independent = ParentLedger(registry)
            rendezvous.wait(timeout=10)
            return independent.append_requirement(parent['parent_id'], owner='human',
                                                   origin=origin, source_text='Requirement ' + origin)
        finally:
            registry.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add, origin) for origin in ('message:2', 'message:3')]
        for future in futures:
            future.result(timeout=15)
    current = ledger.get(parent['parent_id'])
    assert {source['origin'] for source in current['contract']['sources']} == {'message:1', 'message:2', 'message:3'}
    assert len(current['contract']['outcomes']) == 3
