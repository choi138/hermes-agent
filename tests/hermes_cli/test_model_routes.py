"""Tests for hermes_cli.model_routes (ADR-003 Phase 1).

No network: probe tests monkeypatch ``mr._urlopen`` / ``mr._now``.  Health
tests that must exercise real probe logic opt out of the pytest guard via
``HERMES_MODEL_ROUTES_HEALTH_TEST=1``.  All disk IO stays inside the hermetic
``HERMES_HOME`` tmpdir set up by tests/conftest.py.
"""

import errno
import io
import json
import os
import threading
import urllib.error
from pathlib import Path

import pytest

import hermes_cli.config as config_mod
import hermes_cli.model_routes as mr
from hermes_cli.config import validate_config_structure
from hermes_constants import get_hermes_home


# =============================================================================
# Helpers
# =============================================================================


def _providers():
    return {
        "p1": {"base_url": "https://p1.example/v1"},
        "p2": {"base_url": "https://p2.example/v1"},
        "p3": {"base_url": "https://p3.example/v1"},
    }


def _cfg(routes=None, health=None, static_rules=None, router=None, providers=None, model_routes=...):
    cfg = {"providers": _providers() if providers is None else providers}
    if model_routes is not ...:
        cfg["model_routes"] = model_routes
        return cfg
    section = {}
    if routes is not None:
        section["routes"] = routes
    if health is not None:
        section["health"] = health
    if static_rules is not None:
        section["static_rules"] = static_rules
    if router is not None:
        section["router"] = router
    if section:
        cfg["model_routes"] = section
    return cfg


def _route(provider="p1", model="model-a", **extra):
    entry = {"description": "test route", "provider": provider, "model": model}
    entry.update(extra)
    return entry


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError(
        "https://x.example/v1/models", code, f"HTTP {code}", None, io.BytesIO(body)
    )


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _health(tmp_path, **kwargs):
    return mr.HealthConfig(cache_path=str(tmp_path / "health.json"), **kwargs)


def _seed_verdict(health, provider, healthy, ts, reason="seeded"):
    """Write a cache entry directly — passive-first provider_health only
    probes on a stale *unhealthy* verdict, so probe-path tests seed one."""
    path = health.resolved_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mtime_ns = path.stat().st_mtime_ns if path.exists() else None
    cache = {}
    if path.exists():
        cache = json.loads(path.read_text(encoding="utf-8"))
    cache[str(provider)] = {"healthy": bool(healthy), "reason": reason, "ts": float(ts)}
    path.write_text(json.dumps(cache), encoding="utf-8")
    # The memo contract keys on mtime; keep fixture writes distinct even on
    # filesystems that coalesce immediate timestamp updates.
    if previous_mtime_ns is not None and path.stat().st_mtime_ns <= previous_mtime_ns:
        os.utime(path, ns=(path.stat().st_atime_ns, previous_mtime_ns + 1))


def _seed_stale_unhealthy(monkeypatch, health, provider="p1"):
    """Seed an expired unhealthy verdict and pin the clock past its fail TTL,
    arming the recovery-probe path for the next provider_health call."""
    clock = _Clock(1000.0)
    monkeypatch.setattr(mr, "_now", clock)
    _seed_verdict(health, provider, healthy=False, ts=clock.t - health.fail_ttl_seconds - 1)
    return clock


def _patch_resolve(monkeypatch, runtime=None, exc=None):
    """Patch resolve_runtime_provider at model_routes' deferred import site."""

    def fake(**kwargs):
        if exc is not None:
            raise exc
        return dict(runtime or {})

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", fake
    )


_OPENAI_RUNTIME = {
    "base_url": "https://x.example/v1",
    "api_key": "k",
    "api_mode": "chat_completions",
}


def _errors(catalog):
    return [i for i in catalog.issues if i.severity == "error"]


def _warnings(catalog):
    return [i for i in catalog.issues if i.severity == "warning"]


@pytest.fixture
def health_test_env(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH_TEST", "1")


# =============================================================================
# Loader / validation
# =============================================================================


def test_absent_section_dormant():
    catalog = mr.load_routes(_cfg())
    assert catalog.routes == {}
    assert catalog.issues == []
    assert catalog.static_rules == []
    assert catalog.health.enabled is True
    assert catalog.health.ok_ttl_seconds == mr.DEFAULT_OK_TTL_SECONDS
    assert catalog.health.fail_ttl_seconds == mr.DEFAULT_FAIL_TTL_SECONDS
    assert catalog.health.probe_timeout_seconds == mr.DEFAULT_PROBE_TIMEOUT_SECONDS


@pytest.mark.parametrize("section", [{}, {"routes": {}}, None])
def test_empty_section_dormant(section):
    catalog = mr.load_routes(_cfg(model_routes=section))
    assert catalog.routes == {}
    assert catalog.issues == []
    assert catalog.static_rules == []


def test_valid_catalog_loads():
    routes = {
        "dev": _route(
            provider="p1",
            model="model-a",
            reasoning_effort="xhigh",
            accepted=["model-a", "model-b"],
            fallbacks=[
                {"provider": "p2", "model": "model-b", "reasoning_effort": "high"},
                {"provider": "p3", "model": "model-c"},
            ],
        ),
        "chat": _route(provider="p2", model="model-b"),
    }
    catalog = mr.load_routes(_cfg(routes=routes))
    assert catalog.issues == []
    assert list(catalog.routes) == ["dev", "chat"]  # declaration order
    dev = catalog.routes["dev"]
    assert dev.name == "dev"
    assert dev.description == "test route"
    assert dev.provider == "p1"
    assert dev.model == "model-a"
    assert dev.reasoning_effort == "xhigh"
    assert dev.accepted == ("model-a", "model-b")
    assert dev.fallbacks == (
        mr.FallbackSpec(provider="p2", model="model-b", reasoning_effort="high"),
        mr.FallbackSpec(provider="p3", model="model-c", reasoning_effort=""),
    )


def test_unknown_provider_is_error_and_route_dropped():
    catalog = mr.load_routes(_cfg(routes={"dev": _route(provider="nope")}))
    errors = _errors(catalog)
    assert len(errors) == 1
    assert "dev" in errors[0].message and "nope" in errors[0].message
    assert errors[0].hint
    assert "dev" not in catalog.routes


def test_builtin_provider_accepted():
    catalog = mr.load_routes(
        _cfg(routes={"dev": _route(provider="anthropic")}, providers={})
    )
    assert _errors(catalog) == []
    assert "dev" in catalog.routes


def test_fallback_unknown_provider_error():
    routes = {
        "dev": _route(fallbacks=[
            {"provider": "p2", "model": "m"},
            {"provider": "glm-voy-typo", "model": "m"},
        ]),
    }
    catalog = mr.load_routes(_cfg(routes=routes))
    errors = _errors(catalog)
    assert len(errors) == 1
    assert "dev" in errors[0].message
    assert "#2" in errors[0].message
    assert "glm-voy-typo" in errors[0].message
    assert "dev" not in catalog.routes


@pytest.mark.parametrize(
    "effort,ok",
    # "ultra" joined VALID_REASONING_EFFORTS upstream (v2026.7.20, 7550c594c);
    # "turbo" keeps the invalid-token negative case.
    [("turbo", False), ("ultra", True), ("max", True), ("none", True), (None, True),
     ("xhigh", True), (False, True), (True, True)],
)
def test_bad_reasoning_effort_error(effort, ok):
    entry = _route()
    if effort is not None:
        entry["reasoning_effort"] = effort
    catalog = mr.load_routes(_cfg(routes={"dev": entry}))
    if ok:
        assert _errors(catalog) == []
        assert "dev" in catalog.routes
    else:
        assert any("reasoning_effort" in i.message for i in _errors(catalog))
        assert "dev" not in catalog.routes


def test_yaml_bool_reasoning_effort_normalization():
    """YAML 1.1 bools mirror parse_reasoning_effort: `off`/`no`/`false` (bool
    False) disables reasoning ("none"); `on`/`yes`/`true` (bool True) is
    treated as unspecified — neither drops the route."""
    catalog = mr.load_routes(_cfg(routes={
        "dev": _route(reasoning_effort=False),
        "chat": _route(reasoning_effort=True),
    }))
    assert _errors(catalog) == []
    assert catalog.routes["dev"].reasoning_effort == "none"
    assert catalog.routes["chat"].reasoning_effort == ""


def test_case_insensitive_duplicate_route_names_error():
    catalog = mr.load_routes(_cfg(routes={"dev": _route(), "DEV": _route(provider="p2")}))
    errors = _errors(catalog)
    assert len(errors) == 1
    assert catalog.routes == {}


@pytest.mark.parametrize("missing", ["provider", "model"])
def test_missing_model_or_provider_error(missing):
    entry = _route()
    del entry[missing]
    catalog = mr.load_routes(_cfg(routes={"dev": entry}))
    assert any(missing in i.message for i in _errors(catalog))
    assert "dev" not in catalog.routes


@pytest.mark.parametrize(
    "cfg",
    [
        _cfg(model_routes=["not", "a", "mapping"]),
        _cfg(model_routes={"routes": ["dev"]}),
        _cfg(routes={"dev": "just-a-string"}),
        _cfg(routes={"dev": _route(accepted="model-a")}),
        _cfg(routes={"dev": _route(accepted=["model-a", 7])}),
        _cfg(routes={"dev": _route(fallbacks={"provider": "p2"})}),
        _cfg(routes={"dev": _route(fallbacks=["p2:model-b"])}),
    ],
)
def test_shape_errors(cfg):
    catalog = mr.load_routes(cfg)
    assert _errors(catalog)
    assert catalog.routes == {}


def test_unknown_keys_warn():
    routes = {
        "dev": _route(
            surprise=1,
            fallbacks=[{"provider": "p2", "model": "m", "weight": 3}],
        ),
    }
    cfg = _cfg(routes=routes)
    cfg["model_routes"]["mystery"] = True
    catalog = mr.load_routes(cfg)
    warnings = [i.message for i in _warnings(catalog)]
    assert any("surprise" in m for m in warnings)
    assert any("weight" in m for m in warnings)
    assert any("mystery" in m for m in warnings)
    assert _errors(catalog) == []
    assert "dev" in catalog.routes


def test_health_defaults_partial_override_and_bad_types():
    # Absent block → defaults.
    assert mr.load_routes(_cfg(routes={"dev": _route()})).health == mr.HealthConfig()

    # Partial override keeps the other defaults.
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, health={"ok_ttl_seconds": 60}))
    assert catalog.health.ok_ttl_seconds == 60
    assert catalog.health.fail_ttl_seconds == mr.DEFAULT_FAIL_TTL_SECONDS
    assert catalog.health.probe_timeout_seconds == mr.DEFAULT_PROBE_TIMEOUT_SECONDS
    assert catalog.issues == []

    # Bad type / bad value → warning + default retained (never an error).
    catalog = mr.load_routes(_cfg(
        routes={"dev": _route()},
        health={"probe_timeout_seconds": "fast", "ok_ttl_seconds": -5},
    ))
    assert catalog.health.probe_timeout_seconds == mr.DEFAULT_PROBE_TIMEOUT_SECONDS
    assert catalog.health.ok_ttl_seconds == mr.DEFAULT_OK_TTL_SECONDS
    assert len(_warnings(catalog)) == 2
    assert _errors(catalog) == []
    assert "dev" in catalog.routes

    # Empty cache_path resolves under get_hermes_home(), not a hardcoded home.
    resolved = mr.HealthConfig(cache_path="").resolved_cache_path()
    assert resolved == get_hermes_home() / "state" / "model_route_health.json"
    assert str(resolved).endswith(os.path.join("state", "model_route_health.json"))


