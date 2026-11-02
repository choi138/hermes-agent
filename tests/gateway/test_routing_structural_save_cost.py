"""Structural routing saves must not rewrite unrelated conversations."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import hermes_constants
import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source(chat_id):
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="thread", user_id="owner")


def _store(tmp_path, monkeypatch, *, mirror=False):
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: str(tmp_path))
    return SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig(write_sessions_json=mirror))


def test_new_route_preserves_another_writers_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.get_or_create_session(_source("first"))
    external_key = "agent:main:discord:thread:external"
    external = json.dumps({"session_key": external_key, "session_id": "external-session"})
    store._db.save_gateway_routing_entry(external_key, external, scope=store._routing_scope())

    created = store.get_or_create_session(_source("second"))

    rows = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert rows[external_key] == external
    assert json.loads(rows[created.session_key])["session_id"] == created.session_id
    assert not (tmp_path / "sessions" / "sessions.json").exists()
    store._db.close()


def test_new_route_does_not_serialize_unrelated_entries(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    first = store.get_or_create_session(_source("first"))

    def unrelated_serialization():
        raise AssertionError("a new route serialized another conversation")

    monkeypatch.setattr(first, "to_dict", unrelated_serialization)
    created = store.get_or_create_session(_source("second"))
    assert created.session_key != first.session_key
    rows = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert set(rows) == {first.session_key, created.session_key}
    store._db.close()


def test_mirror_mode_keeps_structural_mapping_current(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, mirror=True)
    first = store.get_or_create_session(_source("first"))
    created = store.get_or_create_session(_source("second"))
    mirror = json.loads((tmp_path / "sessions" / "sessions.json").read_text())
    assert mirror[first.session_key]["session_id"] == first.session_id
    assert mirror[created.session_key]["session_id"] == created.session_id
    store._db.close()


def test_six_new_routes_survive_restart_after_concurrent_creation(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store._ensure_loaded()
    start = threading.Barrier(6)

    def create(index):
        start.wait(timeout=5)
        return store.get_or_create_session(_source(str(index)))

    with ThreadPoolExecutor(max_workers=6) as pool:
        entries = list(pool.map(create, range(6)))
    expected = {entry.session_key: entry.session_id for entry in entries}
    rows = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert {key: json.loads(value)["session_id"] for key, value in rows.items()} == expected
    store._db.close()
    restarted = _store(tmp_path, monkeypatch)
    assert {entry.session_key: entry.session_id for entry in (
        restarted.get_or_create_session(_source(str(index))) for index in range(6))} == expected
    restarted._db.close()
