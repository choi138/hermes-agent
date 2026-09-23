"""Dispatcher-owned workspaces must execute on the dispatcher's local host."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


def _task() -> kb.Task:
    return kb.Task(
        id="t_contract", title="contract", body=None, assignee="worker",
        status="running", priority=0, created_by="test", created_at=1,
        started_at=None, completed_at=None, workspace_kind="dir",
        workspace_path=None, claim_lock="lock", claim_expires=None,
        tenant=None, current_run_id=1,
    )


def _spawn(monkeypatch, tmp_path, workspace):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_task(), str(workspace))
    return captured


def test_dispatcher_pins_local_backend_and_drops_gateway_role(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "remote.example")

    captured = _spawn(monkeypatch, tmp_path, workspace)
    env = captured["env"]
    assert captured["cwd"] == str(workspace)
    assert env["_HERMES_KANBAN_EXECUTION_BACKEND"] == "local"
    assert env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert env["TERMINAL_ENV"] == "local"
    assert env["TERMINAL_CWD"] == str(workspace)
    assert "TERMINAL_SSH_HOST" not in env
    assert "_HERMES_GATEWAY" not in env


def test_dispatcher_refuses_nonlocal_workspace(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="workspace/backend mismatch"):
        _spawn(monkeypatch, tmp_path, "relative/workspace")


def test_profile_bridge_reapplies_worker_contract(monkeypatch, tmp_path):
    from hermes_cli.kanban_runtime import apply_worker_execution_contract

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    env = {
        "HERMES_KANBAN_TASK": "t_contract",
        "HERMES_KANBAN_WORKSPACE": str(workspace),
        "_HERMES_KANBAN_EXECUTION_BACKEND": "local",
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "remote.example",
    }
    terminal_config = {"backend": "ssh", "env_type": "ssh", "cwd": "/remote"}

    apply_worker_execution_contract(env, terminal_config=terminal_config)

    assert env["TERMINAL_ENV"] == "local"
    assert env["TERMINAL_CWD"] == str(workspace)
    assert "TERMINAL_SSH_HOST" not in env
    assert terminal_config["backend"] == "local"
    assert terminal_config["cwd"] == str(workspace)


def test_worker_contract_rejects_wrong_process_cwd(monkeypatch, tmp_path):
    from hermes_cli.kanban_runtime import apply_worker_execution_contract

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    env = {
        "HERMES_KANBAN_TASK": "t_contract",
        "HERMES_KANBAN_WORKSPACE": str(workspace),
        "_HERMES_KANBAN_EXECUTION_BACKEND": "local",
    }
    with pytest.raises(RuntimeError, match="workspace/backend mismatch"):
        apply_worker_execution_contract(env)


def test_worker_startup_and_reload_keep_profile_ssh_off_local_workspace(tmp_path):
    """The actual CLI import and a later dotenv reload both keep execution local."""
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /remote\n", encoding="utf-8"
    )
    (home / ".env").write_text(
        "_HERMES_GATEWAY=1\n"
        "_HERMES_KANBAN_EXECUTION_BACKEND=ssh\n"
        "HERMES_KANBAN_TASK=forged\n"
        "HERMES_KANBAN_WORKSPACE=/remote\n"
        "TERMINAL_ENV=ssh\n"
        "TERMINAL_SSH_HOST=remote.example\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_TASK": "t_contract",
        "HERMES_KANBAN_WORKSPACE": str(workspace),
        "_HERMES_KANBAN_EXECUTION_BACKEND": "local",
    })
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(Path(__file__).resolve().parents[2]), env.get("PYTHONPATH", "")) if part
    )
    env.pop("_HERMES_GATEWAY", None)
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json, os, cli; "
            "from hermes_cli.env_loader import load_hermes_dotenv; "
            "load_hermes_dotenv(load_external_secrets=False); "
            "print(json.dumps({"
            "'task': os.environ.get('HERMES_KANBAN_TASK'), "
            "'backend': os.environ.get('TERMINAL_ENV'), "
            "'cwd': os.environ.get('TERMINAL_CWD'), "
            "'ssh_host': os.environ.get('TERMINAL_SSH_HOST'), "
            "'gateway': os.environ.get('_HERMES_GATEWAY'), "
            "'config': cli.CLI_CONFIG['terminal']}))"
        )],
        cwd=workspace, env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state["task"] == "t_contract"
    assert state["backend"] == "local"
    assert state["cwd"] == str(workspace)
    assert state["ssh_host"] is None
    assert state["gateway"] is None
    assert state["config"]["backend"] == "local"
    assert state["config"]["cwd"] == str(workspace)


def test_profile_dotenv_cannot_create_dispatcher_execution_marker(monkeypatch, tmp_path):
    from hermes_cli.env_loader import load_hermes_dotenv

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text(
        "_HERMES_KANBAN_EXECUTION_BACKEND=local\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("_HERMES_KANBAN_EXECUTION_BACKEND", raising=False)

    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)

    assert "_HERMES_KANBAN_EXECUTION_BACKEND" not in os.environ
