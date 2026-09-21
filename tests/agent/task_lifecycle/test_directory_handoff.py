"""Local Mac proof. Substitute only the workload suffix, never the bootstrap.

Independent probes can reuse fixture ``case`` and ``harmless_argv``. The
injected Popen sees bootstrap argv and safe cwd='/', not the original -C path.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle.contract import ExecutionAuthority, TaskContract
from agent.task_lifecycle.executor import MacExecutor, _process_lookup
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError, Phase

pytestmark = [pytest.mark.skipif(sys.platform != "darwin", reason="Mac kqueue exec handoff"),
              pytest.mark.live_system_guard_bypass]  # Only this test's own temporary children.


@pytest.fixture
def case(tmp_path):
    repo, outside, artifacts = (tmp_path / n for n in ("repo", "outside", "artifacts"))
    for p in (repo, outside, artifacts):
        p.mkdir(mode=0o700)
    work = repo / "work"
    work.mkdir()
    spec = repo / "SPEC.md"
    spec.write_text("Harmless pinned directory proof")
    request = TaskRequest(spec, work, repo, artifacts, timeout=10)
    task = TaskContract(spec.read_text(), "pin cwd", (str(repo),), ("network",),
                        ("local proof",), False, "local:probe", "owner", "1", "test",
                        str(repo), str(work))
    authority = ExecutionAuthority("owner", "local:probe", "1", "test", str(repo),
                                   str(work), (str(repo),))
    with_registry = Registry(tmp_path / "ledger.db")
    try:
        yield with_registry, task, request, authority, outside
    finally:
        with_registry.close()


WORKLOAD = """
import json,os,sys
from pathlib import Path
initial=os.stat('.')
Path('initial.json').write_text(json.dumps(dict(dev=initial.st_dev,ino=initial.st_ino)))
prompt=sys.stdin.buffer.read().decode()
# Simulate the CLI's actual directory argument consumption, after cwd handoff.
os.chdir(sys.argv[sys.argv.index('-C')+1])
s=os.stat('.')
data=dict(dev=s.st_dev,ino=s.st_ino,cwd=os.getcwd(),prompt=prompt,pid=os.getpid(),pgid=os.getpgrp())
Path('child.json').write_text(json.dumps(data))
print(json.dumps(data),flush=True)
print('captured stderr',file=sys.stderr,flush=True)
"""


def harmless_argv(argv, code=WORKLOAD):
    """Preserve the production handoff; replace only codex with harmless Python.

    Legacy arm exists to reproduce the exact before-version vulnerability.
    New-code runs must assert the bootstrap branch is used.
    """
    if argv[0] == "codex":
        return [sys.executable, "-I", "-S", "-c", code, *argv[1:]]
    marker = argv.index("--")
    assert argv[marker + 1] == "codex"
    return [*argv[:marker + 1], sys.executable, "-I", "-S", "-c", code, *argv[marker + 2:]]


@pytest.mark.parametrize("target", ["work", "repo"])
@pytest.mark.parametrize("restore_at_spawn", [False, True])
def test_real_after_final_check_never_uses_outside(case, target, restore_at_spawn):
    registry, task, request, authority, outside = case
    original = request.workdir if target == "work" else request.allowed_root
    saved = original.with_name(original.name + "-saved")
    if target == "repo":
        (outside / "work").mkdir()
    identity = request.workdir.stat()
    calls = []
    def restore():
        if original.is_symlink():
            original.unlink()
            saved.rename(original)
    def spawn(argv, **kwargs):
        calls.append(argv)
        original.rename(saved)
        original.symlink_to(outside, target_is_directory=True)
        child = subprocess.Popen(harmless_argv(argv), **kwargs)
        if restore_at_spawn:
            restore()
        return child
    def lookup(pid):
        restore()  # Late arm: after the handoff's exec confirmation, before ledger replay.
        return _process_lookup(pid)
    adapter = MacExecutor(registry, authority=authority, popen=spawn, process_lookup=lookup)
    try:
        run_id = adapter.submit(task, request)
    finally:
        restore()
    assert len(calls) == 1, "attack must reach actual Popen boundary"
    assert not list(outside.rglob("*.json")), "workload wrote in unapproved inode"
    initial = json.loads((request.workdir / "initial.json").read_text())
    assert (initial["dev"], initial["ino"]) == (identity.st_dev, identity.st_ino)
    data = json.loads((request.workdir / "child.json").read_text())
    assert (data["dev"], data["ino"]) == (identity.st_dev, identity.st_ino)
    assert data["prompt"] == request.spec.read_text()
    assert data["pid"] == data["pgid"] == registry.lookup(run_id).start_evidence["pid"]
    assert registry.lookup(run_id).phase is Phase.EXECUTION_FINISHED
    assert registry.lookup(run_id).exit_code == 0
    assert adapter.submit(task, request) == run_id
    assert len(calls) == 1


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_real_stable_handoff_preserves_runner_contract(case, sandbox):
    registry, task, request, authority, _ = case
    request = replace(request, sandbox=sandbox)
    calls = []
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.Popen(harmless_argv(argv), **kwargs)
    adapter = MacExecutor(registry, authority=authority, popen=spawn)
    run_id = adapter.submit(task, request)
    assert registry.lookup(run_id).exit_code == 0
    argv, kwargs = calls[0]
    assert argv[0] != "codex", "must use actual descriptor handoff bootstrap"
    assert argv[argv.index("-C") + 1] == "."
    assert argv[argv.index("-s") + 1] == request.sandbox
    assert kwargs["cwd"] == "/"
    assert kwargs["shell"] is False and kwargs["start_new_session"] is True
    assert "preexec_fn" not in kwargs
    for fd in kwargs["pass_fds"]:
        with pytest.raises(OSError):
            os.fstat(fd)
    artifacts, = request.output_dir.iterdir()
    data = json.loads((artifacts / "events.jsonl").read_text())
    assert data["prompt"] == request.spec.read_text()
    assert data["ino"] == request.workdir.stat().st_ino
    assert (artifacts / "stderr.log").read_text() == "captured stderr\n"
    assert adapter.submit(task, request) == run_id
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["work", "repo"])
def test_real_directory_replacement_before_submission_has_zero_effects(case, target):
    registry, task, request, authority, outside = case
    original = request.workdir if target == "work" else request.allowed_root
    saved = original.with_name(original.name + "-saved")
    original.rename(saved)
    outside.rename(original)
    if target == "repo":
        (original / "work").mkdir()
    try:
        with pytest.raises(LifecycleError):
            MacExecutor(registry, authority=authority,
                        popen=lambda *a, **k: pytest.fail("unapproved launch")).submit(task, request)
        assert registry._conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == 0
        assert registry._conn.execute("SELECT count(*) FROM run_idempotency").fetchone()[0] == 0
    finally:
        original.rename(outside)
        saved.rename(original)


@pytest.mark.parametrize("target,when", [("repo", "before"), ("work", "before"), ("repo", "after")])
def test_acquisition_rename_and_restore_uses_checked_objects(case, monkeypatch, target, when):
    registry, task, request, authority, outside = case
    original = request.allowed_root if target == "repo" else request.workdir
    saved = original.with_name(original.name + "-saved")
    real_open = os.open
    fired = []
    opened = []
    def swap():
        original.rename(saved)
        outside.rename(original)
    def restore():
        original.rename(outside)
        saved.rename(original)
    def racing_open(path, *args, **kwargs):
        frame = sys._getframe(1)
        active = (frame.f_code.co_name == "open_directory"
                  and frame.f_locals.get("expected") is not None)
        if not fired and active and path == target:
            fired.append(path)
            if when == "before":
                swap()
            fd = real_open(path, *args, **kwargs)
            opened.append(fd)
            if when == "after":
                swap()
            restore()  # Before production fstat: path checks alone see no change.
            return fd
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", racing_open)
    # Capability membership is a property of the underlying built-in open.
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, racing_open})
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        return subprocess.Popen(harmless_argv(argv), **kwargs)
    adapter = MacExecutor(registry, authority=authority, popen=spawn)
    if when == "before":
        with pytest.raises(LifecycleError, match="object changed"):
            adapter.submit(task, request)
        assert calls == []
        assert registry._conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == 0
        assert registry._conn.execute("SELECT count(*) FROM run_idempotency").fetchone()[0] == 0
    else:
        assert registry.lookup(adapter.submit(task, request)).exit_code == 0
        assert len(calls) == 1
    assert fired == [target]
    assert not list(outside.rglob("*.json"))
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("when", ["before", "after"])
def test_actual_final_bind_boundary(case, monkeypatch, when):
    registry, task, request, authority, outside = case
    work = request.workdir
    saved = work.with_name("saved")
    fired, calls = [], []
    real_bind = TaskContract.bind
    def swap():
        work.rename(saved)
        work.symlink_to(outside, target_is_directory=True)
        fired.append(when)
    def restore():
        if work.is_symlink():
            work.unlink()
            saved.rename(work)
    def bind(self, *args):
        at_boundary = sys._getframe(1).f_code.co_name == "observed_popen"
        if at_boundary and when == "before":
            swap()
        result = real_bind(self, *args)
        if at_boundary and when == "after":
            swap()
        return result
    monkeypatch.setattr(TaskContract, "bind", bind)
    def spawn(argv, **kwargs):
        calls.append(argv)
        child = subprocess.Popen(harmless_argv(argv), **kwargs)
        restore()
        return child
    try:
        adapter = MacExecutor(registry, authority=authority, popen=spawn)
        if when == "before":
            with pytest.raises(LifecycleError):
                adapter.submit(task, request)
            assert calls == []
        else:
            assert registry.lookup(adapter.submit(task, request)).exit_code == 0
            assert len(calls) == 1
    finally:
        restore()
    assert fired == [when]
    assert not list(outside.rglob("*.json"))


@pytest.mark.parametrize("target", ["work", "repo"])
def test_replacement_at_cli_directory_consumption(case, target):
    registry, task, request, authority, outside = case
    original = request.workdir if target == "work" else request.allowed_root
    saved = original.with_name(original.name + "-saved")
    if target == "repo":
        (outside / "work").mkdir()
    # The harmless workload itself schedules the exact CLI -C use boundary.
    attack = f"""
