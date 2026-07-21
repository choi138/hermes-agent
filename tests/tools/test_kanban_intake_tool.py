from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.request_footprint import canonical_tool_schema_metrics
from gateway.session_context import (
    clear_session_vars,
    reset_session_vars,
    set_session_vars,
)
from hermes_cli import kanban_db as kb
from tools import kanban_intake_tool as intake


@pytest.fixture
def intake_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        intake,
        "load_config",
        lambda: {
            "toolsets": ["kanban"],
            "kanban": {
                "intake_assignee": "default",
                "default_assignee": "unused-fallback",
            },
        },
    )
    reset_session_vars()
    kb.init_db()
    yield home
    reset_session_vars()


def _bind_discord_source(
    *,
    message_id="message-1",
    session_id="session-1",
    wake=None,
    trusted=True,
    chat_id="thread-1",
    thread_id="thread-1",
    user_id="user-1",
    scope_id="guild-1",
    parent_chat_id="channel-1",
):
    return set_session_vars(
        platform="discord",
        profile="default",
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        session_key=f"discord:{chat_id}",
        session_id=session_id,
        message_id=message_id,
        scope_id=scope_id,
        parent_chat_id=parent_chat_id,
        trusted_gateway_source=trusted,
        dispatch_wake=wake,
    )


def _call(args):
    return json.loads(intake._handle_kanban_task(args))


def _create_from_message(title, message_id):
    tokens = _bind_discord_source(message_id=message_id)
    try:
        return _call({"title": title})
    finally:
        clear_session_vars(tokens)


def test_schema_exposes_only_user_owned_intake_fields():
    parameters = intake.KANBAN_TASK_SCHEMA["parameters"]
    properties = set(parameters["properties"])
    assert properties == {
        "operation",
        "task_id",
        "title",
        "body",
        "priority",
        "goal_mode",
        "goal_max_turns",
        "max_retries",
        "max_runtime_seconds",
    }
    assert parameters["additionalProperties"] is False
    assert not properties & {
        "actor",
        "profile",
        "assignee",
        "board",
        "idempotency_key",
        "notifier_profile",
        "guild_id",
        "channel_id",
        "thread_id",
        "message_id",
        "session_id",
    }
    operation = parameters["properties"]["operation"]
    assert operation["enum"] == ["create", "status", "update", "retry"]
    assert operation["default"] == "create"
    # The operations have mutually exclusive required fields, so neither can
    # be unconditional at the top level.  Keep the compact model-facing
    # contract explicit while handler tests enforce each branch below.
    assert "required" not in parameters
    description = intake.KANBAN_TASK_SCHEMA["description"]
    assert "Create needs title" in description
    assert "status/update/retry need task_id" in description
    assert "update allows title/body/priority" in description


def test_schema_keeps_single_intake_surface_within_750_byte_budget():
    metrics = canonical_tool_schema_metrics(
        [{"type": "function", "function": intake.KANBAN_TASK_SCHEMA}]
    )

    assert metrics.count == 1
    assert metrics.json_bytes <= 750


def test_handler_fails_closed_without_trusted_gateway_context(intake_env, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "default")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "forged-message")

    result = _call({"title": "Must not be created"})

    assert "error" in result
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["status", "update", "retry"])
def test_non_create_operations_require_task_id(intake_env, operation):
    tokens = _bind_discord_source()
    try:
        result = _call({"operation": operation})
    finally:
        clear_session_vars(tokens)

    assert result["error"] == f"task_id is required for {operation}"


