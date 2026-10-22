"""Owner-run Kanban turns keep the fork's dispatcher contract: local backend in the card workspace.

The dispatcher materializes the workspace on its own host, so a profile whose terminal is ssh
(production) must still run the card's tools locally there. The pin lives in the session's frozen
terminal policy, bound per turn, never in the owner's process environment.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_card(kanban_home, workspace_path):
    profile = kanban_home / "profiles" / "elias"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="elias",
                             workspace_kind="dir", workspace_path=workspace_path)
        task = kb.claim_task(conn, tid)
    from gateway.session_contract import Principal
    actor = Principal("owner", str(profile), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(profile)), actor=actor,
                                 native_owner=True)
    params = {"board": kb.get_current_board(), "task_id": task.id, "run_id": task.current_run_id,
              "claim_lock": task.claim_lock, "db": str(kb.kanban_db_path())}
    return connection, params


def test_owner_policy_pins_local_backend_over_profile_ssh(kanban_home, tmp_path, monkeypatch):
    from gateway.session_kanban import build_kanban_policy
    from gateway.session_policy import policy_scope
    from tools.terminal_scope import get_terminal_scope, terminal_env

    (kanban_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /remote\n  ssh_host: remote.example\n", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    connection, params = _claimed_card(kanban_home, str(workspace))
    config = {"model": {"default": "profile-model", "provider": "custom"},
              "platform_toolsets": {"cli": ["terminal"]},
              "terminal": {"backend": "ssh", "cwd": "/remote", "ssh_host": "remote.example",
                           "ssh_user": "hermes", "persistent_shell": True, "timeout": 180}}
    import gateway.run  # its import-time config bridge belongs to owner startup, not admission
    cwd_before = os.environ.get("TERMINAL_CWD")

    policy, private = build_kanban_policy(connection, params, config)

    terminal = json.loads(policy.terminal_json)
    assert terminal["TERMINAL_ENV"] == "local"
    assert terminal["TERMINAL_CWD"] == str(workspace) == policy.cwd
    assert not [k for k in terminal if k.startswith("TERMINAL_SSH_")]
    assert not [path for path in private if path[0] is None and path[1] not in terminal]
    assert (os.environ.get("TERMINAL_ENV"), os.environ.get("TERMINAL_CWD")) == ("ssh", cwd_before)
    # The worker binds the whole frozen config: its terminal section must not still say ssh.
    frozen = json.loads(policy.config_json)["terminal"]
    assert frozen == {"backend": "local", "env_type": "local", "cwd": str(workspace), "timeout": 180}
    assert not [path for path in private if path[0] == "terminal"]

    with policy_scope(policy):
        assert terminal_env("TERMINAL_ENV") == "local"
        assert terminal_env("TERMINAL_CWD") == str(workspace)
    assert get_terminal_scope() is None
    assert os.environ["TERMINAL_ENV"] == "ssh"


@pytest.mark.parametrize("workspace_path", [None, "relative/ws", "/nonexistent/kanban/ws"])
def test_owner_refuses_card_without_dispatcher_local_workspace(kanban_home, workspace_path):
    from gateway.session_kanban import build_kanban_policy
    from hermes_state_runtime import RuntimeStoreError

    connection, params = _claimed_card(kanban_home, workspace_path)
    with pytest.raises(RuntimeStoreError, match="invalid_kanban_claim") as excinfo:
        build_kanban_policy(connection, params, {})
    assert "workspace/backend mismatch" in (excinfo.value.detail or "")


def _frame(workspace, terminal, cwd=None, config_terminal=None):
    context = {"task_id": "t1", "board": "default", "db": "/nonexistent/kanban.db", "run_id": 1,
               "claim_lock": "lock", "workspace": str(workspace)}
    if config_terminal is None:
        config_terminal = {"backend": "local", "cwd": str(workspace)}
    return {"policy": {"kanban_json": json.dumps(context), "source": "kanban",
                       "cwd": str(cwd or workspace), "terminal_json": json.dumps(terminal),
                       "config_json": json.dumps({"terminal": config_terminal})}}


_LOCAL = {"TERMINAL_ENV": "local", "TERMINAL_CWD": "{ws}"}


@pytest.mark.parametrize("terminal, cwd, config_terminal", [
    ({"TERMINAL_ENV": "ssh", "TERMINAL_CWD": "{ws}"}, None, None),
    ({"TERMINAL_ENV": "local", "TERMINAL_CWD": "/elsewhere"}, None, None),
    ({**_LOCAL, "TERMINAL_SSH_HOST": "remote.example"}, None, None),
    (_LOCAL, "/elsewhere", None),
    (_LOCAL, None, {"backend": "ssh", "cwd": "{ws}"}),
    (_LOCAL, None, {"backend": "local", "cwd": "{ws}", "ssh_host": "remote.example"}),
    (_LOCAL, None, {}),
])
def test_worker_refuses_turn_whose_policy_breaks_the_contract(tmp_path, monkeypatch, terminal, cwd,
                                                              config_terminal):
    from gateway.session_kanban import bind_worker_context
    from hermes_cli.kanban_runtime import KanbanExecutionContractError

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    terminal = {k: v.replace("{ws}", str(tmp_path)) for k, v in terminal.items()}
    if config_terminal:
        config_terminal = {k: v.replace("{ws}", str(tmp_path)) for k, v in config_terminal.items()}
    with pytest.raises(KanbanExecutionContractError, match="workspace/backend mismatch"):
        bind_worker_context(_frame(tmp_path, terminal, cwd, config_terminal))
    assert "HERMES_KANBAN_TASK" not in os.environ


def test_built_policy_passes_the_worker_contract(kanban_home, tmp_path, monkeypatch):
    """A policy admitted from an ssh profile, hydrated as ``_bootstrap`` does, binds in the worker."""
    from dataclasses import asdict
    from gateway import session_kanban

    (kanban_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  cwd: /remote\n  ssh_host: remote.example\n"
        "  docker_env: {TOKEN: secret}\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    connection, params = _claimed_card(kanban_home, str(workspace))
    config = {"model": {"default": "profile-model", "provider": "custom"},
              "terminal": {"backend": "ssh", "cwd": "/remote", "ssh_host": "remote.example",
                           "docker_env": {"TOKEN": "secret"}}}
    import gateway.run  # its import-time config bridge belongs to owner startup
    policy, private = session_kanban.build_kanban_policy(connection, params, config)

    terminal = json.loads(policy.terminal_json)
    for path, value in private.items():  # _bootstrap's recovery of the stored private secrets
        if path[0] is None:
            terminal[path[1]] = value
    hydrated = asdict(policy)
    hydrated["terminal_json"] = json.dumps(terminal)
    monkeypatch.setattr(session_kanban, "_watch_submitter", lambda db, run_id: None)
    monkeypatch.setattr(os, "chdir", lambda path: None)
    monkeypatch.setattr(os, "environ", dict(os.environ))

    session_kanban.bind_worker_context({"policy": hydrated})

    assert os.environ["HERMES_KANBAN_TASK"] == params["task_id"]
    assert os.environ["HERMES_KANBAN_WORKSPACE"] == str(workspace)


def _isolate_client_env(monkeypatch):
    """``_submit`` pops these from os.environ; unset them first so monkeypatch restores them."""
    for key in list(os.environ):
        if key.startswith(("HERMES_KANBAN_", "_HERMES_KANBAN_", "TERMINAL_")):
            monkeypatch.delenv(key)
    for key in ("HERMES_SESSION_SOURCE", "HERMES_TENANT"):
        monkeypatch.delenv(key, raising=False)


def test_client_does_not_leak_dispatcher_local_pin_into_owner_start(tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_client

    _isolate_client_env(monkeypatch)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": "t1",
                       "HERMES_KANBAN_RUN_ID": "1", "HERMES_KANBAN_CLAIM_LOCK": "lock",
                       "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
                       "HERMES_KANBAN_WORKSPACE": str(workspace),
                       "_HERMES_KANBAN_EXECUTION_BACKEND": "local",
                       "TERMINAL_ENV": "local", "TERMINAL_CWD": str(workspace)}.items():
        monkeypatch.setenv(key, value)
    seen = {}

    async def owner_start(params, board_db):
        seen.update({k: os.environ.get(k) for k in
                     ("TERMINAL_ENV", "TERMINAL_CWD", "_HERMES_KANBAN_EXECUTION_BACKEND")})
        return 0

    monkeypatch.setattr(kanban_worker_client, "run", owner_start)
    assert kanban_worker_client.main() == 0
    assert seen == {"TERMINAL_ENV": None, "TERMINAL_CWD": None, "_HERMES_KANBAN_EXECUTION_BACKEND": None}


def test_client_keeps_terminal_env_it_was_not_pinned_with(tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_client

    _isolate_client_env(monkeypatch)
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": "t1",
                       "HERMES_KANBAN_RUN_ID": "1", "HERMES_KANBAN_CLAIM_LOCK": "lock",
                       "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"), "TERMINAL_ENV": "ssh"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("_HERMES_KANBAN_EXECUTION_BACKEND", raising=False)
    seen = {}

    async def owner_start(params, board_db):
        seen["TERMINAL_ENV"] = os.environ.get("TERMINAL_ENV")
        return 0

    monkeypatch.setattr(kanban_worker_client, "run", owner_start)
    assert kanban_worker_client.main() == 0
    assert seen == {"TERMINAL_ENV": "ssh"}


def test_client_hands_an_env_only_ssh_backend_back_to_the_owner(tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_client
    from hermes_cli.kanban_runtime import bind_local_worker_execution

    _isolate_client_env(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # The launch profile's ssh policy lives only in the environment (systemd / op run).
    env = {"HERMES_KANBAN_TASK": "t1", "TERMINAL_ENV": "ssh", "TERMINAL_CWD": "/remote",
           "TERMINAL_SSH_HOST": "remote.example", "TERMINAL_PERSISTENT_SHELL": "true"}
    bind_local_worker_execution(env, str(workspace))
    assert (env["TERMINAL_ENV"], env["TERMINAL_CWD"]) == ("local", str(workspace))
    assert "TERMINAL_SSH_HOST" not in env
    env.update({"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_RUN_ID": "1",
                "HERMES_KANBAN_CLAIM_LOCK": "lock", "HERMES_KANBAN_DB": str(tmp_path / "kanban.db")})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    seen = {}

    async def owner_start(params, board_db):
        seen.update({k: v for k, v in os.environ.items()
                     if k.startswith(("TERMINAL_", "HERMES_KANBAN_", "_HERMES_KANBAN_"))})
        return 0

    monkeypatch.setattr(kanban_worker_client, "run", owner_start)
    assert kanban_worker_client.main() == 0
    assert seen == {"TERMINAL_ENV": "ssh", "TERMINAL_CWD": "/remote",
                    "TERMINAL_SSH_HOST": "remote.example", "TERMINAL_PERSISTENT_SHELL": "true"}
