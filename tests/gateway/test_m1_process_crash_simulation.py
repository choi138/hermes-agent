"""Real HTTP provider faults and killed interpreters against isolated durable state.

The remote provider and messaging transport are local stand-ins. Gateway dispatch,
AIAgent, the OpenAI HTTP client, transcript DB and recovery policy are real.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Real HTTP faults, cold imports and state writes share the loaded host.
# Keep progress bounded without asserting interpreter or filesystem speed.
_CHILD_PROGRESS_TIMEOUT = 60
_CHILD_PROCESS_TIMEOUT = 180


async def _child(root: Path, scenario: str, stage: str):
    import logging
    logging.basicConfig(level=logging.WARNING)
    from openai import OpenAI
    from tests.gateway.restart_test_helpers import make_restart_source
    from tests.gateway.test_retryable_turn_recovery import (
        _m1_replan_runner, _m1_replan_event, _m1_replan_patch_agent_runtime,
        _m1_replan_await_session_task, _m1_replan_await_background_tasks,
    )

    state = {"action": "fail" if stage == "prepare" else "success", "requests": []}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            print(f"HTTP {self.path}: action={state['action']} stream={body.get('stream')}", flush=True)
            chat_request = self.path.endswith("/chat/completions")
            if chat_request:
                state["requests"].append(body)
                with (root / f"{stage}-requests.jsonl").open("a") as out:
                    out.write(json.dumps(body) + "\n")
            if chat_request and state["action"] == "hang":
                threading.Event().wait(45)
                return
            if not chat_request:
                code, response = 200, {"model_info": {"context_length": 131072}}
            elif state["action"] == "fail":
                code, response = 503, {"error": {"message": "injected provider outage", "type": "server_error"}}
            else:
                code, response = 200, {
                    "id": "local-recovered", "object": "chat.completion", "created": 1,
                    "model": "test-model", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "SIMULATION RECOVERED"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            payload = json.dumps(response).encode()
            streaming = chat_request and code == 200 and body.get("stream")
            if streaming:
                chunk = {"id": "local-recovered", "object": "chat.completion.chunk",
                    "created": 1, "model": "test-model", "choices": [{"index": 0,
                    "delta": {"role": "assistant", "content": "SIMULATION RECOVERED"},
                    "finish_reason": None}]}
                final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                payload = (f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    client = OpenAI(api_key="local-test", base_url=url, max_retries=0, timeout=40)
    patch = pytest.MonkeyPatch()
    _m1_replan_patch_agent_runtime(patch, client)
    patch.setattr("agent.process_bootstrap.OpenAI", lambda **_kwargs: OpenAI(
        api_key="local-test", base_url=url, max_retries=0, timeout=40,
    ))
    patch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "api_key": "local-test", "base_url": url,
        "provider": "openai-compat", "api_mode": "chat_completions",
    })
    runner, adapter, _ = _m1_replan_runner(root / "runtime")
    source = make_restart_source(chat_id="crash-simulation")
    key = runner._session_key_for_source(source)

    if stage == "prepare":
        await adapter.handle_message(_m1_replan_event("inspect original request", source, "original"))
        await _m1_replan_await_session_task(adapter, key, timeout=_CHILD_PROGRESS_TIMEOUT)
        entry = runner.session_store._entries[key]
        assert entry.active_turn is not None, {"requests": len(state["requests"]), "sent": adapter.sent}
        assert entry.active_turn["status"] == "retry_wait", entry.active_turn
        initial_count = len(state["requests"])
        assert initial_count > 0
        # Cancel only the waiting timer, retaining the durable failed-turn proof.
        timers = list(getattr(runner, "_retryable_turn_wakeups", {}).values())
        for task in timers:
            task.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        old = dict(entry.active_turn)
        assert runner.session_store.mark_active_turn_recovery(
            key, old["turn_id"], expected_resume_count=old["resume_count"],
            status="retry_wait", failure_reason="provider_unavailable", retry_delay=0,
            expected_identity=old,
        )
        old = dict(entry.active_turn)
        if scenario == "queued":
            assert runner.session_store.claim_resume_active_turn(
                key, old["turn_id"], runner._boot_id, old["resume_count"] + 1,
                expected_session_id=entry.session_id, expected_turn_id=old["turn_id"],
                expected_resume_count=old["resume_count"], expected_status=old["status"],
                expected_identity=old,
            )
        elif scenario == "executing":
            state["action"] = "hang"
            assert runner._schedule_resume_pending_sessions() == 1
            async with asyncio.timeout(_CHILD_PROGRESS_TIMEOUT):
                while len(state["requests"]) == initial_count:
                    await asyncio.sleep(0.01)
            assert entry.active_turn["dispatch_state"] == "executing"
        elif scenario == "stop_index_failure":
            def cannot_save(*_args, **_kwargs):
                raise OSError("injected session-index write failure")
            patch.setattr(runner.session_store, "_save_entry", cannot_save)
            await adapter.handle_message(_m1_replan_event("/stop", source, "stop"))
            await _m1_replan_await_session_task(adapter, key, timeout=_CHILD_PROGRESS_TIMEOUT)
            tasks = [task for task in adapter._background_tasks if not task.done()]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=_CHILD_PROGRESS_TIMEOUT)
            assert runner.session_store.recovery_is_quarantined(key, old), {
                "record": entry.active_turn, "sent": adapter.sent,
            }

        (root / "ready.json").write_text(json.dumps({
            "pid": os.getpid(), "record": entry.active_turn,
            "requests": len(state["requests"]), "initial_requests": initial_count,
            "sent": adapter.sent,
        }))
        # Parent kills this exact interpreter. No cleanup or graceful shutdown runs.
        await asyncio.Event().wait()
    else:
        scheduled = runner._schedule_resume_pending_sessions()
        await _m1_replan_await_background_tasks(runner, timeout=_CHILD_PROGRESS_TIMEOUT)
        entry = runner.session_store._entries[key]
        transcript = runner.session_store.load_transcript(entry.session_id, repair_alternation=False)
        receipt = {
            "scheduled": scheduled, "requests": len(state["requests"]),
            "sent": adapter.sent, "record": entry.active_turn,
            "original_inputs": sum(row.get("role") == "user" and
                row.get("content") == "inspect original request" for row in transcript),
            "quarantined": bool(entry.active_turn) and
                runner.session_store.recovery_is_quarantined(key, entry.active_turn),
        }
        (root / f"{stage}-receipt.json").write_text(json.dumps(receipt))
        client.close()
        server.shutdown()
        server.server_close()
        patch.undo()


@pytest.mark.parametrize("scenario", ["retry_wait", "queued", "executing", "stop_index_failure"])
def test_http_outage_kill_and_restart(tmp_path, scenario, record_property):
    root = tmp_path / scenario
    hermes_home = root / "isolated-hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text("approvals:\n  destructive_slash_confirm: false\n")
    env = os.environ.copy()
    env.update(HERMES_HOME=str(hermes_home), HOME=str(root), TMPDIR=str(root))
    repo = Path(__file__).resolve().parents[2]
    # -m preserves repo import roots and uses this test's child entry point.
    argv = [sys.executable, "-m", "tests.gateway.test_m1_process_crash_simulation", str(root), scenario]
    with (root / "prepare.log").open("w") as log:
        process = subprocess.Popen(argv + ["prepare"], cwd=repo, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + _CHILD_PROCESS_TIMEOUT
            while not (root / "ready.json").exists():
                assert process.poll() is None, (root / "prepare.log").read_text()
                assert time.monotonic() < deadline, (root / "prepare.log").read_text()
                time.sleep(0.02)
            ready = json.loads((root / "ready.json").read_text())
            record_property("crash_boundary", json.dumps(ready))
            assert ready["pid"] == process.pid
            process.kill()
            process.wait(timeout=5)
            assert process.returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    for stage in ("restart", "restart_again"):
        with (root / f"{stage}.log").open("w") as log:
            completed = subprocess.run(argv + [stage], cwd=repo, env=env,
                stdout=log, stderr=log, timeout=_CHILD_PROCESS_TIMEOUT)
        assert completed.returncode == 0, (root / f"{stage}.log").read_text()
        receipt = json.loads((root / f"{stage}-receipt.json").read_text())
        record_property(stage, json.dumps(receipt))
        expected = int(stage == "restart" and scenario in {"retry_wait", "queued"})
        assert receipt["scheduled"] == expected, receipt
        assert receipt["requests"] == expected, receipt
        assert receipt["sent"].count("SIMULATION RECOVERED") == expected, receipt
        assert receipt["original_inputs"] == 1, receipt
        if scenario in {"retry_wait", "queued"}:
            assert receipt["record"] is None, receipt
        elif scenario == "executing":
            assert receipt["record"]["status"] == "blocked", receipt
            assert receipt["record"]["blocked_reason"] == "unsealed_attempt", receipt
        else:
            assert receipt["quarantined"], receipt
        print(f"SIMULATION {scenario} {stage}: {json.dumps(receipt)}")


if __name__ == "__main__":
    asyncio.run(_child(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
