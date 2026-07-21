"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import pytest


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_heartbeat_schema_guides_meaningful_notes_without_changing_silent_liveness():
    from tools import kanban_tools as kt

    description = kt.KANBAN_HEARTBEAT_SCHEMA["description"]
    note_description = kt.KANBAN_HEARTBEAT_SCHEMA["parameters"]["properties"]["note"][
        "description"
    ]

    assert "2–3 concise fields" in description
    assert "user's language" in description
    assert "current stage" in note_description
    assert "verified evidence/result" in note_description
    assert "next action" in note_description
    assert "Empty automatic heartbeats remain silent" in description


def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


def test_kanban_tools_visible_with_env_var(monkeypatch, tmp_path):
    """Worker sessions get task lifecycle tools, not board-routing tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_kanban_worker_env_overrides_profile_toolset_filter(monkeypatch, tmp_path):
    """Dispatcher-spawned workers must get lifecycle tools even when the
    assignee profile restricts enabled toolsets and does not list kanban.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    schema = get_tool_definitions(
        enabled_toolsets=["terminal"],
        quiet_mode=True,
    )
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_show" in names
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_list" not in names


def test_worker_with_kanban_toolset_still_hides_board_routing(monkeypatch, tmp_path):
    """Task scope wins over profile config for board-routing tools.

    Even if a worker process happens to also have ``toolsets: [kanban]``
    in its config, the HERMES_KANBAN_TASK env var means it's a focused
    worker and must not see kanban_list / kanban_unblock.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert {
        "kanban_list",
        "kanban_unblock",
    }.isdisjoint(kanban), (
        f"Board-routing tools leaked into worker schema: "
        f"{kanban & {'kanban_list', 'kanban_unblock'}}"
    )


def test_kanban_tools_visible_with_toolset_config(monkeypatch, tmp_path):
    """Orchestrator profiles with toolsets: [kanban] see all kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_list",
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_unblock",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.current_run_id is not None
        assert task.claim_lock
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    return tid


