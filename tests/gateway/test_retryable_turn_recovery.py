"""Retryable gateway failures retain and safely resume the same durable turn."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import dotenv


dotenv.load_dotenv = lambda *_args, **_kwargs: False

import httpx
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.turn_recovery import BLOCKED, RETRY_WAIT, failed_turn_recovery, retry_delay_seconds
from tests.gateway.restart_test_helpers import RestartTestAdapter, make_restart_runner, make_restart_source


RECOVERED_RESPONSE = "Recovered without another user message."


def _skip_agent_retry_sleep(monkeypatch):
    # Patch the runtime's binding, not the shared time module: terminal cleanup
    # must retain its real sleep to avoid a busy loop starving file tools.
    monkeypatch.setattr(
        "agent.agent_runtime_helpers.time",
        SimpleNamespace(sleep=lambda _seconds: None, time=time.time, monotonic=time.monotonic),
    )


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    connect = socket.socket.connect

    def guarded_connect(sock, address):
        if isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return connect(sock, address)
            except ValueError:
                pass
        raise AssertionError("network access is disabled in retryable-turn recovery tests")

    # Windows builds asyncio's self-pipe with a loopback TCP socketpair.
    # Keep that local plumbing available and restore the guard after each test.
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def test_network_guard_permits_asyncio_loopback_and_blocks_external():
    with socket.socket() as listener, socket.socket() as client:
        listener.settimeout(1)
        client.settimeout(1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        peer, _ = listener.accept()
        peer.close()
    with socket.socket() as external:
        with pytest.raises(AssertionError, match="network access is disabled"):
            external.connect(("192.0.2.1", 443))


def _provider_response(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


class _RecoveryTransport(BasePlatformAdapter):
    """A successful transport that makes the provider healthy after the failure notice lands."""

    def __init__(self, provider_state):
        super().__init__(
            PlatformConfig(enabled=True, token="test-token"), Platform.TELEGRAM
        )
        self.provider_state = provider_state
        self.sent: list[str] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(content)
        if "Wait a minute and send /retry" in content:
            self.provider_state["notices"] = self.provider_state.get("notices", 0) + 1
            self.provider_state["healthy"] = True
        return SendResult(success=True, message_id=f"m-{len(self.sent)}")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["same_boot", "restart_during_retry", "read_then_fail", "permanent_failure"])
async def test_full_handler_retries_structured_transient_failure_after_notice_delivery(
    tmp_path, monkeypatch, scenario,
):
    """The adapter delivers the real agent failure, then the same durable turn resumes once.

    This drives BasePlatformAdapter -> GatewayRunner -> real AIAgent.run_conversation. Only the
    provider client and platform transport are mocked. The provider stays unavailable throughout
    the first agent turn (including its internal retry budget) and becomes healthy only after the
    transport acknowledges the user-visible failure notice.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    provider_state = {"healthy": False, "requests": [], "healthy_requests": 0}
    client = MagicMock()

    def _complete(**kwargs):
        provider_state["requests"].append(kwargs)
        if scenario == "permanent_failure":
            from openai import AuthenticationError
            request = httpx.Request("POST", "https://mock-provider.invalid/v1/chat/completions")
            raise AuthenticationError("invalid mock key", response=httpx.Response(401, request=request), body=None)
        if not provider_state["healthy"]:
            request = httpx.Request("POST", "https://mock-provider.invalid/v1/chat/completions")
            raise httpx.ConnectError("temporary provider outage", request=request)
        if scenario == "read_then_fail" and provider_state.get("notices") == 1:
            if not provider_state.get("tool_requested"):
                provider_state["tool_requested"] = True
                response = _provider_response(None)
                response.choices[0].finish_reason = "tool_calls"
                response.choices[0].message.tool_calls = [SimpleNamespace(
                    id="read-1", type="function", function=SimpleNamespace(
                        name="read_file", arguments=json.dumps({"path": str(read_path)}),
                    ),
                )]
                return response
            request = httpx.Request("POST", "https://mock-provider.invalid/v1/chat/completions")
            raise httpx.ConnectError("second transient outage", request=request)
        provider_state["healthy_requests"] += 1
        return _provider_response(RECOVERED_RESPONSE)

    read_path = tmp_path / "read.txt"
    read_path.write_text("read-only evidence")
    client.chat.completions.create.side_effect = _complete
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **_kwargs: client)
    from tests.agent.test_run_agent import _make_tool_defs
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *args, **kwargs: _make_tool_defs("read_file"))
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *args, **kwargs: {})
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *args, **kwargs: 0.0)
    _skip_agent_retry_sleep(monkeypatch)
    monkeypatch.setattr("agent.turn_recovery_autorecover.auto_recovery_cycles", lambda _agent: 0)
    monkeypatch.setattr("gateway.turn_recovery.retry_delay_seconds", lambda _count: 60.0 if scenario == "restart_during_retry" else 1.0)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *_args, **_kwargs: "test-model")
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://mock-provider.invalid/v1",
            "provider": "openai-compat",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 4)

    config = GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-token")},
    )
    runner = GatewayRunner(config)
    runner._running = True
    runner._is_user_authorized_for_source = lambda _source, **_kwargs: True
    transport = _RecoveryTransport(provider_state)
    transport.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = transport
    runner._wire_adapter_handlers(transport)
    agent_results = []
    run_agent = runner._run_agent

    async def capture_result(*args, **kwargs):
        result = await run_agent(*args, **kwargs)
        agent_results.append(result)
        return result

    runner._run_agent = capture_result

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="recovery-chat",
        chat_type="dm",
        user_id="owner",
    )
    event = MessageEvent(
        text="finish the original task",
        message_type=MessageType.TEXT,
        source=source,
        message_id="inbound-1",
    )
    await transport.handle_message(event)

    session_key = runner._session_key_for_source(source)
    first_task = transport._session_tasks[session_key]
    await first_task

    entry_after_notice = runner.session_store.get_or_create_session(source)
    if scenario == "permanent_failure":
        from gateway.delivery_ledger import has_turn_obligation
        assert entry_after_notice.active_turn is None
        assert event._gateway_response_kind == "terminal_failure"
        assert has_turn_obligation(session_key, event._gateway_active_turn_id)
        assert runner._schedule_resume_pending_sessions() == 0
        assert provider_state["healthy_requests"] == 0
        assert transport.sent
        return
    assert entry_after_notice.active_turn is not None, {
        name: getattr(event, name, None)
        for name in (
            "_gateway_turn_recovery",
            "_gateway_active_turn_recovery_kept",
            "_gateway_active_turn_expected_resume_count",
            "_gateway_active_turn_id",
            "_gateway_turn_result_interrupted",
        )
    }
    assert entry_after_notice.active_turn["status"] == "retry_wait"
    original_turn_id = entry_after_notice.active_turn["turn_id"]
    assert transport.sent and transport.sent[0] != RECOVERED_RESPONSE

    if scenario == "restart_during_retry":
        for task in runner._retryable_turn_wakeups.values():
            task.cancel()
        await asyncio.gather(*runner._retryable_turn_wakeups.values(), return_exceptions=True)
        # Simulate process loss at the scheduler's durable claim / dispatch boundary.
        # The real scheduler claims the retry; the dead boot never invokes the provider.
        assert runner.session_store.mark_active_turn_recovery(
            session_key, original_turn_id, expected_resume_count=0,
            status=RETRY_WAIT, failure_reason="timeout", retry_delay=0,
        )
        dispatched = AsyncMock()
        runner._run_startup_resume_event = dispatched
        assert runner._schedule_resume_pending_sessions() == 1
        await asyncio.gather(*list(runner._background_tasks))
        dispatched.assert_awaited_once()
        assert runner.session_store.get_or_create_session(source).active_turn["status"] == "resuming"
        runner = GatewayRunner(config)
        runner._running = True
        runner._is_user_authorized_for_source = lambda _source, **_kwargs: True
        transport.gateway_runner = runner
        runner.adapters[Platform.TELEGRAM] = transport
        runner._wire_adapter_handlers(transport)
        assert await runner._reconcile_answered_turns() == 0
        assert runner._schedule_resume_pending_sessions() == 1

    await _wait_until(lambda: RECOVERED_RESPONSE in transport.sent, timeout=10.0)
    await _wait_until(lambda: session_key not in transport._session_tasks)

    final_entry = runner.session_store.get_or_create_session(source)
    assert final_entry.active_turn is None
    assert transport.sent.count(RECOVERED_RESPONSE) == 1
    assert provider_state["healthy_requests"] == 1
    # Reconstruct the crash window after the final is ledgered but before retirement.
    # The actual final response must prevent any new provider call, including after reload.
    requests_before_restart = len(provider_state["requests"])
    assert runner.session_store.begin_active_turn(session_key, original_turn_id, "dead-final-boot", resume_count=2)
    runner = GatewayRunner(config)
    runner._running = True
    runner._is_user_authorized_for_source = lambda _source, **_kwargs: True
    transport.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = transport
    runner._wire_adapter_handlers(transport)
    assert await runner._reconcile_answered_turns() == 1
    assert runner._schedule_resume_pending_sessions() == 0
    assert len(provider_state["requests"]) == requests_before_restart
    assert transport.sent.count(RECOVERED_RESPONSE) == 1
    if scenario == "read_then_fail":
        assert provider_state["notices"] == 2
        failed_retry = agent_results[1]
        assert failed_retry["failed"] is True
        assert failed_retry["history_offset"] > failed_retry["current_turn_user_idx"]
        assert failed_turn_recovery(failed_retry)["status"] == RETRY_WAIT
        transcript = runner.session_store.load_transcript(final_entry.session_id)
        assert sum(row.get("role") == "user" for row in transcript) == 1
        assert any(row.get("tool_calls") for row in transcript)
        assert any(row.get("tool_call_id") == "read-1" for row in transcript)
        assert any(
            row.get("role") == "tool" and row.get("tool_call_id") == "read-1"
            and "read-only evidence" in str(row.get("content"))
            for request in provider_state["requests"] for row in request["messages"]
        )

    request_messages = [
        request["messages"] for request in provider_state["requests"]
        if isinstance(request.get("messages"), list)
    ]
    assert request_messages
    assert all(
        any(
            message.get("role") == "user"
            and "finish the original task" in str(message.get("content"))
            for message in messages
        )
        for messages in request_messages
    )


