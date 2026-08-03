"""Repository cache and execution-owned Git worktree preparation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Callable

GitRunner = Callable[[tuple[str, ...]], str]

_STABLE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_EXECUTION_RE = re.compile(r"wx_[a-f0-9]{24}")
_HEAD_SHA_RE = re.compile(r"[a-f0-9]{40}")
_HEAD_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}")


class WorkspacePreparationError(RuntimeError):
    """A trusted workspace could not be prepared."""


class WorkspaceConflictError(WorkspacePreparationError):
    """An existing worktree is dirty or belongs to another execution."""


@dataclass(frozen=True)
class WorktreeRequest:
    execution_id: str
    repository_node_id: str
    base_repository: str
    head_repository: str
    head_ref: str
    head_sha: str

    def __post_init__(self) -> None:
        if _EXECUTION_RE.fullmatch(self.execution_id) is None:
            raise ValueError("execution_id is invalid")
        if _STABLE_ID_RE.fullmatch(self.repository_node_id) is None:
            raise ValueError("repository_node_id is invalid")
        if _REPOSITORY_RE.fullmatch(self.base_repository) is None:
            raise ValueError("base_repository is invalid")
        if _REPOSITORY_RE.fullmatch(self.head_repository) is None:
            raise ValueError("head_repository is invalid")
        if (
            _HEAD_REF_RE.fullmatch(self.head_ref) is None
            or self.head_ref.startswith(("-", ".", "/"))
            or self.head_ref.endswith(("/", ".", ".lock"))
            or ".." in self.head_ref
            or "//" in self.head_ref
            or "@{" in self.head_ref
        ):
            raise ValueError("head_ref is invalid")
        if _HEAD_SHA_RE.fullmatch(self.head_sha) is None:
            raise ValueError("head_sha is invalid")


def _github_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _anonymous_git_environment(home: Path) -> dict[str, str]:
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    inherited_keys = (
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    )
    environment = {
        key: value
        for key in inherited_keys
        if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_KEY_1": "core.askPass",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home),
        }
    )
    return environment


def _default_git_runner(
    argv: tuple[str, ...],
    *,
    home: Path,
) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            env=_anonymous_git_environment(home),
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspacePreparationError("Git workspace preparation failed") from exc
    return completed.stdout.strip()


@contextmanager
def _repository_process_lock(
    root: Path,
    repository_node_id: str,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    if _STABLE_ID_RE.fullmatch(repository_node_id) is None:
        raise ValueError("repository_node_id is invalid")
    lock_path = root / "locks" / f"{repository_node_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(lock_file.fileno(), mode, 1)
            except OSError as exc:
                if not blocking:
                    raise BlockingIOError from exc
                raise
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        operation = fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(lock_file.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class RepositoryWorktreeManager:
    """Prepare one clean worktree for one durable execution."""

    def __init__(self, root: str | Path, *, runner: GitRunner | None = None) -> None:
        self._root = Path(root).expanduser().resolve()
        self._runner = runner or partial(
            _default_git_runner,
            home=self._root / "anonymous-git-home",
        )
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def workspace_for(self, execution_id: str) -> Path:
        if _EXECUTION_RE.fullmatch(execution_id) is None:
            raise ValueError("execution_id is invalid")
        return (self._root / "executions" / execution_id).resolve()

    def prepare(self, request: WorktreeRequest) -> Path:
        with self._repository_lock(request.repository_node_id):
            with _repository_process_lock(
                self._root,
                request.repository_node_id,
            ):
                return self._prepare_locked(request)

    def _repository_lock(self, repository_node_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repository_node_id, threading.Lock())

    def _prepare_locked(self, request: WorktreeRequest) -> Path:
        cache = (
            self._root / "repositories" / request.repository_node_id
        ).resolve()
        workspace = self.workspace_for(request.execution_id)
        if workspace.exists():
            self._validate_existing(workspace, request)
            return workspace

        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            self._runner(
                ("git", "clone", "--bare", _github_url(request.base_repository), str(cache))
            )
        else:
            origin = self._runner(
                ("git", "-C", str(cache), "remote", "get-url", "origin")
            )
            if origin.rstrip("/") != _github_url(request.base_repository):
                raise WorkspaceConflictError("repository cache origin changed")

        ref = f"refs/hermes-inbox/{request.execution_id}"
        self._runner(
            (
                "git",
                "-C",
                str(cache),
                "fetch",
                "--no-tags",
                "--force",
                _github_url(request.head_repository),
                f"{request.head_sha}:{ref}",
            )
        )
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._runner(
            (
                "git",
                "-C",
                str(cache),
                "worktree",
                "add",
                "--detach",
                str(workspace),
                ref,
            )
        )
        self._write_marker(workspace, request)
        return workspace

    def _validate_existing(self, workspace: Path, request: WorktreeRequest) -> None:
        marker_path = self._marker_path(request.execution_id)
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceConflictError("worktree ownership marker is invalid") from exc
        if marker != asdict(request):
            raise WorkspaceConflictError("worktree belongs to a different execution")
        if self._runner(
            ("git", "-C", str(workspace), "status", "--porcelain")
        ).strip():
            raise WorkspaceConflictError("execution worktree is dirty")
        if self._runner(
            ("git", "-C", str(workspace), "rev-parse", "HEAD")
        ).strip() != request.head_sha:
            raise WorkspaceConflictError("execution worktree HEAD changed")

    def _marker_path(self, execution_id: str) -> Path:
        return (self._root / "ownership" / f"{execution_id}.json").resolve()

    def _write_marker(self, workspace: Path, request: WorktreeRequest) -> None:
        if not workspace.exists():
            raise WorkspacePreparationError("execution worktree was not created")
        marker_path = self._marker_path(request.execution_id)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(request), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(marker_path)
