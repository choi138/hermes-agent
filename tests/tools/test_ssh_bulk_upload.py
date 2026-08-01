"""Tests for SSH bulk upload via tar pipe."""

import os
import stat
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from tools.environments import ssh as ssh_env
from tools.environments.file_sync import quoted_mkdir_command, unique_parent_dirs
from tools.environments.ssh import SSHEnvironment


def _mock_proc(*, returncode=0, poll_return=0, communicate_return=(b"", b""),
               stderr_read=b""):
    """Create a MagicMock mimicking subprocess.Popen for tar/ssh pipes."""
    m = MagicMock()
    m.stdout = MagicMock()
    m.returncode = returncode
    m.poll.return_value = poll_return
    m.wait.return_value = returncode
    m.communicate.return_value = communicate_return
    m.stderr = MagicMock()
    m.stderr.read.side_effect = [stderr_read, b""] if stderr_read else [b""]
    return m


def _timeout_on_timed_wait(command):
    def wait(timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(command, timeout)
        return 0

    return wait


def _tar_version() -> str:
    """Return the local tar implementation banner without raising."""
    try:
        result = subprocess.run(
            ["tar", "--version"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{result.stdout}\n{result.stderr}".lower()


_LOCAL_TAR_VERSION = _tar_version()


@pytest.fixture
def mock_env(monkeypatch):
    """Create an SSHEnvironment with mocked connection/sync."""
    monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/testuser")
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_create_sync_baseline", lambda self: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(
        ssh_env, "FileSyncManager",
        lambda **kw: type("M", (), {"sync": lambda self, **k: None})(),
    )
    env = SSHEnvironment(host="example.com", user="testuser")
    # Existing tests exercise the GNU path unless they explicitly opt into
    # the BSD-compatible fallback.
    env._remote_tar_no_overwrite_dir = True
    return env


class TestSSHBulkUpload:
    """Unit tests for _ssh_bulk_upload — tar pipe mechanics."""

    def test_empty_files_is_noop(self, mock_env):
        """Empty file list should not spawn any subprocesses."""
        with patch.object(subprocess, "run") as mock_run, \
             patch.object(subprocess, "Popen") as mock_popen:
            mock_env._ssh_bulk_upload([])
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_mkdir_batched_into_single_call(self, mock_env, tmp_path):
        """All parent directories should be created in one SSH call."""
        # Create test files
        f1 = tmp_path / "a.txt"
        f1.write_text("aaa")
        f2 = tmp_path / "b.txt"
        f2.write_text("bbb")

        files = [
            (str(f1), "/home/testuser/.hermes/skills/a.txt"),
            (str(f2), "/home/testuser/.hermes/credentials/b.txt"),
        ]

        # Mock subprocess.run for mkdir and Popen for tar pipe
        mock_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))

        def make_proc(cmd, **kwargs):
            m = MagicMock()
            m.stdout = MagicMock()
            m.returncode = 0
            m.poll.return_value = 0
            m.communicate.return_value = (b"", b"")
            m.stderr = MagicMock()
            m.stderr.read.return_value = b""
            return m

        with patch.object(subprocess, "run", mock_run), \
             patch.object(subprocess, "Popen", side_effect=make_proc):
            mock_env._ssh_bulk_upload(files)

        # Exactly one subprocess.run call for mkdir
        assert mock_run.call_count == 1
        mkdir_cmd = mock_run.call_args[0][0]
        # Should contain mkdir -p with both parent dirs
        mkdir_str = " ".join(mkdir_cmd)
        assert "mkdir -p" in mkdir_str
        assert "/home/testuser/.hermes/skills" in mkdir_str
        assert "/home/testuser/.hermes/credentials" in mkdir_str

    def test_staging_symlinks_mirror_remote_layout(self, mock_env, tmp_path):
        """Staged file in staging dir should mirror the remote path structure.

        On platforms where symlinks are available (Linux/macOS) the staged
        entry is a symlink; on Windows it may be a regular copy.  Either way
        the file must exist at the expected path and contain the right data.
        """
        f1 = tmp_path / "local_a.txt"
        f1.write_text("content a")

        files = [
            (str(f1), "/home/testuser/.hermes/skills/my_skill.md"),
        ]

        staging_paths = []

        def capture_tar_cmd(cmd, **kwargs):
            if cmd[0] == "tar":
                # Capture the staging dir from -C argument
                c_idx = cmd.index("-C")
                staging_dir = cmd[c_idx + 1]
                # Check the staged entry exists at the base-relative path
                expected = os.path.join(staging_dir, "skills/my_skill.md")
                staging_paths.append(expected)
                # File must exist (either as symlink or copy)
                assert os.path.exists(expected), f"Expected staged file at {expected}"
                # Content must match the source
                with open(expected, "r") as fh:
                    assert fh.read() == "content a"

            mock = MagicMock()
            mock.stdout = MagicMock()
            mock.returncode = 0
            mock.poll.return_value = 0
            mock.communicate.return_value = (b"", b"")
            mock.stderr = MagicMock()
            mock.stderr.read.return_value = b""
            return mock

        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(subprocess, "Popen", side_effect=capture_tar_cmd):
            mock_env._ssh_bulk_upload(files)

        assert len(staging_paths) == 1, "tar command should have been called"


    def test_bsdtar_extract_command_omits_gnu_option(self, mock_env, tmp_path):
        """Unsupported remote tar implementations must not receive GNU flags."""
        source = tmp_path / "portable.txt"
        source.write_text("portable", encoding="utf-8")
        files = [(str(source), "/home/testuser/.hermes/skills/portable.txt")]
        mock_env._remote_tar_no_overwrite_dir = False

        popen_cmds = []

        def capture_popen(cmd, **kwargs):
            popen_cmds.append(cmd)
            return _mock_proc()

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ), patch.object(subprocess, "Popen", side_effect=capture_popen):
            mock_env._ssh_bulk_upload(files)

        extract_cmd = popen_cmds[1][-1]
        assert "tar xf -" in extract_cmd
        assert "--no-overwrite-dir" not in extract_cmd

    def test_remote_tar_capability_is_detected_once(self, mock_env):
        """The GNU option probe should be cached for the environment lifetime."""
        mock_env._remote_tar_no_overwrite_dir = None
        probe = subprocess.CompletedProcess([], 0, stdout="", stderr="")

        with patch.object(subprocess, "run", return_value=probe) as mock_run:
            assert mock_env._supports_remote_tar_no_overwrite_dir() is True
            assert mock_env._supports_remote_tar_no_overwrite_dir() is True

        mock_run.assert_called_once()
        probe_cmd = mock_run.call_args[0][0]
        assert probe_cmd[-1] == "LC_ALL=C tar --help 2>&1 | grep -q -- --no-overwrite-dir"

    def test_archive_paths_cannot_be_parsed_as_tar_options(self, mock_env, tmp_path):
        """File-only archive members must follow an explicit option terminator."""
        source = tmp_path / "payload"
        source.write_text("safe", encoding="utf-8")
        remote = "/home/testuser/.hermes/skills/--checkpoint-action=exec=sh"
        popen_cmds = []

        def capture_popen(cmd, **kwargs):
            popen_cmds.append(cmd)
            return _mock_proc()

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ), patch.object(subprocess, "Popen", side_effect=capture_popen):
            mock_env._ssh_bulk_upload([(str(source), remote)])

        tar_cmd = popen_cmds[0]
        separator = tar_cmd.index("--")
        assert tar_cmd[separator + 1:] == ["skills/--checkpoint-action=exec=sh"]

    def test_remote_path_escape_is_rejected_before_spawning(self, mock_env, tmp_path):
        """Changing archive construction must not weaken staging containment."""
        source = tmp_path / "payload"
        source.write_text("safe", encoding="utf-8")
        escaped = "/home/testuser/.hermes/../outside.txt"

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as mock_run, \
             patch.object(subprocess, "Popen") as mock_popen:
            with pytest.raises(RuntimeError, match="escapes sync base"):
                mock_env._ssh_bulk_upload([(str(source), escaped)])

        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    def test_bulk_upload_never_stages_remote_home_prefix(self, mock_env, tmp_path):
        """Regression: do not archive /home/<user> path components."""
        f1 = tmp_path / "nested.txt"
        f1.write_text("nested")
        files = [(str(f1), "/home/testuser/.hermes/cache/nested.txt")]

        def capture_tar_cmd(cmd, **kwargs):
            if cmd[0] == "tar":
                c_idx = cmd.index("-C")
                staging_dir = cmd[c_idx + 1]
                assert not os.path.exists(os.path.join(staging_dir, "home"))
                expected = os.path.join(staging_dir, "cache/nested.txt")
                assert os.path.islink(expected)

            mock = MagicMock()
            mock.stdout = MagicMock()
            mock.returncode = 0
            mock.poll.return_value = 0
            mock.communicate.return_value = (b"", b"")
            mock.stderr = MagicMock()
            mock.stderr.read.return_value = b""
            return mock

        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(subprocess, "Popen", side_effect=capture_tar_cmd):
            mock_env._ssh_bulk_upload(files)


    def test_timeout_kills_both_processes(self, mock_env, tmp_path):
        """TimeoutExpired during communicate should kill both processes."""
        f1 = tmp_path / "t.txt"
        f1.write_text("t")
        files = [(str(f1), "/home/testuser/.hermes/skills/t.txt")]

        mock_tar = MagicMock()
        mock_tar.stdout = MagicMock()
        mock_tar.returncode = None
        mock_tar.poll.return_value = None
        mock_tar.stderr = MagicMock()
        mock_tar.stderr.read.return_value = b""

        mock_ssh = MagicMock()
        mock_ssh.communicate.side_effect = subprocess.TimeoutExpired("ssh", 120)
        mock_ssh.wait.side_effect = _timeout_on_timed_wait("ssh")
        mock_ssh.returncode = None
        mock_ssh.poll.return_value = None
        mock_ssh.stderr = MagicMock()
        mock_ssh.stderr.read.return_value = b""

        def make_proc(cmd, **kwargs):
            if cmd[0] == "tar":
                return mock_tar
            return mock_ssh

        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(subprocess, "Popen", side_effect=make_proc):
            with pytest.raises(RuntimeError, match="SSH bulk upload timed out"):
                mock_env._ssh_bulk_upload(files)

        mock_tar.kill.assert_called_once()
        mock_ssh.kill.assert_called_once()
        mock_tar.wait.assert_called()
        mock_ssh.wait.assert_called()

    def test_local_tar_timeout_is_killed_and_both_children_are_reaped(self, mock_env, tmp_path):
        """A stuck local producer after SSH exit must not leave child processes."""
        source = tmp_path / "stuck.txt"
        source.write_text("payload", encoding="utf-8")
        files = [(str(source), "/home/testuser/.hermes/skills/stuck.txt")]
        mock_tar = _mock_proc(returncode=None, poll_return=None)
        mock_tar.communicate.side_effect = subprocess.TimeoutExpired("tar", 10)
        mock_tar.wait.side_effect = _timeout_on_timed_wait("tar")
        mock_ssh = _mock_proc(returncode=0, poll_return=0)

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ), patch.object(subprocess, "Popen", side_effect=[mock_tar, mock_ssh]):
            with pytest.raises(RuntimeError, match="SSH bulk upload timed out"):
                mock_env._ssh_bulk_upload(files)

        mock_tar.kill.assert_called_once()
        mock_tar.wait.assert_called()
        mock_ssh.wait.assert_called()


