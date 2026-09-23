"""Task lifecycle primitives shared by every milestone module."""

from agent.task_lifecycle.types import (
    DELIVERY_PHASES,
    EXECUTION_PHASES,
    TERMINAL_PHASES,
    UNRESOLVED_PHASES,
    VERIFICATION_PHASES,
    LifecycleError,
    Phase,
    PhaseTransitionError,
    is_complete,
    phase_rank,
    require_phase,
)

__all__ = [
    "DELIVERY_PHASES",
    "EXECUTION_PHASES",
    "TERMINAL_PHASES",
    "UNRESOLVED_PHASES",
    "VERIFICATION_PHASES",
    "LifecycleError",
    "Phase",
    "PhaseTransitionError",
    "is_complete",
    "phase_rank",
    "require_phase",
]
