"""Route-prefixed outage fallback; optional and additive to global fallbacks."""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def record_route_provider_outcome(agent: Any, healthy: bool, reason: str) -> None:
    """Best-effort passive health from real primary completion traffic."""
    try:
        from hermes_cli.model_routes import (
            has_unhealthy_verdicts, provider_key_for_runtime, record_provider_outcome,
        )
        if healthy and not has_unhealthy_verdicts():
            return
        key = provider_key_for_runtime(
            provider=getattr(agent, "provider", "") or "",
            base_url=getattr(agent, "base_url", "") or "",
        )
        if key:
            record_provider_outcome(key, healthy, reason)
    except Exception:
        logger.debug("Passive model-route health recording failed", exc_info=True)

def _build_outage_route_fallback_chain(agent: Any) -> tuple[str, list[dict]]:
    """Return the recorded route's healthy fallbacks in declaration order.

    Route membership is intentionally not used to discover intent: one model
    can be accepted by several routes.  The gateway records the route it
    actually applied on ``agent._active_route_name``; without that record this
    additive prefix is disabled and the established global chain is left
    untouched.
    """
    active_route_name = str(
        getattr(agent, "_active_route_name", "") or ""
    ).strip()
    if not active_route_name:
        return "", []

    try:
        from agent.backend_identity import BackendIdentity, should_skip_candidate
        from hermes_cli.config import load_config
        from hermes_cli.model_routes import (
            _cfg_runtime_fallback,
            _lookup_route,
            load_routes,
            provider_health,
            resolve_route,
            runtime_satisfies_route,
        )

        cfg = load_config() or {}
        catalog = load_routes(cfg)
        spec = _lookup_route(catalog, active_route_name)
        if spec is None:
            return "", []

        runtime = {
            "provider": str(getattr(agent, "provider", "") or ""),
            "model": str(getattr(agent, "model", "") or ""),
            "base_url": str(getattr(agent, "base_url", "") or ""),
        }
        reasoning = getattr(agent, "reasoning_config", None)
        if isinstance(reasoning, dict):
            if reasoning.get("enabled") is False:
                runtime["reasoning_effort"] = "none"
            elif reasoning.get("effort"):
                runtime["reasoning_effort"] = str(reasoning["effort"])
        if not runtime_satisfies_route(
            runtime, spec.name, cfg, catalog=catalog
        ):
            # The recorded intent is stale (for example, a later manual model
            # switch moved the session out of the route).  Never replace it by
            # a first-match membership scan: membership is ambiguous.
            return "", []

        # resolve_route owns the route-health ordering.  Its source identifies
        # the earliest eligible fallback, so entries already rejected as
        # unhealthy are not retried by the live fallback walk.
        resolved = resolve_route(spec.name, cfg, catalog=catalog)
        if not resolved:
            return spec.name, []
        first_index = 0
        source = str(resolved.get("source") or "")
        if source.startswith("fallback:"):
            try:
                first_index = max(int(source.split(":", 1)[1]) - 1, 0)
            except (TypeError, ValueError):
                first_index = 0

        current_ident = BackendIdentity.build(
            provider=runtime["provider"],
            model=runtime["model"],
            base_url=runtime["base_url"],
        )
        chain: list[dict] = []
        for index, fallback in enumerate(spec.fallbacks):
            if index < first_index:
                continue
            healthy, _ = provider_health(
                fallback.provider,
                fallback.model,
                cfg=cfg,
                health=catalog.health,
            )
            if not healthy:
                continue
            runtime_cfg = _cfg_runtime_fallback(fallback.provider, cfg)
            entry = {
                "provider": fallback.provider,
                "model": fallback.model,
                "reasoning_effort": fallback.reasoning_effort or "",
                "base_url": str(runtime_cfg.get("base_url") or ""),
                "api_mode": str(runtime_cfg.get("api_mode") or ""),
            }
            candidate_ident = BackendIdentity.build(
                provider=entry["provider"],
                model=entry["model"],
                base_url=entry["base_url"],
            )
            if should_skip_candidate(candidate_ident, current_ident):
                logger.warning(
                    "Route fallback skip: route %s entry %s/%s resolves to "
                    "the current backend",
                    spec.name,
                    entry["provider"],
                    entry["model"],
                )
                continue
            chain.append(entry)
        return spec.name, chain
    except Exception:
        # Route-aware fallback is additive. Any config/import/resolution
        # failure leaves the established global fallback chain untouched.
        logger.debug("Route fallback unavailable; using global chain", exc_info=True)
        return "", []


def _reset_outage_route_fallback_walk(agent: Any) -> None:
    """Forget the transient route prefix without touching the global cursor."""
    agent._outage_route_fallback_walk_active = False
    agent._outage_route_fallback_name = ""
    agent._outage_route_fallback_chain = []
    agent._outage_route_fallback_index = 0


def _next_outage_route_fallback(agent: Any) -> Optional[dict]:
    """Advance the route-prefixed cursor for one outage fallback walk."""
    if getattr(agent, "_outage_route_fallback_walk_active", False):
        chain = getattr(agent, "_outage_route_fallback_chain", [])
        index = int(getattr(agent, "_outage_route_fallback_index", 0) or 0)
    else:
        active_route_name = str(
            getattr(agent, "_active_route_name", "") or ""
        ).strip()
        continuing = (
            bool(getattr(agent, "_fallback_activated", False))
            and bool(active_route_name)
            and active_route_name.lower()
            == str(
                getattr(agent, "_outage_route_fallback_name", "") or ""
            ).lower()
        )
        if continuing:
            chain = getattr(agent, "_outage_route_fallback_chain", [])
            index = int(getattr(agent, "_outage_route_fallback_index", 0) or 0)
        else:
            route_name, chain = _build_outage_route_fallback_chain(agent)
            index = 0
            agent._outage_route_fallback_name = route_name
            agent._outage_route_fallback_chain = chain
            agent._outage_route_fallback_index = 0
        # Keep this true through recursive skip/unresolvable calls so an
        # exhausted route prefix falls into the global chain exactly once.
        agent._outage_route_fallback_walk_active = True

    if index >= len(chain):
        return None
    agent._outage_route_fallback_index = index + 1
    return chain[index]
