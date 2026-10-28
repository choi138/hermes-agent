"""Connect operator-owned plans to SSH lifecycle execution and parent receipts.

Plans are private grants outside worker scope, selected by the authenticated
request fingerprint. No model tool or synthetic notification can install one.
The original envelope is committed before SSH so lost replies reuse its key.
"""
from dataclasses import asdict
import json
import logging

from agent.task_lifecycle.contract import _digest
from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.workflow import read_private

logger = logging.getLogger(__name__)


def managed_context(scope, message):
    """Keep unavailable managed admission fail-closed without stopping conversation."""
    try:
        managed = managed_turn(scope)
    except Exception as exc:
        logger.warning('Managed completion admission unavailable', exc_info=True)
        scope.error = str(exc)
        scope.defer()
        managed = []
    return message + ('\n[Hermes managed lifecycle executions: ' + json.dumps(managed) + ']' if managed else '')


def plan_path(scope):
    from hermes_constants import get_hermes_home
    binding = dict(scope.identity, parent_id=scope.parent['parent_id'],
        sources=scope.parent['contract']['sources'])
    return get_hermes_home() / 'lifecycle' / 'parent-plans' / (_digest(binding) + '.json')


def _schema(registry):
    with registry._lock, registry._conn:
        registry._conn.executescript('''
            CREATE TABLE IF NOT EXISTS lifecycle_gateway_submissions (
                submission_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_gateway_submission_receipts (
                submission_id TEXT PRIMARY KEY, run_id TEXT NOT NULL
            );
        ''')
        for table in ('submissions', 'submission_receipts'):
            for action in ('UPDATE', 'DELETE'):
                registry._conn.execute(f'''CREATE TRIGGER IF NOT EXISTS lifecycle_gateway_{table}_no_{action.lower()}
                    BEFORE {action} ON lifecycle_gateway_{table} BEGIN
                    SELECT RAISE(ABORT, 'gateway execution grants are append-only'); END''')


def _prepare(scope, plan):
    from agent.task_lifecycle.intake import gateway_envelope
    from agent.task_lifecycle.parent_verification import run_coverage
    from gateway.task_completion_policy import GatewayExecutionPolicy
    from agent.task_lifecycle.transport import SSHExecutor
    from gateway.task_completion_requests import source_context
    if plan.get('version') != 1:
        raise LifecycleError('Unsupported parent execution plan')
    binding = dict(scope.identity, origin=scope.request.origin, request_text=scope.request.text)
    if plan.get('binding') != binding:
        raise LifecycleError('Operator plan does not bind the authenticated original request')
    scope.parent = scope.ledger.bind_checks(scope.parent['parent_id'], required_outcomes=plan['outcomes'])
    transport = SSHExecutor(**plan['transport'])
    envelope = gateway_envelope(source=scope.ctx.source, session_key=scope.identity['session_key'],
        request_revision=str(scope.parent['contract']['revision']), request_text=scope.parent['original_request'],
        decision=GatewayExecutionPolicy(**plan['policy']), request=plan['request'],
        objective=plan['objective'], checks=plan['checks'], artifacts=plan['artifacts'],
        spec_sha256=plan['spec_sha256'], forbidden_actions=plan.get('forbidden_actions', ()),
        context=source_context(scope.parent, plan.get('context')))
    scope.parent = scope.ledger.bind_plan(scope.parent['parent_id'], envelope=envelope,
        spec_text=plan['spec_text'], policy=plan['policy'], check_files=plan['check_files'])
    # bind_plan preserves the first execution revision on an exact replay.
    # Submit that canonical envelope, not the newer enrichment revision.
    envelope = scope.parent['contract']['plan']['envelope']
    payload = dict(envelope=envelope, transport=asdict(transport), outcome_ids=plan['outcome_ids'])
    identity = _digest(dict(parent_id=scope.parent['parent_id'], contract_digest=scope.parent['contract_digest'],
                            payload=payload))
    from gateway.task_completion_revision import preparation_schema, resume_preparation
    registry = scope.registry
    preparation_schema(registry)
    encoded = json.dumps(dict(payload=payload, plan=plan), sort_keys=True)
    with registry._lock, registry._conn:
        registry._conn.execute('INSERT OR IGNORE INTO lifecycle_gateway_preparations VALUES(?,?,?,?)',
            (identity, scope.parent['parent_id'], scope.parent['contract_digest'], encoded))
    resume_preparation(registry, scope.ledger, identity, scope.parent['parent_id'],
        scope.parent['contract_digest'], encoded)
    return identity


