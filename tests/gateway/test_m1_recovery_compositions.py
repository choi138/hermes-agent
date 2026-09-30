"""M1 closure compositions across timer, queue, compaction, restart, and CAS boundaries."""

from __future__ import annotations

import asyncio
from copy import copy, deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from gateway.platforms.base import merge_pending_message_event
from gateway.session import AsyncSessionStore
from gateway.turn_recovery import BLOCKED, RETRY_WAIT
from tests.gateway.restart_test_helpers import make_restart_source
from tests.gateway.test_retryable_turn_recovery import (
    RECOVERED_RESPONSE,
    _RecoveryTransport,
    _m1_pi_claim_event,
    _m1_replan_await_background_tasks,
    _m1_replan_await_session_task,
    _m1_replan_event,
    _m1_replan_patch_agent_runtime,
    _m1_replan_runner,
    _provider_response,
    _seed_retry_wait,
    _store,
    _wait_until,
)


@pytest.fixture
def m1_composition_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    # Keep test state outside the platform-default HOME/.hermes root.
    # The production-state guard must remain enabled, including in child processes.
    hermes_home = home / "isolated-hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "approvals:\n  destructive_slash_confirm: false\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", hermes_home / "state.db")
    return hermes_home


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_store", [False, True], ids=["same_boot", "restart"])
async def test_provider_failure_timer_stop_is_sticky(
    tmp_path, monkeypatch, m1_composition_home, fresh_store,
):
    """A real failed turn arms a timer; actual /stop wins before dispatch and survives reload."""
    source = make_restart_source(chat_id=f"timer-stop-{fresh_store}")
    state = {"healthy": False, "requests": [], "healthy_requests": 0}
    client = MagicMock()

    def complete(**kwargs):
        state["requests"].append(kwargs)
        if not state["healthy"]:
            raise httpx.ConnectError(
                "provider unavailable", request=httpx.Request("POST", "https://mock.invalid"),
            )
        state["healthy_requests"] += 1
        return _provider_response("RETRY MUST NOT RUN")

    client.chat.completions.create.side_effect = complete
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    timer_waiting = asyncio.Event()
    release_timer = asyncio.Event()
    real_sleep = asyncio.sleep

    async def observe_retry_wait(delay, *args, **kwargs):
        wakeups = getattr(runner, "_retryable_turn_wakeups", {})
        if asyncio.current_task() in wakeups.values():
            timer_waiting.set()
            # Release the real wakeup task after /stop, then exercise its
            # post-sleep identity/cancellation gate without wall-clock waiting.
            return await asyncio.wait_for(release_timer.wait(), timeout=10)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr("gateway.run_startup.asyncio.sleep", observe_retry_wait)
    runtime = tmp_path / "runtime"
    runner, adapter, _ = _m1_replan_runner(runtime, adapter=_RecoveryTransport(state))
    key = runner._session_key_for_source(source)
    await adapter.handle_message(_m1_replan_event("read the original request", source, "original"))
    await _m1_replan_await_session_task(adapter, key)
    record = runner.session_store._entries[key].active_turn
    assert record is not None and record["status"] == RETRY_WAIT
    await asyncio.wait_for(timer_waiting.wait(), timeout=5)
    assert all(not task.done() for task in runner._retryable_turn_wakeups.values())
    calls_before_stop = len(state["requests"])

    if fresh_store:
        timers = list(runner._retryable_turn_wakeups.values())
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        timer_waiting.clear()
        runner, adapter, _ = _m1_replan_runner(runtime, adapter=_RecoveryTransport(state))
        assert runner._schedule_resume_pending_sessions() == 0
        await asyncio.wait_for(timer_waiting.wait(), timeout=5)
        assert all(not task.done() for task in runner._retryable_turn_wakeups.values())

    await adapter.handle_message(_m1_replan_event("/stop", source, "stop-before-timer"))
    await _m1_replan_await_session_task(adapter, key)
    assert runner.session_store._entries[key].active_turn["blocked_reason"] == "user_cancelled"
    release_timer.set()
    await _wait_until(lambda: not getattr(runner, "_retryable_turn_wakeups", {}))
    cancelled = runner.session_store._entries[key].active_turn
    assert cancelled["status"] == BLOCKED
    assert cancelled["blocked_reason"] == "user_cancelled"
    assert len(state["requests"]) == calls_before_stop
    assert state["healthy_requests"] == 0

    restarted, _, _ = _m1_replan_runner(runtime, adapter=_RecoveryTransport(state))
    assert restarted._schedule_resume_pending_sessions() == 0
    assert not getattr(restarted, "_retryable_turn_wakeups", {})
    assert restarted.session_store._entries[key].active_turn["blocked_reason"] == "user_cancelled"