@pytest.mark.parametrize("bad", ["false", "off", "no", "true", 0, 1, None])
def test_health_enabled_non_bool_warns_and_defaults(bad):
    # bool("false") is True — a quoted YAML string must not silently keep
    # probing enabled; non-bool values warn and fall back to the default.
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, health={"enabled": bad}))
    assert catalog.health.enabled is True
    assert any("health.enabled" in i.message for i in _warnings(catalog))
    assert _errors(catalog) == []


def test_health_enabled_bool_accepted_without_warning():
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, health={"enabled": False}))
    assert catalog.health.enabled is False
    assert catalog.issues == []


def test_static_rules_parse_only():
    rule = {"route": "dev", "when": {"channel": "pr", "sender": "any"}, "reason": "pin"}
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=[rule]))
    assert catalog.static_rules == [rule]
    assert catalog.issues == []

    # Unknown route → error, rule dropped.
    catalog = mr.load_routes(_cfg(
        routes={"dev": _route()},
        static_rules=[{"route": "ghost", "when": {"channel": "pr"}}],
    ))
    assert catalog.static_rules == []
    assert any("ghost" in i.message for i in _errors(catalog))

    # Non-mapping item → error.
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=["dev"]))
    assert catalog.static_rules == []
    assert _errors(catalog)

    # Missing/empty when → error.
    for bad_when in ({}, None):
        item = {"route": "dev"}
        if bad_when is not None:
            item["when"] = bad_when
        catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=[item]))
        assert catalog.static_rules == []
        assert any("when" in i.message for i in _errors(catalog))

    # Extra top-level rule keys → warning, rule kept.
    rule = {"route": "dev", "when": {"anything": 1}, "priority": 9}
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=[rule]))
    assert catalog.static_rules == [rule]
    assert any("priority" in i.message for i in _warnings(catalog))
    assert _errors(catalog) == []


def test_static_rule_is_owner_non_bool_operand_warns():
    # Same footgun class as health.enabled: YAML string "false" is truthy.
    # The rule is kept (it just never matches) but a warning is surfaced.
    for bad in ({"eq": "false"}, {"eq": 1}, {"eq": None}, "false", {"in": [True]}):
        rule = {"route": "dev", "when": {"is_owner": bad}}
        catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=[rule]))
        assert catalog.static_rules == [rule]
        assert any(
            "is_owner" in i.message and "never match" in i.message
            for i in _warnings(catalog)
        ), bad
        assert _errors(catalog) == []

    # Real booleans pass without a warning.
    for good in (True, False):
        rule = {"route": "dev", "when": {"is_owner": {"eq": good}}}
        catalog = mr.load_routes(_cfg(routes={"dev": _route()}, static_rules=[rule]))
        assert catalog.static_rules == [rule]
        assert not any("is_owner" in i.message for i in _warnings(catalog))


def test_default_config_and_known_root_keys():
    assert config_mod.DEFAULT_CONFIG["model_routes"] == {}
    assert "model_routes" in config_mod._KNOWN_ROOT_KEYS


def test_validate_config_structure_surfaces_route_issues():
    issues = validate_config_structure({
        "model_routes": {"routes": {"dev": {"provider": "no-such-provider", "model": "m"}}},
        "providers": {},
    })
    assert any(
        i.severity == "error" and "no-such-provider" in i.message for i in issues
    )

    issues = validate_config_structure({"providers": {}})
    assert not any("model_routes" in i.message for i in issues)


def _default_route_issues(cfg):
    return [i for i in mr.validate_model_routes(cfg) if "default_route" in i.message]


def test_default_route_declared_is_silent():
    for name in ("dev", "DEV", "  dev  "):  # lookup is case-insensitive, whitespace-tolerant
        cfg = _cfg(routes={"dev": _route()})
        cfg["delegation"] = {"default_route": name}
        assert _default_route_issues(cfg) == [], name


def test_default_route_empty_or_absent_is_silent():
    for delegation in (None, {}, {"default_route": ""}, {"default_route": "  "}):
        cfg = _cfg(routes={"dev": _route()})
        if delegation is not None:
            cfg["delegation"] = delegation
        assert _default_route_issues(cfg) == [], delegation


def test_default_route_undeclared_warns():
    cfg = _cfg(routes={"dev": _route()})
    cfg["delegation"] = {"default_route": "nope"}
    hits = _default_route_issues(cfg)
    assert hits and all(i.severity == "warning" for i in hits)

    # No model_routes section at all: any default_route is undeclared.
    cfg = {"providers": _providers(), "delegation": {"default_route": "dev"}}
    assert any(i.severity == "warning" for i in _default_route_issues(cfg))

    # A route dropped by validation errors does NOT count as declared.
    cfg = _cfg(routes={"dev": {"provider": "no-such-provider", "model": "m"}})
    cfg["delegation"] = {"default_route": "dev"}
    assert any(i.severity == "warning" for i in _default_route_issues(cfg))


def test_default_route_non_string_warns():
    for bad in (["dev"], 7, True):
        cfg = _cfg(routes={"dev": _route()})
        cfg["delegation"] = {"default_route": bad}
        hits = _default_route_issues(cfg)
        assert hits and all(i.severity == "warning" for i in hits), bad


def test_validate_config_structure_surfaces_default_route_warning():
    issues = validate_config_structure({
        "providers": _providers(),
        "model_routes": {"routes": {"dev": _route()}},
        "delegation": {"default_route": "nope"},
    })
    assert any(
        i.severity == "warning" and "default_route" in i.message for i in issues
    )


def test_load_routes_end_to_end_real_config():
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "providers:\n"
        "  p1:\n"
        "    base_url: https://p1.example/v1\n"
        "model_routes:\n"
        "  routes:\n"
        "    dev:\n"
        "      description: dev work\n"
        "      provider: p1\n"
        "      model: model-a\n",
        encoding="utf-8",
    )
    catalog = mr.load_routes(None)
    assert "dev" in catalog.routes
    assert catalog.routes["dev"].provider == "p1"
    assert catalog.routes["dev"].model == "model-a"
    assert _errors(catalog) == []


# =============================================================================
# Resolution walk
# =============================================================================


_CHAIN_ROUTES = {
    "dev": _route(
        provider="p1",
        model="model-a",
        reasoning_effort="xhigh",
        fallbacks=[
            {"provider": "p2", "model": "model-b", "reasoning_effort": "high"},
            {"provider": "p3", "model": "model-c"},
        ],
    ),
}


