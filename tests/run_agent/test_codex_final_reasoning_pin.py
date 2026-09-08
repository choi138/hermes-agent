"""Final Responses SDK bytes and app-server refusal, with no external I/O."""
import json
from collections import UserDict
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI, omit, NOT_GIVEN

from agent import codex_runtime, relay_llm
from agent.reasoning_pin import PinnedReasoningError
from agent.transports.codex import ResponsesApiTransport


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.socket.connect_ex", deny)
    monkeypatch.setattr("socket.create_connection", deny)
    monkeypatch.setattr("socket.getaddrinfo", deny)


def make_agent(selection="pinned", consumer=False):
    return SimpleNamespace(
        reasoning_config={"enabled": True, "effort": "max", "selection": selection},
        model="gpt-6-astra", provider="custom", api_mode="codex_responses",
        base_url="https://local.invalid/v1", _interrupt_requested=False,
        _current_api_request_id="offline", _fallback_index=0, is_subagent=False,
        session_id="", _is_codex_backend=lambda: consumer,
        _fire_stream_delta=Mock(), _fire_reasoning_delta=Mock(),
        _touch_activity=Mock(), _client_log_context=lambda: "offline",
    )


def payload(agent):
    return ResponsesApiTransport().build_kwargs(
        model=agent.model, messages=[], tools=[], reasoning_config=agent.reasoning_config,
        provider=agent.provider, base_url=agent.base_url)


def sse_response():
    events = [
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_text.delta", "delta": "ok", "output_index": 0,
         "content_index": 0, "item_id": "msg1"},
        {"type": "response.completed", "response": {"id": "r1", "object": "response",
         "status": "completed", "output": [], "usage": None}},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="".join("data: " + json.dumps(e) + "\n\n" for e in events))


def sdk_client(sent, responder=None):
    def capture(request):
        sent.append(json.loads(request.content))
        return responder() if responder else sse_response()
    return OpenAI(api_key="offline-only", base_url="https://local.invalid/v1",
                  max_retries=0, http_client=httpx.Client(
                      transport=httpx.MockTransport(capture), trust_env=False))


def late_relay(monkeypatch, mutate):
    """Keep production ManagedLlmStream, callbacks and runtime; mutate at its factory."""
    real_stream = relay_llm.stream
    attempts = []
    def stream(request, factory, **kwargs):
        def late_factory(next_request):
            changed = dict(next_request)
            attempts.append(changed)
            mutate(changed, len(attempts))
            return factory(changed)
        return real_stream(request, late_factory, **kwargs)
    monkeypatch.setattr(relay_llm, "stream", stream)
    return attempts


BAD_CHANGES = [
    pytest.param({"reasoning": {"effort": "high"}}, id="high"),
    pytest.param({"reasoning": {"summary": "auto"}}, id="removed-effort"),
    pytest.param({"reasoning": None}, id="none"),
    pytest.param({"reasoning": omit}, id="omit"),
    pytest.param({"reasoning": []}, id="list"),
    pytest.param({"reasoning": "max"}, id="string"),
    pytest.param({"reasoning": {"effort": None}}, id="none-effort"),
    pytest.param({"model": "gpt-5.5"}, id="unsupported-model"),
    pytest.param({"model": None}, id="none-model"),
    pytest.param({"model": omit}, id="omitted-model"),
]


@pytest.mark.parametrize("bypass", [True, False])
@pytest.mark.parametrize("consumer", [True, False])
@pytest.mark.parametrize("extra", [True, False])
@pytest.mark.parametrize("change", BAD_CHANGES)
def test_late_conflict_rejected_before_sdk(monkeypatch, bypass, consumer, extra, change):
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0" if bypass else "1")
    agent = make_agent(consumer=consumer)
    def mutate(request, attempt):
        request.update({"extra_body": change} if extra else change)
    attempts = late_relay(monkeypatch, mutate)
    sent = []
    with sdk_client(sent) as client:
        with pytest.raises(PinnedReasoningError):
            codex_runtime.run_codex_stream(agent, payload(agent), client=client)
    assert sent == []
    assert len(attempts) == 1  # Validation errors never enter connection retry.
    assert agent.reasoning_config["effort"] == "max"
    assert agent.model == "gpt-6-astra"


@pytest.mark.parametrize("bypass", [True, False])
@pytest.mark.parametrize("extra", [[], "bad", UserDict({"reasoning": {"effort": "high"}})])
def test_malformed_or_conflicting_mapping_cannot_be_normalized_away(monkeypatch, bypass, extra):
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0" if bypass else "1")
    late_relay(monkeypatch, lambda request, n: request.update(extra_body=extra))
    agent, sent = make_agent(), []
    with sdk_client(sent) as client:
        with pytest.raises(PinnedReasoningError):
            codex_runtime.run_codex_stream(agent, payload(agent), client=client)
    assert sent == []


@pytest.mark.parametrize("bypass", [True, False])
def test_late_removal_of_reasoning_rejected(monkeypatch, bypass):
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0" if bypass else "1")
    late_relay(monkeypatch, lambda request, n: request.pop("reasoning"))
    agent, sent = make_agent(), []
    with sdk_client(sent) as client:
        with pytest.raises(PinnedReasoningError):
            codex_runtime.run_codex_stream(agent, payload(agent), client=client)
    assert sent == []


