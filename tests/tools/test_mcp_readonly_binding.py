"""Tests for immutable, deadline-bound read-only MCP capabilities."""

import asyncio
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools import mcp_tool


_ALLOWED_TOOLS = frozenset({
    "get_status",
    "search_nodes",
    "search_memory_facts",
    "get_entity_edge",
})
_ALLOWED_ARGS = frozenset({"query", "max_facts"})
_PROFILE_HOME = "/profiles/default"


@contextmanager
def _running_mcp_loop(monkeypatch):
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    assert ready.wait(1)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", loop)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        assert not thread.is_alive()
        loop.close()


def _safe_config():
    return {
        "url": "http://127.0.0.1:8201/mcp",
        "enabled": True,
        "follow_redirects": False,
        "model_visible": False,
        "timeout": 2.0,
        "sampling": {"enabled": False},
        "elicitation": {"enabled": False},
        "tools": {
            "resources": False,
            "prompts": False,
            "include": sorted(_ALLOWED_TOOLS),
        },
    }


def test_raw_config_loader_scopes_to_canonical_server_without_general_loader(
    monkeypatch, tmp_path
):
    raw = {
        "mcp_servers": {
            "unrelated": {
                "url": "https://unrelated.invalid/mcp",
                "headers": {"Authorization": "Bearer ${UNRELATED_SECRET}"},
            },
            "graphiti_canonical": _safe_config(),
        }
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("UNRELATED_SECRET", "synthetic-value-that-must-not-resolve")
    import hermes_constants
    import hermes_cli.config as hermes_config

    monkeypatch.setattr(hermes_constants, "get_config_path", lambda: config_path)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("general config loader used")),
    )
    monkeypatch.setattr(
        mcp_tool,
        "_load_mcp_config",
        lambda: (_ for _ in ()).throw(AssertionError("general MCP loader used")),
    )

    loaded = mcp_tool._load_raw_mcp_server_config(
        "graphiti_canonical", profile_home=str(tmp_path)
    )

    assert loaded == _safe_config()
    assert "synthetic-value-that-must-not-resolve" not in repr(loaded)
    assert (
        mcp_tool._load_raw_mcp_server_config(
            "graphiti_canonical", profile_home=str(tmp_path / "other")
        )
        is None
    )


def test_raw_config_loader_never_constructs_unrelated_mcp_values(monkeypatch, tmp_path):
    target_yaml = yaml.safe_dump(_safe_config(), sort_keys=False)
    raw = (
        "mcp_servers:\n"
        "  unrelated:\n"
        "    payload: !unrelated-secret-constructor {}\n"
        "  graphiti_canonical:\n"
        f"{textwrap.indent(target_yaml, '    ')}"
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(raw, encoding="utf-8")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_config_path", lambda: config_path)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    assert (
        mcp_tool._load_raw_mcp_server_config(
            "graphiti_canonical", profile_home=str(tmp_path)
        )
        == _safe_config()
    )


def _registered_tool_name(name):
    return f"mcp__graphiti_canonical__{name}"


def _fake_server(config, *, profile_home=_PROFILE_HOME, session=None):
    server = mcp_tool.MCPServerTask("graphiti_canonical")
    server._profile_home = profile_home
    server._config = config
    server.tool_timeout = float(config["timeout"])
    server._tools = [
        SimpleNamespace(
            name=name,
            description="read-only test tool",
            inputSchema={"type": "object"},
            outputSchema=None,
        )
        for name in _ALLOWED_TOOLS
    ]
    server._registered_tool_names = sorted(
        _registered_tool_name(name) for name in _ALLOWED_TOOLS
    )

    async def default_call_tool(*_args, **_kwargs):
        raise AssertionError("default fake transport must not be called")

    server.session = (
        session
        if session is not None
        else SimpleNamespace(call_tool=default_call_tool)
    )
    server.initialize_result = SimpleNamespace(capabilities={})
    return server


