"""Model routing catalog — ADR-003 Phase 1.

Purpose-based route catalog: each route in ``model_routes.routes`` maps a
purpose name (``dev``, ``chat``, …) to a concrete runtime (provider/model/
reasoning_effort) plus an ordered, health-checked fallback chain.

Phase 1 scope is the config schema, loader/validation, resolver, and
provider health tracking only.  Health is **passive-first**: verdicts come
from real completion traffic (``record_provider_outcome``, wired into the
agent's fallback-activation and completion-success paths). Route resolution
is observation-only by default; a live caller must explicitly opt into the
remaining recovery probe before a provider with a stale *unhealthy* verdict
is re-checked and the shared verdict cache is updated. Providers with no
verdict (or a stale healthy one) are assumed healthy without any network I/O,
so steady-state route resolution never probes and never burns a real
completion against a healthy backend. Probe semantics stay fail-open (ported
from the skill-gate plugin's ``runtime_catalog.py``): only signals that
indicate the PROVIDER cannot serve completions (credit/quota exhaustion,
402/429, 5xx, connection failures) count as unhealthy; auth-scoped 401/403
(or a malformed probe 400) are treated as healthy so a probe defect can never
freeze routing. Observation-only callers cannot invoke that fail-open path.

``static_rules`` are parsed and validated here; condition matching and
enforcement live in the gateway pre-dispatch router (``gateway/model_router.py``,
Phase 2), as does the ``router`` sub-block that configures it.
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX; merge-on-write still applies
    fcntl = None  # type: ignore[assignment]
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home, parse_reasoning_effort, VALID_REASONING_EFFORTS
from hermes_cli.config import (
    ConfigIssue,
    get_compatible_custom_providers,
    load_config,
    load_config_readonly,
)
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_urlopen = urllib.request.urlopen  # test seam


def _now() -> float:  # test seam
    return time.time()


# =============================================================================
# Constants
# =============================================================================

DEFAULT_OK_TTL_SECONDS = 300.0
DEFAULT_FAIL_TTL_SECONDS = 120.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 2.5
_HEALTH_CACHE_FILENAME = "model_route_health.json"  # under get_hermes_home()/"state"/
_CREDIT_SNIFF_KEYWORDS = ("credit", "insufficient", "quota", "billing")
_HEALTH_ENV = "HERMES_MODEL_ROUTES_HEALTH"
_HEALTH_TEST_ENV = "HERMES_MODEL_ROUTES_HEALTH_TEST"
_ROUTER_MODE_ENV = "HERMES_MODEL_ROUTER_MODE"
_health_state_lock = threading.RLock()

_SECTION_KEYS = {"routes", "health", "static_rules", "router"}
_ROUTE_KEYS = {
    "description", "provider", "model", "reasoning_effort", "accepted", "fallbacks",
    "repromote_after_turns",
}
_FALLBACK_KEYS = {"provider", "model", "reasoning_effort"}
_HEALTH_KEYS = {"enabled", "cache_path", "ok_ttl_seconds", "fail_ttl_seconds", "probe_timeout_seconds"}
_HEALTH_NUMERIC_KEYS = ("ok_ttl_seconds", "fail_ttl_seconds", "probe_timeout_seconds")
_RULE_KEYS = {"name", "route", "when", "reason"}
_ROUTER_KEYS = {
    "mode", "provider", "model", "timeout_ms", "classify_timeout_s", "recent_turns", "normal_downgrade_streak",
    "repromote_after_turns", "chat_route", "label_routes", "decision_log", "refusal",
}
_REFUSAL_KEYS = {
    "enabled", "api_fallback", "clean_fork", "keep_user_turns",
    "min_confidence", "dev_route", "chat_route", "document_route", "notify",
}
_ROUTER_MODES = ("off", "shadow", "enforce")
# Classifier labels that may map to a route. NORMAL is not mappable — its
# downgrade target is ``chat_route`` (hysteresis-gated).
_ROUTER_LABELS = ("SYSTEM_DEV", "FRONTEND_DEV", "DOCUMENT_WORK")
_ROUTER_NUMERIC_KEYS = (
    "timeout_ms",
    "classify_timeout_s",
    "recent_turns",
    "normal_downgrade_streak",
)
DEFAULT_ROUTER_MODEL = "gemini-3-flash-preview"
DEFAULT_ROUTER_PROVIDER = "gemini"


def _effective_router_mode(configured: Any = "off") -> str:
    """Resolve router mode with the emergency environment bridge applied.

    A non-empty environment value is authoritative. Unknown values fail safe
    to ``off`` so a typo in an emergency override can never enable routing.
    """
    override = os.environ.get(_ROUTER_MODE_ENV, "").strip().lower()
    if override:
        return override if override in _ROUTER_MODES else "off"
    mode = (
        str(configured or "").strip().lower()
        if isinstance(configured, str)
        else "off"
    )
    return mode if mode in _ROUTER_MODES else "off"


def _with_effective_router_mode(router: "RouterConfig") -> "RouterConfig":
    mode = _effective_router_mode(router.mode)
    return router if mode == router.mode else replace(router, mode=mode)


# =============================================================================
# Data types
# =============================================================================


@dataclass(frozen=True)
class FallbackSpec:
    provider: str
    model: str
    reasoning_effort: str = ""  # "" = unspecified (NOT inherited from the route)


@dataclass(frozen=True)
class RouteSpec:
    name: str  # as declared in YAML
    description: str  # "" if absent (warning issued)
    provider: str
    model: str
    reasoning_effort: str = ""  # "" = unspecified
    accepted: Tuple[str, ...] = ()  # model ids; empty → legacy membership
    fallbacks: Tuple["FallbackSpec", ...] = ()
    # None = inherit router.repromote_after_turns; <= 0 disables for this route.
    repromote_after_turns: Optional[int] = None


@dataclass(frozen=True)
class HealthConfig:
    enabled: bool = True
    cache_path: str = ""  # "" → get_hermes_home()/"state"/model_route_health.json
    ok_ttl_seconds: float = DEFAULT_OK_TTL_SECONDS
    fail_ttl_seconds: float = DEFAULT_FAIL_TTL_SECONDS
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS

    def resolved_cache_path(self) -> Path:
        if self.cache_path:
            return Path(self.cache_path).expanduser()
        return get_hermes_home() / "state" / _HEALTH_CACHE_FILENAME


@dataclass(frozen=True)
class RefusalConfig:
    """Orthogonal refusal-risk routing under ``model_routes.router.refusal``."""

    enabled: bool = False
    api_fallback: bool = False
    clean_fork: bool = True
    keep_user_turns: int = 5
    min_confidence: float = 0.85
    dev_route: str = "PERMISSIVE_DEV"
    chat_route: str = "PERMISSIVE_CHAT"
    document_route: str = ""  # empty → follow chat_route
    notify: bool = True


@dataclass(frozen=True)
class RouterConfig:
    """``model_routes.router`` — the gateway pre-dispatch dynamic router."""

    mode: str = "off"  # off | shadow | enforce
    provider: str = DEFAULT_ROUTER_PROVIDER
    model: str = DEFAULT_ROUTER_MODEL
    timeout_ms: float = 8000.0
    classify_timeout_s: float = 2.0
    recent_turns: int = 5
    normal_downgrade_streak: int = 3
    repromote_after_turns: int = 3  # accepted-member noops before primary re-promotion
    chat_route: str = ""  # NORMAL downgrade target; "" = downgrades disabled
    label_routes: Tuple[Tuple[str, str], ...] = ()  # (label, route-name) pairs
    decision_log: str = ""  # "" → get_hermes_home()/logs/model_router_decisions.jsonl
    refusal: RefusalConfig = field(default_factory=RefusalConfig)

    def label_route_map(self) -> Dict[str, str]:
        return dict(self.label_routes)


@dataclass
class RouteCatalog:
    # Only VALID routes; declaration order preserved (dict insertion order).
    routes: Dict[str, RouteSpec] = field(default_factory=dict)
    health: HealthConfig = field(default_factory=HealthConfig)
    static_rules: List[Dict[str, Any]] = field(default_factory=list)  # matched in gateway/model_router.py
    router: RouterConfig = field(default_factory=RouterConfig)
    issues: List[ConfigIssue] = field(default_factory=list)


@dataclass(frozen=True)
class RouteResolution:
    """A route directive plus a secret-free explanation of the resolution."""

    directive: Optional[Dict[str, str]]
    reason: str


# =============================================================================
# Matching helpers (ported from skill-gate runtime_catalog.py)
# =============================================================================


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _model_alias_candidates(model: str) -> List[str]:
    candidates = [model]
    dotted_version = re.sub(r"(?<=\d)\.(?=\d)", "-", model)  # dots between digits only
    if dotted_version != model:
        candidates.append(dotted_version)
    all_dots = model.replace(".", "-")
    if all_dots != model:
        candidates.append(all_dots)
    return list(dict.fromkeys(candidates))


def _model_matches(current: Any, expected: Any) -> bool:
    # DIRECTIONAL: only the CURRENT (live runtime) model is alias-expanded —
    # catalog should declare dash forms (claude-opus-4-8 matches live
    # claude-opus-4.8, not vice versa).
    target = _norm(expected)
    if not target:
        return True
    current_text = _norm(current)
    if current_text == target:
        return True
    return target in {_norm(candidate) for candidate in _model_alias_candidates(current_text)}


# =============================================================================
# Loader / validation
# =============================================================================


def _known_provider_names(cfg: Dict[str, Any]) -> set:
    """Names a route/fallback ``provider`` may legally reference.

    Union of the ``providers:`` dict keys/names (plus legacy
    ``custom_providers``) and the built-in canonical provider ids/aliases —
    built-ins (anthropic, openrouter, …) are credential-resolvable without a
    ``providers:`` entry, so rejecting them would be wrong.
    """
    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    names: set = set()
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for key, entry in providers.items():
            names.add(_normalize_custom_provider_name(str(key)))
            if isinstance(entry, dict):
                raw_name = entry.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    names.add(_normalize_custom_provider_name(raw_name))
    try:
        for entry in get_compatible_custom_providers(cfg):
            for name_key in ("name", "provider_key"):
                value = entry.get(name_key)
                if isinstance(value, str) and value.strip():
                    names.add(_normalize_custom_provider_name(value))
    except Exception as exc:
        logger.debug(
            "model_routes: custom provider enumeration failed (%s)",
            type(exc).__name__,
        )
    try:
        from hermes_cli.models import _KNOWN_PROVIDER_NAMES  # heavy module — deferred

        names |= {_norm(name) for name in _KNOWN_PROVIDER_NAMES}
    except Exception as exc:
        logger.debug(
            "model_routes: built-in provider names unavailable (%s)",
            type(exc).__name__,
        )
    names.discard("")
    return names


def _declared_provider_models(cfg: Dict[str, Any], provider_norm: str) -> Optional[Dict[str, Any]]:
    """Return the matched ``providers:`` entry's ``models:`` mapping, if any."""
    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return None
    for key, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        entry_names = {_normalize_custom_provider_name(str(key))}
        raw_name = entry.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            entry_names.add(_normalize_custom_provider_name(raw_name))
        if provider_norm not in entry_names:
            continue
        models = entry.get("models")
        if isinstance(models, dict) and models:
            return models
        if isinstance(models, list) and models:
            return {str(m): {} for m in models if isinstance(m, str) and m.strip()}
        return None
    return None


