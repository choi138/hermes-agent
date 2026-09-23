"""Durable Discord development jobs driven by Codex on an SSH work host.

The gateway owns only the queue and delivery cursor.  The Codex process lives
on the work host, so a gateway restart cannot terminate an in-flight edit.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    ingress_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    ack_message_id TEXT NOT NULL DEFAULT '',
    notified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dev_codex_pending ON jobs(status, updated_at);
"""


def config_for(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config or {}
    raw = cfg.get("dev_codex") or {}
    terminal = cfg.get("terminal") or {}
    if not isinstance(raw, dict):
        return {"enabled": False}
    return {
        "enabled": raw.get("enabled") is True,
        "workspace": str(raw.get("workspace") or "").strip(),
        "ssh_host": str(raw.get("ssh_host") or terminal.get("ssh_host") or "").strip(),
        "ssh_user": str(raw.get("ssh_user") or terminal.get("ssh_user") or "").strip(),
        "ssh_key": str(raw.get("ssh_key") or terminal.get("ssh_key") or "").strip(),
        "remote_root": str(raw.get("remote_root") or "").strip(),
        "codex_bin": str(raw.get("codex_bin") or "codex").strip(),
        "codex_shell": str(raw.get("codex_shell") or "").strip(),
        "route": str(raw.get("route") or "dev").strip(),
    }


def should_dispatch(*, platform: str, route: str, prompt: str, config: dict[str, Any]) -> bool:
    """Send direct edit requests, not discussion about edits, to Mac Codex."""
    if platform != "discord" or not config.get("enabled"):
        return False
    if route != config.get("route"):
        return False
    text = prompt.strip()
    if text.startswith("!dev "):
        return bool(text[5:].strip())
    # The dev model route also handles architecture questions. Match an
    # instruction at the end of the message so quoted requirements such as
    # "수정 가능한 건..." cannot turn a question into an edit job.
    return bool(re.search(
        r"(?:고쳐|수정|구현|만들어|반영|리팩터링|업데이트|커밋|병합)(?:해)?\s*"
        r"(?:줘|주세요|줄래|주실래요|주실 수 있나요)\s*[.!?~]*$"
        r"|^(?:(?:please|can you|could you)\s+)?"
        r"(?:fix|implement|refactor|build|edit|update|modify|commit|merge)\b"
        r"(?!\s+(?:explain|describe|tell))",
        text, re.IGNORECASE,
    ))


def validate_config(config: dict[str, Any]) -> str | None:
    if not config.get("workspace") or not config["workspace"].startswith("/"):
        return "dev_codex.workspace must be an absolute Mac path"
    if not config.get("ssh_host") or not config.get("ssh_user"):
        return "dev_codex needs an SSH host and user"
    if not config.get("ssh_key") or not Path(config["ssh_key"]).is_file():
        return "dev_codex needs a readable SSH private key on the gateway"
    if not config.get("remote_root", "").startswith("/"):
        return "dev_codex.remote_root must be an absolute Mac path"
    return None


@dataclass(frozen=True)
class Job:
    id: str
    ingress_key: str
    source: dict[str, Any]
    prompt: str
    workspace: str
    status: str
    result: str
    error: str
    ack_message_id: str
    notified: bool


class JobStore:
    def __init__(self, path: Path | None = None):
        self.path = path or get_hermes_home() / "dev_codex_jobs.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(row["id"], row["ingress_key"], json.loads(row["source"]),
                   row["prompt"], row["workspace"], row["status"],
                   row["result"], row["error"], row["ack_message_id"], bool(row["notified"]))

    def create(self, *, ingress_key: str, source: dict[str, Any], prompt: str,
               workspace: str) -> Job:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE ingress_key=?", (ingress_key,)).fetchone()
            if row is not None:
                return self._job(row)
            row = db.execute(
                "SELECT * FROM jobs WHERE workspace=? AND status IN ('queued','running') "
                "ORDER BY created_at LIMIT 1", (workspace,),
            ).fetchone()
            if row is not None:
                return self._job(row)
            db.execute(
                "INSERT OR IGNORE INTO jobs(id, ingress_key, source, prompt, workspace, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, ingress_key, json.dumps(source), prompt, workspace, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE ingress_key=?", (ingress_key,)).fetchone()
        return self._job(row)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def latest_for_source(self, platform: str, chat_id: str, thread_id: str) -> Job | None:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
        for row in rows:
            job = self._job(row)
            src = job.source
            if (src.get("platform"), src.get("chat_id"), src.get("thread_id") or "") == (platform, chat_id, thread_id):
                return job
        return None

    def pending(self) -> list[Job]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') OR "
                "(status IN ('done','blocked') AND notified=0) ORDER BY created_at"
            ).fetchall()
        return [self._job(row) for row in rows]

    def active_for_source(self, platform: str, chat_id: str, thread_id: str) -> Job | None:
        for job in self.pending():
            src = job.source
            if job.status in {"queued", "running"} and (
                src.get("platform"), src.get("chat_id"), src.get("thread_id") or ""
            ) == (platform, chat_id, thread_id):
                return job
        return None

    def set_ack(self, job_id: str, message_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET ack_message_id=? WHERE id=?", (message_id, job_id))

    def update(self, job_id: str, status: str, *, result: str = "", error: str = "") -> None:
        if status not in {"queued", "running", "done", "blocked"}:
            raise ValueError(status)
        with self._connect() as db:
            db.execute("UPDATE jobs SET status=?, result=?, error=?, updated_at=? WHERE id=?",
                       (status, result, error, time.time(), job_id))

    def mark_notified(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET notified=1, updated_at=? WHERE id=?", (time.time(), job_id))


class RemoteCodex:
    def __init__(self, config: dict[str, Any]):
        error = validate_config(config)
        if error:
            raise ValueError(error)
        self.config = config

    def _target(self) -> str:
        return f"{self.config['ssh_user']}@{self.config['ssh_host']}"

    def _ssh_base(self) -> list[str]:
        return ["ssh", "-i", self.config["ssh_key"], "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes", self._target()]

    def _run(self, command: str, *, input_text: str | None = None) -> str:
        proc = subprocess.run(self._ssh_base() + [command], input=input_text,
                              text=True, capture_output=True, timeout=45)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:500] or f"SSH exited {proc.returncode}")
        return proc.stdout.strip()

    def _worker_command(self, action: str, job_id: str) -> str:
        root = self.config["remote_root"]
        return f"python3 {shlex.quote(root + '/worker.py')} {shlex.quote(action)} {shlex.quote(job_id)} {shlex.quote(root)}"

    def start(self, job: Job) -> dict[str, Any]:
        root = self.config["remote_root"]
        self._run(f"mkdir -p {shlex.quote(root)}")
        # Copy the small work-host entry point on dispatch.  A running worker
        # owns its already-loaded code, and a reconnect gets the current file.
        script = Path(__file__).resolve().parents[1] / "scripts" / "dev_codex_remote.py"
        proc = subprocess.run(
            ["scp", "-i", self.config["ssh_key"], "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=yes", str(script),
             f"{self._target()}:{root}/worker.py"],
            text=True, capture_output=True, timeout=45,
        )
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])
        payload = json.dumps({"prompt": job.prompt, "workspace": job.workspace,
                              "codex_bin": self.config["codex_bin"],
                              "codex_shell": self.config["codex_shell"]})
        return json.loads(self._run(self._worker_command("start", job.id), input_text=payload))

    def status(self, job_id: str) -> dict[str, Any]:
        return json.loads(self._run(self._worker_command("status", job_id)))
