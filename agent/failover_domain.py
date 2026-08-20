"""Failure-domain identity for provider failover.

Two fallback-chain entries can carry different provider and model names and
still resolve to the *same* endpoint — e.g. a load-balancer alias sitting in
front of the primary's local shim (``custom/gpt-5.6-sol`` and
``codex-lb/gpt-5.5`` both landing on ``http://127.0.0.1:2455/v1``).  The
config-level provider+model / base_url dedup cannot see that: the fallback
entry carries no explicit ``base_url``, so the real destination is only known
after the router has built the client.

When the failure belongs to the *endpoint* (timeout / 5xx / overloaded),
activating such a candidate re-enters the capacity pool that just died.
Identity here is therefore origin-only — scheme, hostname and effective port
decide which pool a request lands in; path, query, fragment, userinfo and
trailing slashes never do.

Origin equality alone must not decide, though: a public provider hostname
fronts a fleet where each model has its own capacity, so a *different* model
there is a genuine recovery path, while a private or loopback address is one
local process where every alias of it died together.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from agent.error_classifier import FailoverReason

# Failures that belong to the endpoint rather than to the model.  Only these
# justify skipping a same-origin candidate: a model-specific failure
# (model_not_found, content_policy_blocked, …) is genuinely recoverable by a
# DIFFERENT model on the SAME endpoint, so those must stay allowed.
INFRASTRUCTURE_FAILOVER_REASONS = frozenset({
    FailoverReason.timeout,
    FailoverReason.server_error,
    FailoverReason.overloaded,
})

_DEFAULT_PORTS = {"http": 80, "https": 443}

# RFC 6761 reserves these names for the loopback interface; no resolver is
# allowed to point them anywhere else.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


def endpoint_origin(base_url: Any) -> str:
    """Return the canonical ``scheme://host:port`` origin for *base_url*.

    Returns ``""`` when no origin can be determined (empty value, no scheme,
    unparseable port, non-URL object such as a test double) so callers can
    fail open instead of guessing.  The hostname is lowercased and default
    ports are made explicit, so ``https://API.Example.com/v1/`` and
    ``https://api.example.com:443/v1?k=v`` compare equal.
    """
    raw = str(base_url or "").strip()
    if "://" not in raw:
        # A schemeless value cannot identify a failure domain: http and https
        # on one host are distinct pools.
        return ""
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if not scheme or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def is_private_or_loopback_origin(value: Any) -> bool:
    """True when *value* names a private, loopback or link-local address.

    Such an address is one local process or appliance, so every chain entry
    resolving to it is the same shim under another name, whatever model it
    advertises.  A public hostname is the opposite: it fronts a fleet where
    separate models can sit on separate capacity, so one model's timeout
    there says nothing about the next one.

    Resolution is purely lexical and never touches the network — only IP
    literals and the RFC 6761 ``localhost`` names count as local.  A DNS
    lookup on the failover path would add latency to an already-degraded
    request and could answer differently on the next attempt, so any other
    hostname is treated as public.  Accepts a full URL, a canonical origin
    from :func:`endpoint_origin`, or a bare ``host[:port]``.
    """
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        host = urlparse(raw if "://" in raw else f"//{raw}").hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    if not host:
        return False
    if host in _LOOPBACK_HOSTNAMES or host.endswith(".localhost"):
        return True
    try:
        address = ip_address(host)
    except ValueError:
        # A name rather than a literal: not resolvable without DNS, and
        # public hostnames are the common case, so stay eligible.
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        # ``::ffff:127.0.0.1`` reaches the same shim as ``127.0.0.1``.
        address = mapped
    return bool(
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def normalize_model_identity(model: Any) -> str:
    """Return a stable full model identifier for replay comparison.

    Provider-qualified names remain qualified: ``openai/foo`` and
    ``anthropic/foo`` are different routes and must not be collapsed merely
    because their final path segment matches.
    """
    return " ".join(str(model or "").strip().lower().split())


def same_failure_domain(left: Any, right: Any) -> bool:
    """True when both URLs resolve to the same, known endpoint origin."""
    left_origin = endpoint_origin(left)
    if not left_origin:
        return False
    return left_origin == endpoint_origin(right)


__all__ = [
    "INFRASTRUCTURE_FAILOVER_REASONS",
    "endpoint_origin",
    "is_private_or_loopback_origin",
    "normalize_model_identity",
    "same_failure_domain",
]