def _terminal_evidence(
    task,
    *,
    intent_id: str,
    action: str,
    block_kind: str | None,
    failure_class: str,
    decision: str,
) -> dict:
    from hermes_cli import kanban_db as kb

    assert task.current_run_id is not None

    def digest(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()

    manifest = {
        "schema_version": 1,
        "task_id": task.id,
        "run_id": task.current_run_id,
        "terminal_intent_id": intent_id,
        "action": action,
        "block_kind": block_kind,
        "source_commit": hashlib.sha1(b"commit").hexdigest(),
        "source_tree": hashlib.sha1(b"tree").hexdigest(),
        "config_digest": digest("config"),
        "lockfile_digest": digest("lock"),
        "toolchain_digest": digest("toolchain"),
        "backend_kind": "ssh",
        "backend_digest": digest("backend"),
        "command_digest": digest("command"),
        "test_plan_digest": digest("test-plan"),
        "fixture_digest": digest("fixture"),
        "seed_digest": digest("seed"),
        "policy_version": "evidence-v1",
        "evidence_at": int(time.time()),
        "freshness_seconds": 3600,
        "failure_class": failure_class,
        "checkpoint_digest": digest("checkpoint"),
        "side_effect": "none",
    }
    return {
        "terminal_intent_id": intent_id,
        "decision": decision,
        "failure_class": failure_class,
        "manifest": manifest,
        "provenance_digest": kb.evidence_manifest_digest(manifest),
    }


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" not in d
    assert "events" not in d


def test_show_explicit_task_id(worker_env):
    """Peek at a different task than the one in env."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other task", assignee="peer")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_show({"task_id": other})
    d = json.loads(out)
    assert d["task"]["id"] == other


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert d["tasks"][0]["parent_count"] == 0
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_list_rejects_invalid_status(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"status": "not-a-state"})
    assert "status must be one of" in json.loads(out).get("error", "")


def test_list_rejects_bad_limit(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_list({"limit": "nope"})).get("error")
    assert json.loads(kt._handle_list({"limit": 0})).get("error")


def test_list_parses_include_archived_string_false(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "false",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived not in ids


def test_list_parses_include_archived_string_true(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "true",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived in ids


def test_list_rejects_bad_include_archived(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"include_archived": "sometimes"})
    assert "include_archived must be" in json.loads(out).get("error", "")


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    assert d["terminal_intent_id"].startswith("ti_")
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
        intent = conn.execute(
            "SELECT status, producer_attestation FROM terminal_intents "
            "WHERE terminal_intent_id=?",
            (d["terminal_intent_id"],),
        ).fetchone()
        assert intent["status"] == "acknowledged"
        assert len(intent["producer_attestation"]) == 64
    finally:
        conn.close()


def test_complete_with_terminal_evidence_uses_durable_intent_and_preserves_handoff(
    worker_env,
):
    """Evidence-aware completion must stage a replayable intent before terminalizing."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None and task.current_run_id is not None and task.claim_lock
        intent_id = "ti_0123456789abcdef"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="verified",
        )
    finally:
        conn.close()

    out = kt._handle_complete({
        "summary": "verified completion with durable handoff",
        "metadata": {"checks": 3},
        "artifacts": ["/tmp/report.pdf"],
        "terminal_evidence": terminal_evidence,
    })
    response = json.loads(out)
    assert response["ok"] is True
    assert response["terminal_intent_id"] == intent_id

    conn = kb.connect()
    try:
        intent = conn.execute(
            "SELECT status, applied_event_id FROM terminal_intents "
            "WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone()
        assert intent is not None and intent["status"] == "acknowledged"
        run = kb.latest_run(conn, worker_env)
        assert run.summary == "verified completion with durable handoff"
        assert run.metadata == {"checks": 3, "artifacts": ["/tmp/report.pdf"]}
        event = conn.execute(
            "SELECT payload FROM task_events WHERE id=?",
            (intent["applied_event_id"],),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["terminal_intent_id"] == intent_id
        assert payload["summary"] == "verified completion with durable handoff"
        assert payload["artifacts"] == ["/tmp/report.pdf"]
    finally:
        conn.close()


def test_complete_terminal_evidence_requires_dispatcher_capability(
    monkeypatch,
    worker_env,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        intent_id = "ti_bbbbbbbbbbbbbbbb"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="verified",
        )
    finally:
        conn.close()

    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK")
    out = json.loads(kt._handle_complete({
        "summary": "must not use the claim read back from the database",
        "terminal_evidence": terminal_evidence,
    }))
    assert "dispatcher run/claim capability" in out["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
        assert conn.execute(
            "SELECT 1 FROM terminal_intents WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone() is None
    finally:
        conn.close()


def test_complete_terminal_evidence_rejects_wrong_claim_capability(
    monkeypatch,
    worker_env,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        intent_id = "ti_cccccccccccccccc"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="verified",
        )
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "wrong-claim-capability")
    out = json.loads(kt._handle_complete({
        "summary": "wrong claim must fail closed",
        "terminal_evidence": terminal_evidence,
    }))
    assert "exact active run and claim" in out["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
        assert conn.execute(
            "SELECT 1 FROM terminal_intents WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone() is None
    finally:
        conn.close()


def test_complete_terminal_evidence_exact_retry_is_idempotent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        intent_id = "ti_dddddddddddddddd"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="verified",
        )
    finally:
        conn.close()

    request = {
        "summary": "response-loss-safe completion",
        "metadata": {"checks": 7},
        "terminal_evidence": terminal_evidence,
    }
    first = json.loads(kt._handle_complete(request))
    second = json.loads(kt._handle_complete(request))
    assert first["ok"] is True
    assert second == first

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, producer_attestation FROM terminal_intents "
            "WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone()
        assert row["status"] == "acknowledged"
        assert len(row["producer_attestation"]) == 64
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id=? AND kind='completed'",
            (worker_env,),
        ).fetchone()
        assert events["n"] == 1
    finally:
        conn.close()


def test_complete_runtime_producer_exact_retry_is_idempotent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    request = {
        "summary": "runtime-produced response-loss-safe completion",
        "metadata": {"checks": 9},
    }
    first = json.loads(kt._handle_complete(request))
    second = json.loads(kt._handle_complete(request))
    assert first["ok"] is True
    assert second == first
    assert first["terminal_intent_id"].startswith("ti_")

    with kb.connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id=? AND kind='completed'",
            (worker_env,),
        ).fetchone()
        assert events["n"] == 1


def test_terminal_intent_db_enforces_action_decision_invariants(worker_env):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        intent_id = "ti_eeeeeeeeeeeeeeee"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="stable_block",
        )
        with pytest.raises(ValueError, match="completion decision"):
            kb.create_completion_terminal_intent(
                conn,
                terminal_intent_id=intent_id,
                task_id=task.id,
                run_id=task.current_run_id,
                claim_lock=task.claim_lock,
                decision="stable_block",
                failure_class="none",
                manifest=terminal_evidence["manifest"],
                provenance_digest=terminal_evidence["provenance_digest"],
                summary="must not stage",
            )
    finally:
        conn.close()


def test_complete_crash_after_staging_replays_exact_handoff(
    monkeypatch,
    worker_env,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None and task.current_run_id is not None and task.claim_lock
        intent_id = "ti_aaaaaaaaaaaaaaaa"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="complete",
            block_kind=None,
            failure_class="none",
            decision="verified",
        )
    finally:
        conn.close()

    original_apply = kb.apply_terminal_intent

    def crash_after_staging(*args, **kwargs):
        raise RuntimeError("simulated crash after intent staging")

    monkeypatch.setattr(kb, "apply_terminal_intent", crash_after_staging)
    out = kt._handle_complete({
        "summary": "crash-safe summary",
        "metadata": {"checks": 5},
        "terminal_evidence": terminal_evidence,
    })
    assert "simulated crash after intent staging" in json.loads(out)["error"]

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        intent = conn.execute(
            "SELECT status FROM terminal_intents WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone()
        assert intent is not None and intent["status"] == "pending"
        handoff = conn.execute(
            "SELECT handoff_digest FROM terminal_handoffs "
            "WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone()
        assert handoff is not None
    finally:
        conn.close()

    monkeypatch.setattr(kb, "apply_terminal_intent", original_apply)
    conn = kb.connect()
    try:
        assert kb.replay_terminal_intents(conn) == {
            "replayed": [intent_id],
            "failed": [],
        }
        task = kb.get_task(conn, worker_env)
        run = kb.latest_run(conn, worker_env)
        assert task.status == "done"
        assert run.summary == "crash-safe summary"
        assert run.metadata == {"checks": 5}
    finally:
        conn.close()


def test_complete_metadata_round_trips_through_show(worker_env):
    """Structured completion metadata should be visible to downstream agents."""
    from tools import kanban_tools as kt

    handoff = {
        "changed_files": ["hermes_cli/kanban.py"],
        "verification": ["pytest tests/tools/test_kanban_tools.py -q"],
        "dependencies": [],
        "blocked_reason": None,
        "retry_notes": "none",
        "residual_risk": ["dashboard rendering not exercised"],
    }

    complete_out = kt._handle_complete({
        "summary": "finished with structured evidence",
        "metadata": handoff,
    })
    assert json.loads(complete_out)["ok"] is True

    show_out = kt._handle_show({"task_id": worker_env})
    shown = json.loads(show_out)
    assert shown["task"]["status"] == "done"
    assert "finished with structured evidence" in shown["worker_context"]
    assert '"changed_files": ["hermes_cli/kanban.py"]' in shown["worker_context"]


def test_complete_stamps_worker_session_id_from_env(monkeypatch, worker_env):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")
    metadata = {"files": 2, "worker_session_id": "user-spoof"}

    out = kt._handle_complete({
        "summary": "done by scoped worker",
        "metadata": metadata,
    })
    assert json.loads(out)["ok"] is True
    assert metadata["worker_session_id"] == "user-spoof"

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "session-trusted",
        }
    finally:
        conn.close()


def test_complete_does_not_stamp_worker_session_id_without_scoped_task(
    monkeypatch, worker_env
):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")

    out = kt._handle_complete({
        "task_id": worker_env,
        "summary": "done outside worker scope",
        "metadata": {"files": 2, "worker_session_id": "user-provided"},
    })
    assert json.loads(out)["ok"] is True

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "user-provided",
        }
    finally:
        conn.close()


def test_complete_with_result_only(worker_env):
    """`result` alone (without summary) is accepted for legacy compat."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({"result": "legacy result"})
    d = json.loads(out)
    assert d["ok"] is True


def test_complete_with_artifacts_lands_in_event_payload(worker_env):
    """``artifacts=[...]`` rides into the completed event payload so the
    gateway notifier can upload them as native attachments. See the
    kanban notifier in gateway/run.py for the consumer side."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "rendered the chart",
        "artifacts": ["/tmp/q3-revenue.png", "/tmp/q3-report.pdf"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        events = kb.list_events(conn, worker_env)
        # Find the completion event
        completed = [e for e in events if e.kind == "completed"]
        assert len(completed) == 1
        payload = completed[0].payload or {}
        assert payload.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
        # And the artifacts also live on metadata for downstream workers
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
    finally:
        conn.close()


def test_complete_artifacts_accepts_single_string(worker_env):
    """A bare string is auto-promoted to a single-element list for convenience."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "one chart",
        "artifacts": "/tmp/chart.png",
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == ["/tmp/chart.png"]
    finally:
        conn.close()


def test_complete_artifacts_merges_with_explicit_metadata_field(worker_env):
    """If the worker passes metadata.artifacts AND the top-level artifacts
    param, merge the two without duplicates."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "merged",
        "metadata": {"artifacts": ["/tmp/a.png"], "other": "fact"},
        "artifacts": ["/tmp/b.pdf", "/tmp/a.png"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        # Order: existing entries first, then new ones, deduplicated.
        assert run.metadata.get("artifacts") == ["/tmp/a.png", "/tmp/b.pdf"]
        assert run.metadata.get("other") == "fact"
    finally:
        conn.close()


def test_complete_rejects_non_list_artifacts(worker_env):
    """Non-list, non-string artifacts should be rejected with a clear error."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "bad shape",
        "artifacts": {"not": "a list"},
    })
    err = json.loads(out).get("error", "")
    assert "artifacts must be a list" in err


def test_complete_missing_scratch_artifact_stays_in_flight(worker_env):
    """A false deliverable claim must return retry guidance, not mark Done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, worker_env, workspace)

    output = kt._handle_complete({
        "summary": "report complete",
        "artifacts": [str(workspace / "missing-report.md")],
    })
    error = json.loads(output).get("error", "")

    assert "could not preserve" in error
    assert "still in-flight" in error
    assert "retry kanban_complete" in error
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"
    assert workspace.exists()


def test_complete_rejects_no_handoff(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({})
    assert json.loads(out).get("error"), "should have errored"


def test_complete_rejects_non_dict_metadata(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "x", "metadata": [1, 2, 3]})
    assert json.loads(out).get("error")


def test_complete_phantom_card_message_advertises_retry(worker_env):
    """A phantom-card rejection must surface a tool_error that explicitly
    tells the worker the task is still in-flight and how to retry — the
    worker has no other channel to discover that. Regression for #22923,
    where the previous wording read like a terminal failure and workers
    routinely abandoned the run instead of trying again.
    """
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "oops claimed a phantom",
        "created_cards": ["t_phantomdeadbeef"],
    })
    err = json.loads(out).get("error", "")
    assert err, f"expected an error, got {out!r}"
    # Phantom id surfaced verbatim.
    assert "t_phantomdeadbeef" in err
    # The retry-is-supported phrasing — these are the literal cues a
    # worker reads to decide whether to retry vs block/abandon. If a
    # future change rewords the message, these checks will catch the
    # regression. See #22923 for the failure mode.
    assert "still in-flight" in err
    assert "Retry kanban_complete" in err
    assert "created_cards=[]" in err

    # Critically: the task is genuinely still in-flight — the gate
    # rejection did not mutate state, so the worker's retry can land.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_retry_with_corrected_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with a
    corrected created_cards list (phantom ids removed) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Create a real child via the tool so it gets the worker-profile
    # attribution the gate trusts.
    child = json.loads(kt._handle_create({
        "title": "real child", "assignee": "peer",
    }))
    assert child["ok"]
    real_id = child["task_id"]

    # First attempt mixes real + phantom — gate rejects.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": [real_id, "t_phantomdeadbeef"],
    }))
    assert rejected.get("error")
    assert "t_phantomdeadbeef" in rejected["error"]

    # Retry with corrected list.
    ok = json.loads(kt._handle_complete({
        "summary": "retry with corrected list",
        "created_cards": [real_id],
    }))
    assert ok.get("ok") is True