def _patch_health_map(monkeypatch, verdicts):
    """Scripted per-provider health; records call order."""
    calls = []

    def fake(
        provider, model="", *, cfg=None, health=None, allow_recovery_probe=False,
    ):
        calls.append(provider)
        return verdicts[provider]

    monkeypatch.setattr(mr, "provider_health", fake)
    return calls


def test_resolve_healthy_default(monkeypatch):
    calls = _patch_health_map(monkeypatch, {"p1": (True, "HTTP 200")})
    result = mr.resolve_route("dev", _cfg(routes=_CHAIN_ROUTES))
    assert result == {
        "route": "dev",
        "provider": "p1",
        "model": "model-a",
        "reasoning_effort": "xhigh",
        "source": "default",
        "reason": "",
    }
    assert all(isinstance(v, str) for v in result.values())
    assert calls == ["p1"]


def test_resolve_default_down_first_fallback(monkeypatch):
    calls = _patch_health_map(monkeypatch, {
        "p1": (False, "HTTP 402"),
        "p2": (True, "HTTP 200"),
    })
    result = mr.resolve_route("dev", _cfg(routes=_CHAIN_ROUTES))
    assert result["source"] == "fallback:1"
    assert result["provider"] == "p2"
    assert result["model"] == "model-b"
    assert result["reasoning_effort"] == "high"
    assert "failover" in result["reason"]
    assert "p1" in result["reason"] and "HTTP 402" in result["reason"]
    assert calls == ["p1", "p2"]


def test_resolve_skips_multiple_unhealthy(monkeypatch):
    calls = _patch_health_map(monkeypatch, {
        "p1": (False, "HTTP 402"),
        "p2": (False, "HTTP 503"),
        "p3": (True, "HTTP 200"),
    })
    result = mr.resolve_route("dev", _cfg(routes=_CHAIN_ROUTES))
    assert result["source"] == "fallback:2"
    assert result["provider"] == "p3"
    assert "p1 unhealthy (HTTP 402); p2 unhealthy (HTTP 503)" in result["reason"]
    assert calls == ["p1", "p2", "p3"]


def test_resolve_all_down_returns_none(monkeypatch):
    _patch_health_map(monkeypatch, {
        "p1": (False, "HTTP 402"),
        "p2": (False, "HTTP 503"),
        "p3": (False, "timed out"),
    })
    assert mr.resolve_route("dev", _cfg(routes=_CHAIN_ROUTES)) is None


def test_resolve_unknown_or_empty_route_none(monkeypatch):
    _patch_health_map(monkeypatch, {"p1": (True, "HTTP 200")})
    cfg = _cfg(routes=_CHAIN_ROUTES)
    assert mr.resolve_route("ghost", cfg) is None
    assert mr.resolve_route("", cfg) is None
    # Case-insensitive lookup finds the route declared as 'dev'.
    result = mr.resolve_route("DEV", cfg)
    assert result is not None
    assert result["route"] == "dev"


def test_resolve_health_disabled_short_circuits(monkeypatch, health_test_env):
    def boom(*args, **kwargs):
        raise AssertionError("probe must not run when health checks are disabled")

    monkeypatch.setattr(mr, "_probe_provider", boom)
    cfg = _cfg(routes=_CHAIN_ROUTES, health={"enabled": False})
    # health_test_env disarms the pytest guard, so only the config kill
    # switch stands between the resolver and the exploding probe.
    assert mr.provider_health("p1", cfg=cfg) == (True, "health checks disabled")
    result = mr.resolve_route("dev", cfg)
    assert result is not None
    assert result["source"] == "default"


def test_resolve_fallback_effort_not_inherited(monkeypatch):
    _patch_health_map(monkeypatch, {
        "p1": (False, "HTTP 500"),
        "p2": (False, "HTTP 500"),
        "p3": (True, "HTTP 200"),
    })
    result = mr.resolve_route("dev", _cfg(routes=_CHAIN_ROUTES))
    # Route default declares xhigh; the winning fallback has no effort of its own.
    assert result["source"] == "fallback:2"
    assert result["reasoning_effort"] == ""


# =============================================================================
# Health probe fail-open matrix
# =============================================================================


@pytest.mark.parametrize(
    "outcome,expect_healthy,reason_substr",
    [
        (_FakeResponse(200), True, "HTTP 200"),
        (_http_error(401), True, "assumed healthy"),
        (_http_error(401), True, "401"),
        (_http_error(403), True, "403"),
        (_http_error(400, b"bad request"), True, "assumed healthy"),
        (_http_error(400, b"error: insufficient credit remaining"), False, "HTTP 400 (credit/quota)"),
        (_http_error(429, b"billing hold"), False, "(credit/quota)"),
        (_http_error(402), False, "HTTP 402"),
        (_http_error(429), False, "HTTP 429"),
        (_http_error(500), False, "HTTP 500"),
        (_http_error(503), False, "HTTP 503"),
        (_http_error(400, b"credit"), False, "(credit/quota)"),
        (_http_error(400, b"insufficient"), False, "(credit/quota)"),
        (_http_error(400, b"quota"), False, "(credit/quota)"),
        (_http_error(400, b"billing"), False, "(credit/quota)"),
    ],
)
def test_probe_matrix(monkeypatch, tmp_path, health_test_env, outcome, expect_healthy, reason_substr):
    _patch_resolve(monkeypatch, runtime=_OPENAI_RUNTIME)

    def fake_urlopen(req, timeout=None):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    healthy, reason = mr.provider_health(
        "p1", "model-a", cfg=_cfg(), health=health,
        allow_recovery_probe=True,
    )
    assert healthy is expect_healthy
    assert reason_substr in reason


def test_probe_connection_error_reason_truncated(monkeypatch, tmp_path, health_test_env):
    _patch_resolve(monkeypatch, runtime=_OPENAI_RUNTIME)
    exc = urllib.error.URLError(ConnectionRefusedError("refused " + "x" * 200))

    def fake_urlopen(req, timeout=None):
        raise exc

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    healthy, reason = mr.provider_health(
        "p1", cfg=_cfg(), health=health, allow_recovery_probe=True,
    )
    assert healthy is False
    assert reason == str(exc)[:80]
    assert len(reason) <= 80


@pytest.mark.parametrize(
    "base_url,expected_url",
    [
        ("https://x.example/v1", "https://x.example/v1/models"),
        ("https://x.example/v1/", "https://x.example/v1/models"),
        ("https://x.example", "https://x.example/v1/models"),
    ],
)
def test_probe_openai_url_construction(monkeypatch, tmp_path, health_test_env, base_url, expected_url):
    _patch_resolve(monkeypatch, runtime={**_OPENAI_RUNTIME, "base_url": base_url})
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResponse(200)

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    healthy, _ = mr.provider_health(
        "p1", cfg=_cfg(), health=health, allow_recovery_probe=True,
    )
    assert healthy is True
    (req,) = captured
    assert req.full_url == expected_url
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Bearer k"


def test_probe_anthropic_messages_shape(monkeypatch, tmp_path, health_test_env):
    _patch_resolve(monkeypatch, runtime={
        "base_url": "https://a.example",
        "api_key": "k",
        "api_mode": "anthropic_messages",
    })
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResponse(200)

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    mr.provider_health(
        "p1", "model-a", cfg=_cfg(), health=health,
        allow_recovery_probe=True,
    )
    (req,) = captured
    assert req.full_url == "https://a.example/v1/messages"
    assert req.get_method() == "POST"
    body = json.loads(req.data.decode("utf-8"))
    assert body["max_tokens"] == 1
    assert body["model"] == "model-a"
    assert body["messages"] == [{"role": "user", "content": "ping"}]
    assert req.get_header("X-api-key") == "k"
    assert req.get_header("Authorization") == "Bearer k"
    assert req.get_header("Anthropic-version") == "2023-06-01"


def test_probe_no_base_url_unhealthy(monkeypatch, tmp_path, health_test_env):
    _patch_resolve(monkeypatch, exc=RuntimeError("no creds"))

    def fake_urlopen(req, timeout=None):  # must never be reached
        raise AssertionError("no probe without a base_url")

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    healthy, reason = mr.provider_health(
        "p1", cfg={"providers": {}}, health=health,
        allow_recovery_probe=True,
    )
    assert healthy is False
    assert reason == "no base_url resolved"


def test_probe_cred_resolution_failure_falls_back_to_cfg_entry(monkeypatch, tmp_path, health_test_env):
    _patch_resolve(monkeypatch, exc=RuntimeError("AuthError: nothing usable"))
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        raise _http_error(401)

    monkeypatch.setattr(mr, "_urlopen", fake_urlopen)
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)
    healthy, reason = mr.provider_health(
        "p1", cfg=_cfg(), health=health, allow_recovery_probe=True,
    )
    # Known base_url + missing key → probe fires → 401 → fail-open healthy.
    assert healthy is True
    assert "assumed healthy" in reason
    (req,) = captured
    assert req.full_url == "https://p1.example/v1/models"
    assert req.get_header("Authorization") == "Bearer "


# =============================================================================
# Cache TTL behavior
# =============================================================================


