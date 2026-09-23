from gateway.agent_health import (
    should_emit_output_silence,
    should_enforce_turn_deadline,
)


def test_output_silence_boundary_does_not_require_pending_inbound():
    assert should_emit_output_silence(
        silence_seconds=600,
        threshold=600,
        turn_live=True,
        already_notified=False,
    )


def test_output_silence_ignores_internal_agent_activity():
    # There is deliberately no activity-summary input: output wall time is the
    # sole progress clock for this policy.
    assert should_emit_output_silence(
        silence_seconds=601,
        threshold=600,
        turn_live=True,
        already_notified=False,
    )


def test_content_delivery_moves_silence_below_boundary():
    assert not should_emit_output_silence(
        silence_seconds=1,
        threshold=600,
        turn_live=True,
        already_notified=False,
    )


def test_output_silence_latches_once():
    assert not should_emit_output_silence(
        silence_seconds=900,
        threshold=600,
        turn_live=True,
        already_notified=True,
    )


def test_output_silence_is_suppressed_while_waiting_on_user_at_boundary():
    assert not should_emit_output_silence(
        silence_seconds=600,
        threshold=600,
        turn_live=True,
        already_notified=False,
        waiting_on_user=True,
    )


def test_generation_key_change_rearms_latch():
    latches = {("session", 14)}
    assert ("session", 15) not in latches
    assert should_emit_output_silence(
        silence_seconds=600,
        threshold=600,
        turn_live=True,
        already_notified=("session", 15) in latches,
    )


def test_deadline_boundary_and_live_turn_gate():
    assert should_enforce_turn_deadline(
        silence_seconds=1500,
        deadline=1500,
        turn_live=True,
    )
    assert not should_enforce_turn_deadline(
        silence_seconds=1500,
        deadline=1500,
        turn_live=False,
    )


def test_deadline_is_suppressed_while_waiting_on_user_at_boundary():
    assert not should_enforce_turn_deadline(
        silence_seconds=1500,
        deadline=1500,
        turn_live=True,
        waiting_on_user=True,
    )