@pytest.mark.parametrize(
    "result",
    [
        {"failed": True, "interrupted": False, "failure_reason": "auth", "failure_retryable": False},
        {
            "failed": True,
            "interrupted": False,
            "failure_reason": "context_overflow",
            "failure_retryable": False,
        },
        {
            "failed": True,
            "interrupted": False,
            "failure_reason": "model_not_found",
            "failure_retryable": False,
        },
        {"failed": False, "interrupted": True, "failure_reason": "timeout", "failure_retryable": True},
        {"failed": True, "interrupted": False, "failure_reason": "timeout"},
    ],
)
def test_only_explicit_retryable_failures_enter_gateway_recovery(result):
    result["messages"] = []

    assert failed_turn_recovery(result) is None


def test_unknown_external_effect_blocks_automatic_replay():
    result = {
        "failed": True,
        "interrupted": False,
        "failure_reason": "timeout",
        "failure_retryable": True,
        "history_offset": 0,
        "messages": [
            {"role": "user", "content": "update the remote record"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "mcp_unknown", "arguments": "{}"}},
                ],
            },
        ],
    }

    decision = failed_turn_recovery(result)

    assert decision == {
        "status": BLOCKED,
        "failure_reason": "timeout",
        "blocked_reason": "external_effect_unknown",
    }


def test_missing_replay_history_blocks_automatic_replay():
    decision = failed_turn_recovery({
        "failed": True,
        "interrupted": False,
        "failure_reason": "timeout",
        "failure_retryable": True,
    })

    assert decision == {
        "status": BLOCKED,
        "failure_reason": "timeout",
        "blocked_reason": "missing_replay_history",
    }


def test_read_only_turn_can_retry_and_backoff_is_bounded():
    result = {
        "failed": True,
        "interrupted": False,
        "failure_reason": "server_error",
        "failure_retryable": True,
        "history_offset": 0,
        "messages": [
            {"role": "user", "content": "inspect the file"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "file contents", "effect_disposition": "none"},
        ],
    }

    assert failed_turn_recovery(result) == {
        "status": RETRY_WAIT,
        "failure_reason": "server_error",
    }
    delays = [retry_delay_seconds(count) for count in range(7)]
    assert delays == sorted(delays)
    assert delays[:5] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert delays[-2:] == [30.0, 30.0]


def _store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    return store


def _seal_seeded_attempt(store, session_key, turn_id, *, resume_count=0):
    entry = store._entries[session_key]
    evidence = store.load_turn_recovery_evidence(entry.active_turn)
    checkpoint = {**evidence["checkpoint"], "resume_count": resume_count}
    assert store.seal_active_turn_evidence(
        session_key,
        turn_id,
        expected_resume_count=resume_count,
        origin_row_id=evidence["origin_row_id"],
        checkpoint=checkpoint,
    )


def _seed_retry_wait(
    store, source, *, boot_id="boot-1", resume_count=0, delay=0.0,
    seed_history=True, seal=True,
):
    entry = store.get_or_create_session(source)
    turn_id = f"{entry.session_id}:{entry.session_id}:retry"
    origin_owner = f"test-owner:{turn_id}"
    if seed_history:
        store.append_to_transcript(entry.session_id, {
            "role": "user",
            "content": "original task",
            "display_metadata": {"gateway_input_owner": origin_owner},
        })
    assert store.begin_active_turn(
        entry.session_key,
        turn_id,
        boot_id,
        resume_count=resume_count,
        origin_session_id=entry.session_id,
        origin_owner=origin_owner,
    )
    if seed_history and seal:
        _seal_seeded_attempt(
            store, entry.session_key, turn_id, resume_count=resume_count,
        )
    assert store.mark_active_turn_recovery(
        entry.session_key,
        turn_id,
        expected_resume_count=resume_count,
        status=RETRY_WAIT,
        failure_reason="timeout",
        retry_delay=delay,
    )
    return entry.session_key, turn_id


def _claim_race_runner(tmp_path, chat_id):
    runner, adapter = make_restart_runner()
    runner._boot_id = "claim-boot"
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id=chat_id)
    session_key, turn_id = _seed_retry_wait(
        runner.session_store, source, boot_id="prior-boot",
    )
    adapter.handle_message = AsyncMock()
    return runner, adapter, source, session_key, turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["reset", "suspend"])
async def test_resume_claim_rejects_route_replacement_or_suspension_at_authorization_boundary(
    tmp_path, replacement,
):
    runner, adapter, source, session_key, _turn_id = _claim_race_runner(
        tmp_path / replacement, f"claim-{replacement}",
    )
    original_session_id = runner.session_store._entries[session_key].session_id

    def authorize(_source, **_kwargs):
        if replacement == "reset":
            assert runner.session_store.reset_session(session_key) is not None
        else:
            assert runner.session_store.suspend_session(session_key)
        return True

    runner._is_user_authorized_for_source = authorize

    assert runner._schedule_resume_pending_sessions() == 0
    current = runner.session_store._entries[session_key]
    if replacement == "reset":
        assert current.session_id != original_session_id
        assert current.active_turn is None
    else:
        assert current.session_id == original_session_id
        assert current.suspended is True
        assert current.active_turn["status"] == RETRY_WAIT
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_resume_claim_rejects_replaced_turn_at_authorization_boundary(tmp_path):
    runner, adapter, _source, session_key, _turn_id = _claim_race_runner(
        tmp_path, "claim-replaced-turn",
    )
    replacement_turn_id = "replacement-turn"

    def authorize(_source, **_kwargs):
        assert runner.session_store.begin_active_turn(
            session_key, replacement_turn_id, "live-boot",
        )
        return True

    runner._is_user_authorized_for_source = authorize

    assert runner._schedule_resume_pending_sessions() == 0
    current = runner.session_store._entries[session_key].active_turn
    assert current["turn_id"] == replacement_turn_id
    assert current["status"] == "running"
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["resume_count", "status"])
async def test_resume_claim_rejects_attempt_state_change_at_authorization_boundary(
    tmp_path, changed_field,
):
    runner, adapter, _source, session_key, turn_id = _claim_race_runner(
        tmp_path / changed_field, f"claim-{changed_field}",
    )

    def authorize(_source, **_kwargs):
        if changed_field == "resume_count":
            assert runner.session_store.begin_active_turn(
                session_key, turn_id, "competing-boot", resume_count=2,
            )
        else:
            assert runner.session_store.cancel_active_turn_recovery(
                session_key, expected_session_id=runner.session_store._entries[session_key].session_id,
                reason="concurrent-stop",
            )
        return True

    runner._is_user_authorized_for_source = authorize

    assert runner._schedule_resume_pending_sessions() == 0
    current = runner.session_store._entries[session_key].active_turn
    assert current[changed_field] == (2 if changed_field == "resume_count" else "blocked")
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_resume_claim_allows_only_one_competing_claimer(tmp_path):
    runner, adapter, _source, session_key, turn_id = _claim_race_runner(
        tmp_path, "claim-duplicate",
    )
    entry = runner.session_store._entries[session_key]
    competing_claims = []

    def authorize(_source, **_kwargs):
        competing_claims.append(runner.session_store.claim_resume_active_turn(
            session_key,
            turn_id,
            runner._boot_id,
            resume_count=1,
            expected_session_id=entry.session_id,
            expected_turn_id=turn_id,
            expected_resume_count=0,
            expected_status=RETRY_WAIT,
        ))
        return True

    runner._is_user_authorized_for_source = authorize

    assert runner._schedule_resume_pending_sessions() == 0
    assert competing_claims == [True]
    current = runner.session_store._entries[session_key].active_turn
    assert current["turn_id"] == turn_id
    assert current["resume_count"] == 1
    assert current["status"] == "resuming"
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_resume_claim_requires_active_turn_to_remain_absent(tmp_path):
    runner, adapter = make_restart_runner()
    runner._boot_id = "claim-boot"
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="claim-legacy-replaced")
    entry = runner.session_store.get_or_create_session(source)
    assert runner.session_store.mark_resume_pending(entry.session_key)
    replacement_turn_id = "new-inbound-turn"
    adapter.handle_message = AsyncMock()

    def authorize(_source, **_kwargs):
        assert runner.session_store.begin_active_turn(
            entry.session_key, replacement_turn_id, runner._boot_id,
        )
        return True

    runner._is_user_authorized_for_source = authorize

    assert runner._schedule_resume_pending_sessions() == 0
    current = runner.session_store._entries[entry.session_key].active_turn
    assert current["turn_id"] == replacement_turn_id
    assert current["status"] == "running"
    adapter.handle_message.assert_not_called()


