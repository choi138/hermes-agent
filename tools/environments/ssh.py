"""SSH remote execution environment with ControlMaster connection persistence."""

import hashlib
import logging
import os
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)

_BULK_UPLOAD_TIMEOUT_SECONDS = 120
_LOCAL_TAR_EXIT_TIMEOUT_SECONDS = 10
_MAX_PROCESS_STDERR_BYTES = 16 * 1024
_PIPE_READ_BYTES = 8192


class _BoundedPipeDrain:
    """Drain a child pipe fully while retaining only a bounded prefix."""

    def __init__(self, pipe, limit: int = _MAX_PROCESS_STDERR_BYTES):
        self._pipe = pipe
        self._limit = limit
        self._captured = bytearray()
        self._total_bytes = 0
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._started = False

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(_PIPE_READ_BYTES)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode(errors="replace")
                self._total_bytes += len(chunk)
                remaining = self._limit - len(self._captured)
                if remaining > 0:
                    self._captured.extend(chunk[:remaining])
        except (OSError, ValueError):
            # A process being killed can close the descriptor while the drain
            # thread is reading. The child is still reaped by the caller.
            pass
        finally:
            try:
                self._pipe.close()
            except (AttributeError, OSError, ValueError):
                pass

    def finish(self) -> tuple[bytes, int]:
        if not self._started:
            try:
                self._pipe.close()
            except (AttributeError, OSError, ValueError):
                pass
            return bytes(self._captured), self._total_bytes
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            try:
                self._pipe.close()
            except (AttributeError, OSError, ValueError):
                pass
            self._thread.join(timeout=1)
        return bytes(self._captured), self._total_bytes


def _format_process_stderr(captured: bytes, total_bytes: int) -> str:
    """Decode, bound, and force-redact stderr for exceptions and logs."""
    from agent.redact import redact_sensitive_text

    text = captured.decode(errors="replace").strip()
    if total_bytes > len(captured):
        notice = (
            f"[stderr truncated: kept {len(captured)} of {total_bytes} bytes]"
        )
        text = f"{text}\n{notice}" if text else notice
    return redact_sensitive_text(text, force=True)


def _terminate_and_reap(*processes: subprocess.Popen) -> None:
    """Kill any live children, then wait for every child to avoid zombies."""
    for proc in processes:
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass

    for proc in processes:
        while True:
            try:
                proc.wait()
                break
            except InterruptedError:
                continue
            except (OSError, subprocess.SubprocessError):
                break


def _relative_remote_path(remote_path: str, base: str) -> str:
    """Return a normalized base-relative POSIX path or reject an escape."""
    normalized_base = posixpath.normpath(base)
    normalized_remote = posixpath.normpath(remote_path)
    try:
        contained = (
            posixpath.isabs(normalized_remote)
            and posixpath.commonpath([normalized_base, normalized_remote])
            == normalized_base
        )
    except ValueError:
        contained = False

    if not contained or normalized_remote == normalized_base:
        raise RuntimeError(
            f"remote path {remote_path!r} escapes sync base {base!r}"
        )

    relative = posixpath.relpath(normalized_remote, normalized_base)
    if relative == "." or relative == ".." or relative.startswith("../"):
        raise RuntimeError(
            f"remote path {remote_path!r} escapes sync base {base!r}"
        )
    return relative


def _staging_path(staging: str, relative: str, remote_path: str, base: str) -> str:
    """Map a POSIX archive member into staging without local path escape."""
    staging_root = os.path.abspath(staging)
    staged = os.path.abspath(os.path.join(staging_root, *relative.split("/")))
    try:
        contained = os.path.commonpath([staging_root, staged]) == staging_root
    except ValueError:
        contained = False
    if not contained:
        raise RuntimeError(
            f"remote path {remote_path!r} escapes sync base {base!r}"
        )
    return staged


