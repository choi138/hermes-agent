"""Shared file sync manager for remote execution backends.

Tracks local file changes via mtime+size, detects deletions, and
syncs to remote environments transactionally.  Used by SSH, Modal,
and Daytona.  Docker and Singularity use bind mounts (live host FS
view) and don't need this.
"""

import hashlib
import json
import math
import subprocess
import psutil
from contextlib import contextmanager
import logging
import os
import posixpath
import shlex
import shutil
import signal
import tarfile
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows — file locking skipped
from pathlib import Path
from typing import Callable

from hermes_constants import get_hermes_home
from tools.environments.base import _file_mtime_key

logger = logging.getLogger(__name__)

# Keep retry sleeps patchable without mutating the shared stdlib ``time``
# module. Patching ``tools.environments.file_sync.time.sleep`` replaces
# ``time.sleep`` globally because ``time`` is the module object; under xdist
# that lets unrelated background threads inflate retry-test call counts.
_sleep = time.sleep
# Same rationale for the rate-limit clock: tests patch ``_monotonic``
# instead of ``time.monotonic`` on the shared module object.
_monotonic = time.monotonic

_SYNC_INTERVAL_SECONDS = 5.0
_FORCE_SYNC_ENV = "HERMES_FORCE_FILE_SYNC"

# Delegation live logs are an append-only observability stream owned by the
# gateway process.  They change while remote tar/scp operations are reading
# them, are never consumed by terminal commands, and were the dominant source
# of "file changed as we read it" sync failures.  Keep durable delegation
# artifacts mirrored, but leave this volatile subtree on its authoritative
# host.
_VOLATILE_REMOTE_PATH_PARTS = ("/cache/delegation/live/",)

# Transport callbacks provided by each backend
UploadFn = Callable[[str, str], None]  # (host_path, remote_path) -> raises on failure
BulkUploadFn = Callable[[list[tuple[str, str]]], None]  # [(host_path, remote_path), ...] -> raises on failure
BulkDownloadFn = Callable[[Path], None]  # (dest_tar_path) -> writes tar archive, raises on failure
SelectiveDownloadFn = Callable[[Path, dict, float], None]
DeleteFn = Callable[[list[str]], None]  # (remote_paths) -> raises on failure
GetFilesFn = Callable[[], list[tuple[str, str]]]  # () -> [(host_path, remote_path), ...]


def sync_back_remote_roots(container_base: str = "/root/.hermes") -> list[str]:
    """Return remote trees whose contents can be mapped back to the host.

    ``iter_sync_files`` uploads credentials, skills, external skills, and
    cache artifacts. Credential files are intentionally upload-only, while
    the other three trees may contain agent-created files that should survive
    sandbox teardown. Downloading the whole remote ``.hermes`` directory is
    both unnecessary and dangerous for latency: an SSH target may also keep a
    checkout, virtualenv, logs, and session databases there.
    """
    base = container_base.rstrip("/")
    return [
        f"{base}/skills",
        f"{base}/external_skills",
        f"{base}/cache",
    ]


def iter_sync_files(container_base: str = "/root/.hermes") -> list[tuple[str, str]]:
    """Enumerate all files that should be synced to a remote environment.

    Combines credentials, skills, and cache into a single flat list of
    (host_path, remote_path) pairs.  Credential paths are remapped from
    the hardcoded /root/.hermes to *container_base* because the remote
    user's home may differ (e.g. /home/daytona, /home/user).
    """
    # Late import: credential_files imports agent modules that create
    # circular dependencies if loaded at file_sync module level.
    from tools.credential_files import (
        get_credential_file_mounts,
        iter_cache_files,
        iter_skills_files,
    )

    files: list[tuple[str, str]] = []
    for entry in get_credential_file_mounts():
        remote = entry["container_path"].replace(
            "/root/.hermes", container_base, 1
        )
        files.append((entry["host_path"], remote))
    for entry in iter_skills_files(container_base=container_base):
        files.append((entry["host_path"], entry["container_path"]))
    for entry in iter_cache_files(container_base=container_base):
        remote_path = entry["container_path"]
        if any(part in remote_path for part in _VOLATILE_REMOTE_PATH_PARTS):
            continue
        files.append((entry["host_path"], remote_path))
    return files