def test_omitted_operation_and_explicit_create_share_create_behavior(intake_env):
    tokens = _bind_discord_source(message_id="implicit-create")
    try:
        implicit = _call({"title": "Implicit create"})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(message_id="explicit-create")
    try:
        explicit = _call({"operation": "create", "title": "Explicit create"})
    finally:
        clear_session_vars(tokens)

    assert implicit["ok"] is True
    assert explicit["ok"] is True
    assert implicit["task_id"] != explicit["task_id"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


def test_create_rejects_task_id_instead_of_silently_ignoring_it(intake_env):
    tokens = _bind_discord_source()
    try:
        result = _call(
            {
                "operation": "create",
                "task_id": "t_existing",
                "title": "Do not ignore a target id",
            }
        )
    finally:
        clear_session_vars(tokens)

    assert result["error"] == "create does not accept field(s): task_id"
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_untrusted_session_binding_does_not_authorize_intake(intake_env):
    tokens = _bind_discord_source(trusted=False)
    try:
        result = _call({"title": "Must not be created"})
    finally:
        clear_session_vars(tokens)

    assert "error" in result


def test_success_uses_server_identity_and_wakes_only_after_commit(intake_env):
    callback_observations = []

    def wake():
        with kb.connect_closing() as conn:
            callback_observations.append(
                (
                    conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM kanban_intake_receipts"
                    ).fetchone()[0],
                    conn.execute(
                        "SELECT COUNT(*) FROM kanban_notify_subs"
                    ).fetchone()[0],
                )
            )

    tokens = _bind_discord_source(wake=wake)
    try:
        result = _call(
            {
                "title": "  Ship intake safely  ",
                "body": "  Preserve goal and retry controls.  ",
                "priority": 9,
                "goal_mode": True,
                "goal_max_turns": 5,
                "max_retries": 1,
                "max_runtime_seconds": 600,
            }
        )
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["assignee"] == "default"
    assert result["deduplicated"] is False
    assert result["subscribed"] is True
    assert result["dispatcher_woken"] is True
    assert callback_observations == [(1, 1, 1)]

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
        receipt = conn.execute("SELECT * FROM kanban_intake_receipts").fetchone()
        sub = conn.execute("SELECT * FROM kanban_notify_subs").fetchone()
    assert task.title == "Ship intake safely"
    assert task.body == "Preserve goal and retry controls."
    assert task.assignee == "default"
    assert task.created_by == "default"
    assert task.priority == 9
    assert task.goal_mode is True
    assert task.goal_max_turns == 5
    assert task.max_retries == 1
    assert task.max_runtime_seconds == 600
    assert receipt["actor_profile"] == "default"
    assert sub["chat_id"] == "thread-1"
    assert sub["thread_id"] == "thread-1"


def test_goal_mode_requires_explicit_positive_turn_budget(intake_env):
    tokens = _bind_discord_source()
    try:
        result = _call({"title": "Open-ended work", "goal_mode": True})
    finally:
        clear_session_vars(tokens)

    assert "goal_max_turns must be >= 1" in result["error"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_same_message_and_request_deduplicate_but_new_message_does_not(intake_env):
    tokens = _bind_discord_source(message_id="message-1")
    try:
        first = _call({"title": "Investigate latency"})
        retry = _call({"title": "Investigate latency"})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(
        message_id="message-1",
        session_id="session-after-restart",
    )
    try:
        restart_retry = _call({"title": "Investigate latency"})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(message_id="message-2")
    try:
        second_message = _call({"title": "Investigate latency"})
    finally:
        clear_session_vars(tokens)

    assert first["task_id"] == retry["task_id"]
    assert retry["deduplicated"] is True
    assert restart_retry["task_id"] == first["task_id"]
    assert restart_retry["deduplicated"] is True
    assert second_message["task_id"] != first["task_id"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_receipts"
        ).fetchone()[0] == 2


def test_same_message_with_changed_request_fails_closed(intake_env):
    tokens = _bind_discord_source(message_id="message-1")
    try:
        first = _call({"title": "Investigate latency"})
        conflict = _call({"title": "Deploy without verification"})
    finally:
        clear_session_vars(tokens)

    assert first["ok"] is True
    assert "error" in conflict
    assert "different immutable content" in conflict["error"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_receipts"
        ).fetchone()[0] == 1


def test_status_returns_owned_task_and_latest_run_summary(intake_env):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call(
            {
                "title": "Report durable status",
                "body": "Return only safe task and run fields.",
                "priority": 4,
            }
        )
    finally:
        clear_session_vars(tokens)

    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, created["task_id"], claimer="status-test")
        assert kb.complete_task(
            conn,
            created["task_id"],
            result="Completed safely",
            summary="Implemented the requested status path.",
        )

    wake_calls = []
    tokens = _bind_discord_source(
        message_id="status-message",
        wake=lambda: wake_calls.append(True),
    )
    try:
        result = _call({"operation": "status", "task_id": created["task_id"]})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["operation"] == "status"
    assert result["task"] == {
        "id": created["task_id"],
        "title": "Report durable status",
        "body": "Return only safe task and run fields.",
        "status": "done",
        "priority": 4,
        "goal_mode": False,
        "goal_max_turns": None,
        "max_retries": None,
        "max_runtime_seconds": None,
    }
    assert result["latest_run"]["status"] == "done"
    assert result["latest_run"]["outcome"] == "completed"
    assert result["latest_run"]["summary"] == (
        "Implemented the requested status path."
    )
    assert result["latest_summary"] == "Implemented the requested status path."
    assert not {
        "profile",
        "assignee",
        "created_by",
        "idempotency_key",
        "claim_lock",
        "worker_pid",
    } & set(result["task"])
    assert wake_calls == []