def test_complete_goal_mode_rejected_by_judge(monkeypatch, tmp_path):
    """Goal-mode tasks must pass the auxiliary judge before completion.
    Regression for #38367: workers bypassing the judge via early kanban_complete."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Set up isolated HERMES_HOME
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
        task = kb.get_task(conn, goal_task_id)
        assert task is not None
        assert task.current_run_id is not None
        assert task.claim_lock
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)

    # Mock the judge to reject the completion. The gate only runs when a
    # judge is reachable, so force the availability probe True as well.
    def mock_judge_goal(goal, last_response, *, timeout=30.0, subgoals=None):
        # Match the real judge_goal contract:
        # (verdict, reason, parse_failed, wait_directive, transport_failed)
        return "continue", "missing verification evidence", False, None, False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", mock_judge_goal)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)

    # Attempt to complete should be rejected
    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]
    assert f"parents=[{goal_task_id}]" in d["error"]

    # Verify the task is NOT completed in the DB
    conn2 = kb.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "running"  # Should still be running, not done
    finally:
        conn2.close()


def test_complete_goal_mode_allows_when_judge_unavailable(monkeypatch, tmp_path):
    """Fail-open: an unreachable judge must not wedge a goal_mode worker.

    judge_goal returns a "continue" verdict when no auxiliary model is
    configured, which is indistinguishable from a real "not done" judgment.
    The gate probes availability first, so completion proceeds rather than
    being rejected forever when no judge can be reached."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
        task = kb.get_task(conn, goal_task_id)
        assert task is not None
        assert task.current_run_id is not None
        assert task.claim_lock
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)

    # No judge reachable. judge_goal must not even be consulted; if it were,
    # this stub would reject — so reaching "done" proves the probe short-circuit.
    def fail_if_called(goal, last_response, *, timeout=30.0, subgoals=None):
        raise AssertionError("judge_goal must not run when no judge is available")

    monkeypatch.setattr("tools.kanban_tools.judge_goal", fail_if_called)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: False)

    out = kt._handle_complete({"summary": "done enough"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn2 = kb.connect()
    try:
        assert kb.get_task(conn2, goal_task_id).status == "done"
    finally:
        conn2.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["block_kind"] == "needs_input"
    assert d["terminal_intent_id"].startswith("ti_")
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_terminal_evidence_is_internal_and_hidden_from_model_schemas():
    from tools import kanban_tools as kt

    assert "terminal_evidence" not in (
        kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    )
    assert "terminal_evidence" not in (
        kt.KANBAN_BLOCK_SCHEMA["parameters"]["properties"]
    )


def test_block_with_terminal_evidence_uses_durable_intent_and_preserves_reason(
    worker_env,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None and task.current_run_id is not None and task.claim_lock
        intent_id = "ti_fedcba9876543210"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="block",
            block_kind="capability",
            failure_class="credential",
            decision="stable_block",
        )
    finally:
        conn.close()

    out = kt._handle_block({
        "reason": "credential is unavailable",
        "kind": "capability",
        "terminal_evidence": terminal_evidence,
    })
    response = json.loads(out)
    assert response["ok"] is True
    assert response["terminal_intent_id"] == intent_id
    assert response["status"] == "blocked"

    conn = kb.connect()
    try:
        intent = conn.execute(
            "SELECT status, applied_event_id FROM terminal_intents "
            "WHERE terminal_intent_id=?",
            (intent_id,),
        ).fetchone()
        assert intent is not None and intent["status"] == "acknowledged"
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "blocked"
        assert run.summary == "credential is unavailable"
        event = conn.execute(
            "SELECT payload FROM task_events WHERE id=?",
            (intent["applied_event_id"],),
        ).fetchone()
        payload = json.loads(event["payload"])
        assert payload["terminal_intent_id"] == intent_id
        assert payload["reason"] == "credential is unavailable"
        assert payload["kind"] == "capability"
        assert payload["recurrences"] == 1
    finally:
        conn.close()


def test_block_terminal_evidence_exact_retry_is_idempotent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        intent_id = "ti_ffffffffffffffff"
        terminal_evidence = _terminal_evidence(
            task,
            intent_id=intent_id,
            action="block",
            block_kind="capability",
            failure_class="credential",
            decision="stable_block",
        )
    finally:
        conn.close()

    request = {
        "reason": "response-loss-safe block",
        "kind": "capability",
        "terminal_evidence": terminal_evidence,
    }
    first = json.loads(kt._handle_block(request))
    second = json.loads(kt._handle_block(request))
    assert first["ok"] is True
    assert second == first

    conn = kb.connect()
    try:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id=? AND kind='blocked'",
            (worker_env,),
        ).fetchone()
        assert events["n"] == 1
    finally:
        conn.close()


def test_block_runtime_producer_exact_retry_is_idempotent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    request = {
        "reason": "runtime-produced response-loss-safe block",
        "kind": "capability",
    }
    first = json.loads(kt._handle_block(request))
    second = json.loads(kt._handle_block(request))
    assert first["ok"] is True
    assert second == first
    assert first["terminal_intent_id"].startswith("ti_")

    with kb.connect() as conn:
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id=? AND kind='blocked'",
            (worker_env,),
        ).fetchone()
        assert events["n"] == 1