def _reconcile(scope, identity, encoded, *, allow_verification=True):
    from agent.task_lifecycle.transport import SSHExecutor
    payload = json.loads(encoded)
    transport = SSHExecutor(**payload['transport'])
    registry = scope.registry
    from gateway.task_completion_revision import submission_run, terminal, supersede_previous
    run_id = submission_run(registry, identity, payload)
    current = scope.parent.get('contract', {}).get('plan', {}).get('envelope')
    if 'contract' in scope.parent and _digest(payload['envelope']) != _digest(current):
        settled = terminal(scope, identity, payload)
        return dict(submission_id=identity, run_id=run_id,
                    phase='superseded_terminal' if settled else 'running')
    status = transport.call('status', run_id)
    phase = status.get('phase')
    if allow_verification and phase in {'execution_finished', 'verifying'}:
        transport.call('verify', run_id)
        phase = transport.call('status', run_id).get('phase')
    if phase in {'verified', 'delivering', 'delivery_unconfirmed', 'delivered'}:
        # Child output is explicitly scoped and remains distinct from parent acceptance.
        transport.receive(run_id, envelope=payload['envelope'],
                          content='Hermes child execution verified; parent acceptance is evaluated separately.')
        scope.ledger.bind_execution(identity, parent_id=scope.parent['parent_id'],
            run_id=run_id, outcome_ids=payload['outcome_ids'])
        if scope.parent.get('contract', {}).get('plan'):
            supersede_previous(scope, run_id)
    return dict(submission_id=identity, run_id=run_id, phase=phase)


def managed_turn(scope):
    """Resume committed grants; only an authenticated user turn can add a new one."""
    if scope.error or scope.parent is None or scope.mode == 'support':
        return []
    registry = scope.registry
    _schema(registry)
    # Every authenticated source is part of the current full objective. Status
    # questions only reconcile the existing grant and never replace its inputs.
    from gateway.task_completion_requests import adds_requirement, task_status
    explicit_retry = bool(scope.request and (adds_requirement(scope.request.text, scope.parent)
        or task_status(scope.request.text)))
    if scope.request and adds_requirement(scope.request.text, scope.parent):
        path = plan_path(scope)
        if not path.exists():
            from gateway.task_completion_planning import install_automatic_plan
            install_automatic_plan(scope, path)
        if path.exists():
            scope.defer()
            plan = json.loads(read_private(path))
            _prepare(scope, plan)
    from gateway.task_completion_revision import preparation_schema, resume_preparation
    preparation_schema(registry)
    for prepared in registry._conn.execute('SELECT preparation_id,parent_id,contract_digest,payload '
            'FROM lifecycle_gateway_preparations WHERE parent_id=? AND contract_digest=?',
            (scope.parent['parent_id'], scope.parent['contract_digest'])).fetchall():
        if explicit_retry:
            resume_preparation(registry, scope.ledger, *prepared)
    rows = registry._conn.execute('SELECT submission_id,payload FROM lifecycle_gateway_submissions '
                                  'WHERE parent_id=?', (scope.parent['parent_id'],)).fetchall()
    results = [_reconcile(scope, identity, payload, allow_verification=explicit_retry)
        for identity, payload in rows]
    if explicit_retry and registry._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='lifecycle_gateway_recovery'").fetchone():
        from gateway.task_completion_recovery import _job_state
        for result in results:
            if result['phase'] in {'accepted', 'ready', 'running', 'verified', 'delivering', 'delivery_unconfirmed', 'delivered'}:
                _job_state(registry, result['submission_id'], 'running')
    scope.managed_executions = results
    return results


def remote_snapshot_reader(registry):
    """Every accepted decision reads current artifact bytes over configured SSH."""
    def read(run_id, contract, host):
        from agent.task_lifecycle.transport import SSHExecutor
        table = registry._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='lifecycle_gateway_submission_receipts'").fetchone()
        if not table:
            raise LifecycleError('No authenticated remote transport binding')
        row = registry._conn.execute('SELECT s.payload FROM lifecycle_gateway_submissions s '
            'JOIN lifecycle_gateway_submission_receipts r ON r.submission_id=s.submission_id '
            'WHERE r.run_id=?', (run_id,)).fetchone()
        if not row:
            raise LifecycleError('Remote run lacks committed gateway transport')
        payload = json.loads(row[0])
        envelope = payload['envelope']
        if any(getattr(contract, key) != envelope[key] for key in
               ('owner', 'origin', 'profile', 'request_text', 'request_revision')):
            raise LifecycleError('Remote readback contract differs from original request')
        package = SSHExecutor(**payload['transport']).call('export-result', run_id,
            payload={'content': 'Current parent acceptance readback', 'attachments': []})
        if (package.get('run_id') != run_id or package.get('host') != host
                or _digest(package.get('contract')) != _digest(asdict(contract))):
            raise LifecycleError('Authenticated remote readback identity mismatch')
        return package['verification']['artifact']
    return read
