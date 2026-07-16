"""Stable gateway tool policies separated from Hermes identity profiles.

An identity profile chooses credentials, memory, sessions, and bot identity.
This module only narrows the immutable tool schema assembled for a session.
Keeping those concepts separate avoids creating synthetic Hermes profiles just
to obtain a smaller prompt and preserves per-conversation prompt caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from agent.request_footprint import (
    ToolSchemaMetrics,
    canonical_tool_schema_metrics,
)


DISCORD_CORE_SCHEMA_BUDGET_BYTES = 50_000


@dataclass(frozen=True)
class GatewayToolPolicy:
    """Resolved, cache-stable policy for one gateway session."""

    name: str
    identity_profile: str
    enabled_toolsets: tuple[str, ...]


def schema_budget_bytes(policy_name: str) -> Optional[int]:
    """Return the measured deployment gate for a policy, if one applies."""

    if str(policy_name) == "discord-core":
        return DISCORD_CORE_SCHEMA_BUDGET_BYTES
    return None


def schema_within_budget(policy_name: str, metrics: ToolSchemaMetrics) -> bool:
    """Return whether a final schema satisfies its policy's byte budget."""

    budget = schema_budget_bytes(policy_name)
    return budget is None or metrics.json_bytes <= budget


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _kanban_is_configured(config: dict[str, Any], enabled: set[str]) -> bool:
    """Match the existing kanban tool's opt-in contract.

    ``tools.kanban_tools`` historically exposes lifecycle tools when the
    profile's top-level ``toolsets`` contains ``kanban``.  Also accept an
    explicit per-Discord entry so a platform-scoped configuration remains
    authoritative.  Requiring ``kanban`` in the already-resolved set preserves
    ``agent.disabled_toolsets`` as the final veto.
    """

    if "kanban" not in enabled:
        return False
    if "kanban" in _string_set(config.get("toolsets")):
        return True
    platform_toolsets = config.get("platform_toolsets")
    if isinstance(platform_toolsets, dict):
        return "kanban" in _string_set(platform_toolsets.get("discord"))
    return False


def _discord_ops_allowed(config: dict[str, Any], source: Any) -> bool:
    """Require an exact user *and* channel allowlist match for full Kanban.

    Wildcards are deliberately not supported.  The normal gateway admission
    check still runs first; this is an additional least-privilege gate for the
    large board-routing surface.
    """

    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        return False
    allowed_users = _string_set(kanban_cfg.get("discord_ops_users"))
    allowed_channels = _string_set(kanban_cfg.get("discord_ops_channels"))
    if not allowed_users or not allowed_channels:
        return False
    if "*" in allowed_users or "*" in allowed_channels:
        return False
    user_id = str(getattr(source, "user_id", "") or "")
    source_channels = {
        str(value)
        for value in (
            getattr(source, "chat_id", None),
            getattr(source, "thread_id", None),
            getattr(source, "parent_chat_id", None),
        )
        if value
    }
    return user_id in allowed_users and bool(source_channels & allowed_channels)


def resolve_gateway_tool_policy(
    config: dict[str, Any],
    *,
    platform: str,
    source: Any,
    identity_profile: str,
    enabled_toolsets: Iterable[str],
    disabled_toolsets: Iterable[str] = (),
) -> GatewayToolPolicy:
    """Return the fixed policy/toolset tuple for a gateway session.

    Discord profiles that opted into Kanban get one asynchronous intake tool.
    Dispatcher workers keep their task-scoped lifecycle policy in
    :mod:`model_tools`; exact user+channel operator allowlists retain the full
    orchestrator surface.  Other platform behavior is unchanged.
    """

    enabled = {str(item) for item in enabled_toolsets if str(item)}
    disabled = {str(item) for item in disabled_toolsets if str(item)}
    profile = str(identity_profile or "default")
    if str(platform) != "discord":
        return GatewayToolPolicy(
            name="platform-default",
            identity_profile=profile,
            enabled_toolsets=tuple(sorted(enabled)),
        )

    configured = "kanban" not in disabled and _kanban_is_configured(config, enabled)
    if configured and _discord_ops_allowed(config, source):
        return GatewayToolPolicy(
            name="discord-ops",
            identity_profile=profile,
            enabled_toolsets=tuple(sorted(enabled)),
        )

    # A normal Discord model must never receive the worker/orchestrator
    # lifecycle surface.  Replace it once, before AIAgent construction, so the
    # schema stays byte-stable for the life of the cached conversation.
    enabled.discard("kanban")
    enabled.discard("kanban_worker")
    enabled.discard("kanban_submit")
    if configured:
        enabled.add("kanban_submit")
    return GatewayToolPolicy(
        name="discord-core",
        identity_profile=profile,
        enabled_toolsets=tuple(sorted(enabled)),
    )