def _validate_effort(value: Any) -> Tuple[bool, str]:
    """Return (valid, normalized_effort). "" and "none"/valid levels pass.

    YAML 1.1 booleans mirror ``parse_reasoning_effort`` semantics: ``off``/
    ``no``/``false`` (bool False) means reasoning disabled ("none"); ``on``/
    ``yes``/``true`` (bool True) is treated as unspecified, same as omitting
    the key — never a route-dropping error.
    """
    if value is None or value is True:
        return True, ""
    if value is False:
        return True, "none"
    if not isinstance(value, str):
        return False, ""
    text = value.strip()
    if not text:
        return True, ""
    if parse_reasoning_effort(text) is None:
        return False, text
    return True, text.lower()


def _effort_hint() -> str:
    return "Use one of: " + "|".join(VALID_REASONING_EFFORTS) + "|none, or omit to leave unset"


def _parse_fallback(
    route_name: str,
    index: int,
    item: Any,
    cfg: Dict[str, Any],
    known_providers: set,
    issues: List[ConfigIssue],
) -> Optional[FallbackSpec]:
    prefix = f"model_routes: route '{route_name}' fallback #{index}"
    if not isinstance(item, dict):
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: must be a mapping (got {type(item).__name__})",
            "Each fallback needs at least: provider, model",
        ))
        return None
    for key in sorted(set(item) - _FALLBACK_KEYS):
        issues.append(ConfigIssue(
            "warning",
            f"{prefix}: unknown key '{key}' ignored",
            f"Supported fallback keys: {', '.join(sorted(_FALLBACK_KEYS))}",
        ))
    provider = item.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: missing 'provider'",
            "Add: provider: <name declared under providers: or a built-in id>",
        ))
        return None
    provider = provider.strip()
    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    if _normalize_custom_provider_name(provider) not in known_providers:
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: unknown provider '{provider}'",
            "Declare it under providers: in config.yaml (or use a built-in provider id)",
        ))
        return None
    model = item.get("model")
    if not isinstance(model, str) or not model.strip():
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: missing 'model'",
            "Add: model: <model-id>",
        ))
        return None
    effort_ok, effort = _validate_effort(item.get("reasoning_effort"))
    if not effort_ok:
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: invalid reasoning_effort {item.get('reasoning_effort')!r}",
            _effort_hint(),
        ))
        return None
    return FallbackSpec(provider=provider, model=model.strip(), reasoning_effort=effort)


