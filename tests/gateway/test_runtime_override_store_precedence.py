from __future__ import annotations

import threading
from types import SimpleNamespace

from gateway.session import SessionStore


SESSION_KEY = "agent:main:local:dm"


def _store_with_entry() -> tuple[SessionStore, SimpleNamespace]:
    entry = SimpleNamespace(
        model_override=None,
        runtime_model=None,
        runtime_provider=None,
        runtime_reasoning_effort=None,
    )
    store = object.__new__(SessionStore)
    store._lock = threading.Lock()
    store._loaded = True
    store._entries = {SESSION_KEY: entry}
    store._save = lambda: None
    return store, entry


def test_runtime_route_write_clears_older_model_override_only():
    # Given
    store, entry = _store_with_entry()
    entry.model_override = {"model": "slash-model", "provider": "slash-provider"}
    entry.runtime_reasoning_effort = "high"
    update_runtime_override = getattr(store, "update_runtime_override", None)

    # When
    assert callable(update_runtime_override)
    updated = update_runtime_override(
        SESSION_KEY,
        model="runtime-model",
        provider="runtime-provider",
    )

    # Then
    assert updated is True
    assert entry.model_override is None
    assert entry.runtime_model == "runtime-model"
    assert entry.runtime_provider == "runtime-provider"
    assert entry.runtime_reasoning_effort == "high"


def test_model_override_write_clears_older_runtime_route_only():
    # Given
    store, entry = _store_with_entry()
    entry.runtime_model = "runtime-model"
    entry.runtime_provider = "runtime-provider"
    entry.runtime_reasoning_effort = "low"

    # When
    store.set_model_override(
        SESSION_KEY,
        {
            "model": "slash-model",
            "provider": "slash-provider",
            "api_key": "must-not-persist",
            "api_mode": "codex_responses",
        },
    )

    # Then
    assert entry.model_override == {
        "model": "slash-model",
        "provider": "slash-provider",
    }
    assert entry.runtime_model is None
    assert entry.runtime_provider is None
    assert entry.runtime_reasoning_effort == "low"
