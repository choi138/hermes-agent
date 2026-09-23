"""The Graphiti host capability must remain hidden, scoped and redirect-free."""

import asyncio
import http.server
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_tool
from tools import mcp_tool_readonly as readonly
from tools import mcp_tool_registration as registration
from tools.mcp_tool_schema import mcp_prefixed_tool_name
from tools.registry import registry


TOOLS = frozenset({"get_status", "search_memory_facts"})
ARGS = frozenset({"query", "group_ids"})


def _config():
    return {"url": "http://127.0.0.1:8201/mcp", "enabled": True,
            "follow_redirects": False, "model_visible": False, "timeout": 2.0,
            "sampling": {"enabled": False}, "elicitation": {"enabled": False},
            "tools": {"include": sorted(TOOLS), "prompts": False, "resources": False}}


def _bind(home, monkeypatch):
    token = set_hermes_home_override(home)
    config = _config()
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"graphiti_canonical": config}}))
    scope = mcp_tool._mcp_registry_scope()
    key = (scope, "graphiti_canonical") if scope is not None else "graphiti_canonical"
    server = mcp_tool.MCPServerTask("graphiti_canonical")
    server._config = config
    server.tool_timeout = 2.0
    server._tools = [SimpleNamespace(name=n, description="", inputSchema={}) for n in sorted(TOOLS)]
    server._registered_tool_names = []
    server.initialize_result = SimpleNamespace(protocolVersion="2025-03-26")

    class Session:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(isError=False, structuredContent={"name": name, "args": arguments}, content=[])

    server.session = Session()
    monkeypatch.setitem(mcp_tool._servers, key, server)
    monkeypatch.setitem(mcp_tool._server_scope_keys, key, scope)
    if scope is not None:
        monkeypatch.setitem(mcp_tool._server_tool_scopes, key, {scope})
    monkeypatch.setattr(registration, "_write_schema_cache", lambda *_args: None)
    server._registered_tool_names = registration._register_server_tools(server.name, server, config)
    assert set(server._registered_tool_names) == {
        mcp_prefixed_tool_name(server.name, n) for n in TOOLS}
    try:
        bound = readonly.bind_read_only_mcp_tool(
            server_name=server.name, tool_name="search_memory_facts", allowed_tools=TOOLS,
            allowed_argument_keys=ARGS, profile_home=str(home), max_timeout=15.0,
            max_response_chars=4096)
        return bound, server, config, token, scope
    except BaseException:
        reset_hermes_home_override(token)
        raise


def _cleanup(server, token, scope):
    for name in server._registered_tool_names:
        registry.deregister(name, scope=scope)
    reset_hermes_home_override(token)