def _credential_host_paths() -> set[str]:
    """Return credential files that are upload-only for remote sandboxes."""
    try:
        from tools.credential_files import get_credential_file_mounts
    except Exception:
        return set()

    paths: set[str] = set()
    try:
        mounts = get_credential_file_mounts()
    except Exception:
        return set()
    for entry in mounts:
        host_path = entry.get("host_path") if isinstance(entry, dict) else None
        if not host_path:
            continue
        try:
            paths.add(str(Path(host_path).expanduser().resolve()))
        except OSError:
            paths.add(str(Path(host_path).expanduser()))
    return paths


def quoted_rm_command(remote_paths: list[str]) -> str:
    """Build a shell ``rm -f`` command for a batch of remote paths."""
    return "rm -f " + " ".join(shlex.quote(p) for p in remote_paths)


def quoted_mkdir_command(dirs: list[str]) -> str:
    """Build a shell ``mkdir -p`` command for a batch of directories."""
    return "mkdir -p " + " ".join(shlex.quote(d) for d in dirs)


def unique_parent_dirs(files: list[tuple[str, str]]) -> list[str]:
    """Extract sorted unique parent directories from (host, remote) pairs."""
    return sorted({posixpath.dirname(remote) for _, remote in files})


def _sha256_file(path: str) -> str:
    """Return hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_SYNC_BACK_MAX_RETRIES = 3
_SYNC_BACK_BACKOFF = (2, 4, 8)  # seconds between retries
_SYNC_BACK_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB — refuse to extract larger tars


_SYNC_BACK_MAX_BYTES_KEY = "sync_back_max_bytes"
_SYNC_BACK_TEMP_PREFIX = "hermes-sync-back-"
_SYNC_BACK_STALE_SECONDS = 30 * 60

class SyncBackRefused(ValueError):
    """A deterministic sync-back failure that must not retry the same download."""


def _sync_back_timeout() -> float:
    from hermes_cli.config import load_config

    raw = ((load_config() or {}).get("terminal") or {}).get("sync_back_timeout", 30)
    try:
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
    except (ValueError, TypeError):
        pass
    logger.warning("sync_back: invalid terminal.sync_back_timeout=%r; using 30s", raw)
    return 30.0


def _sync_back_max_bytes() -> int:
    """Extraction cap; config.yaml ``terminal.sync_back_max_bytes`` overrides it for trees that
    legitimately exceed 2 GiB (a skipped extraction silently discards the whole download)."""
    from hermes_cli.config import load_config

    raw = ((load_config() or {}).get("terminal") or {}).get(_SYNC_BACK_MAX_BYTES_KEY)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("sync_back: ignoring non-integer terminal.%s=%r", _SYNC_BACK_MAX_BYTES_KEY, raw)
    return _SYNC_BACK_MAX_BYTES


def _sync_back_temp_prefix() -> str:
    """Temp prefix embedding the owning PID so the stale sweep can tell a hard-killed
    process's leftovers from another live gateway's in-flight transfer without guessing
    from mtime (a staging dir's mtime does not move while content streams into it)."""
    return f"{_SYNC_BACK_TEMP_PREFIX}{os.getpid()}-"


def _temp_entry_owner_alive(name: str) -> bool:
    """Whether the process that created a sync-back temp entry may still be running.
    Names without a PID count as alive: the age cutoff applies."""
    pid_part = name[len(_SYNC_BACK_TEMP_PREFIX):].split("-", 1)[0]
    if not pid_part.isdigit():
        return True
    return psutil.pid_exists(int(pid_part))


def _cleanup_stale_sync_back_temp(temp_dir: Path | None = None) -> int:
    """Remove sync-back tars and staging dirs left behind by a hard-killed process.

    Only entries carrying this module's prefix are touched: at once when their owner PID
    is dead, otherwise only past ``_SYNC_BACK_STALE_SECONDS``. Returns the number of
    entries removed; a permission error or a race with another sync-back must not prevent
    the current one.
    """
    directory = temp_dir or Path(tempfile.gettempdir())
    cutoff = time.time() - _SYNC_BACK_STALE_SECONDS
    removed = 0
    try:
        candidates = list(directory.glob(f"{_SYNC_BACK_TEMP_PREFIX}*"))
    except OSError:
        logger.debug("sync_back: could not scan temporary directory %s", directory)
        return 0
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                continue
            if _temp_entry_owner_alive(candidate.name) and candidate.lstat().st_mtime >= cutoff:
                continue
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed += 1
            logger.debug("sync_back: removed stale temporary entry %s", candidate)
        except OSError:
            logger.debug("sync_back: could not remove stale temporary entry %s", candidate)
    return removed


def _resolve_host_path_str(host_path: str) -> str:
    """Canonical string form of a host path (``resolve()`` falling back to ``expanduser()``)."""
    try:
        return str(Path(host_path).expanduser().resolve())
    except OSError:
        return str(Path(host_path).expanduser())


class FileSyncState:
    """Mutable sync snapshot that may be shared by equivalent backends.

    Transport callbacks stay on each environment-specific manager. Only the
    target/profile file snapshot and its lock are shared, so a newly-created
    SSH environment can verify local mtimes without uploading the same corpus
    again.
    """

    def __init__(self):
        self.synced_files: dict[str, tuple[float, int]] = {}
        self.pushed_hashes: dict[str, str] = {}
        self.upload_only_host_paths: set[str] = set()
        self.last_sync_time: float = 0.0
        self.lock = threading.RLock()

    def export_snapshot(self) -> dict:
        """Return a JSON-serializable copy under the shared lock."""
        with self.lock:
            return {
                "synced_files": {
                    path: [mtime, size]
                    for path, (mtime, size) in self.synced_files.items()
                },
                "pushed_hashes": dict(self.pushed_hashes),
            }

    def restore_snapshot(
        self,
        synced_files: dict[str, tuple[float, int]],
        pushed_hashes: dict[str, str],
    ) -> None:
        """Replace the snapshot atomically; force the next manager to scan."""
        with self.lock:
            self.synced_files = dict(synced_files)
            self.pushed_hashes = dict(pushed_hashes)
            self.upload_only_host_paths = set()
            self.last_sync_time = 0.0


class FileSyncManager:
    """Tracks local file changes and syncs to a remote environment.

    Backends instantiate this with transport callbacks (upload, delete)
    and a file-source callable.  The manager handles mtime-based change
    detection, deletion tracking, rate limiting, and transactional state.

    Not used by bind-mount backends (Docker, Singularity) — those get
    live host FS views and don't need file sync.
    """

    def __init__(
        self,
        get_files_fn: GetFilesFn,
        upload_fn: UploadFn,
        delete_fn: DeleteFn,
        sync_interval: float = _SYNC_INTERVAL_SECONDS,
        bulk_upload_fn: BulkUploadFn | None = None,
        bulk_download_fn: BulkDownloadFn | None = None,
        shared_state: FileSyncState | None = None,
        selective_download_fn: SelectiveDownloadFn | None = None,
        sync_back_identity: str | None = None,
    ):
        self._get_files_fn = get_files_fn
        self._upload_fn = upload_fn
        self._bulk_upload_fn = bulk_upload_fn
        self._bulk_download_fn = bulk_download_fn
        self._selective_download_fn = selective_download_fn
        self._sync_back_identity = sync_back_identity
        self._sync_back_deadline: float | None = None
        self._delete_fn = delete_fn
        self._shared_state = shared_state or FileSyncState()
        self._transaction_lock = threading.Lock()
        self._sync_interval = sync_interval
        # Equivalent managers share this lock and snapshot across task-scoped
        # SSH environments. Only preparation is serialized; commands remain
        # fully concurrent once the one upload cycle completes.
        self._sync_lock = self._shared_state.lock

    @property
    def shared_state(self) -> FileSyncState:
        return self._shared_state

    @property
    def _synced_files(self) -> dict[str, tuple[float, int]]:
        return self._shared_state.synced_files

    @_synced_files.setter
    def _synced_files(self, value: dict[str, tuple[float, int]]) -> None:
        self._shared_state.synced_files = value

    @property
    def _pushed_hashes(self) -> dict[str, str]:
        return self._shared_state.pushed_hashes

    @_pushed_hashes.setter
    def _pushed_hashes(self, value: dict[str, str]) -> None:
        self._shared_state.pushed_hashes = value

    @property
    def _upload_only_host_paths(self) -> set[str]:
        return self._shared_state.upload_only_host_paths

    @_upload_only_host_paths.setter
    def _upload_only_host_paths(self, value: set[str]) -> None:
        self._shared_state.upload_only_host_paths = value

    @property
    def _last_sync_time(self) -> float:
        return self._shared_state.last_sync_time

    @_last_sync_time.setter
    def _last_sync_time(self, value: float) -> None:
        self._shared_state.last_sync_time = value

    def sync(self, *, force: bool = False) -> bool:
        """Single-flight wrapper around one transactional upload cycle."""
        with self._sync_lock:
            return self._sync_once(force=force)

    def _sync_once(self, *, force: bool = False) -> None:
        """Run a sync cycle: upload changed files, delete removed files. Rate-limited to once
        per ``sync_interval`` unless *force* or ``HERMES_FORCE_FILE_SYNC=1``. Transactional:
        state is committed only if ALL operations succeed; on failure it rolls back so the
        next cycle retries everything."""
        with self._transaction_lock:
            if (self._selective_download_fn is None and not self._pending_sync_back().exists()
                    and not self._sync_back_intent().exists()):
                return self._sync_transaction(force=force)
            self._sync_back_deadline = _monotonic() + _sync_back_timeout()
            try:
                # Recovery and upload share the same cross-process lock as sync-back.
                # A failed recovery must propagate: continuing would overwrite remote edits.
                with self._sync_file_lock(get_hermes_home() / ".sync.lock"):
                    self._recover_pending_sync_back()
                    self._recover_sync_back_intent()
                    return self._sync_transaction(force=force)
            finally:
                self._sync_back_deadline = None

    def _sync_transaction(self, *, force: bool = False) -> None:
        """Execute one sync cycle while holding the per-manager lock."""
        if not force and not os.environ.get(_FORCE_SYNC_ENV):
            now = _monotonic()
            if now - self._last_sync_time < self._sync_interval:
                return True

        current_files = self._get_files_fn()
        self._upload_only_host_paths.update(_credential_host_paths())
        current_remote_paths = {remote for _, remote in current_files}

        # --- Uploads: new or changed files ---
        to_upload: list[tuple[str, str]] = []
        new_files = dict(self._synced_files)
        for host_path, remote_path in current_files:
            file_key = _file_mtime_key(host_path)
            if file_key is None:
                continue
            if self._synced_files.get(remote_path) == file_key:
                continue
            to_upload.append((host_path, remote_path))
            new_files[remote_path] = file_key

        # --- Deletes: synced paths no longer in current set ---
        to_delete = [p for p in self._synced_files if p not in current_remote_paths]

        if not to_upload and not to_delete:
            self._last_sync_time = _monotonic()
            return True

        # Snapshot for rollback (only when there's work to do)
        prev_files = dict(self._synced_files)
        prev_hashes = dict(self._pushed_hashes)

        if to_upload:
            logger.debug("file_sync: uploading %d file(s)", len(to_upload))
        if to_delete:
            logger.debug("file_sync: deleting %d stale remote file(s)", len(to_delete))

        try:
            if to_upload and self._bulk_upload_fn is not None:
                self._bulk_upload_fn(to_upload)
                logger.debug("file_sync: bulk-uploaded %d file(s)", len(to_upload))
            else:
                for host_path, remote_path in to_upload:
                    self._upload_fn(host_path, remote_path)
                    logger.debug("file_sync: uploaded %s -> %s", host_path, remote_path)

            if to_delete:
                self._delete_fn(to_delete)
                logger.debug("file_sync: deleted %s", to_delete)

            # --- Commit (all succeeded) ---
            for host_path, remote_path in to_upload:
                self._pushed_hashes[remote_path] = _sha256_file(host_path)

            for p in to_delete:
                new_files.pop(p, None)
                self._pushed_hashes.pop(p, None)

            self._synced_files = new_files
            self._last_sync_time = _monotonic()
            return True

        except Exception as exc:
            self._synced_files = prev_files
            self._pushed_hashes = prev_hashes
            # Do NOT advance _last_sync_time here: a failed cycle rolls state
            # back so the next cycle can retry. Bumping the rate-limit clock on
            # failure would make the next non-forced sync() return early (the
            # guard above), suppressing that retry for up to _sync_interval and
            # leaving the remote with stale files — contradicting this method's
            # documented "next cycle retries everything" contract.
            logger.warning("file_sync: sync failed, rolled back state: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Sync-back: pull remote changes to host on teardown
    # ------------------------------------------------------------------

    def sync_back(
        self, hermes_home: Path | None = None, *,
        max_attempts: int | None = None,
        bulk_download_fn: BulkDownloadFn | None = None,
        deadline: float | None = None,
    ) -> bool:
        """Serialize teardown within a shared lock/transfer/retry deadline."""
        if self._selective_download_fn is not None:
            configured = _monotonic() + _sync_back_timeout()
            deadline = configured if deadline is None else min(deadline, configured)
        acquired = []
        try:
            for lock in (self._sync_lock, self._transaction_lock):
                ok = lock.acquire() if deadline is None else lock.acquire(
                    timeout=max(0, deadline - _monotonic()))
                if not ok:
                    logger.warning("sync_back: deadline exceeded waiting for active file sync; remote changes retained")
                    return False
                acquired.append(lock)
            self._sync_back_deadline = deadline
            return self._sync_back_transaction(
                hermes_home, max_attempts=max_attempts,
                bulk_download_fn=bulk_download_fn, deadline=deadline)
        finally:
            if len(acquired) == 2:
                self._sync_back_deadline = None
            for lock in reversed(acquired):
                lock.release()


    def _sync_back_transaction(
        self,
        hermes_home: Path | None = None,
        *,
        max_attempts: int | None = None,
        bulk_download_fn: BulkDownloadFn | None = None,
        deadline: float | None = None,
    ) -> bool:
        """Execute sync-back against a stable snapshot of manager state."""
        download_fn = (
            self._bulk_download_fn
            if bulk_download_fn is None
            else bulk_download_fn
        )
        if download_fn is None and self._selective_download_fn is None:
            return True

        # Nothing was ever committed through this manager — the initial
        # push failed or never ran. Skip sync_back to avoid retry storms
        # against an uninitialized remote .hermes/ directory.
        if not self._pushed_hashes and not self._synced_files:
            logger.debug("sync_back: no prior push state — skipping")
            return True

        lock_path = (hermes_home or get_hermes_home()) / ".sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        attempt_limit = (
            _SYNC_BACK_MAX_RETRIES if max_attempts is None else max_attempts
        )
        if attempt_limit < 1:
            raise ValueError("max_attempts must be at least 1")

        last_exc: Exception | None = None
        attempts_run = 0
        for attempt in range(attempt_limit):
            if deadline is not None and _monotonic() >= deadline:
                last_exc = TimeoutError("sync_back deadline expired")
                break
            try:
                attempts_run += 1
                self._sync_back_once(lock_path, download_fn, deadline)
                return True
            except Exception as exc:
                if self._selective_download_fn is not None and isinstance(
                    exc, (SyncBackRefused, TimeoutError, subprocess.TimeoutExpired)
                ):
                    logger.warning("sync_back: stopped without retry (%s); remote changes retained", exc)
                    return False
                last_exc = exc
                if attempt < attempt_limit - 1:
                    delay = _SYNC_BACK_BACKOFF[
                        min(attempt, len(_SYNC_BACK_BACKOFF) - 1)
                    ]
                    if deadline is not None:
                        remaining = deadline - _monotonic()
                        if remaining <= 0:
                            break
                        delay = min(delay, remaining)
                    logger.warning(
                        "sync_back: attempt %d failed (%s), retrying in %ds",
                        attempt + 1, exc, delay,
                    )
                    _sleep(delay)

        logger.warning(
            "sync_back: all %d attempts failed: %s",
            attempts_run,
            last_exc,
        )
        return False

    def _sync_back_once(
        self,
        lock_path: Path,
        bulk_download_fn: BulkDownloadFn,
        deadline: float | None,
    ) -> None:
        """Single sync-back attempt with SIGINT protection and file lock."""
        # signal.signal() only works from the main thread. In gateway
        # contexts cleanup() may run from a worker thread — skip SIGINT
        # deferral there rather than crashing.
        on_main_thread = threading.current_thread() is threading.main_thread()

        deferred_sigint: list[object] = []
        original_handler = None
        if on_main_thread:
            original_handler = signal.getsignal(signal.SIGINT)

            def _defer_sigint(signum, frame):
                deferred_sigint.append((signum, frame))
                logger.debug("sync_back: SIGINT deferred until sync completes")

            signal.signal(signal.SIGINT, _defer_sigint)
        try:
            self._sync_back_locked(lock_path, bulk_download_fn, deadline)
        finally:
            if on_main_thread and original_handler is not None:
                signal.signal(signal.SIGINT, original_handler)
                if deferred_sigint:
                    # Re-deliver the deferred Ctrl+C to the just-restored
                    # handler. ``os.kill(os.getpid(), signal.SIGINT)`` is NOT a
                    # graceful signal on Windows: os.kill only treats
                    # CTRL_C_EVENT(0)/CTRL_BREAK_EVENT(1) as console events; any
                    # other value (SIGINT == 2) routes to TerminateProcess(sig),
                    # hard-killing the CLI (exit code 2) instead of raising
                    # KeyboardInterrupt — so a Ctrl+C during a remote-backend
                    # sync-back would kill the whole session on Windows.
                    # ``signal.raise_signal`` (3.8+) invokes the handler via C
                    # ``raise()`` on every platform.
                    signal.raise_signal(signal.SIGINT)

    def _sync_back_locked(self, lock_path, bulk_download_fn, deadline):
        """Recover previous edits before downloading the current remote changes."""
        with self._sync_file_lock(lock_path):
            self._recover_pending_sync_back()
            self._recover_sync_back_intent()
            self._sync_back_impl(bulk_download_fn)

    def _sync_back_impl(self, bulk_download_fn=None) -> None:
        """Download, diff, and apply remote changes to host."""
        bulk_download_fn = bulk_download_fn or self._bulk_download_fn
        if bulk_download_fn is None and self._selective_download_fn is None:
            raise RuntimeError("_sync_back_impl called without bulk_download_fn")

        # Cache file mapping once to avoid O(n*m) from repeated iteration
        try:
            file_mapping = list(self._get_files_fn())
        except Exception:
            if self._selective_download_fn is not None:
                raise SyncBackRefused("could not enumerate sync-back mappings")
            file_mapping = []

        # A hard kill bypasses the finally below. Reclaim only old entries carrying our
        # prefix before allocating another full-tree download.
        _cleanup_stale_sync_back_temp()

        # mkstemp + close: NamedTemporaryFile keeps an exclusive handle on Windows, so the
        # backend's open(dest, "wb") / write_bytes on the same path raised PermissionError.
        fd, tar_path = tempfile.mkstemp(prefix=_sync_back_temp_prefix(), suffix=".tar")
        os.close(fd)
        try:
            max_bytes = _sync_back_max_bytes()
            upload_only = self._upload_only_host_paths | _credential_host_paths()
            if self._selective_download_fn is not None:
                self._retain_sync_back_intent(file_mapping, upload_only)
                request = self._sync_back_request(file_mapping, upload_only, max_bytes)
                self._selective_download_fn(Path(tar_path), request, self._sync_back_remaining())
            else:
                bulk_download_fn(Path(tar_path))

            # A misbehaving sandbox could produce an arbitrarily large tar.
            try:
                tar_size = os.path.getsize(tar_path)
            except OSError:
                tar_size = 0
            if tar_size > max_bytes:
                logger.warning(
                    "sync_back: remote tar is %d bytes (cap %d, override with terminal.%s) — skipping extraction",
                    tar_size, max_bytes, _SYNC_BACK_MAX_BYTES_KEY)
                raise SyncBackRefused("sync-back archive exceeds byte limit")

            staging_parent = get_hermes_home() if self._selective_download_fn is not None else None
            with tempfile.TemporaryDirectory(prefix=_sync_back_temp_prefix(), dir=staging_parent) as staging:
                with tarfile.open(tar_path) as tar:
                    if self._selective_download_fn is None:
                        tar.extractall(staging, filter="data")
                    else:
                        expanded = 0
                        for member in tar:
                            self._sync_back_remaining()
                            expanded += member.size
                            if not member.isfile() or expanded > max_bytes:
                                raise SyncBackRefused("invalid or oversized selective archive")
                            tar.extract(member, staging, filter="data")

                if self._selective_download_fn is not None:
                    self._retain_sync_back(staging, file_mapping)
                    self._recover_pending_sync_back()
                    return

                upload_only = self._upload_only_host_paths | _credential_host_paths()
                applied = 0
                for dirpath, _dirnames, filenames in os.walk(staging):
                    for fname in filenames:
                        self._sync_back_remaining()
                        staged_file = os.path.join(dirpath, fname)
                        # Remote keys are POSIX; relpath uses host separators (backslashes on Windows).
                        remote_path = "/" + Path(os.path.relpath(staged_file, staging)).as_posix()
                        applied += self._apply_staged_file(staged_file, remote_path, file_mapping, upload_only)

                if applied:
                    logger.info("sync_back: applied %d changed file(s)", applied)
                else:
                    logger.debug("sync_back: no remote changes detected")
        finally:
            try:
                os.unlink(tar_path)
            except OSError:
                pass

    def _resolve_host_path(self, remote_path: str,
                           file_mapping: list[tuple[str, str]] | None = None) -> str | None:
        """Find the host path for a known remote path from the file mapping."""
        mapping = file_mapping if file_mapping is not None else []
        for host, remote in mapping:
            if remote == remote_path:
                return host
        return None

    def _infer_host_path(self, remote_path: str, file_mapping: list[tuple[str, str]] | None = None, *,
                         upload_only_host_paths: set[str] | None = None) -> str | None:
        """Infer a host path for a new remote file by matching path prefixes: an existing
        remote->host pair whose parent directory prefixes *remote_path* gets the same
        substitution (``/root/.hermes/skills/b.md`` -> ``~/.hermes/skills/b.md``)."""
        upload_only_host_paths = upload_only_host_paths or set()
        for host, remote in file_mapping or []:
            remote_dir = posixpath.dirname(remote)  # remote paths are POSIX even on a Windows host
            # Reject unrelated parents before canonicalizing local paths. With
            # thousands of mounts this avoids repeated filesystem traversal.
            if not remote_path.startswith(remote_dir + "/"):
                continue
            if self._is_upload_only_host_path(host, upload_only_host_paths):
                continue
            return str(Path(host).parent / remote_path[len(remote_dir) + 1:])
        return None

    @staticmethod
    def _is_upload_only_host_path(host_path: str, upload_only_host_paths: set[str]) -> bool:
        return bool(upload_only_host_paths) and _resolve_host_path_str(host_path) in upload_only_host_paths


    def _sync_back_remaining(self) -> float:
        if self._sync_back_deadline is None:
            return 120.0
        remaining = self._sync_back_deadline - _monotonic()
        if remaining <= 0:
            raise TimeoutError("sync-back deadline exceeded")
        return remaining


    @contextmanager
    def _sync_file_lock(self, lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            # Windows: no flock — run without serialization
            yield
            return
        lock_fd = open(lock_path, "w", encoding="utf-8")
        try:
            if self._sync_back_deadline is None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            else:
                while True:
                    self._sync_back_remaining()
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        _sleep(min(0.05, self._sync_back_remaining()))
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            lock_fd.close()


    def _pending_sync_back(self) -> Path:
        return get_hermes_home() / ".sync-back-pending"


    def _sync_back_intent(self) -> Path:
        return get_hermes_home() / ".sync-back-intent.json"


    def _retain_sync_back_intent(self, mapping, upload_only) -> None:
        """Under the sync lock, publish recovery inputs before starting the download."""
        intent = self._sync_back_intent()
        manifest = {"identity": self._sync_back_identity, "mapping": mapping,
                    "hashes": self._pushed_hashes, "upload_only": sorted(upload_only)}
        fd, temporary = tempfile.mkstemp(prefix=".sync-back-intent-", dir=intent.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(manifest, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, intent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


    def _recover_sync_back_intent(self) -> None:
        intent = self._sync_back_intent()
        if not intent.exists():
            return
        manifest = json.loads(intent.read_text(encoding="utf-8"))
        if (self._selective_download_fn is None
                or manifest["identity"] != self._sync_back_identity):
            raise SyncBackRefused("unfinished sync-back belongs to another remote; upload blocked")
        # Use the failed session's baseline and mappings, not the fresh manager's
        # empty hashes or a mount list that may have changed since the failure.
        previous = self._get_files_fn, self._pushed_hashes, self._upload_only_host_paths
        try:
            self._get_files_fn = lambda: manifest["mapping"]
            self._pushed_hashes = manifest["hashes"]
            self._upload_only_host_paths = set(manifest["upload_only"]) | previous[2]
            self._sync_back_impl()
            if intent.exists():
                raise SyncBackRefused("unfinished sync-back recovery; upload blocked")
        finally:
            self._get_files_fn, self._pushed_hashes, self._upload_only_host_paths = previous


    def _retain_sync_back(self, staging: str, mapping: list[tuple[str, str]]) -> None:
        """Publish a complete recovery batch before touching any host files."""
        pending = self._pending_sync_back()
        with tempfile.TemporaryDirectory(prefix=".sync-back-preparing-", dir=pending.parent) as preparing:
            shutil.move(staging, str(Path(preparing) / "data"))
            data = Path(preparing) / "data"
            def fail(error):
                raise error
            files = [str((Path(directory) / name).relative_to(data))
                     for directory, _, names in os.walk(data, onerror=fail) for name in names]
            manifest = {"mapping": mapping, "files": files,
                        "upload_only": sorted(self._upload_only_host_paths | _credential_host_paths())}
            if self._sync_back_intent().exists():
                manifest["intent_digest"] = _sha256_file(str(self._sync_back_intent()))
            (Path(preparing) / "mapping.json").write_text(json.dumps(manifest), encoding="utf-8")
            os.rename(preparing, pending)


    def _recover_pending_sync_back(self) -> None:
        pending = self._pending_sync_back()
        if not pending.exists():
            return
        manifest = json.loads((pending / "mapping.json").read_text(encoding="utf-8"))
        mapping = manifest["mapping"]
        upload_only = self._upload_only_host_paths | _credential_host_paths() | set(manifest["upload_only"])
        # The batch already contains only changed files. Replay must not depend on
        # a new manager's push hashes; it is idempotent after partial application.
        hashes, self._pushed_hashes = self._pushed_hashes, {}
        try:
            data = pending / "data"
            progress = pending / "progress.json"
            cursor = json.loads(progress.read_text()) if progress.exists() else 0
            if type(cursor) is not int or not 0 <= cursor <= len(manifest["files"]):
                raise SyncBackRefused("invalid sync-back recovery progress")
            for index in range(cursor, len(manifest["files"])):
                self._sync_back_remaining()
                name = manifest["files"][index]
                staged = data / name
                remote = "/" + staged.relative_to(data).as_posix()
                self._apply_staged_file(str(staged), remote, mapping, upload_only, True)
                # Advance only after successful atomic application. A crash before
                # this rename safely replays at most the last file, not the batch.
                checkpoint = pending / "progress.tmp"
                checkpoint.write_text(json.dumps(index + 1), encoding="utf-8")
                os.replace(checkpoint, progress)
            # The archive has now been applied completely. Retire only its matching
            # download intent; a crash here still leaves a replayable completed batch.
            intent = self._sync_back_intent()
            if (intent.exists() and manifest.get("intent_digest")
                    == _sha256_file(str(intent))):
                intent.unlink()
            # Rename is the commit point. Interrupted garbage collection must not
            # leave an incomplete batch that gets replayed on the next connection.
            with tempfile.TemporaryDirectory(prefix=".sync-back-completed-", dir=pending.parent) as completed:
                os.rename(pending, Path(completed) / "batch")
        finally:
            self._pushed_hashes = hashes


    def _sync_back_request(self, mapping, upload_only, max_bytes) -> dict:
        # Parent prefixes match _infer_host_path, including newly created sibling files.
        allowed = [(host, remote) for host, remote in mapping
                   if not self._is_upload_only_host_path(host, upload_only)]
        roots = {posixpath.dirname(remote) for _, remote in allowed}
        excluded = {remote for host, remote in mapping
                    if self._is_upload_only_host_path(host, upload_only)}
        # Also protect credentials inferred underneath a mapped directory, even if a
        # credential was removed from the current mount list after the initial push.
        parents = {(str(Path(host).parent), posixpath.dirname(remote)) for host, remote in allowed}
        for host_parent, remote_parent in parents:
            self._sync_back_remaining()
            parent = Path(host_parent).resolve()
            for credential in upload_only:
                try:
                    relative = Path(credential).resolve().relative_to(parent)
                except ValueError:
                    continue
                excluded.add(posixpath.join(remote_parent, relative.as_posix()))
        return {"roots": sorted(roots), "excluded": sorted(excluded),
                "hashes": dict(self._pushed_hashes), "max_bytes": max_bytes}


    def _apply_staged_file(
        self, staged_file: str, remote_path: str, file_mapping: list[tuple[str, str]], upload_only_host_paths: set[str],
        atomic: bool = False,
    ) -> int:
        """Copy one extracted remote file onto the host if it changed since push. Returns 1 if
        applied, 0 if skipped (unchanged, unmapped, or an upload-only credential). A host file
        modified since push is overwritten with the remote version (last-write-wins) with a warning."""
        pushed_hash = self._pushed_hashes.get(remote_path)
        if pushed_hash is not None and _sha256_file(staged_file) == pushed_hash:
            return 0  # unchanged from push

        host_path = self._resolve_host_path(remote_path, file_mapping)
        if host_path is None:
            host_path = self._infer_host_path(remote_path, file_mapping, upload_only_host_paths=upload_only_host_paths)
            if host_path is None:
                logger.debug("sync_back: skipping %s (no host mapping)", remote_path)
                return 0

        if self._is_upload_only_host_path(host_path, upload_only_host_paths):
            logger.debug("sync_back: skipping upload-only credential file %s", remote_path)
            return 0

        if pushed_hash is not None and os.path.exists(host_path) and _sha256_file(host_path) != pushed_hash:
            logger.warning(
                "sync_back: conflict on %s — host modified "
                "since push, remote also changed. Applying remote version (last-write-wins).",
                remote_path)

        os.makedirs(os.path.dirname(host_path), exist_ok=True)
        if atomic:
            fd, replacement = tempfile.mkstemp(prefix=".sync-back-", dir=os.path.dirname(host_path))
            os.close(fd)
            try:
                shutil.copy2(staged_file, replacement)
                os.replace(replacement, host_path)
            finally:
                if os.path.exists(replacement):
                    os.unlink(replacement)
        else:
            shutil.copy2(staged_file, host_path)
        return 1
