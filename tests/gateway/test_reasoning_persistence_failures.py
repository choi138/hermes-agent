"""Explicit reasoning selections must survive a real store reload or fail cleanly."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import invoke_tool
from agent.runtime_control import snapshot_runtime
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from run_agent import AIAgent
from tools.registry import invalidate_check_fn_cache, registry


PIN = {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": True}
RELEASE = {"operation": "release_reasoning", "user_requested": True}
CFG = {"agent": {"reasoning_effort": "high"},
       "providers": {"test": {"base_url": "https://local.invalid/v1"}},
       "model_routes": {"routes": {
           "dev": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "max"},
           "chat": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "medium"}}}}


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")

    for target in ("socket.socket.connect", "socket.socket.connect_ex",
                   "socket.create_connection", "socket.getaddrinfo"):
        monkeypatch.setattr(target, deny)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: CFG)
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: CFG)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 128000)
    import tools.runtime_control_tool  # register the production schema
    invalidate_check_fn_cache()


def runner_for(directory, sqlite=False):
    runner = object.__new__(GatewayRunner)
    cfg = GatewayConfig()
    cfg.sessions_dir = directory
    runner.session_store = SessionStore(sessions_dir=directory, config=cfg)
    if not sqlite:
        runner.session_store._db = None
    else:
        assert runner.session_store._routing_db is not None
    runner._evict_cached_agent = MagicMock()
    return runner


def make_agent(runner, key):
    with patch("run_agent.get_tool_definitions", return_value=registry.get_definitions({"model_switch"})), \
         patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
        agent = AIAgent(model="gpt-6-astra", provider="test", api_key="offline-only",
                        base_url="https://local.invalid/v1", api_mode="codex_responses",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        reasoning_config=runner._resolve_session_reasoning_config(session_key=key, model="gpt-6-astra"))
    runner._bind_runtime_update_callback(agent, key)
    return agent


def setup_session(directory, sqlite=False):
    runner = runner_for(directory, sqlite)
    source = SessionSource(platform=Platform.LOCAL, chat_id="durability", chat_type="dm", user_id="owner")
    key = runner.session_store.get_or_create_session(source).session_key
    return runner, key, make_agent(runner, key)


def call(agent, args, sequential=False):
    if not sequential:
        return json.loads(invoke_tool(agent, "model_switch", copy.deepcopy(args), "offline-f1"))
    tool = SimpleNamespace(id="f1", type="function", function=SimpleNamespace(name="model_switch", arguments=json.dumps(args)))
    messages = []
    agent._execute_tool_calls_sequential(SimpleNamespace(content="", tool_calls=[tool], reasoning=None), messages, "offline-f1")
    assert messages[-1]["role"] == "tool"
    return json.loads(messages[-1]["content"])


@pytest.mark.parametrize("operation", [PIN, RELEASE], ids=["pin", "release"])
def test_json_write_failure_preserves_live_cache_disk_and_retry(tmp_path, operation):
    runner, key, agent = setup_session(tmp_path / "sessions")
    if operation == RELEASE:
        assert call(agent, PIN)["success"]
    agent._runtime_turn_restore_snapshot = snapshot_runtime(agent)
    old = copy.deepcopy(agent.reasoning_config)
    old_primary = copy.deepcopy(agent._primary_runtime)
    old_turn = copy.deepcopy(agent._runtime_turn_restore_snapshot)
    old_source = getattr(agent, "_runtime_reasoning_source", None)
    entry = runner.session_store.get_entry(key)
    old_entry = entry.to_dict()
    old_override = copy.deepcopy(runner._session_state(key).conversation.reasoning_override)
    disk = runner.session_store.sessions_dir / "sessions.json"
    before = disk.read_bytes()
    runner.session_store.sessions_dir.chmod(0o500)
    try:
        failed = call(agent, operation, sequential=True)
    finally:
        runner.session_store.sessions_dir.chmod(0o700)
    assert failed["success"] is False, failed
    assert "persistence failed" in failed["error"].lower()
    assert str(tmp_path) not in failed["error"]
    assert "not changed" in failed["error"].lower()
    assert failed["changed"] == []
    assert agent.reasoning_config == old
    assert agent._primary_runtime == old_primary
    assert agent._runtime_turn_restore_snapshot == old_turn
    assert getattr(agent, "_runtime_reasoning_source", None) == old_source
    assert runner._session_state(key).conversation.reasoning_override == old_override
    assert runner.session_store.get_entry(key) is entry
    assert entry.to_dict() == old_entry
    assert disk.read_bytes() == before
    assert make_agent(runner, key).reasoning_config == old
    assert make_agent(runner_for(disk.parent), key).reasoning_config == old
    # An unrelated subsequent save must not flush a failed pin/release from cache.
    assert runner.session_store.update_runtime_override(key, provider="test")
    assert make_agent(runner_for(disk.parent), key).reasoning_config == old
    assert call(agent, operation)["success"]
    assert make_agent(runner_for(disk.parent), key).reasoning_config == agent.reasoning_config


@pytest.mark.parametrize("sqlite,mirror_failure", [(False, False), (True, False), (True, True)])
def test_healthy_durable_store_and_optional_mirror(tmp_path, sqlite, mirror_failure):
    runner, key, agent = setup_session(tmp_path / "sessions", sqlite)
    disk = runner.session_store.sessions_dir / "sessions.json"
    for operation in (PIN, RELEASE):
        before = disk.read_bytes()
        if mirror_failure:
            disk.parent.chmod(0o500)
        try:
            assert call(agent, operation, sequential=True)["success"]
        finally:
            disk.parent.chmod(0o700)
        if mirror_failure:
            assert disk.read_bytes() == before
        rebuilt = make_agent(runner_for(disk.parent, sqlite), key)
        assert rebuilt.reasoning_config == agent.reasoning_config
        assert (rebuilt.reasoning_config.get("selection") == "pinned") == (operation == PIN)


@pytest.mark.parametrize("unavailable", ["store", "entry", "updater", "callback", "empty_key", "unacknowledged"])
@pytest.mark.parametrize("operation", [PIN, RELEASE], ids=["pin", "release"])
def test_missing_persistence_cannot_report_success(tmp_path, unavailable, operation):
    runner, key, agent = setup_session(tmp_path / "sessions")
    if operation == RELEASE:
        assert call(agent, PIN)["success"]
    old = copy.deepcopy(agent.reasoning_config)
    if unavailable == "store":
        runner.session_store = None
    elif unavailable == "entry":
        runner._bind_runtime_update_callback(agent, "missing-session")
    elif unavailable == "updater":
        runner.session_store.update_runtime_override = None
    elif unavailable == "empty_key":
        runner._bind_runtime_update_callback(agent, "")
    elif unavailable == "unacknowledged":
        runner.session_store.update_runtime_override = lambda *a, **kw: None
    else:
        agent.runtime_update_callback = None
    failed = call(agent, operation)
    assert failed["success"] is False
    assert agent.reasoning_config == old


def test_callback_exception_is_sanitized_and_live_selection_unchanged(tmp_path):
    runner, key, agent = setup_session(tmp_path / "sessions")
    old = copy.deepcopy(agent.reasoning_config)

    def failed_callback(**kwargs):
        raise OSError("/private/secret/path token=must-not-leak")

    agent.runtime_update_callback = failed_callback
    failed = call(agent, PIN)
    assert failed["success"] is False
    assert "must-not-leak" not in failed["error"]
    assert "/private/secret" not in failed["error"]
    assert agent.reasoning_config == old


def test_generic_persistence_remains_best_effort(tmp_path):
    runner, key, agent = setup_session(tmp_path / "sessions")
    runner.session_store.sessions_dir.chmod(0o500)
    try:
        # Existing generic helper/callback callers do not acquire strict semantics.
        runner._persist_session_runtime_override(key, reasoning_config={"effort": "low"}, include_reasoning=True)
        agent.runtime_update_callback(scope="session", reasoning_config={"effort": "medium"})
    finally:
        runner.session_store.sessions_dir.chmod(0o700)
    assert runner._session_state(key).conversation.reasoning_override == {"effort": "medium"}
