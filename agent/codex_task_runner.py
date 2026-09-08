"""Deterministic, opt-in local Codex delegation. No classifier or parent state.

The trusted caller supplies an already approved root and sandbox. Effort is
advice, never authority. This POSIX launcher owns a fresh process group and
records CLI completion only; acceptance testing remains the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time

TIER_EFFORTS = {"light": "low", "standard": "medium", "deep": "high", "max": "max"}
MAX_SPEC_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class WorkerSelection:
    requested_tier: str = "standard"
    policy: str = "auto"
    pinned_tier: str | None = None

    def __post_init__(self):
        for tier in (self.requested_tier,):
            if not isinstance(tier, str) or tier not in TIER_EFFORTS:
                raise ValueError("Unknown worker tier")
        if self.policy not in ("auto", "pinned"):
            raise ValueError("Unknown selection policy")
        if self.policy == "pinned":
            if not isinstance(self.pinned_tier, str) or self.pinned_tier not in TIER_EFFORTS:
                raise ValueError("Pinned policy requires a valid pinned tier")
        elif self.pinned_tier is not None:
            raise ValueError("Pinned tier requires pinned policy")

    def metadata(self):
        selected = self.pinned_tier if self.policy == "pinned" else self.requested_tier
        return {"requested_tier": self.requested_tier, "selected_tier": selected,
                "policy": self.policy, "source": "user_pin" if self.policy == "pinned" else "auto",
                "effort": TIER_EFFORTS[selected]}


def _absolute_existing(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Paths must be absolute")
    return path.resolve(strict=True)


@dataclass(frozen=True)
class TaskRequest:
    spec: Path
    workdir: Path
    allowed_root: Path
    output_dir: Path
    selection: WorkerSelection = field(default_factory=WorkerSelection)
    sandbox: str = "read-only"
    timeout: float = 600
    model: str = "gpt-6-astra"

    def __post_init__(self):
        if os.name != "posix":
            raise ValueError("This launcher requires POSIX process groups")
        if not isinstance(self.selection, WorkerSelection):
            raise ValueError("A validated worker selection is required")
        if self.sandbox not in ("read-only", "workspace-write"):
            raise ValueError("Unsupported sandbox")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 86400):
            raise ValueError("Timeout must be finite and between 0 and 86400 seconds")
        if not isinstance(self.model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", self.model):
            raise ValueError("Malformed model")
        for name in ("spec", "workdir", "allowed_root", "output_dir"):
            object.__setattr__(self, name, _absolute_existing(getattr(self, name)))
        if not self.allowed_root.is_dir() or not self.workdir.is_dir():
            raise ValueError("Workdir and allowed root must be directories")
        if not self.workdir.is_relative_to(self.allowed_root):
            raise ValueError("Workdir escapes the approved root")
        if not self.spec.is_relative_to(self.allowed_root) or not self.spec.is_file():
            raise ValueError("SPEC must be a regular file inside the approved root")
        if self.spec.stat().st_size > MAX_SPEC_BYTES:
            raise ValueError("SPEC exceeds size limit")
        out = self.output_dir.stat()
        if not stat.S_ISDIR(out.st_mode) or out.st_uid != os.getuid() or out.st_mode & 0o022:
            raise ValueError("Output directory must be caller-owned and not writable by others")

    def argv(self):
        return ["codex", "exec", "--ephemeral", "-m", self.model, "-c",
                f'model_reasoning_effort="{self.selection.metadata()["effort"]}"',
                "-s", self.sandbox, "-C", str(self.workdir), "--json", "-"]

    def inspect(self):
        return {"status": "planned", "argv": self.argv(), "selection": self.selection.metadata(),
                "sandbox": self.sandbox, "timeout_seconds": self.timeout}


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _stop_group(process):
    # start_new_session gives this task its own process group, including
    # children that keep pipes open after the leader exits. Never signal the
    # calling terminal's group. Always reap the immediate child.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def run_task(request: TaskRequest, *, popen=subprocess.Popen, cancel=None,
             output_limit=MAX_OUTPUT_BYTES, before_spawn=None):
    """Run using real binary pipes; ``popen`` is a Python-only testing seam.

    Each stream is bounded. Over-limit and artifact I/O errors are failures,
    even if Codex exits zero. Raw output stays in private files, never status.
    Credentials/config are inherited by Codex without inspection or override.
    """
    if not isinstance(request, TaskRequest):
        raise ValueError("A validated TaskRequest is required")
    if not isinstance(output_limit, int) or not 0 < output_limit <= MAX_OUTPUT_BYTES:
        raise ValueError("Invalid output limit")
    # Revalidate resolved boundaries just before launch (also catch replaced
    # symlinks). Read a bounded regular SPEC without following a final symlink.
    checked = TaskRequest(**request.__dict__)
    if checked != request:
        raise ValueError("Request paths changed after validation")
    fd = os.open(request.spec, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("SPEC must be a regular file")
        prompt = source.read(MAX_SPEC_BYTES + 1)
    if not prompt or len(prompt) > MAX_SPEC_BYTES:
        raise ValueError("SPEC must be nonempty and within size limit")
    prompt.decode("utf-8")  # Reject malformed text; no guessing or replacement.
    run_dir = Path(tempfile.mkdtemp(prefix="codex-task-", dir=request.output_dir))
    result = {"status": "launch_failed", "exit_code": 127, "process_returncode": None,
              "selection": request.selection.metadata(), "sandbox": request.sandbox,
              "model": request.model, "timeout_seconds": request.timeout, "artifact_dir": str(run_dir)}
    process = None
    started = time.monotonic()
    try:
        with _private_file(run_dir / "events.jsonl") as stdout, _private_file(run_dir / "stderr.log") as stderr:
            registered = True
            if before_spawn is not None:
                try:
                    before_spawn(request, run_dir)
                except Exception:
                    registered = False
                    result.update(status="registration_failed", exit_code=74)
            if not registered:
                pass
            elif cancel is not None and cancel.is_set():
                result.update(status="cancelled", exit_code=130)
            else:
                process = popen(request.argv(), cwd=str(request.workdir), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                start_new_session=True, bufsize=0)
                result.update(status="running", exit_code=1)
                with selectors.DefaultSelector() as selector:
                    for stream, event, data in ((process.stdin, selectors.EVENT_WRITE, None),
                                               (process.stdout, selectors.EVENT_READ, stdout),
                                               (process.stderr, selectors.EVENT_READ, stderr)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, event, data)
                    position = 0
                    sizes = {stdout: 0, stderr: 0}
                    while selector.get_map() or process.poll() is None:
                        if cancel is not None and cancel.is_set():
                            result.update(status="cancelled", exit_code=130)
                            break
                        if time.monotonic() - started >= request.timeout:
                            result.update(status="timed_out", exit_code=124)
                            break
                        for key, _ in selector.select(.05):
                            if key.data is None:
                                try:
                                    position += os.write(key.fd, prompt[position:position + 4096])
                                except BrokenPipeError:
                                    position = len(prompt)
                                if position == len(prompt):
                                    selector.unregister(key.fileobj)
                                    key.fileobj.close()
                            else:
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    key.fileobj.close()
                                    continue
                                destination = key.data
                                remaining = output_limit - sizes[destination]
                                destination.write(chunk[:remaining])
                                if before_spawn is not None:
                                    destination.flush()  # Opt-in live evidence, including short events.
                                sizes[destination] += min(len(chunk), remaining)
                                if len(chunk) > remaining:
                                    result.update(status="output_limit", exit_code=74)
                                    break
                        if result["status"] == "output_limit":
                            break
                if result["status"] == "running":
                    code = process.wait(timeout=max(.001, request.timeout - (time.monotonic() - started)))
                    result.update(status="cli_completed" if code == 0 else "cli_failed",
                                  exit_code=code if code >= 0 else 128 - code)
    except KeyboardInterrupt:
        result.update(status="cancelled", exit_code=130)
    except subprocess.TimeoutExpired:
        result.update(status="timed_out", exit_code=124)
    except OSError:
        result.update(status="launch_failed" if process is None else "artifact_or_transport_failed",
                      exit_code=127 if process is None else 74)
    finally:
        if process is not None:
            _stop_group(process)
            result["process_returncode"] = process.returncode
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    # If this write/flush fails, propagate it: the CLI must exit nonzero.
    with _private_file(run_dir / "status.json") as status_file:
        status_file.write((json.dumps(result, ensure_ascii=True) + "\n").encode())
    return result
