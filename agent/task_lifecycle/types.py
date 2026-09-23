"""Shared vocabulary for the task lifecycle.

Design rule: **execution, verification and delivery are separate events.**
A CLI exiting 0 is not a verified result, and a verified result is not a
delivered one. Every milestone module speaks this vocabulary so no layer can
silently promote one phase into another.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class LifecycleError(Exception):
    """Base class for every task-lifecycle rule violation."""


class PhaseTransitionError(LifecycleError):
    """A phase precondition was not satisfied."""


class Phase(str, Enum):
    """Ordered lifecycle phases plus the unordered terminal outcomes."""

    # Intake — the request is understood but nothing runs yet.
    ACCEPTED = "accepted"
    READY = "ready"

    # Execution — a real process was observed to start.
    RUNNING = "running"
    EXECUTION_FINISHED = "execution_finished"

    # Verification — the produced artifact is being / has been checked.
    VERIFYING = "verifying"
    VERIFIED = "verified"

    # Delivery — the verified result is being / has been handed back.
    DELIVERING = "delivering"
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"
    DELIVERED = "delivered"

    # Unordered outcomes. Unknown/blocked stay discoverable for reconciliation.
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Phases where no real execution has been observed yet. Reporting any of
#: these as "running" is the misreporting bug this module prevents.
UNRESOLVED_PHASES: FrozenSet[Phase] = frozenset({Phase.ACCEPTED, Phase.READY})

EXECUTION_PHASES: FrozenSet[Phase] = frozenset(
    {Phase.RUNNING, Phase.EXECUTION_FINISHED}
)

VERIFICATION_PHASES: FrozenSet[Phase] = frozenset({Phase.VERIFYING, Phase.VERIFIED})

DELIVERY_PHASES: FrozenSet[Phase] = frozenset(
    {Phase.DELIVERING, Phase.DELIVERY_UNCONFIRMED, Phase.DELIVERED}
)

#: Terminal phases. ``DELIVERED`` is the only successful one.
TERMINAL_PHASES: FrozenSet[Phase] = frozenset(
    {Phase.DELIVERED, Phase.CANCELLED}
)

_FAILURES = frozenset({Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED})
ALLOWED_TRANSITIONS = {
    Phase.ACCEPTED: frozenset({Phase.READY}) | _FAILURES,
    Phase.READY: frozenset({Phase.RUNNING}) | _FAILURES,
    Phase.RUNNING: frozenset({Phase.EXECUTION_FINISHED}) | _FAILURES,
    Phase.EXECUTION_FINISHED: frozenset({Phase.VERIFYING}) | _FAILURES,
    Phase.VERIFYING: frozenset({Phase.VERIFIED}) | _FAILURES,
    Phase.VERIFIED: frozenset({Phase.DELIVERING}) | _FAILURES,
    Phase.DELIVERING: frozenset({Phase.DELIVERED, Phase.DELIVERY_UNCONFIRMED}) | _FAILURES,
    Phase.DELIVERY_UNCONFIRMED: frozenset({Phase.DELIVERING, Phase.DELIVERED}) | _FAILURES,
    **{phase: frozenset() for phase in TERMINAL_PHASES},
    Phase.UNKNOWN: frozenset(),
    Phase.BLOCKED: frozenset(),
}

# Happy-path order. Deliberately excludes UNKNOWN/BLOCKED/CANCELLED so that
# comparing against them raises instead of silently ordering a failure.
_ORDER = (
    Phase.ACCEPTED,
    Phase.READY,
    Phase.RUNNING,
    Phase.EXECUTION_FINISHED,
    Phase.VERIFYING,
    Phase.VERIFIED,
    Phase.DELIVERING,
    Phase.DELIVERY_UNCONFIRMED,
    Phase.DELIVERED,
)

_RANKS = {phase: index for index, phase in enumerate(_ORDER)}


def phase_rank(phase: Phase) -> int:
    """Return the happy-path position of ``phase``.

    Raises :class:`LifecycleError` for unordered terminal outcomes, because
    "unknown" is not further along than "running" — it is a different kind of
    answer entirely.
    """

    try:
        return _RANKS[phase]
    except KeyError:
        raise LifecycleError(
            f"phase {phase.value!r} has no happy-path rank; it is an unordered outcome"
        ) from None


def is_complete(phase: Phase) -> bool:
    """Only a confirmed delivery counts as a completed task."""

    return phase is Phase.DELIVERED


def require_phase(phase: Phase, *, at_least: Phase) -> None:
    """Assert ``phase`` has reached ``at_least`` on the happy path."""

    try:
        current = phase_rank(phase)
    except LifecycleError as exc:
        raise PhaseTransitionError(str(exc)) from None
    if current < phase_rank(at_least):
        raise PhaseTransitionError(
            f"phase {phase.value!r} has not reached {at_least.value!r}"
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