def _counting_probe(monkeypatch, results):
    """Patch _probe_provider with a per-provider scripted, counting stub."""
    counts = {}

    def fake(provider, model, cfg, health):
        counts[provider] = counts.get(provider, 0) + 1
        return results[provider]

    monkeypatch.setattr(mr, "_probe_provider", fake)
    return counts


def test_no_verdict_assumes_healthy_without_probe(monkeypatch, tmp_path, health_test_env):
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)

    healthy, reason = mr.provider_health("p1", health=health)
    assert healthy is True
    assert "assumed healthy (passive" in reason
    assert counts == {}  # passive-first: an unknown provider is never probed
    assert not health.resolved_cache_path().exists()  # and nothing is written


def test_stale_healthy_verdict_never_reprobes(monkeypatch, tmp_path, health_test_env):
    clock = _Clock(1000.0)
    monkeypatch.setattr(mr, "_now", clock)
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)
    _seed_verdict(health, "p1", healthy=True, ts=clock.t - health.ok_ttl_seconds - 1)

    healthy, reason = mr.provider_health("p1", health=health)
    assert healthy is True
    assert "assumed healthy (passive" in reason
    assert counts == {}  # a stale healthy verdict is trusted, not re-probed


def test_stale_unhealthy_triggers_recovery_probe(monkeypatch, tmp_path, health_test_env):
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)
    clock = _seed_stale_unhealthy(monkeypatch, health)

    assert mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    ) == (True, "HTTP 200")
    assert counts == {"p1": 1}
    cache = json.loads(health.resolved_cache_path().read_text(encoding="utf-8"))
    assert cache["p1"]["healthy"] is True
    assert cache["p1"]["reason"] == "recovery probe: HTTP 200"

    # The recovered verdict is served from cache within ok_ttl…
    clock.t += health.ok_ttl_seconds - 1
    healthy, reason = mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    )
    assert healthy is True
    assert counts == {"p1": 1}
    # …and after ok_ttl it goes back to probe-free assumed-healthy.
    clock.t += 2
    healthy, reason = mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    )
    assert healthy is True
    assert "assumed healthy (passive" in reason
    assert counts == {"p1": 1}


def test_route_resolution_is_read_only_by_default_and_live_probe_is_opt_in(
    monkeypatch, tmp_path, health_test_env,
):
    health_path = tmp_path / "health.json"
    cfg = _cfg(
        routes={"dev": _route(provider="p1", model="model-a")},
        health={"cache_path": str(health_path)},
    )
    catalog = mr.load_routes(cfg)
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    _seed_stale_unhealthy(monkeypatch, catalog.health)
    before = health_path.read_bytes()

    observed = mr.resolve_route_detailed("dev", cfg, catalog=catalog)

    assert observed.directive is None
    assert "recovery probe suppressed" in observed.reason
    assert counts == {}
    assert health_path.read_bytes() == before

    # Fail closed even if an untyped config/plumbing layer passes a truthy
    # value.  Only an intentional literal True may authorize spend + mutation.
    accidental = mr.resolve_route(
        "dev", cfg, catalog=catalog, allow_recovery_probe="true",  # type: ignore[arg-type]
    )

    assert accidental is None
    assert counts == {}
    assert health_path.read_bytes() == before

    live = mr.resolve_route(
        "dev", cfg, catalog=catalog, allow_recovery_probe=True,
    )

    assert live["provider"] == "p1"
    assert live["source"] == "default"
    assert counts == {"p1": 1}
    verdict = json.loads(health_path.read_text(encoding="utf-8"))["p1"]
    assert verdict["healthy"] is True
    assert verdict["reason"] == "recovery probe: HTTP 200"


def test_fresh_unhealthy_verdict_suppresses_within_fail_ttl(monkeypatch, tmp_path, health_test_env):
    counts = _counting_probe(monkeypatch, {"p1": (False, "HTTP 503")})
    health = _health(tmp_path)
    clock = _Clock(1000.0)
    monkeypatch.setattr(mr, "_now", clock)
    _seed_verdict(health, "p1", healthy=False, ts=clock.t)

    healthy, reason = mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    )
    assert healthy is False
    assert counts == {}  # fresh unhealthy verdict → cached, no probe

    clock.t += health.fail_ttl_seconds + 1
    healthy, reason = mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    )
    assert healthy is False  # recovery probe ran and still failed
    assert counts == {"p1": 1}

    clock.t += health.fail_ttl_seconds + 1
    mr.provider_health("p1", health=health, allow_recovery_probe=True)
    assert counts == {"p1": 2}  # each fail-TTL expiry re-checks recovery


def test_corrupted_cache_file_ignored(monkeypatch, tmp_path, health_test_env):
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)
    path = health.resolved_cache_path()
    path.write_bytes(b"{not json")

    # Corrupt cache reads as empty → no verdict → assumed healthy, no probe.
    healthy, reason = mr.provider_health("p1", health=health)
    assert healthy is True
    assert "assumed healthy (passive" in reason
    assert counts == {}


def test_cache_write_failure_swallowed(monkeypatch, tmp_path, health_test_env):
    _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)

    def broken_write(path, data, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mr, "atomic_json_write", broken_write)
    assert mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    ) == (True, "HTTP 200")


def test_cache_write_merges_concurrent_entries(monkeypatch, tmp_path, health_test_env):
    """A verdict landed by another process during our probe must survive.

    Simulates the gateway+CLI lost-update race: another process writes its
    p2 verdict after we snapshot the cache but before we store ours.  The
    merge-under-lock in _store_health_verdict must keep both entries.
    """
    health = _health(tmp_path)
    path = health.resolved_cache_path()
    foreign = {"healthy": False, "reason": "HTTP 503", "ts": 10_000.0}

    def fake_probe(provider, model, cfg, hc):
        # "Other process" lands p2 while our p1 probe is in flight.
        mr.atomic_json_write(path, {"p2": foreign})
        return True, "HTTP 200"

    monkeypatch.setattr(mr, "_probe_provider", fake_probe)
    monkeypatch.setattr(mr, "_now", _Clock(10_000.0))
    _seed_verdict(health, "p1", healthy=False, ts=10_000.0 - health.fail_ttl_seconds - 1)
    assert mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    ) == (True, "HTTP 200")

    cache = json.loads(path.read_text(encoding="utf-8"))
    assert cache["p2"] == foreign  # not clobbered by our pre-probe snapshot
    assert cache["p1"]["healthy"] is True


def test_cache_write_survives_flock_failure(monkeypatch, tmp_path, health_test_env):
    """A runtime flock() failure (ENOLCK on NFS without lockd, some FUSE
    mounts) must degrade to the lock-less re-read+merge, not skip caching —
    otherwise every resolve re-probes every provider."""

    def bad_flock(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(mr.fcntl, "flock", bad_flock)
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health = _health(tmp_path)
    _seed_stale_unhealthy(monkeypatch, health)

    assert mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    ) == (True, "HTTP 200")
    healthy, reason = mr.provider_health(
        "p1", health=health, allow_recovery_probe=True,
    )
    assert healthy is True
    assert reason == "recovery probe: HTTP 200"  # cached verdict, probe not re-run
    assert counts == {"p1": 1}  # second call served from the cached verdict
    cache = json.loads(health.resolved_cache_path().read_text(encoding="utf-8"))
    assert cache["p1"]["healthy"] is True


def test_kill_switch_precedence(monkeypatch, tmp_path, health_test_env):
    counts = _counting_probe(monkeypatch, {"p1": (True, "HTTP 200")})
    health_off = _health(tmp_path, enabled=False)
    _seed_stale_unhealthy(monkeypatch, health_off)  # arm the only probe path

    # Config kill switch → healthy without probing.
    result = mr.provider_health("p1", health=health_off)
    assert result == (True, "health checks disabled")
    assert counts == {}

    # Env "0" overrides config enabled=True.
    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH", "0")
    result = mr.provider_health("p1", health=_health(tmp_path, enabled=True))
    assert result == (True, "health checks disabled")
    assert counts == {}

    # Env "1" overrides config enabled=False → recovery probe runs.
    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH", "1")
    result = mr.provider_health(
        "p1", health=_health(tmp_path, enabled=False), allow_recovery_probe=True,
    )
    assert result == (True, "HTTP 200")
    assert counts == {"p1": 1}