def test_old_attempt_cannot_overwrite_or_retire_new_attempt(tmp_path):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="stale-attempt")
    session_key, turn_id = _seed_retry_wait(store, source)
    assert store.begin_active_turn(session_key, turn_id, "boot-1", resume_count=1)

    assert not store.mark_active_turn_recovery(
        session_key,
        turn_id,
        expected_resume_count=0,
        status=RETRY_WAIT,
        failure_reason="timeout",
    )
    assert not store.finish_active_turn(session_key, turn_id, expected_resume_count=0)
    current = store.get_or_create_session(source).active_turn
    assert current is not None
    assert current["status"] == "resuming"
    assert current["resume_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "new", "reset"])
async def test_pending_wakeup_is_deduplicated_and_user_command_wins(tmp_path, command):
    runner, _adapter = make_restart_runner()
    runner.session_store = _store(tmp_path / command)
    source = make_restart_source(chat_id=f"cancel-{command}")
    session_key, turn_id = _seed_retry_wait(runner.session_store, source, delay=0.05)
    resume = MagicMock()
    runner._schedule_resume_pending_sessions = resume

    assert runner._schedule_retryable_turn_wakeup(session_key, turn_id) is True
    assert runner._schedule_retryable_turn_wakeup(session_key, turn_id) is False
    wakeups = list(runner._retryable_turn_wakeups.values())
    if command == "stop":
        assert runner.session_store.suspend_session(session_key)
    else:
        assert runner.session_store.reset_session(session_key) is not None

    await asyncio.wait_for(asyncio.gather(*wakeups), timeout=2)

    resume.assert_not_called()
    assert not runner._retryable_turn_wakeups


@pytest.mark.asyncio
async def test_retry_wait_survives_store_reload_and_resumes_on_new_boot(tmp_path):
    source = make_restart_source(chat_id="restart-retry")
    store = _store(tmp_path)
    session_key, turn_id = _seed_retry_wait(store, source, boot_id="old-boot")

    runner, adapter = make_restart_runner()
    runner._boot_id = "new-boot"
    runner.session_store = _store(tmp_path)
    runner._is_user_authorized_for_source = lambda _source, **_kwargs: True
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.gather(*list(runner._background_tasks))

    event = adapter.handle_message.await_args.args[0]
    current = runner.session_store._entries[session_key].active_turn
    assert event._hermes_turn_resume == {name: current[name] for name in (
        "recovery_version", "turn_id", "origin_session_id", "execution_session_id",
        "origin_owner", "boot_id", "dispatch_token", "resume_count",
    )}
    assert event._hermes_turn_resume["turn_id"] == turn_id
    assert event._hermes_turn_resume["resume_count"] == 1
    assert current is not None
    assert current["status"] == "resuming"
    assert current["boot_id"] == "new-boot"


def test_retry_cap_blocks_without_scheduling(tmp_path):
    source = make_restart_source(chat_id="retry-cap")
    runner, adapter = make_restart_runner()
    runner._boot_id = "boot-2"
    runner.session_store = _store(tmp_path)
    session_key, _turn_id = _seed_retry_wait(
        runner.session_store,
        source,
        boot_id="boot-2",
        resume_count=2,
    )
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 0

    current = runner.session_store._entries[session_key].active_turn
    assert current is not None
    assert current["status"] == BLOCKED
    assert current["blocked_reason"] == "retry_cap"
    adapter.handle_message.assert_not_called()


def test_changed_authorization_blocks_retry(tmp_path):
    source = make_restart_source(chat_id="revoked-retry")
    runner, adapter = make_restart_runner()
    runner._boot_id = "boot-2"
    runner.session_store = _store(tmp_path)
    session_key, _turn_id = _seed_retry_wait(runner.session_store, source, boot_id="boot-1")
    runner._is_user_authorized_for_source = lambda _source, **_kwargs: False
    adapter.handle_message = AsyncMock()

    assert runner._schedule_resume_pending_sessions() == 0

    current = runner.session_store._entries[session_key].active_turn
    assert current is not None
    assert current["status"] == BLOCKED
    assert current["blocked_reason"] == "authorization_unavailable"
    adapter.handle_message.assert_not_called()


@pytest.mark.parametrize("closers", [0, 1, 3])
@pytest.mark.parametrize("tool_name", ["read_file", "terminal"])
def test_recovery_uses_current_turn_boundary_not_persistence_offset(closers, tool_name):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    from agent.turn_resume import prepare_resume_history

    history = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "original unfinished task"},
    ] + [{"role": "assistant", "content": FAILED_TURN_NOTICE}] * closers
    normalized, _ = prepare_resume_history(history)
    messages = normalized + [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": tool_name, "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "outcome"},
    ]
    decision = failed_turn_recovery({
        "failed": True, "failure_retryable": True, "failure_reason": "timeout",
        "messages": messages, "history_offset": len(history), "current_turn_user_idx": 2,
    })
    assert decision["status"] == (RETRY_WAIT if tool_name == "read_file" else BLOCKED)
    if tool_name == "terminal":
        assert decision["blocked_reason"] == "external_effect_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["retry_wait", "resuming", "running", "interrupted", "blocked"])
@pytest.mark.parametrize("ledger_state", ["pending", "delivered", "failed"])
async def test_failed_notice_never_retires_unfinished_turn(tmp_path, monkeypatch, status, ledger_state):
    from gateway import delivery_ledger as ledger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="notice-family")
    runner.session_store = _store(tmp_path)
    runner._boot_id = "new-boot"
    runner._is_user_authorized_for_source = lambda *_args, **_kwargs: True
    key, turn_id = _seed_retry_wait(runner.session_store, source)
    event = MessageEvent(text="original task", message_type=MessageType.TEXT, source=source, message_id="initial")
    event._gateway_active_turn_id = turn_id
    event._gateway_response_kind = "failure_notice"
    oid = await adapter._record_delivery_obligation(event, key, "Failure notice", adapter, False)
    assert oid is not None
    if ledger_state == "delivered":
        ledger.mark_delivered(oid)
    elif ledger_state == "failed":
        ledger.mark_failed(oid, "send_path_degraded")
    else:
        ledger._update_state(oid, "pending")
    if status in {"resuming", "running", "interrupted"}:
        runner.session_store.begin_active_turn(key, turn_id, "old-boot", resume_count=int(status != "running"))
        if status == "interrupted":
            runner.session_store.mark_active_turn_interrupted(key, "shutdown")
    elif status == "blocked":
        runner.session_store.mark_active_turn_recovery(
            key, turn_id, expected_resume_count=0, status=BLOCKED,
            failure_reason="timeout", blocked_reason="external_effect_unknown",
        )
    runner.session_store.mark_resume_pending(key)
    runner.session_store = _store(tmp_path)
    assert await runner._reconcile_answered_turns() == 0
    assert runner.session_store.get_or_create_session(source).active_turn is not None
    adapter.handle_message = AsyncMock()
    assert runner._schedule_resume_pending_sessions() == int(status == "retry_wait")
    await asyncio.gather(*list(runner._background_tasks))
    assert runner.session_store.get_or_create_session(source).active_turn is not None


@pytest.mark.parametrize("outcome", ["unmatched", "unknown", "prior_write", "invalid_boundary"])
def test_later_retry_keeps_unproven_effects_blocked(outcome):
    messages = [
        {"role": "user", "content": "original task"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "terminal" if outcome == "prior_write" else "read_file"}},
        ]},
        {"role": "tool", "tool_call_id": "other" if outcome == "unmatched" else "c1", "content": "outcome"},
    ]
    if outcome == "unknown":
        messages[-1]["effect_disposition"] = "unknown"
    decision = failed_turn_recovery({
        "failed": True, "failure_retryable": True, "failure_reason": "timeout",
        "messages": messages, "history_offset": len(messages),
        "current_turn_user_idx": None if outcome == "invalid_boundary" else 0,
    })
    assert decision["status"] == BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["boot", "runtime", "flood_adoption"])
async def test_notice_redelivery_preserves_resume_flags(tmp_path, monkeypatch, route):
    from gateway import delivery_ledger as ledger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="redelivered-notice")
    key, turn_id = _seed_retry_wait(runner.session_store, source)
    runner.session_store.mark_resume_pending(key)
    ledger.record_obligation(
        obligation_id="notice", session_key=key, platform="telegram", chat_id=source.chat_id,
        thread_id=None, content="retry pending", turn_id=turn_id, response_kind="failure_notice",
    )
    if route == "runtime":
        ledger.mark_failed("notice", "send_path_degraded")
        rows = ledger.sweep_failed_for_runtime("telegram")
    else:
        if route == "flood_adoption":
            ledger.mark_failed("notice", "flood_control:60")
        monkeypatch.setattr(ledger, "_owner_alive", lambda *_args: False)
        rows = ledger.sweep_recoverable()
    assert len(rows) == 1
    assert rows[0]["response_kind"] == "failure_notice"
    assert rows[0]["turn_id"] == turn_id
    assert await runner._clear_resume_pending_for_claimed_obligations(rows, require_success=True) == rows
    assert _store(tmp_path).get_or_create_session(source).resume_pending
    if route != "flood_adoption":
        assert await runner._redeliver_claimed_obligations(rows) == 1
        assert adapter.sent
    assert _store(tmp_path).get_or_create_session(source).active_turn is not None
    assert not ledger.has_turn_obligation(key, turn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "final", "terminal_failure", "unsettled_failure"])
@pytest.mark.parametrize("status", ["retry_wait", "resuming", "blocked"])
async def test_final_and_ambiguous_obligations_never_replay(tmp_path, monkeypatch, kind, status):
    from gateway import delivery_ledger as ledger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="completed-or-ambiguous")
    key, turn_id = _seed_retry_wait(runner.session_store, source)
    if status == "resuming":
        runner.session_store.begin_active_turn(key, turn_id, "old-boot", resume_count=1)
    elif status == "blocked":
        runner.session_store.mark_active_turn_recovery(
            key, turn_id, expected_resume_count=0, status=BLOCKED,
            failure_reason="timeout", blocked_reason="external_effect_unknown",
        )
    ledger.record_obligation(
        obligation_id="barrier", session_key=key, platform="telegram", chat_id=source.chat_id,
        thread_id=None, content="already produced output", turn_id=turn_id, response_kind=kind,
    )
    ledger.mark_delivered("barrier")
    adapter.handle_message = AsyncMock()
    retired = await runner._reconcile_answered_turns()
    assert runner._schedule_resume_pending_sessions() == 0
    adapter.handle_message.assert_not_called()
    assert retired == int(status != "blocked")
    if status == "blocked":
        assert runner.session_store._entries[key].active_turn["status"] == BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_late_delivery_cannot_retire_new_attempt(tmp_path, failed):
    runner, _adapter = make_restart_runner()
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="late-delivery")
    key, turn_id = _seed_retry_wait(runner.session_store, source)
    token = runner.session_store.mark_turn_active(key)
    runner.session_store.begin_active_turn(key, turn_id, "boot", resume_count=1)
    event = SimpleNamespace(
        _gateway_active_turn_session_key=key, _gateway_active_turn_token=token,
        _gateway_active_turn_id=turn_id, _gateway_active_turn_expected_resume_count=0,
        _gateway_response_kind="failure_notice" if failed else "final",
    )
    if failed:
        event._gateway_turn_recovery = {"status": RETRY_WAIT, "failure_reason": "timeout"}
    await runner._clear_durable_active_turn(event, defer_delivery=True)
    assert not await runner._finish_durable_active_turn_after_delivery(event)
    assert _store(tmp_path).get_or_create_session(source).active_turn["resume_count"] == 1
    if failed:
        assert event._gateway_response_kind == "failure_notice"


@pytest.mark.asyncio
async def test_old_answer_redelivery_does_not_clear_new_turn_resume(tmp_path):
    runner, _adapter = make_restart_runner()
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="new-turn")
    key, _turn_id = _seed_retry_wait(runner.session_store, source)
    runner.session_store.mark_resume_pending(key)
    rows = [{"session_key": key, "turn_id": "older-turn", "response_kind": "final"}]
    assert await runner._clear_resume_pending_for_claimed_obligations(rows) == rows
    assert _store(tmp_path).get_or_create_session(source).resume_pending