def _parse_route(
    name: str,
    entry: Any,
    cfg: Dict[str, Any],
    known_providers: set,
    issues: List[ConfigIssue],
) -> Optional[RouteSpec]:
    prefix = f"model_routes: route '{name}'"
    if not isinstance(entry, dict):
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: must be a mapping (got {type(entry).__name__})",
            "Each route needs at least: provider, model",
        ))
        return None

    has_error = False
    for key in sorted(set(entry) - _ROUTE_KEYS):
        issues.append(ConfigIssue(
            "warning",
            f"{prefix}: unknown key '{key}' ignored",
            f"Supported route keys: {', '.join(sorted(_ROUTE_KEYS))}",
        ))

    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    provider = entry.get("provider")
    provider_norm = ""
    if not isinstance(provider, str) or not provider.strip():
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: missing 'provider'",
            "Add: provider: <name declared under providers: or a built-in id>",
        ))
        has_error = True
        provider = ""
    else:
        provider = provider.strip()
        provider_norm = _normalize_custom_provider_name(provider)
        if provider_norm not in known_providers:
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: unknown provider '{provider}'",
                "Declare it under providers: in config.yaml (or use a built-in provider id)",
            ))
            has_error = True

    model = entry.get("model")
    if not isinstance(model, str) or not model.strip():
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: missing 'model'",
            "Add: model: <model-id>",
        ))
        has_error = True
        model = ""
    else:
        model = model.strip()
        if not has_error and provider_norm:
            declared = _declared_provider_models(cfg, provider_norm)
            if declared and not any(_model_matches(model, key) for key in declared):
                issues.append(ConfigIssue(
                    "warning",
                    f"{prefix}: model '{model}' is not in provider '{provider}' declared models",
                    "Check for a typo, or add the model under the provider's models: mapping",
                ))

    effort_ok, effort = _validate_effort(entry.get("reasoning_effort"))
    if not effort_ok:
        issues.append(ConfigIssue(
            "error",
            f"{prefix}: invalid reasoning_effort {entry.get('reasoning_effort')!r}",
            _effort_hint(),
        ))
        has_error = True

    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        issues.append(ConfigIssue(
            "warning",
            f"{prefix}: missing 'description'",
            "Add a short purpose description — later phases surface it in tool schemas",
        ))
        description = ""
    else:
        description = description.strip()

    accepted: Tuple[str, ...] = ()
    raw_accepted = entry.get("accepted")
    if raw_accepted is not None:
        if not isinstance(raw_accepted, list):
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: 'accepted' must be a list of model-id strings "
                f"(got {type(raw_accepted).__name__})",
                "Change to:\n  accepted:\n    - <model-id>",
            ))
            has_error = True
        else:
            items: List[str] = []
            for i, item in enumerate(raw_accepted, 1):
                if not isinstance(item, str) or not item.strip():
                    issues.append(ConfigIssue(
                        "error",
                        f"{prefix}: accepted #{i} must be a non-empty model-id string",
                        "List plain model ids (dash forms match live dotted models)",
                    ))
                    has_error = True
                    continue
                items.append(item.strip())
            accepted = tuple(items)

    fallbacks: Tuple[FallbackSpec, ...] = ()
    raw_fallbacks = entry.get("fallbacks")
    if raw_fallbacks is not None:
        if not isinstance(raw_fallbacks, list):
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: 'fallbacks' must be a list (got {type(raw_fallbacks).__name__})",
                "Change to:\n  fallbacks:\n    - provider: <name>\n      model: <model-id>",
            ))
            has_error = True
        else:
            parsed: List[FallbackSpec] = []
            for i, item in enumerate(raw_fallbacks, 1):
                fb = _parse_fallback(name, i, item, cfg, known_providers, issues)
                if fb is None:
                    # A broken chain is worse than no route.
                    has_error = True
                    continue
                parsed.append(fb)
            fallbacks = tuple(parsed)

    repromote_after_turns: Optional[int] = None
    raw_repromote = entry.get("repromote_after_turns")
    if raw_repromote is not None:
        # Unlike the shared numeric validators, zero is meaningful here. A
        # bad tuning value only warns: it must not invalidate the whole route.
        if (
            isinstance(raw_repromote, int)
            and not isinstance(raw_repromote, bool)
            and raw_repromote >= 0
        ):
            repromote_after_turns = raw_repromote
        else:
            issues.append(ConfigIssue(
                "warning",
                f"{prefix}: repromote_after_turns must be an integer >= 0 "
                f"(got {raw_repromote!r}) — router default inherited",
                "Use 0 to disable re-promotion for this route, or omit to inherit "
                "router.repromote_after_turns",
            ))

    if has_error:
        return None
    return RouteSpec(
        name=name,
        description=description,
        provider=provider,
        model=model,
        reasoning_effort=effort,
        accepted=accepted,
        fallbacks=fallbacks,
        repromote_after_turns=repromote_after_turns,
    )


def _parse_routes(raw: Any, cfg: Dict[str, Any], issues: List[ConfigIssue]) -> Dict[str, RouteSpec]:
    routes: Dict[str, RouteSpec] = {}
    if raw is None:
        return routes
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            "error",
            f"model_routes: 'routes' must be a mapping (got {type(raw).__name__})",
            "Change to:\n  routes:\n    <route-name>:\n      provider: <name>\n      model: <model-id>",
        ))
        return routes

    # YAML silently merges exact duplicate keys, so duplicates are only
    # detectable as case-insensitive collisions (dev vs DEV).
    by_lower: Dict[str, List[str]] = {}
    for name in raw:
        by_lower.setdefault(_norm(name), []).append(str(name))
    collided: set = set()
    for lowered, group in by_lower.items():
        if len(group) > 1:
            collided.add(lowered)
            issues.append(ConfigIssue(
                "error",
                f"model_routes: route names {sorted(group)} collide case-insensitively — all dropped",
                "Route lookup is case-insensitive; keep exactly one spelling per route",
            ))

    known_providers = _known_provider_names(cfg)
    for name, entry in raw.items():
        if _norm(name) in collided:
            continue
        spec = _parse_route(str(name), entry, cfg, known_providers, issues)
        if spec is not None:
            routes[spec.name] = spec
    return routes


def _parse_health(raw: Any, issues: List[ConfigIssue]) -> HealthConfig:
    if raw is None:
        return HealthConfig()
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            "warning",
            f"model_routes: 'health' must be a mapping (got {type(raw).__name__}) — defaults used",
            f"Supported health keys: {', '.join(sorted(_HEALTH_KEYS))}",
        ))
        return HealthConfig()

    kwargs: Dict[str, Any] = {}
    for key in sorted(set(raw) - _HEALTH_KEYS):
        issues.append(ConfigIssue(
            "warning",
            f"model_routes: unknown key '{key}' under health ignored",
            f"Supported health keys: {', '.join(sorted(_HEALTH_KEYS))}",
        ))
    if "enabled" in raw:
        enabled = raw["enabled"]
        if isinstance(enabled, bool):
            kwargs["enabled"] = enabled
        else:
            # bool("false") is True — silently coercing would keep probing
            # enabled against the author's intent, so warn + default instead
            # (same treatment as cache_path / the numeric keys).
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: health.enabled must be a boolean "
                f"(got {enabled!r}) — default (true) used",
                "Use an unquoted YAML boolean: enabled: false",
            ))
    if "cache_path" in raw:
        cache_path = raw["cache_path"]
        if isinstance(cache_path, str):
            kwargs["cache_path"] = cache_path.strip()
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: health.cache_path must be a string "
                f"(got {type(cache_path).__name__}) — default used",
                'Use "" for the default <hermes home>/state/model_route_health.json',
            ))
    for key in _HEALTH_NUMERIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            kwargs[key] = float(value)
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: health.{key} must be a number > 0 ({value!r}) — default used",
                f"Example: {key}: {getattr(HealthConfig(), key)}",
            ))
    return HealthConfig(**kwargs)


def _load_health_config_readonly() -> HealthConfig:
    """Read only the health sub-block without parsing the route catalog.

    The completion-success hook calls :func:`has_unhealthy_verdicts` after
    every real response.  Loading the full catalog there would repeatedly
    enumerate providers and validate routes just to discover one cache path.
    ``load_config_readonly`` retains config-file invalidation while avoiding
    the defensive deepcopy; parsing this tiny sub-block is read-only.
    """
    try:
        cfg = load_config_readonly()
        section = cfg.get("model_routes") if isinstance(cfg, dict) else None
        raw = section.get("health") if isinstance(section, dict) else None
        return _parse_health(raw, [])
    except Exception as exc:
        logger.debug(
            "model_routes: lightweight health config load failed (%s)",
            type(exc).__name__,
        )
        return HealthConfig()