def test_block_rejects_empty_reason(worker_env):
    from tools import kanban_tools as kt
    for bad in ["", "   ", None]:
        out = kt._handle_block({"reason": bad})
        assert json.loads(out).get("error")


def _make_goal_mode_worker_env(monkeypatch, tmp_path):
    """Set up an isolated HERMES_HOME with one claimed goal_mode task,
    matching the pattern used by the kanban_complete judge gate tests."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-block-test", assignee="test-worker",
            body="Must achieve X.", goal_mode=True,
        )
        kb.claim_task(conn, goal_task_id)
        task = kb.get_task(conn, goal_task_id)
        assert task is not None
        assert task.current_run_id is not None
        assert task.claim_lock
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    return goal_task_id


def test_block_goal_mode_rejects_missing_kind(monkeypatch, tmp_path):
    """A goal_mode worker calling kanban_block with no kind must not be able
    to use it as an unguarded escape from the goal loop (Issue #38696,
    sibling of the kanban_complete judge gate / Issue #38367)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "giving up"})
    d = json.loads(out)
    assert "error" in d
    assert "goal_mode" in d["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_rejects_disallowed_kind(monkeypatch, tmp_path):
    """`capability` / `transient` are valid kinds in general but must not
    let a goal_mode worker exit the loop without going through the judge."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    for kind in ("capability", "transient"):
        out = kt._handle_block({"reason": "blocked", "kind": kind})
        d = json.loads(out)
        assert "error" in d, f"kind={kind} should be rejected for goal_mode"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_allows_dependency_kind(monkeypatch, tmp_path):
    """`dependency` and `needs_input` represent a genuine external blocker
    the worker cannot resolve itself — these remain ungated.

    `dependency` routes to status='todo' (not 'blocked') per block_task's
    own kind-routing — the goal loop still treats anything outside
    running/ready/done/blocked as a stop, so this is still a legitimate,
    judge-free exit; it's just not the literal 'blocked' status."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "waiting on another task", "kind": "dependency"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "todo"
    finally:
        conn.close()


def test_block_goal_mode_allows_needs_input_kind(monkeypatch, tmp_path):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "need a decision from the user", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_block_non_goal_mode_task_unaffected_by_new_gate(worker_env):
    """The new gate only applies to goal_mode tasks — plain tasks must keep
    blocking freely with no kind, exactly as before this fix."""
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    assert json.loads(out).get("ok") is True


def test_heartbeat_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "progress"})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_without_note(worker_env):
    """note is optional."""
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kb.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_rejects_empty_body(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({"task_id": worker_env, "body": "   "})
    assert json.loads(out).get("error")


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_comment_schema_omits_author_override():
    """The ``author`` property must not appear on KANBAN_COMMENT_SCHEMA;
    exposing it to the LLM would re-introduce the forgery surface this
    handler is hardened against.
    """
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA
    props = KANBAN_COMMENT_SCHEMA["parameters"]["properties"]
    assert "author" not in props


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_worker_create_without_key_is_automatically_idempotent(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    request = {
        "title": "retry-safe child",
        "assignee": "peer",
        "body": "same normalized request",
        "parents": [worker_env],
    }
    first = json.loads(kt._handle_create(request))
    second = json.loads(kt._handle_create(request))
    assert first["ok"] is True
    assert second["task_id"] == first["task_id"]

    with kb.connect() as conn:
        rows = conn.execute(
            "SELECT idempotency_key FROM tasks WHERE title='retry-safe child'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["idempotency_key"].startswith(
            f"worker-create:{worker_env}:"
        )


def test_worker_create_explicit_distinct_keys_allow_intentional_duplicates(worker_env):
    from tools import kanban_tools as kt

    base = {"title": "intentional duplicate", "assignee": "peer"}
    first = json.loads(kt._handle_create({**base, "idempotency_key": "copy-a"}))
    second = json.loads(kt._handle_create({**base, "idempotency_key": "copy-b"}))
    assert first["ok"] is True and second["ok"] is True
    assert first["task_id"] != second["task_id"]


def test_create_inherits_worker_dir_workspace(monkeypatch, worker_env):
    """A worker scoped to a dir: task that spawns a child without a
    workspace arg inherits the dir, not scratch (so follow-up code-gen
    lands in the same project)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    proj = "/home/teknium/myproject"
    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path=proj,
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({"title": "follow-up", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "dir"
        assert child.workspace_path == proj
    finally:
        conn.close()


def test_create_explicit_workspace_beats_inheritance(monkeypatch, worker_env):
    """An explicit workspace arg overrides worker-task inheritance."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path="/home/teknium/proj",
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({
        "title": "scratch child", "assignee": "peer",
        "workspace_kind": "scratch",
    }))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
    finally:
        conn.close()


def test_create_no_worker_task_stays_scratch(monkeypatch, worker_env):
    """Orchestrator/CLI callers (no HERMES_KANBAN_TASK) still default to
    scratch — inheritance only applies to task-scoped workers."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    d = json.loads(kt._handle_create({"title": "orch child", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_stamps_session_id_from_env(monkeypatch, worker_env):
    """When the agent loop runs under ACP, the server propagates the
    originating chat session id via HERMES_SESSION_ID. ``kanban_create``
    reads it and stamps the new task so clients can render a per-session
    board (issue: ACP session linkage on kanban tasks)."""
    monkeypatch.setenv("HERMES_SESSION_ID", "acp-sess-abc")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "from chat",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "acp-sess-abc"
    finally:
        conn.close()


def test_create_session_id_arg_overrides_env(monkeypatch, worker_env):
    """An explicit ``session_id`` arg from the model wins over the env
    propagation. Edge case but exercised: a tool call could carry a
    different session id (e.g. cross-session linking) and the explicit
    arg should not be silently overwritten."""
    monkeypatch.setenv("HERMES_SESSION_ID", "from-env")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "explicit override",
        "assignee": "peer",
        "parents": [worker_env],
        "session_id": "explicit-arg",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "explicit-arg"
    finally:
        conn.close()


def test_create_session_id_absent_when_env_unset(monkeypatch, worker_env):
    """No env var, no arg → session_id stays NULL. Important for backwards
    compatibility: pre-ACP-propagation hosts and CLI-driven creates must
    not accidentally inherit a stale id."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "no session",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id is None
    finally:
        conn.close()


def test_create_rejects_no_title(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"assignee": "x"})).get("error")
    assert json.loads(kt._handle_create({"title": "   ", "assignee": "x"})).get("error")


