"""Mac entry audit and fixed runner bridge; never grants parent completion."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from hermes_constants import get_hermes_home, profile_name_for_home
from .capabilities import ACTIONS
from .registry import Registry
from .types import LifecycleError


def record_legacy_execution(kind, *, registry=None):
    """A local launch lacks gateway authority even if its process later exits 0."""
    from .parent import ParentLedger
    owned = registry is None
    registry = registry or Registry()
    try:
        execution_id = 'mac-legacy-' + uuid4().hex
        ParentLedger(registry).record_execution(execution_id=execution_id,
            session_key='local:uid:' + str(os.getuid()),  # windows-footgun: ok — POSIX-only Mac launcher identity
            profile=profile_name_for_home(get_hermes_home()) or 'unresolved',
            owner='operator:' + str(os.getuid()), kind=kind)  # windows-footgun: ok — POSIX-only Mac launcher identity
        return dict(execution_id=execution_id, status='unverified',
                    parent_id=None, parent_completion=False)
    finally:
        if owned:
            registry.close()


def run_legacy_task(request, **kwargs):
    from agent.codex_task_runner import run_task
    record_legacy_execution('raw-runner:' + request.cli)
    return run_task(request, **kwargs)


def invoke(delegate, argv):
    """Wrap the preserved installed entry; legacy argv/output remains unchanged."""
    argv = list(argv)
    if argv and argv[0] == 'lifecycle':
        if len(argv) < 2 or argv[1] not in ACTIONS:
            raise LifecycleError('Unsupported trusted Mac lifecycle action')
        runner = Path(__file__).resolve().parents[2] / 'scripts/run_codex_task.py'
        # Configured interpreter only; the model supplies data, never a program.
        result = subprocess.run([str(delegate.RUNNER_PYTHON), str(runner), *argv],
                                timeout=3600 if argv[1] == 'verify' else 60, check=False)
        return result.returncode
    if argv and argv[0] in {'claude', 'qa', 'review'}:
        receipt = record_legacy_execution('hermes-delegate:' + argv[0])
        print('[Hermes task: unverified legacy execution ' + receipt['execution_id']
              + '; parent completion withheld]', file=sys.stderr)
    return delegate.main(argv)


def load_legacy(path):
    """Deployment supplies the unchanged legacy entry beside this immutable release."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise LifecycleError('Legacy entry must be an installed regular source file')
    module_spec = importlib.util.spec_from_file_location('hermes_delegate_preserved', path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module