def _parse_static_rules(
    raw: Any,
    routes: Dict[str, RouteSpec],
    issues: List[ConfigIssue],
) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.append(ConfigIssue(
            "error",
            f"model_routes: 'static_rules' must be a list (got {type(raw).__name__})",
            "Change to:\n  static_rules:\n    - route: <route-name>\n      when: {<condition>: <value>}",
        ))
        return []

    valid_names = {_norm(name) for name in routes}
    rules: List[Dict[str, Any]] = []
    for i, item in enumerate(raw, 1):
        prefix = f"model_routes: static_rules #{i}"
        if not isinstance(item, dict):
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: must be a mapping (got {type(item).__name__})",
                "Each rule needs: route (a declared route) and when (a non-empty mapping)",
            ))
            continue
        dropped = False
        for key in sorted(set(item) - _RULE_KEYS):
            issues.append(ConfigIssue(
                "warning",
                f"{prefix}: unknown key '{key}' ignored",
                f"Supported rule keys: {', '.join(sorted(_RULE_KEYS))}",
            ))
        route = item.get("route")
        if not isinstance(route, str) or _norm(route) not in valid_names:
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: 'route' {route!r} does not name a declared valid route",
                "Point the rule at a route declared under model_routes.routes",
            ))
            dropped = True
        when = item.get("when")
        if not isinstance(when, dict) or not when:
            issues.append(ConfigIssue(
                "error",
                f"{prefix}: 'when' must be a non-empty mapping",
                "Conditions: is_owner/chat_id/parent_chat_id/user_id/platform/chat_type "
                "({eq|in|not_in: ...}) and text_matches_any: [regex, ...]",
            ))
            dropped = True
        if isinstance(when, dict) and "is_owner" in when:
            # Same footgun class as health.enabled: YAML string "false" is
            # truthy, and bool-coercing it would invert the author's intent.
            # The matcher (gateway/model_router.py) requires a real bool and
            # never matches otherwise — surface that at parse time.
            _owner_cond = when["is_owner"]
            _owner_eq = _owner_cond.get("eq") if isinstance(_owner_cond, dict) else None
            if not isinstance(_owner_cond, dict) or set(_owner_cond) != {"eq"} or not isinstance(_owner_eq, bool):
                issues.append(ConfigIssue(
                    "warning",
                    f"{prefix}: is_owner condition must be {{eq: <boolean>}} "
                    f"(got {_owner_cond!r}) — this rule will never match",
                    "Use an unquoted YAML boolean: is_owner: {eq: false}",
                ))
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            issues.append(ConfigIssue(
                "warning",
                f"{prefix}: 'reason' must be a string (got {type(reason).__name__})",
                "Use a short human-readable explanation, or omit it",
            ))
        rule_name = item.get("name")
        if rule_name is not None and (not isinstance(rule_name, str) or not rule_name.strip()):
            issues.append(ConfigIssue(
                "warning",
                f"{prefix}: 'name' must be a non-empty string (got {rule_name!r}) — ignored",
                "Name the rule for decision-log attribution, or omit it",
            ))
        if not dropped:
            rules.append(item)
    return rules


def _parse_refusal(raw: Any, issues: List[ConfigIssue]) -> RefusalConfig:
    if raw is None:
        return RefusalConfig()
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            "error",
            f"model_routes: router.refusal must be a mapping "
            f"(got {type(raw).__name__}) — refusal routing stays disabled",
            f"Supported refusal keys: {', '.join(sorted(_REFUSAL_KEYS))}",
        ))
        return RefusalConfig()

    for key in sorted(set(raw) - _REFUSAL_KEYS):
        issues.append(ConfigIssue(
            "warning",
            f"model_routes: unknown key '{key}' under router.refusal ignored",
            f"Supported refusal keys: {', '.join(sorted(_REFUSAL_KEYS))}",
        ))

    kwargs: Dict[str, Any] = {}
    for key in ("enabled", "api_fallback", "clean_fork", "notify"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool):
            kwargs[key] = value
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.refusal.{key} must be a boolean "
                f"(got {value!r}) — default used",
                f"Use an unquoted YAML boolean: {key}: "
                f"{'true' if getattr(RefusalConfig(), key) else 'false'}",
            ))

    if "keep_user_turns" in raw:
        value = raw["keep_user_turns"]
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            kwargs["keep_user_turns"] = value
        else:
            issues.append(ConfigIssue(
                "warning",
                "model_routes: router.refusal.keep_user_turns must be an "
                f"integer > 0 (got {value!r}) — default used",
                f"Example: keep_user_turns: {RefusalConfig().keep_user_turns}",
            ))

    if "min_confidence" in raw:
        value = raw["min_confidence"]
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= float(value) <= 1
        ):
            kwargs["min_confidence"] = float(value)
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.refusal.min_confidence must be a number "
                f"from 0 to 1 ({value!r}) — default used",
                f"Example: min_confidence: {RefusalConfig().min_confidence}",
            ))

    for key in ("dev_route", "chat_route", "document_route"):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, str):
            kwargs[key] = value.strip()
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.refusal.{key} must be a string "
                f"(got {type(value).__name__}) — default used",
                f"Example: {key}: {getattr(RefusalConfig(), key)!r}",
            ))

    return RefusalConfig(**kwargs)


