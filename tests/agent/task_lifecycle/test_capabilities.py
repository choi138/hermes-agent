"""Mixed releases cannot silently launch a task without lifecycle support."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from agent.task_lifecycle.capabilities import require_compatible
from agent.task_lifecycle.transport import SSHExecutor
from agent.task_lifecycle.types import LifecycleError


def receipt():
    return dict(protocol='hermes-task-lifecycle', schema_version=1,
        actions=['capabilities', 'import-request', 'export-result', 'status', 'receipt', 'cancel', 'verify'],
        execution_supported=True, acceptance_scope='child', parent_completion=False,
        cli_sandboxes={'codex': ['read-only', 'workspace-write'], 'claude': ['read-only']})


@pytest.mark.parametrize('changes', [
    {'schema_version': 0}, {'schema_version': True}, {'protocol': 'old'},
    {'execution_supported': False}, {'parent_completion': True},
    {'acceptance_scope': 'parent'}, {'actions': ['import-request']},
    {'cli_sandboxes': {'claude': ['workspace-write']}}])
def test_incompatible_mac_cannot_receive_submission(changes):
    remote = receipt()
    remote.update(changes)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(remote), '')
    executor = SSHExecutor('mac', '/python', '/release/scripts/run_codex_task.py',
                           '/profiles/koharu', '/usr/bin:/bin')
    with patch('agent.task_lifecycle.transport.subprocess.run', side_effect=run):
        with pytest.raises(LifecycleError, match='mismatch'):
            executor.submit({'not': 'launched'})
    assert len(calls) == 1
    assert 'capabilities' in calls[0][-1]
    assert 'import-request' not in calls[0][-1]


def test_negotiated_transport_preserves_payload_and_fixed_remote_identity():
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        response = receipt() if len(calls) == 1 else {'run_id': 'child-1', 'complete': False}
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), '')
    executor = SSHExecutor('mac-alias', '/python', '/release/scripts/run_codex_task.py',
                           '/profiles/koharu', '/usr/bin:/bin')
    envelope = {'origin': 'authenticated-source', 'request_revision': '1'}
    with patch('agent.task_lifecycle.transport.subprocess.run', side_effect=run):
        result = executor.submit(envelope)
    assert result == {'run_id': 'child-1', 'complete': False}
    assert json.loads(calls[1][1]['input']) == envelope
    assert calls[1][0][-2] == 'mac-alias'
    assert 'HERMES_HOME=/profiles/koharu' in calls[1][0][-1]
    assert calls[0][1]['input'] is None


@pytest.mark.platforms('macos')
def test_actual_runner_advertises_child_only_safety_contract(tmp_path):
    script = Path(__file__).resolve().parents[3] / 'scripts/run_codex_task.py'
    result = subprocess.run([sys.executable, str(script), 'lifecycle', 'capabilities'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    remote = require_compatible(json.loads(result.stdout))
    assert remote['parent_completion'] is False
    assert remote['cli_sandboxes']['claude'] == ['read-only']
