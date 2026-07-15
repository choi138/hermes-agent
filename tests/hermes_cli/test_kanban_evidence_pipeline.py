from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _claimed(conn: sqlite3.Connection) -> tuple[str, int, str]:
    task_id = kb.create_task(conn, title="evidence pipeline", assignee="worker")
    task = kb.claim_task(conn, task_id, claimer="worker:1")
    assert task is not None and task.current_run_id is not None and task.claim_lock
    return task_id, task.current_run_id, task.claim_lock


def _manifest(task_id: str, run_id: int, intent_id: str, action: str = "complete") -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": run_id,
        "terminal_intent_id": intent_id,
        "action": action,
        "block_kind": "capability" if action == "block" else None,
        # Git object IDs may be SHA-1 or SHA-256; the manifest itself is
        # always bound by a SHA-256 digest.
        "source_commit": hashlib.sha1(b"commit").hexdigest(),
        "source_tree": hashlib.sha1(b"tree").hexdigest(),
        "config_digest": _digest("config"),
        "lockfile_digest": _digest("lock"),
        "toolchain_digest": _digest("toolchain"),
        "backend_kind": "ssh",
        "backend_digest": _digest("backend"),
        "command_digest": _digest("command"),
        "test_plan_digest": _digest("plan"),
        "fixture_digest": _digest("fixture"),
        "seed_digest": _digest("seed"),
        "policy_version": "evidence-v1",
        "evidence_at": int(time.time()),
        "freshness_seconds": 3600,
        "failure_class": "none",
        "checkpoint_digest": _digest("checkpoint"),
        "side_effect": "none",
    }


def test_manifest_is_canonical_bound_and_fail_closed():
    manifest = _manifest("t_12345678", 7, "ti_1234567890abcdef")
    canonical = kb.canonical_evidence_manifest(manifest)
    assert canonical == json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    digest = kb.evidence_manifest_digest(manifest)
    assert digest == hashlib.sha256(canonical.encode()).hexdigest()
    kb.validate_evidence_manifest(
        manifest,
        digest=digest,
        task_id="t_12345678",
        run_id=7,
        terminal_intent_id="ti_1234567890abcdef",
        action="complete",
        now=manifest["evidence_at"] + 100,
    )

    for mutation in (
        {**manifest, "task_id": "t_87654321"},
        {**manifest, "raw_output": "must never persist"},
        {**manifest, "config_digest": "not-a-digest"},
        {**manifest, "backend_kind": "ssh\nAuthorization: secret"},
    ):
        with pytest.raises(ValueError):
            kb.validate_evidence_manifest(
                mutation,
                digest=digest,
                task_id="t_12345678",
                run_id=7,
                terminal_intent_id="ti_1234567890abcdef",
                action="complete",
                now=manifest["evidence_at"] + 100,
            )


def test_terminal_intent_applies_once_with_exact_run_marker_and_replay(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_1234567890abcdef"
    manifest = _manifest(task_id, run_id, intent_id)
    digest = kb.evidence_manifest_digest(manifest)

    kb.create_terminal_intent(
        conn,
        terminal_intent_id=intent_id,
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        action="complete",
        decision="verified",
        failure_class="none",
        manifest=manifest,
        provenance_digest=digest,
    )
    assert kb.apply_terminal_intent(conn, intent_id, summary="verified completion")
    assert kb.apply_terminal_intent(conn, intent_id, summary="ignored duplicate")

    row = conn.execute(
        "SELECT status, applied_event_id, acknowledged_at FROM terminal_intents WHERE terminal_intent_id=?",
        (intent_id,),
    ).fetchone()
    event = conn.execute("SELECT task_id, run_id, kind, payload FROM task_events WHERE id=?", (row["applied_event_id"],)).fetchone()
    run = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "acknowledged" and row["acknowledged_at"] is not None
    assert (event["task_id"], event["run_id"], event["kind"]) == (task_id, run_id, "completed")
    assert json.loads(event["payload"])["terminal_intent_id"] == intent_id
    assert run["outcome"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == 1


def test_two_connections_racing_apply_converge_on_one_event(conn, monkeypatch):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_9999000011112222"
    manifest = _manifest(task_id, run_id, intent_id)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)
    original_validate = kb.validate_evidence_manifest

    def synchronized_validate(*args, **kwargs):
        original_validate(*args, **kwargs)
        barrier.wait(timeout=5)

    monkeypatch.setattr(kb, "validate_evidence_manifest", synchronized_validate)
    outcomes: list[object] = []

    def worker():
        other = kb.connect(db_path)
        try:
            outcomes.append(kb.apply_terminal_intent(other, intent_id))
        except Exception as exc:  # captured for the assertion below
            outcomes.append(exc)
        finally:
            other.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert outcomes == [True, True]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)
    ).fetchone()[0] == 1


