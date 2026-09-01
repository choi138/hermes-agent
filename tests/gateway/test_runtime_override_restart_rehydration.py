from __future__ import annotations

from types import SimpleNamespace

import pytest

import gateway.run as gateway_run


SESSION_KEY = "agent:main:local:dm"
DEFAULT_MODEL = "default-model"
DEFAULT_RUNTIME = {
    "api_key": "default-secret",
    "base_url": "https://default.example/v1",
    "provider": "default-provider",
    "api_mode": "chat_completions",
    "command": "default-command",
    "args": ["default-arg"],
    "credential_pool": "default-pool",
}


class _FakeStore:
    def __init__(
        self,
        *,
        runtime_model: str | None,
        runtime_provider: str | None,
        runtime_reasoning_effort: str | None = None,
    ) -> None:
        self.entry = SimpleNamespace(
            runtime_model=runtime_model,
            runtime_provider=runtime_provider,
            runtime_reasoning_effort=runtime_reasoning_effort,
        )

    def get_model_override(self, session_key: str) -> None:
        assert session_key == SESSION_KEY
        return None

    def get_entry(self, session_key: str) -> SimpleNamespace:
        assert session_key == SESSION_KEY
        return self.entry


def _runner(store: _FakeStore) -> gateway_run.GatewayRunner:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = store
    runner.config = None
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._reasoning_config = {"enabled": True, "effort": "medium"}
    runner._load_reasoning_config = lambda model="": {
        "enabled": True,
        "effort": "medium",
    }
    return runner


def _resolve(runner: gateway_run.GatewayRunner) -> tuple[str, dict]:
    return runner._resolve_session_agent_runtime(
        session_key=SESSION_KEY,
        user_config={
            "model": {
                "default": DEFAULT_MODEL,
                "provider": DEFAULT_RUNTIME["provider"],
            }
        },
    )


def _runtime_bundle(
    *,
    api_key: str,
    provider: str,
    base_url: str,
    api_mode: str,
) -> dict:
    return {
        "api_key": api_key,
        "base_url": base_url,
        "provider": provider,
        "api_mode": api_mode,
        "command": "target-command",
        "args": ["target-arg"],
        "credential_pool": "target-pool",
    }


def test_persisted_runtime_route_rehydrates_one_keyed_bundle(monkeypatch):
    # Given
    runner = _runner(
        _FakeStore(
            runtime_model="target-model",
            runtime_provider="target-provider",
        )
    )
    calls = []

    def resolve_provider(**kwargs):
        calls.append(kwargs)
        return _runtime_bundle(
            api_key="target-secret",
            provider="target-provider",
            base_url="https://target.example/v1",
            api_mode="codex_responses",
        )

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_provider,
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: pytest.fail("coherent persisted bundle must bypass defaults"),
    )

    # When
    model, runtime = _resolve(runner)

    # Then
    assert calls == [{"requested": "target-provider", "target_model": "target-model"}]
    assert model == "target-model"
    # Upstream's provider-bundle enrichment rides along: requested_provider /
    # request_overrides pass through (None from this mock) and capabilities
    # normalizes to a dict.
    assert runtime == _runtime_bundle(
        api_key="target-secret",
        provider="target-provider",
        base_url="https://target.example/v1",
        api_mode="codex_responses",
    ) | {
        "requested_provider": None,
        "request_overrides": None,
        "capabilities": {},
    }


def test_persisted_runtime_route_accepts_atomic_keyless_bundle(monkeypatch):
    # Given
    runner = _runner(
        _FakeStore(runtime_model="local-model", runtime_provider="local")
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: _runtime_bundle(
            api_key="",
            provider="local",
            base_url="http://127.0.0.1:11434/v1",
            api_mode="chat_completions",
        ),
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: pytest.fail("empty api_key is still a complete provider bundle"),
    )

    # When
    model, runtime = _resolve(runner)

    # Then
    assert model == "local-model"
    assert runtime["api_key"] == ""
    assert runtime["base_url"] == "http://127.0.0.1:11434/v1"
    assert runtime["provider"] == "local"


def test_resolved_provider_identity_and_target_model_mode_win(monkeypatch):
    # Given
    runner = _runner(
        _FakeStore(
            runtime_model="anthropic-family-model",
            runtime_provider="CUSTOM:MY_PROXY",
        )
    )
    seen = []

    def resolve_provider(**kwargs):
        seen.append(kwargs)
        return _runtime_bundle(
            api_key="proxy-secret",
            provider="custom",
            base_url="https://proxy.example/anthropic",
            api_mode="anthropic_messages",
        )

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_provider,
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: pytest.fail("normalized resolved bundle must be authoritative"),
    )

    # When
    model, runtime = _resolve(runner)

    # Then
    assert seen == [
        {
            "requested": "CUSTOM:MY_PROXY",
            "target_model": "anthropic-family-model",
        }
    ]
    assert model == "anthropic-family-model"
    assert runtime["provider"] == "custom"
    assert runtime["api_mode"] == "anthropic_messages"


@pytest.mark.parametrize(
    ("runtime_model", "runtime_provider"),
    [(None, "target-provider"), ("target-model", None), ("", "target-provider")],
)
def test_incomplete_runtime_route_stays_dormant(
    monkeypatch,
    runtime_model,
    runtime_provider,
):
    # Given
    store = _FakeStore(
        runtime_model=runtime_model,
        runtime_provider=runtime_provider,
    )
    runner = _runner(store)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: pytest.fail("incomplete persisted labels must not resolve"),
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: dict(DEFAULT_RUNTIME),
    )

    # When
    model, runtime = _resolve(runner)

    # Then
    assert (model, runtime) == (DEFAULT_MODEL, DEFAULT_RUNTIME)
    assert runner._session_model_overrides == {}
    assert store.entry.runtime_model == runtime_model
    assert store.entry.runtime_provider == runtime_provider


def test_unresolved_runtime_route_fails_closed_and_stays_dormant(monkeypatch):
    # Given
    store = _FakeStore(
        runtime_model="target-model",
        runtime_provider="missing-provider",
    )
    runner = _runner(store)

    def reject_provider(**kwargs):
        raise RuntimeError("provider is no longer configured")

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        reject_provider,
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: dict(DEFAULT_RUNTIME),
    )

    # When
    model, runtime = _resolve(runner)

    # Then
    assert (model, runtime) == (DEFAULT_MODEL, DEFAULT_RUNTIME)
    assert runner._session_model_overrides == {}
    assert store.entry.runtime_model == "target-model"
    assert store.entry.runtime_provider == "missing-provider"


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        ("high", {"enabled": True, "effort": "high"}),
        ("none", {"enabled": False}),
        ("bogus", {"enabled": True, "effort": "medium"}),
    ],
)
def test_persisted_reasoning_rehydrates_only_valid_values(persisted, expected):
    # Given
    runner = _runner(
        _FakeStore(
            runtime_model=None,
            runtime_provider=None,
            runtime_reasoning_effort=persisted,
        )
    )

    # When
    reasoning = runner._resolve_session_reasoning_config(session_key=SESSION_KEY)

    # Then
    assert reasoning == expected
    if persisted == "bogus":
        assert runner._session_reasoning_overrides == {}
