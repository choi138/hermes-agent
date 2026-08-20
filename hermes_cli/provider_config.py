"""Provider-scoped runtime metadata lookup.

Named custom providers keep two identities at runtime: ``provider`` is the
generic transport/billing class (usually ``"custom"``), while
``requested_provider`` retains the configured ``providers.<id>`` key.  Any
provider-specific behavior must prefer the latter or its config is silently
lost after runtime normalization.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional


def _normalize_provider_id(value: object) -> str:
    provider_id = str(value or "").strip().lower().replace(" ", "-")
    if provider_id.startswith("custom:"):
        provider_id = provider_id.split(":", 1)[1]
    return provider_id


def _provider_id_candidates(
    provider_id: object,
    requested_provider: object = None,
) -> Iterator[str]:
    """Yield config lookup ids in the order that matches the live route.

    A runtime provider normalized to ``custom`` needs its durable requested id
    first.  Once failover installs a concrete provider id, however, that live
    id must win over the primary route's stale ``requested_provider`` value.
    """
    seen: set[str] = set()
    normalized_provider = _normalize_provider_id(provider_id)
    if normalized_provider == "custom":
        ordered = (requested_provider, provider_id)
    else:
        ordered = (provider_id, requested_provider)
    for raw in ordered:
        candidate = _normalize_provider_id(raw)
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate


def get_provider_config_entry(
    provider_id: str,
    *,
    requested_provider: Optional[str] = None,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Return the matching ``providers.<id>`` entry and its durable id.

    ``requested_provider`` wins over the normalized runtime provider.  This is
    what lets a named endpoint such as ``providers.codex-lb`` keep its timeout
    and backend capability after the runtime resolver maps it to ``custom``.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    if not isinstance(providers, dict):
        return None

    candidates = tuple(_provider_id_candidates(provider_id, requested_provider))
    if not candidates:
        return None

    # Fast path for the canonical, lower-case config keys written by Hermes.
    for candidate in candidates:
        entry = providers.get(candidate)
        if isinstance(entry, dict):
            return candidate, entry

    # Hand-written configs may use case or the display name as the requested
    # identity. Match both without mutating the cached config mapping.
    for candidate in candidates:
        for raw_key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            aliases = {
                _normalize_provider_id(raw_key),
                _normalize_provider_id(entry.get("name")),
            }
            if candidate in aliases:
                return _normalize_provider_id(raw_key), entry

    return None


def get_provider_backend_family(
    provider_id: str,
    *,
    requested_provider: Optional[str] = None,
) -> str:
    """Return normalized provider backend capability metadata, if configured."""
    resolved = get_provider_config_entry(
        provider_id,
        requested_provider=requested_provider,
    )
    if resolved is None:
        return ""
    _resolved_id, entry = resolved
    raw_family = entry.get("backend_family")
    if not isinstance(raw_family, str):
        return ""
    return raw_family.strip().lower().replace("_", "-").replace(" ", "-")


__all__ = ["get_provider_backend_family", "get_provider_config_entry"]