def _bind(monkeypatch, *, server=None, config=None, active_home=_PROFILE_HOME):
    config = config or _safe_config()
    server = server or _fake_server(config)
    monkeypatch.setattr(
        mcp_tool,
        "_load_raw_mcp_server_config",
        lambda server_name, **_kwargs: (
            config if server_name == "graphiti_canonical" else None
        ),
    )
    monkeypatch.setattr(mcp_tool, "_servers", {"graphiti_canonical": server})
    monkeypatch.setattr(
        mcp_tool,
        "_read_only_registry_attestation",
        lambda names: tuple((name, "test-handler") for name in names),
    )
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
        "http://127.0.0.1:8201/a%253Bb/mcp",
        "http://127.0.0.1:8201/a；b/mcp",
        "http://127.0.0.1:8201/a%EF%BC%9Bb/mcp",
        "http://127.0.0.1:8201/a%255Cb/mcp",
        "http://127.0.0.1:8201/a%250Ab/mcp",
        "http://127.0.0.1:8201/mcp?",
        "http://127.0.0.1:8201/mcp#",
        "http://127.0.0.1:8201/mcp%3Fhidden",
        "http://127.0.0.1:8201/mcp%23hidden",
        "http://127.0.0.1:8201/a%252Fb",
        "http://127.0.0.1:8201/a/../admin",
    ],
)
def test_binding_rejects_loopback_url_embedded_metadata(monkeypatch, url):
    config = _safe_config()
    config["url"] = url

    with pytest.raises(RuntimeError, match="configuration"):
        _bind(monkeypatch, config=config)


def test_canonical_loopback_url_requires_ip_literal(monkeypatch):
    assert mcp_tool._strict_loopback_mcp_url_is_safe("http://localhost:8201/mcp")
    assert mcp_tool._strict_loopback_mcp_url_is_safe(
        "http://127.0.0.1:8201/mcp", require_ip_literal=True
    )
    assert mcp_tool._strict_loopback_mcp_url_is_safe(
        "http://[::1]:8201/mcp", require_ip_literal=True
    )
    assert not mcp_tool._strict_loopback_mcp_url_is_safe(
        "http://localhost:8201/mcp", require_ip_literal=True
    )
    config = _safe_config()
    config["url"] = "http://localhost:8201/mcp"
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
            return SimpleNamespace(
                isError=False, structuredContent={"facts": []}, content=[]
            )

    config = _safe_config()
    server = _fake_server(config, session=Session())
    server._tools.extend([
        SimpleNamespace(name="add_memory"),
        SimpleNamespace(name="delete_memory"),
    ])
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
    assert calls == [("search_memory_facts", {"query": "P1", "max_facts": 12})]


def test_binding_rejects_registered_mutation_tool(monkeypatch):
    config = _safe_config()
    server = _fake_server(config)
    server._registered_tool_names.append(_registered_tool_name("add_memory"))

    with pytest.raises(RuntimeError, match="tool provenance"):
        _bind(monkeypatch, config=config, server=server)


def test_binding_rejects_config_or_live_server_instance_swap(monkeypatch):
    config = _safe_config()
    capability = _bind(monkeypatch, config=config)
    monkeypatch.setattr(
        mcp_tool,
        "_run_on_mcp_loop",
        lambda coro, **_kwargs: asyncio.run(coro),
    )

    changed = _safe_config()
    changed["url"] = "http://127.0.0.1:9999/mcp"
    monkeypatch.setattr(
        mcp_tool,
        "_load_raw_mcp_server_config",
        lambda _server_name, **_kwargs: changed,
    )
    with pytest.raises(RuntimeError, match="configuration"):
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)

    monkeypatch.setattr(
        mcp_tool,
        "_load_raw_mcp_server_config",
        lambda _server_name, **_kwargs: config,
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
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)

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
            server._registered_tool_names.append(_registered_tool_name("add_memory"))
        else:
            server._rpc_lock = asyncio.Lock()
        return asyncio.run(coro)

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_on_loop)

    with pytest.raises(RuntimeError):
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)

    assert calls == []


