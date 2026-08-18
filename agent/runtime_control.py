"""Agent model inspection and switching helpers.

This module is intentionally core-owned (not a plugin) because model/runtime
inspection/switching needs live ``AIAgent`` state and must never reach into
GatewayRunner/CLI private fields from a plugin.  Public tool handlers in
``tools.runtime_control_tool`` provide schemas only; execution is intercepted
by the agent loop and routed here.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from hermes_constants import parse_reasoning_effort

logger = logging.getLogger(__name__)

_ALLOWED_SCOPES = {"turn", "session"}


def _safe_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _sanitize_base_url(raw_url: Any) -> str:
    """Return a display-safe endpoint URL with credentials/query stripped."""
    raw = _safe_str(raw_url).strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return ""
    if not parsed.scheme or not parsed.hostname:
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _reasoning_state(agent: Any) -> Dict[str, Any]:
    cfg = getattr(agent, "reasoning_config", None)
    source = getattr(agent, "_runtime_reasoning_source", None) or "agent"
    if isinstance(cfg, dict):
        if cfg.get("enabled") is False:
            return {"enabled": False, "effort": "none", "source": source}
        effort = str(cfg.get("effort") or "").strip().lower() or None
        return {"enabled": True, "effort": effort, "source": source}
    return {"enabled": None, "effort": None, "source": "default"}


def get_runtime_state(agent: Any) -> Dict[str, Any]:
    """Return a secret-free snapshot of the agent's current effective runtime."""
    return {
        "model": _safe_str(getattr(agent, "model", "")),
        "provider": _safe_str(getattr(agent, "provider", "")),
        "base_url": _sanitize_base_url(getattr(agent, "base_url", "")),
        "api_mode": _safe_str(getattr(agent, "api_mode", "")),
        "session_id": _safe_str(getattr(agent, "session_id", "")),
        "platform": _safe_str(getattr(agent, "platform", "")),
        "has_gateway_session": bool(_safe_str(getattr(agent, "_gateway_session_key", ""))),
        "reasoning": _reasoning_state(agent),
        "turn_override_active": hasattr(agent, "_runtime_turn_restore_snapshot"),
        "model_source": getattr(agent, "_runtime_model_source", None) or "agent",
    }


def model_status(agent: Any) -> str:
    """Tool-facing JSON wrapper for :func:`get_runtime_state`.

    When the config declares model_routes (ADR-003 Phase 3b), a ``routes``
    block is appended: which declared route (if any) the current runtime
    satisfies, plus the available route catalog. With no declared routes —
    or on builds without the model_routes subsystem — the output is
    byte-identical to the historical shape.
    """
    state = get_runtime_state(agent)
    state["success"] = True
    routes = _route_status_info(agent)
    if routes is not None:
        state["routes"] = routes
    _emit_runtime_state_event(agent, event="status", state=state)
    return json.dumps(state, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Model-route selection (ADR-003 Phase 3b)
# ---------------------------------------------------------------------------
#
# ``hermes_cli.model_routes`` is OPTIONAL at runtime: the runtime-control
# patch must run unchanged on builds that do not carry the model_routes
# subsystem.  Every reference below is therefore a deferred import wrapped so
# that when the module is absent (or the catalog is empty/dormant) the tool
# degrades exactly to the historical free-form model/provider behavior.


def _route_catalog_pairs() -> list[tuple[str, str]]:
    """(name, description) pairs for declared model_routes; [] when absent/dormant."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.model_routes import route_catalog_for_schema

        return list(route_catalog_for_schema(load_config()))
    except Exception:
        logger.debug("runtime_control: route catalog unavailable", exc_info=True)
        return []


def _agent_route_runtime(agent: Any) -> Dict[str, Any]:
    """Non-secret runtime snapshot in ``runtime_satisfies_route``'s shape.

    Mirrors the gateway router's snapshot semantics
    (``GatewayRunner._model_router_runtime_snapshot``): disabled reasoning
    maps to effort ``"none"``; an unset effort stays absent.
    """
    snapshot: Dict[str, Any] = {}
    model = _safe_str(getattr(agent, "model", "")).strip()
    provider = _safe_str(getattr(agent, "provider", "")).strip()
    if model:
        snapshot["model"] = model
    if provider:
        snapshot["provider"] = provider
    reasoning = _reasoning_state(agent)
    if reasoning.get("enabled") is False:
        snapshot["reasoning_effort"] = "none"
    elif reasoning.get("effort"):
        snapshot["reasoning_effort"] = reasoning["effort"]
    return snapshot


def _route_status_info(agent: Any) -> Optional[Dict[str, Any]]:
    """Route block for :func:`model_status`; None when routes are absent/dormant."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.model_routes import (
            load_routes,
            route_catalog_for_schema,
            runtime_satisfies_route,
        )
    except Exception:
        return None
    try:
        cfg = load_config()
        catalog = load_routes(cfg)
        pairs = route_catalog_for_schema(cfg, catalog=catalog)
        if not pairs:
            return None
        runtime = _agent_route_runtime(agent)
        current = next(
            (
                name
                for name, _ in pairs
                if runtime_satisfies_route(runtime, name, cfg, catalog=catalog)
            ),
            None,
        )
        return {
            "current": current,
            "available": [
                {"name": name, "description": description}
                for name, description in pairs
            ],
        }
    except Exception:
        logger.debug("runtime_control: route status unavailable", exc_info=True)
        return None


