from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3

import pytest

from agent.task_lifecycle.contract import TaskContract
from agent.task_lifecycle.types import LifecycleError, Phase, UNRESOLVED_PHASES


@pytest.fixture
def task(tmp_path):
    return TaskContract("Fix bug", "Fix bug", (str(tmp_path),), ("push",),
                        ("tests pass",), False, "discord:1", "alice",
                        "1", "default", str(tmp_path), str(tmp_path))


@pytest.fixture
def registry(tmp_path):
    from agent.task_lifecycle.registry import Registry

    instance = Registry(tmp_path / "ledger.db")
    yield instance
    instance.close()


def test_submit_reuses_store_and_conflicts_without_overwrite(registry, task, monkeypatch):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    original = RunIdempotencyStore.reserve
    calls = []

    def reserve(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunIdempotencyStore, "reserve", reserve)
    first = registry.submit(task)
    second = registry.submit(task)
    conflict = registry.submit(replace(task, owner="bob"))
    assert (first.outcome, second.outcome, conflict.outcome) == ("created", "reused", "conflict")
    assert first.run_id == second.run_id == conflict.run_id
    assert first.phase is Phase.ACCEPTED
    assert len(calls) == 3
    assert len(registry.open_runs()) == 1
    assert registry.lookup(first.run_id).contract == task


def test_reconnect_and_missing_lookup(tmp_path, registry, task):
    from agent.task_lifecycle.registry import Registry

    run = registry.submit(task)
    registry.mark_ready(run.run_id)
    other = Registry(tmp_path / "ledger.db")
    try:
        assert other.find_by_contract(task).run_id == run.run_id
        assert other.find_by_contract(task).phase is Phase.READY
        assert other.find_by_contract(replace(task, objective="missing", request_revision="2")) is None
        assert other.find_by_contract(replace(task, owner="bob")).outcome == "conflict"
        assert other.lookup("missing") is None
        with pytest.raises(LifecycleError):
            other.mark_ready("missing")
    finally:
        other.close()


@pytest.mark.parametrize("evidence", [None, {}, {"pid": 1},
    {"pid": 1, "started_at": 10}, {"pid": 0, "started_at": 10, "executor": "mac"},
    {"pid": True, "started_at": 10, "executor": "mac"},
    {"pid": 1, "started_at": float("nan"), "executor": "mac"},
    {"pid": 1, "started_at": 10, "executor": ""}, "not a mapping"])
def test_running_requires_real_start_evidence(registry, task, evidence):
    run = registry.submit(task)
    with pytest.raises(LifecycleError):
        registry.mark_running(run.run_id, start_evidence=evidence)
    with pytest.raises(LifecycleError):
        registry.record_phase(run.run_id, Phase.RUNNING, evidence=evidence)
    assert registry.lookup(run.run_id).phase is Phase.ACCEPTED


def test_omitted_start_evidence_raises_lifecycle_error(registry, task):
    with pytest.raises(LifecycleError):
        registry.mark_running(registry.submit(task).run_id)


