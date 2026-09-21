"""Request isolation and compression recovery through real SDK/HTTP boundaries."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import yaml
from openai import OpenAI

from agent import auxiliary_client as aux


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    state = SimpleNamespace(
        requests=[], primary="completed", backup="completed", silent_started=threading.Event(),
        live_started=threading.Event(), release=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = request["model"]
            state.requests.append(model)
            if model == "no-headers":
                state.silent_started.set()
                state.release.wait(5)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def emit(event):
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

            try:
                emit({"type": "response.created", "response": {"id": "test"}})
                if model == "silent":
                    state.silent_started.set()
                    state.release.wait(5)
                    return
                if model == "live":
                    state.live_started.set()
                    while not state.release.wait(0.03):
                        emit({"type": "response.output_text.delta", "delta": "live "})
                status = state.primary if model == "primary" else state.backup
                content = "   " if status == "empty" else "complete summary"
                if status == "tools":
                    output = [{"type": "function_call", "call_id": "call-1",
                               "name": "read_file", "arguments": "{}"}]
                    status = "completed"
                else:
                    output = [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": content}]}]
                if status == "empty":
                    status = "completed"
                response = {"id": "test", "status": status, "output": output}
                if status == "failed":
                    response["error"] = {"code": "upstream_failed", "message": "provider failed"}
                if status == "incomplete":
                    response["incomplete_details"] = {"reason": "max_output_tokens"}
                for index, item in enumerate(output):
                    emit({"type": "response.output_item.done", "output_index": index, "item": item})
                emit({"type": "response." + status, "response": response})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model": {"provider": "custom", "default": "primary", "base_url": state.url,
                  "api_key": "test-only", "api_mode": "codex_responses"},
        "auxiliary": {"transient_retries": 1, "compression": {
            "provider": "custom", "model": "primary", "base_url": state.url,
            "api_key": "test-only", "api_mode": "codex_responses",
            "fallback_chain": [{"provider": "custom", "model": "backup",
                                "base_url": state.url, "api_key": "test-only",
                                "api_mode": "codex_responses"}]}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    aux.shutdown_cached_clients()
    yield state
    state.release.set()
    aux.shutdown_cached_clients()
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_terminal_failure_keeps_provider_details(endpoint, status):
    endpoint.primary = status
    with OpenAI(api_key="test-only", base_url=endpoint.url, max_retries=0) as client:
        adapter = aux._CodexCompletionsAdapter(client, "primary")
        with pytest.raises(RuntimeError) as caught:
            # Metadata validation must allow cold SDK imports on slower hosts.
            # The request-isolation tests below enforce the short time bounds.
            adapter.create(messages=[{"role": "user", "content": "summarize"}], timeout=10)
    assert caught.value.status == status
    assert caught.value.model == "primary"
    if status == "failed":
        assert aux._event_field(caught.value.error, "code") == "upstream_failed"
    else:
        assert aux._event_field(caught.value.incomplete_details, "reason") == "max_output_tokens"
    assert aux._is_invalid_aux_response_error(caught.value)


@pytest.mark.parametrize("status", ["completed", "tools"])
def test_successful_codex_responses_keep_text_and_tools(endpoint, status):
    endpoint.primary = status
    with OpenAI(api_key="test-only", base_url=endpoint.url, max_retries=0) as client:
        response = aux._CodexCompletionsAdapter(client, "primary").create(messages=[], timeout=2)
    choice = response.choices[0]
    if status == "tools":
        assert choice.finish_reason == "tool_calls"
        assert choice.message.tool_calls[0].function.name == "read_file"
    else:
        assert choice.finish_reason == "stop"
        assert choice.message.content == "complete summary"


@pytest.mark.parametrize("status", ["failed", "incomplete", "empty"])
def test_compression_uses_configured_sibling_fallback(endpoint, status):
    endpoint.primary = status
    route = {}
    response = aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}],
                            route_info=route, timeout=2)
    assert response.choices[0].message.content == "complete summary"
    assert endpoint.requests == ["primary", "backup"]
    assert route["model"] == "backup"


@pytest.mark.parametrize("failed_model", ["silent", "no-headers"])
def test_timeout_is_request_scoped_and_bounded(endpoint, monkeypatch, failed_model):
    monkeypatch.setattr(aux, "_AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.3)
    original_http = aux._openai_http_client_kwargs
    releases = []

    def recording_http(*args, **kwargs):
        result = original_http(*args, **kwargs)
        transport = result["http_client"]
        owner = threading.get_ident()
        close = transport.close

        def record_close():
            releases.append((owner, threading.get_ident()))
            close()

        transport.close = record_close
        return result

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", recording_http)
    with OpenAI(api_key="test-only", base_url=endpoint.url, max_retries=0) as client:
        adapter = aux._CodexCompletionsAdapter(client, "primary")
        with ThreadPoolExecutor(max_workers=2) as pool:
            live = pool.submit(adapter.create, model="live", messages=[], timeout=3)
            try:
                assert endpoint.live_started.wait(2)
                failed = pool.submit(adapter.create, model=failed_model, messages=[], timeout=3)
                assert endpoint.silent_started.wait(2)
                # A generous bound distinguishes prompt isolation from the full SDK timeout.
                with pytest.raises(Exception) as caught:
                    failed.result(timeout=1.5)
                assert failed.done(), "timed-out request is still running"
                assert aux._is_timeout_error(caught.value)
                assert not client.is_closed(), "request timeout closed the shared client"
            finally:
                endpoint.release.set()
            assert live.result(timeout=2).choices[0].message.content == "complete summary"
        assert adapter.create(model="backup", messages=[], timeout=2).choices[0].message.content
    assert len(releases) == 3
    assert all(owner == closer for owner, closer in releases), "a watchdog released transport FDs"


def test_retry_reacquires_a_client_closed_by_failed_attempt(endpoint, monkeypatch):
    original_create = aux._create_with_progress
    used = []

    def close_first(client, *args, **kwargs):
        used.append(client)
        if len(used) == 1:
            client._real_client.close()
            raise ConnectionError("peer closed connection")
        assert client is not used[0], "retry reused the closed local client"
        assert not client._real_client.is_closed()
        return original_create(client, *args, **kwargs)

    monkeypatch.setattr(aux, "_create_with_progress", close_first)
    monkeypatch.setattr(aux, "_TRANSIENT_RETRY_BACKOFF_BASE", 0)
    response = aux.call_llm(task="compression", messages=[{"role": "user", "content": "summarize"}], timeout=2)
    assert response.choices[0].message.content == "complete summary"
    assert len(used) == 2
    assert endpoint.requests == ["primary"]


def test_explicit_cancel_and_late_watchdog_leave_live_request_running(endpoint, monkeypatch):
    monkeypatch.setattr(aux, "_AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.3)
    original_http = aux._openai_http_client_kwargs
    cancelled_transport_closed = threading.Event()
    cancel = threading.Event()

    def recording_http(*args, **kwargs):
        result = original_http(*args, **kwargs)
        if threading.current_thread().name == "hermes-protected-aux-provider":
            close = result["http_client"].close

            def record_close():
                close()
                cancelled_transport_closed.set()

            result["http_client"].close = record_close
        return result

    monkeypatch.setattr(aux, "_openai_http_client_kwargs", recording_http)
    with OpenAI(api_key="test-only", base_url=endpoint.url, max_retries=0) as client:
        wrapper = aux.CodexAuxiliaryClient(client, "primary")

        def cancellable():
            with aux.aux_interrupt_protection(cancel_event=cancel):
                return aux._relay_sync_completion(wrapper, {"model": "silent", "messages": [], "timeout": 3})

        with ThreadPoolExecutor(max_workers=2) as pool:
            live = pool.submit(wrapper.chat.completions.create, model="live", messages=[], timeout=3)
            try:
                assert endpoint.live_started.wait(2)
                cancelled = pool.submit(cancellable)
                assert endpoint.silent_started.wait(2)
                cancel.set()
                with pytest.raises(aux.AuxiliaryExplicitCancellation):
                    cancelled.result(timeout=1)
                # The cancelled owner has returned, but its watchdog still has
                # to wake and release the isolated provider transport safely.
                assert cancelled_transport_closed.wait(2)
                assert not client.is_closed()
            finally:
                endpoint.release.set()
            assert live.result(timeout=2).choices[0].message.content == "complete summary"


@pytest.mark.asyncio
async def test_async_compression_uses_the_same_failure_fallback(endpoint):
    endpoint.primary = "incomplete"
    route = {}
    response = await aux.async_call_llm(
        task="compression", messages=[{"role": "user", "content": "summarize"}],
        route_info=route, timeout=2,
    )
    assert response.choices[0].message.content == "complete summary"
    assert endpoint.requests == ["primary", "backup"]
    assert route["model"] == "backup"


@pytest.mark.parametrize("status", ["failed", "incomplete", "empty"])
def test_exhausted_fallback_preserves_context_and_reports_actual_model(endpoint, status):
    from agent.context_compressor import ContextCompressor

    endpoint.primary = endpoint.backup = status
    compressor = ContextCompressor(
        model="primary", provider="custom", base_url=endpoint.url,
        api_key="test-only", api_mode="codex_responses", config_context_length=100000,
        quiet_mode=True, protect_first_n=2, protect_last_n=2,
    )
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " * 20}
                for i in range(12)]
    before = [dict(message) for message in messages]
    result = compressor.compress(messages, current_tokens=999999, force=True)
    assert result == before
    assert messages == before
    assert compressor._last_compress_aborted
    assert not compressor._previous_summary
    assert "backup" in compressor._last_summary_error
    assert endpoint.requests == ["primary", "backup"]


def test_compression_acceptance_checks_recovered_text_and_reasoning():
    recovered_empty = SimpleNamespace(output_text="<think>internal only</think>")
    with pytest.raises(aux.AuxiliaryResponseError, match="empty content"):
        aux._validate_llm_response(recovered_empty, "compression")
    recovered_partial = SimpleNamespace(output_text="partial summary", finish_reason="length")
    with pytest.raises(aux.AuxiliaryResponseError, match="finish_reason=length"):
        aux._validate_llm_response(recovered_partial, "compression")
    reasoning = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="", reasoning_content="complete summary"))])
    assert aux._validate_llm_response(reasoning, "compression") is reasoning
    tool_only = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=None, tool_calls=[{"id": "call-1"}]))])
    assert aux._validate_llm_response(tool_only, "vision") is tool_only


def test_compressor_empty_guard_names_the_resolved_auxiliary_route(monkeypatch):
    from agent.context_compressor import ContextCompressor

    def empty_summary(**kwargs):
        kwargs["route_info"].update(provider="actual-provider", model="actual-summary-model")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

    monkeypatch.setattr("agent.context_compressor.call_llm", empty_summary)
    compressor = ContextCompressor(model="main-model", config_context_length=100000, quiet_mode=True)
    assert compressor._generate_summary([{"role": "user", "content": "keep this"}]) is None
    assert "provider=actual-provider" in compressor._last_summary_error
    assert "model=actual-summary-model" in compressor._last_summary_error
