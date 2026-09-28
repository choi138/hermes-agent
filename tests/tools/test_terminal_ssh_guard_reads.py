"""Remote guard reads must not enter the command file-sync/snapshot path."""
import subprocess
import json
import pytest
from unittest.mock import Mock

from tools.environments.ssh import SSHEnvironment
from tools.terminal_tool_guards import _read_script_for_guard
from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as scan_gateway_lifecycle


def test_ssh_guard_reads_real_scripts_without_file_sync(tmp_path, monkeypatch):
    env = SSHEnvironment.__new__(SSHEnvironment)
    env.host = "test-host"
    env._sync_manager = Mock()
    env.execute = Mock(side_effect=AssertionError("guard entered command execution"))
    monkeypatch.setattr(env, "_build_ssh_command", lambda: ["bash", "-c"])
    script = tmp_path / "remote.sh"
    script.write_text("echo safe\n")
    # Use the callback directly too: local filesystem visibility must not change routing.
    read = lambda p: _read_script_for_guard(env, str(tmp_path), p, 1024)
    assert read("remote.sh") == "echo safe\n"
    script.write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(read("remote.sh")) is True
    assert read("missing.sh") is None
    assert read(".") is None
    script.write_bytes(b"a" * 2048)
    assert len(read("remote.sh")) == 1025
    script.write_bytes(b"\x7fELF\x00hermes gateway restart")
    assert scan_gateway_lifecycle("bash remote.sh", cwd=str(tmp_path), read_remote_script=read) is False
    env.execute.assert_not_called()
    env._sync_manager.sync.assert_not_called()


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("ssh", 10),
    subprocess.CompletedProcess("ssh", 255, "", "connection lost"),
])
def test_ssh_read_failure_blocks_terminal_execution(tmp_path, monkeypatch, failure):
    import tools.terminal_tool as terminal
    import tools.process_registry as registry

    env = SSHEnvironment.__new__(SSHEnvironment)
    env.host = "test-host"
    env.cwd = str(tmp_path)
    env.execute = Mock(side_effect=AssertionError("command must not execute"))
    transport = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(env, "_build_ssh_command", lambda: ["ssh"])
    monkeypatch.setattr("tools.environments.ssh.subprocess.run", transport)
    monkeypatch.setattr(registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(terminal, "get_session_cwd", lambda *_: None)
    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "ssh", "timeout": 10, "cwd": str(tmp_path),
        "host_cwd": None, "modal_mode": "auto"})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_active_environments", {"default": env})
    monkeypatch.setattr(terminal, "_last_activity", {"default": 0})
    result = json.loads(terminal.terminal_tool(
        f"bash {tmp_path}/first.sh; bash {tmp_path}/restart.sh"
    ))
    assert result.get("status") == "error", result
    assert "Blocked: command or referenced script" in result.get("error", ""), result
    transport.assert_called()
    env.execute.assert_not_called()


def test_remote_script_wins_over_same_named_local_script(tmp_path):
    script = tmp_path / "remote.sh"
    script.write_text("echo safe\n")
    assert scan_gateway_lifecycle(
        f"bash {script}", cwd=str(tmp_path),
        read_remote_script=lambda _: "hermes gateway restart\n")
