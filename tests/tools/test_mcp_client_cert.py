"""Tests for mTLS client certificate config on MCP HTTP/SSE transports.

Covers:

1. ``_resolve_client_cert`` helper — string, tuple, encrypted-key, validation
   errors, missing-file errors.

2. HTTP (new SDK ``streamable_http_client``) path forwards ``cert=`` into the
   user-owned ``httpx.AsyncClient``.

3. SSE path forwards ``cert`` and ``ssl_verify`` via an ``httpx_client_factory``
   without breaking the OAuth/headers/timeout passthrough.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _resolve_client_cert helper
# ---------------------------------------------------------------------------


class TestResolveClientCert:
    def test_returns_none_when_unset(self):
        from tools.mcp_tool import _resolve_client_cert

        assert _resolve_client_cert("srv", {}) is None
        assert _resolve_client_cert("srv", {"url": "https://x"}) is None

    def test_string_form_single_pem(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        pem = tmp_path / "combined.pem"
        pem.write_text("dummy")

        result = _resolve_client_cert("srv", {"client_cert": str(pem)})
        assert result == str(pem)


    def test_list_form_two_elements(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        result = _resolve_client_cert("srv", {
            "client_cert": [str(cert), str(key)],
        })
        assert result == (str(cert), str(key))


    def test_password_must_be_string(self, tmp_path):
        from tools.mcp_tool import _resolve_client_cert

        cert = tmp_path / "client.crt"
        key = tmp_path / "client.key"
        cert.write_text("cert")
        key.write_text("key")

        with pytest.raises(ValueError, match=r"key passphrase.*must be a string"):
            _resolve_client_cert("srv", {
                "client_cert": [str(cert), str(key), 42],
            })


# ---------------------------------------------------------------------------
# HTTP transport — cert forwarded into httpx.AsyncClient
# ---------------------------------------------------------------------------


class TestHTTPClientCert:
    def test_cert_forwarded_to_async_client(self, tmp_path):
        """When client_cert is set, the new-SDK HTTP path passes ``cert=``
        into ``httpx.AsyncClient``."""
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.pem"
        cert.write_text("dummy")

        server = MCPServerTask("remote")
        captured: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class DummyTransportCtx:
            async def __aenter__(self):
                return MagicMock(), MagicMock(), (lambda: None)

            async def __aexit__(self, *a):
                return False

        class DummySession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def initialize(self):
                return None

        async def _discover_tools(self):
            self._shutdown_event.set()

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("tools.mcp_tool._MCP_NEW_HTTP", True), \
                 patch("httpx.AsyncClient", DummyAsyncClient), \
                 patch("tools.mcp_tool.streamable_http_client",
                       return_value=DummyTransportCtx()), \
                 patch("tools.mcp_tool.ClientSession", DummySession), \
                 patch.object(MCPServerTask, "_discover_tools", _discover_tools):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": str(cert),
                })

        asyncio.run(_drive())
        assert captured.get("cert") == str(cert)


    def test_missing_cert_file_surfaces_clear_error(self, tmp_path):
        """A missing cert file fails fast with a server-scoped error message."""
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("remote")

        async def _drive():
            with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
                 patch("tools.mcp_tool._MCP_NEW_HTTP", True):
                await server._run_http({
                    "url": "https://example.com/mcp",
                    "client_cert": str(tmp_path / "nope.pem"),
                })

        with pytest.raises(FileNotFoundError, match=r"remote.*client_cert.*not found"):
            asyncio.run(_drive())


# ---------------------------------------------------------------------------
# SSE transport — cert + verify routed via httpx_client_factory
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_sse_client():
    """Replace ``sse_client`` with a MagicMock that records its kwargs.

    Returns the captured kwargs dict so tests can assert how ``_run_http``
    called it.
    """
    captured_kwargs: dict = {}

    class _FakeStream:
        def __init__(self):
            self._read = AsyncMock()
            self._write = AsyncMock()

        async def __aenter__(self):
            return (self._read, self._write)

        async def __aexit__(self, *a):
            return False

    def fake_sse_client(**kwargs):
        captured_kwargs.clear()
        captured_kwargs.update(kwargs)
        return _FakeStream()

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            mock_session = MagicMock()
            mock_session.initialize = AsyncMock()
            return mock_session

        async def __aexit__(self, *a):
            return False

    with patch("tools.mcp_tool.sse_client", new=fake_sse_client), \
         patch("tools.mcp_tool.ClientSession", new=_FakeSession):
        yield captured_kwargs


class TestSSEClientCert:
    @pytest.mark.parametrize(
        "credential_header,credential_value",
        [
            ("Authorization", "Bearer synthetic"),
            ("Proxy-Authorization", "Basic synthetic"),
            ("Cookie", "session=synthetic"),
        ],
    )
    def test_default_factory_blocks_ambient_credentials_on_cross_origin_redirect(
        self,
        patch_sse_client,
        credential_header,
        credential_value,
    ):
        """Default SSE still needs a redirect guard for ambient credentials.

        Even without configured headers, auth, mTLS, or custom TLS settings, a
        shared SDK client can acquire Authorization, Proxy-Authorization, or a
        Cookie at runtime. The client factory (and its hook) must therefore be
        installed for the default redirect-following path too.
        """
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-default-ambient")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await server._run_http(
                    {
                        "url": "https://origin.example/mcp/sse",
                        "transport": "sse",
                    }
                )

        asyncio.run(drive())
        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None

        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers={}, timeout=httpx.Timeout(30.0), auth=None)

        hook = captured_client_kwargs["event_hooks"]["response"][0]
        benign = httpx.Response(
            302,
            headers={"Location": "https://redirect.example/mcp/sse"},
            request=httpx.Request(
                "GET",
                "https://origin.example/mcp/sse",
                headers={"MCP-Protocol-Version": "2025-03-26"},
            ),
        )
        asyncio.run(hook(benign))

        credential_bearing = httpx.Response(
            302,
            headers={"Location": "https://redirect.example/mcp/sse"},
            request=httpx.Request(
                "GET",
                "https://origin.example/mcp/sse",
                headers={credential_header: credential_value},
            ),
        )
        with pytest.raises(RuntimeError, match="cross-origin.*credential"):
            asyncio.run(hook(credential_bearing))

    def test_factory_blocks_custom_headers_on_cross_origin_redirect(
        self, patch_sse_client
    ):
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await server._run_http(
                    {
                        "url": "https://origin.example/mcp/sse",
                        "transport": "sse",
                        "headers": {"X-Api-Key": "synthetic-secret"},
                    }
                )

        asyncio.run(drive())
        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None

        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(
                headers={
                    "X-Api-Key": "synthetic-secret",
                    "Authorization": "Bearer synthetic",
                    "Cookie": "session=synthetic",
                    "X-Benign": "keep",
                },
                timeout=httpx.Timeout(30.0),
                auth=None,
            )

        hook = captured_client_kwargs["event_hooks"]["response"][0]
        same_origin = httpx.Response(
            302,
            headers={"Location": "/next"},
            request=httpx.Request(
                "GET",
                "https://origin.example/mcp/sse",
                headers={"X-Api-Key": "synthetic-secret"},
            ),
        )
        asyncio.run(hook(same_origin))

        cross_origin = httpx.Response(
            302,
            headers={"Location": "https://redirect.example/mcp/sse"},
            request=httpx.Request(
                "GET",
                "https://origin.example/mcp/sse",
                headers={
                    "X-Api-Key": "synthetic-secret",
                    "Authorization": "Bearer synthetic",
                    "Cookie": "session=synthetic",
                },
            ),
        )
        with pytest.raises(RuntimeError, match="cross-origin.*credential"):
            asyncio.run(hook(cross_origin))

    def test_strict_loopback_factory_disables_proxy_environment(
        self, patch_sse_client
    ):
        from tools.mcp_tool import MCPServerTask

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                try:
                    await asyncio.wait_for(
                        server._run_http(
                            {
                                "url": "http://127.0.0.1:8201/mcp/sse",
                                "transport": "sse",
                                "follow_redirects": False,
                            }
                        ),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, StopAsyncIteration, Exception):
                    pass

        asyncio.run(drive())
        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None
        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers=None, timeout=None, auth=None)

        assert captured_client_kwargs["follow_redirects"] is False
        assert captured_client_kwargs["trust_env"] is False

    def test_factory_injected_when_cert_set(self, patch_sse_client, tmp_path):
        """With client_cert set, an httpx_client_factory is injected that
        applies the cert (and follow_redirects=True to match the SDK)."""
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.pem"
        cert.write_text("dummy")

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(MCPServerTask, "_wait_for_lifecycle_event",
                              new=AsyncMock(return_value="shutdown")), \
                 patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                try:
                    await asyncio.wait_for(
                        server._run_http({
                            "url": "https://example.com/mcp/sse",
                            "transport": "sse",
                            "client_cert": str(cert),
                        }),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, StopAsyncIteration, Exception):
                    pass

        asyncio.run(drive())

        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None, "expected httpx_client_factory to be injected"

        # Invoke the factory the way the SDK would; capture the resulting
        # httpx.AsyncClient kwargs.
        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx
        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers={"x": "y"}, timeout=httpx.Timeout(30.0), auth=None)

        assert captured_client_kwargs["cert"] == str(cert)
        assert captured_client_kwargs["verify"] is True
        assert captured_client_kwargs["follow_redirects"] is True
        assert captured_client_kwargs["headers"] == {"x": "y"}

    def test_sse_cross_origin_redirect_with_cert_is_blocked(
        self, patch_sse_client, tmp_path
    ):
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.pem"
        cert.write_text("dummy")
        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await server._run_http(
                    {
                        "url": "https://origin.example/mcp/sse",
                        "transport": "sse",
                        "headers": {"X-Api-Key": "synthetic-secret"},
                        "client_cert": str(cert),
                    }
                )

        asyncio.run(drive())
        factory = patch_sse_client["httpx_client_factory"]
        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(
                headers={"X-Api-Key": "synthetic-secret"},
                timeout=httpx.Timeout(30.0),
                auth=None,
            )

        hook = captured_client_kwargs["event_hooks"]["response"][0]
        response = httpx.Response(
            302,
            headers={"Location": "https://redirect.example/mcp/sse"},
            request=httpx.Request(
                "GET",
                "https://origin.example/mcp/sse",
                headers={"X-Api-Key": "synthetic-secret"},
            ),
        )
        with pytest.raises(RuntimeError, match="cross-origin.*client certificate"):
            asyncio.run(hook(response))

    def test_sse_oauth_alone_installs_cross_origin_boundary(
        self, patch_sse_client
    ):
        from tools.mcp_tool import MCPServerTask

        auth = object()
        manager = MagicMock()
        manager.get_or_build_provider.return_value = auth
        server = MCPServerTask("sse-oauth")
        server._auth_type = "oauth"
        server._sampling = None

        async def drive():
            with patch(
                "tools.mcp_oauth_manager.get_manager", return_value=manager
            ), patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await server._run_http(
                    {
                        "url": "https://origin.example/mcp/sse",
                        "transport": "sse",
                    }
                )

        asyncio.run(drive())
        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None
        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers={}, timeout=httpx.Timeout(30.0), auth=auth)

        hook = captured_client_kwargs["event_hooks"]["response"][0]
        response = httpx.Response(
            302,
            headers={"Location": "https://redirect.example/mcp/sse"},
            request=httpx.Request("GET", "https://origin.example/mcp/sse"),
        )
        with pytest.raises(RuntimeError, match="cross-origin.*credential"):
            asyncio.run(hook(response))

    def test_sse_client_cert_is_independent_of_streamable_http_api(
        self, patch_sse_client, tmp_path
    ):
        from tools.mcp_tool import MCPServerTask

        cert = tmp_path / "client.pem"
        cert.write_text("dummy")
        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch("tools.mcp_tool._MCP_NEW_HTTP", False), patch.object(
                MCPServerTask,
                "_wait_for_lifecycle_event",
                new=AsyncMock(return_value="shutdown"),
            ), patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                await server._run_http(
                    {
                        "url": "https://example.com/mcp/sse",
                        "transport": "sse",
                        "client_cert": str(cert),
                    }
                )

        asyncio.run(drive())
        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None

        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx

        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers=None, timeout=None, auth=None)

        assert captured_client_kwargs["cert"] == str(cert)

    def test_factory_forwards_custom_ca_bundle(self, patch_sse_client, tmp_path):
        """ssl_verify as a path is forwarded to the factory's httpx client."""
        from tools.mcp_tool import MCPServerTask

        ca_bundle = tmp_path / "ca.pem"
        ca_bundle.write_text("dummy")

        server = MCPServerTask("sse-test")
        server._auth_type = ""
        server._sampling = None

        async def drive():
            with patch.object(MCPServerTask, "_wait_for_lifecycle_event",
                              new=AsyncMock(return_value="shutdown")), \
                 patch.object(MCPServerTask, "_discover_tools", new=AsyncMock()):
                try:
                    await asyncio.wait_for(
                        server._run_http({
                            "url": "https://example.com/mcp/sse",
                            "transport": "sse",
                            "ssl_verify": str(ca_bundle),
                        }),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, StopAsyncIteration, Exception):
                    pass

        asyncio.run(drive())

        factory = patch_sse_client.get("httpx_client_factory")
        assert factory is not None

        captured_client_kwargs: dict = {}

        class DummyAsyncClient:
            def __init__(self, **kwargs):
                captured_client_kwargs.update(kwargs)

        import httpx
        with patch.object(httpx, "AsyncClient", DummyAsyncClient):
            factory(headers=None, timeout=None, auth=None)

        assert captured_client_kwargs["verify"] == str(ca_bundle)
        assert "cert" not in captured_client_kwargs