def test_pytest_guard(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_MODEL_ROUTES_HEALTH_TEST", raising=False)
    assert "PYTEST_CURRENT_TEST" in os.environ  # naturally present under pytest

    def boom(*args, **kwargs):
        raise AssertionError("pytest guard must prevent probing")

    monkeypatch.setattr(mr, "_probe_provider", boom)
    assert mr.provider_health("p1", health=_health(tmp_path)) == (True, "pytest")


# =============================================================================
# Passive health — record_provider_outcome / has_unhealthy_verdicts /
# provider_key_for_runtime
# =============================================================================


@pytest.fixture
def passive_state(monkeypatch):
    """Isolate module-level passive-health state between tests."""
    monkeypatch.setattr(mr, "_last_passive_unhealthy_write", {})
    monkeypatch.setattr(
        mr,
        "_unhealthy_memo",
        {"mtime": None, "value": False, "cache": {}},
    )


def _read_cache(health):
    path = health.resolved_cache_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_record_unhealthy_writes_verdict(monkeypatch, tmp_path, health_test_env, passive_state):
    health = _health(tmp_path)
    monkeypatch.setattr(mr, "_now", _Clock(1000.0))
    mr.record_provider_outcome("p1", False, "server_error", health=health)
    cache = _read_cache(health)
    assert cache["p1"]["healthy"] is False
    assert cache["p1"]["reason"] == "passive: server_error"
    assert cache["p1"]["ts"] == 1000.0


def test_record_unhealthy_burst_suppressed(monkeypatch, tmp_path, health_test_env, passive_state):
    health = _health(tmp_path)
    clock = _Clock(1000.0)
    monkeypatch.setattr(mr, "_now", clock)
    mr.record_provider_outcome("p1", False, "overloaded", health=health)
    clock.t += 1.0  # fallback-chain walk re-reports within the burst window
    mr.record_provider_outcome("p1", False, "server_error", health=health)
    assert _read_cache(health)["p1"]["reason"] == "passive: overloaded"  # first write kept
    clock.t += mr._PASSIVE_WRITE_SUPPRESS_SECONDS  # past the window → ts refresh lands
    mr.record_provider_outcome("p1", False, "server_error", health=health)
    entry = _read_cache(health)["p1"]
    assert entry["reason"] == "passive: server_error"
    assert entry["ts"] == clock.t


def test_record_healthy_only_clears_unhealthy(monkeypatch, tmp_path, health_test_env, passive_state):
    health = _health(tmp_path)
    monkeypatch.setattr(mr, "_now", _Clock(1000.0))

    # No entry → success records nothing (steady state stays write-free).
    mr.record_provider_outcome("p1", True, "recovered", health=health)
    assert _read_cache(health) == {}

    # Healthy entry → left untouched (no ts churn).
    _seed_verdict(health, "p1", healthy=True, ts=500.0, reason="recovery probe: HTTP 200")
    mr.record_provider_outcome("p1", True, "recovered", health=health)
    assert _read_cache(health)["p1"]["ts"] == 500.0

    # Unhealthy entry → cleared to a fresh healthy verdict.
    _seed_verdict(health, "p1", healthy=False, ts=500.0)
    mr.record_provider_outcome("p1", True, "recovered", health=health)
    entry = _read_cache(health)["p1"]
    assert entry["healthy"] is True
    assert entry["reason"] == "passive: recovered"
    assert entry["ts"] == 1000.0


def test_record_outcome_disabled_or_empty_key_noop(monkeypatch, tmp_path, health_test_env, passive_state):
    health = _health(tmp_path, enabled=False)
    mr.record_provider_outcome("p1", False, "server_error", health=health)
    mr.record_provider_outcome("", False, "server_error", health=_health(tmp_path))
    assert _read_cache(_health(tmp_path)) == {}


def test_record_outcome_respects_pytest_guard(monkeypatch, tmp_path, passive_state):
    monkeypatch.delenv("HERMES_MODEL_ROUTES_HEALTH_TEST", raising=False)
    health = _health(tmp_path)
    mr.record_provider_outcome("p1", False, "server_error", health=health)
    assert _read_cache(health) == {}


def test_has_unhealthy_verdicts_memo(monkeypatch, tmp_path, health_test_env, passive_state):
    health = _health(tmp_path)
    assert mr.has_unhealthy_verdicts(health=health) is False  # no cache file

    _seed_verdict(health, "p1", healthy=True, ts=1000.0)
    assert mr.has_unhealthy_verdicts(health=health) is False

    _seed_verdict(health, "p2", healthy=False, ts=1000.0)
    assert mr.has_unhealthy_verdicts(health=health) is True

    # Memo path: same mtime → cached value without re-reading.
    def boom(path):
        raise AssertionError("must not re-read an unchanged cache file")

    monkeypatch.setattr(mr, "_read_health_cache", boom)
    assert mr.has_unhealthy_verdicts(health=health) is True


def test_provider_health_reuses_unchanged_cache_snapshot(
    monkeypatch, tmp_path, health_test_env, passive_state,
):
    health = _health(tmp_path)
    _seed_verdict(health, "p1", healthy=False, ts=1000.0)
    monkeypatch.setattr(mr, "_now", _Clock(1000.0))

    assert mr.provider_health("p1", health=health) == (False, "seeded")

    def boom(path):
        raise AssertionError("unchanged health cache must not be re-read")

    monkeypatch.setattr(mr, "_read_health_cache", boom)
    assert mr.provider_health("p1", health=health) == (False, "seeded")


def test_unhealthy_memo_check_and_write_are_atomic_across_threads(
    monkeypatch, tmp_path, health_test_env, passive_state,
):
    health = _health(tmp_path)
    _seed_verdict(health, "p1", healthy=False, ts=100.0)
    monkeypatch.setattr(mr, "_now", _Clock(200.0))
    original_read = mr._read_health_cache
    scan_read = threading.Event()
    release_scan = threading.Event()
    scan_done = threading.Event()
    clear_done = threading.Event()

    def slow_scanner_read(path):
        cache = original_read(path)
        if threading.current_thread().name == "memo-scan":
            scan_read.set()
            release_scan.wait(2)
        return cache

    monkeypatch.setattr(mr, "_read_health_cache", slow_scanner_read)

    def scan():
        mr.has_unhealthy_verdicts(health=health)
        scan_done.set()

    def clear():
        mr.record_provider_outcome("p1", True, "recovered", health=health)
        clear_done.set()

    threading.Thread(target=scan, name="memo-scan", daemon=True).start()
    assert scan_read.wait(2)
    threading.Thread(target=clear, name="memo-clear", daemon=True).start()

    acquired = mr._health_state_lock.acquire(blocking=False)
    if acquired:
        mr._health_state_lock.release()
    try:
        assert acquired is False, "memo scan did not hold the atomic state lock"
    finally:
        release_scan.set()

    assert scan_done.wait(2)
    assert clear_done.wait(2)
    assert mr.has_unhealthy_verdicts(health=health) is False
    assert mr._unhealthy_memo["value"] is False


def test_healthy_clear_rejects_threaded_stale_unhealthy_write(
    monkeypatch, tmp_path, health_test_env, passive_state,
):
    health = _health(tmp_path)
    path = health.resolved_cache_path()
    _seed_verdict(health, "p1", healthy=False, ts=50.0)
    monkeypatch.setattr(mr, "_now", _Clock(200.0))
    clear_done = threading.Event()
    stale_done = threading.Event()
    stale_result = {}

    def clear():
        mr.record_provider_outcome("p1", True, "recovered", health=health)
        clear_done.set()

    def stale_failure():
        clear_done.wait(2)
        stale_result["stored"] = mr._store_health_verdict(
            path,
            "p1",
            {"healthy": False, "reason": "passive: stale", "ts": 100.0},
        )
        stale_done.set()

    threading.Thread(target=stale_failure, daemon=True).start()
    threading.Thread(target=clear, daemon=True).start()
    assert clear_done.wait(2)
    assert stale_done.wait(2)

    entry = _read_cache(health)["p1"]
    assert stale_result["stored"] is False
    assert entry["healthy"] is True
    assert entry["ts"] == 200.0
    assert mr.has_unhealthy_verdicts(health=health) is False


def test_has_unhealthy_verdicts_skips_full_catalog_parse(
    monkeypatch, tmp_path, health_test_env, passive_state,
):
    health = _health(tmp_path)
    _seed_verdict(health, "p1", healthy=False, ts=1000.0)
    monkeypatch.setattr(
        mr,
        "load_config_readonly",
        lambda: {
            "model_routes": {
                "health": {"cache_path": str(health.resolved_cache_path())},
            },
        },
    )

    def boom(*args, **kwargs):
        raise AssertionError("success-path gate must not parse the route catalog")

    monkeypatch.setattr(mr, "load_routes", boom)
    assert mr.has_unhealthy_verdicts() is True


def test_provider_key_for_runtime_matching():
    cfg = {
        "providers": {
            "claude-lb": {"name": "Claude LB 114", "base_url": "http://10.0.0.114:2455"},
            "codex-nekos": {"base_url": "https://nekos.example/v1"},
        }
    }
    # Config-key match (any case), display-name match, custom:-prefixed slug.
    assert mr.provider_key_for_runtime(provider="claude-lb", cfg=cfg) == "claude-lb"
    assert mr.provider_key_for_runtime(provider="Claude LB 114", cfg=cfg) == "claude-lb"
    assert mr.provider_key_for_runtime(provider="custom:claude-lb", cfg=cfg) == "claude-lb"
    # base_url match when the provider string is unknown/empty.
    assert mr.provider_key_for_runtime(provider="", base_url="http://10.0.0.114:2455/", cfg=cfg) == "claude-lb"
    assert (
        mr.provider_key_for_runtime(provider="mystery", base_url="https://nekos.example/v1", cfg=cfg)
        == "codex-nekos"
    )
    # No match → "" (callers must skip recording, never guess).
    assert mr.provider_key_for_runtime(provider="openrouter", base_url="https://openrouter.ai/api/v1", cfg=cfg) == ""
    assert mr.provider_key_for_runtime(cfg=cfg) == ""
    assert mr.provider_key_for_runtime(provider="claude-lb", cfg={"providers": None}) == ""


def test_resolver_uses_passive_verdict_end_to_end(monkeypatch, tmp_path, health_test_env, passive_state):
    """A passive unhealthy verdict fails the primary over without any probe;
    a passive recovery clears the way back."""
    routes = {"dev": _route(provider="p1", model="model-a", fallbacks=[{"provider": "p2", "model": "model-b"}])}
    cfg = _cfg(routes=routes)
    health = mr.load_routes(cfg).health
    clock = _Clock(1000.0)
    monkeypatch.setattr(mr, "_now", clock)

    def boom(*args, **kwargs):
        raise AssertionError("no probe may run in this scenario")

    monkeypatch.setattr(mr, "_probe_provider", boom)

    # Healthy steady state: primary wins, zero probes.
    result = mr.resolve_route("dev", cfg)
    assert (result["provider"], result["source"]) == ("p1", "default")

    # Real traffic reports p1 down → resolution fails over, still no probe.
    mr.record_provider_outcome("p1", False, "overloaded", health=health)
    result = mr.resolve_route("dev", cfg)
    assert (result["provider"], result["source"]) == ("p2", "fallback:1")

    # Real traffic on p1 succeeds again → primary is trusted immediately.
    mr.record_provider_outcome("p1", True, "recovered", health=health)
    result = mr.resolve_route("dev", cfg)
    assert (result["provider"], result["source"]) == ("p1", "default")


def test_resolver_uses_canonical_provider_key_for_alias(
    tmp_path, health_test_env, passive_state,
):
    cfg = {
        "providers": {
            "primary-key": {
                "name": "Primary Display",
                "base_url": "https://primary.example/v1",
            },
            "fallback": {"base_url": "https://fallback.example/v1"},
        },
        "model_routes": {
            "health": {"cache_path": str(tmp_path / "health.json")},
            "routes": {
                "dev": _route(
                    provider="Primary Display",
                    fallbacks=[{"provider": "fallback", "model": "model-b"}],
                ),
            },
        },
    }
    catalog = mr.load_routes(cfg)
    assert _errors(catalog) == []
    mr.record_provider_outcome(
        "primary-key", False, "rate_limit", health=catalog.health,
    )

    result = mr.resolve_route("dev", cfg, catalog=catalog)
    assert result["provider"] == "fallback"
    assert result["source"] == "fallback:1"


# =============================================================================
# runtime_satisfies_route matching
# =============================================================================


def _membership_cfg(accepted=None, fallbacks=None):
    extra = {}
    if accepted is not None:
        extra["accepted"] = accepted
    if fallbacks is not None:
        extra["fallbacks"] = fallbacks
    return _cfg(routes={"dev": _route(model="model-a", **extra)})


def test_accepted_exact_match():
    cfg = _membership_cfg(accepted=["claude-fable-5"])
    assert mr.runtime_satisfies_route({"model": " Claude-Fable-5 "}, "dev", cfg) is True


def test_alias_dotted_to_dashed():
    cfg = _membership_cfg(accepted=["claude-opus-4-8"])
    assert mr.runtime_satisfies_route({"model": "claude-opus-4.8"}, "dev", cfg) is True
    # DIRECTIONAL: only the live runtime model is alias-expanded.
    cfg = _membership_cfg(accepted=["claude-opus-4.8"])
    assert mr.runtime_satisfies_route({"model": "claude-opus-4-8"}, "dev", cfg) is False


def test_alias_digit_dot_rule_with_prefix():
    cfg = _membership_cfg(accepted=["anthropic/claude-opus-4-8-fast"])
    assert mr.runtime_satisfies_route(
        {"model": "anthropic/claude-opus-4.8-fast"}, "dev", cfg
    ) is True


def test_membership_ignores_effort_and_provider():
    cfg = _membership_cfg(accepted=["model-x"])
    runtime = {
        "model": "model-x",
        "provider": "completely-different",
        "reasoning_effort": "minimal",
        "base_url": "https://elsewhere.example",
    }
    assert mr.runtime_satisfies_route(runtime, "dev", cfg) is True


def test_legacy_membership_when_accepted_empty():
    cfg = _membership_cfg(fallbacks=[{"provider": "p2", "model": "model-b"}])
    assert mr.runtime_satisfies_route({"model": "model-a"}, "dev", cfg) is True
    assert mr.runtime_satisfies_route({"model": "model-b"}, "dev", cfg) is True
    assert mr.runtime_satisfies_route({"model": "model-z"}, "dev", cfg) is False


def test_non_member_unknown_route_bad_runtime():
    cfg = _membership_cfg(accepted=["model-a"])
    assert mr.runtime_satisfies_route({"model": "model-z"}, "dev", cfg) is False
    assert mr.runtime_satisfies_route({"model": "model-a"}, "ghost", cfg) is False
    assert mr.runtime_satisfies_route(None, "dev", cfg) is False
    assert mr.runtime_satisfies_route("model-a", "dev", cfg) is False


def test_primary_declared_effort_must_match_runtime():
    # Plugin parity (runtime_matches_spec, runtime_catalog.py:145-147): a spec
    # that declares reasoning_effort only matches a runtime with that effort.
    cfg = _cfg(routes={"dev": _route(model="model-a", reasoning_effort="xhigh")})
    assert mr.runtime_satisfies_route(
        {"model": "model-a", "reasoning_effort": "xhigh"}, "dev", cfg,
    ) is True
    # Case-insensitive comparison, same as the plugin's _norm.
    assert mr.runtime_satisfies_route(
        {"model": "model-a", "reasoning_effort": " XHIGH "}, "dev", cfg,
    ) is True
    # Missing or differing runtime effort → NOT satisfied.
    assert mr.runtime_satisfies_route({"model": "model-a"}, "dev", cfg) is False
    assert mr.runtime_satisfies_route(
        {"model": "model-a", "reasoning_effort": "low"}, "dev", cfg,
    ) is False


def test_fallback_declared_effort_must_match_runtime():
    cfg = _cfg(routes={"dev": _route(
        model="model-a",
        fallbacks=[{"provider": "p2", "model": "model-b", "reasoning_effort": "low"}],
    )})
    # Fallback declares low: runtime must carry it to satisfy via that spec.
    assert mr.runtime_satisfies_route(
        {"model": "model-b", "reasoning_effort": "low"}, "dev", cfg,
    ) is True
    assert mr.runtime_satisfies_route({"model": "model-b"}, "dev", cfg) is False
    assert mr.runtime_satisfies_route(
        {"model": "model-b", "reasoning_effort": "high"}, "dev", cfg,
    ) is False
    # Primary declares no effort: any (or no) runtime effort satisfies it.
    assert mr.runtime_satisfies_route({"model": "model-a"}, "dev", cfg) is True
    assert mr.runtime_satisfies_route(
        {"model": "model-a", "reasoning_effort": "minimal"}, "dev", cfg,
    ) is True


def test_accepted_membership_stays_model_only_with_route_effort():
    # accepted entries are model-only even when the route declares an effort.
    cfg = _cfg(routes={"dev": _route(
        model="model-a", reasoning_effort="xhigh", accepted=["model-x"],
    )})
    assert mr.runtime_satisfies_route({"model": "model-x"}, "dev", cfg) is True
    assert mr.runtime_satisfies_route(
        {"model": "model-x", "reasoning_effort": "low"}, "dev", cfg,
    ) is True
    # accepted replaces legacy membership entirely: the effort-declaring
    # primary is not a member once accepted is set.
    assert mr.runtime_satisfies_route(
        {"model": "model-a", "reasoning_effort": "xhigh"}, "dev", cfg,
    ) is False


# =============================================================================
# route_catalog_for_schema
# =============================================================================


def test_schema_pairs_order_and_validity():
    routes = {
        "dev": _route(provider="p1"),
        "broken": _route(provider="no-such-provider"),
        "chat": {"provider": "p2", "model": "model-b"},  # no description
    }
    cfg = _cfg(routes=routes)
    catalog = mr.load_routes(cfg)
    pairs = mr.route_catalog_for_schema(catalog=catalog)
    assert pairs == [("dev", "test route"), ("chat", "")]
    assert any(
        i.severity == "warning" and "chat" in i.message and "description" in i.message
        for i in catalog.issues
    )


# =============================================================================
# router sub-block (ADR-003 Phase 2)
# =============================================================================


def _router_routes():
    return {"dev": _route(provider="p1"), "chat": _route(provider="p2", model="model-b")}


def test_router_absent_defaults_off():
    catalog = mr.load_routes(_cfg(routes=_router_routes()))
    assert catalog.issues == []
    assert catalog.router == mr.RouterConfig()
    assert catalog.router.mode == "off"
    assert catalog.router.model == mr.DEFAULT_ROUTER_MODEL
    assert catalog.router.timeout_ms == 8000.0
    assert catalog.router.classify_timeout_s == 2.0
    assert catalog.router.recent_turns == 5
    assert catalog.router.normal_downgrade_streak == 3
    assert catalog.router.repromote_after_turns == 3
    assert catalog.router.chat_route == ""
    assert catalog.router.label_routes == ()
    assert catalog.router.decision_log == ""
    assert catalog.router.refusal == mr.RefusalConfig()
    assert catalog.router.refusal.enabled is False
    assert catalog.router.refusal.api_fallback is False
    assert catalog.router.refusal.clean_fork is True
    assert catalog.router.refusal.keep_user_turns == 5
    assert catalog.router.refusal.mask_on_refusal is True
    assert catalog.router.refusal.soft_detect is True
    assert catalog.router.refusal.max_recovery_hops == 2
    assert catalog.router.refusal.min_confidence == 0.85
    assert catalog.router.refusal.dev_route == "PERMISSIVE_DEV"
    assert catalog.router.refusal.chat_route == "PERMISSIVE_CHAT"
    assert catalog.router.refusal.document_route == ""
    assert catalog.router.refusal.notify is True


def test_router_full_valid_block():
    router = {
        "mode": "shadow",
        "model": "gemini-3-flash-preview",
        "timeout_ms": 5000,
        "classify_timeout_s": 1.5,
        "recent_turns": 8,
        "normal_downgrade_streak": 2,
        "repromote_after_turns": 4,
        "chat_route": "chat",
        "label_routes": {"SYSTEM_DEV": "dev", "FRONTEND_DEV": "dev", "DOCUMENT_WORK": ""},
        "decision_log": "/tmp/decisions.jsonl",
    }
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router=router))
    assert catalog.issues == []
    rc = catalog.router
    assert rc.mode == "shadow"
    assert rc.timeout_ms == 5000.0
    assert rc.classify_timeout_s == 1.5
    assert rc.recent_turns == 8
    assert rc.normal_downgrade_streak == 2
    assert rc.repromote_after_turns == 4
    assert rc.chat_route == "chat"
    # empty-string DOCUMENT_WORK means "never switches" — not an error
    assert rc.label_route_map() == {"SYSTEM_DEV": "dev", "FRONTEND_DEV": "dev"}
    assert rc.decision_log == "/tmp/decisions.jsonl"