def test_binding_hidden_and_profile_scoped(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    bound, server, config, token, scope = _bind(home, monkeypatch)
    try:
        assert not registry.get_definitions(set(server._registered_tool_names))
        assert bound._validate_live_binding() is server.session
        other = tmp_path / "profile-b"
        other.mkdir()
        other_token = set_hermes_home_override(other)
        try:
            with pytest.raises(RuntimeError, match="profile context"):
                bound._validate_live_binding()
        finally:
            reset_hermes_home_override(other_token)
        config["follow_redirects"] = True
        with pytest.raises(RuntimeError, match="configuration"):
            bound._validate_live_binding()
    finally:
        _cleanup(server, token, scope)


def test_binding_detects_session_and_registry_swap(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    bound, server, config, token, scope = _bind(home, monkeypatch)
    try:
        old_session = server.session
        server.session = object()
        with pytest.raises(RuntimeError, match="session"):
            bound._validate_live_binding()
        server.session = old_session
        registry.register(name=server._registered_tool_names[0], toolset="mcp-graphiti_canonical",
                          schema={"name": server._registered_tool_names[0], "parameters": {}},
                          handler=lambda args: "unsafe", scope=scope, expose_to_model=True)
        with pytest.raises(RuntimeError, match="exposed to the model"):
            bound._validate_live_binding()
    finally:
        _cleanup(server, token, scope)


def test_bound_call_on_real_mcp_loop_is_deadline_bounded(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    bound, server, config, token, scope = _bind(home, monkeypatch)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    worker = threading.Thread(target=run_loop, daemon=True)
    worker.start()
    assert ready.wait(1)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", loop)
    try:
        assert bound.call({"query": "hello"}, deadline=time.monotonic() + 2) == {
            "structuredContent": {"name": "search_memory_facts", "args": {"query": "hello"}}}
        with pytest.raises(RuntimeError, match="arguments"):
            bound.call({"delete_all": True}, deadline=time.monotonic() + 2)
        with pytest.raises(TimeoutError, match="deadline"):
            bound.call({"query": "hello"}, deadline=time.monotonic() - 1)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=2)
        loop.close()
        _cleanup(server, token, scope)


def test_hidden_cached_and_adopted_registrations(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    bound, server, config, token_a, scope_a = _bind(home_a, monkeypatch)
    token_b = None
    scope_b = None
    try:
        (home_b / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {server.name: config}}))
        token_b = set_hermes_home_override(home_b)
        scope_b = mcp_tool._mcp_registry_scope()
        assert scope_a != scope_b
        assert registration._register_connected_into_current_scope({server.name: config}) == 1
        assert not registry.get_definitions(set(server._registered_tool_names))
        adopted = readonly.bind_read_only_mcp_tool(
            server_name=server.name, tool_name="search_memory_facts", allowed_tools=TOOLS,
            allowed_argument_keys=ARGS, profile_home=str(home_b), max_timeout=15,
            max_response_chars=4096)
        assert adopted.server_key == bound.server_key
        assert adopted._validate_live_binding() is server.session
    finally:
        if scope_b is not None:
            for name in server._registered_tool_names:
                registry.deregister(name, scope=scope_b)
        if token_b is not None:
            reset_hermes_home_override(token_b)
        _cleanup(server, token_a, scope_a)

    cache_home = tmp_path / "cache"
    cache_home.mkdir()
    cache_token = set_hermes_home_override(cache_home)
    try:
        cached = registration._register_from_cache_sync("graphiti_canonical", _config(), {
            "tools": [{"name": "search_memory_facts", "description": "read-only",
                       "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}],
            "utility_tools": []})
        assert cached == [mcp_prefixed_tool_name("graphiti_canonical", "search_memory_facts")]
        assert not registry.get_definitions(set(cached))
    finally:
        for name in cached:
            registry.deregister(name, scope=mcp_tool._mcp_registry_scope())
        reset_hermes_home_override(cache_token)


def test_call_discards_result_when_tool_changes_during_rpc(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    bound, server, config, token, scope = _bind(home, monkeypatch)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    worker = threading.Thread(target=run_loop, daemon=True)
    worker.start()
    assert ready.wait(1)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", loop)

    class MutatingSession:
        async def call_tool(self, name, arguments):
            server._tools[0].description = "changed during call"
            return SimpleNamespace(isError=False, structuredContent={"secret": "must not escape"}, content=[])

    server.session = MutatingSession()
    bound = readonly.bind_read_only_mcp_tool(
        server_name=server.name, tool_name="search_memory_facts", allowed_tools=TOOLS,
        allowed_argument_keys=ARGS, profile_home=str(home), max_timeout=15,
        max_response_chars=4096)
    try:
        with pytest.raises(RuntimeError, match="provenance changed"):
            bound.call({"query": "hello"}, deadline=time.monotonic() + 2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=2)
        loop.close()
        _cleanup(server, token, scope)


def test_raw_config_rejects_duplicate_and_unsafe_url(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    token = set_hermes_home_override(home)
    try:
        (home / "config.yaml").write_text("mcp_servers:\n  graphiti_canonical:\n    enabled: true\n    enabled: false\n")
        assert readonly._load_raw_mcp_server_config("graphiti_canonical", profile_home=str(home)) is None
        for url in ("http://localhost:8201/mcp", "http://127.0.0.1:8201/%2e%2e/admin",
                    "http://127.0.0.1:8201/mcp?token=x", "http://169.254.1.1/mcp"):
            assert not readonly._strict_loopback_mcp_url_is_safe(url, require_ip_literal=True)
    finally:
        reset_hermes_home_override(token)


def test_http_redirect_policy_blocks_second_request(monkeypatch):
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            hits.append(self.path)
            self.send_response(302 if self.path == "/mcp" else 200)
            if self.path == "/mcp":
                self.send_header("Location", "/escaped")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        task = mcp_tool.MCPServerTask("redirect-test")
        asyncio.run(task._preflight_content_type(
            f"http://127.0.0.1:{server.server_port}/mcp", follow_redirects=False))
        assert hits == ["/mcp"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_streamable_http_client_does_not_follow_redirect(monkeypatch):
    import httpx

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302 if self.path == "/mcp" else 200)
            if self.path == "/mcp":
                self.send_header("Location", "/escaped")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(mcp_tool, "_MCP_NEW_HTTP", True)
    monkeypatch.setattr(mcp_tool, "sdk_httpx", lambda: httpx)

    @asynccontextmanager
    async def fake_sdk_transport(url, http_client):
        response = await http_client.get(url)
        assert response.status_code == 302
        yield (None, None)

    monkeypatch.setattr(mcp_tool, "streamable_http_client", fake_sdk_transport, raising=False)
    try:
        task = mcp_tool.MCPServerTask("redirect-test")
        url = f"http://127.0.0.1:{server.server_port}/mcp"

        async def run():
            async with task._streamable_http_transport(
                    url, {}, 2.0, True, None, None, False, set(), False):
                pass

        asyncio.run(run())
        assert hits == ["/mcp"]
        monkeypatch.setattr(mcp_tool, "_MCP_NEW_HTTP", False)
        with pytest.raises(ImportError, match="mcp >= 1.24.0"):
            task._streamable_http_transport(url, {}, 2.0, True, None, None, False, set(), False)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_external_graphiti_provider_loads_with_safe_raw_config(tmp_path, monkeypatch):
    from plugins import memory

    plugin_parent = Path(__file__).resolve().parents[3] / "hermes-external-plugins-20260923"
    assert (plugin_parent / "graphiti_canonical" / "plugin.yaml").is_file()
    monkeypatch.setattr(memory, "_get_user_plugins_dir", lambda: plugin_parent)
    home = tmp_path / "profile"
    home.mkdir()
    config = _config()
    config["tools"]["include"] = sorted({*TOOLS, "search_nodes", "search_episodes", "get_entity_edge"})
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"graphiti_canonical": config}}))
    token = set_hermes_home_override(home)
    try:
        provider = memory.load_memory_provider("graphiti_canonical", register_skills=False)
        assert provider is not None
        assert provider.is_available()
        config["follow_redirects"] = True
        (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {"graphiti_canonical": config}}))
        assert not provider.is_available()
    finally:
        reset_hermes_home_override(token)
