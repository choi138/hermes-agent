"""Exercise adjacency and evidence through the generic API and stored ledger."""
import json

import pytest

from test_registry import registry, task
from agent.task_lifecycle.types import LifecycleError, Phase, is_complete


PATH = [Phase.ACCEPTED, Phase.READY, Phase.RUNNING, Phase.EXECUTION_FINISHED,
        Phase.VERIFYING, Phase.VERIFIED, Phase.DELIVERING,
        Phase.DELIVERY_UNCONFIRMED, Phase.DELIVERED]
FAILURES = {Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED}
EVIDENCE = {
    Phase.RUNNING: {"pid": 12, "started_at": 123.5, "executor": "mac"},
    Phase.EXECUTION_FINISHED: {"exit_code": 0},
    Phase.VERIFYING: {"verification_ref": "attempt:1"},
    Phase.VERIFIED: {"artifact_revision": "rev:1", "acceptance_digest": "checks:1"},
    Phase.DELIVERING: {"delivery_ref": "obligation:1"},
    Phase.DELIVERED: {"message_id": "msg:1", "channel_id": "channel:1",
                      "content_digest": "sha256:1"},
}


def advance(registry, task, phase):
    run_id = registry.submit(task).run_id
    if phase in FAILURES:
        registry.record_phase(run_id, phase)
    else:
        for step in PATH[1:PATH.index(phase) + 1]:
            registry.record_phase(run_id, step, evidence=EVIDENCE.get(step))
    return run_id


@pytest.mark.parametrize("source", list(Phase))
@pytest.mark.parametrize("target", list(Phase))
def test_every_edge(registry, task, source, target):
    run_id = advance(registry, task, source)
    valid = source == target or (
        source not in FAILURES | {Phase.DELIVERED} and (
            target in FAILURES or
            (source in PATH[:-1] and target == PATH[PATH.index(source) + 1]) or
            (source == Phase.DELIVERING and target == Phase.DELIVERED) or
            (source == Phase.DELIVERY_UNCONFIRMED and target == Phase.DELIVERING)))
    evidence = EVIDENCE.get(target)
    if target == source == Phase.ACCEPTED:
        from dataclasses import asdict
        evidence = {"contract": asdict(task)}
    if valid:
        assert registry.record_phase(run_id, target, evidence=evidence) is target
        assert registry.lookup(run_id).phase is target
    else:
        with pytest.raises(LifecycleError):
            registry.record_phase(run_id, target, evidence=evidence)
        assert registry.lookup(run_id).phase is source
    assert is_complete(source) is (source == Phase.DELIVERED)


def test_parent_accepted_direct_to_delivered(registry, task):
    run_id = registry.submit(task).run_id
    with pytest.raises(LifecycleError):
        registry.record_phase(run_id, Phase.DELIVERED)
    assert registry.lookup(run_id).phase is Phase.ACCEPTED


@pytest.mark.parametrize("phase", list(EVIDENCE))
@pytest.mark.parametrize("bad", [None, {}, "text", [], True])
def test_guarded_phase_requires_structured_evidence(registry, task, phase, bad):
    run_id = advance(registry, task, PATH[PATH.index(phase) - 1])
    with pytest.raises(LifecycleError):
        registry.record_phase(run_id, phase, evidence=bad)


@pytest.mark.parametrize("phase,key", [(p, k) for p, e in EVIDENCE.items() for k in e])
@pytest.mark.parametrize("bad", [None, "", " ", True, [], {}, 1.5])
def test_each_evidence_field_is_validated(registry, task, phase, key, bad):
    if key == "started_at" and bad == 1.5:
        return  # Positive finite timestamps may be fractional.
    run_id = advance(registry, task, PATH[PATH.index(phase) - 1])
    evidence = {**EVIDENCE[phase], key: bad}
    with pytest.raises(LifecycleError):
        registry.record_phase(run_id, phase, evidence=evidence)