original,saved,outside=map(Path,{[str(original), str(saved), str(outside)]!r})
original.rename(saved)
original.symlink_to(outside,target_is_directory=True)
"""
    restore = "original.unlink(); saved.rename(original)"
    code = WORKLOAD.replace("os.chdir(sys.argv[sys.argv.index('-C')+1])",
                            attack + "\nos.chdir(sys.argv[sys.argv.index('-C')+1])")
    code += "\n" + restore
    calls = []
    def spawn(argv, **kwargs):
        calls.append(argv)
        assert argv[0] != "codex"
        return subprocess.Popen(harmless_argv(argv, code), **kwargs)
    run_id = MacExecutor(registry, authority=authority, popen=spawn).submit(task, request)
    assert len(calls) == 1
    assert registry.lookup(run_id).exit_code == 0
    assert not list(outside.rglob("*.json"))
    assert json.loads((request.workdir / "child.json").read_text())["ino"] == request.workdir.stat().st_ino


@pytest.mark.parametrize("failure", ["exec_missing", "bootstrap_exit_zero", "bootstrap_timeout"])
def test_bootstrap_is_not_workload_evidence_and_closes_resources(case, failure):
    registry, task, request, authority, _ = case
    request = replace(request, timeout=.4)
    children, descriptors = [], []
    def spawn(argv, **kwargs):
        descriptors.extend(kwargs["pass_fds"])
        command = list(argv)
        if failure == "exec_missing":
            command[command.index("--") + 1] = "/nonexistent-lifecycle-probe-binary"
        else:
            # Failure injection only: deliberately no exec from bootstrap.
            command[4] = "import time; time.sleep(.1)" if failure == "bootstrap_exit_zero" else "import time; time.sleep(30)"
        child = subprocess.Popen(command, **kwargs)
        children.append(child)
        return child
    run_id = MacExecutor(registry, authority=authority, popen=spawn).submit(task, request)
    assert len(children) == 1
    assert registry.lookup(run_id).phase is Phase.BLOCKED
    assert registry.lookup(run_id).start_evidence is None
    assert registry.lookup(run_id).exit_code is None
    assert children[0].poll() is not None
    assert all(stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr))
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_real_evidence_failure_reaps_child_and_closes_pipes(case):
    registry, task, request, authority, _ = case
    children = []
    def spawn(argv, **kwargs):
        child = subprocess.Popen(harmless_argv(argv), **kwargs)
        children.append(child)
        return child
    adapter = MacExecutor(registry, authority=authority, popen=spawn, process_lookup=lambda pid: None)
    with pytest.raises(LifecycleError, match="identity"):
        adapter.submit(task, request)
    assert registry.find_by_contract(task).phase is Phase.UNKNOWN
    assert children[0].poll() is not None
    assert all(stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr))


def test_handoff_has_no_parent_or_workload_descriptor_leaks(case):
    registry, task, request, authority, _ = case
    before = set(os.listdir("/dev/fd"))
    code = """