@pytest.mark.parametrize(
    "mutation",
    ["registry", "config", "session", "tools", "registered_tools", "rpc_lock"],
)
def test_bound_call_discards_result_when_provenance_changes_during_rpc(
    monkeypatch, mutation
):
    calls = []
    mutate = [lambda: None]

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            mutate[0]()
            return SimpleNamespace(
                isError=False,
                structuredContent={"facts": ["must-be-discarded"]},
                content=[],
            )

    config = _safe_config()
    server = _fake_server(config, session=Session())
    capability = _bind(monkeypatch, config=config, server=server)

    if mutation == "registry":
        mutate[0] = lambda: mcp_tool._servers.__setitem__(
            "graphiti_canonical", _fake_server(config)
        )
    elif mutation == "config":
        mutate[0] = lambda: config.__setitem__("url", "http://127.0.0.1:9999/mcp")
    elif mutation == "session":
        mutate[0] = lambda: setattr(server, "session", SimpleNamespace())
    elif mutation == "tools":
        mutate[0] = lambda: server._tools.append(SimpleNamespace(name="delete_memory"))
    elif mutation == "registered_tools":
        mutate[0] = lambda: server._registered_tool_names.append(
            _registered_tool_name("add_memory")
        )
    else:
        mutate[0] = lambda: setattr(server, "_rpc_lock", asyncio.Lock())

    monkeypatch.setattr(
        mcp_tool,
        "_run_on_mcp_loop",
        lambda coro, **_kwargs: asyncio.run(coro),
    )

    with pytest.raises(RuntimeError):
        capability.call({"query": "P1", "max_facts": 12}, deadline=time.monotonic() + 1)

    assert calls == [("search_memory_facts", {"query": "P1", "max_facts": 12})]


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


def test_bound_call_rejects_replaced_session_callable(monkeypatch):
    calls = []

    class Session:
        async def call_tool(self, name, arguments):
            calls.append(("original", name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"facts": []},
                content=[],
            )

    config = _safe_config()
    session = Session()
    capability = _bind(
        monkeypatch,
        config=config,
        server=_fake_server(config, session=session),
    )

    async def forged_call_tool(name, arguments):
        calls.append(("forged", name, arguments))
        return SimpleNamespace(
            isError=False,
            structuredContent={"facts": ["forged"]},
            content=[],
        )

    session.call_tool = forged_call_tool
    monkeypatch.setattr(
        mcp_tool,
        "_run_on_mcp_loop",
        lambda coro, **_kwargs: asyncio.run(coro),
    )

    with pytest.raises(RuntimeError, match="provenance"):
        capability.call({"query": "P1"}, deadline=time.monotonic() + 1)

    assert calls == []


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


