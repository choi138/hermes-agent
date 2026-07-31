"""Tests for the SSH remote execution environment backend."""

import json
import os
import subprocess
import threading
import uuid
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments import ssh as ssh_env

_SSH_HOST = os.getenv("TERMINAL_SSH_HOST", "")
_SSH_USER = os.getenv("TERMINAL_SSH_USER", "")
_SSH_PORT = int(os.getenv("TERMINAL_SSH_PORT", "22"))
_SSH_KEY = os.getenv("TERMINAL_SSH_KEY", "")

_has_ssh = bool(_SSH_HOST and _SSH_USER)

requires_ssh = pytest.mark.skipif(
    not _has_ssh,
    reason="TERMINAL_SSH_HOST / TERMINAL_SSH_USER not set",
)


def _run(command, task_id="ssh_test", **kwargs):
    from tools.terminal_tool import terminal_tool
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


def _cleanup(task_id="ssh_test"):
    from tools.terminal_tool import cleanup_vm
    cleanup_vm(task_id)


class TestBuildSSHCommand:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    def test_base_flags(self):
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        for flag in ("ControlMaster=auto", "ControlPersist=300",
                      "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
            assert flag in cmd

    def test_custom_port(self):
        env = SSHEnvironment(host="h", user="u", port=2222)
        cmd = env._build_ssh_command()
        assert "-p" in cmd and "2222" in cmd

    def test_key_path(self):
        env = SSHEnvironment(host="h", user="u", key_path="/k")
        cmd = env._build_ssh_command()
        assert "-i" in cmd and "/k" in cmd

    def test_user_host_suffix(self):
        env = SSHEnvironment(host="h", user="u")
        assert env._build_ssh_command()[-1] == "u@h"

    def test_connection_pool_distributes_commands_across_masters(self):
        env = SSHEnvironment(
            host="h", user="u", sync_files=False, connection_pool_size=3
        )

        commands = [env._build_ssh_command() for _ in range(6)]
        paths = [
            next(part.split("=", 1)[1] for part in cmd if part.startswith("ControlPath="))
            for cmd in commands
        ]

        assert len(set(paths)) == 3
        assert sorted(paths.count(path) for path in set(paths)) == [2, 2, 2]

    def test_management_channel_is_outside_work_pool(self):
        env = SSHEnvironment(
            host="h", user="u", sync_files=False, connection_pool_size=3
        )
        work_paths = {str(path) for path in env._control_sockets}
        management = " ".join(env._build_ssh_command(management=True))

        assert all(path not in management for path in work_paths)
        assert str(env._management_control_socket) in management

    def test_sync_state_is_shared_only_for_the_same_target_and_profile(
        self, tmp_path
    ):
        host = f"target-{uuid.uuid4().hex}"
        profile_a = tmp_path / "profile-a"
        profile_b = tmp_path / "profile-b"

        first = ssh_env._target_sync_state(
            "alice", host, 22, "/home/alice", profile_home=profile_a
        )
        same = ssh_env._target_sync_state(
            "alice", host, 22, "/home/alice", profile_home=profile_a
        )
        other_profile = ssh_env._target_sync_state(
            "alice", host, 22, "/home/alice", profile_home=profile_b
        )
        other_target = ssh_env._target_sync_state(
            "alice", host + "-other", 22, "/home/alice", profile_home=profile_a
        )

        assert same is first
        assert other_profile is not first
        assert other_target is not first


class TestPersistentSSHSyncState:
    @staticmethod
    def _make_env(tmp_path):
        env = object.__new__(SSHEnvironment)
        env.user = "alice"
        env.host = "example.test"
        env.port = 22
        env._remote_home = "/home/alice"
        env._session_id = "session-test"
        env._sync_state_key = ssh_env._target_sync_key(
            env.user,
            env.host,
            env.port,
            env._remote_home,
            profile_home=tmp_path,
        )
        env._sync_lease_path = None
        env._sync_lease_registered = False
        env._build_ssh_command = MagicMock(return_value=["ssh", "alice@example.test"])
        return env

    def test_cross_process_leases_publish_only_for_actual_last_owner(
        self, tmp_path
    ):
        first = self._make_env(tmp_path)
        first._session_id = "session-first"
        second = self._make_env(tmp_path)
        second._session_id = "session-second"

        assert first._register_persistent_sync_lease() is False
        assert second._register_persistent_sync_lease() is True

        finalized = []
        assert second._release_persistent_sync_lease(
            finalize=lambda: finalized.append("second")
        ) is False
        assert finalized == []
        assert first._release_persistent_sync_lease(
            finalize=lambda: finalized.append("first")
        ) is True
        assert finalized == ["first"]

    def test_dead_cross_process_lease_is_pruned(self, monkeypatch, tmp_path):
        env = self._make_env(tmp_path)
        lock_path, lease_dir, _ = env._sync_lease_locations()
        stale = lease_dir / "stale.json"
        ssh_env.atomic_json_write(
            stale,
            {
                "pid": 987654321,
                "process_nonce": "dead",
                "session_id": "stale",
            },
        )
        monkeypatch.setattr(
            ssh_env,
            "_pid_is_alive",
            lambda pid: False if pid == 987654321 else True,
        )

        assert env._register_persistent_sync_lease() is False
        assert not stale.exists()
        env._release_persistent_sync_lease()
        assert lock_path.exists()

    def test_unreadable_cross_process_lease_fails_closed(self, tmp_path):
        env = self._make_env(tmp_path)
        _, lease_dir, _ = env._sync_lease_locations()
        lease_dir.mkdir(parents=True, exist_ok=True)
        corrupt = lease_dir / "corrupt.json"
        corrupt.write_text("not json", encoding="utf-8")

        assert env._register_persistent_sync_lease() is True
        env._release_persistent_sync_lease()
        corrupt.unlink()

    def test_clean_marker_restores_snapshot(self, monkeypatch, tmp_path):
        env = self._make_env(tmp_path)
        state = ssh_env.FileSyncState()
        remote_path = "/home/alice/.hermes/skills/example/SKILL.md"
        state.restore_snapshot(
            {remote_path: (123.5, 42)},
            {remote_path: "a" * 64},
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, "", "")
        )
        monkeypatch.setattr(ssh_env.subprocess, "run", run)

        assert env._persist_sync_state(state) is True
        _, cache_path, _ = env._sync_state_locations()
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        run.return_value = subprocess.CompletedProcess(
            [], 0, payload["token"], ""
        )

        restored = ssh_env.FileSyncState()
        assert env._restore_persistent_sync_state(restored) is True
        assert restored.synced_files == state.synced_files
        assert restored.pushed_hashes == state.pushed_hashes
        assert "rm -f" in run.call_args.args[0][-1]

    def test_marker_write_honors_cleanup_timeout(self, monkeypatch, tmp_path):
        env = self._make_env(tmp_path)
        state = ssh_env.FileSyncState()
        state.restore_snapshot(
            {"/home/alice/.hermes/skills/a.txt": (1.0, 1)},
            {},
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, "", "")
        )
        monkeypatch.setattr(ssh_env.subprocess, "run", run)

        assert env._persist_sync_state(state, timeout=1.25) is True
        assert run.call_args.kwargs["timeout"] == 1.25

    def test_failed_remote_marker_discard_drops_local_snapshot(
        self, monkeypatch, tmp_path
    ):
        env = self._make_env(tmp_path)
        _, cache_path, _ = env._sync_state_locations()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text('{"stale": true}', encoding="utf-8")
        run = MagicMock(
            return_value=subprocess.CompletedProcess([], 255, "", "offline")
        )
        monkeypatch.setattr(ssh_env.subprocess, "run", run)

        env._discard_persistent_sync_marker()

        assert not cache_path.exists()
        assert "rm -f" in run.call_args.args[0][-1]

    def test_snapshot_with_remote_path_escape_is_rejected(
        self, monkeypatch, tmp_path
    ):
        env = self._make_env(tmp_path)
        state = ssh_env.FileSyncState()
        state.restore_snapshot(
            {"/home/alice/.hermes/skills/good.txt": (1.0, 1)},
            {},
        )
        run = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, "", "")
        )
        monkeypatch.setattr(ssh_env.subprocess, "run", run)
        assert env._persist_sync_state(state) is True

        _, cache_path, _ = env._sync_state_locations()
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        payload["synced_files"] = {"/tmp/outside": [1.0, 1]}
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        run.return_value = subprocess.CompletedProcess(
            [], 0, payload["token"], ""
        )

        restored = ssh_env.FileSyncState()
        assert env._restore_persistent_sync_state(restored) is False
        assert restored.synced_files == {}