def _parse_router(
    raw: Any,
    routes: Dict[str, RouteSpec],
    cfg: Dict[str, Any],
    issues: List[ConfigIssue],
) -> RouterConfig:
    if raw is None:
        return _with_effective_router_mode(RouterConfig())
    if not isinstance(raw, dict):
        issues.append(ConfigIssue(
            "error",
            f"model_routes: 'router' must be a mapping (got {type(raw).__name__}) — router stays off",
            f"Supported router keys: {', '.join(sorted(_ROUTER_KEYS))}",
        ))
        return _with_effective_router_mode(RouterConfig())

    for key in sorted(set(raw) - _ROUTER_KEYS):
        issues.append(ConfigIssue(
            "warning",
            f"model_routes: unknown key '{key}' under router ignored",
            f"Supported router keys: {', '.join(sorted(_ROUTER_KEYS))}",
        ))

    kwargs: Dict[str, Any] = {}

    if "mode" in raw:
        mode_raw = raw["mode"]
        # YAML 1.1 parses an unquoted ``off`` as boolean False — accept it as
        # the documented default rather than erroring on the example spelling.
        if mode_raw is False:
            kwargs["mode"] = "off"
        elif isinstance(mode_raw, str) and mode_raw.strip().lower() in _ROUTER_MODES:
            kwargs["mode"] = mode_raw.strip().lower()
        else:
            issues.append(ConfigIssue(
                "error",
                f"model_routes: router.mode must be one of {'|'.join(_ROUTER_MODES)} "
                f"(got {mode_raw!r}) — router stays off",
                "Example: mode: shadow",
            ))

    provider_norm = DEFAULT_ROUTER_PROVIDER
    if "provider" in raw:
        provider = raw["provider"]
        if isinstance(provider, str) and provider.strip():
            from hermes_cli.runtime_provider import _normalize_custom_provider_name

            candidate = provider.strip()
            candidate_norm = _normalize_custom_provider_name(candidate)
            if candidate_norm in _known_provider_names(cfg):
                kwargs["provider"] = candidate
                provider_norm = candidate_norm
            else:
                issues.append(ConfigIssue(
                    "error",
                    f"model_routes: router.provider names unknown provider {candidate!r} "
                    f"— default ({DEFAULT_ROUTER_PROVIDER}) used",
                    "Declare it under providers: in config.yaml (or use a built-in provider id)",
                ))
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.provider must be a non-empty string "
                f"(got {provider!r}) — default used",
                f"Example: provider: {DEFAULT_ROUTER_PROVIDER}",
            ))

    if "model" in raw:
        model = raw["model"]
        if isinstance(model, str) and model.strip():
            kwargs["model"] = model.strip()
            declared = _declared_provider_models(cfg, provider_norm)
            if declared and not any(_model_matches(model.strip(), key) for key in declared):
                issues.append(ConfigIssue(
                    "warning",
                    f"model_routes: router.model {model.strip()!r} is not in classifier "
                    f"provider {kwargs.get('provider', DEFAULT_ROUTER_PROVIDER)!r} declared models",
                    "Check for a typo, or add the model under the provider's models: mapping",
                ))
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.model must be a non-empty string "
                f"(got {model!r}) — default used",
                f"Example: model: {DEFAULT_ROUTER_MODEL}",
            ))

    for key in _ROUTER_NUMERIC_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            kwargs[key] = (
                int(value)
                if key in {"recent_turns", "normal_downgrade_streak"}
                else float(value)
            )
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.{key} must be a number > 0 ({value!r}) — default used",
                f"Example: {key}: {getattr(RouterConfig(), key)}",
            ))

    if "repromote_after_turns" in raw:
        value = raw["repromote_after_turns"]
        # Not part of _ROUTER_NUMERIC_KEYS: an explicit zero disables the
        # feature and must not warn then silently fall back to the default.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            kwargs["repromote_after_turns"] = value
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.repromote_after_turns must be an integer >= 0 "
                f"({value!r}) — default used",
                f"Example: repromote_after_turns: {RouterConfig().repromote_after_turns} "
                "(0 disables re-promotion)",
            ))

    valid_names = {_norm(name) for name in routes}

    if "chat_route" in raw:
        chat_route = raw["chat_route"]
        if not isinstance(chat_route, str):
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.chat_route must be a string "
                f"(got {type(chat_route).__name__}) — downgrades disabled",
                'Use "" to disable NORMAL→chat downgrades',
            ))
        elif chat_route.strip() and _norm(chat_route) not in valid_names:
            issues.append(ConfigIssue(
                "error",
                f"model_routes: router.chat_route {chat_route.strip()!r} does not name "
                "a declared valid route — downgrades disabled",
                "Point chat_route at a route declared under model_routes.routes",
            ))
        else:
            kwargs["chat_route"] = chat_route.strip()

    if "label_routes" in raw:
        label_routes = raw["label_routes"]
        if not isinstance(label_routes, dict):
            issues.append(ConfigIssue(
                "error",
                f"model_routes: router.label_routes must be a mapping "
                f"(got {type(label_routes).__name__}) — ignored",
                "Change to:\n  label_routes:\n    SYSTEM_DEV: <route-name>",
            ))
        else:
            pairs: List[Tuple[str, str]] = []
            for label, route in label_routes.items():
                label_text = str(label).strip().upper()
                if label_text not in _ROUTER_LABELS:
                    issues.append(ConfigIssue(
                        "warning",
                        f"model_routes: router.label_routes key {label!r} is not a "
                        "classifier label — ignored",
                        f"Valid labels: {', '.join(_ROUTER_LABELS)} "
                        "(NORMAL downgrades via chat_route)",
                    ))
                    continue
                if route is None or (isinstance(route, str) and not route.strip()):
                    continue  # explicit "": this label never switches
                if not isinstance(route, str) or _norm(route) not in valid_names:
                    issues.append(ConfigIssue(
                        "error",
                        f"model_routes: router.label_routes.{label_text} {route!r} does "
                        "not name a declared valid route — label disabled",
                        "Point the label at a route declared under model_routes.routes",
                    ))
                    continue
                pairs.append((label_text, route.strip()))
            kwargs["label_routes"] = tuple(pairs)

    if "decision_log" in raw:
        decision_log = raw["decision_log"]
        if isinstance(decision_log, str):
            kwargs["decision_log"] = decision_log.strip()
        else:
            issues.append(ConfigIssue(
                "warning",
                f"model_routes: router.decision_log must be a string "
                f"(got {type(decision_log).__name__}) — default used",
                'Use "" for the default <hermes home>/logs/model_router_decisions.jsonl',
            ))

    if "refusal" in raw:
        kwargs["refusal"] = _parse_refusal(raw["refusal"], issues)

    router = _with_effective_router_mode(RouterConfig(**kwargs))
    if router.mode != "off" and not routes:
        issues.append(ConfigIssue(
            "warning",
            f"model_routes: router.mode is '{router.mode}' but no valid routes are "
            "declared — every decision will be a no-op",
            "Declare routes under model_routes.routes (see cli-config.yaml.example)",
        ))
    return router


def load_routes(cfg: Optional[Dict[str, Any]] = None) -> RouteCatalog:
    """Parse+validate ``cfg["model_routes"]`` into a :class:`RouteCatalog`.

    Absent/empty section → dormant catalog (no routes, default health, no
    issues).  Routes with any error-severity violation are dropped.
    """
    if cfg is None:
        cfg = load_config()

    catalog = RouteCatalog()
    section = cfg.get("model_routes")
    if not section:
        catalog.router = _with_effective_router_mode(catalog.router)
        return catalog
    if not isinstance(section, dict):
        catalog.router = _with_effective_router_mode(catalog.router)
        catalog.issues.append(ConfigIssue(
            "error",
            f"model_routes must be a mapping (got {type(section).__name__})",
            "See cli-config.yaml.example for the model_routes schema",
        ))
        return catalog

    for key in sorted(set(section) - _SECTION_KEYS):
        catalog.issues.append(ConfigIssue(
            "warning",
            f"model_routes: unknown key '{key}' under model_routes ignored",
            f"Supported keys: {', '.join(sorted(_SECTION_KEYS))}",
        ))

    catalog.routes = _parse_routes(section.get("routes"), cfg, catalog.issues)
    catalog.health = _parse_health(section.get("health"), catalog.issues)
    catalog.static_rules = _parse_static_rules(section.get("static_rules"), catalog.routes, catalog.issues)
    catalog.router = _parse_router(section.get("router"), catalog.routes, cfg, catalog.issues)
    return catalog


def validate_model_routes(cfg: Optional[Dict[str, Any]] = None) -> List[ConfigIssue]:
    """Config-validation hook — called by ``config.validate_config_structure``."""
    if cfg is None:
        cfg = load_config()
    catalog = load_routes(cfg)
    issues = catalog.issues

    # delegation.default_route lives OUTSIDE the model_routes section but must
    # name a declared route — checked here (not in load_routes) so a
    # default_route with no model_routes section at all still warns.
    delegation = cfg.get("delegation")
    default_route = delegation.get("default_route") if isinstance(delegation, dict) else None
    if default_route is not None:
        if not isinstance(default_route, str):
            issues.append(ConfigIssue(
                "warning",
                f"delegation.default_route must be a string "
                f"(got {type(default_route).__name__}) — ignored",
                "Name a route declared under model_routes.routes, or omit the key",
            ))
        elif default_route.strip() and _lookup_route(catalog, default_route) is None:
            issues.append(ConfigIssue(
                "warning",
                f"delegation.default_route {default_route.strip()!r} does not name a "
                "declared valid route — every delegate_task call without an explicit "
                "route will fail",
                "Declare it under model_routes.routes, or remove delegation.default_route",
            ))
    return issues


