"""Automatic intake through an operator-configured, bounded plan adapter.

The adapter proposes checks/outcomes for a current authenticated request. Its
execution policy and SSH target always come from profile config; independent
coverage must pass before the proposal can launch anything.
"""
import hashlib
import json
from pathlib import Path
import subprocess

from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.workflow import write_private


def automatic_plan(scope):
    config = getattr(scope.ctx, 'user_config', None) or {}
    gate = config.get('completion_gate', {}) if isinstance(config, dict) else {}
    if gate.get('mode', 'support') not in {'canary', 'enforce'}:
        return None
    planner = gate.get('planner')
    if not planner:
        return None
    argv = planner.get('argv')
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(arg, str) and arg and '\0' not in arg for arg in argv)
            or not Path(argv[0]).is_absolute()):
        raise LifecycleError('Operator planner requires an absolute argv')
    root = Path(planner['worker_root']).resolve()
    from agent.task_lifecycle.verification import check_files
    files = check_files([{'argv': argv}], root)
    payload = dict(binding=dict(scope.identity, origin=scope.request.origin,
        request_text=scope.request.text), parent=scope.parent)
    result = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
        text=True, shell=False, timeout=planner.get('timeout', 300), cwd=root, check=False)
    if result.returncode or len(result.stdout.encode()) > 262144:
        raise LifecycleError('Operator planner failed or exceeded output limit')
    proposed = json.loads(result.stdout)
    allowed = {'outcomes', 'outcome_ids', 'objective', 'checks', 'artifacts', 'spec_sha256', 'spec_text',
               'forbidden_actions', 'context'}
    if not isinstance(proposed, dict) or set(proposed) - allowed:
        raise LifecycleError('Planner may not select execution identity, host or policy')
    granted = gate.get('checks', [])
    if not proposed.get('checks') or any(check not in granted for check in proposed['checks']):
        raise LifecycleError('Planner checks must select exact operator-granted commands')
    # Static operator grants bound the remote paths, executable, sandbox and timeout.
    trusted = {key: gate[key] for key in ('transport', 'policy', 'request', 'coverage', 'check_files')}
    plan = dict(version=1, binding=payload['binding'], **trusted, **proposed)
    plan['planner_receipt'] = dict(argv=argv, files=files,
        stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest())
    return plan


def install_automatic_plan(scope, path):
    plan = automatic_plan(scope)
    if plan is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        write_private(path, plan)
    except FileExistsError:
        # An overlapping duplicate authenticated admission owns the same immutable input.
        pass
    return True