def _ensure_ssh_available() -> None:
    """Fail fast with a clear error when the SSH client is unavailable."""
    if not shutil.which("ssh"):
        raise RuntimeError(
            "SSH is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )
    if not shutil.which("scp"):
        raise RuntimeError(
            "SCP is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )


class SSHEnvironment(BaseEnvironment):
    """Run commands on a remote machine over SSH.

    Spawn-per-call: every execute() spawns a fresh ``ssh ... bash -c`` process.
    Session snapshot preserves env vars across calls.
    CWD persists via in-band stdout markers.
    Uses SSH ControlMaster for connection reuse.
    """

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = "",
                 sync_files: bool = True):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # Keep the socket filename short and deterministic so the full path
        # stays under the 104-byte sun_path limit that macOS enforces on
        # Unix domain sockets. A raw ``user@host:port`` — especially with an
        # IPv6 host — plus the 16-byte random suffix SSH appends in
        # ControlMaster mode easily exceeds the limit under macOS's
        # deeply-nested $TMPDIR (e.g. /var/folders/xx/yy/T/). Hashing the
        # triple keeps the path stable across reconnects so ControlMaster
        # reuse still works.
        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()
        ).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()
        self._remote_tar_no_overwrite_dir: bool | None = None
        self._sync_manager: FileSyncManager | None = None

        if sync_files:
            self._ensure_remote_dirs()
            self._sync_manager = FileSyncManager(
                get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
                upload_fn=self._scp_upload,
                delete_fn=self._ssh_delete,
                bulk_upload_fn=self._ssh_bulk_upload,
                bulk_download_fn=self._ssh_bulk_download,
            )
            self._sync_manager.sync(force=True)

        self.init_session()

    def _build_ssh_command(self, extra_args: list | None = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        cmd.extend(["-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _establish_connection(self):
        cmd = self._build_ssh_command()
        cmd.append("echo 'SSH connection established'")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"SSH connection failed: {error_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH connection to {self.user}@{self.host} timed out")

    def _detect_remote_home(self) -> str:
        """Detect the remote user's home directory."""
        try:
            cmd = self._build_ssh_command()
            cmd.append("echo $HOME")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            home = result.stdout.strip()
            if home and result.returncode == 0:
                logger.debug("SSH: remote home = %s", home)
                return home
        except Exception:
            pass
        if self.user == "root":
            return "/root"
        return f"/home/{self.user}"

    # ------------------------------------------------------------------
    # File sync (via FileSyncManager)
    # ------------------------------------------------------------------

    def _ensure_remote_dirs(self) -> None:
        """Create base ~/.hermes directory tree on remote in one SSH call."""
        base = f"{self._remote_home}/.hermes"
        dirs = [base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]
        cmd = self._build_ssh_command()
        cmd.append(quoted_mkdir_command(dirs))
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

    # _get_sync_files provided via iter_sync_files in FileSyncManager init

    def _supports_remote_tar_no_overwrite_dir(self) -> bool:
        """Probe and cache support for GNU tar's directory-mode guard."""
        cached = self._remote_tar_no_overwrite_dir
        if cached is not None:
            return cached

        cmd = self._build_ssh_command()
        cmd.append(
            "LC_ALL=C tar --help 2>&1 | grep -q -- --no-overwrite-dir"
        )
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            supported = result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            # The file-only archive layout below is safe without the option,
            # so a failed probe conservatively selects the portable path.
            logger.debug(
                "SSH: remote tar capability probe failed (%s)",
                type(exc).__name__,
            )
            supported = False

        self._remote_tar_no_overwrite_dir = supported
        logger.debug(
            "SSH: remote tar --no-overwrite-dir support = %s", supported
        )
        return supported

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Upload a single file via scp over ControlMaster."""
        parent = str(Path(remote_path).parent)
        mkdir_cmd = self._build_ssh_command()
        mkdir_cmd.append(f"mkdir -p {shlex.quote(parent)}")
        subprocess.run(
            mkdir_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

        scp_cmd = ["scp", "-o", f"ControlPath={self.control_socket}"]
        if self.port != 22:
            scp_cmd.extend(["-P", str(self.port)])
        if self.key_path:
            scp_cmd.extend(["-i", self.key_path])
        scp_cmd.extend([host_path, f"{self.user}@{self.host}:{remote_path}"])
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scp failed: {result.stderr.strip()}")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files in a single tar-over-SSH stream.

        Pipes ``tar c`` on the local side through an SSH connection to
        ``tar x`` on the remote, transferring all files in one TCP stream
        instead of spawning a subprocess per file.  Directory creation is
        batched into a single ``mkdir -p`` call beforehand.

        Typical improvement: ~580 files goes from O(N) scp round-trips
        to a single streaming transfer.
        """
        if not files:
            return

        base = f"{self._remote_home}/.hermes"
        validated_files: list[tuple[str, str, str]] = []
        normalized_files: list[tuple[str, str]] = []
        normalized_base = posixpath.normpath(base)
        for host_path, remote_path in files:
            relative = _relative_remote_path(remote_path, normalized_base)
            normalized_remote = posixpath.join(normalized_base, relative)
            validated_files.append((host_path, remote_path, relative))
            normalized_files.append((host_path, normalized_remote))

        parents = unique_parent_dirs(normalized_files)
        if parents:
            cmd = self._build_ssh_command()
            cmd.append(quoted_mkdir_command(parents))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(f"remote mkdir failed: {result.stderr.strip()}")

        # Symlink staging avoids fragile GNU tar --transform rules.
        # On Windows without Developer Mode, symlink creation raises
        # OSError with winerror 1314 (privilege not held).  Catch only
        # that specific error and fall back to a plain copy; all other
        # OSErrors (e.g. disk full, bad path) are re-raised as normal.
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-bulk-") as staging:
            archive_members: list[str] = []
            for host_path, remote_path, rel_remote in validated_files:
                staged = _staging_path(staging, rel_remote, remote_path, base)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError as e:
                    # WinError 1314: symlink privilege not held (Windows without Dev Mode)
                    if getattr(e, "winerror", None) == 1314:
                        shutil.copy2(host_path, staged)
                    else:
                        raise
                archive_members.append(rel_remote)

            # Archive files explicitly instead of archiving '.'.  That omits
            # directory entries entirely, so BSD tar cannot chmod an existing
            # base/parent directory even though it lacks --no-overwrite-dir.
            # The option terminator prevents a remote filename beginning with
            # '-' from being interpreted by the local tar command.
            tar_cmd = [
                "tar", "-chf", "-", "-C", staging, "--", *archive_members,
            ]
            ssh_cmd = self._build_ssh_command()
            extract_cmd = "tar xf -"
            if self._supports_remote_tar_no_overwrite_dir():
                # Retain the GNU defense in depth from #17767.  The file-only
                # archive makes the fallback safe on BSD tar as well.
                extract_cmd += " --no-overwrite-dir"
            extract_cmd += f" -C {shlex.quote(base)}"
            ssh_cmd.append(extract_cmd)

            tar_proc = subprocess.Popen(
                tar_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ssh_proc = subprocess.Popen(
                    ssh_cmd, stdin=tar_proc.stdout, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except BaseException:
                if tar_proc.stdout is not None:
                    tar_proc.stdout.close()
                _terminate_and_reap(tar_proc)
                if tar_proc.stderr is not None:
                    tar_proc.stderr.close()
                raise

            # Allow tar_proc to receive SIGPIPE if ssh_proc exits early
            if tar_proc.stdout is not None:
                tar_proc.stdout.close()

            tar_stderr_drain = _BoundedPipeDrain(tar_proc.stderr)
            ssh_stderr_drain = _BoundedPipeDrain(ssh_proc.stderr)
            drains_started = False
            timed_out = False

            try:
                tar_stderr_drain.start()
                ssh_stderr_drain.start()
                drains_started = True
                ssh_proc.wait(timeout=_BULK_UPLOAD_TIMEOUT_SECONDS)
                tar_proc.wait(timeout=_LOCAL_TAR_EXIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_and_reap(tar_proc, ssh_proc)
            except BaseException:
                _terminate_and_reap(tar_proc, ssh_proc)
                raise
            finally:
                if not drains_started:
                    _terminate_and_reap(tar_proc, ssh_proc)
                tar_stderr_raw, tar_stderr_total = tar_stderr_drain.finish()
                ssh_stderr_raw, ssh_stderr_total = ssh_stderr_drain.finish()

            if timed_out:
                raise RuntimeError("SSH bulk upload timed out")

            tar_stderr = _format_process_stderr(
                tar_stderr_raw, tar_stderr_total
            )
            ssh_stderr = _format_process_stderr(
                ssh_stderr_raw, ssh_stderr_total
            )

            # A remote extractor that exits early commonly gives local tar a
            # secondary SIGPIPE (-13). Report the remote root cause first and
            # retain both statuses when both children failed.
            if ssh_proc.returncode != 0:
                message = f"tar extract over SSH failed (rc={ssh_proc.returncode})"
                if ssh_stderr:
                    message += f": {ssh_stderr}"
                if tar_proc.returncode != 0:
                    message += (
                        f"; local tar create also failed (rc={tar_proc.returncode})"
                    )
                    if tar_stderr:
                        message += f": {tar_stderr}"
                raise RuntimeError(message)

            if tar_proc.returncode != 0:
                message = f"tar create failed (rc={tar_proc.returncode})"
                if tar_stderr:
                    message += f": {tar_stderr}"
                raise RuntimeError(
                    message
                )

        logger.debug("SSH: bulk-uploaded %d file(s) via tar pipe", len(files))

    def _ssh_bulk_download(self, dest: Path) -> None:
        """Download remote .hermes/ as a tar archive."""
        # Tar from / with the full path so archive entries preserve absolute
        # paths (e.g. home/user/.hermes/skills/f.py), matching _pushed_hashes keys.
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        ssh_cmd = self._build_ssh_command()
        ssh_cmd.append(f"tar cf - -C / {shlex.quote(rel_base)}")
        with open(dest, "wb") as f:
            result = subprocess.run(
                ssh_cmd,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        if result.returncode != 0:
            raise RuntimeError(f"SSH bulk download failed: {result.stderr.decode(errors='replace').strip()}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        """Batch-delete remote files in one SSH call."""
        cmd = self._build_ssh_command()
        cmd.append(quoted_rm_command(remote_paths))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"remote rm failed: {result.stderr.strip()}")

    def _before_execute(self) -> None:
        """Sync files to remote via FileSyncManager (rate-limited internally)."""
        if self._sync_manager is not None:
            self._sync_manager.sync()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn an SSH process that runs bash on the remote host."""
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(cmd_string)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(cmd_string)])

        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        # Detach first so repeated cleanup (including BaseEnvironment.__del__)
        # cannot download and apply the same remote archive twice.
        sync_manager = self._sync_manager
        self._sync_manager = None
        if sync_manager is not None:
            logger.info("SSH: syncing files from sandbox...")
            sync_manager.sync_back()

        if self.control_socket.exists():
            try:
                cmd = ["ssh", "-o", f"ControlPath={self.control_socket}",
                       "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                    stdin=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.control_socket.unlink()
            except OSError:
                pass
