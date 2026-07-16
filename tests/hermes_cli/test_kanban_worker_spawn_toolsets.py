from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def _make_task(kb, *, assignee: str, workspace_kind: str = "dir"):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind=workspace_kind,
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_default_spawn_never_boots_the_tui(monkeypatch, tmp_path):
    """Workers are headless: an inherited HERMES_TUI=1 (or a TUI-default
    config) must not send the quiet chat run into the Ink TUI, whose no-TTY
    bail-out exits 0 without doing the task — every attempt then ends in
    "protocol violation". The spawn pins --cli (highest-precedence interface
    flag) and strips HERMES_TUI from the child env."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("display:\n  interface: tui\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_TUI", "1")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4243

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert "--cli" in captured["cmd"]
    assert "HERMES_TUI" not in captured["env"]


def test_default_spawn_drops_gateway_terminal_backend_for_local_default_profile(
    monkeypatch, tmp_path,
):
    """A profile without ``terminal`` must get the local default.

    The embedded dispatcher inherits the gateway profile's bridged SSH values.
    Passing those into ``hermes -p <assignee>`` prevented a fresh profile from
    selecting its local default and routed file tools to the gateway's Mac SSH
    backend instead.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "raiden"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "agent:\n  max_turns: 30\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text(
        "terminal:\n  backend: ssh\n  ssh_host: mac.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "mac.example")
    monkeypatch.setenv("TERMINAL_SSH_USER", "gateway-user")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "180")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    kb._default_spawn(_make_task(kb, assignee="raiden"), str(workspace))

    child_env = captured["env"]
    assert child_env["HERMES_HOME"] == str(profile)
    assert child_env["TERMINAL_CWD"] == str(workspace)
    assert child_env["TERMINAL_ENV"] == "local"
    assert "TERMINAL_SSH_HOST" not in child_env
    assert "TERMINAL_SSH_USER" not in child_env
    # Generic command ceilings may remain as a fallback; they do not select a
    # host/backend and the assignee profile can override them at CLI startup.
    assert child_env["TERMINAL_TIMEOUT"] == "180"
    assert "_HERMES_GATEWAY" not in child_env


def test_isolated_worker_env_resolves_local_backend_through_real_cli_import(
    monkeypatch, tmp_path,
):
    """Exercise the real profile config bridge, not only the Popen mock."""
    profile = tmp_path / "profiles" / "raiden"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        "agent:\n  max_turns: 30\n",
        encoding="utf-8",
    )
    # Keep the repository .env in fallback-only mode for this isolated profile.
    profile.joinpath(".env").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from hermes_cli import kanban_db as kb

    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(profile),
        "_HERMES_GATEWAY": "1",
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "mac.example",
        "TERMINAL_CWD": "/Users/gateway",
    })
    kb._isolate_worker_terminal_env(env)
    env["HERMES_HOME"] = str(profile)
    env["TERMINAL_CWD"] = str(workspace)
    repo_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(repo_root)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, os; import cli; "
                "print(json.dumps({'backend': os.environ.get('TERMINAL_ENV'), "
                "'cwd': os.environ.get('TERMINAL_CWD')}))"
            ),
        ],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    resolved = json.loads(probe.stdout.strip().splitlines()[-1])

    assert resolved == {"backend": "local", "cwd": str(workspace)}


