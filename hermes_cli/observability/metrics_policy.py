"""Side-effect-free config gates for Hermes local metrics.

``relay_shared_metrics.enabled()`` is NOT a pure predicate: when it returns
False it pops and deactivates the profile's Relay runtime. The direct
(Relay-independent) recorder needs the same config read WITHOUT that side
effect, so the pure read lives here and ``enabled()`` delegates to it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _shared_metrics_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import read_raw_config_readonly

        # Read-only fast path: this gate runs a few times per agent turn and the
        # mutable read_raw_config() paid a full config deepcopy on every call.
        config = read_raw_config_readonly() or {}
    except Exception:
        logger.debug("Unable to read Hermes shared-metrics policy", exc_info=True)
        return {}
    telemetry = config.get("telemetry") if isinstance(config, dict) else None
    shared_metrics = (
        telemetry.get("shared_metrics") if isinstance(telemetry, dict) else None
    )
    return shared_metrics if isinstance(shared_metrics, dict) else {}


def shared_metrics_consent() -> bool:
    """Return the profile-owned shared-metrics collection consent.

    Pure: reads config and returns. Collection consent is profile-owned —
    managed config overlays may control runtime policy but cannot opt a profile
    into or out of shared metrics.
    """
    return _shared_metrics_config().get("enabled") is True


def local_observations_enabled() -> bool:
    """Return whether raw local observation samples may be recorded.

    Gated by BOTH the profile-owned consent above and the additive
    ``telemetry.shared_metrics.local_observations`` opt-out (default True), so
    raw-sample retention can be turned off without turning counters off.
    """
    shared_metrics = _shared_metrics_config()
    if shared_metrics.get("enabled") is not True:
        return False
    return shared_metrics.get("local_observations", True) is not False
