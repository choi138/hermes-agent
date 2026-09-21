"""Contract tests for the shared task-lifecycle vocabulary.

Every milestone module (contract/registry/executor, acceptance/delivery,
context pack/correction, metrics) imports these primitives.  They are
deliberately tiny: the point is that "accepted", "running", "verified" and
"delivered" are *distinct* states with an explicit, non-guessable ordering,
and that a phase can never be inferred from an adjacent one.
"""

import pytest

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


class TestPhaseVocabulary:
    def test_every_lifecycle_stage_is_a_separate_phase(self):
        # Execution ending, verification passing and delivery landing are
        # three different events. Collapsing any pair is the defect this
        # vocabulary exists to prevent.
        assert Phase.EXECUTION_FINISHED != Phase.VERIFIED
        assert Phase.VERIFIED != Phase.DELIVERED
        assert Phase.ACCEPTED != Phase.RUNNING

    def test_accepted_is_not_running(self):
        # "queued/ready" must never be reported as "running".
        assert Phase.ACCEPTED in UNRESOLVED_PHASES
        assert Phase.READY in UNRESOLVED_PHASES
        assert Phase.RUNNING not in UNRESOLVED_PHASES

    def test_phase_groups_are_disjoint_and_cover_the_pipeline(self):
        groups = [EXECUTION_PHASES, VERIFICATION_PHASES, DELIVERY_PHASES]
        for left_index, left in enumerate(groups):
            for right in groups[left_index + 1 :]:
                assert not (left & right)
        assert Phase.RUNNING in EXECUTION_PHASES
        assert Phase.VERIFIED in VERIFICATION_PHASES
        assert Phase.DELIVERED in DELIVERY_PHASES

    def test_unknown_and_blocked_remain_recoverable_but_not_complete(self):
        assert Phase.UNKNOWN not in TERMINAL_PHASES
        assert Phase.BLOCKED not in TERMINAL_PHASES
        assert not is_complete(Phase.UNKNOWN)
        assert not is_complete(Phase.BLOCKED)


class TestCompletion:
    def test_only_delivered_counts_as_complete(self):
        assert is_complete(Phase.DELIVERED)

    @pytest.mark.parametrize(
        "phase",
        [
            Phase.ACCEPTED,
            Phase.READY,
            Phase.RUNNING,
            Phase.EXECUTION_FINISHED,
            Phase.VERIFYING,
            Phase.VERIFIED,
            Phase.DELIVERING,
            Phase.DELIVERY_UNCONFIRMED,
        ],
    )
    def test_no_earlier_phase_is_complete(self, phase):
        # Notably: a passing verification is NOT completion, and an
        # unconfirmed delivery is NOT completion.
        assert not is_complete(phase)


class TestOrdering:
    def test_rank_is_monotonic_along_the_happy_path(self):
        happy = [
            Phase.ACCEPTED,
            Phase.READY,
            Phase.RUNNING,
            Phase.EXECUTION_FINISHED,
            Phase.VERIFYING,
            Phase.VERIFIED,
            Phase.DELIVERING,
            Phase.DELIVERED,
        ]
        ranks = [phase_rank(p) for p in happy]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)

    def test_terminal_failure_phases_have_no_rank(self):
        for phase in (Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED):
            with pytest.raises(LifecycleError):
                phase_rank(phase)


class TestRequirePhase:
    def test_require_phase_accepts_a_satisfied_precondition(self):
        require_phase(Phase.VERIFIED, at_least=Phase.EXECUTION_FINISHED)

    def test_require_phase_rejects_an_unmet_precondition(self):
        # Delivering before verification is the exact mistake that lets a
        # "done" report escape without evidence.
        with pytest.raises(PhaseTransitionError) as excinfo:
            require_phase(Phase.RUNNING, at_least=Phase.VERIFIED)
        assert "running" in str(excinfo.value)
        assert "verified" in str(excinfo.value)

    def test_require_phase_rejects_unresolved_terminal_states(self):
        with pytest.raises(PhaseTransitionError):
            require_phase(Phase.UNKNOWN, at_least=Phase.ACCEPTED)


class TestErrors:
    def test_transition_error_is_a_lifecycle_error(self):
        assert issubclass(PhaseTransitionError, LifecycleError)
