"""Typed lifecycle publication: a child's acceptance never certifies its parent."""
from .parent import ParentLedger
from .types import LifecycleError


def publication(registry, run_id, content, *, parent_id=None, decision_id=None, ledger=None):
    """Trusted queue boundary. Source model text is only child execution output."""
    run = registry.lookup(run_id)
    if run is None:
        raise LifecycleError('Unknown publication execution')
    if (parent_id is None) != (decision_id is None):
        raise LifecycleError('Parent completion needs both parent and current decision identity')
    if parent_id is None:
        return dict(result_kind='execution_result', task_completion=None,
            content='[Hermes execution result; parent completion not established]\n\n'
                    + content + f'\n\n참조: {run_id}')
    ledger = ledger or ParentLedger(registry)
    parent = ledger.get(parent_id)
    if (run.contract.owner, run.contract.profile, run.contract.request_text) != (
            parent['owner'], parent['profile'], parent['original_request']):
        raise LifecycleError('Publication execution belongs to a different parent')
    decision = ledger.decision(parent_id)
    if (not decision['accepted'] or decision['decision_id'] != decision_id
            or run_id not in {v['run_id'] for v in decision['receipts']}):
        raise LifecycleError('Current authoritative parent completion required')
    text = ('[Hermes task: verified]\nparent: ' + parent_id
            + '\ncontract: ' + decision['contract_digest']
            + '\ndecision: ' + decision_id + f'\n참조: {run_id}')
    return dict(result_kind='task_completion', task_completion=decision, content=text)


def validate_publication(registry, run_id, payload, *, ledger=None):
    """Recheck immediately before send/readback, including startup recovery.

    Old untyped payloads remain stored for audit; they cannot acquire verified
    task semantics during an upgrade. Already confirmed sends are historical.
    """
    kind = payload.get('result_kind')
    if kind == 'execution_result':
        if payload.get('task_completion') is not None or not payload['content'].startswith(
                '[Hermes execution result; parent completion not established]\n'):
            raise LifecycleError('Invalid execution-result publication')
        return
    if kind != 'task_completion' or not isinstance(payload.get('task_completion'), dict):
        raise LifecycleError('Legacy/untyped result needs explicit publication reconciliation')
    decision = payload['task_completion']
    current = publication(registry, run_id, '', parent_id=decision['parent_id'],
        decision_id=decision['decision_id'], ledger=ledger)
    if current['task_completion'] != decision or current['content'] != payload['content']:
        raise LifecycleError('Queued parent publication is stale')