def test_bound_call_deadline_covers_slow_raw_config_verification(monkeypatch):
    class Session:
        async def call_tool(self, _name, arguments):
            del arguments
            raise AssertionError("transport must not run after deadline")

    config = _safe_config()
    capability = _bind(
        monkeypatch,
        config=config,
        server=_fake_server(config, session=Session()),
    )
    slow_read_done = threading.Event()

    def slow_raw_config(_server_name, **_kwargs):
        time.sleep(0.25)
        slow_read_done.set()
        return config

    monkeypatch.setattr(mcp_tool, "_load_raw_mcp_server_config", slow_raw_config)

    with _running_mcp_loop(monkeypatch):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            capability.call(
                {"query": "P1", "max_facts": 12},
                deadline=started + 0.06,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 0.18
        assert slow_read_done.wait(0.5)


def test_run_on_mcp_loop_waits_for_cooperative_cancellation_cleanup(monkeypatch):
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    def release_cleanup():
        assert cleanup_started.wait(1)
        time.sleep(0.02)
        allow_cleanup.set()

    releaser = threading.Thread(target=release_cleanup, daemon=True)
    releaser.start()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(allow_cleanup.wait)
            cleanup_done.set()

    with _running_mcp_loop(monkeypatch):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            mcp_tool._run_on_mcp_loop(operation(), timeout=0.15)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert cleanup_done.is_set()

    releaser.join(timeout=1)
    assert not releaser.is_alive()


def test_run_on_mcp_loop_bounds_lingering_cancellation_cleanup(monkeypatch):
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_done = threading.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.to_thread(allow_cleanup.wait)
            cleanup_done.set()

    with _running_mcp_loop(monkeypatch):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            mcp_tool._run_on_mcp_loop(operation(), timeout=0.12)
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert cleanup_started.wait(0.05)
        assert not cleanup_done.is_set()
        allow_cleanup.set()
        assert cleanup_done.wait(0.5)


def test_bound_call_rejects_unapproved_arguments_before_transport(monkeypatch):
    capability = _bind(monkeypatch)

    with pytest.raises(RuntimeError, match="arguments"):
        capability.call(
            {"query": "P1", "delete_all": True}, deadline=time.monotonic() + 1
        )


def test_read_only_serializer_rejects_list_subclass_before_iteration():
    class ExplodingList(list):
        def __iter__(self):
            raise AssertionError("oversized container was traversed")

    result = SimpleNamespace(
        isError=False,
        structuredContent={"facts": ExplodingList([None] * 257)},
        content=[],
    )
    with pytest.raises(RuntimeError, match="malformed"):
        mcp_tool._serialize_read_only_result(result, 262_144)


def test_read_only_serializer_rejects_excessive_nesting():
    nested = {"fact": "safe"}
    for _ in range(12):
        nested = {"next": nested}
    result = SimpleNamespace(isError=False, structuredContent=nested, content=[])

    with pytest.raises(RuntimeError, match="exceeds"):
        mcp_tool._serialize_read_only_result(result, 262_144)


def test_timed_out_config_validation_is_single_flight(monkeypatch):
    config = _safe_config()
    capability = _bind(monkeypatch, config=config)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def slow_config(*_args, **_kwargs):
        calls.append(1)
        started.set()
        release.wait(0.5)
        finished.set()
        return config

    monkeypatch.setattr(mcp_tool, "_load_raw_mcp_server_config", slow_config)
    try:
        with _running_mcp_loop(monkeypatch):
            with pytest.raises(TimeoutError):
                capability.call({"query": "P1"}, deadline=time.monotonic() + 0.05)
            assert started.wait(0.1)
            second_started = time.monotonic()
            with pytest.raises((RuntimeError, TimeoutError)):
                capability.call({"query": "P1"}, deadline=time.monotonic() + 0.1)
            assert time.monotonic() - second_started < 0.15
            assert calls == [1]
    finally:
        release.set()
        assert finished.wait(0.5)


def test_bound_timeout_reconnects_exact_transport_after_rpc_starts(monkeypatch):
    config = _safe_config()
    release = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()

    class Session:
        async def call_tool(self, _tool_name, arguments=None):
            del arguments
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                while not release.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        continue
                finished.set()
                raise

    server = _fake_server(config, session=Session())
    capability = _bind(monkeypatch, server=server, config=config)
    try:
        with _running_mcp_loop(monkeypatch):
            with pytest.raises(TimeoutError):
                capability.call({"query": "P1"}, deadline=time.monotonic() + 0.08)
            assert cancelled.wait(0.1)
            reconnect_was_signalled = server._reconnect_event.is_set()
            shutdown_was_signalled = server._shutdown_event.is_set()
            release.set()
            assert finished.wait(0.5)
            assert reconnect_was_signalled
            assert not shutdown_was_signalled
    finally:
        release.set()
        assert finished.wait(0.5)


def test_raw_config_loader_rejects_malformed_yaml_outside_target(monkeypatch, tmp_path):
    raw = "mcp_servers:\n  unrelated:\n    broken: [unterminated\n  graphiti_canonical:\n" + textwrap.indent(
        yaml.safe_dump(_safe_config(), sort_keys=False), "    "
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(raw, encoding="utf-8")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_config_path", lambda: config_path)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert mcp_tool._load_raw_mcp_server_config(
        "graphiti_canonical", profile_home=str(tmp_path)
    ) is None


def test_raw_config_loader_rejects_duplicate_target_keys(monkeypatch, tmp_path):
    raw = "mcp_servers:\n  graphiti_canonical:\n    timeout: 1.0\n    timeout: 2.0\n"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(raw, encoding="utf-8")
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_config_path", lambda: config_path)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert mcp_tool._load_raw_mcp_server_config(
        "graphiti_canonical", profile_home=str(tmp_path)
    ) is None


