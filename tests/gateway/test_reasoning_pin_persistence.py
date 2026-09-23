"""Explicit reasoning selections survive restart and fail without changing live state."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore


def _source():
    return SessionSource(platform=Platform.LOCAL, chat_id="reasoning-pin", user_id="owner")


def _runner(store):
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._load_reasoning_config = lambda model="": {"enabled": True, "effort": "high"}
    runner._evict_cached_agent = MagicMock()
    return runner


def test_session_pin_survives_restart_then_reset_clears_it(tmp_path):
    first = SessionStore(tmp_path, GatewayConfig())
    key = first.get_or_create_session(_source()).session_key
    runner = _runner(first)

    assert runner._apply_reasoning_selection(key, "local", "max")
    assert first.get_reasoning_override(key) == {
        "enabled": True, "effort": "max", "selection": "pinned",
    }
    assert runner._resolve_session_reasoning_config(session_key=key)["selection"] == "pinned"

    restarted = _runner(SessionStore(tmp_path, GatewayConfig()))
    assert restarted._resolve_session_reasoning_config(session_key=key) == {
        "enabled": True, "effort": "max", "selection": "pinned",
    }
    restarted.session_store.reset_session(key)
    restarted._clear_conversation_scope(key, reason="new")
    assert _runner(SessionStore(tmp_path, GatewayConfig()))._resolve_session_reasoning_config(
        session_key=key
    ) == {"enabled": True, "effort": "high"}


def test_failed_pin_and_release_leave_memory_and_disk_unchanged(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, GatewayConfig())
    key = store.get_or_create_session(_source()).session_key
    runner = _runner(store)
    assert runner._apply_reasoning_selection(key, "local", "max")
    before = store._entries[key].to_dict()
    original = runner._resolve_session_reasoning_config(session_key=key).copy()

    def fail(*args, **kwargs):
        raise OSError("offline write error")

    monkeypatch.setattr(store, "_persist_routing_data", fail)
    assert "not changed" in runner._apply_reasoning_selection(key, "local", "low")
    assert "not changed" in runner._apply_reasoning_selection(key, "local", "reset")
    assert store._entries[key].to_dict() == before
    assert runner._resolve_session_reasoning_config(session_key=key) == original
    assert SessionStore(tmp_path, GatewayConfig()).get_reasoning_override(key) == original


def test_previous_production_row_restores_pin_without_secret_fields():
    now = datetime.now(timezone.utc).isoformat()
    entry = SessionEntry.from_dict({
        "session_key": "agent:main:local:reasoning-pin", "session_id": "session-1",
        "created_at": now, "updated_at": now,
        "runtime_reasoning_effort": "max", "runtime_reasoning_selection": "pinned",
    })
    assert entry.runtime_reasoning_effort == "max"
    assert entry.runtime_reasoning_selection == "pinned"
    assert entry.to_dict()["runtime_reasoning_selection"] == "pinned"
    assert "api_key" not in entry.to_dict()


def test_missing_session_cannot_acknowledge_pin(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    assert store.set_reasoning_override("missing", {"effort": "max", "selection": "pinned"}) is False
    with pytest.raises(ValueError):
        store.set_reasoning_override("missing", {"effort": "unknown", "selection": "pinned"})