@pytest.mark.asyncio
async def test_mixed_recovery_human_command_followers_drain_once(
    tmp_path, monkeypatch, m1_composition_home,
):
    """Actual /stop and adapter drains keep chronology while stale recovery copies stay silent."""
    runtime = tmp_path / "runtime"
    runner, adapter, _ = _m1_replan_runner(runtime)
    source = make_restart_source(chat_id="mixed-followers")
    key, recovery = _m1_pi_claim_event(runner.session_store, source)
    human = _m1_replan_event("human follower", source, "human")
    command = _m1_replan_event("/status", source, "status")
    duplicate = copy(recovery)
    duplicate.message_id = "recovery-copy"
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    for event in (recovery, human, command, duplicate):
        merge_pending_message_event(adapter._pending_messages, key, event, merge_text=True)

    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    await adapter.handle_message(_m1_replan_event("/stop", source, "stop"))
    await _m1_replan_await_background_tasks(runner)

    rows = runner.session_store.load_transcript(
        runner.session_store._entries[key].session_id, repair_alternation=False,
    )
    users = [row.get("content") for row in rows if row.get("role") == "user"]
    assert users.count("human follower") == 1
    assert "/status" not in users
    assert client.chat.completions.create.call_count == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert runner.session_store._entries[key].active_turn is None
    assert key not in adapter._pending_messages
    assert key not in adapter._active_sessions


