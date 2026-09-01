"""Tests: the dispatcher writes the local worker execution contract on spawn.

``hermes_cli/kanban_runtime.py`` defines the contract and both ``cli.py`` and
``hermes_cli/env_loader.py`` reapply it after the assignee profile's terminal
config is bridged — but the dispatcher never wrote it, so the whole mechanism
was inert: ``_HERMES_KANBAN_EXECUTION_BACKEND`` was absent from every worker
env and a profile with ``terminal.backend: ssh`` sent this host's absolute
workspace path to another machine.

The worker also inherited ``_HERMES_GATEWAY=1`` from the dispatching gateway,
which makes cli.py's bridge skip the ``TERMINAL_CWD`` export it needs.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli.kanban_runtime import (
    KANBAN_EXECUTION_BACKEND_ENV,
    KANBAN_LOCAL_EXECUTION_BACKEND,
    KanbanExecutionContractError,
)


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_contract",
        title="contract",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


@pytest.fixture()
def kb(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8"
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as module

    monkeypatch.setattr(module, "_resolve_hermes_argv", lambda: ["hermes"])
    return module


def _spawn(kb, monkeypatch, workspace: str) -> dict:
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(_make_task(kb), workspace)
    return captured


def test_spawn_binds_local_execution_contract(kb, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _spawn(kb, monkeypatch, str(workspace))

    env = captured["env"]
    assert env[KANBAN_EXECUTION_BACKEND_ENV] == KANBAN_LOCAL_EXECUTION_BACKEND
    assert env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert env["TERMINAL_ENV"] == KANBAN_LOCAL_EXECUTION_BACKEND


def test_spawn_drops_the_gateway_role_marker(kb, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    captured = _spawn(kb, monkeypatch, str(workspace))

    assert "_HERMES_GATEWAY" not in captured["env"]


def test_spawn_drops_an_inherited_remote_terminal_location(kb, monkeypatch, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "gateway.example.com")

    captured = _spawn(kb, monkeypatch, str(workspace))

    env = captured["env"]
    assert env["TERMINAL_ENV"] == KANBAN_LOCAL_EXECUTION_BACKEND
    assert "TERMINAL_SSH_HOST" not in env


def test_spawn_fails_closed_when_the_workspace_is_not_local(kb, monkeypatch, tmp_path):
    """A workspace that cannot be pinned must stop the worker, not run it anyway."""
    with pytest.raises(KanbanExecutionContractError):
        _spawn(kb, monkeypatch, "relative/workspace")