def test_status_denies_other_discord_actor_or_conversation(intake_env):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call({"title": "Private intake task"})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(message_id="other-actor", user_id="user-2")
    try:
        other_actor = _call(
            {"operation": "status", "task_id": created["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(
        message_id="other-conversation",
        chat_id="thread-2",
        thread_id="thread-2",
    )
    try:
        other_conversation = _call(
            {"operation": "status", "task_id": created["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    expected = "kanban_task: task not found or not owned by this Discord conversation"
    assert other_actor["error"] == expected
    assert other_conversation["error"] == expected


def test_update_changes_only_safe_fields_for_owner(intake_env):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call(
            {
                "title": "Original title",
                "body": "Original body",
                "priority": 1,
                "max_retries": 3,
            }
        )
    finally:
        clear_session_vars(tokens)

    wake_calls = []
    tokens = _bind_discord_source(
        message_id="update-message",
        wake=lambda: wake_calls.append(True),
    )
    try:
        result = _call(
            {
                "operation": "update",
                "task_id": created["task_id"],
                "title": "  Revised title  ",
                "body": "  Revised body  ",
                "priority": 8,
            }
        )
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["operation"] == "update"
    assert result["updated_fields"] == ["body", "priority", "title"]
    assert result["task"]["title"] == "Revised title"
    assert result["task"]["body"] == "Revised body"
    assert result["task"]["priority"] == 8
    assert result["task"]["status"] == "ready"
    assert wake_calls == []
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, created["task_id"])
        events = kb.list_events(conn, created["task_id"])
    assert task.max_retries == 3
    assert events[-1].kind == "edited"
    assert events[-1].payload == {"fields": ["body", "priority", "title"]}


@pytest.mark.parametrize(
    "source_overrides",
    [
        {"user_id": "user-2"},
        {"chat_id": "thread-2", "thread_id": "thread-2"},
    ],
)
def test_update_denies_other_actor_or_conversation(
    intake_env, source_overrides
):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call({"title": "Owner title", "priority": 2})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(
        message_id="unauthorized-update",
        **source_overrides,
    )
    try:
        result = _call(
            {
                "operation": "update",
                "task_id": created["task_id"],
                "title": "Unauthorized title",
                "priority": 99,
            }
        )
    finally:
        clear_session_vars(tokens)

    assert result["error"] == (
        "kanban_task: task not found or not owned by this Discord conversation"
    )
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, created["task_id"])
    assert task.title == "Owner title"
    assert task.priority == 2


def test_update_rejects_unsafe_fields_and_empty_patch(intake_env):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call({"title": "Safe fields only", "max_retries": 2})
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(message_id="unsafe-update")
    try:
        unsafe = _call(
            {
                "operation": "update",
                "task_id": created["task_id"],
                "max_retries": 9,
            }
        )
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(message_id="empty-update")
    try:
        empty = _call({"operation": "update", "task_id": created["task_id"]})
    finally:
        clear_session_vars(tokens)

    assert unsafe["error"] == "update does not accept field(s): max_retries"
    assert empty["error"] == "update requires title, body, or priority"
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, created["task_id"])
    assert task.max_retries == 2


def test_update_rejects_running_and_terminal_tasks(intake_env):
    tokens = _bind_discord_source(message_id="create-message")
    try:
        created = _call({"title": "Immutable while active"})
    finally:
        clear_session_vars(tokens)

    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, created["task_id"], claimer="update-test")

    tokens = _bind_discord_source(message_id="running-update")
    try:
        running = _call(
            {
                "operation": "update",
                "task_id": created["task_id"],
                "title": "Running rewrite",
            }
        )
    finally:
        clear_session_vars(tokens)

    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, created["task_id"], summary="Finished")

    tokens = _bind_discord_source(message_id="done-update")
    try:
        done = _call(
            {
                "operation": "update",
                "task_id": created["task_id"],
                "title": "Done rewrite",
            }
        )
    finally:
        clear_session_vars(tokens)

    assert running["error"] == "kanban_task: cannot update task in running state"
    assert done["error"] == "kanban_task: cannot update task in done state"
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, created["task_id"])
    assert task.title == "Immutable while active"