class TestControlSocketPath:
    """Regression tests for issue #11840.

    macOS caps Unix domain socket paths at 104 bytes (sun_path). SSH
    appends a 16-byte random suffix to the control socket path when
    operating in ControlMaster mode. An IPv6 host embedded in the
    filename plus the deeply-nested macOS $TMPDIR easily blows past
    the limit, causing every tool call to fail immediately.
    """

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    # SSH appends ``.XXXXXXXXXXXXXXXX`` (17 bytes) to the ControlPath in
    # ControlMaster mode; the macOS sun_path field is 104 bytes including
    # the NUL terminator, so the usable path length is 103 bytes.
    _SSH_CONTROLMASTER_SUFFIX = 17
    _MAX_SUN_PATH = 103

    def test_fits_under_macos_socket_limit_with_ipv6_host(self, monkeypatch):
        """A realistic macOS $TMPDIR + IPv6 host must still produce a
        control socket path that fits once SSH appends its ControlMaster
        suffix (see issue #11840)."""
        # Simulate the macOS $TMPDIR shape from the issue traceback —
        # 48 bytes, the typical length of ``/var/folders/XX/YYYYYYYYY/T``.
        fake_tmp = "/var/folders/2t/wbkw5yb158jc3zhswgl7tz9c0000gn/T"
        monkeypatch.setattr("tools.environments.ssh.tempfile.gettempdir",
                            lambda: fake_tmp)
        # The simulated path doesn't exist on the test host — skip the
        # real mkdir so __init__ can proceed.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "mkdir", lambda *a, **k: None)

        env = SSHEnvironment(
            host="9373:9b91:4480:558d:708e:e601:24e8:d8d0",
            user="hermes",
            port=22,
        )

        total_len = len(str(env.control_socket)) + self._SSH_CONTROLMASTER_SUFFIX
        assert total_len <= self._MAX_SUN_PATH, (
            f"control socket path would exceed the {self._MAX_SUN_PATH}-byte "
            f"Unix domain socket limit once SSH appends its 16-byte suffix: "
            f"{env.control_socket} (+{self._SSH_CONTROLMASTER_SUFFIX} = {total_len})"
        )

    def test_path_is_isolated_across_instances(self):
        """Each environment must own its socket, even for the same target."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        assert first.control_socket != second.control_socket

    def test_cleanup_of_one_instance_keeps_peer_socket(self):
        """Cleaning one environment must not tear down its same-target peer."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        first.control_socket.touch(exist_ok=True)
        second.control_socket.touch(exist_ok=True)

        first.cleanup()

        assert second.control_socket.exists()
        second.cleanup()

    def test_path_differs_for_different_targets(self):
        """Different (user, host, port) triples must produce different paths."""
        base = SSHEnvironment(host="h", user="u", port=22).control_socket
        assert SSHEnvironment(host="h", user="u", port=23).control_socket != base
        assert SSHEnvironment(host="h", user="v", port=22).control_socket != base
        assert SSHEnvironment(host="g", user="u", port=22).control_socket != base