def test_default_ssh_profile_uses_local_scratch_for_real_file_and_terminal_tools(
    monkeypatch, tmp_path,
):
    """Identity stays on ``default`` while execution follows local scratch.

    This is the production failure from run 316: the default profile explicitly
    selects SSH, while the dispatcher injects a Linux scratch path. Exercise the
    real CLI config bridge plus real file/terminal tool implementations so a
    Popen-env-only test cannot hide the backend being reactivated later.
    """

    root = tmp_path / ".hermes"
    root.mkdir()
    workspace = root / "kanban" / "boards" / "test" / "workspaces" / "t_spawn_tools"
    workspace.mkdir(parents=True)
    fake_remote_workspace = tmp_path / "mac-remote"
    fake_remote_workspace.mkdir()
    wrong_home = tmp_path / "wrong-home"

    root.joinpath("config.yaml").write_text(
        (
            "model: worker-identity-model\n"
            "terminal:\n"
            "  backend: ssh\n"
            f"  cwd: {json.dumps(str(fake_remote_workspace))}\n"
            "  ssh_host: mac.example\n"
            "  ssh_user: gateway-user\n"
            "platform_toolsets:\n"
            "  cli:\n"
            "    - file\n"
            "    - terminal\n"
            "toolsets:\n"
            "  - hermes-cli\n"
        ),
        encoding="utf-8",
    )
    # Profile dotenv is authoritative for ordinary settings. It must not be
    # able to replace dispatcher-issued process identity/workspace pins.
    root.joinpath(".env").write_text(
        (
            "_HERMES_KANBAN_EXECUTION_BACKEND=ssh\n"
            "_HERMES_GATEWAY=1\n"
            "HERMES_KANBAN_TASK=t_wrong\n"
            f"HERMES_KANBAN_WORKSPACE={fake_remote_workspace}\n"
            f"HERMES_HOME={wrong_home}\n"
            "HERMES_PROFILE=wrong\n"
            "TERMINAL_ENV=ssh\n"
            f"TERMINAL_CWD={fake_remote_workspace}\n"
            "TERMINAL_SSH_HOST=dotenv-mac.example\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(
        _make_task(kb, assignee="default", workspace_kind="scratch"),
        str(workspace),
        board="test",
    )
    # subprocess.run uses subprocess.Popen internally; restore it for the real
    # child probe after capturing the worker launch contract.
    monkeypatch.setattr(subprocess, "Popen", real_popen)

    child_env = captured["env"]
    assert captured["cwd"] == str(workspace)
    assert child_env["HERMES_HOME"] == str(root)
    assert child_env["HERMES_PROFILE"] == "default"
    assert child_env["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert child_env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert child_env["_HERMES_KANBAN_EXECUTION_BACKEND"] == "local"

    repo_root = Path(__file__).resolve().parents[2]
    child_env["PYTHONPATH"] = str(repo_root)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, os; import cli; import run_agent; "
                "from tools.terminal_tool import _get_env_config, terminal_tool; "
                "from tools.file_tools import write_file_tool; "
                "cfg = _get_env_config(); "
                "assert cfg['env_type'] == 'local', cfg; "
                "write = json.loads(write_file_tool('marker.txt', 'bound-local', "
                "task_id='binding-e2e')); "
                "term = json.loads(terminal_tool('pwd', task_id='binding-e2e')); "
                "print(json.dumps({"
                "'backend': cfg['env_type'], 'cwd': cfg['cwd'], "
                "'terminal_config': cli.CLI_CONFIG.get('terminal'), "
                "'model': cli.CLI_CONFIG.get('model', {}).get('default'), "
                "'home': os.environ.get('HERMES_HOME'), "
                "'profile': os.environ.get('HERMES_PROFILE'), "
                "'task': os.environ.get('HERMES_KANBAN_TASK'), "
                "'workspace': os.environ.get('HERMES_KANBAN_WORKSPACE'), "
                "'ssh_host_present': 'TERMINAL_SSH_HOST' in os.environ, "
                "'gateway_marker_present': '_HERMES_GATEWAY' in os.environ, "
                "'write': write, 'terminal': term}))"
            ),
        ],
        cwd=workspace,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    resolved = json.loads(probe.stdout.strip().splitlines()[-1])

    assert resolved["backend"] == "local"
    assert resolved["cwd"] == str(workspace)
    assert resolved["terminal_config"]["backend"] == "local"
    assert resolved["terminal_config"]["cwd"] == str(workspace)
    assert resolved["model"] == "worker-identity-model"
    assert resolved["home"] == str(root)
    assert resolved["profile"] == "default"
    assert resolved["task"] == "t_spawn_tools"
    assert resolved["workspace"] == str(workspace)
    assert resolved["ssh_host_present"] is False
    assert resolved["gateway_marker_present"] is False
    assert resolved["write"].get("error") in (None, "")
    assert resolved["terminal"]["exit_code"] == 0
    assert resolved["terminal"]["output"].strip() == str(workspace)
    assert workspace.joinpath("marker.txt").read_text(encoding="utf-8") == "bound-local"
    assert not fake_remote_workspace.joinpath("marker.txt").exists()
    # Worker runtime binding must not mutate the profile's normal SSH setting.
    assert "backend: ssh" in root.joinpath("config.yaml").read_text(encoding="utf-8")


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]
