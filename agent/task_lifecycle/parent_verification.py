"""Trusted parent coverage execution and aggregation of lifecycle receipts."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

from .contract import _digest
from .registry import _decoded, _encoded
from .types import LifecycleError, Phase


def _coverage_process(argv, payload, *, timeout, worker_root):
    from agent.codex_task_runner import _stop_group
    from hermes_constants import get_hermes_home
    from tools.environments.local import served_profile_child_env
    import psutil
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=worker_root, shell=False, start_new_session=True,
        env=served_profile_child_env(target_home=get_hermes_home(), inherit_credentials=True))
    try:
        process._hermes_started_at = psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:
        pass  # A short-lived adapter may already have exited.
    try:
        stdout, stderr = process.communicate(_encoded(payload), timeout=timeout)
        return process.returncode, stdout, stderr
    finally:
        _stop_group(process)


def run_coverage(ledger, parent_id, *, argv, reviewer, implementation_cli, worker_root, timeout=300):
    """Run an operator-selected read-only reviewer against the complete sources.

    A model-supplied approved payload is not an input to this API. The approved
    adapter must enforce read-only access and emit the bound JSON verdict.
    """
    from .verification import check_files, validate_checks
    if reviewer == implementation_cli:
        raise LifecycleError('Coverage reviewer must be independent of implementation')
    check = dict(name='original-request-coverage', kind='review', argv=argv,
                 reviewer=reviewer, timeout=timeout)
    validate_checks([check])
    files = check_files([check], worker_root)
    parent = ledger.get(parent_id)
    plan_digest = parent['contract'].get('plan_digest')
    if not plan_digest or plan_digest != _digest(parent['contract'].get('plan')):
        raise LifecycleError('Approved execution plan must be bound before coverage')
    binding = dict(parent_id=parent_id, contract_digest=parent['contract_digest'],
                   original_request_digest=_digest(parent['original_request']), plan_digest=plan_digest)
    payload = dict(binding=binding, contract=parent['contract'])
    started = time.time()
    code, stdout, stderr = _coverage_process(argv, payload, timeout=timeout,
        worker_root=worker_root)
    if len(stdout.encode()) > 65536:
        raise LifecycleError('Coverage verdict exceeds limit')
    try:
        verdict = json.loads(stdout)
    except ValueError as exc:
        raise LifecycleError('Coverage adapter did not emit a JSON verdict') from exc
    required = {v['id'] for v in parent['contract']['outcomes']}
    if (code != 0 or not isinstance(verdict, dict)
            or any(verdict.get(k) != v for k, v in binding.items())
            or verdict.get('approved') is not True or verdict.get('missing') != []
            or set(verdict.get('covered_outcomes', [])) != required):
        raise LifecycleError('Independent coverage did not approve the current original request')
    receipt = dict(binding=binding, reviewer=reviewer, implementation_cli=implementation_cli,
        argv=argv, check_files=files, worker_root=str(Path(worker_root).resolve()),
        verdict=verdict, started_at=started, finished_at=time.time(),
        stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest())
    r = ledger.registry
    with r._lock, r._conn:
        r._conn.execute('BEGIN IMMEDIATE')
        if ledger.get(parent_id)['contract_digest'] != parent['contract_digest']:
            raise LifecycleError('Parent contract changed during coverage review')
        row = r._conn.execute('SELECT receipt FROM lifecycle_parent_coverage '
            'WHERE parent_id=? AND contract_digest=?', (parent_id, parent['contract_digest'])).fetchone()
        if row:
            return _decoded(row[0])
        r._conn.execute('INSERT INTO lifecycle_parent_coverage VALUES(?,?,?)',
            (parent_id, parent['contract_digest'], _encoded(receipt)))
    return receipt


def _child_receipt(registry, run_id, remote_snapshot):
    from .verification import check_files, evidence, snapshot
    run = registry.lookup(run_id)
    if not run or not run.contract.execution_digest:
        raise LifecycleError('No managed lifecycle child')
    if run.phase not in {Phase.VERIFIED, Phase.DELIVERING, Phase.DELIVERY_UNCONFIRMED, Phase.DELIVERED}:
        raise LifecycleError('Child has not passed lifecycle verification')
    verified = evidence(registry, run_id)
    job = registry.job(run_id)
    grant, execution = job['payload'], job['result']
    if not verified or verified.get('accepted') is not True or not execution:
        raise LifecycleError('Missing durable child acceptance/execution receipt')
    remote = grant.get('remote_snapshot')
    if not remote:
        if check_files(grant['checks'], run.contract.repo_root) != grant['check_files']:
            raise LifecycleError('Acceptance commands changed')
        if snapshot(run.contract.workdir, tuple(verified['artifact']['files'])) != verified['artifact']:
            raise LifecycleError('Child artifacts changed')
    else:
        if remote.get('artifact') != verified['artifact']:
            raise LifecycleError('Imported remote artifact mismatch')
        if remote_snapshot is None:
            raise LifecycleError('Authenticated current remote artifact readback is required')
        if remote_snapshot(run_id, run.contract, remote['host']) != verified['artifact']:
            raise LifecycleError('Current remote artifact changed')
    phase_evidence = run.evidence.get(Phase.VERIFIED, {})
    if (phase_evidence.get('acceptance_digest') != _digest(verified)
            or phase_evidence.get('artifact_revision') != verified['artifact']['revision']):
        raise LifecycleError('Child acceptance digest mismatch')
    return run, verified


def _coverage(ledger, parent):
    from .verification import check_files
    row = ledger.registry._conn.execute('SELECT receipt FROM lifecycle_parent_coverage '
        'WHERE parent_id=? AND contract_digest=?', (parent['parent_id'], parent['contract_digest'])).fetchone()
    if not row:
        raise LifecycleError('Independent original-request coverage not run')
    receipt = _decoded(row[0])
    check = dict(argv=receipt['argv'])
    if check_files([check], receipt['worker_root']) != receipt['check_files']:
        raise LifecycleError('Coverage adapter changed')
    binding = receipt['binding']
    if (binding['contract_digest'] != parent['contract_digest']
            or binding['original_request_digest'] != _digest(parent['original_request'])
            or binding.get('plan_digest') != parent['contract'].get('plan_digest')
            or not binding.get('plan_digest')):
        raise LifecycleError('Coverage receipt does not bind the current original request')
    return receipt


def evaluate(ledger, parent_id):
    """Fail closed. Status remains available when evidence is absent or broken."""
    r = ledger.registry
    from .parent_inputs import snapshot as input_snapshot, effective_executions, pending, digest
    from .parent_plan import validate_child
    with r._lock, r._conn:
        r._conn.execute('BEGIN IMMEDIATE')
        parent = ledger.get(parent_id)
        inputs = input_snapshot(r, parent)
    missing, receipts, satisfied = [], [], set()
    try:
        coverage = _coverage(ledger, parent)
    except (LifecycleError, KeyError, ValueError, OSError) as exc:
        missing.append(str(exc))
        coverage = None
    rows = effective_executions(inputs)
    missing.extend(pending(inputs))
    from .parent_supersession import validate_observations
    try:
        validate_observations(ledger, parent, inputs)
    except (LifecycleError, KeyError, ValueError, OSError) as exc:
        missing.append('Historical execution supersession:' + str(exc))
    requirements = {v['id']: v for v in parent['contract']['outcomes']}
    for execution_id, attributed_parent, run_id, encoded in rows:
        attribution = _decoded(encoded)
        if not attributed_parent or not run_id:
            missing.append(f'unverified execution:{execution_id}')
            continue
        if attribution['contract_digest'] != parent['contract_digest']:
            missing.append(f'stale execution:{execution_id}')
            continue
        try:
            run, verified = _child_receipt(r, run_id, ledger.remote_snapshot)
            validate_child(ledger, parent, run)
            not_before = max(attribution['recorded_at'], attribution.get('not_before', 0))
            if any(c['started_at'] < not_before for c in verified['checks']):
                raise LifecycleError('Acceptance checks predate this execution')
            if (run.contract.owner, run.contract.profile, run.contract.request_text) != (
                    parent['owner'], parent['profile'], parent['original_request']):
                raise LifecycleError('Child parent binding mismatch')
            checks = {(c['kind'], c['name']) for c in verified['checks']
                      if c['passed'] is True and c['revision'] == verified['artifact']['revision']}
            for outcome_id in attribution['outcome_ids']:
                required = {(c['kind'], c['name']) for c in requirements[outcome_id]['checks']}
                if required and required <= checks:
                    satisfied.add(outcome_id)
            receipts.append(dict(run_id=run_id, contract_digest=run.contract.digest(),
                acceptance_digest=_digest(verified), artifact_revision=verified['artifact']['revision']))
        except (LifecycleError, KeyError, ValueError, OSError) as exc:
            missing.append(f'{execution_id}:{exc}')
    missing.extend(f'outcome:{identity}' for identity in requirements.keys() - satisfied)
    with r._lock, r._conn:
        r._conn.execute('BEGIN IMMEDIATE')
        if ledger.get(parent_id)['contract_digest'] != parent['contract_digest']:
            missing.append('Parent contract changed during completion aggregation')
        if digest(input_snapshot(r, parent)) != digest(inputs):
            missing.append('Completion inputs changed during aggregation')
        evidence = dict(parent_id=parent_id, contract_digest=parent['contract_digest'],
            input_digest=digest(inputs), accepted=not missing,
            status='verified' if not missing else 'unverified', missing=sorted(missing),
            coverage_digest=_digest(coverage) if coverage else None,
            receipts=sorted(receipts, key=lambda v: v['run_id']))
        decision_id = _digest(evidence)
        r._conn.execute('INSERT OR IGNORE INTO lifecycle_parent_decisions VALUES(?,?,?,?)',
            (decision_id, parent_id, _encoded(evidence), time.time()))
    return dict(evidence, decision_id=decision_id)
