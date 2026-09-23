"""Read project context through the terminal session's filesystem namespace.

Prompt reads never close a live terminal. A cold SSH read owns only its
probe-only connection, and every remote failure leaves project context empty.
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace

from agent.deadline import resolve_timeout, run_bounded_sync

logger = logging.getLogger(__name__)
_PROBE_SLOTS = threading.BoundedSemaphore(4)


@dataclass
class PromptBackend:
    config: dict
    task_id: str
    cwd: str
    env: object = None

    @property
    def kind(self) -> str:
        return self.config["env_type"]

    @property
    def is_remote(self) -> bool:
        if self.kind == "local":
            return False
        from agent.terminal_env_registry import provider_flag

        # A plugin explicitly using the host filesystem may read local paths.
        # Unknown non-local names cannot grant host files prompt authority.
        return bool(provider_flag(self.kind, "is_remote", True))


def resolve_prompt_backend(task_id: str | None = None, cwd=None) -> PromptBackend:
    from agent.runtime_cwd import _session_cwd_override
    from gateway.session_context import get_session_env
    from tools import terminal_tool as tt
    from tools.file_tools_paths import _terminal_env_type_for_task
    from tools.terminal_tool_lifecycle import get_active_env

    task_id = task_id or get_session_env("HERMES_SESSION_ID") or "default"
    config = tt._get_env_config()
    overrides = tt.resolve_task_overrides(task_id)
    config = {**config, **overrides}
    env = get_active_env(task_id)
    if env is not None:
        config["env_type"] = _terminal_env_type_for_task(task_id)
        if config["env_type"] == "ssh":
            # A live session's transport owns the target, not ambient config.
            for key in ("host", "user", "port"):
                config["ssh_" + key] = getattr(env, key, config.get("ssh_" + key))
    selected_cwd = overrides.get("cwd") or _session_cwd_override() or config["cwd"]
    if tt._is_container_backend(config["env_type"]) and tt._is_unusable_container_cwd(selected_cwd):
        selected_cwd = "/workspace" if tt._resolve_task_host_cwd(config, task_id) else config["cwd"]
    selected_cwd = tt._resolve_command_cwd(
        workdir=str(cwd) if cwd is not None else None,
        default_cwd=selected_cwd,
        session_key=task_id,
        env_type=config["env_type"],
    )
    return PromptBackend(config, task_id, selected_cwd, env)


def read_backend(backend: PromptBackend, operation):
    """Bound connection setup, all reads and owned cleanup by one deadline."""
    timeout = resolve_timeout("prompt.backend", default=8.0) or 8.0
    if not _PROBE_SLOTS.acquire(blocking=False):
        return None
    deadline = time.monotonic() + timeout

    def work():
        from tools.terminal_tool_backends import _create_environment, _ssh_config_from_config
        from tools.terminal_tool_lifecycle import ensure_task_env
        from tools.terminal_tool import _resolve_container_task_id

        owned = None
        try:
            env = backend.env
            if env is None and backend.kind == "ssh":
                owned = env = _create_environment(
                    env_type="ssh", image="", cwd=backend.cwd, timeout=timeout,
                    ssh_config=_ssh_config_from_config(backend.config),
                    task_id=_resolve_container_task_id(backend.task_id), probe_only=True,
                )
            elif env is None:
                # A shared sandbox is owned by its terminal lifecycle, not this read.
                env = ensure_task_env(backend.task_id)
            if env is None:
                return None

            def execute(command):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("prompt backend deadline")
                result = env.execute(command, cwd=backend.cwd, timeout=remaining)
                if time.monotonic() >= deadline:
                    raise TimeoutError("prompt backend deadline")
                return result

            return operation(execute)
        finally:
            try:
                if owned is not None:
                    owned.cleanup()
            finally:
                _PROBE_SLOTS.release()

    try:
        result = run_bounded_sync(work, timeout, label="prompt-backend")
        return None if result.timed_out else result.value
    except Exception as exc:
        # Backend exception text can include command output or credentials.
        logger.debug("Prompt backend read failed (%s)", type(exc).__name__)
        return None


class BackendPath:
    """The read-only Path operations context discovery needs, backed by a shell."""

    def __init__(self, path, execute):
        self.path = PurePosixPath(path)
        self.execute = execute

    @classmethod
    def working_directory(cls, execute):
        result = execute("pwd -P")
        path = result.get("output", "").strip()
        if result.get("returncode") != 0 or not path.startswith("/") or "\n" in path:
            raise OSError("Remote working directory unavailable")
        metadata = {}

        def cached_execute(command):
            if command.startswith("test "):
                if command not in metadata:
                    metadata[command] = execute(command)
                return metadata[command]
            return execute(command)

        return cls(path, cached_execute)

    def __str__(self):
        return str(self.path)

    def __fspath__(self):
        return str(self)

    def __eq__(self, other):
        return isinstance(other, BackendPath) and self.path == other.path

    def __lt__(self, other):
        return self.path < other.path

    def __truediv__(self, name):
        return BackendPath(self.path / name, self.execute)

    def joinpath(self, *parts):
        return BackendPath(self.path.joinpath(*parts), self.execute)

    @property
    def name(self):
        return self.path.name

    @property
    def parents(self):
        return [BackendPath(parent, self.execute) for parent in self.path.parents]

    def resolve(self):
        # working_directory() already got the shell's physical absolute cwd.
        return self

    def relative_to(self, other):
        return self.path.relative_to(other.path)

    def is_relative_to(self, other):
        return self.path.is_relative_to(other.path)

    def _test(self, flag):
        result = self.execute(f"test {flag} {shlex.quote(str(self))}")
        code = result.get("returncode")
        if code not in (0, 1):
            raise OSError("Remote path lookup failed")
        return code == 0

    def exists(self):
        return self._test("-e")

    def is_dir(self):
        return self._test("-d")

    def is_file(self):
        return self._test("-f")

    def read_text(self, encoding="utf-8"):
        if not self.is_file():
            raise OSError("Not a regular context file")
        result = self.execute(f"cat {shlex.quote(str(self))}")
        if result.get("returncode") != 0:
            raise OSError("Remote context read failed")
        return result.get("output", "")

    def stat(self):
        result = self.execute(f"wc -c < {shlex.quote(str(self))}")
        if result.get("returncode") != 0:
            raise OSError("Remote context stat failed")
        return SimpleNamespace(st_size=int(result.get("output", "").strip()))

    def glob(self, pattern):
        if pattern != "*.mdc":
            raise ValueError("Unsupported context-file glob")
        result = self.execute(
            f"for f in {shlex.quote(str(self))}/*.mdc; do "
            "if [ -f \"$f\" ]; then printf '%s\\0' \"$f\"; fi; done"
        )
        if result.get("returncode") != 0:
            raise OSError("Remote context listing failed")
        return [BackendPath(path, self.execute) for path in result.get("output", "").split("\0") if path]