def test_phases_events_and_regression(registry, task, tmp_path):
    run_id = registry.submit(task).run_id
    assert registry.lookup(run_id).phase in UNRESOLVED_PHASES
    registry.mark_ready(run_id)
    assert registry.lookup(run_id).phase in UNRESOLVED_PHASES
    evidence = {"pid": 123, "started_at": 1234.5, "executor": "mac"}
    registry.mark_running(run_id, start_evidence=evidence)
    registry.mark_execution_finished(run_id, exit_code=7)
    registry.record_phase(run_id, Phase.VERIFYING, evidence={"verification_ref": "attempt:1"})
    registry.record_phase(run_id, Phase.VERIFIED,
                          evidence={"checks": "passed", "artifact_revision": "rev:1",
                                    "acceptance_digest": "checks:1"})
    with pytest.raises(LifecycleError):
        registry.mark_running(run_id, start_evidence=evidence)
    state = registry.lookup(run_id)
    assert state.phase is Phase.VERIFIED
    assert state.start_evidence == evidence and state.exit_code == 7
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        rows = conn.execute("SELECT phase, evidence FROM lifecycle_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        assert [row[0] for row in rows] == ["accepted", "ready", "running", "execution_finished", "verifying", "verified"]
        assert json.loads(rows[0][1])["contract"]["objective"] == task.objective
        assert json.loads(rows[2][1]) == evidence
        for sql in ("UPDATE lifecycle_events SET phase='running'", "DELETE FROM lifecycle_events"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


@pytest.mark.parametrize("phase", [Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED, Phase.DELIVERED])
def test_terminal_excluded_from_open_runs(registry, task, phase):
    from test_state_machine import advance
    run_id = advance(registry, task, phase)
    assert [r.run_id for r in registry.open_runs()] == ([run_id] if phase in {Phase.UNKNOWN, Phase.BLOCKED} else [])
    for outcome in (Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED):
        run_id = registry.submit(replace(task, request_revision=outcome.value)).run_id
        registry.record_phase(run_id, outcome)
        assert registry.lookup(run_id).phase is outcome


def test_exit_requires_start_and_integer_code(registry, task):
    run_id = registry.submit(task).run_id
    with pytest.raises(LifecycleError):
        registry.mark_execution_finished(run_id, exit_code=0)
    registry.mark_ready(run_id)
    registry.mark_running(run_id, start_evidence={"pid": 1, "started_at": 10, "executor": "mac"})
    with pytest.raises(LifecycleError):
        registry.mark_execution_finished(run_id, exit_code=None)
    with pytest.raises(LifecycleError):
        registry.record_phase(run_id, Phase.EXECUTION_FINISHED)


def test_concurrent_reservation_has_one_created(tmp_path, task):
    from agent.task_lifecycle.registry import Registry

    registries = [Registry(tmp_path / "ledger.db") for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda registry: registry.submit(task), registries))
        assert [r.outcome for r in results].count("created") == 1
        assert len({r.run_id for r in results}) == 1
        with sqlite3.connect(tmp_path / "ledger.db") as conn:
            assert conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == 1
    finally:
        for registry in registries:
            registry.close()


def test_reservation_survives_crash_before_first_event(tmp_path, task):
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    from agent.task_lifecycle.registry import Registry
    from dataclasses import asdict

    store = RunIdempotencyStore(str(tmp_path / "ledger.db"))
    store.reserve("task_lifecycle", task.idempotency_key(), task.digest(), "crashed-run",
                  {"phase": "accepted", "contract": asdict(task)})
    store.close()
    registry = Registry(tmp_path / "ledger.db")
    try:
        assert registry.find_by_contract(task).run_id == "crashed-run"
        assert registry.open_runs()[0].phase is Phase.ACCEPTED
        assert registry.submit(task).outcome == "reused"
    finally:
        registry.close()


def test_legacy_draft_events_migrate_without_touching_original_rows(tmp_path, task):
    from agent.task_lifecycle.registry import Registry
    db=tmp_path/'legacy.db'
    registry=Registry(db)
    run_id=registry.submit(task).run_id
    registry.mark_ready(run_id)
    registry.close()
    with sqlite3.connect(db) as conn:
        conn.execute('ALTER TABLE lifecycle_events RENAME TO events')
        conn.execute('DROP TRIGGER lifecycle_events_v2_no_update')
        conn.execute('DROP TRIGGER lifecycle_events_v2_no_delete')
        conn.execute("CREATE TRIGGER lifecycle_events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END")
        before=conn.execute('SELECT * FROM events').fetchall()
    first=Registry(db)
    assert first.lookup(run_id).phase is Phase.READY
    first.close()
    second=Registry(db)
    assert second.lookup(run_id).phase is Phase.READY
    second.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT * FROM events').fetchall()==before
        assert conn.execute('SELECT count(*) FROM lifecycle_events').fetchone()[0]==len(before)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM lifecycle_events')
