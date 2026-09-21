"""Rejected authority/scope must have no reservation or launch side effects."""
from dataclasses import replace
from pathlib import Path

import pytest

from test_executor import setup, executor
from agent import codex_task_runner
from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle.types import LifecycleError


def no_side_effects(registry, monkeypatch):
    calls = []
    def runner(*args, **kwargs):
        calls.append("runner")
        return {}
    original = registry._store.reserve
    def reserve(*args, **kwargs):
        calls.append("reserve")
        return original(*args, **kwargs)
    monkeypatch.setattr(codex_task_runner, "run_task", runner)
    monkeypatch.setattr(registry._store, "reserve", reserve)
    return calls


def assert_empty(registry, calls):
    assert calls == []
    assert registry._conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == 0
    assert registry._conn.execute("SELECT count(*) FROM run_idempotency").fetchone()[0] == 0


def test_parent_workdir_outside_contract(setup, tmp_path, monkeypatch):
    registry, task, request = setup
    other = tmp_path.parent / (tmp_path.name + "-outside")
    other.mkdir()
    spec = other / "SPEC.md"
    spec.write_text("Harmless synthetic request")
    request = TaskRequest(spec, other, other, request.output_dir)
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        executor(registry, popen=lambda *a, **k: calls.append("popen")).submit(task, request)
    assert_empty(registry, calls)


def test_parent_approval_required_without_reference(setup, monkeypatch):
    registry, task, request = setup
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        task = replace(task, requires_approval=True)
        executor(registry, popen=lambda *a, **k: calls.append("popen")).submit(task, request)
    assert_empty(registry, calls)


@pytest.mark.parametrize("field,value", [
    ("owner", "bob"), ("profile", "other"), ("origin", "discord:other"),
    ("request_revision", "2"), ("allowed_paths", ("/",)),
    ("owner", ""), ("origin", None), ("profile", "bad\0value"),
    ("request_revision", True), ("request_revision", " "),
    ("approval_ref", "forged:1"),
])
def test_reject_binding_mismatch_before_reservation(setup, monkeypatch, field, value):
    registry, task, request = setup
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        changed = replace(task, **{field: value})
        executor(registry, popen=lambda *a, **k: calls.append("popen")).submit(changed, request)
    assert_empty(registry, calls)


@pytest.mark.parametrize("field", ["repo_root", "workdir", "allowed_paths"])
@pytest.mark.parametrize("value", [None, "relative", "/tmp/../escape", "/tmp/evil\0path"])
def test_malformed_scope_has_no_side_effects(setup, monkeypatch, field, value):
    registry, task, request = setup
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        changed = replace(task, **{field: (value,) if field == "allowed_paths" else value})
        executor(registry).submit(changed, request)
    assert_empty(registry, calls)


@pytest.mark.parametrize("field", ["workdir", "allowed_root"])
def test_request_must_match_exact_bound_paths(setup, monkeypatch, field):
    registry, task, request = setup
    subdir = request.workdir / "subdir"
    subdir.mkdir()
    changed = replace(request, **{field: subdir if field == "workdir" else request.allowed_root.parent})
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        executor(registry).submit(task, changed)
    assert_empty(registry, calls)


@pytest.mark.parametrize("approval", [None, "", " ", True, {}, "different:approval"])
def test_approval_ref_must_match_trusted_binding(setup, monkeypatch, approval):
    registry, task, request = setup
    task = replace(task, requires_approval=True, approval_ref="approval:1")
    adapter = executor(registry)
    adapter.authority = replace(adapter.authority, requires_approval=True, approval_ref="approval:1")
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        adapter.submit(replace(task, approval_ref=approval), request)
    assert_empty(registry, calls)


def test_cannot_downgrade_approval_requirement(setup, monkeypatch):
    registry, task, request = setup
    adapter = executor(registry)
    adapter.authority = replace(adapter.authority, requires_approval=True, approval_ref="approval:1")
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        adapter.submit(task, request)
    assert_empty(registry, calls)


def test_separate_authority_is_mandatory(setup, monkeypatch):
    registry, task, request = setup
    calls = no_side_effects(registry, monkeypatch)
    with pytest.raises(LifecycleError):
        executor(registry, authority=None).submit(task, request)
    assert_empty(registry, calls)


def test_real_harmless_child_approved_retry_and_payload_conflict(setup):
    import subprocess
    import sys
    from agent.task_lifecycle.types import Phase

    registry, task, request = setup
    task = replace(task, requires_approval=True, approval_ref="approval:1")
    calls = []
    def harmless(_argv, **kwargs):
        from test_directory_handoff import harmless_argv
        # Retain the real bootstrap, inherited directory FD and CLI -C '.'.
        calls.append(_argv[_argv.index("-C") + 1])
        return subprocess.Popen(harmless_argv(_argv), **kwargs)
    adapter = executor(registry, popen=harmless)
    adapter.authority = replace(adapter.authority, requires_approval=True, approval_ref="approval:1")
    run_id = adapter.submit(task, request)
    assert registry.lookup(run_id).phase is Phase.EXECUTION_FINISHED
    assert registry.lookup(run_id).exit_code == 0
    assert adapter.submit(task, request) == run_id
    with pytest.raises(LifecycleError, match="conflict"):
        adapter.submit(replace(task, objective="changed payload"), request)
    assert registry.submit(replace(task, objective="changed payload")).outcome == "conflict"
    assert calls == ["."]
    assert registry.lookup(run_id).contract == task


def test_symlink_escape_rejected_before_reservation(setup, monkeypatch):
    registry, task, request = setup
    original = request.workdir
    moved = original.with_name(original.name + "-moved")
    calls = no_side_effects(registry, monkeypatch)
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    try:
        with pytest.raises(LifecycleError):
            executor(registry).submit(task, request)
        assert_empty(registry, calls)
    finally:
        original.unlink()
        moved.rename(original)


@pytest.mark.parametrize("when", ["before_callback", "after_callback"])
def test_binding_rechecked_at_spawn(setup, monkeypatch, when):
    registry, task, request = setup
    calls = []
    errors = []
    adapter = executor(registry, popen=lambda *a, **k: calls.append("popen"))
    def runner(actual, *, before_spawn, popen):
        if when == "before_callback":
            adapter.authority = replace(adapter.authority, request_revision="changed")
        try:
            before_spawn(actual, request.output_dir)
            adapter.authority = replace(adapter.authority, request_revision="changed")
            popen(actual.argv())
        except LifecycleError as exc:
            errors.append(exc)
        return {}
    monkeypatch.setattr(codex_task_runner, "run_task", runner)
    run_id = adapter.submit(task, request)
    assert len(errors) == 1
    assert calls == []
    assert registry.lookup(run_id).start_evidence is None


def test_informational_bypass_does_not_register(setup, monkeypatch):
    from agent.task_lifecycle.contract import TaskContract
    registry, _, _ = setup
    calls = no_side_effects(registry, monkeypatch)
    for _ in range(2):
        assert TaskContract.from_request("What is the status?", origin="discord:123", owner="alice") is None
    assert_empty(registry, calls)