def test_crash_after_lifecycle_commit_replays_postcommit_once(conn, monkeypatch):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_7777888899990000"
    manifest = _manifest(task_id, run_id, intent_id)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    original_finish = kb._finish_terminal_postcommit

    def crash_before_ack(*args, **kwargs):
        raise RuntimeError("simulated crash after lifecycle commit")

    monkeypatch.setattr(kb, "_finish_terminal_postcommit", crash_before_ack)
    with pytest.raises(RuntimeError, match="simulated crash"):
        kb.apply_terminal_intent(conn, intent_id)
    assert conn.execute(
        "SELECT status FROM terminal_intents WHERE terminal_intent_id=?", (intent_id,)
    ).fetchone()[0] == "applied"
    assert conn.execute(
        "SELECT status FROM terminal_postcommit WHERE terminal_intent_id=?", (intent_id,)
    ).fetchone()[0] == "pending"

    monkeypatch.setattr(kb, "_finish_terminal_postcommit", original_finish)
    assert kb.apply_terminal_intent(conn, intent_id)
    assert conn.execute(
        "SELECT status FROM terminal_postcommit WHERE terminal_intent_id=?", (intent_id,)
    ).fetchone()[0] == "done"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)
    ).fetchone()[0] == 1


def test_stale_intent_cannot_claim_unrelated_terminal_state(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_abcdef1234567890"
    manifest = _manifest(task_id, run_id, intent_id)
    kb.create_terminal_intent(
        conn,
        terminal_intent_id=intent_id,
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        action="complete",
        decision="verified",
        failure_class="none",
        manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    assert kb.complete_task(conn, task_id, expected_run_id=run_id)
    with pytest.raises(kb.TerminalIntentConflict):
        kb.apply_terminal_intent(conn, intent_id)
    assert conn.execute("SELECT status FROM terminal_intents WHERE terminal_intent_id=?", (intent_id,)).fetchone()[0] == "pending"


def test_terminal_intent_registration_requires_exact_active_run_ownership(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_3131313131313131"
    manifest = _manifest(task_id, run_id, intent_id)
    kwargs = dict(
        terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    with pytest.raises(ValueError, match="decision"):
        kb.create_terminal_intent(conn, **{**kwargs, "decision": "secret-shaped-arbitrary-value"})
    with pytest.raises(kb.TerminalIntentConflict, match="active run"):
        kb.create_terminal_intent(conn, **{**kwargs, "claim_lock": "foreign:999"})
    assert conn.execute(
        "SELECT COUNT(*) FROM terminal_intents WHERE terminal_intent_id=?",
        (intent_id,),
    ).fetchone()[0] == 0


def test_block_intent_and_concurrent_duplicate_insert_are_idempotent(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_1111222233334444"
    manifest = _manifest(task_id, run_id, intent_id, action="block")
    manifest["failure_class"] = "credential"
    kwargs = dict(
        terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="block", decision="stable_block",
        failure_class="credential", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    kb.create_terminal_intent(conn, **kwargs)
    kb.create_terminal_intent(conn, **kwargs)
    assert kb.apply_terminal_intent(conn, intent_id, block_kind="capability")
    assert kb.get_task(conn, task_id).status == "blocked"
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (task_id,)).fetchone()
    assert json.loads(event[0])["terminal_intent_id"] == intent_id


def test_typed_recovery_policy_requires_verified_safe_checkpoint():
    assert kb.recovery_decision("network", verified_checkpoint=True, digest_matches=True, side_effect="none") == "resume"
    assert kb.recovery_decision("network", verified_checkpoint=False, digest_matches=True, side_effect="none") == "fresh"
    assert kb.recovery_decision("assertion", verified_checkpoint=True, digest_matches=True, side_effect="none") == "no_retry"
    assert kb.recovery_decision("credential", verified_checkpoint=True, digest_matches=True, side_effect="none") == "stable_block"
    assert kb.recovery_decision("worker", verified_checkpoint=True, digest_matches=False, side_effect="none") == "human_gate"
    assert kb.recovery_decision("provider", verified_checkpoint=True, digest_matches=True, side_effect="unknown") == "human_gate"


def test_correction_lineage_single_flight_uses_full_identity(conn):
    args = dict(root_cause_id="rc_12345678", affected_scope_digest=_digest("scope"), policy_or_test_plan_version="v1", independent_variant="primary")
    leader = kb.acquire_correction_lineage(conn, owner_task_id="t_aaaaaaaa", **args)
    follower = kb.acquire_correction_lineage(conn, owner_task_id="t_bbbbbbbb", **args)
    other = kb.acquire_correction_lineage(conn, owner_task_id="t_cccccccc", **{**args, "independent_variant": "qa-contradiction"})
    assert leader["role"] == "leader"
    assert follower == {"role": "follower", "leader_task_id": "t_aaaaaaaa", "lineage_id": leader["lineage_id"]}
    assert other["role"] == "leader" and other["lineage_id"] != leader["lineage_id"]
    kb.resolve_correction_lineage(conn, leader["lineage_id"], owner_task_id="t_aaaaaaaa")
    replacement = kb.acquire_correction_lineage(conn, owner_task_id="t_bbbbbbbb", **args)
    assert replacement["role"] == "leader" and replacement["lineage_id"] != leader["lineage_id"]


def test_correction_lineage_two_connection_race_has_one_leader(conn):
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []
    args = dict(
        root_cause_id="rc_race0001",
        affected_scope_digest=_digest("race-scope"),
        policy_or_test_plan_version="v1",
        independent_variant="primary",
    )

    def contender(owner: str) -> None:
        other = kb.connect(db_path)
        try:
            barrier.wait(timeout=5)
            results.append(kb.acquire_correction_lineage(
                other, owner_task_id=owner, **args,
            ))
        except BaseException as exc:  # pragma: no cover - thread handoff
            errors.append(exc)
        finally:
            other.close()

    threads = [
        threading.Thread(target=contender, args=("t_11111111",)),
        threading.Thread(target=contender, args=("t_22222222",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert sorted(result["role"] for result in results) == ["follower", "leader"]
    leader = next(result for result in results if result["role"] == "leader")
    follower = next(result for result in results if result["role"] == "follower")
    assert follower["leader_task_id"] == leader["leader_task_id"]
    assert follower["lineage_id"] == leader["lineage_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM correction_lineages WHERE root_cause_id=? AND status='active'",
        (args["root_cause_id"],),
    ).fetchone()[0] == 1


def test_shadow_verification_never_skips_and_rejects_high_risk():
    eligible = kb.evaluate_shadow_verification(
        provenance_verified=True, risk="low", verdict_source="deterministic_test",
        external_side_effect=False, stale=False, flaky=False,
    )
    assert eligible == {"mode": "shadow", "verification_skipped": False, "eligible": True, "reason": "eligible_shadow_observation"}
    blocked = kb.evaluate_shadow_verification(
        provenance_verified=True, risk="high", verdict_source="reviewer",
        external_side_effect=True, stale=False, flaky=False,
    )
    assert blocked["mode"] == "shadow" and blocked["verification_skipped"] is False
    assert blocked["eligible"] is False and blocked["reason"] == "reviewer_verdict"


def test_manifest_action_and_failure_class_must_agree():
    complete = _manifest("t_12345678", 7, "ti_1234567890abcdef")
    complete["failure_class"] = "credential"
    with pytest.raises(ValueError, match="complete evidence must have failure_class=none"):
        kb.evidence_manifest_digest(complete)

    blocked = _manifest("t_12345678", 7, "ti_1234567890abcdef", action="block")
    with pytest.raises(ValueError, match="block evidence requires a failure class"):
        kb.evidence_manifest_digest(blocked)

    malformed_source = _manifest("t_12345678", 7, "ti_1234567890abcdef")
    malformed_source["source_tree"] = "not-a-git-object"
    with pytest.raises(ValueError, match="source_tree"):
        kb.evidence_manifest_digest(malformed_source)


def test_terminal_intent_id_is_immutable_and_event_tampering_fails(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_2222333344445555"
    manifest = _manifest(task_id, run_id, intent_id)
    kwargs = dict(
        terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    kb.create_terminal_intent(conn, **kwargs)
    with pytest.raises(kb.TerminalIntentConflict):
        kb.create_terminal_intent(conn, **{**kwargs, "decision": "fresh"})
    kb.apply_terminal_intent(conn, intent_id)
    event_id = conn.execute(
        "SELECT applied_event_id FROM terminal_intents WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()[0]
    conn.execute("UPDATE task_events SET payload='{}' WHERE id=?", (event_id,))
    conn.commit()
    with pytest.raises(kb.TerminalIntentConflict, match="another intent"):
        kb.apply_terminal_intent(conn, intent_id)


def test_applied_event_provenance_tampering_fails_closed(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_1212121212121212"
    manifest = _manifest(task_id, run_id, intent_id)
    digest = kb.evidence_manifest_digest(manifest)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest, provenance_digest=digest,
    )
    kb.apply_terminal_intent(conn, intent_id)
    event_id = conn.execute(
        "SELECT applied_event_id FROM terminal_intents WHERE terminal_intent_id=?",
        (intent_id,),
    ).fetchone()[0]
    payload = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE id=?", (event_id,),
    ).fetchone()[0])
    payload["provenance_digest"] = _digest("foreign evidence")
    conn.execute(
        "UPDATE task_events SET payload=? WHERE id=?",
        (json.dumps(payload), event_id),
    )
    conn.commit()
    with pytest.raises(kb.TerminalIntentConflict, match="provenance"):
        kb.apply_terminal_intent(conn, intent_id)


def test_crash_scan_replays_pending_terminal_intent_before_requeue(conn, monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    task_id = kb.create_task(conn, title="pending terminal", assignee="worker")
    task = kb.claim_task(conn, task_id, claimer=f"{host}:pending")
    assert task is not None and task.current_run_id is not None and task.claim_lock
    fake_pid = 987654
    kb._set_worker_pid(conn, task_id, fake_pid)
    intent_id = "ti_5656565656565656"
    manifest = _manifest(task_id, task.current_run_id, intent_id)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id,
        run_id=task.current_run_id, claim_lock=task.claim_lock,
        action="complete", decision="verified", failure_class="none",
        manifest=manifest, provenance_digest=kb.evidence_manifest_digest(manifest),
    )

    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb._record_worker_exit(fake_pid, 0)
    assert kb.detect_crashed_workers(conn) == []
    assert kb.get_task(conn, task_id).status == "done"
    intent = conn.execute(
        "SELECT status FROM terminal_intents WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()
    assert intent["status"] == "acknowledged"
    kinds = [row["kind"] for row in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,),
    ).fetchall()]
    assert "completed" in kinds
    assert "protocol_violation" not in kinds


def test_pending_postcommit_scan_recovers_crash_gap(conn, monkeypatch):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_6666777788889999"
    manifest = _manifest(task_id, run_id, intent_id)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    original_finish = kb._finish_terminal_postcommit
    monkeypatch.setattr(kb, "_finish_terminal_postcommit", lambda *_args, **_kwargs: None)
    kb.apply_terminal_intent(conn, intent_id)
    monkeypatch.setattr(kb, "_finish_terminal_postcommit", original_finish)
    assert kb.replay_terminal_postcommits(conn, limit=10) == [intent_id]
    assert kb.replay_terminal_postcommits(conn, limit=10) == []


def test_recovery_and_shadow_classifiers_fail_closed():
    with pytest.raises(ValueError):
        kb.recovery_decision("typo", verified_checkpoint=True, digest_matches=True, side_effect="none")
    with pytest.raises(ValueError):
        kb.recovery_decision("network", verified_checkpoint=True, digest_matches=True, side_effect="unsafe")
    with pytest.raises(ValueError):
        kb.evaluate_shadow_verification(
            provenance_verified=True, risk="unknown", verdict_source="deterministic_test",
            external_side_effect=False, stale=False, flaky=False,
        )
    with pytest.raises(ValueError):
        kb.evaluate_shadow_verification(
            provenance_verified=True, risk="low", verdict_source="mystery",
            external_side_effect=False, stale=False, flaky=False,
        )

    medium_risk = kb.evaluate_shadow_verification(
        provenance_verified=True, risk="medium",
        verdict_source="deterministic_test", external_side_effect=False,
        stale=False, flaky=False,
    )
    assert medium_risk == {
        "mode": "shadow", "verification_skipped": False,
        "eligible": False, "reason": "risk_not_low",
    }


def test_correction_lineage_concurrent_connections_choose_one_leader(conn):
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []

    def worker(owner: str):
        other = kb.connect(db_path)
        try:
            barrier.wait(timeout=5)
            outcomes.append(kb.acquire_correction_lineage(
                other, root_cause_id="rc_race",
                affected_scope_digest=_digest("race-scope"),
                policy_or_test_plan_version="v1", independent_variant="primary",
                owner_task_id=owner,
            ))
        finally:
            other.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("t_aaaaaaaa", "t_bbbbbbbb")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(item["role"] for item in outcomes) == ["follower", "leader"]
    assert len({item["leader_task_id"] for item in outcomes}) == 1


def test_evidence_summary_is_not_persisted(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_1010101010101010"
    manifest = _manifest(task_id, run_id, intent_id)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    kb.apply_terminal_intent(conn, intent_id, summary="raw-sensitive-marker")
    dump = " ".join(
        str(value) for table in ("terminal_intents", "task_events", "task_runs")
        for row in conn.execute(f"SELECT * FROM {table}") for value in row
    )
    assert "raw-sensitive-marker" not in dump


def test_schema_initialization_is_idempotent_for_new_evidence_tables(tmp_path: Path):
    db_path = tmp_path / "reopen.db"
    with kb.connect(db_path):
        pass
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as reopened:
        tables = {row[0] for row in reopened.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"terminal_intents", "terminal_postcommit", "correction_lineages"} <= tables
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