def test_router_refusal_partial_config_inherits_defaults():
    catalog = mr.load_routes(_cfg(
        routes=_router_routes(),
        router={"refusal": {"enabled": True, "min_confidence": 0.9, "notify": False}},
    ))
    assert catalog.issues == []
    refusal = catalog.router.refusal
    assert refusal.enabled is True
    assert refusal.api_fallback is False
    assert refusal.clean_fork is True
    assert refusal.keep_user_turns == 5
    assert refusal.mask_on_refusal is True
    assert refusal.soft_detect is True
    assert refusal.max_recovery_hops == 2
    assert refusal.min_confidence == 0.9
    assert refusal.dev_route == "PERMISSIVE_DEV"
    assert refusal.chat_route == "PERMISSIVE_CHAT"
    assert refusal.document_route == ""
    assert refusal.notify is False


def test_router_refusal_explicit_disabled_config_parsed():
    catalog = mr.load_routes(_cfg(
        routes=_router_routes(),
        router={"refusal": {
            "enabled": False,
            "api_fallback": True,
            "clean_fork": False,
            "keep_user_turns": 3,
            "mask_on_refusal": False,
            "soft_detect": False,
            "max_recovery_hops": 4,
            "min_confidence": 0.72,
            "dev_route": "dev",
            "chat_route": "chat",
            "document_route": "chat",
            "notify": True,
        }},
    ))
    assert catalog.issues == []
    assert catalog.router.refusal == mr.RefusalConfig(
        enabled=False,
        api_fallback=True,
        clean_fork=False,
        keep_user_turns=3,
        mask_on_refusal=False,
        soft_detect=False,
        max_recovery_hops=4,
        min_confidence=0.72,
        dev_route="dev",
        chat_route="chat",
        document_route="chat",
        notify=True,
    )


