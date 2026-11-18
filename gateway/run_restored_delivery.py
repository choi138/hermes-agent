"""Candidate selection for resumed sessions, including Relay fronted platforms."""
import logging
from typing import Any, Optional

from gateway.config import Platform
from gateway.run_startup_restore import _orphaned_active_turn

logger = logging.getLogger("gateway.run")


def resume_pending_candidates(runner: Any, platform: Optional[Platform] = None) -> Optional[list]:
    """Snapshot pending entries unless enumeration fails or the boot loop breaker trips."""
    relay = runner._primary_adapters().get(Platform.RELAY) if platform == Platform.RELAY else None
    fronts = getattr(relay, "fronts_platform", None)
    boot_id = getattr(runner, "_boot_id", None)
    try:
        with runner.session_store._lock:
            runner.session_store._ensure_loaded_locked()
            candidates = [
                entry for entry in runner.session_store._entries.values()
                if not entry.suspended
                and entry.origin is not None
                and (platform is None or entry.origin.platform == platform
                     or (callable(fronts) and fronts(entry.origin.platform)))
                and (
                    _orphaned_active_turn(entry, boot_id) is not None
                    or (
                        entry.resume_pending
                        and not (entry.active_turn and entry.active_turn.get("status") == "blocked")
                        and entry.resume_reason in runner._AUTO_RESUME_REASONS
                        and not (entry.active_turn and entry.active_turn.get("boot_id") == boot_id)
                    )
                )
            ]
    except Exception as exc:
        logger.warning("Failed to enumerate resume-pending sessions: %s", exc, exc_info=True)
        return None
    # Continuation recovery keeps its own execution budget; only user restarts count toward the breaker.
    from gateway.process_continuations import filter_restart_candidates
    return filter_restart_candidates(runner, candidates)
