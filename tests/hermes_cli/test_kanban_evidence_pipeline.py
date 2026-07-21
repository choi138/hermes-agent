from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

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


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Evidence Test",
            "-c", "user.email=evidence@example.invalid", "commit", "-q",
            "-m", "fixture",
        ],
        check=True,
    )
    return repo


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


def _stage_bound_ledger_intent(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    intent_id: str,
    *,
    evidence_at: int | None = None,
    freshness_seconds: int = 3600,
    side_effect: str = "none",
    prefix: str = "ledger",
) -> tuple[dict, dict]:
    claim_lock = conn.execute(
        "SELECT claim_lock FROM tasks WHERE id=?", (task_id,),
    ).fetchone()[0]
    manifest = _manifest(task_id, run_id, intent_id)
    observed_at = int(time.time()) if evidence_at is None else evidence_at
    manifest.update({
        "command_digest": _digest(f"{prefix}-input"),
        "checkpoint_digest": _digest(f"{prefix}-artifact"),
        "toolchain_digest": _digest(f"{prefix}-toolchain"),
        "test_plan_digest": _digest(f"{prefix}-test-plan"),
        "policy_version": "runtime-evidence-v1",
        "evidence_at": observed_at,
        "freshness_seconds": freshness_seconds,
        "side_effect": side_effect,
    })
    with patch.object(kb, "_record_terminal_run_evidence", return_value=None):
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
    exact = {
        "input_digest": manifest["command_digest"],
        "artifact_digest": manifest["checkpoint_digest"],
        "toolchain_digest": manifest["toolchain_digest"],
        "environment_digest": kb._run_evidence_environment_digest(manifest),
        "test_plan_digest": manifest["test_plan_digest"],
        "policy_version": manifest["policy_version"],
        "reusable_class": "focused_test",
    }
    return manifest, exact


def test_runtime_producer_uses_real_git_provenance_without_leaking_handoff(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Evidence Test",
            "-c", "user.email=evidence@example.invalid",
            "commit", "-q", "-m", "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{commit}"],
        text=True,
    ).strip()
    tree = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
        text=True,
    ).strip()

    evidence = evidence_module.produce_terminal_evidence(
        claim_lock="claim-secret-must-not-persist",
        task_id="t_12345678",
        run_id=1,
        action="complete",
        decision="verified",
        failure_class="none",
        block_kind=None,
        handoff={
            "result": None,
            "summary": "handoff-secret-must-not-persist",
            "metadata": {"tests_run": 3},
            "verified_cards": [],
        },
        workspace=str(repo),
        evidence_at=1_700_000_000,
    )

    assert evidence["manifest"]["source_commit"] == commit
    assert evidence["manifest"]["source_tree"] == tree
    serialized = json.dumps(evidence, sort_keys=True)
    assert "claim-secret-must-not-persist" not in serialized
    assert "handoff-secret-must-not-persist" not in serialized

    real_run = evidence_module.subprocess.run

    def _timeout_status(argv, **kwargs):
        if "status" in argv:
            raise subprocess.TimeoutExpired(argv, timeout=2)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(evidence_module.subprocess, "run", _timeout_status)
    observed_commit, observed_tree, state_digest = evidence_module._git_provenance(
        str(repo), "fallback-seed",
    )
    assert (observed_commit, observed_tree) == (commit, tree)
    assert len(state_digest) == 64


