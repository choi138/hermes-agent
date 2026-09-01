"""Tests for immutable, deadline-bound read-only MCP capabilities."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import mcp_tool


_ALLOWED_TOOLS = frozenset({
    "get_status",
    "search_nodes",
    "search_memory_facts",
    "get_entity_edge",
})
_ALLOWED_ARGS = frozenset({"query", "max_facts"})
_PROFILE_HOME = "/profiles/default"


def _safe_config():
    return {
        "url": "http://127.0.0.1:8201/mcp",
        "enabled": True,
        "follow_redirects": False,
        "timeout": 2.0,
        "sampling": {"enabled": False},
        "elicitation": {"enabled": False},
        "tools": {
            "resources": False,
            "prompts": False,
            "include": sorted(_ALLOWED_TOOLS),
        },
    }


def _registered_tool_name(name):
    return f"mcp__graphiti_canonical__{name}"


def _fake_server(config, *, profile_home=_PROFILE_HOME, session=None):
    server = mcp_tool.MCPServerTask("graphiti_canonical")
    server._profile_home = profile_home
    server._config = config
    server.tool_timeout = float(config["timeout"])
    server._tools = [SimpleNamespace(name=name) for name in _ALLOWED_TOOLS]
    server._registered_tool_names = sorted(
        _registered_tool_name(name) for name in _ALLOWED_TOOLS
    )
    server.session = session or SimpleNamespace()
    return server


def _bind(monkeypatch, *, server=None, config=None, active_home=_PROFILE_HOME):
    config = config or _safe_config()
    server = server or _fake_server(config)
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"graphiti_canonical": config}
    )
    monkeypatch.setattr(mcp_tool, "_servers", {"graphiti_canonical": server})
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: Path(active_home))
    return mcp_tool.bind_read_only_mcp_tool(
        server_name="graphiti_canonical",
        tool_name="search_memory_facts",
        allowed_tools=_ALLOWED_TOOLS,
        allowed_argument_keys=_ALLOWED_ARGS,
        profile_home=_PROFILE_HOME,
        max_timeout=2.5,
        max_response_chars=262_144,
    )


def test_mcp_server_task_can_store_bound_profile_home():
    server = mcp_tool.MCPServerTask("graphiti_canonical")

    assert server._profile_home == ""


def test_empty_profile_home_never_normalizes_to_current_directory(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)

    assert mcp_tool._normalize_profile_home("") == ""


def test_binding_rejects_cross_profile_server_and_context(monkeypatch):
    config = _safe_config()
    cross_profile_server = _fake_server(config, profile_home="/profiles/other")

    with pytest.raises(RuntimeError, match="profile"):
        _bind(monkeypatch, server=cross_profile_server, config=config)

    with pytest.raises(RuntimeError, match="profile"):
        _bind(monkeypatch, config=config, active_home="/profiles/other")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8201/mcp?token=synthetic",
        "http://127.0.0.1:8201/mcp#token=synthetic",
        "http://127.0.0.1:8201/mcp;token=synthetic",
        "http://127.0.0.1:8201/a;b/mcp",
        "http://127.0.0.1:8201/a%3Bb/mcp",
    ],
)
def test_binding_rejects_loopback_url_embedded_metadata(monkeypatch, url):
    config = _safe_config()
    config["url"] = url

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


@pytest.mark.parametrize("follow_redirects", [None, True])
def test_binding_requires_redirects_explicitly_disabled(monkeypatch, follow_redirects):
    config = _safe_config()
    if follow_redirects is None:
        config.pop("follow_redirects")
    else:
        config["follow_redirects"] = follow_redirects

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


@pytest.mark.parametrize(
    ("location", "key", "value"),
    [
        ("server", "description", "canonical recall"),
        ("server", "client_cert", None),
        ("server", "headers", {}),
        ("sampling", "handler", None),
        ("elicitation", "handler", False),
    ],
)
def test_binding_rejects_unknown_config_keys_even_when_falsey(
    monkeypatch, location, key, value
):
    config = _safe_config()
    target = config if location == "server" else config[location]
    target[key] = value

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


@pytest.mark.parametrize("enabled", [None, 1, "true"])
def test_binding_requires_enabled_to_be_explicit_boolean_true(monkeypatch, enabled):
    config = _safe_config()
    if enabled is None:
        config.pop("enabled")
    else:
        config["enabled"] = enabled

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


def test_binding_requires_exact_fixed_read_only_tool_allowlist(monkeypatch):
    config = _safe_config()
    config["tools"]["include"] = ["search_memory_facts"]

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


def test_binding_accepts_extra_raw_tools_and_binds_their_full_fingerprint(monkeypatch):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(isError=False, structuredContent={"facts": []}, content=[])

    config = _safe_config()
    server = _fake_server(config, session=Session())
    server._tools.extend(
        [SimpleNamespace(name="add_memory"), SimpleNamespace(name="delete_memory")]
    )
    capability = _bind(monkeypatch, config=config, server=server)
    monkeypatch.setattr(
        mcp_tool,
        "_run_on_mcp_loop",
        lambda coro, **_kwargs: asyncio.run(coro),
    )

    result = capability.call(
        {"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1
    )

    assert result == {"structuredContent": {"facts": []}}
    assert calls == [
        ("search_memory_facts", {"query": "P1", "max_facts": 12})
    ]


def test_binding_rejects_registered_mutation_tool(monkeypatch):
    config = _safe_config()
    server = _fake_server(config)
    server._registered_tool_names.append(
        _registered_tool_name("add_memory")
    )

    with pytest.raises(RuntimeError, match="tool provenance"):
        _bind(monkeypatch, config=config, server=server)


def test_binding_rejects_config_or_live_server_instance_swap(monkeypatch):
    config = _safe_config()
    capability = _bind(monkeypatch, config=config)

    changed = _safe_config()
    changed["url"] = "http://127.0.0.1:9999/mcp"
    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"graphiti_canonical": changed}
    )
    with pytest.raises(RuntimeError, match="configuration"):
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)

    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config", lambda: {"graphiti_canonical": config}
    )
    monkeypatch.setattr(
        mcp_tool,
        "_servers",
        {"graphiti_canonical": _fake_server(config)},
    )
    with pytest.raises(RuntimeError, match="instance"):
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)


def test_bound_call_rejects_session_replacement_after_bind(monkeypatch):
    calls = []

    class Session:
        def __init__(self, label):
            self.label = label

        async def call_tool(self, _name, arguments):
            del arguments
            calls.append(self.label)
            return SimpleNamespace(isError=False, structuredContent={}, content=[])

    config = _safe_config()
    original = Session("original")
    replacement = Session("replacement")
    server = _fake_server(config, session=original)
    capability = _bind(monkeypatch, config=config, server=server)
    server.session = replacement
    monkeypatch.setattr(
        mcp_tool,
        "_run_on_mcp_loop",
        lambda coro, **_kwargs: asyncio.run(coro),
    )

    with pytest.raises(RuntimeError, match="session"):
        capability.call(
            {"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1
        )

    assert calls == []


@pytest.mark.parametrize(
    "mutation", ["registry", "config", "tools", "registered_tools", "rpc_lock"]
)
def test_bound_call_revalidates_after_rpc_lock_acquisition(monkeypatch, mutation):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(isError=False, structuredContent={}, content=[])

    config = _safe_config()
    server = _fake_server(config, session=Session())
    capability = _bind(monkeypatch, config=config, server=server)

    def run_on_loop(coro, **_kwargs):
        if mutation == "registry":
            mcp_tool._servers["graphiti_canonical"] = _fake_server(config)
        elif mutation == "config":
            config["url"] = "http://127.0.0.1:9999/mcp"
        elif mutation == "tools":
            server._tools.append(SimpleNamespace(name="delete_memory"))
        elif mutation == "registered_tools":
            server._registered_tool_names.append(
                _registered_tool_name("add_memory")
            )
        else:
            server._rpc_lock = asyncio.Lock()
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_on_loop)

    with pytest.raises(RuntimeError):
        capability.call(
            {"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1
        )

    assert calls == []


def test_bound_call_invokes_exact_session_once_with_remaining_deadline(
    monkeypatch,
):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"facts": []},
                content=[],
            )

    config = _safe_config()
    capability = _bind(
        monkeypatch,
        config=config,
        server=_fake_server(config, session=Session()),
    )
    timeouts = []

    def run_on_loop(coro, *, timeout=None):
        timeouts.append(timeout)
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_on_loop)
    deadline = time.monotonic() + 0.5

    result = capability.call({"query": "P1", "max_facts": 12}, deadline=deadline)

    assert result == {"structuredContent": {"facts": []}}
    assert calls == [("search_memory_facts", {"query": "P1", "max_facts": 12})]
    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.5


def test_bound_call_uses_configured_timeout_below_global_ceiling(monkeypatch):
    class Session:
        async def call_tool(self, _name, arguments):
            assert arguments == {"query": "P1", "max_facts": 12}
            return SimpleNamespace(
                isError=False,
                structuredContent={"facts": []},
                content=[],
            )

    config = _safe_config()
    config["timeout"] = 0.2
    capability = _bind(
        monkeypatch,
        config=config,
        server=_fake_server(config, session=Session()),
    )
    timeouts = []

    def run_on_loop(coro, *, timeout=None):
        timeouts.append(timeout)
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_on_loop)

    capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 2)

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.2


def test_bound_call_rejects_unapproved_arguments_before_transport(monkeypatch):
    capability = _bind(monkeypatch)

    with pytest.raises(RuntimeError, match="arguments"):
        capability.call(
            {"query": "P1", "delete_all": True}, deadline=time.monotonic() + 1
        )
