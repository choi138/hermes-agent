"""Tests for Discord's semantic, secret-safe progress snapshot."""

from gateway.progress_snapshot import ProgressSnapshot, SemanticProgressTracker


def test_snapshot_renders_three_semantic_fields() -> None:
    snapshot = ProgressSnapshot(
        stage="Inspecting the current state",
        confirmed="Request received",
        next_action="Gather evidence before changing anything",
    )

    assert snapshot.render() == (
        "**Current stage:** Inspecting the current state\n"
        "**Confirmed:** Request received\n"
        "**Next:** Gather evidence before changing anything"
    )


def test_kanban_tool_is_rendered_as_inspection_without_raw_details() -> None:
    tracker = SemanticProgressTracker()
    snapshot = tracker.tool_started(
        "kanban_show",
        preview="board=private-roadmap",
        args={"token": "super-secret", "board": "private-roadmap"},
    )
    rendered = snapshot.render()

    assert snapshot.stage == "Inspecting the current state"
    assert "kanban_show" not in rendered
    assert "private-roadmap" not in rendered
    assert "super-secret" not in rendered


def test_terminal_activity_maps_to_semantic_work_phases() -> None:
    tracker = SemanticProgressTracker()

    assert tracker.tool_started(
        "terminal", preview="scripts/run_tests.sh tests/gateway -q"
    ).stage == "Verifying the changes"
    assert tracker.tool_started(
        "apply_patch", preview="*** Update File: private.py"
    ).stage == "Applying the change"
    assert tracker.tool_started(
        "terminal", preview="ssh pi 'sudo systemctl restart hermes-gateway'"
    ).stage == "Deploying the verified change"
    assert tracker.tool_started(
        "terminal", preview="journalctl -u hermes-gateway --since today"
    ).stage == "Observing runtime health"
    assert tracker.tool_started(
        "terminal", preview="ssh pi 'scripts/run_tests.sh tests/gateway -q'"
    ).stage == "Verifying the changes"
    assert tracker.tool_started(
        "terminal", preview="ssh pi 'journalctl -u hermes-gateway -n 50'"
    ).stage == "Observing runtime health"


def test_only_successful_completion_updates_confirmed_evidence() -> None:
    tracker = SemanticProgressTracker()
    started = tracker.tool_started(
        "terminal", preview="scripts/run_tests.sh tests/gateway -q"
    )

    failed = tracker.tool_completed(
        "terminal",
        is_error=True,
        result="token=must-never-render",
    )
    assert failed.confirmed == started.confirmed
    assert "must-never-render" not in failed.render()
    assert failed.next_action == "Review the failure and choose a safe retry or alternative"

    tracker.tool_started("terminal", preview="scripts/run_tests.sh tests/gateway -q")
    succeeded = tracker.tool_completed(
        "terminal",
        is_error=False,
        result="all tests passed; password=still-private",
    )
    assert succeeded.confirmed == "Verification completed successfully"
    assert "still-private" not in succeeded.render()


def test_initial_and_cancelled_snapshots_do_not_claim_unverified_work() -> None:
    tracker = SemanticProgressTracker()

    assert tracker.snapshot.confirmed == "Request received"
    cancelled = tracker.cancelled()
    assert cancelled.stage == "Request stopped"
    assert cancelled.confirmed == "No work was started"