@pytest.mark.parametrize("value", [0, -1, 2.5, "3", True, None])
def test_router_refusal_invalid_keep_user_turns_warns_and_defaults(value):
    catalog = mr.load_routes(_cfg(
        routes=_router_routes(),
        router={"refusal": {"keep_user_turns": value}},
    ))
    assert catalog.router.refusal.keep_user_turns == 5
    assert any("keep_user_turns" in issue.message for issue in _warnings(catalog))


def test_router_not_a_mapping_is_error_and_off():
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router=[1]))
    errors = _errors(catalog)
    assert len(errors) == 1 and "router" in errors[0].message
    assert catalog.router.mode == "off"


def test_router_invalid_mode_is_error_and_off():
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router={"mode": "audit"}))
    errors = _errors(catalog)
    assert len(errors) == 1 and "mode" in errors[0].message
    assert catalog.router.mode == "off"


def test_router_yaml_false_mode_is_off_without_issue():
    # YAML 1.1: unquoted ``mode: off`` arrives as boolean False.
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router={"mode": False}))
    assert catalog.issues == []
    assert catalog.router.mode == "off"


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [
        ("shadow", "off", "off"),
        ("off", "shadow", "shadow"),
        ("shadow", "enforce", "enforce"),
        ("enforce", "typo", "off"),
    ],
)
def test_router_mode_env_bridge_takes_precedence(
    monkeypatch, configured, override, expected,
):
    monkeypatch.setenv("HERMES_MODEL_ROUTER_MODE", override)
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"mode": configured})
    )
    assert catalog.router.mode == expected


def test_router_unknown_key_warns():
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"mode": "shadow", "modle": "typo"})
    )
    warnings = _warnings(catalog)
    assert any("modle" in w.message for w in warnings)
    assert catalog.router.mode == "shadow"


@pytest.mark.parametrize(
    "key",
    [
        "timeout_ms",
        "classify_timeout_s",
        "recent_turns",
        "normal_downgrade_streak",
    ],
)
@pytest.mark.parametrize("value", [0, -1, "5", True, None])
def test_router_invalid_numeric_warns_and_defaults(key, value):
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router={key: value}))
    warnings = _warnings(catalog)
    assert any(key in w.message for w in warnings)
    assert getattr(catalog.router, key) == getattr(mr.RouterConfig(), key)


def test_repromote_after_turns_defaults():
    catalog = mr.load_routes(_cfg(routes=_router_routes()))
    assert catalog.issues == []
    assert catalog.router.repromote_after_turns == 3
    assert catalog.routes["dev"].repromote_after_turns is None


def test_router_repromote_after_turns_zero_disables_without_warning():
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"repromote_after_turns": 0})
    )
    assert catalog.issues == []
    assert catalog.router.repromote_after_turns == 0


def test_route_repromote_after_turns_override_parsed():
    routes = _router_routes()
    routes["dev"]["repromote_after_turns"] = 5
    routes["chat"]["repromote_after_turns"] = 0
    catalog = mr.load_routes(_cfg(routes=routes))
    assert catalog.issues == []
    assert catalog.routes["dev"].repromote_after_turns == 5
    assert catalog.routes["chat"].repromote_after_turns == 0


@pytest.mark.parametrize("bad", ["3", True, False, -1, 2.5])
def test_router_repromote_after_turns_invalid_warns_and_defaults(bad):
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"repromote_after_turns": bad})
    )
    assert any("repromote_after_turns" in w.message for w in _warnings(catalog))
    assert _errors(catalog) == []
    assert catalog.router.repromote_after_turns == 3


@pytest.mark.parametrize("bad", ["3", True, False, -1, 2.5])
def test_route_repromote_after_turns_invalid_warns_route_kept(bad):
    catalog = mr.load_routes(
        _cfg(routes={"dev": _route(repromote_after_turns=bad)})
    )
    assert any("repromote_after_turns" in w.message for w in _warnings(catalog))
    assert _errors(catalog) == []
    assert "dev" in catalog.routes
    assert catalog.routes["dev"].repromote_after_turns is None


def test_router_chat_route_must_name_declared_route():
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"mode": "shadow", "chat_route": "ghost"})
    )
    errors = _errors(catalog)
    assert len(errors) == 1 and "chat_route" in errors[0].message
    assert catalog.router.chat_route == ""  # downgrades disabled, mode preserved
    assert catalog.router.mode == "shadow"


def test_router_chat_route_case_insensitive_lookup():
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"chat_route": "CHAT"})
    )
    assert _errors(catalog) == []
    assert catalog.router.chat_route == "CHAT"


def test_router_label_routes_unknown_route_is_error_label_disabled():
    router = {"label_routes": {"SYSTEM_DEV": "ghost", "FRONTEND_DEV": "dev"}}
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router=router))
    errors = _errors(catalog)
    assert len(errors) == 1 and "SYSTEM_DEV" in errors[0].message
    assert catalog.router.label_route_map() == {"FRONTEND_DEV": "dev"}


def test_router_label_routes_unknown_label_warns():
    router = {"label_routes": {"NORMAL": "chat", "SYSTEM_DEV": "dev"}}
    catalog = mr.load_routes(_cfg(routes=_router_routes(), router=router))
    warnings = _warnings(catalog)
    assert any("NORMAL" in w.message for w in warnings)
    assert catalog.router.label_route_map() == {"SYSTEM_DEV": "dev"}


