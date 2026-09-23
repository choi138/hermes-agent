#!/usr/bin/env python3
"""Work-host side of a durable Discord development job.

No Hermes imports: the gateway copies this file to the Mac over SSH.  The
job directory is the idempotency boundary; a reconnect observes or resumes
the same Codex session instead of issuing a second initial prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _write(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temp, path)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _status(job_dir: Path) -> dict:
    path = job_dir / "status.json"
    if not path.exists():
        return {"status": "missing"}
    value = _read(path)
    if value.get("status") == "running" and not _alive(int(value.get("pid") or 0)):
        thread_id = value.get("thread_id")
        if thread_id and int(value.get("attempts") or 1) < 3:
            value = {**value, "status": "interrupted", "error": "Codex process stopped; session can resume"}
        else:
            # The initial Codex request might have edited files before it
            # recorded a thread id.  Never run the original prompt again.
            value = {**value, "status": "blocked", "error": "Codex stopped without a recoverable session or exhausted 3 attempts; inspect the workspace"}
        _write(path, value)
    return value


def _start(job_dir: Path, root: Path) -> dict:
    try:
        job_dir.mkdir(parents=True)
    except FileExistsError:
        # Another gateway invocation may have created the directory but not
        # published the first status yet.
        for _ in range(50):
            value = _status(job_dir)
            if value.get("status") != "missing":
                break
            time.sleep(0.1)
        if value.get("status") == "interrupted":
            return _resume(job_dir, root, value)
        return value

    try:
        payload = json.load(sys.stdin)
        workspace = Path(payload["workspace"]).expanduser().resolve()
        if not workspace.is_dir() or not (workspace / ".git").exists():
            raise ValueError(f"Mac workspace is not a git repository: {workspace}")
        codex_bin = payload.get("codex_bin") or "codex"
        if shutil.which(codex_bin) is None:
            raise ValueError("codex CLI is unavailable in the SSH environment")
        (job_dir / "prompt.txt").write_text(payload["prompt"], encoding="utf-8")
        (job_dir / "workspace.txt").write_text(str(workspace), encoding="utf-8")
        (job_dir / "codex_bin.txt").write_text(codex_bin, encoding="utf-8")
        return _launch(job_dir, root, resume=False)
    except Exception as exc:
        value = {"status": "blocked", "error": str(exc)[:500]}
        _write(job_dir / "status.json", value)
        return value


def _resume(job_dir: Path, root: Path, previous: dict) -> dict:
    if not previous.get("thread_id"):
        return previous
    return _launch(job_dir, root, resume=True, thread_id=previous["thread_id"])


def _launch(job_dir: Path, root: Path, *, resume: bool, thread_id: str = "") -> dict:
    mode = "resume" if resume else "initial"
    output = (job_dir / f"{mode}.log").open("ab")
    process = subprocess.Popen(
        [sys.executable, str(root / "worker.py"), "run", job_dir.name, str(root), mode],
        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
        cwd=(job_dir / "workspace.txt").read_text(encoding="utf-8"),
        start_new_session=True,
    )
    output.close()
    value = {"status": "running", "pid": process.pid, "thread_id": thread_id,
             "mode": mode, "started_at": time.time(),
             "attempts": (int(_read(job_dir / "status.json").get("attempts") or 1) + 1
                          if resume else 1)}
    _write(job_dir / "status.json", value)
    return value


def _run(job_dir: Path, mode: str) -> int:
    status_path = job_dir / "status.json"
    # Parent writes status immediately after spawn.  Wait for that handoff.
    for _ in range(50):
        if status_path.exists() and _read(status_path).get("pid") == os.getpid():
            break
        time.sleep(0.1)
    state = _read(status_path)
    workspace = (job_dir / "workspace.txt").read_text(encoding="utf-8")
    codex_bin = (job_dir / "codex_bin.txt").read_text(encoding="utf-8")
    result_path = job_dir / "result.txt"
    if mode == "resume":
        command = [codex_bin, "exec", "resume", "--json", "-o", str(result_path),
                   "-c", 'approval_policy="never"', "-c", 'sandbox_mode="workspace-write"',
                   state["thread_id"], "-"]
        prompt = ("Resume the original development task in this workspace. Check existing "
                  "changes and progress before acting; do not repeat completed operations. "
                  "Finish and report verification.")
    else:
        command = [codex_bin, "exec", "--json", "-o", str(result_path),
                   "-C", workspace, "-s", "workspace-write", "--approve-for-me", "-"]
        prompt = (job_dir / "prompt.txt").read_text(encoding="utf-8")
    events_path = job_dir / f"{mode}.jsonl"
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, cwd=workspace) as proc:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
        with events_path.open("w", encoding="utf-8") as events:
            for line in proc.stdout:
                events.write(line)
                events.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    current = _read(status_path)
                    current["thread_id"] = event["thread_id"]
                    _write(status_path, current)
        code = proc.wait()
    current = _read(status_path)
    result = result_path.read_text(encoding="utf-8").strip() if result_path.exists() else ""
    if code == 0 and result:
        current.update(status="done", result=result[:12000], error="", ended_at=time.time())
    elif current.get("thread_id") and int(current.get("attempts") or 1) < 3:
        current.update(status="interrupted", error=f"Codex exited {code}; resuming session",
                       ended_at=time.time())
    else:
        current.update(status="blocked", error=f"Codex exited {code}; inspect {events_path}",
                       ended_at=time.time())
    _write(status_path, current)
    return code


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit("usage: worker.py start|status|run JOB_ID ROOT [initial|resume]")
    action, job_id, raw_root = sys.argv[1:4]
    if not job_id.isascii() or not job_id.isalnum():
        raise SystemExit("invalid job id")
    root = Path(raw_root).expanduser().resolve()
    job_dir = root / job_id
    if action == "start":
        print(json.dumps(_start(job_dir, root)))
    elif action == "status":
        print(json.dumps(_status(job_dir)))
    elif action == "run":
        return _run(job_dir, sys.argv[4])
    else:
        raise SystemExit("invalid action")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
