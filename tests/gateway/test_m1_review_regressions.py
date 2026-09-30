"""Behavioral regressions discovered by the independent M1 review."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from copy import deepcopy
import pytest
from tests.gateway.test_retryable_turn_recovery import (
    _m1_replan_runner, _m1_replan_home, _seed_retry_wait, _store,
)
from tests.gateway.restart_test_helpers import make_restart_source


@pytest.mark.parametrize("missing", ["call_id", "result"])
def test_readonly_recovery_requires_complete_call_result_proof(missing):
    from gateway.turn_recovery import failed_turn_recovery
    call = {"id": "read-1", "function": {"name": "read_file", "arguments": "{}"}}
    rows = [{"role": "user", "content": "inspect"}, {"role": "assistant", "tool_calls": [call]}]
    if missing == "call_id":
        call.pop("id")
    # Both live and durable proof agree on the same incomplete call.
    decision = failed_turn_recovery({"failed": True, "failure_retryable": True,
        "failure_reason": "timeout", "messages": rows, "current_turn_user_idx": 0},
        evidence={"rows": rows})
    assert decision["status"] == "blocked"
    assert decision["blocked_reason"] == ("missing_tool_call_id" if missing == "call_id" else "missing_tool_result")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["lookup", "cancellation"])
async def test_stop_cannot_cancel_successor_during_store_await(
    tmp_path, monkeypatch, _m1_replan_home, boundary,
):
    from unittest.mock import MagicMock
    runner, _, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="stop-successor")
    key, _ = _seed_retry_wait(runner.session_store, source)
    entry = runner.session_store._entries[key]
    old_agent, new_agent = MagicMock(), MagicMock()
    runner._session_state(key).turn.agent = old_agent
    runner._begin_session_run_generation(key)
    interrupted = []
    monkeypatch.setattr("gateway.run.request_hard_interrupt", lambda agent, *a, **kw: interrupted.append(agent))

    def replace_turn():
        runner.session_store.begin_active_turn(key, "successor", "boot",
            origin_session_id=entry.session_id, origin_owner="successor-owner")
        runner._session_state(key).turn.agent = new_agent
        runner._begin_session_run_generation(key)

    method = "lookup_by_session_key" if boundary == "lookup" else "cancel_active_turn_recovery"
    original = getattr(runner.async_session_store, method)

    async def replace_before_store_call(*args, **kwargs):
        replace_turn()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runner.async_session_store, method, replace_before_store_call)
    await runner._interrupt_and_clear_session(key, source,
        interrupt_reason="user_stop", invalidation_reason="stop_command")
    assert entry.active_turn["turn_id"] == "successor"
    assert entry.active_turn["status"] == "running"
    assert runner._session_state(key).turn.agent is new_agent
    assert interrupted == []

@pytest.mark.parametrize("settled", ["retry_wait", "blocked", "queued"])
def test_shutdown_preserves_settled_recovery(tmp_path, settled):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="shutdown-race")
    key, turn = _seed_retry_wait(store, source)
    entry = store._entries[key]
    if settled == "blocked":
        assert store.cancel_active_turn_recovery(key, expected_session_id=entry.session_id, reason="stop")
    elif settled == "queued":
        prior = deepcopy(entry.active_turn)
        assert store.claim_resume_active_turn(key, turn, "old-boot", 1,
            expected_session_id=entry.session_id, expected_turn_id=turn,
            expected_resume_count=0, expected_status="retry_wait", expected_identity=prior)
    before = deepcopy(entry.active_turn)
    assert not store.mark_active_turn_interrupted(key, "shutdown_timeout")
    assert _store(tmp_path).get_or_create_session(source).active_turn == before

@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [True, False])
@pytest.mark.parametrize("count", [2, 3])
async def test_retry_cap_only_charges_executed_attempts(tmp_path, _m1_replan_home, queued, count):
    runner, _, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="cap-ticket")
    key, turn = _seed_retry_wait(runner.session_store, source, delay=0)
    entry = runner.session_store._entries[key]
    prior = deepcopy(entry.active_turn)
    assert runner.session_store.claim_resume_active_turn(key, turn, "dead", count,
        expected_session_id=entry.session_id, expected_turn_id=turn,
        expected_resume_count=0, expected_status="retry_wait", expected_identity=prior)
    if not queued:
        assert runner.session_store.consume_resume_dispatch(key, dict(entry.active_turn))
    expected = queued and count == 2
    assert runner._schedule_resume_pending_sessions() == int(expected)
    if expected:
        assert entry.active_turn["resume_count"] == count
    else:
        assert entry.active_turn["blocked_reason"] == "retry_cap"
    tasks = list(runner._background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

@pytest.mark.parametrize("queued", [False, True])
def test_failed_cancel_persistence_fences_restart(tmp_path, monkeypatch, queued):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="cancel-restart")
    key, turn = _seed_retry_wait(store, source, delay=0)
    entry = store._entries[key]
    if queued:
        assert store.claim_resume_active_turn(key, turn, "dead", 1,
            expected_session_id=entry.session_id, expected_turn_id=turn,
            expected_resume_count=0, expected_status="retry_wait", expected_identity=dict(entry.active_turn))
    before = deepcopy(entry.active_turn)
    def fail(*args, **kwargs):
        raise OSError("injected index-write failure")
    monkeypatch.setattr(store, "_save_entry", fail)
    with pytest.raises(OSError):
        store.cancel_active_turn_recovery(key, expected_session_id=entry.session_id, reason="stop")
    fresh = _store(tmp_path)
    assert fresh.recovery_is_quarantined(key, fresh.get_or_create_session(source).active_turn)
    # A fresh interpreter has no access to the old store's in-memory quarantine.
    script = """
