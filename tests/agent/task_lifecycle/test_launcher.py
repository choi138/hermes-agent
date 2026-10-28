"""Mac entry records legacy execution without promoting process output."""
import sqlite3
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.task_lifecycle.launcher import invoke, record_legacy_execution, run_legacy_task
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError


def test_legacy_entry_persists_unattributed_execution_across_restart(tmp_path):
    path = tmp_path / 'state.db'
    registry = Registry(path)
    try:
        receipt = record_legacy_execution('raw-runner:codex', registry=registry)
    finally:
        registry.close()
    registry = Registry(path)
    try:
        rows = registry._conn.execute('SELECT execution_id,parent_id,run_id,kind '
                                     'FROM lifecycle_parent_executions').fetchall()
        assert rows == [(receipt['execution_id'], None, None, 'raw-runner:codex')]
        assert registry.open_runs() == []
        assert receipt['parent_completion'] is False
        assert receipt['status'] == 'unverified'
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            registry._conn.execute('DELETE FROM lifecycle_parent_executions')
    finally:
        registry.close()


def test_entry_preserves_legacy_argv_and_does_not_certify_its_zero_exit():
    calls = []
    delegate = SimpleNamespace(main=lambda argv: calls.append(argv) or 0)
    argv = ['claude', '--spec', '/approved/SPEC.md']
    with patch('agent.task_lifecycle.launcher.record_legacy_execution',
               return_value={'execution_id': 'observed-child'}) as audit:
        assert invoke(delegate, argv) == 0
    audit.assert_called_once_with('hermes-delegate:claude')
    assert calls == [argv]


def test_trusted_entry_only_invokes_fixed_runner_not_payload_program():
    delegate = SimpleNamespace(RUNNER_PYTHON='/approved/python')
    with patch('agent.task_lifecycle.launcher.subprocess.run',
               return_value=SimpleNamespace(returncode=0)) as run:
        assert invoke(delegate, ['lifecycle', 'import-request']) == 0
        argv = run.call_args.args[0]
        assert argv[0] == '/approved/python'
        assert argv[1].endswith('/scripts/run_codex_task.py')
        assert argv[2:] == ['lifecycle', 'import-request']
        with pytest.raises(LifecycleError, match='Unsupported'):
            invoke(delegate, ['lifecycle', '_worker', 'untrusted'])
        assert run.call_count == 1


def test_status_and_preparation_do_not_create_execution_receipts():
    delegate = SimpleNamespace(main=lambda argv: 0)
    with patch('agent.task_lifecycle.launcher.record_legacy_execution') as audit:
        assert invoke(delegate, ['status', '/run']) == 0
        assert invoke(delegate, ['codex', '--help']) == 0
    audit.assert_not_called()


def test_raw_runner_audits_before_child_and_preserves_process_result():
    order = []
    result = {'status': 'cli_completed', 'exit_code': 0}
    with patch('agent.task_lifecycle.launcher.record_legacy_execution',
               side_effect=lambda kind: order.append(('audit', kind))), patch(
               'agent.codex_task_runner.run_task',
               side_effect=lambda request, **kwargs: order.append(('child', request.cli)) or result):
        returned = run_legacy_task(SimpleNamespace(cli='codex'))
    assert returned is result
    assert order == [('audit', 'raw-runner:codex'), ('child', 'codex')]


def test_installed_entry_pins_runner_release_and_pm_interpreter():
    from scripts.run_hermes_delegate import main
    delegate = SimpleNamespace(RUNNER_PYTHON='/old/python')
    with patch('agent.task_lifecycle.launcher.load_legacy', return_value=delegate), patch(
            'agent.task_lifecycle.launcher.invoke', return_value=0) as dispatch:
        assert main(['lifecycle', 'capabilities']) == 0
    dispatch.assert_called_once_with(delegate, ['lifecycle', 'capabilities'])
    assert delegate.RUNNER_PYTHON == Path(sys.executable)
    assert delegate.RUNNER == delegate.RELEASE / 'scripts/run_codex_task.py'