# =============================================================================
# Resolution
# =============================================================================


def _lookup_route(catalog: RouteCatalog, route_name: str) -> Optional[RouteSpec]:
    name = str(route_name or "").strip().lower()
    if not name:
        return None
    for spec in catalog.routes.values():
        if spec.name.strip().lower() == name:
            return spec
    return None


def resolve_route_detailed(
    route_name: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    catalog: Optional[RouteCatalog] = None,
    allow_recovery_probe: bool = False,
) -> RouteResolution:
    """Walk default → fallbacks and retain why resolution did or did not win.

    Resolution is observation-only by default: cached health is read, but a
    stale unhealthy verdict is not actively re-probed and the shared cache is
    never rewritten. A live caller that is authorized to spend provider quota
    and update shared health must pass ``allow_recovery_probe=True`` explicitly.

    ``directive`` is ``None`` for unknown routes or when the whole chain is
    unhealthy (callers emit no switch and stay put — never route to a dead
    provider). ``reason`` remains populated so observation logs can distinguish
    an unknown route, cached failure, and a suppressed recovery probe.
    """
    catalog = catalog or load_routes(cfg)
    spec = _lookup_route(catalog, route_name)
    if spec is None:
        logger.warning("model_routes: unknown route %r", route_name)
        return RouteResolution(None, f"unknown route {str(route_name or '').strip()!r}")

    chain: List[Tuple[str, str, str, str]] = [
        (spec.provider, spec.model, spec.reasoning_effort, "default")
    ]
    for i, fb in enumerate(spec.fallbacks, 1):  # source index is 1-based
        chain.append((fb.provider, fb.model, fb.reasoning_effort, f"fallback:{i}"))

    failures: List[str] = []
    for provider, model, effort, source in chain:
        healthy, reason = provider_health(
            provider,
            model,
            cfg=cfg,
            health=catalog.health,
            allow_recovery_probe=allow_recovery_probe,
        )
        if healthy:
            directive = {
                "route": spec.name,
                "provider": provider,
                "model": model,
                "reasoning_effort": effort or "",
                "source": source,
                "reason": f"failover — {'; '.join(failures)}" if failures else "",
            }
            resolution_reason = directive["reason"] or (
                f"selected {source} {provider}/{model} ({reason})"
            )
            return RouteResolution(directive, resolution_reason)
        failures.append(f"{provider} unhealthy ({reason})")

    resolution_reason = f"no healthy runtime — {'; '.join(failures)}"
    logger.warning(
        "model_routes: route %r has no healthy runtime: %s",
        spec.name, "; ".join(failures),
    )
    return RouteResolution(None, resolution_reason)


def resolve_route(
    route_name: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    catalog: Optional[RouteCatalog] = None,
    allow_recovery_probe: bool = False,
) -> Optional[Dict[str, str]]:
    """Return the first cached-healthy runtime; active recovery is opt-in.

    The safe default is read-only and performs no provider I/O. Live callers
    may explicitly pass ``allow_recovery_probe=True`` to re-check a stale
    unhealthy verdict and persist the recovery result.
    """
    return resolve_route_detailed(
        route_name,
        cfg,
        catalog=catalog,
        allow_recovery_probe=allow_recovery_probe,
    ).directive


def resolve_route_runtime(
    route_name: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    catalog: Optional[RouteCatalog] = None,
    allow_recovery_probe: bool = False,
) -> Optional[Dict[str, str]]:
    """Resolve a healthy route through the canonical runtime-provider chain.

    The returned snapshot is deliberately secret-free: it proves that the
    selected provider/model can be resolved and exposes only the fields a
    router may safely classify or audit.  Credentials remain inside
    :func:`hermes_cli.runtime_provider.resolve_runtime_provider` and are never
    copied into route state or decision logs.
    """
    directive = resolve_route(
        route_name,
        cfg,
        catalog=catalog,
        allow_recovery_probe=allow_recovery_probe,
    )
    if directive is None:
        return None
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        resolved = resolve_runtime_provider(
            requested=directive["provider"],
            target_model=directive["model"],
        )
    except Exception as exc:
        logger.debug(
            "model_routes: runtime resolution failed for route %r (%s)",
            directive.get("route") or route_name,
            type(exc).__name__,
        )
        return None
    if not isinstance(resolved, dict):
        return None
    return {
        "route": directive["route"],
        "provider": str(resolved.get("provider") or directive["provider"]),
        "model": directive["model"],
        "reasoning_effort": directive["reasoning_effort"],
        "api_mode": str(resolved.get("api_mode") or ""),
        "source": directive["source"],
        "reason": directive["reason"],
    }


# =============================================================================
# Provider health probing (ported from skill-gate runtime_catalog.py)
# =============================================================================


def _health_checks_enabled(health: HealthConfig) -> bool:
    override = os.environ.get(_HEALTH_ENV, "").strip().lower()
    if override in ("0", "false", "off"):
        return False
    if override in ("1", "true", "on"):
        return True
    return health.enabled