def test_bound_call_freezes_arguments_before_dispatch(monkeypatch):
    original = {"query": "P1", "max_facts": 12}

    class Session:
        async def call_tool(self, _name, arguments):
            original["query"] = "mutated-after-validation"
            assert arguments == {"query": "P1", "max_facts": 12}
            assert arguments is not original
            return SimpleNamespace(isError=False, structuredContent={}, content=[])

    config = _safe_config()
    capability = _bind(monkeypatch, server=_fake_server(config, session=Session()))
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", lambda coro, **_kw: asyncio.run(coro))
    capability.call(original, deadline=time.monotonic() + 1)


def test_read_only_serializer_rejects_list_subclass_before_hooks():
    class HookedList(list):
        def __len__(self):
            raise AssertionError("subclass hook invoked")

    result = SimpleNamespace(
        isError=False, structuredContent={"facts": HookedList(["safe"])}, content=[]
    )
    with pytest.raises(RuntimeError, match="malformed"):
        mcp_tool._serialize_read_only_result(result, 262_144)


def test_read_only_serializer_rejects_content_list_subclass_before_index_hook():
    class HookedContentList(list):
        def __getitem__(self, _key):
            raise AssertionError("subclass index hook invoked")

    result = SimpleNamespace(
        isError=False,
        structuredContent={"facts": []},
        content=HookedContentList([SimpleNamespace(text="safe")]),
    )
    with pytest.raises(RuntimeError, match="malformed"):
        mcp_tool._serialize_read_only_result(result, 262_144)


def test_lock_contention_timeout_does_not_poison_server(monkeypatch):
    class Session:
        async def call_tool(self, *_args, **_kwargs):
            raise AssertionError("RPC must not start while lock is held")

    config = _safe_config()
    server = _fake_server(config, session=Session())
    capability = _bind(monkeypatch, server=server, config=config)
    acquired = threading.Event()
    release = threading.Event()

    async def hold_rpc_lock():
        async with server._rpc_lock:
            acquired.set()
            await asyncio.to_thread(release.wait)

    try:
        with _running_mcp_loop(monkeypatch) as loop:
            holder = asyncio.run_coroutine_threadsafe(hold_rpc_lock(), loop)
            try:
                assert acquired.wait(0.5)
                with pytest.raises(TimeoutError):
                    capability.call(
                        {"query": "P1"}, deadline=time.monotonic() + 0.06
                    )
                assert not server._shutdown_event.is_set()
                assert not server._reconnect_event.is_set()
            finally:
                release.set()
                holder.result(timeout=0.5)
    finally:
        release.set()


def test_expired_deadline_is_checked_before_argument_walk(monkeypatch):
    class HookedList(list):
        def __len__(self):
            raise AssertionError("arguments were inspected after deadline")

    capability = _bind(monkeypatch)
    with pytest.raises(TimeoutError, match="deadline expired"):
        capability.call(
            {"query": HookedList(["P1"])}, deadline=time.monotonic() - 1
        )


@pytest.mark.parametrize("mutation", ["tool_object", "tool_schema", "initialize", "registry"])
def test_bound_call_rejects_extended_provenance_mutation(monkeypatch, mutation):
    config = _safe_config()
    server = _fake_server(config)
    capability = _bind(monkeypatch, server=server, config=config)
    if mutation == "tool_object":
        server._tools = [
            SimpleNamespace(
                name=tool.name,
                description=tool.description,
                inputSchema=dict(tool.inputSchema),
                outputSchema=tool.outputSchema,
            )
            for tool in server._tools
        ]
    elif mutation == "tool_schema":
        server._tools[0].inputSchema["properties"] = {"write": {"type": "boolean"}}
    elif mutation == "initialize":
        server.initialize_result.capabilities["sampling"] = {}
    else:
        monkeypatch.setattr(
            mcp_tool,
            "_read_only_registry_attestation",
            lambda names: tuple((name, "replacement-handler") for name in names),
        )
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", lambda coro, **_kw: asyncio.run(coro))
    with pytest.raises(RuntimeError, match="provenance"):
        capability.call({"query": "P1"}, deadline=time.monotonic() + 1)
