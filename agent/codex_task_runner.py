"""Deterministic, opt-in local Codex delegation. No classifier or parent state.

The trusted caller supplies an already approved root and sandbox. Effort is
advice, never authority. This POSIX launcher owns a fresh process group and
records CLI completion only; acceptance testing remains the caller's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
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

TIER_EFFORTS = {
    "light": "low", "standard": "medium", "deep": "high",
    "xhigh": "xhigh", "max": "max",
}
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
                "policy": self.policy,
                "source": "user_pin" if self.policy == "pinned" else "legacy_tier",
                "effort": TIER_EFFORTS[selected]}

    @classmethod
    def for_effort(cls, effort, *, pinned=False):
        tiers = {value: key for key, value in TIER_EFFORTS.items()}
        if effort not in tiers:
            raise ValueError("Unsupported effort")
        tier = tiers[effort]
        return cls(tier, "pinned" if pinned else "auto", tier if pinned else None)


def _absolute_existing(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Paths must be absolute")
    return path.resolve(strict=True)


CLAUDE_ADVISORS = ("opus",)
# Per-run only: the advisor tool needs feature-flag fetching, which the user-level
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 turns off. Never edit settings.json.
ADVISOR_SETTINGS = '{"env":{"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":""}}'


@dataclass(frozen=True)
class TaskRequest:
    spec: Path
    workdir: Path
    allowed_root: Path
    output_dir: Path
    selection: WorkerSelection = field(default_factory=WorkerSelection)
    sandbox: str = "read-only"
    timeout: float = 600
    model: str = "gpt-6.1-sol"
    cli: str = "codex"
    policy_input: dict | None = None
    advisor: str | None = None

    def __post_init__(self):
        if os.name != "posix":
            raise ValueError("This launcher requires POSIX process groups")
        if not isinstance(self.selection, WorkerSelection):
            raise ValueError("A validated worker selection is required")
        if self.sandbox not in ("read-only", "workspace-write"):
            raise ValueError("Unsupported sandbox")
        if self.cli not in ("codex", "claude"):
            raise ValueError("Unsupported CLI")
        if self.advisor is not None and (self.cli != "claude" or self.advisor not in CLAUDE_ADVISORS):
            raise ValueError("Advisor is supported only for the Claude CLI with an approved advisor model")
        if self.cli == "claude" and self.sandbox != "read-only":
            raise ValueError("Claude currently supports read-only tasks only")
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
        _checked_policy_receipt(None, self)

    def argv(self):
        _checked_policy_receipt(None, self)
        if self.cli == "claude":
            return ["claude", "--print", "--verbose", "--output-format", "stream-json",
                    "--no-session-persistence", "--model", self.model,
                    "--effort", self.selection.metadata()["effort"],
                    "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep",
                    "--allowedTools", "Read,Glob,Grep", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}',
                    *(("--advisor", self.advisor, "--settings", ADVISOR_SETTINGS) if self.advisor else ())]
        return ["codex", "exec", "--ephemeral", "-m", self.model, "-c",
                f'model_reasoning_effort="{self.selection.metadata()["effort"]}"',
                "-s", self.sandbox, "-C", str(self.workdir), "--json", "-"]

    def inspect(self, policy_receipt=None):
        effort = self.selection.metadata()["effort"]
        return {"status": "planned", "argv": self.argv(), "selection": self.selection.metadata(),
                "sandbox": self.sandbox, "timeout_seconds": self.timeout,
                "policy": _checked_policy_receipt(policy_receipt, self),
                "configuration": {
                    "requested": {"model": self.model, "effort": effort},
                    "serialized": {"model": self.model, "effort": effort},
                    "observed": {"model": None, "effort": None},
                },
                "acceptance": {"status": "unknown", "independent_validation": False}}


def _checked_policy_receipt(receipt, request):
    """Recompute policy at the executor boundary; a receipt is not authority."""
    selected = {"model": request.model, "effort": request.selection.metadata()["effort"]}
    if request.cli == "codex":
        from agent.codex_worker_policy import PolicyInput, decide_worker
        if request.policy_input is None:
            values = {"model_override": request.model,
                      "effort_override": selected["effort"]}
            if request.selection.policy == "pinned":
                values.pop("effort_override")
                values["pinned_effort"] = selected["effort"]
        else:
            if not isinstance(request.policy_input, dict):
                raise ValueError("Policy input must be a mapping")
            values = json.loads(json.dumps(request.policy_input))
            if len(json.dumps(values)) > 65536:
                raise ValueError("Policy input exceeds size limit")
            if "handoff_refs" in values:
                if not isinstance(values["handoff_refs"], list):
                    raise ValueError("Handoff references must be a list or tuple")
                values["handoff_refs"] = tuple(values["handoff_refs"])
        try:
            expected = decide_worker(PolicyInput(**values)).receipt()
        except TypeError as exc:
            raise ValueError("Invalid policy input fields") from exc
        if expected["action"] != "spawn":
            raise ValueError("Policy forbids execution: " + expected["next_action"])
        if expected["selected"] != selected:
            raise ValueError("Policy selection does not match execution configuration")
        if receipt is not None and receipt != expected:
            raise ValueError("Policy receipt does not match recomputed execution policy")
        return expected
    if request.policy_input is not None:
        raise ValueError("Codex policy input cannot be applied to Claude")
    if receipt is None:
        return {"policy_version": None, "action": "spawn", "task_class": None,
                "selected": selected, "reason": "Claude explicit pass-through",
                "override_source": "caller_override", "attempt": 1}
    if not isinstance(receipt, dict) or len(json.dumps(receipt)) > 65536:
        raise ValueError("Invalid policy receipt")
    if receipt.get("action") != "spawn" or receipt.get("selected") != selected:
        raise ValueError("Policy receipt forbids execution or mismatches configuration")
    return json.loads(json.dumps(receipt))


def _runtime_evidence(path):
    observed = {"model": None, "effort": None}
    usage = None
    try:
        with open(path, "rb") as stream:
            for raw in stream:
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") in ("thread.started", "turn.started", "session.config"):
                    if isinstance(event.get("model"), str):
                        observed["model"] = event["model"]
                    if event.get("reasoning_effort") in ("low", "medium", "high", "xhigh", "max"):
                        observed["effort"] = event["reasoning_effort"]
                candidate = event.get("usage")
                if event.get("type") == "turn.completed":
                    candidate = candidate if isinstance(candidate, dict) else {}
                    normalized = {}
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens",
                                "reasoning_tokens", "cache_write_input_tokens"):
                        value = (candidate.get("reasoning_output_tokens", candidate.get(key))
                                 if key == "reasoning_tokens" else candidate.get(key))
                        normalized[key] = value if type(value) is int and value >= 0 else None
                    if usage is None:
                        usage = normalized
                    else:
                        usage = {key: (usage[key] + value if usage[key] is not None
                                       and value is not None else None)
                                 for key, value in normalized.items()}
    except OSError:
        pass
    return observed, usage


def _private_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _stop_group(process):
    # start_new_session gives this task its own process group, including
    # children that keep pipes open after the leader exits. Never signal the
    # calling terminal's group. Always reap the immediate child.
    import psutil

    def signal_owned(sig):
        # poll()/wait() may already have reaped the leader. A new process
        # reusing its PID is not ours, even if its group has the same number.
        expected = getattr(process, "_hermes_started_at", None)
        try:
            leader = psutil.Process(process.pid)
            if expected is not None and leader.create_time() != expected:
                return
        except psutil.NoSuchProcess:
            pass
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin can report EPERM for the group of an already reaped
            # sandboxed child. Suppress only when no owned live member remains.
            for member in psutil.process_iter(["pid", "uids", "status"]):
                try:
                    if (member.info["uids"].real == os.getuid()
                            and member.info["status"] != psutil.STATUS_ZOMBIE
                            and os.getpgid(member.pid) == process.pid):
                        raise
                except (ProcessLookupError, psutil.NoSuchProcess):
                    continue
            if process.poll() is None:
                raise

    signal_owned(signal.SIGTERM)
    try:
        process.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        pass
    signal_owned(signal.SIGKILL)
    process.wait(timeout=2)


def run_task(request: TaskRequest, *, popen=subprocess.Popen, cancel=None,
             output_limit=MAX_OUTPUT_BYTES, before_spawn=None, prompt_bytes=None,
             policy_receipt=None):
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
    if prompt_bytes is not None:
        if not isinstance(prompt_bytes, bytes):
            raise ValueError("Explicit prompt must be bytes")
        prompt = prompt_bytes
    if not prompt or len(prompt) > MAX_SPEC_BYTES:
        raise ValueError("SPEC must be nonempty and within size limit")
    prompt.decode("utf-8")  # Reject malformed text; no guessing or replacement.
    effort = request.selection.metadata()["effort"]
    policy = _checked_policy_receipt(policy_receipt, request)
    run_dir = Path(tempfile.mkdtemp(prefix="codex-task-", dir=request.output_dir))
    result = {"status": "launch_failed", "exit_code": 127, "process_returncode": None,
              "selection": request.selection.metadata(), "sandbox": request.sandbox,
              "model": request.model, "cli": request.cli,
              "timeout_seconds": request.timeout, "artifact_dir": str(run_dir),
              "policy": policy,
              "configuration": {
                  "requested": {"model": request.model, "effort": effort},
                  "serialized": {"model": request.model, "effort": effort},
                  "observed": {"model": None, "effort": None},
              },
              "execution": {"status": "launch_failed", "cli_exit_code": None},
              "acceptance": {"status": "unknown", "independent_validation": False},
              "metrics": {
                  "attempt": policy.get("attempt", 1), "wall_time_seconds": None,
                  "usage": None, "worker_cost_usd": None,
                  "coordinator_usage": None, "review_usage": None,
              },
              "input_receipt": {"sha256": hashlib.sha256(prompt).hexdigest(),
                                "bytes": len(prompt), "written_bytes": 0, "pipe_complete": False}}
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
                import psutil
                try:
                    process._hermes_started_at = psutil.Process(process.pid).create_time()
                except psutil.NoSuchProcess:
                    pass
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
                                    selector.unregister(key.fileobj)
                                    key.fileobj.close()
                                    continue
                                result["input_receipt"].update(
                                    written_bytes=position, pipe_complete=position == len(prompt))
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
            try:
                _stop_group(process)
            except (OSError, subprocess.TimeoutExpired):
                result.update(status="cleanup_failed", exit_code=74)
            result["process_returncode"] = process.returncode
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
    observed, usage = _runtime_evidence(run_dir / "events.jsonl")
    result["configuration"]["observed"] = observed
    result["metrics"]["usage"] = usage
    result["metrics"]["wall_time_seconds"] = round(time.monotonic() - started, 6)
    result["execution"] = {
        "status": result["status"],
        "cli_exit_code": result["process_returncode"],
    }
    # If this write/flush fails, propagate it: the CLI must exit nonzero.
    with _private_file(run_dir / "status.json") as status_file:
        status_file.write((json.dumps(result, ensure_ascii=True) + "\n").encode())
    return result
