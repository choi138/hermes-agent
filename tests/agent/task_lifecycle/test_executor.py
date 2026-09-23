from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace

import pytest

from agent import codex_task_runner
from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle.contract import ExecutionAuthority, TaskContract
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError, Phase, UNRESOLVED_PHASES


@pytest.fixture
def setup(tmp_path):
    spec = tmp_path / "SPEC.md"
    spec.write_text("Fix the lifecycle bug")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    request = TaskRequest(spec, tmp_path, tmp_path, artifacts)
    task = TaskContract(spec.read_text(), "Fix bug", (str(tmp_path),), ("push",),
                        ("pytest",), False, "discord:123", "alice",
                        "1", "default", str(tmp_path), str(tmp_path))
    registry = Registry(tmp_path / "ledger.db")
    yield registry, task, request
    registry.close()


def executor(registry, **kwargs):
    # These callers exercise a Mac authority, including its pinned directory
    # identity. Linux rejection is tested separately, not mistaken for a
    # successful authority/security rejection in the behavioral tests here.
    if sys.platform != "darwin":
        pytest.skip("Mac execution authority requires Darwin openat/fchdir/kqueue")
    from agent.task_lifecycle.executor import MacExecutor
    from pathlib import Path

    root = str(Path(registry._conn.execute("PRAGMA database_list").fetchone()[2]).parent)
    kwargs.setdefault("authority", ExecutionAuthority(
        "alice", "discord:123", "1", "default", root, root, (root,)))
    return MacExecutor(registry, **kwargs)


def evidence(pid=321, started_at=12345.5):
    return {"pid": pid, "started_at": started_at, "executor": "mac"}


@pytest.fixture
def fake_exec_confirmation(monkeypatch):
    # These three unit tests deliberately have no OS process. Keep the real
    # argv/FD handoff, stub only kernel exec observation for their fake PID.
    from agent.task_lifecycle import directory_handoff
    monkeypatch.setattr(directory_handoff, "_confirm_exec", lambda *args: None)


def test_launch_order_and_exit_evidence(setup, monkeypatch, fake_exec_confirmation):
    registry, task, request = setup
    seen = []

    def popen(*args, **kwargs):
        run = registry.find_by_contract(task)
        assert run.phase is Phase.READY
        seen.append("popen")
        return SimpleNamespace(pid=321)

    def run_task(actual, *, before_spawn, popen):
        assert actual is request
        assert registry.find_by_contract(task).phase is Phase.ACCEPTED
        before_spawn(actual, request.output_dir)
        assert registry.find_by_contract(task).phase in UNRESOLVED_PHASES
        process = popen(actual.argv(), cwd=str(actual.workdir), shell=False, start_new_session=True)
        run_id = registry.find_by_contract(task).run_id
        assert process.pid == 321
        assert adapter.status(run_id) is Phase.RUNNING
        saved = registry.lookup(run_id).start_evidence
        assert {key:saved[key] for key in evidence()} == evidence()
        assert saved['host'] and saved['boot'] > 0 and saved['uid'] == os.getuid()
        return {"process_returncode": 3, "exit_code": 74}

    monkeypatch.setattr(codex_task_runner, "run_task", run_task)
    adapter = executor(registry, popen=popen, process_lookup=lambda pid: evidence(pid))
    run_id = adapter.submit(task, request)
    assert registry.lookup(run_id).phase is Phase.EXECUTION_FINISHED
    assert registry.lookup(run_id).exit_code == 3
    assert adapter.submit(task, request) == run_id
    assert adapter.submit(replace(task), request) == run_id
    with pytest.raises(LifecycleError):
        adapter.submit(replace(task, owner="bob"), request)
    assert seen == ["popen"]


@pytest.mark.parametrize("ready", [False, True])
def test_unresolved_status_never_claims_running(setup, ready):
    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    if ready:
        registry.mark_ready(run_id)
    adapter = executor(registry, process_lookup=lambda pid: pytest.fail("no pid to inspect"))
    assert adapter.status(run_id) in UNRESOLVED_PHASES


@pytest.mark.parametrize("current", [None, evidence(started_at=999)])
def test_disappeared_or_reused_process_becomes_unknown_without_retry(setup, current):
    registry, task, request = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    adapter = executor(registry, process_lookup=lambda pid: current,
                       popen=lambda *a, **k: pytest.fail("must not restart"))
    assert adapter.status(run_id) is Phase.UNKNOWN
    assert registry.lookup(run_id).phase is Phase.UNKNOWN
    assert adapter.submit(task, request) == run_id


def test_exit_evidence_preserves_finished_phase(setup):
    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    registry.mark_execution_finished(run_id, exit_code=0)
    adapter = executor(registry, process_lookup=lambda pid: None)
    assert adapter.status(run_id) is Phase.EXECUTION_FINISHED


@pytest.mark.parametrize("current", [None, evidence(started_at=999), evidence(pid=999)])
def test_cancel_refuses_missing_or_mismatched_identity(setup, current):
    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    adapter = executor(registry, process_lookup=lambda pid: current,
                       terminate=lambda *a: pytest.fail("must not terminate"))
    with pytest.raises(LifecycleError):
        adapter.cancel(run_id)
    assert registry.lookup(run_id).phase is Phase.RUNNING