def test_router_label_routes_not_mapping_is_error():
    catalog = mr.load_routes(
        _cfg(routes=_router_routes(), router={"label_routes": ["SYSTEM_DEV"]})
    )
    errors = _errors(catalog)
    assert len(errors) == 1 and "label_routes" in errors[0].message
    assert catalog.router.label_routes == ()


def test_router_active_mode_with_no_routes_warns():
    catalog = mr.load_routes(_cfg(model_routes={"router": {"mode": "shadow"}}))
    warnings = _warnings(catalog)
    assert any("no valid routes" in w.message for w in warnings)
    assert catalog.router.mode == "shadow"


def test_router_off_mode_with_no_routes_is_quiet():
    catalog = mr.load_routes(_cfg(model_routes={"router": {"mode": "off"}}))
    assert catalog.issues == []


def test_static_rule_optional_name_accepted():
    rules = [
        {"name": "pr-shorthand", "route": "dev", "when": {"text_matches_any": ["x"]}},
        {"route": "chat", "when": {"is_owner": {"eq": False}}},  # legacy: no name
    ]
    catalog = mr.load_routes(_cfg(routes=_router_routes(), static_rules=rules))
    assert catalog.issues == []
    assert [r.get("name") for r in catalog.static_rules] == ["pr-shorthand", None]


def test_static_rule_bad_name_warns_but_rule_kept():
    rules = [{"name": 3, "route": "dev", "when": {"platform": {"eq": "telegram"}}}]
    catalog = mr.load_routes(_cfg(routes=_router_routes(), static_rules=rules))
    warnings = _warnings(catalog)
    assert any("'name'" in w.message for w in warnings)
    assert len(catalog.static_rules) == 1


def test_router_classifier_provider_and_model_are_configurable():
    providers = _providers()
    providers["classifier"] = {
        "base_url": "https://classifier.example/v1",
        "models": {"router-v1": {}},
    }
    catalog = mr.load_routes(
        _cfg(
            routes=_router_routes(),
            providers=providers,
            router={"provider": "classifier", "model": "router-v1"},
        )
    )
    assert _errors(catalog) == []
    assert catalog.router.provider == "classifier"
    assert catalog.router.model == "router-v1"


def test_router_unknown_classifier_provider_fails_back_to_default():
    catalog = mr.load_routes(
        _cfg(
            routes=_router_routes(),
            router={"provider": "missing-classifier", "model": "router-v1"},
        )
    )
    assert any("router.provider" in issue.message for issue in _errors(catalog))
    assert catalog.router.provider == mr.DEFAULT_ROUTER_PROVIDER
    assert catalog.router.model == "router-v1"


def test_resolve_route_runtime_uses_canonical_resolver_without_secrets(monkeypatch):
    cfg = _cfg(routes={"dev": _route(provider="p1", model="model-a")})
    calls = []

    def fake_resolve_runtime_provider(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "custom",
            "api_mode": "anthropic_messages",
            "base_url": "https://secret-endpoint.example",
            "api_key": "must-not-escape",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )
    runtime = mr.resolve_route_runtime("dev", cfg)

    assert calls == [{"requested": "p1", "target_model": "model-a"}]
    assert runtime == {
        "route": "dev",
        "provider": "custom",
        "model": "model-a",
        "reasoning_effort": "",
        "api_mode": "anthropic_messages",
        "source": "default",
        "reason": "",
    }
    assert "api_key" not in runtime
    assert "base_url" not in runtime


# =============================================================================
# model_routes.moods — M1 placeholder (parsed and validated, not yet consumed)
# =============================================================================


def _moods_cfg(moods, routes=None):
    section = {"routes": routes if routes is not None else {"dev": _route()}}
    section["moods"] = moods
    return {"providers": _providers(), "model_routes": section}


def test_moods_defaults_when_section_absent():
    catalog = mr.load_routes(_cfg(routes={"dev": _route()}))
    assert catalog.moods == mr.MoodsConfig()
    assert catalog.moods.enabled is False
    assert catalog.moods.dir == ""
    assert catalog.moods.confidence_threshold == 0.7
    assert catalog.moods.apply_model_routing is False
    assert catalog.moods.routes == ()
    assert catalog.moods.route_map() == {}
    assert not [i for i in catalog.issues if i.severity == "error"]


def test_moods_default_dir_under_hermes_home():
    assert mr.MoodsConfig().resolved_dir() == get_hermes_home() / "moods"
    assert mr.MoodsConfig(dir="~/custom/moods").resolved_dir() == (
        Path("~/custom/moods").expanduser()
    )


def test_moods_full_block_parsed():
    catalog = mr.load_routes(_moods_cfg(
        {
            "enabled": True,
            "dir": "~/.hermes/moods",
            "confidence_threshold": 0.55,
            "apply_model_routing": True,
            "routes": {"care": "gentle", "playful": "dev"},
        },
        routes={"dev": _route(), "gentle": _route(provider="p2")},
    ))
    moods = catalog.moods
    assert moods.enabled is True
    assert moods.dir == "~/.hermes/moods"
    assert moods.confidence_threshold == 0.55
    assert moods.apply_model_routing is True
    assert moods.route_map() == {"care": "gentle", "playful": "dev"}
    assert not [i for i in catalog.issues if i.severity == "error"]


def test_moods_partial_block_inherits_defaults():
    catalog = mr.load_routes(_moods_cfg({"enabled": True}))
    assert catalog.moods.enabled is True
    assert catalog.moods.confidence_threshold == 0.7
    assert catalog.moods.apply_model_routing is False
    assert catalog.moods.routes == ()


def test_moods_permissive_route_is_rejected_with_error():
    """Hard product rule: a mood must never select a refusal-bypass route."""
    catalog = mr.load_routes(_moods_cfg(
        {"enabled": True, "routes": {"care": "PERMISSIVE_CHAT"}},
        routes={"dev": _route(), "PERMISSIVE_CHAT": _route(provider="p2")},
    ))
    errors = [i for i in catalog.issues if i.severity == "error"]
    assert len(errors) == 1
    assert "moods.routes.care" in errors[0].message
    assert "PERMISSIVE_CHAT" in errors[0].message
    assert "refusal-bypass" in errors[0].message
    # Rejected outright — never reachable by any later consumer.
    assert catalog.moods.route_map() == {}


@pytest.mark.parametrize(
    "route_name", ["PERMISSIVE_DEV", "PERMISSIVE_CHAT", "permissive_dev", "PERMISSIVE_X"]
)
def test_moods_permissive_rejected_by_prefix_even_when_undeclared(route_name):
    catalog = mr.load_routes(_moods_cfg({"routes": {"cute": route_name}}))
    errors = [i for i in catalog.issues if i.severity == "error"]
    assert len(errors) == 1
    assert route_name in errors[0].message
    assert catalog.moods.routes == ()


def test_moods_unknown_route_and_unknown_mood_are_warnings():
    catalog = mr.load_routes(_moods_cfg(
        {"routes": {"care": "nonexistent", "grumpy": "dev", "focused": "dev"}}
    ))
    assert catalog.moods.route_map() == {"focused": "dev"}
    assert not [i for i in catalog.issues if i.severity == "error"]
    messages = " | ".join(i.message for i in catalog.issues)
    assert "unknown route 'nonexistent'" in messages
    assert "unknown mood 'grumpy'" in messages


def test_moods_bad_scalar_types_fall_back_to_defaults():
    catalog = mr.load_routes(_moods_cfg({
        "enabled": "yes",
        "dir": 5,
        "confidence_threshold": 1.5,
        "apply_model_routing": "true",
    }))
    assert catalog.moods == mr.MoodsConfig()
    assert len([i for i in catalog.issues if i.severity == "warning"]) >= 4


def test_moods_non_mapping_section_disables_and_errors():
    catalog = mr.load_routes(_moods_cfg(["enabled"]))
    assert catalog.moods == mr.MoodsConfig()
    errors = [i for i in catalog.issues if i.severity == "error"]
    assert len(errors) == 1
    assert "'moods' must be a mapping" in errors[0].message


def test_moods_non_mapping_routes_errors():
    catalog = mr.load_routes(_moods_cfg({"routes": ["care"]}))
    assert catalog.moods.routes == ()
    errors = [i for i in catalog.issues if i.severity == "error"]
    assert len(errors) == 1
    assert "moods.routes must be a mapping" in errors[0].message


def test_moods_unknown_key_warns_but_keeps_parsing():
    catalog = mr.load_routes(_moods_cfg({"enabled": True, "nope": 1}))
    assert catalog.moods.enabled is True
    assert any("unknown key 'nope' under moods" in i.message for i in catalog.issues)


def test_moods_is_a_recognized_top_level_section():
    """`moods` must not trip the unknown-top-level-key warning."""
    catalog = mr.load_routes(_moods_cfg({"enabled": False}))
    assert not any(
        "unknown key 'moods' under model_routes" in i.message for i in catalog.issues
    )