def test_runtime_provenance_hashes_dirty_file_contents_not_only_git_status(tmp_path: Path):
    from hermes_cli import kanban_evidence as evidence_module

    repo = tmp_path / "dirty-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=Evidence Test",
            "-c", "user.email=evidence@example.invalid", "commit", "-q",
            "-m", "fixture",
        ],
        check=True,
    )

    tracked.write_text("dirty-one\n", encoding="utf-8")
    first = evidence_module.produce_terminal_evidence(
        claim_lock="claim-a", task_id="t_12345678", run_id=1,
        action="complete", decision="verified", failure_class="none",
        block_kind=None,
        handoff={"result": None, "summary": "same", "metadata": {}, "verified_cards": []},
        workspace=str(repo), evidence_at=1_700_000_000,
    )["manifest"]
    first_status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain=v1"], text=True,
    )

    tracked.write_text("dirty-two\n", encoding="utf-8")
    second = evidence_module.produce_terminal_evidence(
        claim_lock="claim-b", task_id="t_87654321", run_id=2,
        action="complete", decision="verified", failure_class="none",
        block_kind=None,
        handoff={"result": None, "summary": "same", "metadata": {}, "verified_cards": []},
        workspace=str(repo), evidence_at=1_700_000_001,
    )["manifest"]
    second_status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain=v1"], text=True,
    )

    assert first_status == second_status
    assert first["source_tree"] == second["source_tree"]
    assert first["fixture_digest"] == second["fixture_digest"]
    assert first["seed_digest"] == second["seed_digest"]
    assert first["checkpoint_digest"] != second["checkpoint_digest"]