import json, sys
from pathlib import Path
from gateway.config import GatewayConfig
from gateway.session import SessionStore
store = SessionStore(sessions_dir=Path(sys.argv[1]), config=GatewayConfig())
store._ensure_loaded()
record = store._entries[sys.argv[2]].active_turn
print(json.dumps(store.recovery_is_quarantined(sys.argv[2], record)))
"""
    child = subprocess.run([sys.executable, "-c", script, str(store.sessions_dir), key],
                           cwd=Path(__file__).resolve().parents[2], capture_output=True,
                           text=True, check=True, timeout=20)
    assert json.loads(child.stdout.strip().splitlines()[-1]) is True
    assert not fresh.claim_resume_active_turn(key, turn, "next", before["resume_count"] + 1,
        expected_session_id=entry.session_id, expected_turn_id=turn,
        expected_resume_count=before["resume_count"], expected_status=before["status"], expected_identity=before)
    assert fresh.begin_active_turn(key, "fresh-user", "next", origin_session_id=entry.session_id, origin_owner="fresh-owner")
    assert not fresh.recovery_is_quarantined(key, fresh._entries[key].active_turn)


@pytest.mark.asyncio
async def test_authorization_is_rechecked_at_worker_dispatch(tmp_path, monkeypatch, _m1_replan_home):
    from unittest.mock import MagicMock
    from gateway.run_turn_runner import TurnRunner
    from tests.gateway.test_retryable_turn_recovery import (
        _m1_pi_claim_event, _m1_replan_patch_agent_runtime, _provider_response,
        _m1_replan_await_background_tasks,
    )
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="auth-dispatch")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response("MUST NOT RUN")
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    prepare = TurnRunner._prepare_turn_message
    def revoke_after_prepare(self, history):
        result = prepare(self, history)
        runner._is_user_authorized_for_source = lambda *args, **kwargs: False
        return result
    monkeypatch.setattr(TurnRunner, "_prepare_turn_message", revoke_after_prepare)
    await adapter.handle_message(event)
    await _m1_replan_await_background_tasks(runner)
    assert client.chat.completions.create.call_count == 0
    assert "MUST NOT RUN" not in adapter.sent