class TestTerminalToolConfig:
    def test_ssh_persistent_default_true(self, monkeypatch):
        """SSH persistent defaults to True (via TERMINAL_PERSISTENT_SHELL)."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.delenv("TERMINAL_PERSISTENT_SHELL", raising=False)
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is True

    def test_ssh_persistent_explicit_false(self, monkeypatch):
        """Per-backend env var overrides the global default."""
        monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False

    def test_ssh_persistent_explicit_true(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is True

    def test_ssh_persistent_respects_config(self, monkeypatch):
        """TERMINAL_PERSISTENT_SHELL=false disables SSH persistent by default."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.setenv("TERMINAL_PERSISTENT_SHELL", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False


class TestSSHPreflight:
    def test_sync_directories_cover_every_download_archive_root(self, monkeypatch):
        env = object.__new__(ssh_env.SSHEnvironment)
        env._remote_home = "/home/alice"
        env._build_ssh_command = lambda: ["ssh", "alice@example.com"]
        run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        monkeypatch.setattr(ssh_env.subprocess, "run", run)

        env._ensure_remote_dirs()

        remote_command = run.call_args.args[0][-1]
        assert "/home/alice/.hermes/skills" in remote_command
        assert "/home/alice/.hermes/external_skills" in remote_command
        assert "/home/alice/.hermes/cache" in remote_command

    def test_ensure_ssh_available_raises_clear_error_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="SSH is not installed or not in PATH"):
            ssh_env._ensure_ssh_available()

    def test_ssh_environment_checks_availability_before_connect(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            ssh_env.SSHEnvironment,
            "_establish_connection",
            lambda self: pytest.fail("_establish_connection should not run when ssh is missing"),
        )

        with pytest.raises(RuntimeError, match="openssh-client"):
            ssh_env.SSHEnvironment(host="example.com", user="alice")

    def test_ssh_environment_connects_when_ssh_exists(self, monkeypatch):
        called = {"count": 0}

        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")

        def _fake_establish(self):
            called["count"] += 1

        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", _fake_establish)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_create_sync_baseline", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", lambda **kw: type("M", (), {"sync": lambda self, **k: None})())

        env = ssh_env.SSHEnvironment(host="example.com", user="alice")

        assert called["count"] == 1
        assert env.host == "example.com"
        assert env.user == "alice"

    def test_environment_factory_wires_persistence_and_pool(self, monkeypatch):
        from tools import terminal_tool as terminal_mod

        captured = {}
        sentinel = object()

        def _fake_ssh_environment(**kwargs):
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(terminal_mod, "_SSHEnvironment", _fake_ssh_environment)

        result = terminal_mod._create_environment(
            env_type="ssh",
            image="",
            cwd="~",
            timeout=30,
            ssh_config={
                "host": "example.com",
                "user": "alice",
                "persistent": True,
                "connection_pool_size": 4,
            },
        )

        assert result is sentinel
        assert captured["persistent"] is True
        assert captured["connection_pool_size"] == 4

    def test_probe_mode_skips_hermes_file_sync(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(
            ssh_env.SSHEnvironment,
            "_ensure_remote_dirs",
            lambda self: pytest.fail("probe mode must not prepare sync directories"),
        )
        monkeypatch.setattr(
            ssh_env,
            "FileSyncManager",
            lambda **_kwargs: pytest.fail("probe mode must not create a file sync manager"),
        )

        env = ssh_env.SSHEnvironment(
            host="example.com",
            user="alice",
            sync_files=False,
        )

        assert env._sync_manager is None
        env.control_socket = type(env.control_socket)("/nonexistent/socket")
        env.cleanup()

    def test_environment_factory_marks_ssh_probe_as_no_sync(self, monkeypatch):
        from tools import terminal_tool as terminal_mod

        captured = {}
        sentinel = object()

        def _fake_ssh_environment(**kwargs):
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(terminal_mod, "_SSHEnvironment", _fake_ssh_environment)

        result = terminal_mod._create_environment(
            env_type="ssh",
            image="",
            cwd="~",
            timeout=30,
            ssh_config={"host": "example.com", "user": "alice"},
            probe_only=True,
        )

        assert result is sentinel
        assert captured["sync_files"] is False


class TestSSHRemoteProcessCleanup:
    def test_execute_marks_command_active_for_its_full_lifetime(self, monkeypatch):
        env = object.__new__(SSHEnvironment)
        env._active_commands_lock = threading.Lock()
        env._active_commands = 0

        def _fake_base_execute(self, *_args, **_kwargs):
            assert self.has_active_commands() is True
            return {"output": "ok", "returncode": 0}

        monkeypatch.setattr(ssh_env.BaseEnvironment, "execute", _fake_base_execute)

        assert env.execute("true")["returncode"] == 0
        assert env.has_active_commands() is False

    def test_idle_reaper_preserves_environment_with_active_command(self, monkeypatch):
        from tools import terminal_tool as terminal_mod

        env = MagicMock()
        env.has_active_commands.return_value = True
        active = {"default": env}
        activity = {"default": 0.0}
        monkeypatch.setattr(terminal_mod, "_active_environments", active)
        monkeypatch.setattr(terminal_mod, "_last_activity", activity)

        terminal_mod._cleanup_inactive_envs(lifetime_seconds=1)

        assert terminal_mod._active_environments["default"] is env
        assert terminal_mod._last_activity["default"] > 0
        env.cleanup.assert_not_called()

    def test_kill_process_uses_dedicated_remote_tree_cleanup(self, monkeypatch):
        env = object.__new__(SSHEnvironment)
        env._build_ssh_command = MagicMock(return_value=["ssh", "u@h"])

        proc = MagicMock()
        proc.poll.return_value = None
        proc._hermes_remote_pid_file = "/tmp/hermes-remote-test.pid"
        run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        monkeypatch.setattr(ssh_env.subprocess, "run", run)

        env._kill_process(proc)

        env._build_ssh_command.assert_called_once_with(management=True)
        remote_command = run.call_args.args[0][-1]
        assert "ps -eo pid=,ppid=" in remote_command
        assert "kill -TERM" in remote_command
        assert "kill -KILL" in remote_command
        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2)


