"""Caller-owned http_client (no Hermes attempt factory) keeps the caller's transport policy."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from openai import OpenAI

from agent.auxiliary_client import _CodexCompletionsAdapter


def _frame(kind: str, data: str) -> bytes:
    return f"event: {kind}\ndata: {data}\n\n".encode()


_COMPLETED = (
    _frame("response.output_text.delta", '{"type":"response.output_text.delta","delta":"ok"}')
    + _frame("response.completed", '{"type":"response.completed","response":{"status":"completed","id":"done"}}')
)


class _SigningAuth(httpx.Auth):
    """Stands in for caller auth that lives on the http_client (e.g. Bedrock SigV4)."""

    def auth_flow(self, request):
        request.headers["X-Caller-Signature"] = "signed"
        yield request


def test_caller_owned_http_client_auth_and_transport_are_used(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_COMPLETED)

    monkeypatch.setattr(
        "agent.process_bootstrap.build_keepalive_http_client",
        lambda *_a, **_k: pytest.fail("caller-owned http_client was replaced by a Hermes default transport"))
    caller = OpenAI(api_key="aws-sdk", base_url="https://example.test/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler), auth=_SigningAuth()))
    try:
        out = _CodexCompletionsAdapter(caller, "m").create(
            model="m", messages=[{"role": "user", "content": "synthetic"}], timeout=5)
        assert out.choices[0].message.content == "ok"
        assert len(seen) == 1
        assert seen[0].headers.get("X-Caller-Signature") == "signed"
        # The caller's client stays usable for the next request.
        _CodexCompletionsAdapter(caller, "m").create(
            model="m", messages=[{"role": "user", "content": "synthetic"}], timeout=5)
        assert len(seen) == 2
    finally:
        caller.close()


class _Stream(httpx.SyncByteStream):
    def __init__(self, role: str, sibling_started: threading.Event, sibling_release: threading.Event):
        self.role = role
        self.sibling_started = sibling_started
        self.sibling_release = sibling_release
        self.closed = threading.Event()

    def __iter__(self):
        if self.role == "A":
            yield _frame("response.in_progress", '{"type":"response.in_progress"}')
            self.closed.wait(2)
            raise httpx.ReadError("timed-out stream closed")
        if self.role == "B":
            self.sibling_started.set()
            yield _frame("response.output_text.delta", '{"type":"response.output_text.delta","delta":"B first "}')
            self.sibling_release.wait(5)
            if self.closed.is_set():
                return
            payload = "B final"
        else:
            payload = "C final"
        yield _frame("response.output_text.delta", '{"type":"response.output_text.delta","delta":"' + payload + '"}')
        yield _frame("response.completed", '{"type":"response.completed","response":{"status":"completed","id":"done"}}')

    def close(self):
        self.closed.set()


def test_caller_owned_timeout_never_closes_the_shared_client(monkeypatch):
    sibling_started = threading.Event()
    sibling_release = threading.Event()
    transport_closed = threading.Event()

    class _Transport(httpx.BaseTransport):
        def handle_request(self, request):
            if transport_closed.is_set():
                raise httpx.ConnectError("transport closed", request=request)
            import json
            role = json.loads(request.content)["model"]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Stream(role, sibling_started, sibling_release))

        def close(self):
            transport_closed.set()

    monkeypatch.setattr(
        "agent.process_bootstrap.build_keepalive_http_client",
        lambda *_a, **_k: pytest.fail("caller-owned http_client was replaced by a Hermes default transport"))
    caller = OpenAI(api_key="aws-sdk", base_url="https://example.test/v1", max_retries=0,
                    http_client=httpx.Client(transport=_Transport()))
    adapter = _CodexCompletionsAdapter(caller, "B")
    result: dict[str, object] = {}

    def call(role: str, timeout: float):
        return adapter.create(model=role, messages=[{"role": "user", "content": "synthetic"}],
                              timeout=timeout, no_progress_timeout=timeout)

    def sibling():
        try:
            result["B"] = call("B", 5)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            result["B"] = exc

    worker = threading.Thread(target=sibling)
    worker.start()
    try:
        assert sibling_started.wait(2)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            call("A", 0.12)
        assert time.monotonic() - started < 1.5
        sibling_release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not isinstance(result["B"], BaseException), result["B"]
        assert result["B"].choices[0].message.content == "B first B final"
        assert not transport_closed.is_set()
        assert call("C", 5).choices[0].message.content == "C final"
    finally:
        sibling_release.set()
        caller.close()


def test_explicit_factory_never_replaces_a_caller_supplied_http_client():
    """A caller's own http_client wins over any factory: attempts must reuse it as-is."""
    from agent.auxiliary_client import _create_openai_client

    built = []

    def factory():
        built.append(httpx.Client())
        return built[-1]

    caller = httpx.Client(headers={"X-Caller-Policy": "kept"})
    client = _create_openai_client(api_key="synthetic-key", base_url="https://example.test/v1",
                                   http_client=caller, http_client_factory=factory)
    try:
        assert getattr(client, "_hermes_attempt_http_client_factory", None) is None
        attempt = _CodexCompletionsAdapter(client, "m")._attempt_client()
        assert attempt is client
        assert attempt._client is caller
        assert built == []
    finally:
        client.close()
        for extra in built:
            extra.close()


@pytest.mark.parametrize("failure", ["returns_none", "skewed_signature"])
def test_unbuildable_private_transport_falls_back_to_the_existing_client(monkeypatch, failure):
    """Base serves on the SDK pool when the Hermes seam cannot build a transport; so must we."""
    from agent.auxiliary_client import _create_openai_client

    if failure == "returns_none":
        builder = lambda *_a, **_k: None  # noqa: E731 - bad CA/proxy path inside the seam
    else:
        def builder(base_url, *, async_mode=False, verify=None):  # pre-private_transport signature
            return None
    monkeypatch.setattr("agent.process_bootstrap.build_keepalive_http_client", builder)
    client = _create_openai_client(api_key="synthetic-key", base_url="https://chatgpt.com/backend-api/codex")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_COMPLETED)

    client._client._transport = httpx.MockTransport(handler)
    client._client._mounts = {}
    try:
        adapter = _CodexCompletionsAdapter(client, "gpt-5")
        assert adapter._attempt_client() is client
        out = adapter.create(model="gpt-5", messages=[{"role": "user", "content": "synthetic"}], timeout=5)
        assert out.choices[0].message.content == "ok"
        assert len(seen) == 1
        # The shared client was not closed by the finished attempt and keeps serving.
        assert not client._client.is_closed
        adapter.create(model="gpt-5", messages=[{"role": "user", "content": "synthetic"}], timeout=5)
        assert len(seen) == 2
    finally:
        client.close()