def test_create_rejects_no_assignee(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"title": "t"})).get("error")


def test_create_rejects_non_list_parents(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "t", "assignee": "a", "parents": 42})
    assert json.loads(out).get("error")


def test_create_parses_triage_string_false(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "not triage",
        "assignee": "peer",
        "triage": "false",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "ready"
    finally:
        conn.close()


def test_create_parses_triage_string_true(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "needs triage",
        "assignee": "peer",
        "triage": "true",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "triage"
    finally:
        conn.close()


def test_create_rejects_bad_triage(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "bad triage",
        "assignee": "peer",
        "triage": "sometimes",
    })
    assert "triage must be" in json.loads(out).get("error", "")


def test_create_accepts_string_parent(worker_env):
    """Convenience: a single parent id as string is coerced to [id]."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "parents": worker_env,
    })
    assert json.loads(out)["ok"]


def test_create_accepts_skills_list(worker_env):
    """Tool writes the per-task skills through to the kernel."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "skilled",
        "assignee": "linguist",
        "skills": ["translation", "github-code-review"],
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation", "github-code-review"]


def test_create_accepts_skills_string(worker_env):
    """Convenience: a single skill name as string is coerced to [name]."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "one-skill",
        "assignee": "a",
        "skills": "translation",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation"]


def test_create_rejects_non_list_skills(worker_env):
    """skills: 42 must be rejected, not silently dropped."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "skills": 42,
    })
    assert json.loads(out).get("error")


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_link_rejects_self_reference(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": worker_env, "child_id": worker_env})
    assert json.loads(out).get("error")


def test_link_rejects_missing_args(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_link({"parent_id": "x"})).get("error")
    assert json.loads(kt._handle_link({"child_id": "y"})).get("error")


def test_link_rejects_cycle(worker_env):
    """A → B, then try to link B → A."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x", parents=[a])
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": b, "child_id": a})
    assert json.loads(out).get("error")


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_with_pending_parents_returns_todo(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": child})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "todo"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, child).status == "todo"
    finally:
        conn.close()


def test_unblock_rejects_non_blocked_task(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": worker_env})
    assert json.loads(out).get("error")


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------

def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban worker protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    assert "meaningful, verified" in prompt
    assert "Automatic liveness heartbeats" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt


def test_kanban_guidance_prompt_size_bounded(monkeypatch, tmp_path):
    """Sanity: the guidance block stays lean so it doesn't blow up the
    cached prompt.

    The block keeps only the lifecycle contract and points detailed state back
    to worker_context/tool schemas instead of embedding a reference manual in
    every model call.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from agent.prompt_builder import KANBAN_GUIDANCE
    assert 1_500 < len(KANBAN_GUIDANCE) < 2_500, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


def test_kanban_model_schema_footprint_is_bounded():
    """Keep the always-sent worker tool surface materially below the old
    17k-character schema payload without snapshotting individual prose."""
    from tools import kanban_tools as kt

    all_schemas = [
        value
        for name, value in vars(kt).items()
        if name.startswith("KANBAN_") and name.endswith("_SCHEMA")
    ]
    worker_schemas = [
        kt.KANBAN_SHOW_SCHEMA,
        kt.KANBAN_COMPLETE_SCHEMA,
        kt.KANBAN_BLOCK_SCHEMA,
        kt.KANBAN_HEARTBEAT_SCHEMA,
        kt.KANBAN_COMMENT_SCHEMA,
        kt.KANBAN_CREATE_SCHEMA,
        kt.KANBAN_LINK_SCHEMA,
    ]

    def _size(schemas):
        return sum(
            len(json.dumps(schema, sort_keys=True, separators=(",", ":")))
            for schema in schemas
        )

    assert _size(all_schemas) < 12_000
    assert _size(worker_schemas) < 10_000


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_block_rejects_foreign_task_id(worker_env):
    """A worker cannot block a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_block({"task_id": other, "reason": "evil"})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_heartbeat_rejects_foreign_task_id(worker_env):
    """A worker cannot heartbeat a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        # Put sibling in running state so heartbeat would otherwise succeed.
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"task_id": other})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_worker_complete_own_task_still_works(worker_env):
    """The ownership check doesn't break the normal own-task happy path."""
    from tools import kanban_tools as kt
    # Both implicit (no task_id arg) and explicit (matching env) must work.
    out = kt._handle_complete({"task_id": worker_env, "summary": "explicit own"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == worker_env


def test_worker_complete_rejects_stale_run_id(worker_env, monkeypatch):
    """A retried worker cannot complete the task using an old run token."""
    from hermes_cli import kanban_db as kb
    import hermes_cli.kanban_db as _kb

    # detect_crashed_workers now gates each running task behind a
    # launch-window grace period (c002668ff) so a freshly-spawned worker
    # whose PID isn't yet visible on /proc isn't reclaimed. The fixture
    # creates the task moments before this assertion, so the grace
    # period (default 30s) would skip the liveness check. Zero it out
    # for this test — we WANT immediate reclamation here.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    conn = kb.connect()
    try:
        run1 = kb.latest_run(conn, worker_env)
        kb._set_worker_pid(conn, worker_env, 98765)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [worker_env]

        kb.claim_task(conn, worker_env)
        run2 = kb.latest_run(conn, worker_env)
        assert run2.id != run1.id
    finally:
        conn.close()

    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.id))
    out = kt._handle_complete({"summary": "late stale completion"})
    d = json.loads(out)
    assert d.get("ok") is not True

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run2.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run2.id))
    out = kt._handle_complete({"summary": "current completion"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid


# ---------------------------------------------------------------------------
# Optional ``board`` parameter — per-call DB override
# ---------------------------------------------------------------------------
#
# The dispatcher pins the active board via HERMES_KANBAN_BOARD env var,
# but a Telegram-side orchestrator handling multiple boards needs to be
# able to route a single tool call to a specific board's DB without
# restarting Hermes. These tests pin that ``board=<slug>`` argument
# routes each handler to that board's sqlite file, and that omitting
# ``board`` preserves the legacy env-driven resolution.


@pytest.fixture
def multi_board_env(monkeypatch, tmp_path):
    """Isolated Hermes home with two distinct kanban boards seeded.

    Returns ``("default", "alt")`` slugs. The default board has one
    pre-existing task ``seed_default``; ``alt`` has ``seed_alt``. No
    HERMES_KANBAN_TASK is pinned (orchestrator context) — workers test
    the env-task case via the existing ``worker_env`` fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make sure neither HERMES_KANBAN_DB nor HERMES_KANBAN_BOARD pin a
    # board — the test is specifically about the per-call override.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Default board — implicit
    conn = kb.connect()
    try:
        seed_default = kb.create_task(
            conn, title="seed-default", assignee="worker-d"
        )
    finally:
        conn.close()
    # Alt board — explicit slug routes the connection to a separate DB
    conn = kb.connect(board="alt")
    try:
        seed_alt = kb.create_task(
            conn, title="seed-alt", assignee="worker-a"
        )
    finally:
        conn.close()
    return {
        "default_seed": seed_default,
        "alt_seed": seed_alt,
        "default_db": kb.kanban_db_path(),
        "alt_db": kb.kanban_db_path(board="alt"),
    }


def test_board_param_routes_create_to_alt_board(multi_board_env):
    """kanban_create with ``board="alt"`` must write into the alt board's DB,
    not the default one."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-only",
        "assignee": "worker",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    new_tid = d["task_id"]

    # Lands on alt board.
    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, new_tid).title == "alt-only"
    # Does NOT land on default board.
    with kb.connect() as conn:
        assert kb.get_task(conn, new_tid) is None


def test_board_param_routes_list_to_alt_board(multi_board_env):
    """kanban_list filters by the board parameter, not env-active."""
    from tools import kanban_tools as kt

    # Default — sees seed-default, not seed-alt.
    default_out = json.loads(kt._handle_list({}))
    default_titles = {t["title"] for t in default_out["tasks"]}
    assert "seed-default" in default_titles
    assert "seed-alt" not in default_titles

    # Alt — sees seed-alt, not seed-default.
    alt_out = json.loads(kt._handle_list({"board": "alt"}))
    alt_titles = {t["title"] for t in alt_out["tasks"]}
    assert "seed-alt" in alt_titles
    assert "seed-default" not in alt_titles


def test_board_param_routes_show_to_alt_board(multi_board_env):
    """kanban_show reads from the board parameter, not env-active.

    Tasks across boards may share ids (the id space is per-DB) but the
    seed task ids in this fixture are distinct, so a cross-board show
    must return the matching task only when board is correct.
    """
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Without board override, the alt task is invisible.
    bad = json.loads(kt._handle_show({"task_id": alt_seed}))
    assert "not found" in bad.get("error", "")

    # With board override, it's readable.
    good = json.loads(kt._handle_show({"task_id": alt_seed, "board": "alt"}))
    assert good["task"]["id"] == alt_seed
    assert good["task"]["title"] == "seed-alt"


def test_board_param_routes_assign_via_create_to_alt(multi_board_env):
    """Workflow test for the 'assign' UX — create with assignee on a
    specific board. (The CLI has a separate ``kanban assign`` verb; the
    MCP surface assigns at task creation time.)"""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-assigned",
        "assignee": "linguist",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect(board="alt") as conn:
        task = kb.get_task(conn, d["task_id"])
        assert task is not None
        assert task.assignee == "linguist"


def test_board_param_routes_comment_to_alt_board(multi_board_env):
    """kanban_comment routes the insert to the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    out = kt._handle_comment({
        "task_id": alt_seed,
        "body": "alt comment",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        comments = kb.list_comments(conn, alt_seed)
        assert len(comments) == 1
        assert comments[0].body == "alt comment"
    # Default board does not have this task at all, so no rogue comment.
    with kb.connect() as conn:
        assert kb.get_task(conn, alt_seed) is None


def test_board_param_routes_complete_to_alt_board(multi_board_env):
    """kanban_complete on the alt board closes the alt task, leaving
    the default seed untouched."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Make alt task running so complete is valid.
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_complete({
        "task_id": alt_seed,
        "summary": "alt close",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "done"
    # Default seed is unchanged.
    with kb.connect() as conn:
        default_seed = multi_board_env["default_seed"]
        assert kb.get_task(conn, default_seed).status == "ready"


def test_board_param_routes_block_to_alt_board(multi_board_env):
    """kanban_block targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_block({
        "task_id": alt_seed,
        "reason": "need input on alt board",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "blocked"


def test_board_param_routes_unblock_to_alt_board(multi_board_env):
    """kanban_unblock targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.block_task(conn, alt_seed, reason="waiting")
        assert kb.get_task(conn, alt_seed).status == "blocked"

    out = kt._handle_unblock({"task_id": alt_seed, "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "ready"


def test_board_param_routes_heartbeat_to_alt_board(monkeypatch, tmp_path):
    """kanban_heartbeat targets the alt board's DB. Worker-scoped, so we
    use the worker-env style fixture inline (pinning HERMES_KANBAN_TASK
    to a task that exists in the alt board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "alt-worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Seed the alt board with a claimed task.
    with kb.connect(board="alt") as conn:
        tid = kb.create_task(conn, title="alt hb", assignee="alt-worker")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "alive on alt", "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True

    # Heartbeat event landed in the alt DB.
    with kb.connect(board="alt") as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "heartbeat"]
        assert len(events) == 1


def test_board_param_routes_link_to_alt_board(multi_board_env):
    """kanban_link operates on the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect(board="alt") as conn:
        a = kb.create_task(conn, title="A-alt", assignee="x")
        b = kb.create_task(conn, title="B-alt", assignee="x")

    out = kt._handle_link({
        "parent_id": a,
        "child_id": b,
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert b in kb.child_ids(conn, a)


def test_board_param_none_falls_back_to_env(worker_env):
    """When ``board`` is omitted or None, behaviour is unchanged from
    before this feature — calls land on whatever the env resolves to.
    Regression guard against accidentally rewiring default resolution."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_show({})  # no board, no task_id
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    out = kt._handle_show({"task_id": worker_env, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    # Sanity: the env-resolved path is the legacy default DB, NOT an
    # 'alt' board path. Confirms the override path was not silently
    # forced.
    assert kb.kanban_db_path() == kb.kanban_db_path(board="default")


def test_board_param_rejects_invalid_slug(multi_board_env):
    """A board slug that fails ``_normalize_board_slug`` surfaces as a
    structured tool_error rather than a 500 / unhandled exception."""
    from tools import kanban_tools as kt

    out = kt._handle_list({"board": "Has Spaces"})
    err = json.loads(out).get("error", "")
    assert "invalid board slug" in err, f"got {err!r}"


def test_board_param_in_all_schemas():
    """Every kanban_* tool schema must expose an optional ``board``
    parameter. This pins the contract surfaced to the LLM — adding a
    new kanban tool without ``board`` will fail CI immediately."""
    from tools import kanban_tools as kt

    schemas = [
        kt.KANBAN_SHOW_SCHEMA,
        kt.KANBAN_LIST_SCHEMA,
        kt.KANBAN_COMPLETE_SCHEMA,
        kt.KANBAN_BLOCK_SCHEMA,
        kt.KANBAN_HEARTBEAT_SCHEMA,
        kt.KANBAN_COMMENT_SCHEMA,
        kt.KANBAN_CREATE_SCHEMA,
        kt.KANBAN_UNBLOCK_SCHEMA,
        kt.KANBAN_LINK_SCHEMA,
        kt.KANBAN_ATTACH_SCHEMA,
        kt.KANBAN_ATTACH_URL_SCHEMA,
        kt.KANBAN_ATTACHMENTS_SCHEMA,
    ]
    for schema in schemas:
        props = schema["parameters"]["properties"]
        assert "board" in props, (
            f"{schema['name']} is missing the 'board' property"
        )
        assert props["board"]["type"] == "string"
        # board is optional everywhere — never in required.
        assert "board" not in schema["parameters"].get("required", []), (
            f"{schema['name']} marks board as required; must be optional"
        )


# ---------------------------------------------------------------------------
# kanban_create auto-subscribe behaviour
#
# When a worker calls kanban_create from inside a session that has a
# persistent delivery channel, the originating session should be
# subscribed to the new task's completion/block events automatically.
# - Gateway sessions: HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID set.
# - TUI sessions: HERMES_SESSION_KEY (or HERMES_SESSION_ID) set, with
#   the platform/chat_id ContextVars intentionally empty.
# - CLI / cron / test sessions: no delivery channel -> no subscription.
# - Config gate kanban.auto_subscribe_on_create: false -> no subscription
#   even when the session has a delivery channel.
# ---------------------------------------------------------------------------

def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return list(kb.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _sub_index(subs):
    """Normalise a list of notify-subs (dicts or objects) into dicts
    keyed by platform+chat_id, so assertions work regardless of the
    return shape."""
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "platform": getattr(s, "platform", None),
                "chat_id": getattr(s, "chat_id", None),
                "thread_id": getattr(s, "thread_id", None),
                "user_id": getattr(s, "user_id", None),
            })
    return out


def test_create_subscribes_gateway_session(monkeypatch, worker_env):
    """A gateway session (platform + chat_id set) gets auto-subscribed
    to its own kanban_create result, and the response surfaces the
    ``subscribed`` flag so the orchestrator can react."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-9")

    out = kt._handle_create({
        "title": "auto-sub gateway",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "telegram"
    assert s["chat_id"] == "chat-42"
    assert s["thread_id"] == "thread-7"
    assert s["user_id"] == "user-9"


def test_create_subscribes_tui_session_via_session_key(monkeypatch, worker_env):
    """TUI / desktop sessions don't have a platform/chat_id (single
    local channel), but the parent process exports HERMES_SESSION_KEY.
    We should still auto-subscribe, with platform='tui' and
    chat_id=<key>."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "auto-sub tui",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"


def test_create_does_not_subscribe_in_cli_session(monkeypatch, worker_env):
    """CLI / cron / test sessions have no persistent delivery channel.
    _maybe_auto_subscribe returns False and no row is written."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub cli",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_respects_auto_subscribe_on_create_false(monkeypatch, worker_env, tmp_path):
    """The config gate kanban.auto_subscribe_on_create=false must
    suppress auto-subscription even when the session has a delivery
    channel. This is the knob that addresses the upstream design
    concern from PR #19718 (reverted in #19721) — users who want
    explicit kanban_notify-subscribe calls per task get that."""
    # worker_env already created <tmp>/.hermes; use a fresh sibling
    # home to avoid mkdir() colliding with the worker's directory.
    home = tmp_path / "gate-home" / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "channel-1")

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no sub gated",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_partial_session_context_no_subscribe(monkeypatch, worker_env):
    """Only one of (platform, chat_id) set -> no implicit subscribe.
    Either both are set (gateway) or neither (TUI / CLI); partial is
    ambiguous and the safe default is to skip."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub partial",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d


def test_maybe_auto_subscribe_swallows_add_notify_sub_failure(monkeypatch, worker_env):
    """If add_notify_sub itself raises (e.g. DB locked, schema drift),
    _maybe_auto_subscribe must NOT bubble that up and fail the parent
    kanban_create. The function returns False and the parent create
    still succeeds with subscribed=False."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")

    from hermes_cli import kanban_db as kb

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    out = kt._handle_create({
        "title": "auto-sub tolerates add_notify_sub failure",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    assert d["subscribed"] is False, d


# ---------------------------------------------------------------------------
# Attachments — kanban_attach / kanban_attach_url / kanban_attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_private_urls(monkeypatch):
    """Opt the SSRF guard into private/loopback targets for local fixtures.

    Mirrors a user setting HERMES_ALLOW_PRIVATE_URLS on a private network.
    Resets the url_safety process-lifetime cache on both sides so the
    override neither leaks in nor out of the test.
    """
    from tools import url_safety

    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def test_attach_roundtrips_bytes_to_row_and_disk(worker_env):
    """kanban_attach decodes base64, writes the blob, and records the row."""
    import base64
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    content = b"hello attachment from a tool"
    out = kt._handle_attach({
        "filename": "notes.txt",
        "content_base64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
    })
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(content)
    att_id = d["attachment_id"]

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["notes.txt"]
        a = atts[0]
        assert a.id == att_id
        assert a.content_type == "text/plain"
        assert a.uploaded_by == "agent"
        # Blob is on disk under the task's attachments dir with the bytes.
        assert Path(a.stored_path).read_bytes() == content
        assert Path(a.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(worker_env).resolve()
        )
    finally:
        conn.close()


def test_attach_rejects_oversize(worker_env, monkeypatch):
    """A decoded payload over the cap returns a clean tool error, no row."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Shrink the cap so we don't have to build a 25 MB payload.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 8)
    out = kt._handle_attach({
        "filename": "big.bin",
        "content_base64": base64.b64encode(b"0123456789").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_rejects_bad_base64(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach({"filename": "x.txt", "content_base64": "not base64!!!"})
    d = json.loads(out)
    assert "error" in d and "base64" in d["error"]


def test_attach_requires_filename_and_content(worker_env):
    from tools import kanban_tools as kt

    assert "error" in json.loads(kt._handle_attach({"content_base64": "QQ=="}))
    assert "error" in json.loads(kt._handle_attach({"filename": "x.txt"}))


def test_attach_enforces_worker_task_ownership(worker_env):
    """A worker scoped to its own task can't attach to a foreign task."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="someone else's task", assignee="peer")
    finally:
        conn.close()

    out = kt._handle_attach({
        "task_id": other,
        "filename": "x.txt",
        "content_base64": base64.b64encode(b"x").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "scoped to task" in d["error"]


def test_attachments_lists_uploaded_files(worker_env):
    import base64

    from tools import kanban_tools as kt

    kt._handle_attach({
        "filename": "a.txt",
        "content_base64": base64.b64encode(b"aaa").decode(),
    })
    kt._handle_attach({
        "filename": "b.txt",
        "content_base64": base64.b64encode(b"bbbb").decode(),
    })
    out = kt._handle_attachments({})
    d = json.loads(out)
    assert d.get("ok") is True
    names = sorted(a["filename"] for a in d["attachments"])
    assert names == ["a.txt", "b.txt"]
    sizes = {a["filename"]: a["size"] for a in d["attachments"]}
    assert sizes == {"a.txt": 3, "b.txt": 4}


def test_attachments_unknown_task_errors(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attachments({"task_id": "t_nope"})
    assert "error" in json.loads(out)


def test_attach_url_fetches_local_fixture(worker_env, allow_private_urls):
    """kanban_attach_url downloads from an http(s) URL and stores the bytes.

    The fixture server lives on loopback, which the SSRF guard blocks by
    default — opted in via the allow_private_urls fixture exactly like a
    user on a private network would.
    """
    import http.server
    import threading
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    payload = b"downloaded-by-url body"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({
            "url": f"http://127.0.0.1:{port}/files/report.bin",
        })
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        # Filename derived from the URL path leaf.
        assert atts[0].filename == "report.bin"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def test_attach_url_rejects_oversize_stream(worker_env, monkeypatch, allow_private_urls):
    """An oversize response body is rejected during download, no row written."""
    import http.server
    import threading

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    big = b"x" * (64 * 1024)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *a):
            pass

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({"url": f"http://127.0.0.1:{port}/big.bin"})
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_rejects_non_http_scheme(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": "file:///etc/passwd"})
    d = json.loads(out)
    assert "error" in d
    assert "scheme" in d["error"]


# ---------------------------------------------------------------------------
# kanban_attach_url — SSRF guard (tools/url_safety.is_safe_url per hop)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_url_guard(monkeypatch):
    """Force the SSRF guard to its secure default for this test.

    Clears HERMES_ALLOW_PRIVATE_URLS and resets url_safety's process-lifetime
    cache on both sides so a prior test's opt-in can't leak in.
    """
    from tools import url_safety

    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _assert_attach_url_blocked(worker_env, url):
    """Call kanban_attach_url with ``url`` and assert the SSRF guard fired
    (clean tool error, no attachment row, no network fetch needed)."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": url})
    d = json.loads(out)
    assert "error" in d, out
    assert "SSRF" in d["error"] or "blocked" in d["error"].lower(), out
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_blocks_loopback(worker_env, default_url_guard):
    """http://127.0.0.1/ is rejected before any connection is made."""
    _assert_attach_url_blocked(worker_env, "http://127.0.0.1/")


def test_attach_url_blocks_cloud_metadata(worker_env, default_url_guard):
    """The cloud metadata endpoint is rejected — the #1 SSRF target."""
    _assert_attach_url_blocked(
        worker_env, "http://169.254.169.254/latest/meta-data/"
    )


def test_attach_url_blocks_private_range(worker_env, default_url_guard):
    """RFC1918 addresses (http://10.0.0.1/) are rejected."""
    _assert_attach_url_blocked(worker_env, "http://10.0.0.1/")


def _fake_public_dns(monkeypatch, mapping):
    """Patch url_safety's getaddrinfo so hostnames in ``mapping`` resolve to
    the given (public) IPs and literal IPs resolve to themselves — no real
    DNS or network traffic."""
    import ipaddress
    import socket as _socket

    real_af, real_sock = _socket.AF_INET, _socket.SOCK_STREAM

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            # Literal IPs pass through; unknown hostnames fail like NXDOMAIN.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise _socket.gaierror(f"fake DNS: unknown host {host!r}")
            ip = host
        return [(real_af, real_sock, 6, "", (ip, 0))]

    from tools import url_safety
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in {
            k.lower() for k in self.headers
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_attach_url_blocks_redirect_to_loopback(worker_env, default_url_guard, monkeypatch):
    """A public host 302ing to loopback is caught on the redirect hop.

    The pre-flight check passes (public IP), then the mocked response
    redirects to http://127.0.0.1/ — the guard must re-validate the
    Location target and refuse to follow it.
    """
    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    requested = []

    def fake_stream(method, url, **kwargs):
        requested.append(url)
        assert kwargs.get("follow_redirects") is False
        return _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1/latest/secrets"},
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/report.pdf"})
    d = json.loads(out)
    assert "error" in d, out
    assert "127.0.0.1" in d["error"], out
    # Only the public hop was ever fetched; the loopback target never was.
    assert requested == ["http://files.example.com/report.pdf"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_happy_path_public_host(worker_env, default_url_guard, monkeypatch):
    """A public URL passes the guard and the bytes are stored (mocked fetch)."""
    from pathlib import Path

    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    payload = b"public fetch body"

    def fake_stream(method, url, **kwargs):
        assert url == "http://files.example.com/docs/spec.pdf"
        return _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "application/pdf; charset=binary"},
            body=payload,
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/docs/spec.pdf"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["spec.pdf"]
        assert atts[0].content_type == "application/pdf"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()