class TestSSHBulkUploadTarIntegration:
    """Real local tar round trips through the production pipeline."""

    @staticmethod
    def _make_shell_env(remote_home, *, no_overwrite_dir: bool | None):
        env = object.__new__(SSHEnvironment)
        env._remote_home = str(remote_home)
        env._remote_tar_no_overwrite_dir = no_overwrite_dir
        env._build_ssh_command = lambda extra_args=None: ["sh", "-c"]
        return env

    @staticmethod
    def _assert_round_trip(env, tmp_path):
        base = tmp_path / "remote home" / ".hermes"
        parent = base / "skills"
        parent.mkdir(parents=True)
        base.chmod(0o700)
        parent.chmod(0o711)
        base_mode = stat.S_IMODE(base.stat().st_mode)
        parent_mode = stat.S_IMODE(parent.stat().st_mode)

        source = tmp_path / "dummy-tool.sh"
        source.write_text("#!/bin/sh\nprintf portable", encoding="utf-8")
        source.chmod(0o750)
        destination = parent / "dummy-tool.sh"

        env._ssh_bulk_upload([(str(source), str(destination))])

        assert destination.read_text(encoding="utf-8") == "#!/bin/sh\nprintf portable"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o750
        assert stat.S_IMODE(base.stat().st_mode) == base_mode
        assert stat.S_IMODE(parent.stat().st_mode) == parent_mode

    @pytest.mark.skipif(
        sys.platform != "darwin" or "bsdtar" not in _LOCAL_TAR_VERSION,
        reason="requires macOS BSD tar",
    )
    def test_bsdtar_round_trip_preserves_existing_directory_modes(self, tmp_path):
        """macOS BSD tar succeeds without changing base or parent modes."""
        remote_home = tmp_path / "remote home"
        env = self._make_shell_env(remote_home, no_overwrite_dir=None)
        assert env._supports_remote_tar_no_overwrite_dir() is False
        self._assert_round_trip(env, tmp_path)

    @pytest.mark.skipif(
        os.name != "posix" or "gnu tar" not in _LOCAL_TAR_VERSION,
        reason="requires POSIX GNU tar",
    )
    def test_gnu_tar_round_trip_preserves_existing_directory_modes(self, tmp_path):
        """GNU tar retains the existing no-overwrite-dir protection."""
        remote_home = tmp_path / "remote home"
        env = self._make_shell_env(remote_home, no_overwrite_dir=None)
        assert env._supports_remote_tar_no_overwrite_dir() is True
        self._assert_round_trip(env, tmp_path)