def test_retry_requeues_blocked_task_and_wakes_after_commit(intake_env):
    created = _create_from_message("Retry blocked work", "create-blocked")
    with kb.connect_closing() as conn:
        assert kb.block_task(
            conn,
            created["task_id"],
            reason="Waiting for input",
            kind="needs_input",
        )

    callback_observations = []

    def wake():
        with kb.connect_closing() as conn:
            callback_observations.append(
                (
                    kb.get_task(conn, created["task_id"]).status,
                    kb.list_events(conn, created["task_id"])[-1].kind,
                )
            )

    tokens = _bind_discord_source(message_id="retry-blocked", wake=wake)
    try:
        result = _call({"operation": "retry", "task_id": created["task_id"]})
    finally:
        clear_session_vars(tokens)

    assert result["ok"] is True
    assert result["operation"] == "retry"
    assert result["retried_from"] == "blocked"
    assert result["task"]["status"] == "ready"
    assert result["dispatcher_woken"] is True
    assert callback_observations == [("ready", "unblocked")]


def test_retry_requeues_triage_and_failed_run_tasks(intake_env):
    triage = _create_from_message("Retry triage work", "create-triage")
    failed = _create_from_message("Retry failed work", "create-failed")

    with kb.connect_closing() as conn:
        assert kb.block_task(
            conn,
            triage["task_id"],
            reason="Need input",
            kind="needs_input",
        )
        assert kb.unblock_task(conn, triage["task_id"])
        assert kb.block_task(
            conn,
            triage["task_id"],
            reason="Still need input",
            kind="needs_input",
        )
        assert kb.get_task(conn, triage["task_id"]).status == "triage"

        assert kb.claim_task(conn, failed["task_id"], claimer="retry-test")
        assert not kb._record_spawn_failure(
            conn,
            failed["task_id"],
            "worker failed to start",
            failure_limit=2,
        )
        assert kb.get_task(conn, failed["task_id"]).status == "ready"
        assert kb.latest_run(conn, failed["task_id"]).outcome == "spawn_failed"

    wake_calls = []
    tokens = _bind_discord_source(
        message_id="retry-triage",
        wake=lambda: wake_calls.append("triage"),
    )
    try:
        triage_result = _call(
            {"operation": "retry", "task_id": triage["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    tokens = _bind_discord_source(
        message_id="retry-failed",
        wake=lambda: wake_calls.append("failed"),
    )
    try:
        failed_result = _call(
            {"operation": "retry", "task_id": failed["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    assert triage_result["retried_from"] == "triage"
    assert triage_result["task"]["status"] == "ready"
    assert failed_result["retried_from"] == "failed"
    assert failed_result["task"]["status"] == "ready"
    assert wake_calls == ["triage", "failed"]
    with kb.connect_closing() as conn:
        failed_task = kb.get_task(conn, failed["task_id"])
        failed_events = kb.list_events(conn, failed["task_id"])
    assert failed_task.consecutive_failures == 0
    assert failed_task.last_failure_error is None
    assert failed_events[-1].kind == "retried"


def test_retry_rejects_ready_running_done_and_foreign_tasks(intake_env):
    ready = _create_from_message("Already ready", "create-ready")
    running = _create_from_message("Already running", "create-running")
    done = _create_from_message("Already done", "create-done")
    foreign = _create_from_message("Foreign blocked", "create-foreign")

    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, running["task_id"], claimer="retry-test")
        assert kb.complete_task(conn, done["task_id"], summary="Done")
        assert kb.block_task(conn, foreign["task_id"], reason="Blocked")

    wake_calls = []

    def retry(task, message_id, **source_overrides):
        tokens = _bind_discord_source(
            message_id=message_id,
            wake=lambda: wake_calls.append(message_id),
            **source_overrides,
        )
        try:
            return _call({"operation": "retry", "task_id": task["task_id"]})
        finally:
            clear_session_vars(tokens)

    ready_result = retry(ready, "retry-ready")
    running_result = retry(running, "retry-running")
    done_result = retry(done, "retry-done")
    foreign_result = retry(foreign, "retry-foreign", user_id="user-2")

    assert ready_result["error"] == "kanban_task: cannot retry task in ready state"
    assert running_result["error"] == (
        "kanban_task: cannot retry task in running state"
    )
    assert done_result["error"] == "kanban_task: cannot retry task in done state"
    assert foreign_result["error"] == (
        "kanban_task: task not found or not owned by this Discord conversation"
    )
    assert wake_calls == []


def test_update_replay_is_durable_and_changed_payload_conflicts(intake_env):
    created = _create_from_message("Idempotent update", "create-update-replay")
    tokens = _bind_discord_source(message_id="update-replay")
    try:
        request = {
            "operation": "update",
            "task_id": created["task_id"],
            "title": "Updated exactly once",
            "priority": 7,
        }
        first = _call(request)
        replay = _call(request)
        conflict = _call({**request, "priority": 8})
    finally:
        clear_session_vars(tokens)

    assert first["ok"] is True
    assert first["deduplicated"] is False
    assert replay["ok"] is True
    assert replay["deduplicated"] is True
    assert replay["updated_fields"] == first["updated_fields"]
    assert replay["task"] == first["task"]
    assert "different immutable content" in conflict["error"]

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, created["task_id"])
        edited = [
            event
            for event in kb.list_events(conn, created["task_id"])
            if event.kind == "edited"
        ]
        receipt = conn.execute(
            "SELECT operation, task_id FROM kanban_intake_operations"
        ).fetchone()
    assert task.priority == 7
    assert len(edited) == 1
    assert (receipt["operation"], receipt["task_id"]) == (
        "update",
        created["task_id"],
    )


def test_retry_replay_does_not_requeue_twice_and_changed_task_conflicts(intake_env):
    first_task = _create_from_message("First retry target", "create-first-retry")
    second_task = _create_from_message("Second retry target", "create-second-retry")
    with kb.connect_closing() as conn:
        assert kb.block_task(conn, first_task["task_id"], reason="Blocked once")
        assert kb.block_task(conn, second_task["task_id"], reason="Still blocked")

    tokens = _bind_discord_source(message_id="retry-replay")
    try:
        request = {"operation": "retry", "task_id": first_task["task_id"]}
        first = _call(request)
        replay = _call(request)
        conflict = _call(
            {"operation": "retry", "task_id": second_task["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    assert first["ok"] is True
    assert first["deduplicated"] is False
    assert replay["ok"] is True
    assert replay["deduplicated"] is True
    assert replay["retried_from"] == "blocked"
    assert "different immutable content" in conflict["error"]
    with kb.connect_closing() as conn:
        first_events = kb.list_events(conn, first_task["task_id"])
        first_state = kb.get_task(conn, first_task["task_id"]).status
        second_state = kb.get_task(conn, second_task["task_id"]).status
    assert sum(event.kind == "unblocked" for event in first_events) == 1
    assert first_state == "ready"
    assert second_state == "blocked"


def test_create_and_operations_share_one_message_receipt_namespace(intake_env):
    target = _create_from_message("Existing target", "target-create")

    tokens = _bind_discord_source(message_id="operation-first")
    try:
        status = _call({"operation": "status", "task_id": target["task_id"]})
        create_after_operation = _call({"title": "Must not reuse operation key"})
    finally:
        clear_session_vars(tokens)

    created_first = _create_from_message("Created first", "create-first")
    tokens = _bind_discord_source(message_id="create-first")
    try:
        operation_after_create = _call(
            {"operation": "status", "task_id": created_first["task_id"]}
        )
    finally:
        clear_session_vars(tokens)

    assert status["ok"] is True
    assert "different immutable content" in create_after_operation["error"]
    assert "different immutable content" in operation_after_create["error"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_receipts"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_intake_operations"
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "server_owned_field",
    ["assignee", "board", "idempotency_key", "notifier_profile", "message_id"],
)
def test_model_cannot_supply_server_owned_fields(intake_env, server_owned_field):
    tokens = _bind_discord_source()
    try:
        result = _call(
            {"title": "Attempt privilege injection", server_owned_field: "attacker"}
        )
    finally:
        clear_session_vars(tokens)

    assert "error" in result
    assert server_owned_field in result["error"]
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_non_discord_trusted_gateway_turn_is_rejected(intake_env):
    tokens = set_session_vars(
        platform="telegram",
        profile="default",
        chat_id="chat-1",
        user_id="user-1",
        message_id="message-1",
        trusted_gateway_source=True,
    )
    try:
        result = _call({"title": "Discord-only intake"})
    finally:
        clear_session_vars(tokens)

    assert "error" in result