@pytest.mark.parametrize("bypass", [True, False])
@pytest.mark.parametrize("consumer", [True, False])
@pytest.mark.parametrize("selection", ["pinned", "auto"])
def test_valid_wire_preserves_precedence_sanitation_and_first_frame(monkeypatch, bypass, consumer, selection):
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0" if bypass else "1")
    agent, sent = make_agent(selection, consumer), []
    effort = "max" if selection == "pinned" else "high"
    def mutate(request, n):
        request.update(reasoning={"effort": "high"}, model="gpt-5.5",
                       prompt_cache_retention="24h", extra_body={
            "model": "gpt-6-astra", "reasoning": {"effort": effort},
            "prompt_cache_retention": "24h", "input": "extra input wins", "tools": [],
        })
    late_relay(monkeypatch, mutate)
    first_delta = Mock()
    stamp = Mock()
    monkeypatch.setattr(codex_runtime, "_stamp_codex_first_frame", stamp)
    with sdk_client(sent) as client:
        final = codex_runtime.run_codex_stream(agent, payload(agent), client=client,
                                             on_first_delta=first_delta)
    assert final.status == "completed"
    assert len(sent) == 1
    assert sent[0]["reasoning"]["effort"] == effort
    assert sent[0]["model"] == "gpt-6-astra"
    assert sent[0]["input"] == "extra input wins" and sent[0]["tools"] == []
    assert ("prompt_cache_retention" in sent[0]) is (not consumer)
    assert stamp.call_count == 3
    first_delta.assert_called_once()
    agent._fire_stream_delta.assert_called_once_with("ok")


@pytest.mark.parametrize("extra", [None, omit, NOT_GIVEN, {}])
def test_absent_extra_keeps_exact_pin(monkeypatch, extra):
    late_relay(monkeypatch, lambda request, n: request.update(extra_body=extra))
    agent, sent = make_agent(), []
    with sdk_client(sent) as client:
        codex_runtime.run_codex_stream(agent, payload(agent), client=client)
    assert sent[0]["reasoning"]["effort"] == "max"


def test_post_bypass_body_is_checked_too(monkeypatch):
    # SDK accepts Mapping, but the existing bulk bypass preserves only dict.
    # Before normalization this effective request is max; afterwards it is high.
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0")
    late_relay(monkeypatch, lambda request, n: request.update(
        reasoning={"effort": "high"},
        extra_body=UserDict({"reasoning": {"effort": "max"}})))
    agent, sent = make_agent(), []
    with sdk_client(sent) as client:
        with pytest.raises(PinnedReasoningError):
            codex_runtime.run_codex_stream(agent, payload(agent), client=client)
    assert sent == []


@pytest.mark.parametrize("change", [{}, {"reasoning": {"effort": "high"}},
                                    {"extra_body": {"model": "gpt-5.5"}}])
def test_each_physical_retry_checks_its_own_final_request(monkeypatch, change):
    agent, sent = make_agent(), []
    attempts = late_relay(monkeypatch, lambda request, n: request.update(change if n == 2 else {}))
    class Disconnected(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.RemoteProtocolError("offline disconnect before first frame")
            yield b""  # Make iteration raise, rather than stream construction.
    def respond():
        if len(sent) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Disconnected())
        return sse_response()
    with sdk_client(sent, respond) as client:
        if change:
            with pytest.raises(PinnedReasoningError):
                codex_runtime.run_codex_stream(agent, payload(agent), client=client)
        else:
            assert codex_runtime.run_codex_stream(agent, payload(agent), client=client).status == "completed"
    assert len(attempts) == 2
    assert len(sent) == (1 if change else 2)
    assert all(body["reasoning"]["effort"] == "max" for body in sent)
    assert all(body["model"] == "gpt-6-astra" for body in sent)
    assert agent._fire_stream_delta.call_count == (0 if change else 1)


@pytest.mark.parametrize("extra", [[], "legacy", UserDict({"reasoning": {"effort": "high"}})])
def test_auto_keeps_existing_extra_normalization(monkeypatch, extra):
    monkeypatch.setenv("HERMES_CODEX_SDK_TRANSFORM", "0")
    late_relay(monkeypatch, lambda request, n: request.update(extra_body=extra))
    agent, sent = make_agent("auto"), []
    with sdk_client(sent) as client:
        assert codex_runtime.run_codex_stream(agent, payload(agent), client=client).status == "completed"
    assert sent[0]["reasoning"]["effort"] == "max"