class TestSSHBulkUploadWiring:
    """Verify bulk_upload_fn is wired into FileSyncManager."""

    def test_filesyncmanager_receives_bulk_upload_fn(self, monkeypatch):
        """SSHEnvironment should pass _ssh_bulk_upload to FileSyncManager."""
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/root")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_create_sync_baseline", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)

        captured_kwargs = {}

        class FakeSyncManager:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def sync(self, **kw):
                pass

        monkeypatch.setattr(ssh_env, "FileSyncManager", FakeSyncManager)

        env = SSHEnvironment(host="h", user="u")

        assert "bulk_upload_fn" in captured_kwargs
        assert captured_kwargs["bulk_upload_fn"] is not None
        # Should be the bound method
        assert callable(captured_kwargs["bulk_upload_fn"])


class TestSharedHelpers:
    """Direct unit tests for file_sync.py helpers."""

    def test_quoted_mkdir_command_basic(self):
        result = quoted_mkdir_command(["/a", "/b/c"])
        assert result == "mkdir -p /a /b/c"


    def test_unique_parent_dirs_empty(self):
        assert unique_parent_dirs([]) == []


class TestSSHBulkUploadEdgeCases:
    """Edge cases for _ssh_bulk_upload."""

    def test_ssh_popen_failure_kills_tar(self, mock_env, tmp_path):
        """If SSH Popen raises, tar process must be killed and cleaned up."""
        f1 = tmp_path / "e.txt"
        f1.write_text("e")
        files = [(str(f1), "/home/testuser/.hermes/skills/e.txt")]

        mock_tar = _mock_proc(returncode=None, poll_return=None)

        call_count = 0

        def failing_ssh_popen(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_tar  # tar Popen succeeds
            raise OSError("SSH binary not found")

        with patch.object(subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(subprocess, "Popen", side_effect=failing_ssh_popen):
            with pytest.raises(OSError, match="SSH binary not found"):
                mock_env._ssh_bulk_upload(files)

        mock_tar.kill.assert_called_once()
        mock_tar.wait.assert_called_once()
