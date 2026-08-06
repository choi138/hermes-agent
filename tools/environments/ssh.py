"""SSH remote execution environment with ControlMaster connection persistence."""

import hashlib
import json
import logging
import os
import posixpath
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.file_sync import (
    FileSyncManager,
    FileSyncState,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    sync_back_remote_roots,
    unique_parent_dirs,
)
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_BULK_UPLOAD_TIMEOUT_SECONDS = 120
_BULK_DOWNLOAD_TIMEOUT_SECONDS = 120
_LOCAL_TAR_EXIT_TIMEOUT_SECONDS = 10
_SHUTDOWN_CONTROL_EXIT_RESERVE_SECONDS = 1.0
_MAX_PROCESS_STDERR_BYTES = 16 * 1024
_PIPE_READ_BYTES = 8192
_MAX_CONNECTION_POOL_SIZE = 8
_REMOTE_KILL_TIMEOUT_SECONDS = 8
_SYNC_STATE_CACHE_VERSION = 1
_SYNC_STATE_PROCESS_NONCE = secrets.token_hex(16)
_monotonic = time.monotonic


# A persistent SSH environment is normally shared by all foreground and
# delegated terminal calls in the gateway process.  Lifecycle/sync operations
# for the same target must not overlap when an idle reaper hands ownership to a
# newly-created environment.  Command execution itself does not take this lock
# and remains fully parallel.
_TARGET_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[tuple[str, str, int], threading.RLock] = {}
_TARGET_SYNC_STATES: dict[tuple[str, str, int, str, str], FileSyncState] = {}
_TARGET_PERSISTENT_STATE_CHECKED: set[tuple[str, str, int, str, str]] = set()
_TARGET_ACTIVE_SYNC_ENVS: dict[tuple[str, str, int, str, str], int] = {}


