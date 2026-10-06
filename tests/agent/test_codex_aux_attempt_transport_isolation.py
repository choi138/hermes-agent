"""A timed-out Codex auxiliary request cannot damage a sibling's transport."""

from __future__ import annotations

import threading
import time
import ssl
from types import MethodType
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from agent.auxiliary_client import _CodexCompletionsAdapter


def _hermes_cached(**kwargs) -> OpenAI:
    """Stand-in for a Hermes-built shared client: carries the private-transport factory that
    ``_create_openai_client`` installs, resolved at call time so tests can patch the builder."""
    client = OpenAI(**kwargs)

    def factory():
        from agent import process_bootstrap
        return process_bootstrap.build_keepalive_http_client(str(client.base_url), private_transport=True)

    client._hermes_attempt_http_client_factory = factory
    return client


def _frame(kind: str, data: str) -> bytes:
    return f"event: {kind}\ndata: {data}\n\n".encode()


class _Events(httpx.SyncByteStream):
    def __init__(self, role: str, transport: "_Transport", sibling_started: threading.Event,
                 sibling_release: threading.Event):
        self.role = role
        self.transport = transport
        self.sibling_started = sibling_started
        self.sibling_release = sibling_release
        self.closed = threading.Event()

    def __iter__(self):
        if self.role == "A":
            yield _frame("response.in_progress", '{"type":"response.in_progress"}')
            self.closed.wait(0.5)
            raise httpx.ReadError("timed-out stream closed")
        if self.role == "B":
            self.sibling_started.set()
            yield _frame("response.output_text.delta", '{"type":"response.output_text.delta","delta":"B first "}')
            self.sibling_release.wait(5)
            if self.transport.closed.is_set():
                return
            payload = "B final"
        else:
            payload = "C final"
        yield _frame("response.output_text.delta", '{"type":"response.output_text.delta","delta":"' + payload + '"}')
        yield _frame("response.completed", '{"type":"response.completed","response":{"status":"completed","id":"done"}}')

    def close(self):
        self.closed.set()


class _Transport(httpx.BaseTransport):
    def __init__(self, sibling_started: threading.Event, sibling_release: threading.Event):
        self.closed = threading.Event()
        self.sibling_started = sibling_started
        self.sibling_release = sibling_release
        self.streams: list[_Events] = []
        self.headers: list[httpx.Headers] = []
        self._pool = SimpleNamespace(_connections=[])

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.closed.is_set():
            raise httpx.ConnectError("transport closed", request=request)
        self.headers.append(request.headers)
        import json
        role = json.loads(request.content)["model"]
        stream = _Events(role, self, self.sibling_started, self.sibling_release)
        self.streams.append(stream)

        class _Socket:
            def settimeout(self, _timeout):
                pass

            def shutdown(self, _how):
                stream.close()

        class _Connection:
            def __init__(self):
                self._connection = self
                self._network_stream = self

            def get_extra_info(self, name):
                return _Socket() if name == "socket" else None

        self._pool._connections.append(_Connection())
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    def close(self):
        self.closed.set()
        for stream in self.streams:
            stream.close()


@pytest.mark.parametrize("with_timeout", [False, True])
def test_timeout_preserves_sibling_and_next_request(monkeypatch, with_timeout):
    sibling_started = threading.Event()
    sibling_release = threading.Event()
    transports: list[_Transport] = []

    def private_http_client(*_args, **_kwargs):
        transport = _Transport(sibling_started, sibling_release)
        transports.append(transport)
        return httpx.Client(transport=transport)

    # The production attempt factory must request a fresh underlying transport.
    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client", private_http_client)
    cached_transport = _Transport(sibling_started, sibling_release)
    cached = _hermes_cached(api_key="synthetic-key", base_url="https://example.test/codex",
                    http_client=httpx.Client(transport=cached_transport), max_retries=0,
                    default_headers={"X-Synthetic": "preserved"})
    adapter = _CodexCompletionsAdapter(cached, "B")
    result: dict[str, object] = {}

    def call(role: str, timeout: float):
        return adapter.create(model=role, messages=[{"role": "user", "content": "synthetic"}],
                              timeout=timeout, no_progress_timeout=timeout)

    def sibling():
        try:
            result["B"] = call("B", 5)
        except BaseException as exc:
            result["B"] = exc

    worker = threading.Thread(target=sibling)
    worker.start()
    try:
        assert sibling_started.wait(2)
        if with_timeout:
            timeout_started = time.monotonic()
            with pytest.raises(TimeoutError):
                call("A", 0.12)
            assert time.monotonic() - timeout_started < 1
        sibling_release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not isinstance(result["B"], BaseException)
        assert result["B"].choices[0].message.content == "B first B final"
        assert call("C", 5).choices[0].message.content == "C final"
        if with_timeout:
            assert len(transports) >= 3
            assert len({id(transport) for transport in transports}) == len(transports)
            assert all(headers["x-synthetic"] == "preserved"
                       for transport in transports for headers in transport.headers)
            assert all(headers["authorization"] == "Bearer synthetic-key"
                       for transport in transports for headers in transport.headers)
    finally:
        sibling_release.set()
        worker.join(5)
        cached.close()