import os,sys,json
sys.stdin.buffer.read()
fds=[]
for text in os.listdir('/dev/fd'):
    fd=int(text)
    if fd>2:
        try: os.fstat(fd)
        except OSError: continue
        fds.append(fd)
print(json.dumps(fds))
"""
    def spawn(argv, **kwargs):
        return subprocess.Popen(harmless_argv(argv, code), **kwargs)
    adapter = MacExecutor(registry, authority=authority, popen=spawn)
    for i in range(8):
        adapter.authority = replace(authority, request_revision=str(i))
        assert registry.lookup(adapter.submit(replace(task, request_revision=str(i)), request)).exit_code == 0
    assert set(os.listdir("/dev/fd")) == before
    for artifact in request.output_dir.iterdir():
        assert json.loads((artifact / "events.jsonl").read_text()) == []


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_real_timeout_and_cancel_preserve_owned_process_group(case, mode):
    import psutil
    registry, task, request, authority, _ = case
    request = replace(request, timeout=.6 if mode == "timeout" else 3)
    children, errors, result = [], [], []
    code = """
import os,sys,time,subprocess,json
from pathlib import Path
sys.stdin.buffer.read()
child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(30)'])
Path('group.json').write_text(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),child=child.pid)))
time.sleep(30)
"""
    def spawn(argv, **kwargs):
        child = subprocess.Popen(harmless_argv(argv, code), **kwargs)
        children.append(child)
        return child
    adapter = MacExecutor(registry, authority=authority, popen=spawn)
    def submit():
        try:
            result.append(adapter.submit(task, request))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=submit)
    thread.start()
    try:
        until = time.monotonic() + 2
        marker = request.workdir / "group.json"
        while not marker.exists() and time.monotonic() < until:
            time.sleep(.01)
        assert marker.exists(), "must reach real workload and descendant creation"
        data = json.loads(marker.read_text())
        assert data["pid"] == data["pgid"] == children[0].pid
        assert data["pgid"] != os.getpgrp()
        if mode == "cancel":
            run = registry.find_by_contract(task)
            assert adapter.cancel(run.run_id) is Phase.CANCELLED
        thread.join(1.5)
        assert not thread.is_alive(), "cancellation must not wait for task timeout/descendant pipes"
        assert errors == []
        assert children[0].poll() is not None
        try:
            descendant = psutil.Process(data["child"])
            assert not descendant.is_running() or descendant.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
        status_file, = request.output_dir.glob("*/status.json")
        status = json.loads(status_file.read_text())
        if mode == "timeout":
            assert status["status"] == "timed_out" and status["exit_code"] == 124
        else:
            assert registry.lookup(result[0]).phase is Phase.CANCELLED
    finally:
        # On RED, allow the bounded real runner to finish its group cleanup.
        thread.join(5)
