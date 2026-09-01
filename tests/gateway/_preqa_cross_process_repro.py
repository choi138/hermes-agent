"""Pre-QA-only process-boundary reproducer for baseline a5c46b9a3.

This is not a production test and must not be merged. It intentionally starts
producer and notifier in distinct Python processes and shares only a temporary
SQLite database path between them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRODUCER = r'''
import json
from hermes_cli import kanban_db as kb
kb.init_db()
conn = kb.connect()
try:
    tid = kb.create_task(conn, title="cross-process role delivery", assignee="shinei")
    kb.add_notify_sub(
        conn, task_id=tid, platform="discord", chat_id="channel-42",
        thread_id="thread-77", notifier_profile="default",
    )
    task = kb.claim_task(conn, tid)
    kb.complete_task(
        conn, tid, summary="SHINEI_COMPLETION_MUST_BE_SENT_BY_SHINEI",
        expected_run_id=task.current_run_id,
    )
    print(json.dumps({"task_id": tid, "run_id": task.current_run_id}))
finally:
    conn.close()
'''

CONSUMER = r'''
import asyncio
import json
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import config as cfg
from hermes_cli import kanban_db as kb

class DefaultOnlyAdapter:
    def __init__(self): self.sent = []
    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})
    async def handle_message(self, event): pass

async def main():
    cfg.load_config = lambda: {"kanban": {"dispatch_in_gateway": True}}
    adapter = DefaultOnlyAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}  # no cross-process object/registry sharing
    runner._kanban_notifier_profile = "default"
    runner._kanban_sub_fail_counts = {}
    runner._active_profile_name = lambda: "default"
    real_sleep = asyncio.sleep
    async def one_tick(delay):
        if delay == 5: return None
        runner._running = False
        await real_sleep(0)
    asyncio.sleep = one_tick
    await runner._kanban_notifier_watcher(interval=1)
    conn = kb.connect()
    try:
        remaining_subs = kb.list_notify_subs(conn)
    finally:
        conn.close()
    print(json.dumps({"default_process_sent": adapter.sent, "remaining_subs": remaining_subs}))

asyncio.run(main())
'''


def run_process(code: str, env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        text=True, capture_output=True, check=False,
    )
    if proc.returncode:
        raise SystemExit(json.dumps({
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }))
    return {"pid_isolated_output": json.loads(proc.stdout), "stderr": proc.stderr}


with tempfile.TemporaryDirectory(prefix="hermes-preqa-") as td:
    db = str(Path(td) / "board.db")
    env = dict(os.environ)
    env["HERMES_KANBAN_DB"] = db
    env["HERMES_KANBAN_DISPATCH_IN_GATEWAY"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    producer = run_process(PRODUCER, env)
    consumer = run_process(CONSUMER, env)
    print(json.dumps({"db": db, "producer": producer, "consumer": consumer}, indent=2))
