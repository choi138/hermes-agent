"""Explicit terminal-effect audit before a full revised execution can replace old work."""
import time
import subprocess

from .contract import _digest
from .registry import _decoded, _encoded
from .types import LifecycleError


def _submission(ledger, parent, submission_id):
    conn = ledger.registry._conn
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'lifecycle_gateway_submissions', 'lifecycle_gateway_submission_receipts'} <= tables:
        raise LifecycleError('Supersession needs a committed original submission and remote receipt')
    row = conn.execute('SELECT s.parent_id,s.payload,r.run_id FROM lifecycle_gateway_submissions s '
        'JOIN lifecycle_gateway_submission_receipts r ON r.submission_id=s.submission_id '
        'WHERE s.submission_id=?', (submission_id,)).fetchone()
    if not row or row[0] != parent['parent_id']:
        raise LifecycleError('Supersession needs a committed original submission and remote receipt')
    payload = _decoded(row[1])
    envelope = payload['envelope']
    if (any(envelope[key] != parent[key] for key in ('owner', 'profile'))
            or envelope['request_text'] != parent['original_request']
            or envelope['origin'] not in {source['origin'] for source in parent['contract']['sources']}
            or envelope['destination']['session_key'] != parent['session_key']):
        raise LifecycleError('Original submission source/session differs from current parent')
    return payload, row[2]


def _terminal(status, run_id):
    if (not isinstance(status, dict) or status.get('run_id') != run_id
            or status.get('worker_alive') is not False
            or status.get('phase') in {None, 'accepted', 'ready', 'running'}
            or not isinstance(status.get('execution'), dict)
            or type(status['execution'].get('process_returncode')) is not int):
        raise LifecycleError('Original writer has no confirmed terminal execution receipt')
    return _digest(status['execution'])


def _read_terminal(payload, run_id, expected_digest=None):
    from .transport import SSHExecutor
    try:
        status = SSHExecutor(**payload['transport']).call('status', run_id)
    except subprocess.SubprocessError as exc:
        raise LifecycleError('Original terminal readback unavailable') from exc
    actual = _terminal(status, run_id)
    if expected_digest is not None and actual != expected_digest:
        raise LifecycleError('Original terminal execution receipt changed')
    return actual


def record_terminal_submission(ledger, parent_id, *, submission_id, status):
    parent = ledger.get(parent_id)
    payload, run_id = _submission(ledger, parent, submission_id)
    result_digest = _terminal(status, run_id)
    # Never accept a caller's terminal flag without current authenticated readback.
    _read_terminal(payload, run_id, result_digest)
    proof = dict(submission_id=submission_id, run_id=run_id, envelope_digest=_digest(payload['envelope']),
        transport_digest=_digest(payload['transport']), result_digest=result_digest, observed_at=time.time())
    registry = ledger.registry
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        row = registry._conn.execute('SELECT parent_id,run_id,evidence FROM lifecycle_parent_terminal_submissions '
            'WHERE submission_id=?', (submission_id,)).fetchone()
        if row:
            prior = _decoded(row[2])
            if row[:2] != (parent_id, run_id) or any(prior[key] != proof[key] for key in proof if key != 'observed_at'):
                raise LifecycleError('Original terminal observation conflict')
            return prior
        registry._conn.execute('INSERT INTO lifecycle_parent_terminal_submissions VALUES(?,?,?,?)',
            (submission_id, parent_id, run_id, _encoded(proof)))
    return proof


def _replacement(ledger, parent, run_id, outcomes, not_before):
    from .parent_plan import validate_child
    from .parent_verification import _coverage, _child_receipt
    if set(outcomes) != {outcome['id'] for outcome in parent['contract']['outcomes']}:
        raise LifecycleError('Replacement must cover every current parent outcome')
    coverage = _coverage(ledger, parent)
    if coverage['started_at'] < not_before:
        raise LifecycleError('Replacement coverage predates the original terminal observation')
    run, verified = _child_receipt(ledger.registry, run_id, ledger.remote_snapshot)
    validate_child(ledger, parent, run)
    if run.start_evidence.get('started_at', 0) < not_before:
        raise LifecycleError('Replacement execution predates the original terminal observation')
    if any(check['started_at'] < not_before for check in verified['checks']):
        raise LifecycleError('Replacement checks predate the original terminal observation')
    passed = {(check['kind'], check['name']) for check in verified['checks']
              if check['passed'] and check['revision'] == verified['artifact']['revision']}
    for outcome in parent['contract']['outcomes']:
        required = {(check['kind'], check['name']) for check in outcome['checks']}
        if not required or not required <= passed:
            raise LifecycleError('Replacement acceptance omits a current requirement')
    return dict(acceptance_digest=_digest(verified), coverage_digest=_digest(coverage))


