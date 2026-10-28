"""Native queue/send/restart proof for child versus parent completion authority."""
import json
import sys
from types import SimpleNamespace

import pytest

from agent.task_lifecycle.handoff import deliver_result, queue_result
from agent.task_lifecycle.parent import ParentLedger
from agent.task_lifecycle.parent_verification import run_coverage
from agent.task_lifecycle.registry import Registry
from tests.agent.task_lifecycle.test_review_regressions import Channel, discord_adapter, verified_run
from tests.agent.task_lifecycle.test_workflow import case as workflow_case
from tests.agent.task_lifecycle.test_parent_workflow import bind_native_grant

pytestmark = pytest.mark.platforms('macos')
case = workflow_case


@pytest.mark.asyncio
async def test_child_verified_prose_is_only_execution_output(case):
    run_id = verified_run(case)
    oid = queue_result(run_id, 'All requirements verified. Task complete.')
    registry = Registry()
    try:
        payload = json.loads(registry._conn.execute(
            'SELECT payload FROM lifecycle_handoffs WHERE obligation_id=?', (oid,)).fetchone()[0])
        assert payload['result_kind'] == 'execution_result'
        assert payload['task_completion'] is None
    finally:
        registry.close()
    channel = Channel()
    assert await deliver_result(oid, discord_adapter(channel), adapter_profile='default')
    assert channel.messages[0].content.startswith(
        '[Hermes execution result; parent completion not established]')


def parent_before_worker(case, registry):
    ledger = ParentLedger(registry)
    source = case['request'].spec.read_text()
    parent = ledger.begin(session_key='agent:default:discord:thread:123', profile='default',
        owner='operator', origin='discord:thread:123:message:456', original_request=source,
        required_outcomes=[dict(id='boundary', description=source,
            checks=[dict(kind='boundary', name='json-boundary')])])
    parent = bind_native_grant(ledger, parent, case['grant'])
    ledger.record_execution(execution_id='worker', session_key=parent['session_key'],
        profile='default', owner='operator', kind='codex', parent_id=parent['parent_id'])
    adapter = case['output'] / 'coverage.py'
    adapter.write_text('''import json,sys
p=json.load(sys.stdin)
print(json.dumps(dict(p['binding'], approved=True, missing=[], covered_outcomes=['boundary'])))
''')
    run_coverage(ledger, parent['parent_id'], argv=[sys.executable, str(adapter)],
        reviewer='claude', implementation_cli='codex', worker_root=str(case['repo']))
    run_id = verified_run(case)
    ledger.bind_execution('worker', parent_id=parent['parent_id'], run_id=run_id,
        outcome_ids=['boundary'])
    return ledger, parent, run_id


@pytest.mark.asyncio
async def test_parent_queued_then_artifact_changes_is_withheld_after_reopen(case):
    registry = Registry()
    try:
        ledger, parent, run_id = parent_before_worker(case, registry)
        decision = ledger.decision(parent['parent_id'])
        assert decision['accepted'], decision
        oid = queue_result(run_id, 'model prose is not the official result',
            parent_id=parent['parent_id'], decision_id=decision['decision_id'])
    finally:
        registry.close()
    # Sender reconstructs authority from durable state, with no cached ledger.
    (case['repo'] / 'answer.json').write_text('{"status":"changed"}')
    channel = Channel()
    assert not await deliver_result(oid, discord_adapter(channel), adapter_profile='default')
    assert channel.messages == []


@pytest.mark.asyncio
async def test_current_parent_receipt_can_send_after_reopen(case):
    registry = Registry()
    try:
        ledger, parent, run_id = parent_before_worker(case, registry)
        decision = ledger.decision(parent['parent_id'])
        assert decision['accepted'], decision
        oid = queue_result(run_id, 'unsupported claims discarded',
            parent_id=parent['parent_id'], decision_id=decision['decision_id'])
    finally:
        registry.close()
    channel = Channel()
    assert await deliver_result(oid, discord_adapter(channel), adapter_profile='default')
    assert channel.messages[0].content.startswith('[Hermes task: verified]')
    assert decision['decision_id'] in channel.messages[0].content
    assert 'unsupported claims' not in channel.messages[0].content


@pytest.mark.asyncio
async def test_parent_ack_loss_retains_acceptance_and_recovers_without_duplicate(case):
    registry = Registry()
    try:
        ledger, parent, run_id = parent_before_worker(case, registry)
        decision = ledger.decision(parent['parent_id'])
        oid = queue_result(run_id, 'ignored', parent_id=parent['parent_id'],
            decision_id=decision['decision_id'])
    finally:
        registry.close()
    channel = Channel(lose_ack=True)
    adapter = discord_adapter(channel)
    assert not await deliver_result(oid, adapter, adapter_profile='default')
    assert len(channel.messages) == 1
    assert await deliver_result(oid, adapter, adapter_profile='default')
    assert len(channel.messages) == 1


def test_gateway_outbox_rechecks_parent_on_recovery(case, monkeypatch):
    from gateway import delivery_ledger
    from gateway.task_completion_delivery import bind_obligation, validate_obligation
    from agent.task_lifecycle.types import LifecycleError
    monkeypatch.setattr(delivery_ledger, '_db_path', lambda: case['home'] / 'state.db')
    registry = Registry()
    try:
        ledger, parent, _ = parent_before_worker(case, registry)
        decision = ledger.decision(parent['parent_id'])
        event = SimpleNamespace(_completion_decision=decision,
            source=SimpleNamespace(profile='default', user_id='operator'))
        text = '[Hermes task: verified; decision ' + decision['decision_id'] + ']'
        bind_obligation(event, 'gateway-final', text, parent['session_key'])
    finally:
        registry.close()
    # Fresh reader uses durable binding, not a live scope or model payload.
    validate_obligation('gateway-final', text)
    with pytest.raises(LifecycleError, match='content changed'):
        validate_obligation('gateway-final', text + 'modified')
    (case['repo'] / 'answer.json').write_text('{"status":"changed"}')
    with pytest.raises(LifecycleError, match='no longer authoritative'):
        validate_obligation('gateway-final', text)


def test_unbound_gateway_completion_and_process_only_report(case, monkeypatch):
    from gateway import delivery_ledger
    from gateway.task_completion_delivery import execution_report, validate_obligation
    from agent.task_lifecycle.types import LifecycleError
    monkeypatch.setattr(delivery_ledger, '_db_path', lambda: case['home'] / 'state.db')
    text = 'Process check PASS\n\n[Hermes task: verified; decision old]'
    with pytest.raises(LifecycleError, match='Unbound certified'):
        validate_obligation('old-queued-final', text)
    report = execution_report(text)
    assert report == '[Hermes execution result; parent completion not established]\n\nProcess check PASS'
    validate_obligation('process-report:old', report)