def _resolve_route_switch_target(agent: Any, route_name: str) -> tuple[str, Dict[str, Any]]:
    """Resolve a ``model_switch`` route request against the declared catalog.

    Returns ``(status, payload)``:

    - ``("error", <tool error payload>)`` — model_routes unavailable, unknown
      route, or the whole fallback chain unhealthy.  The payload names the
      declared routes so the model can retry with a valid one.
    - ``("noop", {"route": <name>})`` — the current runtime already satisfies
      the route (mirrors the gateway router's ``noop_satisfied`` outcome).
    - ``("resolved", <resolve_route dict>)`` — first healthy
      provider/model/reasoning_effort along the route's chain.
    """
    name = str(route_name or "").strip()
    try:
        from hermes_cli.config import load_config
        from hermes_cli.model_routes import (
            load_routes,
            resolve_route,
            runtime_satisfies_route,
        )
    except Exception:
        # Branch-alone build: the schema never offers `route`, so this is a
        # defensive, model-visible degradation to free-form behavior.
        return "error", {
            "success": False,
            "error": (
                f"Route-based switching is unavailable on this build; unknown parameter "
                f"route={name!r}. Request an explicit model/provider instead."
            ),
        }

    cfg = load_config()
    catalog = load_routes(cfg)
    declared = [spec.name for spec in catalog.routes.values()]
    declared_text = ", ".join(declared) or "(none declared)"
    matched = next((n for n in declared if n.strip().lower() == name.lower()), None)
    if matched is None:
        return "error", {
            "success": False,
            "error": f"Unknown route '{name}'. Declared routes: {declared_text}.",
            "declared_routes": declared,
        }
    if runtime_satisfies_route(_agent_route_runtime(agent), matched, cfg, catalog=catalog):
        return "noop", {"route": matched}
    resolved = resolve_route(matched, cfg, catalog=catalog)
    if resolved is None:
        return "error", {
            "success": False,
            "error": (
                f"Route '{matched}' has no healthy runtime (its default and every "
                f"fallback failed the provider health check). Declared routes: "
                f"{declared_text}. Retry with a different route."
            ),
            "declared_routes": declared,
        }
    return "resolved", resolved


