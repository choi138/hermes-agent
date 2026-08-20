"""First-party Hermes observability integrations."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def observe_lifecycle(hook_name: str, **kwargs: Any) -> None:
    """Dispatch a Hermes lifecycle event to built-in observability features."""
    from . import local_observations, relay_shared_metrics

    _safe_observe(relay_shared_metrics.observe_lifecycle, hook_name, kwargs)
    _safe_observe(local_observations.observe_lifecycle, hook_name, kwargs)


def handles_hook(hook_name: str) -> bool:
    """Return whether any built-in observability feature handles a hook."""
    from . import relay_shared_metrics

    # Deliberately NOT OR-ed with the local recorder's own predicate: every hook
    # it needs is already in relay_shared_metrics.HANDLED_HOOKS behind the same
    # config gate, so the OR would return an identical value while doubling a
    # lock-serialised config stat that runs a few times per agent turn.
    return relay_shared_metrics.handles_hook(hook_name)


def _safe_observe(callback: Any, hook_name: str, kwargs: dict[str, Any]) -> None:
    try:
        callback(hook_name, **kwargs)
    except Exception:
        logger.warning(
            "Built-in observability hook failed: %s", hook_name, exc_info=True
        )
