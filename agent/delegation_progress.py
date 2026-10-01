"""Bounded, read-only work evidence and a local receipt-driven outbox (POSIX).

No model, network sender, worker control, or import-time background activity.
Manifest paths are operator authority; event contents never supply authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict


SCHEMA_VERSION = 1
SOURCE = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h",
          ".cpp", ".hpp", ".java", ".kt", ".swift", ".sh", ".css", ".scss", ".html"}
DOCS = {".md", ".rst"}
CONFIG_NAMES = {"pyproject.toml", "package.json", "tsconfig.json", "Cargo.toml", "Makefile"}
SECRET = re.compile(r"(?:secret|credential|auth|token|password|private.?key|^id_rsa|^id_ed25519)", re.I)
EXCLUDED = {".git", ".codex", ".hermes", ".ssh", ".aws", ".venv", "venv",
            "node_modules", "__pycache__", ".pytest_cache", ".delegation-progress"}


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _path(value, *, exists=True):
    if not isinstance(value, (str, Path)) or not Path(value).is_absolute():
        raise ValueError("absolute_path_required")
    path = Path(value)
    if path != path.resolve(strict=exists):
        raise ValueError("symlink_or_noncanonical_path")
    return path


@dataclass(frozen=True)
class Manifest:
    run_id: str
    worktree: Path
    approved_root: Path
    artifact_root: Path
    thread_id: str
    task_label: str
    event_path: Path | None = None
    receipt_path: Path | None = None
    coordinator_stage: str = "working"
    cli_status: str = "running"
    pid: int | None = None
    process_start: str | None = None
    schema_version: int = SCHEMA_VERSION
    code_scope: tuple[str, ...] = ()
    worker_cli: str = 'codex'
    sender_profile: str | None = None
    expected_bot_id: str | None = None

    def __post_init__(self):
        from scripts.delegation_progress_discord_send import validate_profile
        validate_profile(self.sender_profile, self.expected_bot_id)
        if self.worker_cli not in ('codex', 'claude'):
            raise ValueError('worker_cli')
        if (not isinstance(self.code_scope, (tuple, list)) or len(self.code_scope) > 1024 or
                any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', p) or
                    p.startswith('/') or '..' in p.split('/') or not file_class(p) for p in self.code_scope)):
            raise ValueError('code_scope')
        object.__setattr__(self, 'code_scope', tuple(sorted(set(self.code_scope))))
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("manifest_schema")
        if not isinstance(self.run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.run_id):
            raise ValueError("run_identity")
        if not isinstance(self.thread_id, str) or not re.fullmatch(r"[0-9]{1,22}", self.thread_id):
            raise ValueError("thread_identity")
        if not isinstance(self.task_label, str) or not 1 <= len(self.task_label) <= 160:
            raise ValueError("task_label")
        if self.coordinator_stage not in ("working", "verifying", "final_verified", "stopped"):
            raise ValueError("coordinator_stage")
        if self.cli_status not in ("running", "needs_user"):
            raise ValueError("cli_status")
        for name in ("worktree", "approved_root", "artifact_root"):
            path = _path(getattr(self, name))
            if not path.is_dir():
                raise ValueError("directory_required")
            object.__setattr__(self, name, path)
        if not self.worktree.is_relative_to(self.approved_root):
            raise ValueError("worktree_outside_approval")
        if self.artifact_root.is_relative_to(self.worktree):
            raise ValueError("artifacts_must_be_outside_worktree")
        for name in ("event_path", "receipt_path"):
            value = getattr(self, name)
            if value is not None:
                path = _path(value, exists=False)
                if not path.is_relative_to(self.artifact_root) or path == self.artifact_root:
                    raise ValueError("artifact_outside_trust_root")
                object.__setattr__(self, name, path)
        if (self.pid is None) != (self.process_start is None):
            raise ValueError("pid_requires_start_identity")
        if self.pid is not None and (type(self.pid) is not int or self.pid <= 0
                                    or not isinstance(self.process_start, str)
                                    or not 1 <= len(self.process_start) <= 80):
            raise ValueError("process_identity")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("manifest_object_required")
        try:
            return cls(**value)
        except (TypeError, OSError) as exc:
            raise ValueError("invalid_manifest") from exc

    @classmethod
    def load(cls, path):
        path = _path(path)
        return cls.from_dict(json.loads(_read(path.parent, path.name, 32768)))

    def binding(self):
        values = {key: str(getattr(self, key)) for key in (
            "run_id", "worktree", "approved_root", "artifact_root", "thread_id",
            "event_path", "receipt_path")}
        if self.code_scope:
            values['code_scope'] = self.code_scope
        # Preserve existing Codex bindings, bind the additional adapter explicitly.
        if self.worker_cli != 'codex':
            values['worker_cli'] = self.worker_cli
        values.update(self.sender_identity())
        return _digest(_json(values))

    def sender_identity(self):
        # Omitted fields preserve the exact historical default binding digest.
        return ({} if self.sender_profile is None else
                dict(sender_profile=self.sender_profile, expected_bot_id=self.expected_bot_id))


@contextmanager
def _open(root, relative):
    """Walk with directory descriptors: never follow intermediate/final symlinks."""
    parts = PurePosixPath(relative).parts
    if not parts or any(p in ("..", "/") for p in parts):
        raise ValueError("unsafe_relative_path")
    fds = []
    try:
        # Also walk the absolute root; a replaced ancestor cannot redirect reads.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        fds.append(fd)
        for part in Path(root).parts[1:] + parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        fds.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("symlink_or_special")
        yield file_fd, info
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _read(root, relative, limit):
    with _open(root, relative) as (fd, info):
        if info.st_size > limit:
            raise ValueError("read_truncated")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("read_truncated")
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("read_changed")
        return data


def _bounded_command(argv, *, cwd=None, limit=1024 * 1024, timeout=10):
    """Bound both pipes while the process is running, not after communicate()."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    process = subprocess.Popen(argv, cwd=cwd, shell=False, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("command_timeout")
                for key, _ in selector.select(.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("command_truncated")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            if process.wait(timeout=max(.001, deadline - time.monotonic())):
                raise ValueError("command_failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()


def _git(manifest, *args):
    if manifest.worktree.resolve(strict=True) != manifest.worktree:
        raise ValueError("worktree_boundary_changed")
    return _bounded_command(["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                             "-c", "core.untrackedCache=false", *args], cwd=manifest.worktree,
                            limit=4 * 1024 * 1024)


def file_class(path):
    parts = PurePosixPath(path).parts
    if not parts or any(p in EXCLUDED or p.lower().startswith(".env") or SECRET.search(p)
                        or p.startswith(".git") for p in parts):
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in SOURCE:
        return "tests" if any(p in ("tests", "test", "__tests__") for p in parts) or parts[-1].startswith("test_") else "source"
    if suffix in DOCS:
        return "docs"
    if parts[-1] in CONFIG_NAMES:
        return "build"
    return None


def collect(manifest, *, max_files=1024, max_bytes=8 * 1024 * 1024, per_file=512 * 1024):
    """Inventory allowlisted source, including clean tracked files between polls.

    Index and HEAD blob IDs distinguish staging/committing from worktree edits.
    No source contents, command output, or credential hashes leave this function.
    """
    result = {"files": {}, "errors": [], "available": False, "head": None,
              "excluded": 0, "omitted": 0}
    try:
        top = _git(manifest, "rev-parse", "--show-toplevel").decode().rstrip("\n")
        if Path(top).resolve() != manifest.worktree:
            raise ValueError("not_worktree_root")
        raw = _git(manifest, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        index_raw = _git(manifest, "ls-files", "--stage", "-z")
        # An unborn repository has no HEAD, but status/index still provide evidence.
        try:
            result["head"] = _git(manifest, "rev-parse", "--verify", "HEAD").decode().strip()
        except ValueError as exc:
            if str(exc) != "command_failed":
                raise
            # Only a valid, unresolved symbolic branch is unborn. An existing ref
            # with a missing object, detached/broken HEAD, or failed tree read is
            # an actual git failure, regardless of whether the index is populated.
            branch = _git(manifest, "symbolic-ref", "-q", "HEAD").decode().strip()
            _git(manifest, "check-ref-format", branch)
            refs = _git(manifest, "for-each-ref", "--format=%(refname)").decode().splitlines()
            if not branch.startswith("refs/heads/") or branch in refs:
                raise ValueError("invalid_unborn_head")
            head_raw = b""
        else:
            head_raw = _git(manifest, "ls-tree", "-rz", "HEAD")
        statuses = {}
        records = iter(raw.split(b"\0"))
        for record in records:
            if not record:
                continue
            code, path = record[:2].decode("ascii"), os.fsdecode(record[3:])
            statuses[path] = code
            if "R" in code or "C" in code:
                old = os.fsdecode(next(records))
                statuses[old] = "D "
        index, heads = {}, {}
        for record in index_raw.split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                mode, oid, stage = meta.decode().split()
                index.setdefault(os.fsdecode(path), []).append([mode, oid, stage])
        for record in head_raw.split(b"\0"):
            if record:
                meta, path = record.split(b"\t", 1)
                heads[os.fsdecode(path)] = meta.decode().split()[2]
        names = sorted(set(statuses) | set(index) | set(heads), key=lambda path: (path not in statuses, path))
        allowed = list(manifest.code_scope) if manifest.code_scope else [path for path in names if file_class(path)]
        result["excluded"] = len(set(names) - set(allowed))
        if len(allowed) > max_files:
            result["errors"].append("files_truncated")
            result["omitted"] += len(allowed) - max_files
        budget = max_bytes
        for path in allowed[:max_files]:
            entry = {"class": file_class(path), "status": statuses.get(path, "  "),
                     "index": index.get(path), "head": heads.get(path), "content": None,
                     "known": True}
            try:
                content = _read(manifest.worktree, path, min(per_file, max(0, budget)))
                budget -= len(content)
                entry["content"] = _digest(content)
            except FileNotFoundError:
                # Missing tracked paths are deletions; absent untracked paths raced status.
                if path not in heads and path not in index and path not in manifest.code_scope:
                    entry["known"] = False
                    result["errors"].append("file_changed_during_observation")
            except (OSError, ValueError) as exc:
                entry["known"] = False
                reason = "content_truncated" if str(exc) == "read_truncated" else "file_unreadable"
                if isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.ENOTDIR) or str(exc) == "symlink_or_special":
                    reason = "symlink_or_special"
                result["errors"].append(reason)
                result["omitted"] += 1
            result["files"][path] = entry
        # Do not call a concurrent stage/commit a stable observation.
        if raw != _git(manifest, "status", "--porcelain=v1", "-z", "--untracked-files=all") or index_raw != _git(manifest, "ls-files", "--stage", "-z"):
            result["errors"].append("git_changed_during_observation")
        result["available"] = not result["errors"]
    except (OSError, ValueError, UnicodeError, StopIteration, subprocess.SubprocessError) as exc:
        result["errors"].append("git_unavailable")
        if str(exc) == "command_truncated":
            result["errors"].append("git_truncated")
    result["errors"] = sorted(set(result["errors"]))
    return result


def _changes(previous, current, *, head_changed=False):
    changes = []
    for path in sorted(set(previous) | set(current)):
        old, new = previous.get(path), current.get(path)
        if old == new or old is not None and not old["known"] or new is not None and not new["known"]:
            continue
        kinds = []
        if new is None or new["content"] is None and old and old["content"] is not None:
            kinds.append("deleted")
            if new is None and old["head"] and head_changed:
                kinds.append("committed")
        elif old is None:
            kinds.append("added")
        elif old["content"] != new["content"]:
            kinds.append("content")
        if old and new:
            if old["head"] != new["head"]:
                kinds.append("committed")
            elif (old["status"] != "  " and new["status"] == "  "
                  and old["content"] != new["content"]):
                kinds.append("reverted")
            if old["index"] != new["index"] and new["status"][0] not in (" ", "?"):
                kinds.append("staged")
            if old["status"] != new["status"] and not kinds:
                kinds.append("status")
        if kinds:
            changes.append({"path": path, "class": (new or old)["class"], "kinds": kinds})
    return changes


def _likely_pytest(command, *, depth=0):
    """Identify a test attempt, NEVER authorize a pass or interpret its stdout.

    Read executable positions through common wrappers. Incomplete/unsupported
    arguments after a known test executable still invalidate an older result.
    Quoted source passed to echo/printf/python -c is never an executable position.
    """
    if not isinstance(command, str) or depth > 4:
        return False
    command = command[:4096]
    # This narrow shell prefix is common in Codex commands; it cannot grant pass
    # recognition. Do not search arbitrary shell/source text for the word pytest.
    command = re.sub(r"^\s*cd\s+[A-Za-z0-9_./-]+\s*&&\s*", "", command, count=1)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = []
    try:
        words.extend(lexer)
    except ValueError:
        pass  # Keep only complete prefix tokens preceding malformed arguments.
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)
    if not words:
        return False
    executable = Path(words[0]).name
    if executable in ("bash", "zsh", "sh"):
        if len(words) >= 3 and words[1] in ("-c", "-lc"):
            return _likely_pytest(words[2], depth=depth + 1)
        return len(words) >= 2 and words[1] in ("scripts/run_tests.sh", "./scripts/run_tests.sh")
    if executable == "env":
        args = words[1:]
        while args:
            if args[0] in ("-u", "--unset", "-C", "--chdir"):
                args = args[2:]
            elif args[0] in ("-i", "--ignore-environment", "--") or re.fullmatch(
                    r"(?:[A-Za-z_][A-Za-z0-9_]*|--unset|--chdir)=.*", args[0]):
                args = args[1:]
            else:
                break
        return _likely_pytest(shlex.join(args), depth=depth + 1)
    if executable in ("uv", "poetry", "pipenv") and words[1:2] == ["run"]:
        return _likely_pytest(shlex.join(words[2:]), depth=depth + 1)
    if executable in ("timeout", "gtimeout"):
        args = words[1:]
        while args and args[0].startswith("-"):
            args = args[2:] if args[0] in ("-k", "--kill-after", "-s", "--signal") else args[1:]
        if args and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[smhd]?", args[0]):
            return _likely_pytest(shlex.join(args[1:]), depth=depth + 1)
        return False
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", executable):
        args = words[1:]
        while args:
            if args[0] in ("-W", "-X"):
                args = args[2:]
            elif args[0] in ("-B", "-u", "-I", "-E", "-s", "-S", "-O", "-OO") or args[0].startswith(("-W", "-X")):
                args = args[1:]
            else:
                break
        return args[:2] == ["-m", "pytest"]
    return executable in ("pytest", "pytest-3") or words[0] in (
        "scripts/run_tests.sh", "./scripts/run_tests.sh")


def _pytest_identity(command, *, canonical_only=False):
    if not isinstance(command, str) or len(command) > 4096:
        return False
    # The common Codex shell envelope is accepted only for one simple command.
    try:
        words = shlex.split(command)
        if len(words) == 3 and words[0] in ("/bin/bash", "/bin/zsh", "/bin/sh") and words[1] in ("-c", "-lc"):
            command = words[2]
        if re.search(r"[^A-Za-z0-9_./: =-]", command):
            return False
        words = shlex.split(command)
    except ValueError:
        return False
    while words and re.fullmatch(r'HERMES_(?:PYTHON=[A-Za-z0-9_./-]+|TEST_FILE_RETRIES=0)', words[0]):
        words.pop(0)
    if not words:
        return False
    executable = Path(words[0]).name
    if words[:2] == ["bash", "scripts/run_tests.sh"] or words[:1] == ["scripts/run_tests.sh"]:
        args = words[2:] if words[0] == "bash" else words[1:]
        while args[:1] in (["-j"], ["--file-timeout"]):
            if len(args) < 2 or not re.fullmatch(r"[1-9][0-9]{0,3}", args[1]):
                return False
            args = args[2:]
        return bool(args) and all(re.fullmatch(r"tests/[A-Za-z0-9_./]+", arg) and ".." not in arg for arg in args)
    elif canonical_only:
        return False
    elif executable in ("python", "python3") and words[1:3] == ["-m", "pytest"]:
        args = words[3:]
    elif executable in ("pytest", "pytest-3"):
        args = words[1:]
    else:
        return False
    # No -c, -k, plugins, code execution wrappers, output redirection, or arbitrary options.
    return all(arg in ("-q", "-v", "-vv", "-x", "--disable-warnings", "--no-header",
                       "--tb=short", "--tb=long", "--tb=no") or
               not arg.startswith("-") and re.fullmatch(r"[A-Za-z0-9_./:]+", arg)
               for arg in args)


def _test_summary(output, code, *, canonical=False):
    if not isinstance(output, str) or type(code) is not int:
        return {"status": "unknown"}
    if code != 0:
        return {"status": "failed", "exit_code": code}
    if canonical:
        matches = re.findall(r"^=== Summary: ([0-9]{1,8}) files, ([0-9]{1,8}) tests passed, ([0-9]{1,8}) failed(?:, ([0-9]{1,8}) skipped)? \(100% complete\) in [0-9]+\.[0-9]+s \([0-9]+ workers\) ===$", output, re.M)
        if len(matches) != 1 or int(matches[0][0]) <= 0 or int(matches[0][1]) <= 0 or int(matches[0][2]):
            return {"status": "unknown"}
        return {"status": "passed", "passed": int(matches[0][1]), "skipped": int(matches[0][3] or 0),
                "exit_code": 0, "scope": "canonical_command"}
    # Last nonempty line must be a complete pytest summary, with real duration.
    lines = output.strip().splitlines()
    if not lines:
        return {"status": "unknown"}
    match = re.fullmatch(r"(?:=+ )?((?:[0-9]+ [a-z]+)(?:, [0-9]+ [a-z]+)*) in [0-9]+(?:\.[0-9]+)?s(?: =+)?", lines[-1])
    if not match:
        return {"status": "unknown"}
    counts = {}
    for count, name in re.findall(r"([0-9]+) ([a-z]+)", match[1]):
        if name not in ("passed", "failed", "error", "errors", "skipped", "deselected", "xfailed", "xpassed", "warning", "warnings") or name in counts or len(count) > 8:
            return {"status": "unknown"}
        counts[name] = int(count)
    if counts.get("passed", 0) <= 0 or any(counts.get(key) for key in ("failed", "error", "errors", "xpassed")):
        return {"status": "unknown"}
    return {"status": "passed", "passed": counts["passed"], "skipped": counts.get("skipped", 0), "exit_code": 0}


def _claude_event(value, state, now):
    """Stream-json activity only; raw text/tool inputs never become report data.

    Read-only Claude has no trusted test runner. A model's result text is never
    treated as a test receipt or as accepted completion.
    """
    if not isinstance(value, dict):
        return
    kind = value.get('type')
    message = value.get('message', {})
    if kind not in ('assistant', 'user') or not isinstance(message, dict):
        return
    content = message.get('content', [])
    if not isinstance(content, list):
        return
    for block in content[:256]:
        if not isinstance(block, dict):
            continue
        block_kind = block.get('type')
        if kind == 'assistant' and block_kind == 'tool_use' and block.get('name') in ('Read', 'Glob', 'Grep'):
            raw_id, phase = block.get('id'), 'read_started'
        elif kind == 'user' and block_kind == 'tool_result':
            raw_id, phase = block.get('tool_use_id'), 'read_completed'
        elif kind == 'assistant' and block_kind == 'text':
            raw_id, phase = message.get('id'), 'response_observed'
        else:
            continue
        if not isinstance(raw_id, str) or not 1 <= len(raw_id) <= 256:
            continue
        identity = _digest((raw_id + phase).encode())
        if identity in state['seen_events']:
            continue
        state['seen_events'] = (state['seen_events'] + [identity])[-256:]
        state['execution'] = {'phase': phase, 'observed_at': now}
        state['execution_since_queue'] = True


def _event(value, state, now):
    if not isinstance(value, dict) or value.get("type") not in ("item.started", "item.completed"):
        return
    item = value.get("item")
    if not isinstance(item, dict) or item.get("type") not in ("command_execution", "file_change"):
        return
    if not isinstance(item.get("id"), str) or not 1 <= len(item["id"]) <= 256:
        return
    completed = value["type"] == "item.completed"
    if item.get("status") != ("completed" if completed else "in_progress"):
        return
    identity = _digest((item["id"] + value["type"]).encode())
    if identity in state["seen_events"]:
        return
    state["seen_events"] = (state["seen_events"] + [identity])[-256:]
    phase = "file_change_completed" if item["type"] == "file_change" and completed else "execution_started"
    if item["type"] == "command_execution" and completed:
        phase = "execution_completed"
    state["execution"] = {"phase": phase, "observed_at": now}
    state["execution_since_queue"] = True
    if item["type"] != "command_execution":
        return
    command = item.get("command")
    command_hash = _digest(command.encode()) if isinstance(command, str) else None
    key = _digest(item["id"].encode())
    runs = state.setdefault("command_runs", {})
    prior = runs.get(key)
    if prior and prior["completed"]:
        return  # A delayed started event must never reopen a completed item.
    likely = _likely_pytest(command)
    if prior is None:
        runs[key] = {"command": command_hash, "test": likely, "completed": completed,
                     "order": max((run["order"] for run in runs.values()), default=0) + 1,
                     "code_fingerprint": state.get('current_fingerprint') if not completed else None}
        if likely:
            state["latest_test"] = key
        # Match the existing bounded event-dedup window; no raw commands stored.
        if len(runs) > 256:
            # JSON persistence sorts dictionary keys; eviction must use event order.
            del runs[min(runs, key=lambda old: runs[old]["order"])]
    else:
        prior["completed"] = completed
    if not likely and not (prior and prior["test"]):
        return
    if state.get("latest_test") != key:
        return  # Completion of an older overlapping test is historical only.
    trusted = _pytest_identity(command) and (prior is None or prior["command"] == command_hash)
    state["tests"] = (_test_summary(item.get("aggregated_output"), item.get("exit_code"),
                                   canonical=_pytest_identity(command, canonical_only=True))
                      if completed and trusted else {"status": "unknown" if completed else "in_progress"})
    state["tests"]["observed_at"] = now
    state['tests']['run_id'] = state.get('run_id')
    state['tests']['evidence_ref'] = identity
    bound = runs[key].get('code_fingerprint')
    state['tests']['code_fingerprint'] = bound if bound == state.get('current_fingerprint') else None


def _events(manifest, state, now, *, limit=1024 * 1024):
    if manifest.event_path is None:
        return []
    try:
        relative = str(manifest.event_path.relative_to(manifest.artifact_root))
        with _open(manifest.artifact_root, relative) as (fd, info):
            identity = [info.st_dev, info.st_ino]
            cursor = state.get("cursor")
            if cursor is None:
                state["cursor"] = {"identity": identity, "offset": info.st_size}
                return []  # Existing event history is not new progress.
            if cursor["identity"] is None:
                cursor["identity"] = identity
            if cursor["identity"] != identity or info.st_size < cursor["offset"]:
                state["cursor"] = {"identity": identity, "offset": info.st_size}
                state["tests"] = {"status": "unknown"}
                state["execution"] = {"phase": "unknown"}
                return ["events_replaced_or_truncated"]
            os.lseek(fd, cursor["offset"], os.SEEK_SET)
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                # Do not parse a partial/overflowed record. Rebaseline explicitly.
                cursor["offset"] = info.st_size
                state["tests"] = {"status": "unknown"}
                return ["events_truncated"]
            end = data.rfind(b"\n") + 1
            errors = []
            for line in data[:end].splitlines():
                try:
                    value = json.loads(line)
                    (_claude_event if manifest.worker_cli == 'claude' else _event)(value, state, now)
                except (ValueError, UnicodeError, RecursionError):
                    errors.append("events_invalid")
            cursor["offset"] += end  # Incomplete trailing record is retried, never persisted raw.
            return sorted(set(errors))
    except FileNotFoundError:
        # Remember that registration preceded file creation, so first new events count.
        state.setdefault("cursor", {"identity": None, "offset": 0})
        return ["events_unavailable"]
    except (OSError, ValueError):
        return ["events_unavailable"]


TERMINAL = {"cli_completed", "cli_failed", "timed_out", "cancelled", "launch_failed",
            "output_limit", "artifact_or_transport_failed", "registration_failed"}


def _receipt(manifest, state):
    if manifest.receipt_path is None:
        return []
    try:
        raw = _read(manifest.artifact_root, str(manifest.receipt_path.relative_to(manifest.artifact_root)), 32768)
        value = json.loads(raw)
        if (not isinstance(value, dict) or not isinstance(value.get("status"), str)
                or value["status"] not in TERMINAL or type(value.get("exit_code")) is not int):
            raise ValueError("invalid_receipt")
        if (value["status"] == "cli_completed") != (value["exit_code"] == 0):
            raise ValueError("inconsistent_receipt")
        receipt = {"status": value["status"], "exit_code": value["exit_code"]}
        if state.get("receipt") and state["receipt"] != receipt:
            raise ValueError("receipt_changed")
        state["receipt"] = receipt
        return []
    except FileNotFoundError:
        # Missing is expected only before the first terminal receipt exists.
        return ["receipt_unavailable"] if state.get("receipt") else []
    except (OSError, ValueError, RecursionError):
        return ["receipt_unavailable"]


def _probe(pid, start):
    try:
        identity = _bounded_command(["ps", "-p", str(pid), "-o", "lstart="], limit=1024, timeout=2).decode().strip()
        return "alive" if identity == start else "unknown"
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return "unknown"


def safe_source_name(path):
    """Conservative relative source names only; never reinterpret media or links.

    Reject rather than repair attacker-controlled names. ASCII punctuation is
    allowlisted and long/high-entropy credential-like segments are suppressed.
    Labels and all file contents remain local. Underscores are escaped for Discord.
    """
    if (not isinstance(path, str) or not 1 <= len(path) <= 110
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or path.startswith(('/', '.')) or any(p in ('', '.', '..') for p in path.split('/'))
            or not file_class(path) or re.search(r"MEDIA|https?|www\.|[A-Za-z0-9]{24,}|(?:sk|ghp|github_pat|xox[baprs])[-_]", path, re.I)):
        return "안전한 이름 표시 불가"
    return path.replace('_', r'\_')


from agent.delegation_progress_policy import render, fingerprint, plan


def _private_dir(path):
    _path(path, exists=False)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_directory_required")


@contextmanager
def _lock(path, *, wait=False):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("private_lock_required")
        deadline = time.monotonic() + (10 if wait else 0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ValueError("run_locked") from exc
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def _atomic(path, data):
    encoded = _json(data)
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("state_limit")
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_registration(request, manifest_path, state_dir, thread_id, task_label, *, code_scope=(),
                          sender_profile=None, expected_bot_id=None):
    path = _path(manifest_path, exists=False)
    root = _path(state_dir, exists=False)
    if path.exists() or path.is_relative_to(request.workdir) or root.is_relative_to(request.workdir):
        raise ValueError("new_external_registration_required")
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_manifest_parent_required")
    Manifest("registration-check", request.workdir, request.allowed_root, request.output_dir,
             thread_id, task_label, code_scope=code_scope,
             sender_profile=sender_profile, expected_bot_id=expected_bot_id)


def register_run(request, run_dir, manifest_path, state_dir, thread_id, task_label, *, code_scope=(),
                 sender_profile=None, expected_bot_id=None):
    """Opt-in callback. Durable baseline precedes exclusive manifest publication."""
    validate_registration(request, manifest_path, state_dir, thread_id, task_label, code_scope=code_scope,
                          sender_profile=sender_profile, expected_bot_id=expected_bot_id)
    manifest = Manifest(uuid.uuid4().hex, request.workdir, request.allowed_root, run_dir,
                        thread_id, task_label, run_dir / "events.jsonl", run_dir / "status.json",
                        code_scope=code_scope, worker_cli=request.cli,
                        sender_profile=sender_profile, expected_bot_id=expected_bot_id)
    progress = Progress(manifest, state_dir)
    snapshot = progress.tick()["snapshot"]
    # A bounded inventory is a valid, explicitly partial baseline. A failed
    # git/event read is not registration. Preserve all coverage gaps in state.
    if set(snapshot['errors']) - {'files_truncated', 'content_truncated', 'symlink_or_special'}:
        raise ValueError("registration_baseline_unavailable")
    target = Path(manifest_path)
    data = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(manifest).items()}
    # Hard-link publication is atomic and refuses replacement of an existing run.
    fd, name = tempfile.mkstemp(prefix=".register-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, target, follow_symlinks=False)
        os.unlink(name)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return manifest


def set_stage(manifest_path, stage, *, dry_run=False):
    """Explicit coordinator command; immutable routing remains unchanged."""
    from dataclasses import replace
    if stage == "final_verified":
        raise ValueError("final_verified_requires_begin_validation_and_record_validation")
    path = _path(manifest_path)
    manifest = replace(Manifest.load(path), coordinator_stage=stage)
    if not dry_run:
        with _lock(path.with_suffix('.lock'), wait=True):
            current = Manifest.load(path)
            if current.binding() != manifest.binding():
                raise ValueError("manifest_identity_changed")
            _atomic(path, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(manifest).items()})
    return {"status": stage, "run_id": manifest.run_id}


class Progress:
    def __init__(self, manifest, state_dir, *, interval=1200, clock=time.time, process_probe=None, light_poll=False):
        if not isinstance(manifest, Manifest):
            raise ValueError("validated_manifest_required")
        if isinstance(interval, bool) or not math.isfinite(interval) or not 1 <= interval <= 86400:
            raise ValueError("interval")
        self.manifest = manifest
        self.root = _path(state_dir, exists=False)
        if self.root.is_relative_to(manifest.worktree) or self.root == manifest.artifact_root:
            raise ValueError("state_must_be_separate")
        self.directory = self.root / manifest.run_id
        self.path = self.directory / "state.json"
        self.interval, self.clock, self.process_probe = interval, clock, process_probe
        self._watching = False
        self.manifest_error = False
        self.light_poll = light_poll
        self._collection_cache = None

    def _stat_signature(self):
        """Bounded metadata only. Uncertainty falls back to full collection."""
        signatures = []
        try:
            paths = []
            for directory, dirs, files in os.walk(self.manifest.worktree, followlinks=False):
                dirs[:] = sorted(d for d in dirs if file_class(str(
                    (Path(directory) / d / 'probe.py').relative_to(self.manifest.worktree))))
                paths.append(Path(directory))  # additions/deletions alter directory metadata
                paths.extend(Path(directory) / f for f in files
                             if file_class(str((Path(directory) / f).relative_to(self.manifest.worktree))))
                if len(paths) > 4096:
                    return None
            paths.extend(p for p in (self.manifest.event_path, self.manifest.receipt_path,
                                    self.manifest.worktree / '.git') if p is not None)
            for path in paths:
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        return None
                    signatures.append((str(path), info.st_dev, info.st_ino, info.st_size,
                                       info.st_mtime_ns, info.st_ctime_ns, info.st_mode))
                except FileNotFoundError:
                    signatures.append((str(path), None))
            return signatures
        except OSError:
            return None

    def _collect(self, state, now, *, force_full=False):
        # Only background working-phase polling uses the cache. Public snapshots,
        # validation tickets and every terminal/verification observation stay exact.
        eligible = (not force_full and self.light_poll and self._watching and not state.get('validation')
                    and self.manifest.coordinator_stage == 'working'
                    and not state.get('receipt') and state['tests'].get('status') == 'unknown')
        signature = self._stat_signature() if eligible else None
        cached = self._collection_cache
        if (signature is not None and cached and 0 <= now - cached[0] < 60
                and signature == cached[1]):
            return deepcopy(cached[2])
        result = collect(self.manifest)
        # Changes during collection cannot authorize a cache hit.
        if signature is not None and result['available'] and signature == self._stat_signature():
            self._collection_cache = (now, signature, deepcopy(result))
        else:
            self._collection_cache = None
        return result

    def _load(self):
        try:
            state = json.loads(_read(self.directory, "state.json", 16 * 1024 * 1024))
        except FileNotFoundError:
            return {"schema_version": 2, "binding": self.manifest.binding(),
                    "previous": None, "accumulated": {}, "cumulative": {}, "last_queued_at": None,
                    "sequence": 0, "pending": [], "delivered": None, "stopped": False,
                    "tests": {"status": "unknown"}, "execution": {"phase": "unknown"},
                    "seen_events": [], "execution_since_queue": False, "terminal_seen": []}
        if (not isinstance(state, dict) or state.get("schema_version") not in (1, 2)
                or state.get("binding") != self.manifest.binding()):
            raise ValueError("state_identity_or_schema_mismatch")
        required = {"previous", "accumulated", "cumulative", "last_queued_at", "sequence",
                    "pending", "delivered", "stopped", "tests", "execution", "seen_events",
                    "execution_since_queue", "terminal_seen"}
        if not required <= state.keys():
            raise ValueError("invalid_state")
        for key in ("accumulated", "cumulative", "tests", "execution"):
            if not isinstance(state[key], dict):
                raise ValueError("invalid_state")
        for key in ("pending", "seen_events", "terminal_seen"):
            if not isinstance(state[key], list):
                raise ValueError("invalid_state")
        if type(state["sequence"]) is not int or state["sequence"] < 0 or type(state["stopped"]) is not bool:
            raise ValueError("invalid_state")
        if not isinstance(state.get("command_runs", {}), dict) or type(state.get("waiting", False)) is not bool:
            raise ValueError("invalid_state")
        if state["schema_version"] == 1:
            raise ValueError("v1_requires_explicit_migrate_or_legacy_drain")
        return state

    @contextmanager
    def _transaction(self):
        _private_dir(self.root)
        _private_dir(self.directory)
        with _lock(self.directory / "state.lock", wait=True):
            yield

    @contextmanager
    def watcher(self):
        """Lifetime fencing distinct from short transactions, so ack remains usable."""
        _private_dir(self.root)
        _private_dir(self.directory)
        with _lock(self.directory / "watch.lock"):
            self._watching = True
            try:
                yield self
            finally:
                self._watching = False

    def _observe(self, state, now, *, force_full=False):
        observation = self._collect(state, now, force_full=force_full)
        previous = state["previous"]
        delta = _changes(previous["files"], observation["files"],
                         head_changed=previous["head"] != observation["head"]) if previous else []
        if previous and "files_truncated" in previous["errors"]:
            delta = [c for c in delta if c["path"] in previous["files"]]
        # Incomplete global inventory must never make absent paths look deleted.
        if not observation["available"]:
            delta = [c for c in delta if c["path"] in observation["files"] and observation["files"][c["path"]]["known"]]
        if delta:
            state["latest_changes"] = delta
        for change in delta:
            path = change["path"]
            state["accumulated"][path] = change
            state["cumulative"][path] = change["class"]
        if observation["available"] or previous is None and "git_unavailable" not in observation["errors"]:
            state["previous"] = observation
        elif previous:
            # Retain unknown/missing entries, update only independently read files.
            previous["files"].update({p: e for p, e in observation["files"].items() if e["known"]})
        state['current_fingerprint'] = fingerprint(observation)
        state['run_id'] = self.manifest.run_id
        errors = observation["errors"] + _events(self.manifest, state, now) + _receipt(self.manifest, state)
        tests = state['tests']
        tests['applicable'] = bool(tests.get('status') == 'passed' and tests.get('code_fingerprint') and
                                  tests['code_fingerprint'] == state['current_fingerprint'] and
                                  tests.get('run_id') == self.manifest.run_id)
        from agent.delegation_progress_evidence import applicable_validation
        validation = applicable_validation(self, state, observation)

        liveness = "unknown"
        if self.manifest.pid is not None:
            try:
                liveness = (self.process_probe or _probe)(self.manifest.pid, self.manifest.process_start)
                if liveness not in ("alive", "unknown"):
                    liveness = "unknown"
            except (OSError, ValueError):
                errors.append("process_probe_unavailable")
        if self.manifest_error:
            errors.append("manifest_unavailable")
        if not errors:
            state["last_complete_observation_at"] = now
        current_errors = sorted(set(errors))
        errors = sorted(set(errors + state.get("observation_errors", [])))
        state["observation_errors"] = errors
        receipt = state.get("receipt", {})
        return {"run_id": self.manifest.run_id, "observed_at": now,
                "worker_cli": self.manifest.worker_cli,
                "task_label": self.manifest.task_label, "code_fingerprint": state["current_fingerprint"],
                "validation": validation,
                "baseline": previous is None, "available": observation["available"] and not current_errors,
                "errors": sorted(set(errors)), "omitted": observation["omitted"],
                "current_errors": current_errors,
                "last_complete_observation_at": state.get("last_complete_observation_at"),
                "changes": delta, "display_changes": state.get("latest_changes", []),
                "files_available": observation["available"],
                "cumulative_classes": {key: list(state["cumulative"].values()).count(key)
                                       for key in ("source", "tests", "docs", "build")},
                "coordinator_stage": self.manifest.coordinator_stage,
                "liveness": liveness, "execution": state["execution"], "tests": state["tests"],
                "execution_since_queue": state["execution_since_queue"],
                "exit_status": receipt.get("status", self.manifest.cli_status),
                "exit_code": receipt.get("exit_code")}

    def snapshot(self, *, now=None):
        state = self._load()
        if state['stopped'] or state.get('closing'):
            # Accepted run evidence belongs to its frozen code fingerprint,
            # including while final deliveries are still awaiting acknowledgement.
            return state['final_snapshot']
        return self._observe(state, self.clock() if now is None else now, force_full=True)

    def _tick(self, state, now):
        if state["stopped"]:
            return {"snapshot": state["final_snapshot"], "queued": [], "stopped": True}
        snapshot = state['final_snapshot'] if state.get('closing') else self._observe(state, now)
        queued = []
        if state.get('closing'):
            # Frozen accepted terminal observation. Delivery must drain before stop.
            snapshot = state['final_snapshot']
        else:
            for operation, event_id, event, content in plan(state, snapshot, now, self.interval):
                if len(state['pending']) >= 32:
                    raise ValueError('outbox_full')
                state['sequence'] += 1
                message = {'id': f"{self.manifest.run_id}:{state['sequence']}",
                           'run_id': self.manifest.run_id, 'sequence': state['sequence'],
                           'thread_id': self.manifest.thread_id, 'observed_at': now,
                           'operation': operation, 'event_id': event_id, 'event': event,
                           'content': content, 'allowed_mentions': {'parse': [], 'replied_user': False}}
                message.update(self.manifest.sender_identity())
                state['pending'].append(message)
                queued.append(message)
        if state.get('closing') and not state['pending']:
            state['stopped'] = True
        return {'snapshot': snapshot, 'queued': queued, 'stopped': state['stopped']}

    def tick(self, *, now=None, dry_run=False):
        now = self.clock() if now is None else now
        if isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("observation_clock")
        if dry_run:
            return self._tick(self._load(), now)
        with self._transaction():
            with self._tick_fence():
                state = self._load()
                result = self._tick(state, now)
                _atomic(self.path, state)
                return result

    @contextmanager
    def _tick_fence(self):
        if self._watching:
            yield
        else:
            with _lock(self.directory / "watch.lock"):
                yield

    def peek(self):
        pending = self._load()["pending"]
        return pending[0] if pending else None

    def ack(self, message_id, *, dry_run=False):
        def consume(state):
            if not state["pending"] or state["pending"][0]["id"] != message_id:
                raise ValueError("ack_requires_exact_head_id")
            message = state["pending"].pop(0)
            state["delivered"] = {"id": message["id"], "observed_at": message["observed_at"]}
            if state.get('closing') and not state['pending']:
                state['stopped'] = True
            return {"acked": message_id}
        if dry_run:
            return consume(self._load())
        with self._transaction():
            state = self._load()
            result = consume(state)
            _atomic(self.path, state)
            return result