@pytest.mark.asyncio
async def test_reconciliation_cannot_retire_attempt_started_during_ledger_read(tmp_path, monkeypatch):
    runner, _adapter = make_restart_runner()
    runner.session_store = _store(tmp_path)
    source = make_restart_source(chat_id="reconcile-race")
    key, turn_id = _seed_retry_wait(runner.session_store, source)

    def answered(*_args):
        runner.session_store.begin_active_turn(key, turn_id, "new-boot", resume_count=1)
        return True

    monkeypatch.setattr("gateway.delivery_ledger.has_turn_obligation", answered)
    assert await runner._reconcile_answered_turns() == 0
    assert _store(tmp_path).get_or_create_session(source).active_turn["resume_count"] == 1


def test_legacy_ledger_upgrade_keeps_ambiguous_output_as_replay_barrier(tmp_path, monkeypatch):
    import sqlite3
    from gateway import delivery_ledger as ledger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("""CREATE TABLE delivery_obligations (
            obligation_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
            platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT,
            content TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            owner_pid INTEGER, owner_started_at REAL, last_error TEXT,
            adapter_profile TEXT, turn_id TEXT
        )""")
        db.execute("""INSERT INTO delivery_obligations
            (obligation_id, session_key, platform, chat_id, content, state, created_at, updated_at, turn_id)
            VALUES ('legacy', 'session', 'telegram', 'chat', 'ambiguous old output', 'delivered', 1, 1, 'turn')""")
    assert ledger.has_turn_obligation("session", "turn")
    # Reopening through the current schema never upgrades ambiguous output to a notice.
    assert ledger.has_turn_obligation("session", "turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("history_kind", ["read_only", "external_write", "missing", "unmatched", "unknown"])
async def test_restart_during_retry_rechecks_the_interrupted_attempt(tmp_path, monkeypatch, history_kind):
    from gateway import delivery_ledger as ledger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = make_restart_source(chat_id="interrupted-retry")
    key, turn_id = _seed_retry_wait(
        runner.session_store, source, seed_history=False, seal=False,
    )
    entry = runner.session_store.get_or_create_session(source)
    if history_kind != "missing":
        owner = entry.active_turn["origin_owner"]
        for row in [
            {
                "role": "user",
                "content": "original task",
                "display_metadata": {"gateway_input_owner": owner},
            },
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "terminal" if history_kind == "external_write" else "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "other" if history_kind == "unmatched" else "c1", "content": "result",
             **({"effect_disposition": "unknown"} if history_kind == "unknown" else {})},
        ]:
            runner.session_store.append_to_transcript(entry.session_id, row)
        _seal_seeded_attempt(runner.session_store, key, turn_id)
    ledger.record_obligation(
        obligation_id="old-notice", session_key=key, platform="telegram", chat_id=source.chat_id,
        thread_id=None, content="previous failure", turn_id=turn_id, response_kind="failure_notice",
    )
    ledger.mark_delivered("old-notice")
    runner.session_store.begin_active_turn(key, turn_id, "dead-boot", resume_count=1)
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    runner._boot_id = "new-boot"
    runner._is_user_authorized_for_source = lambda *_args, **_kwargs: True
    adapter.handle_message = AsyncMock()
    # begin_active_turn creates an unsealed execution; even a read-only tail is not proof.
    assert runner._schedule_resume_pending_sessions() == 0
    await asyncio.gather(*list(runner._background_tasks))
    record = runner.session_store.get_or_create_session(source).active_turn
    assert record["status"] == BLOCKED
    adapter.handle_message.assert_not_called()


@pytest.fixture
def _m1_replan_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    hermes_home = home / "hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "approvals:\n  destructive_slash_confirm: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", hermes_home / "state.db")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return hermes_home


def _m1_replan_runner(tmp_path, adapter=None):
    config = GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test-token")},
    )
    runner = GatewayRunner(config)
    runner._running = True
    runner._is_user_authorized_for_source = lambda _source, **_kwargs: True
    transport = adapter or RestartTestAdapter()
    transport.gateway_runner = runner
    runner.adapters[Platform.TELEGRAM] = transport
    runner._wire_adapter_handlers(transport)
    return runner, transport, config


def _m1_replan_event(text, source, message_id):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
    )


def _m1_replan_patch_agent_runtime(monkeypatch, client, tool_name="read_file"):
    from tests.agent.test_run_agent import _make_tool_defs

    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **_kwargs: client)
    monkeypatch.setattr(
        "model_tools.get_tool_definitions",
        lambda *_args, **_kwargs: _make_tool_defs(tool_name),
    )
    monkeypatch.setattr("model_tools.check_toolset_requirements", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *_args, **_kwargs: 0.0)
    _skip_agent_retry_sleep(monkeypatch)
    monkeypatch.setattr("agent.turn_recovery_autorecover.auto_recovery_cycles", lambda _agent: 0)
    monkeypatch.setattr("gateway.turn_recovery.retry_delay_seconds", lambda _count: 60.0)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda *_args, **_kwargs: "test-model")
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://mock-provider.invalid/v1",
            "provider": "openai-compat",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 4)


async def _m1_replan_await_session_task(adapter, session_key, timeout=15.0):
    task = adapter._session_tasks.get(session_key)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


async def _m1_replan_await_background_tasks(runner, timeout=15.0):
    async def drain():
        while True:
            tasks = set(runner._background_tasks)
            for adapter in runner.adapters.values():
                tasks.update(adapter._session_tasks.values())
            tasks = {task for task in tasks if not task.done()}
            if not tasks:
                return
            await asyncio.gather(*tasks)
    await asyncio.wait_for(drain(), timeout=timeout)