class _SyncStateFileLock:
    """Short cross-process lock for SSH sync-state lease transitions."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0, os.SEEK_END)
                if self._handle.tell() == 0:
                    self._handle.write(b"\0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            self._handle.close()
            self._handle = None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _live_sync_leases(lease_dir: Path) -> list[Path]:
    """Return live leases and remove only leases known to be stale."""
    if not lease_dir.is_dir():
        return []
    live: list[Path] = []
    for lease_path in lease_dir.glob("*.json"):
        try:
            payload = json.loads(lease_path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            nonce = str(payload.get("process_nonce") or "")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # An unreadable lease cannot safely be declared stale. It may cost
            # a cache hit, but it must never permit a stale mirror claim.
            live.append(lease_path)
            continue
        current_pid_stale = (
            pid == os.getpid() and nonce != _SYNC_STATE_PROCESS_NONCE
        )
        if current_pid_stale or not _pid_is_alive(pid):
            try:
                lease_path.unlink()
            except OSError:
                live.append(lease_path)
            continue
        live.append(lease_path)
    return live


def _target_lifecycle_lock(user: str, host: str, port: int) -> threading.RLock:
    key = (user, host, port)
    with _TARGET_LOCKS_GUARD:
        lock = _TARGET_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _TARGET_LOCKS[key] = lock
        return lock


def _target_sync_key(
    user: str,
    host: str,
    port: int,
    remote_home: str,
    *,
    profile_home: str | Path | None = None,
) -> tuple[str, str, int, str, str]:
    local_home = Path(profile_home or get_hermes_home()).expanduser().resolve()
    return (user, host, port, remote_home.rstrip("/"), str(local_home))


def _target_sync_state(
    user: str,
    host: str,
    port: int,
    remote_home: str,
    *,
    profile_home: str | Path | None = None,
) -> FileSyncState:
    """Return the process-wide sync snapshot for one target and profile."""
    key = _target_sync_key(
        user,
        host,
        port,
        remote_home,
        profile_home=profile_home,
    )
    with _TARGET_LOCKS_GUARD:
        state = _TARGET_SYNC_STATES.get(key)
        if state is None:
            state = FileSyncState()
            _TARGET_SYNC_STATES[key] = state
        return state


def _claim_persistent_state_check(
    key: tuple[str, str, int, str, str],
) -> bool:
    """Return True once per process for a target/profile cache restore."""
    with _TARGET_LOCKS_GUARD:
        if key in _TARGET_PERSISTENT_STATE_CHECKED:
            return False
        _TARGET_PERSISTENT_STATE_CHECKED.add(key)
        return True


def _register_active_sync_env(
    key: tuple[str, str, int, str, str],
) -> None:
    with _TARGET_LOCKS_GUARD:
        _TARGET_ACTIVE_SYNC_ENVS[key] = _TARGET_ACTIVE_SYNC_ENVS.get(key, 0) + 1


def _release_active_sync_env(
    key: tuple[str, str, int, str, str],
) -> bool:
    """Drop one environment and return whether it was the last active one."""
    with _TARGET_LOCKS_GUARD:
        remaining = max(0, _TARGET_ACTIVE_SYNC_ENVS.get(key, 1) - 1)
        if remaining:
            _TARGET_ACTIVE_SYNC_ENVS[key] = remaining
            return False
        _TARGET_ACTIVE_SYNC_ENVS.pop(key, None)
        return True


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
                 sync_files: bool = True, persistent: bool = False,
                 connection_pool_size: int = 1):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self._persistent = bool(persistent)
        try:
            requested_pool_size = int(connection_pool_size)
        except (TypeError, ValueError):
            requested_pool_size = 1
        self._connection_pool_size = max(
            1, min(requested_pool_size, _MAX_CONNECTION_POOL_SIZE)
        )
        self._socket_selection_lock = threading.Lock()
        self._next_socket_index = 0
        self._target_lifecycle_lock = _target_lifecycle_lock(user, host, port)
        self._active_commands_lock = threading.Lock()
        self._active_commands = 0
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        self._sync_manager: FileSyncManager | None = None
        self._sync_baseline_path: str | None = None
        self._sync_state_key: tuple[str, str, int, str, str] | None = None
        self._sync_state_registered = False
        self._sync_lease_path: Path | None = None
        self._sync_lease_registered = False

        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # Keep the socket filename short so the full path
        # stays under the 104-byte sun_path limit that macOS enforces on
        # Unix domain sockets. A raw ``user@host:port`` — especially with an
        # IPv6 host — plus the 16-byte random suffix SSH appends in
        # ControlMaster mode easily exceeds the limit under macOS's
        # deeply-nested $TMPDIR (e.g. /var/folders/xx/yy/T/). Include a
        # per-instance nonce in the digest: commands within this environment
        # still reuse one master, while cleanup can never terminate another
        # environment's same-target connection.
        _socket_seed = f"{user}@{host}:{port}".encode() + os.urandom(16)
        self._control_sockets = tuple(
            self.control_dir
            / f"{hashlib.sha256(_socket_seed + str(index).encode()).hexdigest()[:16]}.sock"
            for index in range(self._connection_pool_size)
        )
        # Backward-compatible public attribute used by diagnostics and tests.
        # A size-one pool behaves exactly like the historical implementation.
        self.control_socket = self._control_sockets[0]
        self._management_control_socket = self.control_dir / (
            f"{hashlib.sha256(_socket_seed + b'management').hexdigest()[:16]}.sock"
        )
        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()
        self._remote_tar_no_overwrite_dir: bool | None = None

        try:
            if sync_files:
                with self._target_lifecycle_lock:
                    self._ensure_remote_dirs()
                    self._sync_state_key = _target_sync_key(
                        self.user,
                        self.host,
                        self.port,
                        self._remote_home,
                    )
                    other_process_active = self._register_persistent_sync_lease()
                    shared_state = _target_sync_state(
                        self.user,
                        self.host,
                        self.port,
                        self._remote_home,
                    )
                    if _claim_persistent_state_check(self._sync_state_key):
                        if other_process_active:
                            # A clean marker cannot describe the live remote tree
                            # while another Hermes process owns it.
                            self._discard_persistent_sync_marker()
                        else:
                            self._restore_persistent_sync_state(shared_state)
                    self._sync_manager = FileSyncManager(
                        get_files_fn=lambda: iter_sync_files(
                            f"{self._remote_home}/.hermes"
                        ),
                        upload_fn=self._scp_upload,
                        delete_fn=self._ssh_delete,
                        bulk_upload_fn=self._ssh_bulk_upload,
                        bulk_download_fn=self._ssh_bulk_download,
                        shared_state=shared_state,
                    )
                    self._sync_manager.sync(force=True)
                    self._create_sync_baseline()
                    _register_active_sync_env(self._sync_state_key)
                    self._sync_state_registered = True

            self.init_session()
        except BaseException:
            self._release_persistent_sync_lease()
            raise

    def _select_control_socket(self) -> Path:
        with self._socket_selection_lock:
            socket = self._control_sockets[
                self._next_socket_index % len(self._control_sockets)
            ]
            self._next_socket_index += 1
            return socket

    def _build_ssh_command(
        self,
        extra_args: list | None = None,
        *,
        control_socket: Path | None = None,
        management: bool = False,
    ) -> list:
        if control_socket is None:
            control_socket = (
                self._management_control_socket
                if management
                else self._select_control_socket()
            )
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={control_socket}"])
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
        # Establish every work master plus the dedicated management master in
        # parallel. Commands are distributed round-robin across the work pool;
        # timeout cleanup never pays a fresh SSH handshake or competes with a
        # saturated work connection.
        def _connect(control_socket: Path) -> None:
            cmd = self._build_ssh_command(control_socket=control_socket)
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
                raise RuntimeError(
                    f"SSH connection to {self.user}@{self.host} timed out"
                )

        sockets = [*self._control_sockets, self._management_control_socket]
        with ThreadPoolExecutor(max_workers=len(sockets)) as executor:
            list(executor.map(_connect, sockets))

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
        dirs = [
            base,
            f"{base}/credentials",
            f"{base}/.sync-state",
            *sync_back_remote_roots(base),
        ]
        cmd = self._build_ssh_command()
        cmd.append(quoted_mkdir_command(dirs))
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

    def _sync_state_locations(self) -> tuple[str, Path, str]:
        """Return cache id, local snapshot path, and remote clean marker."""
        if self._sync_state_key is None:
            raise RuntimeError("SSH sync state key is unavailable")
        material = "\0".join(str(part) for part in self._sync_state_key)
        cache_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        local_cache = (
            Path(self._sync_state_key[-1])
            / "cache"
            / "ssh-sync-state"
            / f"{cache_id}.json"
        )
        remote_marker = (
            f"{self._remote_home}/.hermes/.sync-state/{cache_id}.token"
        )
        return cache_id, local_cache, remote_marker

    def _sync_lease_locations(self) -> tuple[Path, Path, Path]:
        """Return cross-process lock, lease directory, and this env's lease."""
        cache_id, local_cache, _ = self._sync_state_locations()
        state_root = local_cache.parent
        lease_dir = state_root / "leases" / cache_id
        lease_path = lease_dir / (
            f"{os.getpid()}-{_SYNC_STATE_PROCESS_NONCE}-{self._session_id}.json"
        )
        return state_root / ".leases.lock", lease_dir, lease_path

    def _register_persistent_sync_lease(self) -> bool:
        """Register this environment and report another live process/env."""
        try:
            lock_path, lease_dir, lease_path = self._sync_lease_locations()
            with _SyncStateFileLock(lock_path):
                other_leases = _live_sync_leases(lease_dir)
                atomic_json_write(
                    lease_path,
                    {
                        "pid": os.getpid(),
                        "process_nonce": _SYNC_STATE_PROCESS_NONCE,
                        "session_id": self._session_id,
                        "created_at": time.time(),
                    },
                    mode=0o600,
                )
            self._sync_lease_path = lease_path
            self._sync_lease_registered = True
            return bool(other_leases)
        except (OSError, RuntimeError) as exc:
            # No lease means we cannot prove exclusive clean ownership. Force
            # conservative behavior: skip restore and never publish a marker.
            logger.warning("SSH: sync-state lease registration failed: %s", exc)
            self._sync_lease_path = None
            self._sync_lease_registered = False
            return True

    def _release_persistent_sync_lease(self, finalize=None) -> bool:
        """Remove this lease; run *finalize* only while no other lease exists."""
        if not getattr(self, "_sync_lease_registered", False):
            return False
        lease_path = getattr(self, "_sync_lease_path", None)
        try:
            lock_path, lease_dir, expected_path = self._sync_lease_locations()
            lease_path = lease_path or expected_path
            with _SyncStateFileLock(lock_path):
                try:
                    lease_path.unlink()
                except FileNotFoundError:
                    pass
                remaining = _live_sync_leases(lease_dir)
                self._sync_lease_registered = False
                self._sync_lease_path = None
                if remaining:
                    return False
                if finalize is not None:
                    finalize()
                return True
        except Exception as exc:
            logger.warning("SSH: sync-state lease release failed: %s", exc)
            return False
        finally:
            # A failed release intentionally leaves the on-disk lease behind;
            # the next process prunes it after this PID exits.
            self._sync_lease_registered = False
            self._sync_lease_path = None

    def _discard_persistent_sync_marker(self) -> None:
        """Invalidate a marker that cannot be trusted during concurrent use."""
        _, local_cache, remote_marker = self._sync_state_locations()
        if not local_cache.is_file():
            return
        cmd = self._build_ssh_command(management=True)
        cmd.append(f"rm -f {shlex.quote(remote_marker)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                logger.debug(
                    "SSH: persistent sync marker discard failed: %s",
                    result.stderr.strip() or result.stdout.strip(),
                )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("SSH: persistent sync marker discard failed: %s", exc)
        finally:
            # The remote deletion can fail during a transient disconnect. Drop
            # the paired local half regardless, so a leftover remote token can
            # never validate an out-of-date snapshot on a later startup.
            try:
                local_cache.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "SSH: persistent sync snapshot discard failed: %s", exc
                )

    def _restore_persistent_sync_state(self, state: FileSyncState) -> bool:
        """Consume a clean-shutdown marker and restore its local snapshot."""
        cache_id, local_cache, remote_marker = self._sync_state_locations()
        # A remote marker is only useful when its paired local snapshot exists.
        # Skip the network round trip on a true first start (and in isolated
        # tests), while still falling back to a conservative full sync.
        if not local_cache.is_file():
            return False
        quoted_marker = shlex.quote(remote_marker)
        cmd = self._build_ssh_command(management=True)
        cmd.append(
            f"if [ -f {quoted_marker} ]; then head -c 128 {quoted_marker}; fi; "
            f"rm -f {quoted_marker}"
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("SSH: persistent sync marker read failed: %s", exc)
            return False
        remote_token = (
            (result.stdout or "").strip() if result.returncode == 0 else ""
        )
        if (
            len(remote_token) != 64
            or any(ch not in "0123456789abcdef" for ch in remote_token)
        ):
            return False

        try:
            payload = json.loads(local_cache.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return False
            local_token = str(payload.get("token") or "")
            if (
                payload.get("version") != _SYNC_STATE_CACHE_VERSION
                or payload.get("cache_id") != cache_id
                or not secrets.compare_digest(local_token, remote_token)
            ):
                return False

            raw_files = payload.get("synced_files")
            raw_hashes = payload.get("pushed_hashes")
            if not isinstance(raw_files, dict) or not isinstance(raw_hashes, dict):
                return False
            if len(raw_files) > 100_000 or len(raw_hashes) > len(raw_files):
                return False

            remote_base = f"{self._remote_home}/.hermes"
            synced_files: dict[str, tuple[float, int]] = {}
            for remote_path, value in raw_files.items():
                if not isinstance(remote_path, str):
                    return False
                _relative_remote_path(remote_path, remote_base)
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or not isinstance(value[0], (int, float))
                    or not isinstance(value[1], int)
                    or value[1] < 0
                ):
                    return False
                synced_files[remote_path] = (float(value[0]), value[1])

            pushed_hashes: dict[str, str] = {}
            for remote_path, digest in raw_hashes.items():
                if (
                    remote_path not in synced_files
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest)
                ):
                    return False
                pushed_hashes[remote_path] = digest
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            logger.debug("SSH: persistent sync snapshot rejected: %s", exc)
            return False

        state.restore_snapshot(synced_files, pushed_hashes)
        logger.info(
            "SSH: restored clean sync snapshot for %d file(s)",
            len(synced_files),
        )
        return True

    def _persist_sync_state(
        self,
        state: FileSyncState,
        *,
        timeout: float = 10.0,
    ) -> bool:
        """Atomically pair a local snapshot with a remote clean marker."""
        cache_id, local_cache, remote_marker = self._sync_state_locations()
        snapshot = state.export_snapshot()
        token = secrets.token_hex(32)
        payload = {
            "version": _SYNC_STATE_CACHE_VERSION,
            "cache_id": cache_id,
            "token": token,
            "created_at": time.time(),
            **snapshot,
        }
        try:
            atomic_json_write(local_cache, payload, mode=0o600)
        except OSError as exc:
            logger.debug("SSH: persistent sync snapshot write failed: %s", exc)
            return False

        quoted_marker = shlex.quote(remote_marker)
        quoted_tmp = shlex.quote(f"{remote_marker}.tmp-{self._session_id}")
        cmd = self._build_ssh_command(management=True)
        cmd.append(
            f"umask 077 && printf '%s' {shlex.quote(token)} > {quoted_tmp} "
            f"&& mv -f {quoted_tmp} {quoted_marker}"
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("SSH: persistent sync marker write failed: %s", exc)
            return False
        if result.returncode != 0:
            logger.debug(
                "SSH: persistent sync marker write failed: %s",
                result.stderr.strip() or result.stdout.strip(),
            )
            return False
        logger.info(
            "SSH: saved clean sync snapshot for %d file(s)",
            len(snapshot["synced_files"]),
        )
        return True

    def _create_sync_baseline(self) -> None:
        """Create a remote timestamp marking the end of the initial mirror.

        Sync-back can then archive only files created or modified during this
        environment's lifetime.  Host files uploaded later may also be newer,
        but FileSyncManager's pushed hashes cheaply discard those unchanged
        copies after download.
        """
        marker = f"/tmp/hermes-sync-baseline-{self._session_id}"
        cmd = self._build_ssh_command()
        cmd.append(f"umask 077 && : > {shlex.quote(marker)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("SSH: failed to create incremental sync marker: %s", exc)
            return
        if result.returncode == 0:
            self._sync_baseline_path = marker
        else:
            logger.warning(
                "SSH: failed to create incremental sync marker: %s",
                result.stderr.strip() or result.stdout.strip(),
            )

    def _remove_sync_baseline(self, *, timeout: float = 3.0) -> None:
        marker = self._sync_baseline_path
        self._sync_baseline_path = None
        if not marker:
            return
        cmd = self._build_ssh_command(management=True)
        cmd.append(f"rm -f -- {shlex.quote(marker)}")
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            pass

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
        control_socket = self._select_control_socket()
        mkdir_cmd = self._build_ssh_command(control_socket=control_socket)
        mkdir_cmd.append(f"mkdir -p {shlex.quote(parent)}")
        subprocess.run(
            mkdir_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

        scp_cmd = ["scp", "-o", f"ControlPath={control_socket}"]
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

    def _ssh_bulk_download(
        self,
        dest: Path,
        *,
        timeout: float = _BULK_DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        """Download files changed during this environment as one tar archive.

        Credentials are upload-only. Skills, external skills, and cache files
        are the only trees FileSyncManager can map back to the host, so avoid
        archiving unrelated remote state such as checkouts, virtualenvs, logs,
        and session databases under ``.hermes``.  When the initial-sync marker
        is available, ``find -cnewer`` emits only lifetime changes (including
        copied files whose old mtime was preserved).  Volatile
        delegation live logs are always excluded because the gateway is their
        authoritative writer. Tar from ``/`` with full relative paths so
        members still match ``_pushed_hashes`` keys.
        """
        base = f"{self._remote_home}/.hermes"
        remote_roots = sync_back_remote_roots(base)
        archive_members = [shlex.quote(path.lstrip("/")) for path in remote_roots]
        live_root = f"{base}/cache/delegation/live".lstrip("/")
        marker = self._sync_baseline_path
        ssh_cmd = self._build_ssh_command()
        if marker:
            ssh_cmd.append(
                "cd / && find "
                f"{' '.join(archive_members)} -type f "
                f"-cnewer {shlex.quote(marker)} "
                f"! -path {shlex.quote(live_root)} "
                f"! -path {shlex.quote(live_root + '/*')} -print0 "
                "| tar --null -T - -cf -"
            )
        else:
            ssh_cmd.append(
                "tar cf - "
                f"--exclude={shlex.quote(live_root)} "
                f"--exclude={shlex.quote(live_root + '/*')} "
                f"-C / -- {' '.join(archive_members)}"
            )
        with open(dest, "wb") as f:
            result = subprocess.run(
                ssh_cmd,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=timeout,
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
            with self._target_lifecycle_lock:
                self._sync_manager.sync()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, *args, **kwargs) -> dict:
        """Track in-flight work so the idle reaper cannot tear it down."""
        with self._active_commands_lock:
            self._active_commands += 1
        try:
            return super().execute(*args, **kwargs)
        finally:
            with self._active_commands_lock:
                self._active_commands -= 1

    def has_active_commands(self) -> bool:
        with self._active_commands_lock:
            return self._active_commands > 0

    @staticmethod
    def _remote_tree_kill_command(pid_file: str) -> str:
        """Build a Linux/macOS-compatible descendant-first kill script."""
        marker = shlex.quote(pid_file)
        return (
            f"__hermes_marker={marker}; "
            "if [ -r \"$__hermes_marker\" ]; then "
            "__hermes_root=$(sed -n '1{s/[^0-9].*$//;p;}' \"$__hermes_marker\"); "
            "case \"$__hermes_root\" in ''|*[!0-9]*) ;; *) "
            "__hermes_children() { "
            "ps -eo pid=,ppid= 2>/dev/null | "
            "awk -v parent=\"$1\" '$2 == parent { print $1 }'; "
            "}; "
            "__hermes_collect() { "
            "kill -STOP \"$1\" 2>/dev/null || true; "
            "for __hermes_child in $(__hermes_children \"$1\"); do "
            "__hermes_collect \"$__hermes_child\"; done; "
            "printf '%s\\n' \"$1\"; "
            "}; "
            "__hermes_pids=$(__hermes_collect \"$__hermes_root\"); "
            "if [ -n \"$__hermes_pids\" ]; then "
            "kill -TERM $__hermes_pids 2>/dev/null || true; "
            "kill -CONT $__hermes_pids 2>/dev/null || true; "
            "sleep 0.5; "
            "kill -KILL $__hermes_pids 2>/dev/null || true; "
            "fi ;; esac; fi; "
            "rm -f -- \"$__hermes_marker\""
        )

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn an SSH process that runs bash on the remote host."""
        token = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        remote_pid_file = f"/tmp/hermes-remote-{self._session_id}-{token}.pid"
        quoted_pid_file = shlex.quote(remote_pid_file)
        remote_wrapper = (
            "umask 077\n"
            f"printf '%s\\n' \"$$\" > {quoted_pid_file}\n"
            f"trap 'rm -f -- {quoted_pid_file}' EXIT\n"
            f"{cmd_string}"
        )
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(remote_wrapper)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(remote_wrapper)])

        proc = _popen_bash(cmd, stdin_data)
        proc._hermes_remote_pid_file = remote_pid_file
        return proc

    def _kill_process(self, proc) -> None:
        """Kill the remote command tree, then reap the local SSH client."""
        remote_pid_file = getattr(proc, "_hermes_remote_pid_file", None)
        if remote_pid_file:
            cmd = self._build_ssh_command(management=True)
            # Force bash: zsh intentionally does not SH_WORD_SPLIT scalar
            # command-substitution output, which would turn the newline-separated
            # PID list into one invalid argument and leave the frozen tree alive.
            cmd.extend(
                [
                    "bash",
                    "-c",
                    shlex.quote(self._remote_tree_kill_command(remote_pid_file)),
                ]
            )
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_REMOTE_KILL_TIMEOUT_SECONDS,
                    stdin=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    logger.warning(
                        "SSH: remote process-tree cleanup failed (rc=%s): %s",
                        result.returncode,
                        result.stderr.strip() or result.stdout.strip(),
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("SSH: remote process-tree cleanup failed: %s", exc)

        try:
            if proc.poll() is None:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass

    def cleanup(self, *, shutdown_timeout_seconds: float | None = None):
        # Serialize ownership transfer so concurrent cleanup callers (including
        # BaseEnvironment.__del__) cannot sync back or close this master twice.
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            sync_manager = self._sync_manager
            self._sync_manager = None
            shutdown_deadline = None
            sync_deadline = None
            if shutdown_timeout_seconds is not None:
                shutdown_timeout_seconds = max(
                    0.0, float(shutdown_timeout_seconds)
                )
                shutdown_deadline = _monotonic() + shutdown_timeout_seconds
                control_reserve = min(
                    _SHUTDOWN_CONTROL_EXIT_RESERVE_SECONDS,
                    shutdown_timeout_seconds / 2,
                )
                sync_deadline = shutdown_deadline - control_reserve
            # Prevent a just-created replacement environment from uploading
            # into the same remote tree while this instance is downloading and
            # closing its masters.  Normal command execution remains unlocked.
            with self._target_lifecycle_lock:
                sync_back_succeeded = False
                try:
                    if sync_manager is not None:
                        logger.info("SSH: syncing files from sandbox...")
                        if sync_deadline is None:
                            sync_back_succeeded = sync_manager.sync_back() is True
                        elif sync_deadline > _monotonic():
                            def _bounded_download(dest: Path) -> None:
                                remaining = sync_deadline - _monotonic()
                                if remaining <= 0:
                                    raise TimeoutError(
                                        "SSH shutdown sync-back deadline expired"
                                    )
                                self._ssh_bulk_download(dest, timeout=remaining)

                            sync_back_succeeded = (
                                sync_manager.sync_back(
                                    max_attempts=1,
                                    bulk_download_fn=_bounded_download,
                                    deadline=sync_deadline,
                                )
                                is True
                            )
                        else:
                            logger.warning(
                                "SSH: shutdown sync-back skipped because its "
                                "cleanup budget is exhausted"
                            )
                finally:
                    is_last_sync_env = False
                    if (
                        self._sync_state_registered
                        and self._sync_state_key is not None
                    ):
                        is_last_sync_env = _release_active_sync_env(
                            self._sync_state_key
                        )
                        self._sync_state_registered = False

                    # Publish a clean marker only while holding the local
                    # cross-process lease lock and only after this environment
                    # is the final owner. A new Hermes process cannot register
                    # between the last-owner check and the remote marker write.
                    def _publish_clean_state() -> None:
                        if sync_manager is None:
                            return
                        if sync_deadline is not None and sync_deadline <= _monotonic():
                            return
                        if sync_manager.sync(force=True) is not True:
                            return
                        persist_timeout = 10.0
                        if sync_deadline is not None:
                            remaining = sync_deadline - _monotonic()
                            if remaining <= 0:
                                return
                            persist_timeout = min(persist_timeout, remaining)
                        self._persist_sync_state(
                            sync_manager.shared_state,
                            timeout=persist_timeout,
                        )

                    publish = (
                        _publish_clean_state
                        if (
                            is_last_sync_env
                            and sync_manager is not None
                            and sync_back_succeeded
                        )
                        else None
                    )
                    self._release_persistent_sync_lease(finalize=publish)

                    marker_timeout = 3.0
                    if shutdown_deadline is not None:
                        marker_timeout = min(
                            marker_timeout,
                            max(0.0, shutdown_deadline - _monotonic()),
                        )
                    if marker_timeout > 0:
                        self._remove_sync_baseline(timeout=marker_timeout)

                    if len(self._control_sockets) == 1:
                        # Preserve compatibility with callers/tests that replace
                        # the historical public control_socket attribute.
                        control_sockets = [Path(self.control_socket)]
                    else:
                        control_sockets = list(self._control_sockets)
                    control_sockets.append(self._management_control_socket)

                    seen: set[Path] = set()
                    for control_socket in control_sockets:
                        if control_socket in seen:
                            continue
                        seen.add(control_socket)
                        if not control_socket.exists():
                            continue
                        try:
                            control_timeout = 5.0
                            if shutdown_deadline is not None:
                                control_timeout = min(
                                    control_timeout,
                                    max(0.0, shutdown_deadline - _monotonic()),
                                )
                            if control_timeout > 0:
                                cmd = [
                                    "ssh", "-o", f"ControlPath={control_socket}",
                                    "-O", "exit", f"{self.user}@{self.host}",
                                ]
                                subprocess.run(
                                    cmd,
                                    capture_output=True,
                                    timeout=control_timeout,
                                    stdin=subprocess.DEVNULL,
                                )
                        except (OSError, subprocess.SubprocessError):
                            pass
                        try:
                            control_socket.unlink()
                        except OSError:
                            pass
