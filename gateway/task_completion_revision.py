"""Resume authenticated immutable preparations after prior writers have exited."""
import json
from types import SimpleNamespace

from agent.task_lifecycle.contract import _digest
from agent.task_lifecycle.types import LifecycleError


def submission_run(registry, identity, payload):
    from agent.task_lifecycle.transport import SSHExecutor
    row = registry._conn.execute('SELECT run_id FROM lifecycle_gateway_submission_receipts '
        'WHERE submission_id=?', (identity,)).fetchone()
    if row:
        return row[0]
    run_id = SSHExecutor(**payload['transport']).submit(payload['envelope'])['run_id']
    with registry._lock, registry._conn:
        registry._conn.execute('INSERT OR IGNORE INTO lifecycle_gateway_submission_receipts VALUES(?,?)',
                               (identity, run_id))
        actual = registry._conn.execute('SELECT run_id FROM lifecycle_gateway_submission_receipts '
            'WHERE submission_id=?', (identity,)).fetchone()[0]
        if actual != run_id:
            raise LifecycleError('Original execution has conflicting remote receipts')
    return run_id


def historical(scope):
    current = scope.parent['contract'].get('plan', {}).get('envelope')
    rows = scope.registry._conn.execute('SELECT submission_id,payload FROM lifecycle_gateway_submissions '
        'WHERE parent_id=? ORDER BY rowid', (scope.parent['parent_id'],)).fetchall()
    return [(identity, json.loads(encoded)) for identity, encoded in rows
            if _digest(json.loads(encoded)['envelope']) != _digest(current)]


def terminal(scope, identity, payload):
    from gateway.task_completion_remote import remote_status_reader
    run_id = submission_run(scope.registry, identity, payload)
    status = remote_status_reader(scope.registry)(run_id)
    if status.get('worker_alive') is not False or type((status.get('execution') or {}).get('process_returncode')) is not int:
        return False
    scope.ledger.record_terminal_submission(scope.parent['parent_id'], submission_id=identity, status=status)
    return True


def supersede_previous(scope, replacement_run_id):
    outcomes = [outcome['id'] for outcome in scope.parent['contract']['outcomes']]
    for identity, _ in historical(scope):
        scope.ledger.supersede_execution(identity, parent_id=scope.parent['parent_id'],
            replacement_run_id=replacement_run_id, outcome_ids=outcomes)


def preparation_schema(registry):
    with registry._lock, registry._conn:
        registry._conn.execute('''CREATE TABLE IF NOT EXISTS lifecycle_gateway_preparations (
            preparation_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
            contract_digest TEXT NOT NULL, payload TEXT NOT NULL)''')
        for action in ('UPDATE', 'DELETE'):
            registry._conn.execute(f'''CREATE TRIGGER IF NOT EXISTS lifecycle_gateway_preparations_no_{action.lower()}
                BEFORE {action} ON lifecycle_gateway_preparations BEGIN
                SELECT RAISE(ABORT, 'gateway preparations are append-only'); END''')


def resume_preparation(registry, ledger, identity, parent_id, contract_digest, encoded, *, allow_coverage=True):
    """Only a previously authenticated bound grant can launch; stale grants stay closed."""
    from agent.task_lifecycle.parent_verification import run_coverage, _coverage
    parent = ledger.get(parent_id)
    if parent['contract_digest'] != contract_digest:
        return False
    data = json.loads(encoded)
    payload, plan = data['payload'], data['plan']
    if parent['contract']['plan']['envelope'] != payload['envelope']:
        raise LifecycleError('Prepared launch differs from current authenticated plan')
    if registry._conn.execute('SELECT 1 FROM lifecycle_gateway_submissions WHERE submission_id=?', (identity,)).fetchone():
        return True
    scope = SimpleNamespace(parent=parent, ledger=ledger, registry=registry)
    if not all(terminal(scope, prior_id, prior) for prior_id, prior in historical(scope)):
        return False
    try:
        _coverage(ledger, parent)
    except LifecycleError:
        if not allow_coverage:
            raise
        coverage = plan['coverage']
        run_coverage(ledger, parent_id, argv=coverage['argv'], reviewer=coverage['reviewer'],
            implementation_cli=plan['request']['cli'], worker_root=coverage['worker_root'],
            timeout=coverage.get('timeout', 300))
    # A concurrently admitted requirement invalidates the frozen launch authority.
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        if ledger.get(parent_id)['contract_digest'] != contract_digest:
            raise LifecycleError('Parent changed before prepared launch')
    ledger.record_execution(execution_id=identity, **{key: parent[key] for key in ('session_key', 'profile', 'owner')},
        kind='managed_lifecycle', parent_id=parent_id, outcome_ids=payload['outcome_ids'])
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        if ledger.get(parent_id)['contract_digest'] != contract_digest:
            raise LifecycleError('Parent changed while recording prepared execution')
        registry._conn.execute('INSERT OR IGNORE INTO lifecycle_gateway_submissions VALUES(?,?,?)',
                               (identity, parent_id, json.dumps(payload, sort_keys=True)))
    return True