def _emit_runtime_state_event(agent: Any, *, event: str, state: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort plugin notification for effective runtime state.

    Runtime-control tools are intercepted by the agent loop and do not always
    travel through the normal registry/post_tool_call path.  A dedicated hook
    lets guardrail plugins inspect the current model/provider/reasoning without
    depending on generic tool-history observability.
    """
    try:
        from hermes_cli.plugins import invoke_hook

        runtime_state = state if isinstance(state, dict) else get_runtime_state(agent)
        invoke_hook(
            "runtime_state",
            event=event,
            state=runtime_state,
            session_id=_safe_str(getattr(agent, "session_id", "")),
            platform=_safe_str(getattr(agent, "platform", "")),
        )
    except Exception as exc:  # pragma: no cover - defensive plugin seam
        logger.debug("runtime_state hook failed: %s", exc)


def snapshot_runtime(agent: Any) -> Dict[str, Any]:
    """Capture enough live state to restore a turn-scoped runtime switch."""
    return {
        "model": getattr(agent, "model", ""),
        "provider": getattr(agent, "provider", ""),
        "api_key": getattr(agent, "api_key", ""),
        "base_url": getattr(agent, "base_url", ""),
        "api_mode": getattr(agent, "api_mode", ""),
        "reasoning_config": copy.deepcopy(getattr(agent, "reasoning_config", None)),
        "runtime_model_source": getattr(agent, "_runtime_model_source", None),
        "runtime_reasoning_source": getattr(agent, "_runtime_reasoning_source", None),
        "fallback_chain": copy.deepcopy(getattr(agent, "_fallback_chain", None)),
        "fallback_model": copy.deepcopy(getattr(agent, "_fallback_model", None)),
        "fallback_index": getattr(agent, "_fallback_index", None),
        "fallback_activated": getattr(agent, "_fallback_activated", None),
    }


def restore_runtime(agent: Any, snapshot: Dict[str, Any]) -> None:
    """Restore a runtime snapshot captured by :func:`snapshot_runtime`."""
    if not isinstance(snapshot, dict):
        return

    old_model = snapshot.get("model", "")
    old_provider = snapshot.get("provider", "")
    old_api_key = snapshot.get("api_key", "")
    old_base_url = snapshot.get("base_url", "")
    old_api_mode = snapshot.get("api_mode", "")

    model_changed = (
        getattr(agent, "model", "") != old_model
        or getattr(agent, "provider", "") != old_provider
        or getattr(agent, "base_url", "") != old_base_url
        or getattr(agent, "api_mode", "") != old_api_mode
    )
    if model_changed and hasattr(agent, "switch_model"):
        agent.switch_model(
            new_model=old_model,
            new_provider=old_provider,
            api_key=old_api_key,
            base_url=old_base_url,
            api_mode=old_api_mode,
        )
    else:
        if old_model is not None:
            agent.model = old_model
        if old_provider is not None:
            agent.provider = old_provider
        if old_base_url is not None:
            agent.base_url = old_base_url
        if old_api_mode is not None:
            agent.api_mode = old_api_mode
        if old_api_key is not None:
            agent.api_key = old_api_key

    agent.reasoning_config = copy.deepcopy(snapshot.get("reasoning_config"))

    for attr, key in (
        ("_fallback_chain", "fallback_chain"),
        ("_fallback_model", "fallback_model"),
        ("_fallback_index", "fallback_index"),
        ("_fallback_activated", "fallback_activated"),
    ):
        if key in snapshot:
            setattr(agent, attr, copy.deepcopy(snapshot.get(key)))

    if snapshot.get("runtime_model_source") is None:
        if hasattr(agent, "_runtime_model_source"):
            delattr(agent, "_runtime_model_source")
    else:
        agent._runtime_model_source = snapshot.get("runtime_model_source")

    if snapshot.get("runtime_reasoning_source") is None:
        if hasattr(agent, "_runtime_reasoning_source"):
            delattr(agent, "_runtime_reasoning_source")
    else:
        agent._runtime_reasoning_source = snapshot.get("runtime_reasoning_source")


def restore_pending_turn_runtime(agent: Any) -> bool:
    """Restore and clear a pending turn-scoped runtime override, if any."""
    snapshot = getattr(agent, "_runtime_turn_restore_snapshot", None)
    if not snapshot:
        return False
    try:
        restore_runtime(agent, snapshot)
        return True
    finally:
        try:
            delattr(agent, "_runtime_turn_restore_snapshot")
        except AttributeError:
            pass


def _configured_model_names(entry: Any) -> set[str]:
    """Extract declared model IDs from a provider/custom-provider config entry."""
    models: set[str] = set()
    if not isinstance(entry, dict):
        return models

    for key in ("default_model", "model"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            models.add(value.strip())

    raw_models = entry.get("models")
    if isinstance(raw_models, dict):
        models.update(str(model).strip() for model in raw_models if str(model).strip())
    elif isinstance(raw_models, list):
        for item in raw_models:
            if isinstance(item, str) and item.strip():
                models.add(item.strip())
            elif isinstance(item, dict):
                for key in ("id", "name", "model"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        models.add(value.strip())
                        break
    return models


def _configured_model_targets(cfg: Any) -> Dict[str, set[str]]:
    """Return provider -> configured model IDs using config.yaml as the SoT."""
    targets: Dict[str, set[str]] = {}
    if not isinstance(cfg, dict):
        return targets

    providers = cfg.get("providers") or {}
    if isinstance(providers, dict):
        for provider, entry in providers.items():
            if not isinstance(provider, str) or not provider.strip():
                continue
            models = _configured_model_names(entry)
            if models:
                targets[provider.strip()] = models

    custom_providers = cfg.get("custom_providers") or []
    if isinstance(custom_providers, list):
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            slug = "custom:" + name.strip().lower().replace(" ", "-")
            models = _configured_model_names(entry)
            if models:
                targets[slug] = models

    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, dict):
        provider = model_cfg.get("provider")
        model = model_cfg.get("default") or model_cfg.get("model")
        if isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip():
            targets.setdefault(provider.strip(), set()).add(model.strip())

    return targets


def _match_configured_value(value: str, candidates: set[str] | Dict[str, set[str]]) -> str | None:
    if value in candidates:
        return value
    lowered = value.lower()
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.lower() == lowered:
            return candidate
    return None


def _configured_default_model(cfg: Any, provider: str) -> str | None:
    if not isinstance(cfg, dict) or not provider:
        return None
    providers = cfg.get("providers") or {}
    if isinstance(providers, dict):
        entry = providers.get(provider)
        if isinstance(entry, dict):
            value = entry.get("default_model") or entry.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    custom_providers = cfg.get("custom_providers") or []
    if isinstance(custom_providers, list) and provider.startswith("custom:"):
        for entry in custom_providers:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            slug = "custom:" + name.strip().lower().replace(" ", "-")
            if slug == provider:
                value = entry.get("default_model") or entry.get("model")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, dict) and model_cfg.get("provider") == provider:
        value = model_cfg.get("default") or model_cfg.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _format_configured_targets(targets: Dict[str, set[str]], *, limit: int = 8) -> Dict[str, list[str]]:
    formatted: Dict[str, list[str]] = {}
    for provider in sorted(targets)[:limit]:
        formatted[provider] = sorted(targets[provider])[:limit]
    return formatted


def resolve_agent_model_target_strict(
    *,
    requested_provider: str,
    requested_model: str,
    current_provider: str,
    current_model: str,
) -> tuple[bool, str, str, Dict[str, Any] | None]:
    """Resolve agent-callable model switches only through configured targets.

    Human ``/model`` commands intentionally support fuzzy catalog discovery.  The
    agent-facing tool must not invent provider/model names, so this function uses
    config.yaml provider declarations as the source of truth before the shared
    resolver is allowed to run.
    """
    from hermes_cli.config import load_config

    cfg = load_config()
    targets = _configured_model_targets(cfg)
    requested_provider = requested_provider.strip()
    requested_model = requested_model.strip()
    current_provider = current_provider.strip()
    current_model = current_model.strip()

    def error(message: str, **extra: Any) -> tuple[bool, str, str, Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "success": False,
            "error": message,
            "configured_targets": _format_configured_targets(targets),
        }
        payload.update(extra)
        return False, "", "", payload

    if not targets:
        return error(
            "No configured provider/model targets found in config.yaml. Agent model_switch may only select models declared in config.yaml.",
        )

    provider = ""
    if requested_provider:
        provider_match = _match_configured_value(requested_provider, targets)
        if provider_match is None:
            return error(
                "Provider is not configured. Agent model_switch may only select providers declared in config.yaml.",
                requested={"provider": requested_provider, "model": requested_model},
            )
        provider = provider_match

    if provider and not requested_model:
        default_model = _configured_default_model(cfg, provider)
        model_match = _match_configured_value(default_model or "", targets[provider]) if default_model else None
        if model_match is None:
            model_match = _match_configured_value(current_model, targets[provider]) if current_provider == provider else None
        if model_match is None:
            model_match = sorted(targets[provider])[0]
        return True, provider, model_match, None

    if provider and requested_model:
        model_match = _match_configured_value(requested_model, targets[provider])
        if model_match is None:
            return error(
                "Model is not configured for provider. Agent model_switch may only select models declared in config.yaml.",
                requested={"provider": provider, "model": requested_model},
            )
        return True, provider, model_match, None

    if requested_model:
        current_provider_match = _match_configured_value(current_provider, targets)
        if current_provider_match is not None:
            model_match = _match_configured_value(requested_model, targets[current_provider_match])
            if model_match is not None:
                return True, current_provider_match, model_match, None

        matches: list[tuple[str, str]] = []
        for candidate_provider, models in targets.items():
            model_match = _match_configured_value(requested_model, models)
            if model_match is not None:
                matches.append((candidate_provider, model_match))
        if not matches:
            return error(
                "Model is not configured. Agent model_switch may only select models declared in config.yaml.",
                requested={"provider": requested_provider, "model": requested_model},
            )
        providers = sorted({match_provider for match_provider, _ in matches})
        if len(providers) > 1:
            return error(
                "Ambiguous configured model; specify provider.",
                requested={"provider": requested_provider, "model": requested_model},
                matching_providers=providers,
            )
        return True, matches[0][0], matches[0][1], None

    return error("No model/provider target requested.")


def resolve_model_switch(
    *,
    raw_input: str,
    current_provider: str,
    current_model: str,
    current_base_url: str,
    current_api_key: str,
    explicit_provider: str,
):
    """Resolve a requested model/provider switch via Hermes' shared resolver."""
    from hermes_cli.config import get_compatible_custom_providers, load_config
    from hermes_cli.model_switch import switch_model as _switch_model

    cfg = load_config()
    user_providers = cfg.get("providers") or {} if isinstance(cfg, dict) else {}
    custom_providers = get_compatible_custom_providers(cfg) if isinstance(cfg, dict) else None
    return _switch_model(
        raw_input=raw_input,
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
        current_api_key=current_api_key,
        is_global=False,
        explicit_provider=explicit_provider,
        user_providers=user_providers,
        custom_providers=custom_providers,
    )


def _notify_runtime_update(
    agent: Any,
    *,
    scope: str,
    model_override: Optional[Dict[str, Any]],
    reasoning_config: Optional[Dict[str, Any]],
) -> Optional[str]:
    callback = getattr(agent, "runtime_update_callback", None)
    if not callable(callback):
        return None
    try:
        callback(
            scope=scope,
            model_override=model_override,
            reasoning_config=copy.deepcopy(reasoning_config),
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive surface callback guard
        logger.warning("runtime_update_callback failed: %s", exc)
        return str(exc) or exc.__class__.__name__


def model_switch(
    agent: Any,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    route: Optional[str] = None,
    scope: str = "turn",
    reason: Optional[str] = None,
) -> str:
    """Switch the live agent model/reasoning for this turn or session.

    ``global`` scope is deliberately unsupported.  Persisting to config.yaml is
    user-command territory; this tool can only affect the current turn or
    current session.

    ``route`` (ADR-003 Phase 3b) selects a config-declared model_routes route
    by purpose; its health-checked resolution feeds the same apply path as a
    free-form model/provider switch.  When both ``route`` and an explicit
    ``reasoning_effort`` are given, the explicit effort wins over the route's
    default effort.
    """
    # Always force session scope — turn scope was removed (May 2026) because
    # LLMs omit the parameter ~29% of the time, and the default "turn" caused
    # silent model reversion at turn end.  The schema no longer exposes scope.
    scope = "session"

    requested_model = str(model or "").strip()
    requested_provider = str(provider or "").strip()
    requested_reasoning = str(reasoning_effort or "").strip().lower()
    requested_route = str(route or "").strip()
    if (
        not requested_model
        and not requested_provider
        and not requested_reasoning
        and not requested_route
    ):
        return json.dumps(
            {"success": False, "error": "No runtime change requested."},
            ensure_ascii=False,
        )
    if requested_route and (requested_model or requested_provider):
        return json.dumps(
            {
                "success": False,
                "error": "Pass either 'route' or explicit 'model'/'provider', not both.",
            },
            ensure_ascii=False,
        )

    parsed_reasoning = None
    if requested_reasoning:
        parsed_reasoning = parse_reasoning_effort(requested_reasoning)
        if parsed_reasoning is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "Invalid reasoning_effort. Use none, minimal, low, medium, high, or xhigh.",
                },
                ensure_ascii=False,
            )

    model_override = None
    changed = []
    persistence_error = None
    route_info: Optional[Dict[str, Any]] = None

    # Resolve the switch target: a declared route (health-checked walk) or the
    # strict config-declared free-form model/provider.  Both feed the SAME
    # apply path below (shared resolver -> agent.switch_model -> callback).
    target_provider = ""
    target_model = ""

    if requested_route:
        route_status, route_payload = _resolve_route_switch_target(agent, requested_route)
        if route_status == "error":
            return json.dumps(route_payload, ensure_ascii=False)
        if route_status == "noop":
            matched_route = route_payload["route"]
            if parsed_reasoning is None:
                # Pure no-op: the current runtime is already a member of the
                # route — do not re-apply or fire the gateway callback.
                state = get_runtime_state(agent)
                return json.dumps(
                    {
                        "success": True,
                        "scope": scope,
                        "changed": [],
                        "noop": True,
                        "route": {"name": matched_route, "already_satisfied": True},
                        "message": f"Already on route '{matched_route}'; no switch applied.",
                        "reason": str(reason or ""),
                        "runtime": state,
                    },
                    ensure_ascii=False,
                )
            # Explicit reasoning_effort alongside an already-satisfied route:
            # skip the model re-apply and fall through to the effort-only path.
            route_info = {"name": matched_route, "already_satisfied": True}
        else:
            target_provider = str(route_payload.get("provider") or "")
            target_model = str(route_payload.get("model") or "")
            route_info = {"name": str(route_payload.get("route") or requested_route)}
            if str(route_payload.get("source") or "default") != "default":
                route_info["source"] = str(route_payload.get("source") or "")
            failover_reason = str(route_payload.get("reason") or "")
            if failover_reason:
                route_info["failover"] = failover_reason
            # Route default effort applies only when no explicit effort was
            # requested (explicit reasoning_effort wins).
            route_effort = str(route_payload.get("reasoning_effort") or "").strip().lower()
            if parsed_reasoning is None and route_effort:
                parsed_reasoning = parse_reasoning_effort(route_effort)
    elif requested_model or requested_provider:
        ok, target_provider, target_model, strict_error = resolve_agent_model_target_strict(
            requested_provider=requested_provider,
            requested_model=requested_model,
            current_provider=str(getattr(agent, "provider", "") or ""),
            current_model=str(getattr(agent, "model", "") or ""),
        )
        if not ok:
            return json.dumps(strict_error, ensure_ascii=False)

    if target_model or target_provider:
        current_provider = str(getattr(agent, "provider", "") or "")
        current_model = str(getattr(agent, "model", "") or "")
        current_api_key = getattr(agent, "api_key", "")
        if not isinstance(current_api_key, str):
            current_api_key = ""
        result = resolve_model_switch(
            raw_input=target_model,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=str(getattr(agent, "base_url", "") or ""),
            current_api_key=current_api_key,
            explicit_provider=target_provider,
        )
        if not getattr(result, "success", False):
            return json.dumps(
                {
                    "success": False,
                    "error": getattr(result, "error_message", "Model switch failed") or "Model switch failed",
                },
                ensure_ascii=False,
            )
        if getattr(result, "target_provider", "") != target_provider or getattr(result, "new_model", "") != target_model:
            return json.dumps(
                {
                    "success": False,
                    "error": "Shared model resolver resolved outside configured agent target; refusing to apply free-form/fuzzy model switch.",
                    "requested": {
                        "provider": requested_provider,
                        "model": requested_model,
                        **({"route": requested_route} if requested_route else {}),
                    },
                    "expected": {"provider": target_provider, "model": target_model},
                    "resolved": {
                        "provider": getattr(result, "target_provider", ""),
                        "model": getattr(result, "new_model", ""),
                    },
                },
                ensure_ascii=False,
            )

        agent.switch_model(
            new_model=result.new_model,
            new_provider=result.target_provider,
            api_key=getattr(result, "api_key", "") or "",
            base_url=getattr(result, "base_url", "") or "",
            api_mode=getattr(result, "api_mode", "") or "",
        )
        agent._runtime_model_source = f"model_switch:{scope}"
        model_override = {
            "model": result.new_model,
            "provider": result.target_provider,
            "api_key": getattr(result, "api_key", "") or "",
            "base_url": getattr(result, "base_url", "") or "",
            "api_mode": getattr(result, "api_mode", "") or "",
        }
        changed.append("model")

    if parsed_reasoning is not None:
        agent.reasoning_config = copy.deepcopy(parsed_reasoning)
        agent._runtime_reasoning_source = f"model_switch:{scope}"
        changed.append("reasoning")

    # Note: turn-scope snapshot/restore was removed (scope is always session).
    # If a legacy snapshot exists from a prior code path, update it defensively.
    if hasattr(agent, "_runtime_turn_restore_snapshot"):
        snapshot = agent._runtime_turn_restore_snapshot
        if model_override:
            snapshot.update(
                {
                    "model": model_override.get("model", ""),
                    "provider": model_override.get("provider", ""),
                    "api_key": model_override.get("api_key", ""),
                    "base_url": model_override.get("base_url", ""),
                    "api_mode": model_override.get("api_mode", ""),
                    "runtime_model_source": getattr(agent, "_runtime_model_source", None),
                    "fallback_chain": copy.deepcopy(getattr(agent, "_fallback_chain", None)),
                    "fallback_model": copy.deepcopy(getattr(agent, "_fallback_model", None)),
                    "fallback_index": getattr(agent, "_fallback_index", None),
                    "fallback_activated": getattr(agent, "_fallback_activated", None),
                }
            )
        if parsed_reasoning is not None:
            snapshot["reasoning_config"] = copy.deepcopy(parsed_reasoning)
            snapshot["runtime_reasoning_source"] = getattr(
                agent, "_runtime_reasoning_source", None
            )

    persistence_error = _notify_runtime_update(
        agent,
        scope=scope,
        model_override=model_override,
        reasoning_config=parsed_reasoning,
    )

    state = get_runtime_state(agent)
    _emit_runtime_state_event(agent, event="switch", state=state)
    response = {
        "success": True,
        "scope": scope,
        "changed": changed,
        "reason": str(reason or ""),
        "runtime": state,
    }
    if route_info is not None:
        response["route"] = route_info
    warning = getattr(locals().get("result", None), "warning_message", "") if "result" in locals() else ""
    if warning:
        response["warning"] = warning
    if persistence_error:
        response["persistence_warning"] = (
            "Runtime changed on the live agent, but the session persistence callback failed: "
            f"{persistence_error}"
        )
    return json.dumps(response, ensure_ascii=False)


__all__ = [
    "get_runtime_state",
    "model_status",
    "model_switch",
    "snapshot_runtime",
    "restore_runtime",
    "restore_pending_turn_runtime",
    "resolve_model_switch",
]
