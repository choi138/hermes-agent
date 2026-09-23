"""Durable dev-job admission and work-host process contract."""

import json
import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from gateway.dev_codex_jobs import JobStore, config_for, should_dispatch
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource


WORKER = Path(__file__).resolve().parents[2] / "scripts" / "dev_codex_remote.py"


def test_dev_route_only_dispatches_mutating_discord_requests():
    config = config_for({"dev_codex": {"enabled": True}})
    assert should_dispatch(platform="discord", route="dev", prompt="이 버그 고쳐줘", config=config)
    assert not should_dispatch(platform="discord", route="dev", prompt="왜 느렸어?", config=config)
    assert not should_dispatch(platform="discord", route="dev", prompt="고쳐야 하는 부분이 있어?", config=config)
    assert not should_dispatch(platform="slack", route="dev", prompt="fix this", config=config)


def test_duplicate_ingress_returns_same_job(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    source = {"platform": "discord", "chat_id": "1", "thread_id": "2"}
    first = store.create(ingress_key="discord:1:3", source=source, prompt="fix", workspace="/repo")
    second = store.create(ingress_key="discord:1:3", source=source, prompt="fix", workspace="/repo")
    assert first.id == second.id
    busy = store.create(ingress_key="discord:other:4", source={"platform": "discord", "chat_id": "other"},
                        prompt="edit", workspace="/repo")
    assert busy.id == first.id
    assert store.active_for_source("discord", "1", "2").id == first.id
    store.update(first.id, "done", result="ready")
    assert store.pending()[0].result == "ready"
    store.mark_notified(first.id)
    assert store.pending() == []


def test_gateway_ack_persists_thread_and_message_cursor(tmp_path):
    class Adapter:
        async def edit_message(self, **kwargs):
            return SendResult(success=True, message_id=kwargs["message_id"])

    runner = object.__new__(GatewayRunner)
    runner._dev_codex_store = JobStore(tmp_path / "jobs.sqlite3")
    runner._adapter_for_source = lambda source: Adapter()
    runner._thread_metadata_for_source = lambda source: {"thread_id": source.thread_id}
    key = tmp_path / "key"
    key.write_text("test")
    config = config_for({"dev_codex": {
        "enabled": True, "workspace": str(tmp_path), "ssh_host": "mac",
        "ssh_user": "user", "ssh_key": str(key), "remote_root": "/tmp/jobs",
    }})
    source = SessionSource(platform=Platform.DISCORD, chat_id="channel", thread_id="thread")
    event = SimpleNamespace(message_id="message", text="고쳐줘")
    asyncio.run(runner._accept_dev_codex_job(event, source, config, "ack"))
    job = runner._dev_codex_store.latest_for_source("discord", "channel", "thread")
    assert job.ingress_key == "discord:channel:message"
    assert job.ack_message_id == "ack"
    assert job.source["thread_id"] == "thread"


def test_work_host_start_survives_caller_and_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "worker.py").write_bytes(WORKER.read_bytes())
    codex = tmp_path / "codex"
    codex.write_text("#!/usr/bin/env fake_node\n")
    codex.chmod(0o755)
    fake_node = tmp_path / "fake_node"
    fake_node.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        "args=sys.argv[2:]\n"
        "if '--approve-for-me' in args and '-s' in args: sys.exit(2)\n"
        "path=pathlib.Path(args[args.index('-o')+1])\n"
        "print(json.dumps({'type':'thread.started','thread_id':'fake-session'}), flush=True)\n"
        "time.sleep(0.2)\n"
        "path.write_text('implemented and checked')\n"
    )
    fake_node.chmod(0o755)
    command = [sys.executable, str(WORKER), "start", "abc123", str(root)]
    payload = json.dumps({"prompt": "fix it", "workspace": str(repo), "codex_bin": str(codex)})
    first = json.loads(subprocess.check_output(command, input=payload.encode()))
    assert first["status"] == "running"
    second = json.loads(subprocess.check_output(command))
    assert second["pid"] == first["pid"]
    for _ in range(50):
        state = json.loads(subprocess.check_output(
            [sys.executable, str(WORKER), "status", "abc123", str(root)]
        ))
        if state["status"] == "done":
            break
        time.sleep(0.1)
    assert state["status"] == "done"
    assert state["thread_id"] == "fake-session"
    assert state["result"] == "implemented and checked"


def test_interrupted_session_resumes_same_thread(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "worker.py").write_bytes(WORKER.read_bytes())
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "if 'resume' not in args:\n"
        " print(json.dumps({'type':'thread.started','thread_id':'same-thread'}), flush=True)\n"
        " sys.exit(1)\n"
        "pathlib.Path(args[args.index('-o')+1]).write_text('resumed')\n"
    )
    codex.chmod(0o755)
    command = [sys.executable, str(WORKER), "start", "resume123", str(root)]
    payload = json.dumps({"prompt": "fix", "workspace": str(repo), "codex_bin": str(codex)})
    subprocess.check_output(command, input=payload.encode())
    for _ in range(50):
        state = json.loads(subprocess.check_output(
            [sys.executable, str(WORKER), "status", "resume123", str(root)]))
        if state["status"] == "interrupted":
            break
        time.sleep(0.1)
    assert state["status"] == "interrupted"
    restarted = json.loads(subprocess.check_output(command))
    assert restarted["mode"] == "resume"
    for _ in range(50):
        state = json.loads(subprocess.check_output(
            [sys.executable, str(WORKER), "status", "resume123", str(root)]))
        if state["status"] == "done":
            break
        time.sleep(0.1)
    assert state["status"] == "done"
    assert state["thread_id"] == "same-thread"
    assert state["result"] == "resumed"
