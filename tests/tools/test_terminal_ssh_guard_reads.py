"""Remote guard reads must not enter the command file-sync/snapshot path."""
import subprocess
import json
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from tools.environments.ssh import SSHEnvironment
from tools.terminal_tool_guards import _read_script_for_guard
from cron.lifecycle_guard import scan_gateway_lifecycle


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
    assert scan_gateway_lifecycle(read("remote.sh"))[0] is True
    assert read("missing.sh") is None
    assert read(".") is None
    script.write_bytes(b"a" * 2048)
    assert len(read("remote.sh")) == 1025
    script.write_bytes(b"binary\x00bytes")
    assert read("remote.sh") is None
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
    monkeypatch.setattr(env, "_run_ssh", transport)
    monkeypatch.setattr(registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setattr(terminal, "get_session_cwd", lambda *_: None)
    monkeypatch.setattr(terminal, "_plan_execution", lambda *_a, **_k: SimpleNamespace(
        config={}, env_type="ssh", effective_task_id="ssh-read-failure-test",
        cwd=str(tmp_path), effective_timeout=10, promoted_from_foreground_timeout=None,
    ))
    monkeypatch.setattr(terminal, "_acquire_env", lambda *_a, **_k: env)
    monkeypatch.setattr(terminal, "_run_approval_guards", lambda *_a, **_k: terminal._ApprovalVerdict())
    execute = Mock(return_value='{"status": "executed"}')
    monkeypatch.setattr(terminal, "_run_foreground", execute)
    result = json.loads(terminal.terminal_tool(
        f"bash {tmp_path}/first.sh; bash {tmp_path}/restart.sh"
    ))
    assert "could not scan" in result.get("error", "")
    assert "backend read failed" in result["error"]
    execute.assert_not_called()
    env.execute.assert_not_called()


@pytest.mark.parametrize("later_text, expected", [("echo safe\n", False), ("hermes gateway restart\n", True)])
def test_failed_inert_mention_does_not_abort_remaining_scan(tmp_path, later_text, expected):
    first, later = tmp_path / "first.sh", tmp_path / "later.sh"
    reads = []

    def read(path):
        reads.append(path)
        if path == str(first):
            raise subprocess.TimeoutExpired("ssh", 10)
        return later_text if path == str(later) else None

    command = f"python3 - <<'PY'\nprint('{first}')\nprint('{later}')\nPY"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), read_remote_script=read)
    assert unsafe is expected
    assert refusal is None
    assert str(later) in reads
