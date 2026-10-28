"""Concurrent durable receipts must reconcile without regressing later phases."""

import sqlite3

import pytest

from agent.task_lifecycle.contract import TaskContract
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError, Phase


@pytest.fixture
def execution(tmp_path):
    database = tmp_path / "ledger.db"
    observer, worker = Registry(database), Registry(database)
    contract = TaskContract("Fix bug", "Fix bug", (str(tmp_path),), ("push",),
        ("tests pass",), False, "discord:1", "alice", "1", "default",
        str(tmp_path), str(tmp_path))
    run_id = worker.submit(contract).run_id
    worker.prepare_job(run_id, {"request": "Fix bug"})
    assert worker.claim_job(run_id, "worker", {"pid": 123})
    worker.mark_ready(run_id)
    try:
        yield observer, worker, run_id, database
    finally:
        observer.close()
        worker.close()


def start(worker, run_id):
    worker.mark_running(run_id,
        start_evidence={"pid": 123, "started_at": 1234.5, "executor": "test"})


def events(database, run_id):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT phase,evidence FROM lifecycle_events WHERE run_id=? ORDER BY id",
            (run_id,)).fetchall()


@pytest.mark.parametrize("advanced", [
    Phase.EXECUTION_FINISHED, Phase.VERIFYING, Phase.VERIFIED,
    Phase.DELIVERING, Phase.DELIVERY_UNCONFIRMED, Phase.DELIVERED,
])
@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("winner", ["worker", "observer", "observer_during_worker_record"])
def test_stale_observer_keeps_concurrently_committed_exit(
        execution, advanced, exit_code, winner, monkeypatch):
    observer, worker, run_id, database = execution
    start(worker, run_id)
    worker.finish_job(run_id, "worker", {"process_returncode": exit_code})
    # The status caller sees the receipt before the worker's exit event. The
    # separate writer then commits before reconciliation obtains its write lock.
    stale = observer.lookup(run_id)
    assert stale.phase is Phase.RUNNING
    assert observer.job(run_id)["result"]["process_returncode"] == exit_code
    if winner == "worker":
        worker.mark_execution_finished(run_id, exit_code=exit_code)
    elif winner == "observer":
        observer.reconcile_execution(run_id)
    else:
        original_lookup = worker.lookup

        def reconcile_after_snapshot(identity):
            snapshot = original_lookup(identity)
            if snapshot.phase is Phase.RUNNING:
                observer.reconcile_execution(identity)
            return snapshot

        # Schedule the observer between the worker's public lookup and its
        # phase transaction, without replacing either registry's DB operations.
        monkeypatch.setattr(worker, "lookup", reconcile_after_snapshot)
        worker.mark_execution_finished(run_id, exit_code=exit_code)
    later = [
        (Phase.VERIFYING, {"verification_ref": "review:1"}),
        (Phase.VERIFIED, {"artifact_revision": "revision:1", "acceptance_digest": "checks:1"}),
        (Phase.DELIVERING, {"delivery_ref": "delivery:1"}),
        (Phase.DELIVERY_UNCONFIRMED, None),
        (Phase.DELIVERED, {"message_id": "message:1", "channel_id": "channel:1",
                           "content_digest": "content:1"}),
    ]
    if advanced is not Phase.EXECUTION_FINISHED:
        for phase, evidence in later:
            worker.record_phase(run_id, phase, evidence=evidence)
            if phase is advanced:
                break
    before = events(database, run_id)
    observer.reconcile_execution(run_id)
    observer.reconcile_execution(run_id)
    if winner == "observer":
        worker.mark_execution_finished(run_id, exit_code=exit_code)
    current = observer.lookup(run_id)
    assert current.phase is advanced and current.exit_code == exit_code
    assert events(database, run_id) == before


@pytest.mark.parametrize("invalid", [
    "no_receipt", "bad_receipt", "boolean_receipt", "nonmapping_receipt",
    "unknown_without_start", "blocked", "cancelled", "conflicting_exit",
])
def test_reconciliation_rejects_missing_or_conflicting_execution_evidence(execution, invalid):
    observer, worker, run_id, database = execution
    if invalid != "unknown_without_start":
        start(worker, run_id)
    if invalid != "no_receipt":
        receipt = {"process_returncode": 0}
        if invalid == "bad_receipt":
            receipt = {"status": "worker_failed", "exit_code": 74}
        elif invalid == "boolean_receipt":
            receipt = {"process_returncode": True}
        elif invalid == "nonmapping_receipt":
            receipt = [0]
        worker.finish_job(run_id, "worker", receipt)
    if invalid == "unknown_without_start":
        worker.record_phase(run_id, Phase.UNKNOWN)
    elif invalid in {"blocked", "cancelled"}:
        worker.record_phase(run_id, Phase(invalid))
    elif invalid == "conflicting_exit":
        worker.mark_execution_finished(run_id, exit_code=7)
    before = events(database, run_id)
    with pytest.raises(LifecycleError, match="No durable execution receipt"):
        observer.reconcile_execution(run_id)
    assert events(database, run_id) == before