async def _m1_replan_await_running_agent(runner, session_key, timeout=5.0):
    """Wait until the runner promotes the concrete agent used by busy commands."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    await _wait_until(
        lambda: runner._running_agents.get(session_key) not in (None, _AGENT_PENDING_SENTINEL),
        timeout=timeout,
    )


def _m1_replan_success_result(text):
    return {
        "final_response": text,
        "messages": [],
        "completed": True,
        "failed": False,
        "interrupted": False,
        "history_offset": 0,
    }


@pytest.mark.asyncio
async def test_m1_replan_r1_steer_preserves_original_effect_boundary(
    tmp_path, monkeypatch, _m1_replan_home,
):
    source = make_restart_source(chat_id="m1-r1-steer")
    provider_requests = []
    client = MagicMock()
    tool_entered = threading.Event()
    release_tool = threading.Event()
    tool_effects = []

    def complete(**kwargs):
        provider_requests.append(kwargs)
        if len(provider_requests) == 1:
            response = _provider_response(None)
            response.choices[0].finish_reason = "tool_calls"
            response.choices[0].message.tool_calls = [SimpleNamespace(
                id="terminal-effect-1",
                type="function",
                function=SimpleNamespace(name="terminal", arguments='{"command":"inert"}'),
            )]
            return response
        request = httpx.Request("POST", "https://mock-provider.invalid/v1/chat/completions")
        raise httpx.ConnectError("retryable provider outage", request=request)

    def inert_terminal(*args, **kwargs):
        tool_effects.append((args, kwargs))
        tool_entered.set()
        if not release_tool.wait(timeout=5.0):
            raise AssertionError("test did not release the inert terminal boundary")
        return "inert terminal effect completed"

    client.chat.completions.create.side_effect = complete
    _m1_replan_patch_agent_runtime(monkeypatch, client, tool_name="terminal")
    monkeypatch.setattr("model_tools.handle_function_call", inert_terminal)
    runner, adapter, config = _m1_replan_runner(tmp_path / "runtime")

    original = _m1_replan_event("perform the original task", source, "m1-r1-original")
    await adapter.handle_message(original)
    session_key = runner._session_key_for_source(source)
    assert await asyncio.wait_for(asyncio.to_thread(tool_entered.wait, 5.0), timeout=6.0)
    await _m1_replan_await_running_agent(runner, session_key)

    steer = _m1_replan_event("/steer continue carefully", source, "m1-r1-steer")
    await adapter.handle_message(steer)
    release_tool.set()
    await _m1_replan_await_session_task(adapter, session_key)

    entry = runner.session_store.get_or_create_session(source)
    transcript = runner.session_store.load_transcript(entry.session_id, repair_alternation=False)
    steer_rows = [row for row in transcript if row.get("display_kind") == "steer"]
    assert len(steer_rows) == 1 and "continue carefully" in str(steer_rows[0].get("content"))
    assert any(
        call.get("function", {}).get("name") == "terminal"
        for row in transcript
        for call in row.get("tool_calls", []) or []
    )
    assert any(row.get("tool_call_id") == "terminal-effect-1" for row in transcript)
    assert len(tool_effects) == 1

    wakeups = list(getattr(runner, "_retryable_turn_wakeups", {}).values())
    for task in wakeups:
        task.cancel()
    await asyncio.gather(*wakeups, return_exceptions=True)
    record = entry.active_turn
    assert record is not None
    if record.get("status") == RETRY_WAIT:
        assert runner.session_store.mark_active_turn_recovery(
            session_key,
            record["turn_id"],
            expected_resume_count=record["resume_count"],
            status=RETRY_WAIT,
            failure_reason=record.get("failure_reason") or "timeout",
            retry_delay=0,
        )

    restarted = GatewayRunner(config)
    restarted._running = True
    restarted._is_user_authorized_for_source = lambda _source, **_kwargs: True
    restarted_adapter = RestartTestAdapter()
    restarted_adapter.gateway_runner = restarted
    restarted.adapters[Platform.TELEGRAM] = restarted_adapter
    restarted._wire_adapter_handlers(restarted_adapter)
    stale_agent_calls = []

    async def stale_agent_spy(**kwargs):
        stale_agent_calls.append(kwargs)
        return _m1_replan_success_result("stale R1 resume must not run")

    restarted._run_agent = stale_agent_spy
    scheduled = restarted._schedule_resume_pending_sessions()
    await _m1_replan_await_background_tasks(restarted)

    assert stale_agent_calls == [], {
        "scheduled": scheduled,
        "active_turn": restarted.session_store._entries[session_key].active_turn,
        "durable_roles": [row.get("role") for row in transcript],
        "steer_rows": steer_rows,
        "tool_effect_count": len(tool_effects),
    }


@pytest.mark.asyncio
async def test_m1_replan_r2_stop_retry_wait_via_adapter(
    tmp_path, _m1_replan_home,
):
    source = make_restart_source(chat_id="m1-r2-stop")
    runner, adapter, config = _m1_replan_runner(tmp_path / "runtime")
    session_key, _turn_id = _seed_retry_wait(
        runner.session_store,
        source,
        boot_id="m1-r2-dead-boot",
        delay=0,
    )

    stop = _m1_replan_event("/stop", source, "m1-r2-stop-command")
    await adapter.handle_message(stop)
    await _m1_replan_await_session_task(adapter, session_key)
    stop_replies = list(adapter.sent)

    restarted = GatewayRunner(config)
    restarted._running = True
    restarted._is_user_authorized_for_source = lambda _source, **_kwargs: True
    restarted_adapter = RestartTestAdapter()
    restarted_adapter.gateway_runner = restarted
    restarted.adapters[Platform.TELEGRAM] = restarted_adapter
    restarted._wire_adapter_handlers(restarted_adapter)
    stale_agent_calls = []

    async def stale_agent_spy(**kwargs):
        stale_agent_calls.append(kwargs)
        return _m1_replan_success_result("stale R2 resume must not run")

    restarted._run_agent = stale_agent_spy
    scheduled = restarted._schedule_resume_pending_sessions()
    await _m1_replan_await_background_tasks(restarted)

    assert stale_agent_calls == [], {
        "scheduled": scheduled,
        "stop_replies": stop_replies,
        "active_turn": restarted.session_store._entries[session_key].active_turn,
    }
    assert stop_replies and all("No active task" not in reply for reply in stop_replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["new", "reset"])
async def test_m1_replan_r3_reset_after_claim_via_adapter(
    tmp_path, _m1_replan_home, command,
):
    source = make_restart_source(chat_id=f"m1-r3-{command}")
    runner, adapter, _config = _m1_replan_runner(tmp_path / command)
    session_key, _turn_id = _seed_retry_wait(
        runner.session_store,
        source,
        boot_id="m1-r3-dead-boot",
        delay=0,
    )
    old_session_id = runner.session_store._entries[session_key].session_id
    dispatch_claimed = asyncio.Event()
    release_dispatch = asyncio.Event()
    real_dispatch = runner._run_startup_resume_event

    async def dispatch_after_barrier(*args, **kwargs):
        dispatch_claimed.set()
        await asyncio.wait_for(release_dispatch.wait(), timeout=5.0)
        return await real_dispatch(*args, **kwargs)

    runner._run_startup_resume_event = dispatch_after_barrier
    stale_agent_calls = []

    async def stale_agent_spy(**kwargs):
        stale_agent_calls.append(kwargs)
        return _m1_replan_success_result("stale R3 resume must not run")

    runner._run_agent = stale_agent_spy
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.wait_for(dispatch_claimed.wait(), timeout=5.0)
    claimed_tasks = list(runner._background_tasks)

    reset = _m1_replan_event(f"/{command}", source, f"m1-r3-{command}-command")
    await adapter.handle_message(reset)
    await _m1_replan_await_session_task(adapter, session_key)
    new_session_id = runner.session_store._entries[session_key].session_id
    assert new_session_id != old_session_id
    command_send_count = len(adapter.sent)

    release_dispatch.set()
    if claimed_tasks:
        await asyncio.wait_for(
            asyncio.gather(*claimed_tasks, return_exceptions=True),
            timeout=15.0,
        )

    new_transcript = runner.session_store.load_transcript(
        new_session_id,
        repair_alternation=False,
    )
    blank_user_rows = [
        row for row in new_transcript
        if row.get("role") == "user" and not str(row.get("content") or "").strip()
    ]
    stale_replies = adapter.sent[command_send_count:]
    assert stale_agent_calls == [], {
        "command": command,
        "old_session_id": old_session_id,
        "new_session_id": new_session_id,
        "agent_calls": stale_agent_calls,
        "blank_user_rows": blank_user_rows,
        "stale_replies": stale_replies,
    }
    assert runner.session_store._entries[session_key].session_id == new_session_id
    assert blank_user_rows == []
    assert stale_replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_store", [False, True], ids=["same_boot", "restart"])
async def test_m1_replan_valid_real_agent_resume_positive_control(
    tmp_path, monkeypatch, _m1_replan_home, fresh_store,
):
    source = make_restart_source(chat_id="m1-positive-control")
    provider_state = {"healthy": False, "requests": [], "healthy_requests": 0}
    client = MagicMock()

    def complete(**kwargs):
        provider_state["requests"].append(kwargs)
        if not provider_state["healthy"]:
            request = httpx.Request("POST", "https://mock-provider.invalid/v1/chat/completions")
            raise httpx.ConnectError("retryable provider outage", request=request)
        provider_state["healthy_requests"] += 1
        return _provider_response(RECOVERED_RESPONSE)

    client.chat.completions.create.side_effect = complete
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    transport = _RecoveryTransport(provider_state)
    runner, adapter, _config = _m1_replan_runner(tmp_path / "runtime", adapter=transport)
    agent_results = []
    real_run_agent = runner._run_agent

    async def capture_real_agent(*args, **kwargs):
        result = await real_run_agent(*args, **kwargs)
        agent_results.append(result)
        return result

    runner._run_agent = capture_real_agent
    original = _m1_replan_event("resume this read-only request", source, "m1-positive-original")
    await adapter.handle_message(original)
    session_key = runner._session_key_for_source(source)
    await _m1_replan_await_session_task(adapter, session_key)
    record = runner.session_store._entries[session_key].active_turn
    assert record is not None and record["status"] == RETRY_WAIT
    assert provider_state["healthy"] is True

    wakeups = list(getattr(runner, "_retryable_turn_wakeups", {}).values())
    for task in wakeups:
        task.cancel()
    await asyncio.gather(*wakeups, return_exceptions=True)
    assert runner.session_store.mark_active_turn_recovery(
        session_key,
        record["turn_id"],
        expected_resume_count=record["resume_count"],
        status=RETRY_WAIT,
        failure_reason=record.get("failure_reason") or "timeout",
        retry_delay=0,
    )

    if fresh_store:
        runner, adapter, _ = _m1_replan_runner(
            tmp_path / "runtime", adapter=_RecoveryTransport(provider_state),
        )
        real_run_agent = runner._run_agent
        runner._run_agent = capture_real_agent
    assert runner._schedule_resume_pending_sessions() == 1
    await _m1_replan_await_background_tasks(runner)

    assert len(agent_results) == 2
    assert agent_results[0]["failed"] is True
    assert agent_results[1]["failed"] is False
    assert provider_state["healthy_requests"] == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert runner.session_store._entries[session_key].active_turn is None


@pytest.mark.asyncio
async def test_m1_replan_r1_restart_steer_effect_fixture(tmp_path, _m1_replan_home):
    """Reconstruct review R1 crash state; not a claim of live crash injection."""
    from agent.prompt_builder import steer_user_row

    source = make_restart_source(chat_id="m1-r1-crash-fixture")
    runner, _adapter, config = _m1_replan_runner(tmp_path / "runtime")
    key, turn = _seed_retry_wait(runner.session_store, source)
    sid = runner.session_store._entries[key].session_id
    for row in [
        {"role": "assistant", "tool_calls": [{"id": "effect-before-steer", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "effect-before-steer", "content": "inert recorded effect"},
        steer_user_row("continue carefully"),
        {"role": "assistant", "tool_calls": [{"id": "read-after-steer", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "read-after-steer", "content": "read result"},
    ]:
        runner.session_store.append_to_transcript(sid, row)
    runner.session_store.begin_active_turn(key, turn, "dead-boot", resume_count=1)
    history = runner.session_store.load_transcript(sid, repair_alternation=False)
    full = failed_turn_recovery({"failed": True, "failure_retryable": True,
        "failure_reason": "timeout", "messages": history, "current_turn_user_idx": 0})
    assert full["status"] == BLOCKED
    restarted, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    calls = []
    async def spy(**kwargs):
        calls.append(kwargs)
        return _m1_replan_success_result("forbidden stale recovery")
    restarted._run_agent = spy
    scheduled = restarted._schedule_resume_pending_sessions()
    await _m1_replan_await_background_tasks(restarted)
    assert scheduled == 0 and calls == [], {"scheduled": scheduled, "calls": len(calls), "full": full}


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("execution_session_id", "other-session"),
        ("turn_id", "other-turn"),
        ("resume_count", 99),
        ("boot_id", "other-boot"),
        ("dispatch_token", "other-token"),
        ("recovery_version", 2),
        ("recovery_version", True),
    ],
)
def test_m1_dispatch_identity_mismatch_fails_closed(tmp_path, field, replacement):
    store = _store(tmp_path)
    source = make_restart_source(chat_id=f"identity-{field}")
    key, turn_id = _seed_retry_wait(store, source, boot_id="dead-boot")
    entry = store._entries[key]
    assert store.claim_resume_active_turn(
        key,
        turn_id,
        "new-boot",
        1,
        expected_session_id=entry.session_id,
        expected_turn_id=turn_id,
        expected_resume_count=0,
        expected_status=RETRY_WAIT,
    )
    marker = {
        name: entry.active_turn[name] for name in (
            "recovery_version", "turn_id", "origin_session_id", "execution_session_id",
            "origin_owner", "boot_id", "dispatch_token", "resume_count",
        )
    }
    marker[field] = replacement

    assert store.resume_owner_matches(key, marker, phase="queued") is False
    assert store.consume_resume_dispatch(key, marker) is False
    assert entry.active_turn["dispatch_state"] == "queued"


def test_m1_dispatch_ticket_is_single_use_and_cancel_is_sticky(tmp_path):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="single-use")
    key, turn_id = _seed_retry_wait(store, source, boot_id="dead-boot")
    entry = store._entries[key]
    assert store.claim_resume_active_turn(
        key,
        turn_id,
        "new-boot",
        1,
        expected_session_id=entry.session_id,
        expected_turn_id=turn_id,
        expected_resume_count=0,
        expected_status=RETRY_WAIT,
    )
    marker = {
        name: entry.active_turn[name] for name in (
            "recovery_version", "turn_id", "origin_session_id", "execution_session_id",
            "origin_owner", "boot_id", "dispatch_token", "resume_count",
        )
    }

    assert store.consume_resume_dispatch(key, marker) is True
    assert store.consume_resume_dispatch(key, marker) is False
    assert store.resume_owner_matches(key, marker, phase="executing") is True
    assert store.cancel_active_turn_recovery(
        key, expected_session_id=entry.session_id, reason="test_stop",
    ) is True
    assert store.resume_owner_matches(key, marker, phase="executing") is False
    assert store.mark_active_turn_recovery(
        key,
        turn_id,
        expected_resume_count=1,
        status=RETRY_WAIT,
        failure_reason="late_failure",
        expected_dispatch_token=marker["dispatch_token"],
    ) is False
    assert _store(tmp_path).get_or_create_session(source).active_turn["blocked_reason"] == "user_cancelled"


def test_m1_prior_turn_effect_is_outside_owned_evidence(tmp_path):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="prior-effect")
    entry = store.get_or_create_session(source)
    for row in (
        {"role": "user", "content": "prior task"},
        {"role": "assistant", "tool_calls": [
            {"id": "old-write", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "old-write", "content": "done"},
    ):
        store.append_to_transcript(entry.session_id, row)
    key, _turn_id = _seed_retry_wait(store, source)

    evidence = store.load_turn_recovery_evidence(store._entries[key].active_turn)
    assert evidence["rows"][0]["content"] == "original task"
    assert all(row.get("tool_call_id") != "old-write" for row in evidence["rows"])
    decision = failed_turn_recovery({
        "failed": True,
        "failure_retryable": True,
        "failure_reason": "timeout",
    }, evidence=evidence)
    assert decision["status"] == RETRY_WAIT


def test_m1_checkpoint_change_and_missing_origin_fail_closed(tmp_path):
    from gateway.session_transcript import RecoveryEvidenceError

    store = _store(tmp_path)
    source = make_restart_source(chat_id="proof-loss")
    key, _turn_id = _seed_retry_wait(store, source)
    record = dict(store._entries[key].active_turn)

    changed = {**record, "checkpoint": {**record["checkpoint"], "evidence_sha256": "0" * 64}}
    with pytest.raises(RecoveryEvidenceError, match="checkpoint_evidence_changed"):
        store.load_turn_recovery_evidence(changed)

    missing = {**record, "origin_owner": "missing-owner", "origin_row_id": None}
    with pytest.raises(RecoveryEvidenceError, match="missing_turn_boundary"):
        store.load_turn_recovery_evidence(missing)


def test_m1_malformed_versioned_record_never_becomes_legacy_resume(tmp_path):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="malformed-record")
    entry = store.get_or_create_session(source)
    raw = entry.to_dict()
    raw["resume_pending"] = True
    raw["active_turn"] = {
        "recovery_version": 1,
        "turn_id": "",
        "resume_count": True,
        "status": "not-a-state",
    }
    malformed = type(entry).from_dict(raw)

    assert malformed.active_turn["status"] == BLOCKED
    assert malformed.active_turn["blocked_reason"] == "invalid_recovery_record"


def _m1_pi_claim_event(store, source, *, phase="queued"):
    key, turn = _seed_retry_wait(store, source, boot_id="dead-boot")
    entry = store._entries[key]
    assert store.claim_resume_active_turn(
        key, turn, "claim-boot", 1, expected_session_id=entry.session_id,
        expected_turn_id=turn, expected_resume_count=0, expected_status=RETRY_WAIT,
        expected_identity=dict(entry.active_turn),
    )
    marker = {name: entry.active_turn[name] for name in (
        "recovery_version", "turn_id", "origin_session_id", "execution_session_id",
        "origin_owner", "boot_id", "dispatch_token", "resume_count",
    )}
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True,
                         metadata={"gateway_session_key": key, "gateway_session_id": entry.session_id})
    event._hermes_turn_resume = marker
    event._gateway_active_turn_id = turn
    if phase == "executing":
        assert store.consume_resume_dispatch(key, marker)
    return key, event


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,persist", [("terminal", False), ("read_file", False), ("read_file", True)])
async def test_m1_pi_settlement_requires_actual_result_coverage(tmp_path, tool, persist):
    from gateway.session import AsyncSessionStore
    from gateway.run_turn import GatewayTurnMixin

    store = _store(tmp_path)
    source = make_restart_source(chat_id="settlement-coverage")
    entry = store.get_or_create_session(source)
    key = entry.session_key
    store.begin_active_turn(key, "coverage-turn", "boot", origin_session_id=entry.session_id,
                            origin_owner="coverage-owner")
    user = {"role": "user", "content": "read", "display_metadata": {"gateway_input_owner": "coverage-owner"}}
    store.append_to_transcript(entry.session_id, user)
    rows = [user, {"role": "assistant", "tool_calls": [
        {"id": "call", "type": "function", "function": {"name": tool, "arguments": "{}"}},
    ]}, {"role": "tool", "tool_call_id": "call", "content": "inert result",
         "effect_disposition": "unknown" if tool == "terminal" else "none"}]
    if persist:
        for row in rows[1:]:
            store.append_to_transcript(entry.session_id, row)
    event = SimpleNamespace(_gateway_active_turn_id="coverage-turn")
    result = {"failed": True, "failure_retryable": True, "failure_reason": "timeout",
              "messages": rows, "current_turn_user_idx": 0}
    await GatewayTurnMixin._hmwa_settle_retryable_turn(
        SimpleNamespace(async_session_store=AsyncSessionStore(store)), event=event,
        session_entry=entry, session_key=key, agent_result=result,
    )
    assert event._gateway_turn_recovery["status"] == (RETRY_WAIT if persist else BLOCKED)
    if persist:
        assert entry.active_turn["checkpoint"]["evidence_row_count"] == len(rows)
    else:
        assert event._gateway_turn_recovery["blocked_reason"] == "unpersisted_result_evidence"
        assert "checkpoint" not in entry.active_turn


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "executing_unsealed", "retry_wait_sealed"])
async def test_m1_r1_crash_checkpoint_policy(tmp_path, monkeypatch, _m1_replan_home, phase):
    source = make_restart_source(chat_id="crash-phases")
    runner, _, _ = _m1_replan_runner(tmp_path / "runtime")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    if phase != "queued":
        assert runner.session_store.consume_resume_dispatch(key, event._hermes_turn_resume)
    if phase == "retry_wait_sealed":
        record = dict(runner.session_store._entries[key].active_turn)
        proof = runner.session_store.load_turn_recovery_evidence(record)
        assert runner.session_store.seal_active_turn_evidence(
            key, record["turn_id"], expected_resume_count=1,
            origin_row_id=proof["origin_row_id"], checkpoint=proof["checkpoint"],
            expected_identity=record,
        )
        assert runner.session_store.mark_active_turn_recovery(
            key, record["turn_id"], expected_resume_count=1, status=RETRY_WAIT,
            failure_reason="timeout", expected_identity=record,
        )
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    restarted, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    assert restarted._schedule_resume_pending_sessions() == int(phase != "executing_unsealed")
    await _m1_replan_await_background_tasks(restarted)
    assert client.chat.completions.create.call_count == int(phase != "executing_unsealed")
    assert adapter.sent.count(RECOVERED_RESPONSE) == int(phase != "executing_unsealed")
    if phase == "executing_unsealed":
        assert restarted.session_store._entries[key].active_turn["blocked_reason"] == "unsealed_attempt"


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["concurrent", "sequential", "queued_copy"])
async def test_m1_r3_duplicate_event_consumed_once(tmp_path, monkeypatch, _m1_replan_home, duplicate):
    from copy import copy
    from gateway.platforms.base import merge_pending_message_event

    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="duplicate-events")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    if duplicate == "concurrent":
        await asyncio.gather(adapter.handle_message(event), adapter.handle_message(copy(event)))
    elif duplicate == "queued_copy":
        guard = asyncio.Event()
        adapter._active_sessions[key] = guard
        merge_pending_message_event(adapter._pending_messages, key, event)
        merge_pending_message_event(adapter._pending_messages, key, copy(event), merge_text=True)
        await adapter._drain_pending_after_session_command(key, guard)
    else:
        await adapter.handle_message(event)
        await _m1_replan_await_background_tasks(runner)
        await adapter.handle_message(copy(event))
    await _m1_replan_await_background_tasks(runner)
    assert client.chat.completions.create.call_count == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert runner.session_store._entries[key].active_turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["normal_drain", "command_drain", "late_finally_drain", "runner_fifo"])
@pytest.mark.parametrize("recovery_first", [False, True])
async def test_m1_r3_queue_redelivery_revalidates(
    tmp_path, monkeypatch, _m1_replan_home, route, recovery_first,
):
    from gateway.platforms.base import merge_pending_message_event

    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="queue-redelivery")
    key, recovery = _m1_pi_claim_event(runner.session_store, source)
    assert runner.session_store.cancel_active_turn_recovery(
        key, expected_session_id=runner.session_store._entries[key].session_id, reason="test_cancel",
    )
    human = _m1_replan_event("human remains separate", source, "human-fifo")
    events = [recovery, human] if recovery_first else [human, recovery]
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    for event in events:
        if route == "runner_fifo":
            runner._enqueue_fifo(key, event, adapter)
        else:
            merge_pending_message_event(adapter._pending_messages, key, event, merge_text=True)
    if route == "command_drain":
        await adapter._drain_pending_after_session_command(key, guard)
    elif route == "late_finally_drain":
        adapter._finish_session_task(key, guard)
    else:
        # A real ordinary turn traverses the normal/finally drain. FIFO promotion is
        # performed by the wired runner callback, not a substituted queue processor.
        lead = _m1_replan_event("lead turn", source, "lead-fifo")
        await adapter._process_message_background(lead, key)
    await _m1_replan_await_background_tasks(runner)
    expected = 2 if route in {"normal_drain", "runner_fifo"} else 1
    assert client.chat.completions.create.call_count == expected
    assert adapter.sent.count(RECOVERED_RESPONSE) == expected
    transcript = runner.session_store.load_transcript(
        runner.session_store._entries[key].session_id, repair_alternation=False,
    )
    users = [row for row in transcript if row.get("role") == "user"]
    assert sum(row.get("content") == "human remains separate" for row in users) == 1
    assert all(str(row.get("content") or "").strip() for row in users)
    assert recovery.text == "" and human.text == "human remains separate"


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_m1_pi_cancel_write_quarantines_exact_attempt(
    tmp_path, monkeypatch, _m1_replan_home, queued,
):
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="cancel-write")
    if queued:
        key, event = _m1_pi_claim_event(runner.session_store, source)
    else:
        key, _ = _seed_retry_wait(runner.session_store, source, delay=0)
        event = None
    entry = runner.session_store._entries[key]
    before = dict(entry.active_turn)
    save = runner.session_store._save_entry

    def fail_cancel(*args, **kwargs):
        if ((kwargs.get("entry_data") or {}).get("active_turn") or {}).get("blocked_reason") == "user_cancelled":
            raise OSError("injected cancel write fault")
        return save(*args, **kwargs)

    monkeypatch.setattr(runner.session_store, "_save_entry", fail_cancel)
    if queued:
        adapter._active_sessions[key] = asyncio.Event()
        adapter._pending_messages[key] = event
    await adapter.handle_message(_m1_replan_event("/stop", source, "stop-fault"))
    await _m1_replan_await_background_tasks(runner)
    assert entry.active_turn == before  # Failed write was never published as durable success.
    assert runner.session_store.recovery_is_quarantined(key, before)
    assert any("not guaranteed" in text for text in adapter.sent)
    assert not any("I interrupted local work" in text for text in adapter.sent)
    assert runner._schedule_resume_pending_sessions() == 0
    if event:
        assert not runner.session_store.consume_resume_dispatch(key, event._hermes_turn_resume)
        await adapter.handle_message(event)
        await _m1_replan_await_background_tasks(runner)
    monkeypatch.setattr(runner.session_store, "_save_entry", save)
    runner.session_store.begin_active_turn(key, "new-user-turn", "boot", origin_session_id=entry.session_id,
                                          origin_owner="new-owner")
    assert not runner.session_store.recovery_is_quarantined(key, entry.active_turn)


@pytest.mark.parametrize("field", ["boot_id", "execution_session_id", "origin_session_id", "origin_owner", "dispatch_token"])
def test_m1_pi_publication_requires_complete_identity(tmp_path, field):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="publication-identity")
    key, event = _m1_pi_claim_event(store, source, phase="executing")
    entry = store._entries[key]
    receipt = dict(entry.active_turn)
    evidence = store.load_turn_recovery_evidence(receipt)
    # Simulate an intervening generation change while evidence was being read.
    entry.active_turn = {**entry.active_turn, field: "replacement"}
    assert not store.seal_active_turn_evidence(
        key, receipt["turn_id"], expected_resume_count=receipt["resume_count"],
        origin_row_id=evidence["origin_row_id"], checkpoint=evidence["checkpoint"], expected_identity=receipt,
    )
    assert not store.mark_active_turn_recovery(
        key, receipt["turn_id"], expected_resume_count=receipt["resume_count"],
        status=RETRY_WAIT, failure_reason="late_result", expected_identity=receipt,
    )
    assert entry.active_turn[field] == "replacement"


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_effect", [False, True], ids=["readonly_steer", "prior_completed_effect"])
async def test_m1_r1_readonly_steer_recovers_same_origin(
    tmp_path, monkeypatch, _m1_replan_home, prior_effect,
):
    source = make_restart_source(chat_id="readonly-steer-positive")
    entered, release = threading.Event(), threading.Event()
    state = {"phase": "prior" if prior_effect else "current", "healthy": False, "requests": [], "healthy_requests": 0}
    effects = []
    client = MagicMock()

    def complete(**kwargs):
        state["requests"].append(kwargs)
        phase = state["phase"]
        if phase == "prior":
            if state.get("prior_call"):
                return _provider_response("Prior effect completed.")
            state["prior_call"] = True
            name, call_id = "terminal", "prior-effect"
        elif state["healthy"]:
            state["healthy_requests"] += 1
            return _provider_response(RECOVERED_RESPONSE)
        elif not state.get("current_call"):
            state["current_call"] = True
            name, call_id = "read_file", "current-read"
        else:
            raise httpx.ConnectError("transient failure", request=httpx.Request("POST", "https://mock.invalid"))
        result = _provider_response(None)
        result.choices[0].finish_reason = "tool_calls"
        result.choices[0].message.tool_calls = [SimpleNamespace(
            id=call_id, type="function", function=SimpleNamespace(name=name, arguments="{}"),
        )]
        return result

    def inert_tool(*args, **kwargs):
        effects.append(state["phase"])
        if state["phase"] == "current":
            entered.set()
            assert release.wait(5), "steer barrier was not released"
        return "inert tool result"

    client.chat.completions.create.side_effect = complete
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    from tests.agent.test_run_agent import _make_tool_defs
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **k: _make_tool_defs("terminal", "read_file"))
    monkeypatch.setattr("model_tools.handle_function_call", inert_tool)
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime", adapter=_RecoveryTransport(state))
    key = runner._session_key_for_source(source)
    if prior_effect:
        await adapter.handle_message(_m1_replan_event("complete prior effect", source, "prior"))
        await _m1_replan_await_session_task(adapter, key)
        assert adapter.sent.count("Prior effect completed.") == 1
        state["phase"] = "current"
    await adapter.handle_message(_m1_replan_event("read current task", source, "current"))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
    await _m1_replan_await_running_agent(runner, key)
    await adapter.handle_message(_m1_replan_event("/steer continue carefully", source, "steer"))
    release.set()
    await _m1_replan_await_session_task(adapter, key)
    entry = runner.session_store._entries[key]
    record = dict(entry.active_turn)
    assert record["status"] == RETRY_WAIT
    evidence = runner.session_store.load_turn_recovery_evidence(record)
    assert any(row.get("tool_call_id") == "current-read" for row in evidence["rows"])
    assert not any(row.get("tool_call_id") == "prior-effect" for row in evidence["rows"])
    for timer in list(getattr(runner, "_retryable_turn_wakeups", {}).values()):
        timer.cancel()
    await asyncio.gather(*list(getattr(runner, "_retryable_turn_wakeups", {}).values()), return_exceptions=True)
    assert runner.session_store.mark_active_turn_recovery(
        key, record["turn_id"], expected_resume_count=record["resume_count"],
        status=RETRY_WAIT, failure_reason="timeout", retry_delay=0, expected_identity=record,
    )
    restarted, adapter, _ = _m1_replan_runner(tmp_path / "runtime", adapter=_RecoveryTransport(state))
    assert restarted._schedule_resume_pending_sessions() == 1
    await _m1_replan_await_background_tasks(restarted)
    assert state["healthy_requests"] == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert effects == (["prior", "current"] if prior_effect else ["current"])
    rows = restarted.session_store.load_transcript(entry.session_id, repair_alternation=False)
    assert sum((row.get("display_metadata") or {}).get("gateway_input_owner") == record["origin_owner"] for row in rows) == 1
    assert any(row.get("display_kind") == "steer" for row in rows)
    assert all(str(row.get("content") or "").strip() for row in rows if row.get("role") == "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_m1_cancelled_result_cannot_rearm(tmp_path, monkeypatch, _m1_replan_home, late_failure):
    source = make_restart_source(chat_id="busy-stop-result-race")
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    entered, release = threading.Event(), threading.Event()
    client = MagicMock()

    def complete(**kwargs):
        entered.set()
        assert release.wait(5), "provider result barrier was not released"
        if late_failure:
            raise httpx.ConnectError("late failure", request=httpx.Request("POST", "https://mock.invalid"))
        return _provider_response("LATE RESULT MUST NOT BE SENT")

    client.chat.completions.create.side_effect = complete
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    await adapter.handle_message(event)
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
    cancelled = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_cancel = runner.session_store.cancel_active_turn_recovery

    def cancel_and_signal(*args, **kwargs):
        result = real_cancel(*args, **kwargs)
        loop.call_soon_threadsafe(cancelled.set)
        return result

    monkeypatch.setattr(runner.session_store, "cancel_active_turn_recovery", cancel_and_signal)
    stop_task = asyncio.create_task(adapter.handle_message(_m1_replan_event("/stop", source, "busy-stop")))
    try:
        await asyncio.wait_for(cancelled.wait(), 4)
    finally:
        release.set()
    await asyncio.wait_for(stop_task, 10)
    await _m1_replan_await_background_tasks(runner)
    record = runner.session_store._entries[key].active_turn
    assert record["status"] == BLOCKED and record["blocked_reason"] == "user_cancelled"
    assert not getattr(runner, "_retryable_turn_wakeups", {})
    assert runner._schedule_resume_pending_sessions() == 0
    assert "LATE RESULT MUST NOT BE SENT" not in adapter.sent
    empty = make_restart_source(chat_id="empty-stop-control")
    await adapter.handle_message(_m1_replan_event("/stop", empty, "empty-stop"))
    await _m1_replan_await_background_tasks(runner)
    assert runner.session_store.get_or_create_session(empty).active_turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_adapter", "during_prepare"])
async def test_m1_r2_stop_after_claim(tmp_path, monkeypatch, _m1_replan_home, boundary):
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="stop-after-claim")
    key, _ = _seed_retry_wait(runner.session_store, source, boot_id="dead", delay=0)
    entered, release = asyncio.Event(), asyncio.Event()
    target = "_run_startup_resume_event" if boundary == "before_adapter" else "_hmwa_prepare_turn"
    original = getattr(runner, target)

    async def pause_then_continue(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runner, target, pause_then_continue)
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response("STALE RECOVERY")
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    assert runner._schedule_resume_pending_sessions() == 1
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await asyncio.wait_for(adapter.handle_message(_m1_replan_event("/stop", source, "stop")), 5)
        await _m1_replan_await_session_task(adapter, key)
        assert runner.session_store._entries[key].active_turn["blocked_reason"] == "user_cancelled"
        sent_at_stop = len(adapter.sent)
    finally:
        release.set()
    await _m1_replan_await_background_tasks(runner)
    assert client.chat.completions.create.call_count == 0
    assert adapter.sent[sent_at_stop:] == []
    assert runner._schedule_resume_pending_sessions() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["origin_read", "claim_write", "consume_write", "checkpoint_write"])
async def test_m1_recovery_store_failures_fail_closed(tmp_path, monkeypatch, _m1_replan_home, fault):
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="store-fault")
    key, _ = _seed_retry_wait(runner.session_store, source, boot_id="dead", delay=0)
    store = runner.session_store
    injected = []
    client = MagicMock()
    if fault == "checkpoint_write":
        client.chat.completions.create.side_effect = httpx.ConnectError(
            "provider unavailable", request=httpx.Request("POST", "https://mock.invalid"),
        )
    else:
        client.chat.completions.create.return_value = _provider_response("MUST NOT RUN")
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    if fault == "origin_read":
        db = store._db_for_session_id(store._entries[key].session_id)

        def read_fault(*args, **kwargs):
            injected.append(fault)
            raise OSError("injected raw read fault")

        monkeypatch.setattr(db, "get_turn_recovery_rows", read_fault)
    else:
        save = store._save_entry

        def save_fault(*args, **kwargs):
            record = (kwargs.get("entry_data") or {}).get("active_turn") or {}
            matches = {
                "claim_write": record.get("dispatch_state") == "queued",
                "consume_write": record.get("dispatch_state") == "executing",
                "checkpoint_write": (record.get("checkpoint") or {}).get("resume_count") == 1,
            }
            if matches[fault]:
                injected.append(fault)
                raise OSError("injected " + fault)
            return save(*args, **kwargs)

        monkeypatch.setattr(store, "_save_entry", save_fault)
    runner._schedule_resume_pending_sessions()
    await _m1_replan_await_background_tasks(runner)
    assert injected, "fault must hit the real operation"
    if fault == "checkpoint_write":
        assert client.chat.completions.create.call_count > 0
        record = store._entries[key].active_turn
        assert not record or (record.get("checkpoint") or {}).get("resume_count") != 1
        assert not record or record.get("status") != RETRY_WAIT
    else:
        assert client.chat.completions.create.call_count == 0
    assert not getattr(runner, "_retryable_turn_wakeups", {})
    assert "MUST NOT RUN" not in adapter.sent


def test_m1_pi_raw_evidence_uses_one_statement_snapshot(tmp_path, monkeypatch):
    """A competing append after the SELECT cannot create a hybrid origin/tail read."""
    from contextlib import contextmanager
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "snapshot.db")
    writer = SessionDB(tmp_path / "snapshot.db")
    db.create_session("snapshot", source="test")
    origin = db.append_message("snapshot", "user", "original", display_metadata={"gateway_input_owner": "owner"})
    read_ctx = db._read_ctx
    statements = []

    class ReadConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            if sql.lstrip().upper().startswith("SELECT"):
                # End the statement before the competing write, including DELETE-journal
                # hosts. A second SELECT would then observe the newer tail.
                rows = cursor.fetchall()
                statements.append(sql)
                if len(statements) == 1:
                    writer.append_message("snapshot", "assistant", tool_calls=[{
                        "id": "late-effect", "function": {"name": "terminal", "arguments": "{}"},
                    }])
                return SimpleNamespace(fetchall=lambda: rows)
            return cursor

    @contextmanager
    def interleaved_read():
        with read_ctx() as connection:
            yield ReadConnection(connection)

    try:
        monkeypatch.setattr(db, "_read_ctx", interleaved_read)
        snapshot = db.get_turn_recovery_rows("snapshot", "owner")
        assert [row["id"] for row in snapshot] == [origin]
        assert len(statements) == 1
        monkeypatch.setattr(db, "_read_ctx", read_ctx)
        assert len(db.get_turn_recovery_rows("snapshot", "owner")) == 2
    finally:
        writer.close()
        db.close()


@pytest.mark.parametrize("version", [True, False, "1", 1.0, None])
def test_m1_pi_version_type_never_coerces_into_v1(tmp_path, version):
    store = _store(tmp_path)
    source = make_restart_source(chat_id="version-types")
    key, event = _m1_pi_claim_event(store, source)
    entry = store._entries[key]
    raw = entry.to_dict()
    raw["active_turn"] = {**raw["active_turn"], "recovery_version": version}
    loaded = type(entry).from_dict(raw)
    assert loaded.active_turn["status"] == BLOCKED
    event._hermes_turn_resume["recovery_version"] = version
    assert not store.resume_owner_matches(key, event._hermes_turn_resume)
    assert not store.consume_resume_dispatch(key, event._hermes_turn_resume)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["in_place_kept_origin", "in_place_summary_only", "rotated", "missing_raw_origin"])
async def test_m1_r1_compaction_evidence_publication(tmp_path, monkeypatch, _m1_replan_home, mode):
    """Exercise real publication and settlement; summary generation is outside this test cut."""
    import copy
    import time
    from agent import conversation_compression as cc
    from gateway.session import AsyncSessionStore
    from gateway.run_turn import GatewayTurnMixin
    from run_agent import AIAgent

    store = _store(tmp_path)
    source = make_restart_source(chat_id="compression-publication")
    key, _ = _seed_retry_wait(store, source, seal=False)
    entry = store._entries[key]
    record = dict(entry.active_turn)
    sid = entry.session_id
    rows = store.load_transcript(sid, repair_alternation=False)
    rows.extend([
        {"role": "assistant", "tool_calls": [{
            "id": "effect", "type": "function", "function": {"name": "terminal", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "effect", "content": "inert result " * 200,
         "effect_disposition": "unknown"},
        {"role": "user", "content": "continue carefully", "display_kind": "steer"},
    ])
    for row in rows[1:]:
        store.append_to_transcript(sid, row)
    db = store._db_for_session_id(sid)
    client = MagicMock()
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    agent = AIAgent(
        api_key="test-key", base_url="https://mock.invalid/v1", model="test-model",
        provider="openai-compat", api_mode="chat_completions", quiet_mode=True,
        session_db=db, session_id=sid, skip_context_files=True, skip_memory=True,
    )
    # All source rows are already durable; real rotation must not re-append them.
    agent._persist_user_message_idx = len(rows)
    compressed = [{"role": "user", "content": "summary", "_compressed_summary": True}]
    if mode == "in_place_kept_origin":
        compressed.append(copy.deepcopy(rows[0]))
    outcome = cc._commit_compaction(
        agent, rows, compressed, in_place=mode != "rotated",
        lease=SimpleNamespace(holder=None, watermark=None, ttl=60),
        new_system_prompt="sys", system_message="sys", compressed_user_turn_outcome="none",
        messages_before_compression=copy.deepcopy(rows), made_progress=True,
        attempt=cc._Attempt(snapshot={}, generation=0, started_at=time.monotonic()),
    )
    assert outcome.session_commit_succeeded, outcome.split_status
    assert outcome.compacted_in_place is (mode != "rotated")
    if mode == "missing_raw_origin":
        db._execute_write(lambda conn: conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND role = 'user' AND display_metadata IS NOT NULL",
            (sid,),
        ))
    if mode == "rotated":
        assert agent.session_id != sid
        entry.session_id = agent.session_id
    event = SimpleNamespace(_gateway_active_turn_id=record["turn_id"])
    result = {"failed": True, "failure_retryable": True, "failure_reason": "timeout",
              "messages": compressed, "current_turn_user_idx": 1 if mode == "in_place_kept_origin" else None}
    await GatewayTurnMixin._hmwa_settle_retryable_turn(
        SimpleNamespace(async_session_store=AsyncSessionStore(store)), event=event,
        session_entry=entry, session_key=key, agent_result=result,
    )
    assert event._gateway_turn_recovery["status"] == BLOCKED
    reasons = {
        "in_place_kept_origin": "missing_turn_boundary",  # Archive + copied owner is ambiguous.
        "in_place_summary_only": "missing_turn_boundary",
        "rotated": "rotated_origin",
        "missing_raw_origin": "missing_turn_boundary",
    }
    assert event._gateway_turn_recovery["blocked_reason"] == reasons[mode]
    assert "checkpoint" not in entry.active_turn
    assert client.chat.completions.create.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("closers", [0, 1, 3])
async def test_m1_r1_normalization_does_not_shift_owner_projection(
    tmp_path, monkeypatch, _m1_replan_home, closers,
):
    from agent.turn_failure_copy import FAILED_TURN_NOTICE
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="projection-identity")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    entry = runner.session_store._entries[key]
    owner = entry.active_turn["origin_owner"]
    # Repeated text has a different owner; observed rows must not enter replay.
    store = runner.session_store
    store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "original task", "display_kind": "steer",
        "display_metadata": {"gateway_input_owner": "distinct-steer"},
    })
    store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "OBSERVED ONLY", "observed": True,
        "display_metadata": {"gateway_input_owner": "observed-owner"},
    })
    for _ in range(closers):
        store.append_to_transcript(entry.session_id, {"role": "assistant", "content": FAILED_TURN_NOTICE})
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response(RECOVERED_RESPONSE)
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    results = []
    real_run = runner._run_agent

    async def capture(*args, **kwargs):
        result = await real_run(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(runner, "_run_agent", capture)
    await adapter.handle_message(event)
    await _m1_replan_await_background_tasks(runner)
    assert client.chat.completions.create.call_count == 1
    assert adapter.sent.count(RECOVERED_RESPONSE) == 1
    assert len(results) == 1
    result = results[0]
    boundary = result["current_turn_user_idx"]
    assert result["messages"][boundary]["display_metadata"]["gateway_input_owner"] == owner
    assert sum((row.get("display_metadata") or {}).get("gateway_input_owner") == owner
               for row in result["messages"]) == 1
    assert all(row.get("content") != "OBSERVED ONLY" for row in result["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["sid", "turn", "count", "boot", "token", "suspended", "malformed"])
async def test_m1_r3_identity_mismatch_through_adapter(tmp_path, monkeypatch, _m1_replan_home, change):
    runner, adapter, _ = _m1_replan_runner(tmp_path / "runtime")
    source = make_restart_source(chat_id="adapter-identity")
    key, event = _m1_pi_claim_event(runner.session_store, source)
    entry = runner.session_store._entries[key]
    if change == "suspended":
        entry.suspended = True
    elif change == "malformed":
        event._hermes_turn_resume = None  # Attribute presence is not the legacy absence case.
    else:
        field = {"sid": "execution_session_id", "turn": "turn_id", "count": "resume_count",
                 "boot": "boot_id", "token": "dispatch_token"}[change]
        entry.active_turn = {**entry.active_turn, field: 17 if change == "count" else "replacement"}
    successor = dict(entry.active_turn)
    client = MagicMock()
    client.chat.completions.create.return_value = _provider_response("MUST NOT RUN")
    _m1_replan_patch_agent_runtime(monkeypatch, client)
    await adapter.handle_message(event)
    await _m1_replan_await_background_tasks(runner)
    assert client.chat.completions.create.call_count == 0
    assert adapter.sent == []
    assert entry.active_turn == successor