@pytest.mark.asyncio
async def test_adapter_shutdown_spools_mixed_followers_in_order(
    tmp_path, m1_composition_home,
):
    """The real adapter shutdown path preserves a recovery head and every distinct follower."""
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="shutdown-followers")
    key, recovery = _m1_pi_claim_event(runner.session_store, source)
    events = [
        recovery,
        _m1_replan_event("human follower", source, "human"),
        _m1_replan_event("/status", source, "status"),
        copy(recovery),
    ]
    events[-1].message_id = "recovery-copy"
    for event in events:
        merge_pending_message_event(adapter._pending_messages, key, event, merge_text=True)

    await adapter.cancel_background_tasks()

    files = sorted((m1_composition_home / "pending_messages").glob("pending-*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    assert len(payloads) == 4
    head = [payload for payload in payloads if "seq" not in payload]
    followers = sorted(
        (payload for payload in payloads if "seq" in payload), key=lambda payload: payload["seq"],
    )
    assert [payload["data"]["text"] for payload in head] == [""]
    assert [payload["data"]["text"] for payload in followers] == [
        "human follower", "/status", "",
    ]
    assert not adapter._pending_messages
    assert not adapter._active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_origin", [False, True], ids=["retained_owner", "lost_owner"])
async def test_compaction_readonly_steer_uses_raw_owner_evidence(
    tmp_path, monkeypatch, m1_composition_home, lose_origin,
):
    """Real in-place compaction resumes only while the original owned audit row survives."""
    from agent import conversation_compression as cc
    from agent.prompt_builder import steer_user_row
    from gateway.run_turn import GatewayTurnMixin
    from run_agent import AIAgent

    runtime = tmp_path / "runtime"
    store = _store(runtime)
    source = make_restart_source(chat_id=f"compaction-{lose_origin}")
    entry = store.get_or_create_session(source)
    key, sid = entry.session_key, entry.session_id
    turn_id = f"{sid}:{sid}:compaction"
    owner = f"owner:{turn_id}"
    assert store.begin_active_turn(
        key, turn_id, "boot", origin_session_id=sid, origin_owner=owner,
    )
    original_rows = [
        {"role": "user", "content": "inspect the original", "display_metadata": {"gateway_input_owner": owner}},
        {"role": "assistant", "tool_calls": [{
            "id": "read-1", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "read-1", "content": "read result", "effect_disposition": "none"},
        steer_user_row("continue carefully"),
    ]
    for row in original_rows:
        store.append_to_transcript(sid, row)
    db = store._db_for_session_id(sid)
    client = MagicMock()
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    agent = AIAgent(
        api_key="test-key", base_url="https://mock.invalid/v1", model="test-model",
        provider="openai-compat", api_mode="chat_completions", quiet_mode=True,
        session_db=db, session_id=sid, skip_context_files=True, skip_memory=True,
    )
    agent._persist_user_message_idx = len(original_rows)
    compressed = [
        {"role": "user", "content": "summary", "_compressed_summary": True},
        deepcopy(original_rows[-1]),
    ]
    outcome = cc._commit_compaction(
        agent, original_rows, compressed, in_place=True,
        lease=SimpleNamespace(holder=None, watermark=None, ttl=60),
        new_system_prompt="sys", system_message="sys", compressed_user_turn_outcome="none",
        messages_before_compression=deepcopy(original_rows), made_progress=True,
        attempt=cc._Attempt(snapshot={}, generation=0, started_at=time.monotonic()),
    )
    assert outcome.session_commit_succeeded and outcome.compacted_in_place
    audit = db.get_turn_recovery_rows(sid, owner)
    assert sum(
        (row.get("display_metadata") or {}).get("gateway_input_owner") == owner for row in audit
    ) == 1
    assert any(row.get("tool_call_id") == "read-1" for row in audit)
    assert any("continue carefully" in str(row.get("content")) for row in audit)
    if lose_origin:
        db._execute_write(lambda conn: conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND role = 'user' "
            "AND display_metadata IS NOT NULL",
            (sid,),
        ))

    event = SimpleNamespace(_gateway_active_turn_id=turn_id)
    from agent.turn_context import export_current_turn_boundary
    from agent.turn_context_compaction import _reanchor
    agent._current_turn_id = turn_id
    agent._current_turn_gateway_input_owner = owner
    _reanchor(agent, compressed, "inspect the original")
    result = export_current_turn_boundary(agent, {
        "failed": True, "failure_retryable": True, "failure_reason": "timeout",
        "messages": compressed,
    }, "inspect the original", gateway_input_owner=owner)
    await GatewayTurnMixin._hmwa_settle_retryable_turn(
        SimpleNamespace(async_session_store=AsyncSessionStore(store)), event=event,
        session_entry=entry, session_key=key, agent_result=result,
    )
    if lose_origin:
        assert event._gateway_turn_recovery["status"] == BLOCKED
        assert event._gateway_turn_recovery["blocked_reason"] == "missing_turn_boundary"
        return

    assert event._gateway_turn_recovery["status"] == RETRY_WAIT
    identity = event._gateway_recovery_identity
    assert store.mark_active_turn_recovery(
        key, turn_id, expected_resume_count=0, status=RETRY_WAIT,
        failure_reason="timeout", retry_delay=0, expected_identity=identity,
    )
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    restarted, adapter, _ = _m1_replan_runner(runtime)
    assert restarted._schedule_resume_pending_sessions() == 1
    await _m1_replan_await_background_tasks(restarted)
    assert client.chat.completions.create.call_count == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert restarted.session_store._entries[key].active_turn is None


@pytest.mark.parametrize("phase", ["queued", "executing_unsealed", "retry_wait_sealed"])
def test_subprocess_reconstructs_durable_recovery_phase(
    tmp_path, m1_composition_home, phase,
):
    """A fresh interpreter applies the restart policy; this is reconstruction, not fault injection."""
    runtime = tmp_path / phase
    runner, _, _ = _m1_replan_runner(runtime)
    source = make_restart_source(chat_id=f"subprocess-{phase}")
    if phase in {"queued", "executing_unsealed"}:
        key, event = _m1_pi_claim_event(runner.session_store, source)
        if phase == "executing_unsealed":
            assert runner.session_store.consume_resume_dispatch(key, event._hermes_turn_resume)
    else:
        key, _ = _seed_retry_wait(runner.session_store, source, boot_id="dead", delay=0)

    script = textwrap.dedent(
        """
        import asyncio, json, sys
        from pathlib import Path
        from unittest.mock import MagicMock
        import pytest
        from tests.gateway.test_retryable_turn_recovery import (
            _m1_replan_await_background_tasks, _m1_replan_runner,
            _m1_replan_patch_agent_runtime, _provider_response,
        )

        async def main():
            runner, adapter, _ = _m1_replan_runner(Path(sys.argv[1]))
            client = MagicMock()
            client.chat.completions.create.return_value = _provider_response("subprocess recovered")
            patch = pytest.MonkeyPatch()
            _m1_replan_patch_agent_runtime(patch, client)
            scheduled = runner._schedule_resume_pending_sessions()
            await _m1_replan_await_background_tasks(runner)
            entry = next(iter(runner.session_store._entries.values()))
            print(json.dumps({
                "scheduled": scheduled,
                "calls": client.chat.completions.create.call_count,
                "active": entry.active_turn,
                "sent": adapter.sent.count("subprocess recovered"),
            }))
        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    env["HOME"] = str(m1_composition_home.parent)
    env["HERMES_HOME"] = str(m1_composition_home)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(runtime)],
        cwd=Path(__file__).resolve().parents[2], env=env,
        text=True, capture_output=True, timeout=20, check=True,
    )
    receipt = json.loads(completed.stdout.strip().splitlines()[-1])
    expected = int(phase != "executing_unsealed")
    assert receipt["scheduled"] == expected, (receipt, completed.stderr)
    assert receipt["calls"] == expected
    assert receipt["sent"] == expected
    if phase == "executing_unsealed":
        assert receipt["active"]["status"] == BLOCKED
        assert receipt["active"]["blocked_reason"] == "unsealed_attempt"
    else:
        assert receipt["active"] is None


@pytest.mark.asyncio
async def test_late_recovery_notice_cannot_retire_successor(
    tmp_path, monkeypatch, m1_composition_home,
):
    """A late recovery CAS loses to a newer turn; the newer turn still retires normally."""
    runner, _, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="late-successor")
    key, old_event = _m1_pi_claim_event(runner.session_store, source, phase="executing")
    entry = runner.session_store._entries[key]
    old_identity = dict(entry.active_turn)
    old_event._gateway_active_turn_session_key = key
    old_event._gateway_active_turn_id = old_identity["turn_id"]
    old_event._gateway_turn_recovery = {
        "status": RETRY_WAIT, "failure_reason": "late_provider_failure",
    }
    old_event._gateway_recovery_identity = old_identity
    entered, release = asyncio.Event(), asyncio.Event()
    real_mark = runner.async_session_store.mark_active_turn_recovery

    async def mark_after_barrier(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)
        return await real_mark(*args, **kwargs)

    monkeypatch.setattr(runner.async_session_store, "mark_active_turn_recovery", mark_after_barrier)
    late = asyncio.create_task(runner._clear_durable_active_turn(old_event))
    await asyncio.wait_for(entered.wait(), timeout=5)
    successor_id = f"{entry.session_id}:{entry.session_id}:successor"
    assert runner.session_store.begin_active_turn(
        key, successor_id, "new-boot", origin_session_id=entry.session_id,
        origin_owner="successor-owner",
    )
    successor = dict(entry.active_turn)
    release.set()
    await asyncio.wait_for(late, timeout=5)

    assert entry.active_turn == successor
    assert entry.active_turn["turn_id"] == successor_id
    assert not getattr(runner, "_retryable_turn_wakeups", {})

    successor_event = SimpleNamespace(
        _gateway_active_turn_session_key=key,
        _gateway_active_turn_id=successor_id,
    )
    await runner._clear_durable_active_turn(successor_event)
    assert entry.active_turn is None
