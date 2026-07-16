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
    *, message_id="message-1", session_id="session-1", wake=None, trusted=True
):
    return set_session_vars(
        platform="discord",
        profile="default",
        chat_id="thread-1",
        thread_id="thread-1",
        user_id="user-1",
        session_key="discord:thread-1",
        session_id=session_id,
        message_id=message_id,
        scope_id="guild-1",
        parent_chat_id="channel-1",
        trusted_gateway_source=trusted,
        dispatch_wake=wake,
    )


def _call(args):
    return json.loads(intake._handle_kanban_task(args))


def test_schema_exposes_only_user_owned_intake_fields():
    properties = set(intake.KANBAN_TASK_SCHEMA["parameters"]["properties"])
    assert properties == {
        "title",
        "body",
        "priority",
        "goal_mode",
        "goal_max_turns",
        "max_retries",
        "max_runtime_seconds",
    }
    assert intake.KANBAN_TASK_SCHEMA["parameters"]["additionalProperties"] is False
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