def test_private_httpx_attempts_do_not_share_underlying_pools(monkeypatch):
    from agent import process_bootstrap

    monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url", lambda _url: None)
    clients = [process_bootstrap.build_keepalive_http_client(
        "https://example.test/codex", private_transport=True) for _ in range(2)]
    try:
        assert all(client is not None for client in clients)
        pools = []
        for client in clients:
            transports = [client._transport, *client._mounts.values()]
            pools.append({id(transport._pool) for transport in transports
                          if transport is not None and hasattr(transport, "_pool")})
        assert all(pools)
        assert pools[0].isdisjoint(pools[1])
    finally:
        for client in clients:
            if client is not None:
                client.close()


def test_attempts_reuse_configured_http_factory_options_without_sharing_pools():
    from agent.auxiliary_client import _create_openai_client

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    clients = []

    def http_factory():
        client = httpx.Client(
            proxy="http://synthetic-proxy.invalid:3128", verify=context,
            headers={"X-HTTP-Required": "needed"}, trust_env=False,
        )
        clients.append(client)
        return client

    cached = _create_openai_client(
        api_key="synthetic-key", base_url="https://example.test/codex",
        http_client_factory=http_factory, default_headers={"X-SDK-Required": "sdk"},
    )
    adapter = _CodexCompletionsAdapter(cached, "synthetic-model")
    attempts = [adapter._attempt_client() for _ in range(2)]
    try:
        assert len(clients) == 3
        assert all(client.headers["X-HTTP-Required"] == "needed" for client in clients)
        proxy_pools = [next(transport._pool for transport in client._mounts.values()
                            if transport is not None) for client in clients]
        assert all(type(pool).__name__ == "HTTPProxy" for pool in proxy_pools)
        assert all(pool._ssl_context is context for pool in proxy_pools)
        assert len({id(pool) for pool in proxy_pools}) == 3
        assert all(attempt.default_headers["X-SDK-Required"] == "sdk" for attempt in attempts)
        assert all(attempt.api_key == "synthetic-key" for attempt in attempts)
        requests = []

        def proxy_response(_pool, request):
            import httpcore
            requests.append(request)
            return httpcore.Response(200, headers=[(b"content-type", b"application/json")],
                                     content=(b'{"id":"synthetic","object":"chat.completion",'
                                              b'"created":0,"model":"synthetic-model",'
                                              b'"choices":[{"index":0,"message":{"role":"assistant",'
                                              b'"content":"ok"},"finish_reason":"stop"}]}'))

        proxy_pools[1].handle_request = MethodType(proxy_response, proxy_pools[1])
        assert attempts[0].chat.completions.create(
            model="synthetic-model", messages=[{"role": "user", "content": "synthetic"}],
        ).choices[0].message.content == "ok"
        assert len(requests) == 1
        headers = httpx.Headers(requests[0].headers)
        assert headers["x-http-required"] == "needed"
        assert headers["x-sdk-required"] == "sdk"
        assert headers["authorization"] == "Bearer synthetic-key"
    finally:
        for attempt in attempts:
            attempt.close()
        cached.close()


def test_production_attempt_factory_keeps_resolved_proxy_and_tls(monkeypatch):
    from agent import auxiliary_client as aux, process_bootstrap

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(aux, "_resolve_aux_verify", lambda _base_url: context)
    monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url",
                        lambda _base_url: "http://synthetic-proxy.invalid:3128")
    cached = aux._create_openai_client(api_key="synthetic-key", base_url="https://example.test/codex")
    monkeypatch.setattr(process_bootstrap, "_get_proxy_for_base_url", lambda _base_url: None)
    attempts = [_CodexCompletionsAdapter(cached, "synthetic-model")._attempt_client()
                for _ in range(2)]
    assert all(attempt is not cached for attempt in attempts)
    try:
        pools = [next(transport._pool for transport in client._mounts.values()
                      if transport is not None)
                 for client in [cached._client, *(attempt._client for attempt in attempts)]]
        assert all(type(pool).__name__ == "HTTPProxy" for pool in pools)
        assert all(pool._ssl_context is context for pool in pools)
        assert len({id(pool) for pool in pools}) == 3
    finally:
        for attempt in attempts:
            attempt.close()
        cached.close()


