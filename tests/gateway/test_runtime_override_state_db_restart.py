from __future__ import annotations

import json

import pytest

import gateway.run as gateway_run
import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
    )


def _runner(store: SessionStore) -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    runner.config = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._load_reasoning_config = lambda model="": {
        "enabled": True,
        "effort": "medium",
    }
    return runner


def test_runtime_route_rehydrates_from_state_db_without_json(tmp_path, monkeypatch):
    # Given
    # Upstream #66887 pins the routing index to get_hermes_home()/state.db at
    # store construction, so the test home must move too — DEFAULT_DB_PATH
    # alone would split reads (tmp) from routing writes (real home).
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    sessions_dir = tmp_path / "sessions"
    config = GatewayConfig(write_sessions_json=False)
    first_store = SessionStore(sessions_dir=sessions_dir, config=config)
    entry = first_store.get_or_create_session(_source())
    session_key = entry.session_key
    assert first_store.update_runtime_override(
        session_key,
        model="restart-model",
        provider="restart-provider",
        reasoning_effort="high",
    )
    assert not (sessions_dir / "sessions.json").exists()
    first_db = first_store._db
    assert first_db is not None
    rows = first_db.load_gateway_routing_entries(
        scope=first_store._routing_scope()
    )
    durable = json.loads(rows[session_key])
    assert durable["runtime_model"] == "restart-model"
    assert durable["runtime_provider"] == "restart-provider"
    assert durable["runtime_reasoning_effort"] == "high"
    assert "api_key" not in durable
    assert "base_url" not in durable
    assert "api_mode" not in durable
    assert "fake-runtime-key" not in rows[session_key]
    first_db.close()

    restarted_store = SessionStore(sessions_dir=sessions_dir, config=config)
    restarted_db = restarted_store._db
    assert restarted_db is not None
    runner = _runner(restarted_store)

    def resolve_provider(**kwargs):
        assert kwargs == {
            "requested": "restart-provider",
            "target_model": "restart-model",
        }
        return {
            "api_key": "fake-runtime-key",
            "base_url": "https://restart.example/v1",
            "provider": "restart-provider",
            "api_mode": "codex_responses",
            "command": None,
            "args": [],
            "credential_pool": None,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_provider,
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: pytest.fail("DB-only restart must use the persisted route bundle"),
    )

    # When
    model, runtime = runner._resolve_session_agent_runtime(
        session_key=session_key,
        user_config={
            "model": {
                "default": "default-model",
                "provider": "default-provider",
            }
        },
    )
    reasoning = runner._resolve_session_reasoning_config(session_key=session_key)

    # Then
    assert model == "restart-model"
    assert runtime["provider"] == "restart-provider"
    assert runtime["base_url"] == "https://restart.example/v1"
    assert runtime["api_key"] == "fake-runtime-key"
    assert runtime["api_mode"] == "codex_responses"
    assert reasoning == {"enabled": True, "effort": "high"}
    restarted_db.close()