def test_git_output_budget_kills_producer_before_materializing_payload(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    spawned = []

    class OversizedGitProcess:
        def __init__(self, argv, *, stdout, **kwargs):
            self.argv = argv
            self.stdout = stdout
            self.returncode = None
            self.killed = False
            stdout.write(b"x" * 33)
            stdout.flush()
            spawned.append(self)

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(evidence_module.subprocess, "Popen", OversizedGitProcess)

    with pytest.raises(ValueError, match="exceeds hashing budget"):
        evidence_module._git_bytes(
            tmp_path,
            "status",
            max_bytes=32,
        )

    assert len(spawned) == 1
    assert spawned[0].killed is True


def test_runtime_provenance_fails_closed_when_git_hash_times_out(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = _git_repo(tmp_path, "timeout-repo")

    def _timeout(_root):
        raise subprocess.TimeoutExpired(["git", "status"], timeout=3)

    monkeypatch.setattr(evidence_module, "_git_worktree_content_digest", _timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_git_worktree_has_no_head(
    tmp_path: Path,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = tmp_path / "unborn-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "tracked.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unable to resolve Git workspace provenance"):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_workspace_disappears(
    tmp_path: Path,
):
    from hermes_cli import kanban_evidence as evidence_module

    workspace = tmp_path / "removed-workspace"
    workspace.mkdir()
    workspace.rmdir()

    with pytest.raises(RuntimeError, match="workspace is unavailable"):
        evidence_module._git_provenance(str(workspace), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_git_workspace_is_replaced(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = _git_repo(tmp_path, "replaceable-repo")
    replacement = _git_repo(tmp_path, "replacement-repo")
    moved_repo = tmp_path / "moved-original-repo"
    real_digest = evidence_module._git_worktree_content_digest

    def _digest_then_replace(root: Path) -> str:
        digest = real_digest(root)
        root.rename(moved_repo)
        replacement.rename(root)
        return digest

    monkeypatch.setattr(
        evidence_module,
        "_git_worktree_content_digest",
        _digest_then_replace,
    )

    with pytest.raises(RuntimeError, match="workspace is unavailable"):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_for_dangling_git_metadata(
    tmp_path: Path,
):
    from hermes_cli import kanban_evidence as evidence_module

    workspace = tmp_path / "damaged-repo"
    workspace.mkdir()
    marker = workspace / ".git"
    try:
        marker.symlink_to(tmp_path / "missing-git-metadata", target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(RuntimeError, match="unable to determine Git workspace membership"):
        evidence_module._git_provenance(str(workspace), "stable-intent-seed")


def test_runtime_provenance_fails_closed_for_bare_repository(tmp_path: Path):
    from hermes_cli import kanban_evidence as evidence_module

    repo = tmp_path / "bare-repo.git"
    subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True)

    with pytest.raises(RuntimeError, match="Git workspace is not a worktree"):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_worktree_exceeds_budget(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = _git_repo(tmp_path, "oversized-repo")
    (repo / "tracked.txt").write_text("x" * 4096, encoding="utf-8")
    monkeypatch.setattr(evidence_module, "_MAX_WORKTREE_HASH_BYTES", 64)

    with pytest.raises(ValueError, match="exceeds hashing budget"):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_untracked_file_disappears(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = _git_repo(tmp_path, "disappearing-repo")
    untracked = repo / "untracked.txt"
    untracked.write_text("ephemeral", encoding="utf-8")
    real_lstat = Path.lstat

    def _disappearing_lstat(path: Path):
        if path == untracked:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", _disappearing_lstat)

    with pytest.raises(FileNotFoundError):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_fails_closed_when_untracked_file_is_unreadable(
    tmp_path: Path,
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    repo = _git_repo(tmp_path, "unreadable-repo")
    untracked = repo / "untracked.txt"
    untracked.write_text("private", encoding="utf-8")
    real_open = Path.open

    def _unreadable_open(path: Path, *args, **kwargs):
        if path == untracked:
            raise PermissionError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _unreadable_open)

    with pytest.raises(PermissionError):
        evidence_module._git_provenance(str(repo), "stable-intent-seed")


def test_runtime_provenance_changes_backend_digest(monkeypatch):
    from hermes_cli import kanban_evidence as evidence_module

    common = dict(
        claim_lock="claim", task_id="t_12345678", run_id=1,
        action="complete", decision="verified", failure_class="none",
        block_kind=None,
        handoff={"result": None, "summary": "same", "metadata": {}, "verified_cards": []},
        evidence_at=1_700_000_000,
    )
    monkeypatch.setattr(evidence_module, "_runtime_fingerprint", lambda: {"image": "v1"})
    first = evidence_module.produce_terminal_evidence(**common)["manifest"]
    monkeypatch.setattr(evidence_module, "_runtime_fingerprint", lambda: {"image": "v2"})
    second = evidence_module.produce_terminal_evidence(**common)["manifest"]
    assert first["backend_digest"] != second["backend_digest"]


def test_toolchain_digest_tracks_distribution_versions_and_is_order_independent(
    monkeypatch,
):
    from hermes_cli import kanban_evidence as evidence_module

    class Distribution:
        def __init__(self, name: str, version: str):
            self.metadata = {"Name": name}
            self.version = version

    packages = [Distribution("Py-Test", "8.4.0"), Distribution("Hermes-Agent", "1.0")]
    monkeypatch.setattr(
        evidence_module.importlib.metadata,
        "distributions",
        lambda: packages,
    )
    first = evidence_module._toolchain_digest()

    packages.reverse()
    assert evidence_module._toolchain_digest() == first

    packages[0].version = "1.1"
    assert evidence_module._toolchain_digest() != first


def test_terminal_intent_and_evidence_are_one_transaction(conn, monkeypatch):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_2020202020202020"
    manifest = _manifest(task_id, run_id, intent_id)

    def fail_ledger(*_args, **_kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(kb, "_record_terminal_run_evidence", fail_ledger)
    with pytest.raises(RuntimeError, match="ledger write failed"):
        kb.create_terminal_intent(
            conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
            claim_lock=claim_lock, action="complete", decision="verified",
            failure_class="none", manifest=manifest,
            provenance_digest=kb.evidence_manifest_digest(manifest),
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM terminal_intents WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM run_evidence_ledger WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()[0] == 0


def test_existing_terminal_intent_backfills_missing_evidence(conn, monkeypatch):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_3030303030303030"
    manifest = _manifest(task_id, run_id, intent_id)
    original = kb._record_terminal_run_evidence
    monkeypatch.setattr(kb, "_record_terminal_run_evidence", lambda *_args, **_kwargs: None)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM run_evidence_ledger WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()[0] == 0

    monkeypatch.setattr(kb, "_record_terminal_run_evidence", original)
    kb.create_terminal_intent(
        conn, terminal_intent_id=intent_id, task_id=task_id, run_id=run_id,
        claim_lock=claim_lock, action="complete", decision="verified",
        failure_class="none", manifest=manifest,
        provenance_digest=kb.evidence_manifest_digest(manifest),
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM run_evidence_ledger WHERE terminal_intent_id=?", (intent_id,),
    ).fetchone()[0] == 1


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
    lineage = kb.acquire_correction_lineage(
        conn,
        root_cause_id="terminal-retry",
        affected_scope_digest=_digest("terminal-scope"),
        policy_or_test_plan_version="qa-v2",
        independent_variant="primary",
        owner_task_id=task_id,
    )
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
    payload = json.loads(event["payload"])
    assert payload["terminal_intent_id"] == intent_id
    assert payload["correction_lineages_resolved"] == 1
    assert run["outcome"] == "completed"
    lineage_row = conn.execute(
        "SELECT status, resolved_at FROM correction_lineages WHERE id=?",
        (lineage["lineage_id"],),
    ).fetchone()
    assert lineage_row["status"] == "resolved"
    assert lineage_row["resolved_at"] is not None
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


def test_typed_recovery_stops_repeated_root_causes_and_terminal_omission():
    assert kb.recovery_decision(
        "network",
        verified_checkpoint=True,
        digest_matches=True,
        side_effect="none",
        same_root_observations=2,
    ) == "no_retry"
    assert kb.recovery_decision(
        "terminal_write",
        verified_checkpoint=True,
        digest_matches=True,
        side_effect="none",
        terminal_recovery_attempts=1,
    ) == "no_retry"


def test_correction_lineage_single_flight_uses_full_identity(conn):
    leader_owner = kb.create_task(conn, title="leader owner")
    follower_owner = kb.create_task(conn, title="follower owner")
    variant_owner = kb.create_task(conn, title="variant owner")
    args = dict(root_cause_id="rc_12345678", affected_scope_digest=_digest("scope"), policy_or_test_plan_version="v1", independent_variant="primary")
    leader = kb.acquire_correction_lineage(conn, owner_task_id=leader_owner, **args)
    follower = kb.acquire_correction_lineage(conn, owner_task_id=follower_owner, **args)
    other = kb.acquire_correction_lineage(conn, owner_task_id=variant_owner, **{**args, "independent_variant": "qa-contradiction"})
    assert leader["role"] == "leader"
    assert follower == {"role": "follower", "leader_task_id": leader_owner, "lineage_id": leader["lineage_id"]}
    assert other["role"] == "leader" and other["lineage_id"] != leader["lineage_id"]
    kb.resolve_correction_lineage(conn, leader["lineage_id"], owner_task_id=leader_owner)
    replacement = kb.acquire_correction_lineage(conn, owner_task_id=follower_owner, **args)
    assert replacement["role"] == "leader" and replacement["lineage_id"] != leader["lineage_id"]


def test_correction_lineage_rejects_missing_owner_task(conn):
    with pytest.raises(ValueError, match="owner task must exist"):
        kb.acquire_correction_lineage(
            conn,
            root_cause_id="rc_missing_owner",
            affected_scope_digest=_digest("scope"),
            policy_or_test_plan_version="v1",
            independent_variant="primary",
            owner_task_id="t_dangling",
        )


def test_correction_lineage_identity_is_isolated_by_owner_tenant(conn):
    tenant_a_owner = kb.create_task(conn, title="tenant a", tenant="tenant-a")
    tenant_b_owner = kb.create_task(conn, title="tenant b", tenant="tenant-b")
    args = dict(
        root_cause_id="rc_shared_identity",
        affected_scope_digest=_digest("shared-scope"),
        policy_or_test_plan_version="v1",
        independent_variant="primary",
    )

    tenant_a = kb.acquire_correction_lineage(
        conn, owner_task_id=tenant_a_owner, **args,
    )
    tenant_b = kb.acquire_correction_lineage(
        conn, owner_task_id=tenant_b_owner, **args,
    )

    assert tenant_a["role"] == tenant_b["role"] == "leader"
    assert tenant_a["leader_task_id"] == tenant_a_owner
    assert tenant_b["leader_task_id"] == tenant_b_owner
    assert tenant_a["lineage_id"] != tenant_b["lineage_id"]
    assert kb.active_correction_lineage(
        conn, tenant="tenant-a", **args,
    )["leader_task_id"] == tenant_a_owner
    assert kb.active_correction_lineage(
        conn, tenant="tenant-b", **args,
    )["leader_task_id"] == tenant_b_owner
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT tenant, leader_task_id FROM correction_lineages "
            "WHERE status='active' ORDER BY tenant"
        )
    ] == [("tenant-a", tenant_a_owner), ("tenant-b", tenant_b_owner)]

    kb.resolve_correction_lineage(
        conn,
        tenant_a["lineage_id"],
        owner_task_id=tenant_a_owner,
    )
    assert kb.active_correction_lineage(
        conn, tenant="tenant-a", **args,
    ) is None
    assert kb.active_correction_lineage(
        conn, tenant="tenant-b", **args,
    )["leader_task_id"] == tenant_b_owner

    tenant_a_replacement_owner = kb.create_task(
        conn, title="tenant a replacement", tenant="tenant-a",
    )
    tenant_a_replacement = kb.acquire_correction_lineage(
        conn, owner_task_id=tenant_a_replacement_owner, **args,
    )
    assert tenant_a_replacement["role"] == "leader"
    assert tenant_a_replacement["lineage_id"] != tenant_a["lineage_id"]

    assert kb.delete_task(conn, tenant_a_replacement_owner) is True
    assert kb.active_correction_lineage(
        conn, tenant="tenant-a", **args,
    ) is None
    assert kb.active_correction_lineage(
        conn, tenant="tenant-b", **args,
    )["leader_task_id"] == tenant_b_owner


def test_correction_lineage_cross_tenant_race_keeps_independent_leaders(conn):
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    errors: list[BaseException] = []
    args = dict(
        root_cause_id="rc_cross_tenant_race",
        affected_scope_digest=_digest("cross-tenant-race"),
        policy_or_test_plan_version="v1",
        independent_variant="primary",
    )
    owners = {
        tenant: kb.create_task(
            conn, title=f"{tenant} owner", tenant=tenant,
        )
        for tenant in ("tenant-a", "tenant-b")
    }

    def contender(tenant: str) -> None:
        other = kb.connect(db_path)
        try:
            barrier.wait(timeout=5)
            results[tenant] = kb.acquire_correction_lineage(
                other,
                owner_task_id=owners[tenant],
                **args,
            )
        except BaseException as exc:  # pragma: no cover - thread handoff
            errors.append(exc)
        finally:
            other.close()

    threads = [
        threading.Thread(target=contender, args=(tenant,))
        for tenant in owners
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert set(results) == set(owners)
    assert all(result["role"] == "leader" for result in results.values())
    assert {
        tenant: result["leader_task_id"]
        for tenant, result in results.items()
    } == owners
    assert len({result["lineage_id"] for result in results.values()}) == 2
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT tenant, leader_task_id FROM correction_lineages "
            "WHERE root_cause_id=? AND status='active' ORDER BY tenant",
            (args["root_cause_id"],),
        )
    ] == [("tenant-a", owners["tenant-a"]), ("tenant-b", owners["tenant-b"])]


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

    owners = [
        kb.create_task(conn, title="race owner 1"),
        kb.create_task(conn, title="race owner 2"),
    ]
    threads = [
        threading.Thread(target=contender, args=(owner,))
        for owner in owners
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


def test_tampered_producer_attestation_cannot_commit_terminal_transition(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_2323232323232323"
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
    conn.execute(
        "UPDATE terminal_intents SET producer_attestation=? "
        "WHERE terminal_intent_id=?",
        ("0" * 64, intent_id),
    )
    conn.commit()

    with pytest.raises(kb.TerminalIntentConflict, match="producer attestation"):
        kb.apply_terminal_intent(conn, intent_id)

    assert kb.get_task(conn, task_id).status == "running"
    intent = conn.execute(
        "SELECT status, applied_event_id FROM terminal_intents "
        "WHERE terminal_intent_id=?",
        (intent_id,),
    ).fetchone()
    assert dict(intent) == {"status": "pending", "applied_event_id": None}
    run = conn.execute(
        "SELECT status, ended_at FROM task_runs WHERE id=?", (run_id,),
    ).fetchone()
    assert dict(run) == {"status": "running", "ended_at": None}
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? "
        "AND kind='completed'",
        (task_id,),
    ).fetchone()[0] == 0


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

    owners = [
        kb.create_task(conn, title="concurrent owner A"),
        kb.create_task(conn, title="concurrent owner B"),
    ]
    threads = [
        threading.Thread(target=worker, args=(owner,))
        for owner in owners
    ]
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
        assert {
            "terminal_intents",
            "terminal_postcommit",
            "correction_lineages",
            "task_failure_accounting",
        } <= tables
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_run_evidence_ledger_round_trips_exact_provenance(conn):
    task_id, run_id, _claim_lock = _claimed(conn)
    intent_id = "ti_9090909090909090"
    manifest, exact = _stage_bound_ledger_intent(
        conn, task_id, run_id, intent_id,
    )
    evidence_id = kb.record_run_evidence(
        conn,
        task_id=task_id,
        run_id=run_id,
        attempt=1,
        failure_class="none",
        evidence_at=manifest["evidence_at"],
        terminal_intent_id=intent_id,
        outcome="passed",
        checkpoint_kind="tests_passed",
        checkpoint_digest=_digest("checkpoint"),
        side_effect="none",
        **exact,
    )

    row = kb.get_run_evidence(conn, evidence_id)
    assert row["task_id"] == task_id
    assert row["run_id"] == run_id
    assert row["artifact_digest"] == manifest["checkpoint_digest"]
    assert row["checkpoint_kind"] == "tests_passed"
    assert row["attempt"] == 1

    with pytest.raises(ValueError, match="existing terminal intent"):
        kb.record_run_evidence(
            conn,
            task_id=task_id,
            run_id=run_id,
            attempt=2,
            failure_class="none",
            evidence_at=manifest["evidence_at"],
            terminal_intent_id="ti_0000000000000000",
            outcome="passed",
            checkpoint_kind="tests_passed",
            checkpoint_digest=_digest("checkpoint-2"),
            side_effect="none",
            **exact,
        )


def test_run_evidence_conflicting_replay_is_rejected(conn):
    task_id, run_id, _claim_lock = _claimed(conn)
    intent_id = "ti_9090909090909091"
    manifest, exact = _stage_bound_ledger_intent(
        conn, task_id, run_id, intent_id, prefix="replay-conflict",
    )
    common = {
        "task_id": task_id,
        "run_id": run_id,
        "attempt": 1,
        "failure_class": "none",
        "evidence_at": manifest["evidence_at"],
        "terminal_intent_id": intent_id,
        "outcome": "passed",
        "side_effect": "none",
        **exact,
    }
    evidence_id = kb.record_run_evidence(
        conn,
        checkpoint_kind="tests_passed",
        checkpoint_digest=_digest("replay-checkpoint-a"),
        **common,
    )

    with pytest.raises(ValueError, match="idempotency conflict"):
        kb.record_run_evidence(
            conn,
            checkpoint_kind="build_passed",
            checkpoint_digest=_digest("replay-checkpoint-b"),
            **common,
        )

    stored = kb.get_run_evidence(conn, evidence_id)
    assert stored["checkpoint_kind"] == "tests_passed"
    assert stored["checkpoint_digest"] == _digest("replay-checkpoint-a")


def test_exact_evidence_reuse_requires_full_match_and_no_external_side_effect(conn):
    task_id, run_id, _claim_lock = _claimed(conn)
    intent_id = "ti_9191919191919191"
    manifest, exact = _stage_bound_ledger_intent(
        conn, task_id, run_id, intent_id, prefix="exact",
    )
    kb.record_run_evidence(
        conn, task_id=task_id, run_id=run_id, attempt=1,
        failure_class="none", evidence_at=manifest["evidence_at"],
        terminal_intent_id=intent_id, outcome="passed",
        checkpoint_kind="tests_passed", checkpoint_digest=_digest("checkpoint"),
        side_effect="none", **exact,
    )

    assert kb.find_exact_reusable_evidence(
        conn, task_id=task_id, now=manifest["evidence_at"] + 100, **exact,
    )["hit"] is True
    assert kb.find_exact_reusable_evidence(
        conn,
        task_id=task_id,
        now=manifest["evidence_at"] + 100,
        exclude_terminal_intent_id=intent_id,
        **exact,
    ) == {"hit": False, "reason": "provenance_mismatch"}
    mismatch = kb.find_exact_reusable_evidence(
        conn, task_id=task_id, now=manifest["evidence_at"] + 100,
        **{**exact, "artifact_digest": _digest("changed")},
    )
    assert mismatch == {"hit": False, "reason": "provenance_mismatch"}
    stale = kb.find_exact_reusable_evidence(
        conn, task_id=task_id,
        now=manifest["evidence_at"] + manifest["freshness_seconds"] + 1,
        **exact,
    )
    assert stale == {"hit": False, "reason": "stale_evidence"}

    external_id = "ti_9292929292929292"
    external_manifest, external_exact = _stage_bound_ledger_intent(
        conn, task_id, run_id, external_id,
        evidence_at=manifest["evidence_at"], side_effect="unknown", prefix="external",
    )
    with pytest.raises(ValueError, match="side-effect-free"):
        kb.record_run_evidence(
            conn, task_id=task_id, run_id=run_id, attempt=2,
            failure_class="none", evidence_at=external_manifest["evidence_at"],
            terminal_intent_id=external_id, outcome="passed",
            checkpoint_kind="tests_passed", checkpoint_digest=_digest("checkpoint-2"),
            side_effect="unknown", **external_exact,
        )


def test_exact_evidence_reuse_is_tenant_scoped(conn):
    source_id = kb.create_task(
        conn, title="tenant-a evidence", assignee="worker", tenant="tenant-a",
    )
    source = kb.claim_task(conn, source_id, claimer="worker:tenant-a")
    assert source is not None and source.current_run_id is not None
    intent_id = "ti_9494949494949495"
    manifest, exact = _stage_bound_ledger_intent(
        conn, source_id, source.current_run_id, intent_id, prefix="tenant-scope",
    )
    kb.record_run_evidence(
        conn,
        task_id=source_id,
        run_id=source.current_run_id,
        attempt=1,
        failure_class="none",
        evidence_at=manifest["evidence_at"],
        terminal_intent_id=intent_id,
        outcome="passed",
        checkpoint_kind="tests_passed",
        checkpoint_digest=_digest("tenant-checkpoint"),
        side_effect="none",
        **exact,
    )
    same_tenant_id = kb.create_task(
        conn, title="same tenant consumer", assignee="worker", tenant="tenant-a",
    )
    other_tenant_id = kb.create_task(
        conn, title="other tenant consumer", assignee="worker", tenant="tenant-b",
    )

    assert kb.find_exact_reusable_evidence(
        conn,
        task_id=same_tenant_id,
        now=manifest["evidence_at"] + 1,
        **exact,
    )["hit"] is True
    assert kb.find_exact_reusable_evidence(
        conn,
        task_id=other_tenant_id,
        now=manifest["evidence_at"] + 1,
        **exact,
    ) == {"hit": False, "reason": "provenance_mismatch"}


def test_no_hit_shadow_audit_is_task_owned_and_hard_deleted(conn):
    task_id = kb.create_task(
        conn, title="owned shadow audit", assignee="worker", tenant="tenant-a",
    )
    exact = {
        "input_digest": _digest("owned-input"),
        "artifact_digest": _digest("owned-artifact"),
        "toolchain_digest": _digest("owned-toolchain"),
        "environment_digest": _digest("owned-environment"),
        "test_plan_digest": _digest("owned-plan"),
        "policy_version": "evidence-v1",
        "reusable_class": "focused_test",
    }
    audit = kb.record_shadow_evidence_decision(
        conn,
        task_id=task_id,
        terminal_intent_id=None,
        risk="low",
        verdict_source="deterministic_test",
        external_side_effect=False,
        stale=False,
        flaky=False,
        observed_at=int(time.time()),
        **exact,
    )
    row = conn.execute(
        "SELECT task_id, terminal_intent_id, candidate_hit "
        "FROM shadow_evidence_audit WHERE id=?",
        (audit["audit_id"],),
    ).fetchone()
    assert dict(row) == {
        "task_id": task_id,
        "terminal_intent_id": None,
        "candidate_hit": 0,
    }

    assert kb.delete_task(conn, task_id) is True
    assert conn.execute(
        "SELECT COUNT(*) FROM shadow_evidence_audit WHERE id=?",
        (audit["audit_id"],),
    ).fetchone()[0] == 0


def test_shadow_reuse_persists_deterministic_audit_without_skipping(conn):
    task_id, run_id, _claim_lock = _claimed(conn)
    intent_id = "ti_9393939393939393"
    manifest, exact = _stage_bound_ledger_intent(
        conn, task_id, run_id, intent_id, prefix="shadow",
    )
    kb.record_run_evidence(
        conn, task_id=task_id, run_id=run_id, attempt=1,
        failure_class="none", evidence_at=manifest["evidence_at"],
        terminal_intent_id=intent_id, outcome="passed",
        checkpoint_kind="tests_passed", checkpoint_digest=_digest("shadow-checkpoint"),
        side_effect="none", **exact,
    )

    first = kb.record_shadow_evidence_decision(
        conn, task_id=task_id, terminal_intent_id=intent_id,
        risk="low", verdict_source="deterministic_test",
        external_side_effect=False, stale=False, flaky=False,
        observed_at=manifest["evidence_at"] + 100, **exact,
    )
    second = kb.record_shadow_evidence_decision(
        conn, task_id=task_id, terminal_intent_id=intent_id,
        risk="low", verdict_source="deterministic_test",
        external_side_effect=False, stale=False, flaky=False,
        observed_at=manifest["evidence_at"] + 100, **exact,
    )
    assert first["candidate_hit"] is True
    assert first["verification_skipped"] is False
    assert second == first

    prohibited = kb.record_shadow_evidence_decision(
        conn, task_id=task_id, terminal_intent_id=intent_id,
        risk="high", verdict_source="reviewer",
        external_side_effect=True, stale=False, flaky=False,
        observed_at=manifest["evidence_at"] + 101,
        **{**exact, "reusable_class": "prohibited"},
    )
    assert prohibited["candidate_hit"] is False
    assert prohibited["verification_skipped"] is False
    assert prohibited["reason"] == "prohibited_class"


def test_terminal_intent_records_provenance_and_shadow_audit(conn):
    task_id, run_id, claim_lock = _claimed(conn)
    intent_id = "ti_9494949494949494"
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
        handoff={
            "result": None,
            "summary": "focused checks passed",
            "metadata": {"verification_class": "focused_test"},
            "verified_cards": [],
        },
    )

    ledger = conn.execute(
        "SELECT * FROM run_evidence_ledger WHERE terminal_intent_id=?",
        (intent_id,),
    ).fetchone()
    audit = conn.execute(
        "SELECT * FROM shadow_evidence_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ledger is not None and ledger["reusable_class"] == "focused_test"
    assert audit is not None and audit["verification_skipped"] == 0
    assert audit["candidate_hit"] == 0