def _setup_ssh_env(monkeypatch, persistent: bool):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", _SSH_HOST)
    monkeypatch.setenv("TERMINAL_SSH_USER", _SSH_USER)
    monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true" if persistent else "false")
    if _SSH_PORT != 22:
        monkeypatch.setenv("TERMINAL_SSH_PORT", str(_SSH_PORT))
    if _SSH_KEY:
        monkeypatch.setenv("TERMINAL_SSH_KEY", _SSH_KEY)


@requires_ssh
class TestOneShotSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=False)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello")
        assert r["exit_code"] == 0
        assert "hello" in r["output"]

    def test_exit_code(self):
        r = _run("exit 42")
        assert r["exit_code"] == 42

    def test_state_does_not_persist(self):
        _run("export HERMES_ONESHOT_TEST=yes")
        r = _run("echo $HERMES_ONESHOT_TEST")
        assert r["output"].strip() == ""


@requires_ssh
class TestPersistentSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=True)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello-persistent")
        assert r["exit_code"] == 0
        assert "hello-persistent" in r["output"]

    def test_env_var_persists(self):
        _run("export HERMES_PERSIST_TEST=works")
        r = _run("echo $HERMES_PERSIST_TEST")
        assert r["output"].strip() == "works"

    def test_cwd_persists(self):
        _run("cd /tmp")
        r = _run("pwd")
        assert r["output"].strip() == "/tmp"

    def test_exit_code(self):
        r = _run("(exit 42)")
        assert r["exit_code"] == 42

    def test_stderr(self):
        r = _run("echo oops >&2")
        assert r["exit_code"] == 0
        assert "oops" in r["output"]

    def test_multiline_output(self):
        r = _run("echo a; echo b; echo c")
        lines = r["output"].strip().splitlines()
        assert lines == ["a", "b", "c"]

    def test_timeout_then_recovery(self):
        r = _run("sleep 999", timeout=2)
        assert r["exit_code"] == 124
        r = _run("echo alive")
        assert r["exit_code"] == 0
        assert "alive" in r["output"]

    def test_timeout_kills_remote_child_tree(self):
        marker = f"/tmp/hermes-timeout-child-{uuid.uuid4().hex}.pid"
        r = _run(
            f"sleep 999 & child=$!; echo $child > {marker}; wait $child",
            timeout=2,
        )
        assert r["exit_code"] == 124

        check = _run(
            f"pid=$(cat {marker}); "
            f"if kill -0 $pid 2>/dev/null; then echo alive; else echo dead; fi; "
            f"rm -f {marker}",
            timeout=10,
        )
        assert check["output"].strip() == "dead"

    def test_large_output(self):
        r = _run("seq 1 1000")
        assert r["exit_code"] == 0
        lines = r["output"].strip().splitlines()
        assert len(lines) == 1000
        assert lines[0] == "1"
        assert lines[-1] == "1000"