@pytest.fixture
def managed_relay(monkeypatch):
    # Optional nemo_relay is absent in the shared venv. Supply only its host
    # interface; production ManagedLlmStream, request merging, provider callback,
    # async iteration, error propagation and SDK serialization all remain real.
    async def execute(name, request, callback, observe_chunk, finalizer, **kwargs):
        async def generate():
            async for chunk in callback(request):
                observe_chunk(chunk)
                yield chunk
            finalizer()
        return generate()
    async def run_in_session(session, fn, *args, **kwargs):
        return await fn(*args, **kwargs)
    relay = SimpleNamespace(
        LLMRequest=lambda headers, content: SimpleNamespace(headers=headers, content=content),
        llm=SimpleNamespace(stream_execute=execute))
    host = SimpleNamespace(relay=relay, managed_execution_enabled=lambda: True,
        acquire_operation_lease=lambda: Mock(), run_in_session_async=run_in_session)
    monkeypatch.setattr(relay_llm.relay_runtime, "resolve_execution_context",
        lambda session_id: (host, SimpleNamespace(session_id=session_id), None))
    return relay


@pytest.mark.parametrize("selection", ["pinned", "auto"])
@pytest.mark.parametrize("change", [{}, {"reasoning": {"effort": "high"}},
    {"extra_body": {"reasoning": {"effort": "high"}}}, {"model": "gpt-5.5"}])
def test_managed_relay_adapter_mutation(monkeypatch, managed_relay, selection, change):
    relay = managed_relay
    real_execute = relay.llm.stream_execute
    calls = []
    async def mutate(name, request, callback, *args, **kwargs):
        calls.append(request)
        changed = relay.LLMRequest({}, {**request.content, **change})
        return await real_execute(name, changed, callback, *args, **kwargs)
    monkeypatch.setattr(relay.llm, "stream_execute", mutate)
    agent, sent = make_agent(selection), []
    agent.session_id = "pin-boundary"
    rejected = selection == "pinned" and bool(change)
    with sdk_client(sent) as client:
        if rejected:
            with pytest.raises(PinnedReasoningError):
                codex_runtime.run_codex_stream(agent, payload(agent), client=client)
        else:
            assert codex_runtime.run_codex_stream(agent, payload(agent), client=client).status == "completed"
    assert len(calls) == 1
    assert len(sent) == (0 if rejected else 1)
    if sent:
        expected_effort = "high" if "reasoning" in change or "extra_body" in change else "max"
        assert sent[0]["reasoning"]["effort"] == expected_effort
        assert sent[0]["model"] == change.get("model", "gpt-6-astra")


@pytest.mark.parametrize("change", [{}, {"extra_body": {"reasoning": {"effort": "high"}}},
                                    {"model": "gpt-5.5"}])
def test_managed_relay_reconnect_rechecks_same_physical_factory(monkeypatch, managed_relay, change):
    relay = managed_relay
    real_execute = relay.llm.stream_execute
    physical_calls, logical_calls = [], []
    async def reconnect(name, request, callback, *args, **kwargs):
        logical_calls.append(request)
        async def provider(next_request):
            physical_calls.append(next_request)
            try:
                async for chunk in callback(next_request):
                    yield chunk
            except httpx.RemoteProtocolError:
                changed = relay.LLMRequest({}, {**next_request.content, **change})
                physical_calls.append(changed)
                async for chunk in callback(changed):
                    yield chunk
        return await real_execute(name, request, provider, *args, **kwargs)
    monkeypatch.setattr(relay.llm, "stream_execute", reconnect)
    agent, sent = make_agent(), []
    agent.session_id = "pin-boundary"
    class Disconnected(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.RemoteProtocolError("offline reconnect")
            yield b""
    def respond():
        if len(sent) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Disconnected())
        return sse_response()
    with sdk_client(sent, respond) as client:
        if change:
            with pytest.raises(PinnedReasoningError):
                codex_runtime.run_codex_stream(agent, payload(agent), client=client)
        else:
            assert codex_runtime.run_codex_stream(agent, payload(agent), client=client).status == "completed"
    assert len(logical_calls) == 1
    assert len(physical_calls) == 2
    assert len(sent) == (1 if change else 2)
    assert all(body["reasoning"]["effort"] == "max" for body in sent)
    assert all(body["model"] == "gpt-6-astra" for body in sent)


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("selection", ["pinned", "auto"])
def test_app_server_pin_refused_before_session_side_effects(monkeypatch, tmp_path, cached, selection):
    class ReachedTurn(BaseException):
        pass
    session = Mock()
    session.run_turn.side_effect = ReachedTurn
    constructor = Mock(return_value=session)
    monkeypatch.setattr("agent.transports.codex_app_server_session.CodexAppServerSession", constructor)
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None)
    monkeypatch.setattr("tools.approval.is_approval_bypass_active", lambda: False)
    agent = make_agent(selection)
    agent.api_mode = "codex_app_server"
    agent.session_cwd = str(tmp_path)
    agent._codex_session = session if cached else None
    expected = PinnedReasoningError if selection == "pinned" else ReachedTurn
    with pytest.raises(expected):
        codex_runtime.run_codex_app_server_turn(agent, user_message="offline",
            original_user_message="offline", messages=[], effective_task_id="offline")
    assert constructor.call_count == (0 if cached or selection == "pinned" else 1)
    assert session.run_turn.call_count == (0 if selection == "pinned" else 1)
    session.close.assert_not_called()
    if selection == "auto":
        session.run_turn.assert_called_once_with(user_input="offline")
    else:
        assert agent._codex_session is (session if cached else None)