def supersede_execution(ledger, execution_id, *, parent_id, replacement_run_id, outcome_ids):
    parent = ledger.get(parent_id)
    payload, old_run_id = _submission(ledger, parent, execution_id)
    if replacement_run_id == old_run_id:
        raise LifecycleError('A historical child cannot replace itself')
    conn = ledger.registry._conn
    original = conn.execute('SELECT parent_id,session_key,profile,owner,attribution FROM lifecycle_parent_executions '
        'WHERE execution_id=?', (execution_id,)).fetchone()
    if not original or original[:4] != tuple(parent[key] for key in ('parent_id', 'session_key', 'profile', 'owner')):
        raise LifecycleError('Only this parent historical managed execution may be superseded')
    terminal = conn.execute('SELECT evidence FROM lifecycle_parent_terminal_submissions '
        'WHERE submission_id=? AND parent_id=? AND run_id=?', (execution_id, parent_id, old_run_id)).fetchone()
    if not terminal:
        raise LifecycleError('Original terminal observation is required before replacement')
    proof = _decoded(terminal[0])
    _read_terminal(payload, old_run_id, proof['result_digest'])
    request = parent['contract'].get('plan', {}).get('envelope', {}).get('request', {})
    if any(request.get(key) != payload['envelope']['request'][key] for key in ('allowed_root', 'workdir')):
        raise LifecycleError('Replacement must verify the original repository and workdir effects')
    attribution = _decoded(original[4])
    not_before = max(proof['observed_at'], attribution['recorded_at'])
    accepted = _replacement(ledger, parent, replacement_run_id, outcome_ids, not_before)
    evidence = dict(old_run_id=old_run_id, replacement_run_id=replacement_run_id,
        terminal_digest=_digest(proof), not_before=not_before, **accepted)
    link = _encoded(dict(outcome_ids=list(outcome_ids), contract_digest=parent['contract_digest'],
                         recorded_at=attribution['recorded_at'], not_before=not_before))
    registry = ledger.registry
    with registry._lock, registry._conn:
        conn.execute('BEGIN IMMEDIATE')
        if ledger.get(parent_id)['contract_digest'] != parent['contract_digest']:
            raise LifecycleError('Parent changed during execution supersession')
        values = (execution_id, parent['contract_digest'], parent_id, replacement_run_id)
        for table, encoded in [('lifecycle_parent_execution_links', link),
                               ('lifecycle_parent_supersessions', _encoded(evidence))]:
            prior = conn.execute('SELECT * FROM ' + table + ' WHERE execution_id=? AND contract_digest=?', values[:2]).fetchone()
            if prior and prior != (*values, encoded):
                raise LifecycleError('Historical execution supersession conflict')
            conn.execute('INSERT OR IGNORE INTO ' + table + ' VALUES(?,?,?,?,?)', (*values, encoded))
    return evidence


def validate_observations(ledger, parent, inputs):
    """Run current terminal readback outside the aggregation DB transaction."""
    terminals = {row[0]: _decoded(row[3]) for row in inputs['terminals']}
    for execution_id, _, _, run_id, encoded in inputs['supersessions']:
        evidence = _decoded(encoded)
        proof = terminals.get(execution_id)
        payload, old_run = _submission(ledger, parent, execution_id)
        if (not proof or evidence['terminal_digest'] != _digest(proof)
                or evidence['old_run_id'] != old_run or evidence['replacement_run_id'] != run_id
                or proof['envelope_digest'] != _digest(payload['envelope'])
                or proof['transport_digest'] != _digest(payload['transport'])):
            raise LifecycleError('Historical supersession terminal/source binding mismatch')
        _read_terminal(payload, old_run, proof['result_digest'])
        accepted = _replacement(ledger, parent, run_id,
            [outcome['id'] for outcome in parent['contract']['outcomes']], evidence['not_before'])
        if any(accepted[key] != evidence[key] for key in accepted):
            raise LifecycleError('Historical supersession replacement evidence changed')