def test_late_pre_stream_response_is_closed_and_never_committed(monkeypatch):
    created: list[_Events] = []
    transports: list[_Transport] = []

    class _LateTransport(_Transport):
        def handle_request(self, request):
            time.sleep(0.22)  # local synthetic handler returns after the watchdog expires
            response = super().handle_request(request)
            created.append(self.streams[-1])
            return response

    def private_http_client(*_args, **_kwargs):
        transport = _LateTransport(threading.Event(), threading.Event())
        transports.append(transport)
        return httpx.Client(transport=transport)

    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client", private_http_client)
    cached = _hermes_cached(api_key="synthetic-key", base_url="https://example.test/codex",
                    http_client=httpx.Client(transport=httpx.MockTransport(
                        lambda _request: pytest.fail("cached transport was used"))), max_retries=0)
    try:
        with pytest.raises(TimeoutError):
            _CodexCompletionsAdapter(cached, "C").create(
                model="C", messages=[{"role": "user", "content": "synthetic"}],
                timeout=0.08, no_progress_timeout=0.08)
        assert created and created[0].closed.is_set()
        assert transports[0].closed.is_set()
    finally:
        cached.close()


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        # Content but no terminal frame: the shared consumer settles it as completed (base behavior).
        ([("response.output_text.delta", '{"type":"response.output_text.delta","delta":"partial"}')], "partial"),
        # Content then an incomplete/failed terminal: handled exactly as base all-work does.
        ([("response.output_text.delta", '{"type":"response.output_text.delta","delta":"cut"}'),
          ("response.incomplete", '{"type":"response.incomplete","response":{"status":"incomplete","id":"x",'
                                  '"incomplete_details":{"reason":"max_output_tokens"}}}')], "cut"),
    ],
)
def test_terminal_frame_handling_matches_base(monkeypatch, frames, expected):
    payload = b"".join(_frame(kind, data) for kind, data in frames)
    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client",
                        lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                            content=payload))))
    cached = _hermes_cached(api_key="synthetic-key", base_url="https://example.test/codex",
                            http_client=httpx.Client(transport=httpx.MockTransport(
                                lambda _request: pytest.fail("cached transport was used"))), max_retries=0)
    try:
        out = _CodexCompletionsAdapter(cached, "C").create(
            model="C", messages=[{"role": "user", "content": "synthetic"}], timeout=1)
        assert out.choices[0].message.content == expected
    finally:
        cached.close()


def test_stream_without_content_or_terminal_still_fails(monkeypatch):
    payload = _frame("response.in_progress", '{"type":"response.in_progress"}')
    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client",
                        lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                            content=payload))))
    cached = _hermes_cached(api_key="synthetic-key", base_url="https://example.test/codex",
                            http_client=httpx.Client(transport=httpx.MockTransport(
                                lambda _request: pytest.fail("cached transport was used"))), max_retries=0)
    try:
        with pytest.raises(RuntimeError):
            _CodexCompletionsAdapter(cached, "C").create(
                model="C", messages=[{"role": "user", "content": "synthetic"}], timeout=1)
    finally:
        cached.close()


def test_watchdog_winning_during_finalization_cannot_return_success(monkeypatch):
    from agent import auxiliary_client as aux

    completed = _frame("response.output_text.delta",
                       '{"type":"response.output_text.delta","delta":"complete"}') + _frame(
                           "response.completed",
                           '{"type":"response.completed","response":{"status":"completed","id":"done"}}')
    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client",
                        lambda *_args, **_kwargs: httpx.Client(transport=httpx.MockTransport(
                            lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                            content=completed))))
    original_parse = aux._parse_codex_final_response

    def slow_finalization(final):
        time.sleep(0.22)
        return original_parse(final)

    monkeypatch.setattr(aux, "_parse_codex_final_response", slow_finalization)
    cached = _hermes_cached(api_key="synthetic-key", base_url="https://example.test/codex",
                    http_client=httpx.Client(transport=httpx.MockTransport(
                        lambda _request: pytest.fail("cached transport was used"))), max_retries=0)
    try:
        with pytest.raises(TimeoutError):
            _CodexCompletionsAdapter(cached, "C").create(
                model="C", messages=[{"role": "user", "content": "synthetic"}],
                timeout=0.08, no_progress_timeout=0.08)
    finally:
        cached.close()