def _cfg_runtime_fallback(provider: str, cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Minimal probe runtime straight from the ``providers:`` entry, key-less.

    Preserves skill-gate semantics: a known base_url with a missing key still
    probes (a 401 answer then counts healthy via fail-open); no entry → {}.
    """
    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    if cfg is None:
        try:
            cfg = load_config()
        except Exception as exc:
            logger.debug(
                "model_routes: config load failed during probe fallback (%s)",
                type(exc).__name__,
            )
            return {}
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(providers, dict):
        return {}
    target = _normalize_custom_provider_name(str(provider or ""))
    if not target:
        return {}
    for key, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        entry_names = {_normalize_custom_provider_name(str(key))}
        raw_name = entry.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            entry_names.add(_normalize_custom_provider_name(raw_name))
        if target not in entry_names:
            continue
        base_url = ""
        for url_key in ("base_url", "url", "api"):
            value = entry.get(url_key)
            if isinstance(value, str) and value.strip():
                base_url = value.strip()
                break
        return {
            "base_url": base_url,
            "api_mode": str(entry.get("api_mode") or ""),
            "api_key": "",
            "default_model": str(entry.get("default_model") or entry.get("model") or ""),
        }
    return {}


def _probe_provider(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
    health: HealthConfig,
) -> Tuple[bool, str]:
    """One live probe. anthropic_messages mode sends a 1-token message (also
    catches credit exhaustion); OpenAI-compatible modes GET /models."""
    runtime: Dict[str, Any] = {}
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        resolved = resolve_runtime_provider(requested=provider, target_model=model or None)
        if isinstance(resolved, dict) and resolved.get("base_url"):
            runtime = resolved
    except Exception as exc:
        logger.debug(
            "model_routes: runtime resolution failed for %r (%s)",
            provider,
            type(exc).__name__,
        )
    if not runtime:
        runtime = _cfg_runtime_fallback(provider, cfg)

    base = str(runtime.get("base_url") or "").rstrip("/")
    if not base:
        return False, "no base_url resolved"
    key = str(runtime.get("api_key") or "")
    api_mode = str(runtime.get("api_mode") or "")
    try:
        if api_mode == "anthropic_messages":
            url = f"{base}/v1/messages"
            body = json.dumps({
                "model": str(model or runtime.get("default_model") or runtime.get("model") or ""),
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }).encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "x-api-key": key,
                "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01",
            })
        else:
            url = base + ("/models" if base.endswith("/v1") else "/v1/models")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with _urlopen(req, timeout=health.probe_timeout_seconds) as resp:
            code = getattr(resp, "status", 200)
        return (200 <= code < 300), f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        # Fail-open semantics: only signals that indicate the PROVIDER cannot
        # serve completions count as unhealthy — credit/quota exhaustion
        # (body sniff; Anthropic reports low credit as HTTP 400), 402/429,
        # and 5xx. Auth-scoped 401/403 (or a malformed probe 400) usually
        # means OUR probe credentials/shape are off, not that the provider is
        # down — treat as healthy so a probe defect can never freeze routing.
        body = ""
        try:
            body = exc.read(500).decode("utf-8", "replace").lower()
        except Exception:
            body = ""
        if any(word in body for word in _CREDIT_SNIFF_KEYWORDS):
            return False, f"HTTP {exc.code} (credit/quota)"
        if exc.code in (402, 429) or exc.code >= 500:
            return False, f"HTTP {exc.code}"
        return True, f"assumed healthy (auth-scoped HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001
        # Connection refused / DNS / timeout — provider is genuinely unreachable.
        return False, str(exc)[:80]


def _read_health_cache(path: Path) -> Dict[str, Any]:
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _health_entry_timestamp(entry: Any) -> float:
    if not isinstance(entry, dict):
        return float("-inf")
    try:
        return float(entry.get("ts"))
    except (TypeError, ValueError):
        return float("-inf")


def _health_cache_signature(path: Path) -> Optional[Tuple[str, int, int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        str(path),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        int(getattr(stat, "st_ino", 0) or 0),
    )


def _update_unhealthy_memo_locked(path: Path, cache: Dict[str, Any]) -> None:
    _unhealthy_memo["mtime"] = _health_cache_signature(path)
    _unhealthy_memo["cache"] = cache
    _unhealthy_memo["value"] = any(
        isinstance(verdict, dict) and not verdict.get("healthy")
        for verdict in cache.values()
    )


def _read_health_cache_memoized_locked(path: Path) -> Dict[str, Any]:
    """Return the cached read-only snapshot when the file is unchanged."""
    signature = _health_cache_signature(path)
    if signature is None:
        _unhealthy_memo["mtime"] = None
        _unhealthy_memo["cache"] = {}
        _unhealthy_memo["value"] = False
        return {}
    cached = _unhealthy_memo.get("cache")
    if _unhealthy_memo.get("mtime") == signature and isinstance(cached, dict):
        return cached
    cache = _read_health_cache(path)
    _update_unhealthy_memo_locked(path, cache)
    return cache


def _store_health_verdict(path: Path, key: str, entry: Dict[str, Any]) -> bool:
    """Merge one verdict into the shared cache under an exclusive flock.

    Concurrent hermes processes (gateway + interactive CLI) share this file;
    a whole-file read-modify-write from a pre-probe snapshot would let one
    process clobber the other's fresh verdict (lost update), dropping its
    fail_ttl suppression and re-blocking on a dead provider.  Re-reading
    inside the lock means every merge starts from the latest snapshot.
    Older observations never replace newer ones. At equal timestamps a healthy
    verdict wins, preventing a delayed stale failure from undoing recovery.
    Best-effort: returns ``False`` when rejected or not cached.
    """
    with _health_state_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(path.name + ".lock")
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                locked = False
                if fcntl is not None:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                        locked = True
                    except OSError as exc:
                        # e.g. ENOLCK on NFS without lockd, or FUSE mounts that
                        # reject flock — degrade to the lock-less re-read+merge
                        # rather than skipping the cache write entirely.
                        logger.debug(
                            "model_routes: flock unavailable for %s; writing lock-less (%s)",
                            lock_path,
                            type(exc).__name__,
                        )
                try:
                    cache = _read_health_cache(path)
                    existing = cache.get(key)
                    existing_ts = _health_entry_timestamp(existing)
                    incoming_ts = _health_entry_timestamp(entry)
                    stale = existing_ts > incoming_ts or (
                        existing_ts == incoming_ts
                        and isinstance(existing, dict)
                        and bool(existing.get("healthy"))
                        and not bool(entry.get("healthy"))
                    )
                    if stale:
                        _update_unhealthy_memo_locked(path, cache)
                        return False
                    cache[key] = entry
                    atomic_json_write(path, cache)
                    _update_unhealthy_memo_locked(path, cache)
                    return True
                finally:
                    if locked:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            logger.debug(
                "model_routes: health cache write failed for %s (%s)",
                path,
                type(exc).__name__,
            )
            return False


def provider_health(
    provider: str,
    model: str = "",
    *,
    cfg: Optional[Dict[str, Any]] = None,
    health: Optional[HealthConfig] = None,
    allow_recovery_probe: bool = False,
) -> Tuple[bool, str]:
    """Cached passive health verdict with opt-in fail-open recovery probing.

    Passive-first semantics:

    - fresh cache entry (within its TTL) → cached verdict, no I/O
    - no entry, or a stale *healthy* entry → assumed healthy, no probe —
      real traffic (``record_provider_outcome``) is the health signal
    - stale *unhealthy* entry → remains unhealthy by default (read-only); a
      caller may explicitly opt into one live recovery probe, so a session
      parked on a fallback can be walked back to a healed primary without
      waiting for someone else's traffic to prove it

    The recovery probe is the only active-probe path left and is fail-closed
    behind ``allow_recovery_probe is True``. A healthy provider is never
    probed, so steady state costs zero network calls and zero completion
    tokens.
    """
    if health is None:
        health = load_routes(cfg).health
    if not _health_checks_enabled(health):
        return True, "health checks disabled"
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(_HEALTH_TEST_ENV):
        return True, "pytest"

    path = health.resolved_cache_path()
    with _health_state_lock:
        cache = _read_health_cache_memoized_locked(path)

    now = _now()
    key = str(provider or "")
    if isinstance(cfg, dict):
        # Passive outcomes are filed under the canonical ``providers:`` key.
        # Routes may legally use that entry's display name or different case;
        # normalize those aliases so the resolver sees the same verdict.
        key = provider_key_for_runtime(provider=key, cfg=cfg) or key
    entry = cache.get(key)
    last_unhealthy = False
    if isinstance(entry, dict):
        try:
            ts = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        ttl = health.ok_ttl_seconds if entry.get("healthy") else health.fail_ttl_seconds
        if now - ts < ttl:
            return bool(entry.get("healthy")), str(entry.get("reason") or "cached")
        last_unhealthy = not bool(entry.get("healthy"))

    if not last_unhealthy:
        # No verdict, or the last one was healthy: trust it without probing.
        # A wrong assumption self-corrects on the first real failure via
        # record_provider_outcome; a probe here would burn a completion per
        # ok_ttl window against a provider that real traffic already covers.
        return True, "assumed healthy (passive; no fresh failure verdict)"

    if allow_recovery_probe is not True:
        cached_reason = str(entry.get("reason") or "cached unhealthy")
        return False, (
            "stale unhealthy verdict (recovery probe suppressed): "
            f"{cached_reason}"
        )

    healthy, reason = _probe_provider(key, str(model or ""), cfg, health)
    stored = _store_health_verdict(
        path,
        key,
        {"healthy": healthy, "reason": f"recovery probe: {reason}", "ts": now},
    )
    if not stored:
        latest = _read_health_cache(path).get(key)
        if isinstance(latest, dict) and _health_entry_timestamp(latest) >= now:
            return bool(latest.get("healthy")), str(
                latest.get("reason") or "newer cached verdict"
            )
    return healthy, reason


# =============================================================================
# Passive health — verdicts from real completion traffic
# =============================================================================

# Burst guard for repeated unhealthy writes: a fallback-chain walk can report
# the same dead provider several times within one turn (recursive skip calls,
# multi-entry chains). One verdict per provider per window is plenty — the
# suppression is in-process only, so concurrent hermes processes still land
# their own (idempotent) writes via the flock merge.
_PASSIVE_WRITE_SUPPRESS_SECONDS = 5.0
_last_passive_unhealthy_write: Dict[str, float] = {}

# mtime-keyed memo for has_unhealthy_verdicts() — the completion-success hook
# runs per API call, so steady state is limited to cached config metadata plus
# one cache-file stat, not full route parsing or a JSON read.
_unhealthy_memo: Dict[str, Any] = {"mtime": None, "value": False}


def provider_key_for_runtime(
    provider: str = "",
    base_url: str = "",
    cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Map an agent's runtime identity to its ``providers:`` config key.

    The health cache (and route specs) key providers by their config-entry
    name (e.g. ``claude-lb``); the live agent may carry that key, a
    ``custom:<name>`` menu slug, a display name (``Claude LB 114``), or only
    a ``base_url``. Returns ``""`` when nothing matches — callers must treat
    that as "don't record" (never guess a key: a verdict filed under the
    wrong provider is worse than no verdict).
    """
    from hermes_cli.runtime_provider import _normalize_custom_provider_name

    if cfg is None:
        try:
            cfg = load_config()
        except Exception as exc:
            logger.debug(
                "model_routes: config load failed during provider-key mapping (%s)",
                type(exc).__name__,
            )
            return ""
    providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if not isinstance(providers, dict):
        return ""

    target = _normalize_custom_provider_name(str(provider or ""))
    if target.startswith("custom:"):
        target = target[len("custom:"):]
    base = str(base_url or "").strip().rstrip("/").lower()

    url_match = ""
    for key, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        key_str = str(key)
        names = {_normalize_custom_provider_name(key_str)}
        raw_name = entry.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            names.add(_normalize_custom_provider_name(raw_name))
        if target and target in names:
            return key_str
        if base and not url_match:
            for url_key in ("base_url", "url", "api"):
                value = entry.get(url_key)
                if isinstance(value, str) and value.strip().rstrip("/").lower() == base:
                    url_match = key_str
                    break
    return url_match


def record_provider_outcome(
    provider_key: str,
    healthy: bool,
    reason: str,
    *,
    health: Optional[HealthConfig] = None,
) -> None:
    """File a health verdict observed from a real completion attempt.

    - ``healthy=False``: the agent gave up on this provider for an
      outage-shaped reason (5xx/429/402/credit/timeout). Written (almost)
      always — refreshing ``ts`` keeps the fail-TTL suppression alive while
      errors keep flowing.
    - ``healthy=True``: a live completion succeeded. Only *clears* an
      existing unhealthy verdict; it never creates or refreshes entries, so
      the steady-state success path stays write-free.

    Best-effort by contract: never raises, never blocks routing.
    """
    key = str(provider_key or "").strip()
    if not key:
        return
    try:
        if health is None:
            health = _load_health_config_readonly()
        if not _health_checks_enabled(health):
            return
        if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(_HEALTH_TEST_ENV):
            return  # same seam as provider_health — agent-path unit tests must not touch the real cache
        path = health.resolved_cache_path()
        observed_at = _now()
        with _health_state_lock:
            if healthy:
                entry = _read_health_cache(path).get(key)
                if not (isinstance(entry, dict) and not entry.get("healthy")):
                    return  # nothing to clear
            else:
                last = _last_passive_unhealthy_write.get(key, 0.0)
                if observed_at - last < _PASSIVE_WRITE_SUPPRESS_SECONDS:
                    return
            stored = _store_health_verdict(
                path,
                key,
                {
                    "healthy": bool(healthy),
                    "reason": f"passive: {reason}",
                    "ts": observed_at,
                },
            )
            if stored:
                if healthy:
                    _last_passive_unhealthy_write.pop(key, None)
                else:
                    _last_passive_unhealthy_write[key] = observed_at
    except Exception as exc:
        logger.debug(
            "model_routes: passive health record failed for %r (%s)",
            key,
            type(exc).__name__,
        )


def has_unhealthy_verdicts(health: Optional[HealthConfig] = None) -> bool:
    """True when the health cache currently holds any unhealthy verdict.

    Cheap gate for the per-completion success hook: a cached config metadata
    lookup plus one cache-file ``stat`` in steady state, and a JSON parse only
    when the cache file actually changed.  The full route catalog is never
    parsed on this hot path.
    """
    try:
        if health is None:
            health = _load_health_config_readonly()
        if not _health_checks_enabled(health):
            return False
        if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get(_HEALTH_TEST_ENV):
            return False
        path = health.resolved_cache_path()
        with _health_state_lock:
            _read_health_cache_memoized_locked(path)
            return bool(_unhealthy_memo.get("value"))
    except Exception as exc:
        logger.debug(
            "model_routes: unhealthy-verdict scan failed (%s)",
            type(exc).__name__,
        )
        return False


# =============================================================================
# Membership / schema exposure
# =============================================================================


def runtime_satisfies_route(
    runtime: Dict[str, Any],
    route_name: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    catalog: Optional[RouteCatalog] = None,
) -> bool:
    """True when the live runtime is already a member of the route.

    Membership semantics mirror the skill-gate plugin's
    ``runtime_matches_spec`` (runtime_catalog.py:145-147):

    - ``accepted`` entries are model-only by design — provider/base_url/
      reasoning_effort are delivery details there and never change tier
      membership.
    - Legacy membership (no ``accepted``): the PRIMARY and each FALLBACK are
      full specs. A spec that declares ``reasoning_effort`` only matches a
      runtime whose ``reasoning_effort`` equals it — a runtime with a
      missing or different effort does NOT satisfy that spec (a dev route
      pinned to xhigh is not satisfied by the same model thinking at low).
    """
    if not isinstance(runtime, dict):
        return False
    catalog = catalog or load_routes(cfg)
    spec = _lookup_route(catalog, route_name)
    if spec is None:
        return False
    current_model = runtime.get("model")
    if spec.accepted:
        return any(_model_matches(current_model, candidate) for candidate in spec.accepted)
    current_effort = _norm(runtime.get("reasoning_effort"))
    member_specs = [(spec.model, spec.reasoning_effort)]
    member_specs.extend((fb.model, fb.reasoning_effort) for fb in spec.fallbacks)
    for model, effort in member_specs:
        if not _model_matches(current_model, model):
            continue
        target_effort = _norm(effort)
        if target_effort and current_effort != target_effort:
            continue
        return True
    return False


def route_catalog_for_schema(
    cfg: Optional[Dict[str, Any]] = None,
    *,
    catalog: Optional[RouteCatalog] = None,
) -> List[Tuple[str, str]]:
    """(name, description) pairs for valid routes, in declaration order."""
    catalog = catalog or load_routes(cfg)
    return [(spec.name, spec.description) for spec in catalog.routes.values()]
