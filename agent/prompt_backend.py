"""Read-only prompt discovery in the terminal's namespace and session.

The caller owns the deadline; a probe must never close a session's transport.
Only an SSH metadata connection created here is owned (and closed) here.
"""

from __future__ import annotations

import logging
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from agent.deadline import run_bounded_sync, resolve_timeout
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_PROBE_SLOTS = threading.BoundedSemaphore(4)


@dataclass
class PromptBackend:
    config: dict
    task_id: str
    cwd: str
    env: object = None

    @property
    def kind(self):
        return self.config["env_type"]

    @property
    def is_remote(self):
        if self.kind == "local":
            return False
        from agent.terminal_env_registry import provider_flag

        # Respect plugins that explicitly use the host filesystem. Unknown
        # names fail closed: they cannot grant controller files authority.
        return bool(provider_flag(self.kind, "is_remote", True))

    def cache_key(self):
        # No credentials, forwarded environment values, or arbitrary plugin
        # config in cache keys. Sandboxes are keyed by the actual instance,
        # since two instances with the same image/cwd need not share state.
        from gateway.session_context import get_session_env

        return (
            str(get_hermes_home()), self.task_id, self.kind, self.cwd,
            get_session_env("HERMES_SESSION_PROFILE"),
            self.config.get("ssh_host"), self.config.get("ssh_user"),
            self.config.get("ssh_port"), self.env,
        )


def resolve_prompt_backend(task_id=None, cwd=None):
    from gateway.session_context import get_session_env
    from tools import terminal_tool as tt
    from tools.file_tools import _terminal_env_type_for_task
    from agent.runtime_cwd import _session_cwd_override

    task_id = task_id or get_session_env("HERMES_SESSION_ID") or "default"
    config = tt._get_env_config()
    overrides = tt.resolve_task_overrides(task_id)
    config = {**config, **overrides}
    env = tt.get_active_env(task_id)
    if env is not None:
        config["env_type"] = _terminal_env_type_for_task(task_id)
        if config["env_type"] == "ssh":
            # A live session keeps its transport until its owner replaces it.
            # Ambient config is only a seed for a new connection.
            for key in ("host", "user", "port"):
                config["ssh_" + key] = getattr(env, key, config.get("ssh_" + key))
    selected_cwd = overrides.get("cwd") or _session_cwd_override() or config["cwd"]
    if tt._is_container_backend(config["env_type"]):
        if tt._is_unusable_container_cwd(selected_cwd):
            selected_cwd = (
                "/workspace" if tt._resolve_task_host_cwd(config, task_id)
                else config["cwd"]
            )
    selected_cwd = tt._resolve_command_cwd(
        workdir=str(cwd) if cwd is not None else None,
        default_cwd=selected_cwd, session_key=task_id, env_type=config["env_type"],
    )
    return PromptBackend(config, task_id, selected_cwd, env)


def read_backend(backend, operation):
    """Bound setup, all reads, and owned cleanup with one wall-clock budget.

    A stuck backend may ignore its command timeout. The shared deadline
    primitive lets the caller return; slots remain held until workers exit,
    preventing repeated prompt builds from accumulating abandoned workers.
    """
    timeout = resolve_timeout("prompt.backend", default=8.0) or 8.0
    if not _PROBE_SLOTS.acquire(blocking=False):
        return None
    deadline = time.monotonic() + timeout

    def work():
        from tools import terminal_tool as tt

        owned = None
        try:
            env = backend.env
            if env is None and backend.kind == "ssh":
                owned = env = tt._create_environment(
                    env_type="ssh", image="", cwd=backend.cwd,
                    timeout=timeout,
                    ssh_config=tt._ssh_config_from_config(backend.config),
                    task_id=tt._resolve_container_task_id(backend.task_id),
                    probe_only=True,
                )
            elif env is None:
                # A disposable container would describe a different filesystem
                # and cleanup could destroy a persistent shared sandbox.
                env = tt.ensure_task_env(
                    backend.task_id, config=backend.config, cwd=backend.cwd,
                )
                backend.env = env
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
        # Backend exceptions can contain credentials/command output.
        logger.debug("Prompt backend read failed (%s)", type(exc).__name__)
        return None


class BackendPath:
    """Small read-only Path interface used by the existing context loaders.

    PurePosixPath handles names only. Every filesystem query runs through
    the owning backend, never through controller Path.exists/resolve/home.
    """

    def __init__(self, path, execute):
        self.path = PurePosixPath(path)
        self.execute = execute

    @classmethod
    def working_directory(cls, execute):
        result = execute("pwd -P")
        path = result.get("output", "").strip()
        if result.get("returncode") != 0 or not path.startswith("/") or "\n" in path:
            raise OSError("Remote working directory unavailable")
        # Discovery traverses the same git-root chain for multiple context
        # types. Cache metadata only for this build, avoiding duplicate SSH
        # round trips without retaining another session's filesystem state.
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

    @property
    def name(self):
        return self.path.name

    @property
    def parents(self):
        return [BackendPath(p, self.execute) for p in self.path.parents]

    def resolve(self):
        return self

    def relative_to(self, other):
        return self.path.relative_to(other.path)

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
        # Only regular files: a named pipe must not tie up a discovery worker.
        if not self._test("-f"):
            raise OSError("Not a regular context file")
        result = self.execute(f"cat {shlex.quote(str(self))}")
        if result.get("returncode") != 0:
            raise OSError("Remote context read failed")
        return result.get("output", "")

    def glob(self, pattern):
        if pattern != "*.mdc":
            raise ValueError("Unsupported context-file glob")
        result = self.execute(
            f"for f in {shlex.quote(str(self))}/*.mdc; do "
            "if [ -f \"$f\" ]; then printf '%s\\0' \"$f\"; fi; done"
        )
        if result.get("returncode") != 0:
            raise OSError("Remote context listing failed")
        return [BackendPath(p, self.execute)
                for p in result.get("output", "").split("\0") if p]