@pytest.mark.parametrize("phase", list(Phase))
def test_same_phase_replay_does_not_append_and_conflicts(registry, task, phase):
    from dataclasses import asdict
    run_id = advance(registry, task, phase)
    evidence = {"contract": asdict(task)} if phase == Phase.ACCEPTED else EVIDENCE.get(phase)
    count = registry._conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0]
    registry.record_phase(run_id, phase, evidence=evidence)
    assert registry._conn.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0] == count
    with pytest.raises(LifecycleError):
        registry.record_phase(run_id, phase, evidence={**(evidence or {}), "changed": True})


def test_reconstruction_preserves_all_milestone_evidence(registry, task):
    run_id = advance(registry, task, Phase.DELIVERED)
    run = registry.lookup(run_id)
    for phase, evidence in EVIDENCE.items():
        assert run.evidence[phase] == evidence


@pytest.mark.parametrize("phase,encoded", [
    ("invented", "null"), ("delivered", "null"), ("running", "{}"),
    ("ready", "not json"), ("ready", '"text"'),
])
def test_malformed_stored_event_is_rejected(registry, task, phase, encoded):
    run_id = registry.submit(task).run_id
    with registry._conn:
        registry._conn.execute(
            "INSERT INTO lifecycle_events(run_id, phase, evidence, recorded_at) VALUES(?, ?, ?, 1)",
            (run_id, phase, encoded))
    with pytest.raises(LifecycleError):
        registry.lookup(run_id)


@pytest.mark.parametrize("phase,key", [(p, k) for p, e in EVIDENCE.items() for k in e])
def test_replay_validates_guarded_fields(registry, task, phase, key):
    run_id = advance(registry, task, PATH[PATH.index(phase) - 1])
    with registry._conn:
        registry._conn.execute(
            "INSERT INTO lifecycle_events(run_id, phase, evidence, recorded_at) VALUES(?, ?, ?, 1)",
            (run_id, phase.value, json.dumps({**EVIDENCE[phase], key: None})))
    with pytest.raises(LifecycleError):
        registry.lookup(run_id)


@pytest.mark.parametrize("encoded", ['{"note": NaN}', '{"note": 1, "note": 2}'])
def test_replay_rejects_ambiguous_json(registry, task, encoded):
    run_id = registry.submit(task).run_id
    with registry._conn:
        registry._conn.execute(
            "INSERT INTO lifecycle_events(run_id, phase, evidence, recorded_at) VALUES(?, 'ready', ?, 1)",
            (run_id, encoded))
    with pytest.raises(LifecycleError):
        registry.lookup(run_id)


def test_replay_must_not_rebind_stored_paths(registry, task, tmp_path):
    from dataclasses import asdict
    # Simulate a crash after reserve with an invalid, noncanonical accepted event.
    values = asdict(task)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    values["workdir"] = str(alias)
    registry._store.reserve(registry.SCOPE, task.idempotency_key(), task.digest(), "raw-run",
                            {"phase": "accepted", "contract": asdict(task)})
    with registry._conn:
        registry._conn.execute(
            "INSERT INTO lifecycle_events(run_id, phase, evidence, recorded_at) VALUES('raw-run', 'accepted', ?, 1)",
            (json.dumps({"contract": values}),))
    with pytest.raises(LifecycleError):
        registry.lookup("raw-run")


def test_delivery_retry_retains_both_attempts(registry, task):
    run_id = advance(registry, task, Phase.DELIVERY_UNCONFIRMED)
    second = {"delivery_ref": "obligation:retry"}
    registry.record_phase(run_id, Phase.DELIVERING, evidence=second)
    registry.record_phase(run_id, Phase.DELIVERED, evidence=EVIDENCE[Phase.DELIVERED])
    run = registry.lookup(run_id)
    assert run.evidence[Phase.DELIVERING] == second
    assert run.evidence[Phase.VERIFIED] == EVIDENCE[Phase.VERIFIED]
    assert [json.loads(row[0]) for row in registry._conn.execute(
        "SELECT evidence FROM lifecycle_events WHERE run_id=? AND phase='delivering' ORDER BY id",
        (run_id,))] == [EVIDENCE[Phase.DELIVERING], second]