def test_cancel_matching_identity_and_reconnect(setup, tmp_path):
    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    other = Registry(tmp_path / "ledger.db")
    calls = []
    try:
        adapter = executor(other, process_lookup=lambda pid: evidence(pid),
                           terminate=lambda pid, started_at: calls.append((pid, started_at)))
        assert adapter.cancel(run_id) is Phase.CANCELLED
        assert calls == [(321, 12345.5)]
        assert registry.lookup(run_id).phase is Phase.CANCELLED
    finally:
        other.close()


def test_cancel_without_start_and_unknown_run_refused(setup):
    registry, task, _ = setup
    adapter = executor(registry, terminate=lambda *a: pytest.fail("must not terminate"))
    with pytest.raises(LifecycleError):
        adapter.cancel(registry.submit(task).run_id)
    with pytest.raises(LifecycleError):
        adapter.status("missing")
    with pytest.raises(LifecycleError):
        adapter.cancel("missing")


def test_real_runner_file_path_without_os_process(setup, monkeypatch, fake_exec_confirmation):
    registry, task, request = setup
    descriptors = []

    def pipe():
        read, write = os.pipe()
        descriptors.extend([os.fdopen(read, "rb", buffering=0), os.fdopen(write, "wb", buffering=0)])
        return descriptors[-2:]

    stdin_read, stdin = pipe()
    stdout, stdout_write = pipe()
    stderr, stderr_write = pipe()
    stdout_write.write(b'{"type":"done"}\n')
    stdout_write.close()
    stderr_write.close()
    process = SimpleNamespace(pid=321, stdin=stdin, stdout=stdout, stderr=stderr,
                              poll=lambda: 0, wait=lambda **kw: 0, returncode=0)
    monkeypatch.setattr(codex_task_runner, "_stop_group", lambda process: None)

    def popen(*args, **kwargs):
        assert registry.find_by_contract(task).phase is Phase.READY
        assert kwargs["start_new_session"] is True
        return process

    try:
        adapter = executor(registry, popen=popen, process_lookup=lambda pid: evidence(pid))
        run_id = adapter.submit(task, request)
        assert adapter.status(run_id) is Phase.EXECUTION_FINISHED
        assert registry.lookup(run_id).exit_code == 0
        status_file, = request.output_dir.glob("*/status.json")
        assert json.loads(status_file.read_text())["status"] == "cli_completed"
        assert stdin_read.read() == request.spec.read_bytes()
    finally:
        for stream in descriptors:
            stream.close()


def test_real_runner_spawn_failure_does_not_claim_execution(setup):
    registry, task, request = setup

    def missing(*args, **kwargs):
        raise OSError("missing binary")

    adapter = executor(registry, popen=missing)
    run_id = adapter.submit(task, request)
    assert registry.lookup(run_id).phase is Phase.BLOCKED
    assert registry.lookup(run_id).start_evidence is None
    assert registry.lookup(run_id).exit_code is None


def test_start_evidence_failure_cleans_spawned_process(setup, monkeypatch, fake_exec_confirmation):
    registry, task, request = setup
    stopped = []
    process = SimpleNamespace(pid=321)
    monkeypatch.setattr(codex_task_runner, "_stop_group", lambda child: stopped.append(child.pid))
    adapter = executor(registry, popen=lambda *a, **k: process, process_lookup=lambda pid: None)
    with pytest.raises(LifecycleError):
        adapter.submit(task, request)
    assert stopped == [321]
    assert registry.find_by_contract(task).phase is Phase.UNKNOWN
    assert adapter.submit(task, request) == registry.find_by_contract(task).run_id


def test_validation_failure_is_blocked_and_not_retried(setup):
    registry, task, request = setup
    request.spec.write_text("")
    adapter = executor(registry, popen=lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError):
        adapter.submit(task, request)
    assert registry.find_by_contract(task).phase is Phase.BLOCKED
    assert adapter.submit(task, request) == registry.find_by_contract(task).run_id


def test_default_process_lookup_and_termination_rechecks_identity(setup, monkeypatch):
    import psutil

    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    calls = []
    times = iter([12345.5, 12345.5, 12345.5])
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        pid=pid, is_running=lambda: True, status=lambda: psutil.STATUS_RUNNING,
        create_time=lambda: next(times), terminate=lambda: calls.append(pid)))
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append(pid))
    adapter = executor(registry)
    assert adapter.status(run_id) is Phase.RUNNING
    assert adapter.cancel(run_id) is Phase.CANCELLED
    assert calls == [321]


def test_default_termination_detects_identity_change_after_initial_lookup(setup, monkeypatch):
    import psutil

    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        pid=pid, create_time=lambda: 999, is_running=lambda: True,
        terminate=lambda: pytest.fail("must not signal reused pid")))
    adapter = executor(registry, process_lookup=lambda pid: evidence(pid))
    with pytest.raises(LifecycleError):
        adapter.cancel(run_id)


def test_default_lookup_missing_process(setup, monkeypatch):
    import psutil

    registry, task, _ = setup
    run_id = registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence=evidence())

    def missing(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing)
    assert executor(registry).status(run_id) is Phase.UNKNOWN
