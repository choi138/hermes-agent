"""Tests for Discord's semantic, secret-safe progress snapshot."""

from gateway.progress_snapshot import (
    ProgressSnapshot,
    SemanticProgressTracker,
    render_busy_progress,
    render_inactivity_timeout,
    render_inactivity_warning,
    render_long_running,
)


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


def test_korean_snapshot_localizes_all_fields_without_internal_details() -> None:
    tracker = SemanticProgressTracker(language="ko")
    snapshot = tracker.tool_started(
        "terminal",
        preview="scripts/run_tests.sh tests/private -q",
        args={"command": "scripts/run_tests.sh tests/private -q"},
    )
    rendered = snapshot.render()

    assert rendered.startswith("**현재 단계:** 변경 사항을 검증하고 있습니다")
    assert "**확인:** 요청을 수신했습니다" in rendered
    assert "**다음:**" in rendered
    assert "terminal" not in rendered
    assert "iteration" not in rendered.casefold()
    assert "Current stage" not in rendered


def test_generic_long_running_copy_never_contains_tool_or_iteration() -> None:
    rendered = render_long_running(3, language="ko")

    assert rendered == "⏳ 작업을 계속 진행하고 있습니다 (3분 경과)."
    assert "terminal" not in rendered
    assert "iteration" not in rendered.casefold()


def test_discord_long_running_copy_stays_inside_korean_semantic_template() -> None:
    tracker = SemanticProgressTracker(language="ko")
    snapshot = tracker.tool_started("terminal", preview="private command")

    rendered = render_long_running(3, language="ko", snapshot=snapshot)

    assert rendered.startswith("**현재 단계:**")
    assert "**확인:** 요청을 수신했습니다 · ⏳ 작업을 계속 진행하고 있습니다 (3분 경과)." in rendered
    assert "**다음:**" in rendered
    assert "terminal" not in rendered
    assert "iteration" not in rendered.casefold()


def test_all_discord_long_running_notices_share_safe_korean_template() -> None:
    tracker = SemanticProgressTracker(language="ko")
    tracker.tool_started(
        "terminal",
        preview="scripts/run_tests.sh tests/private -q password=secret",
    )
    confirmed = tracker.tool_completed(
        "terminal",
        is_error=False,
        result="private result token=secret",
    )

    rendered_notices = (
        render_busy_progress(3, language="ko"),
        render_inactivity_warning(
            15,
            language="ko",
            snapshot=confirmed,
        ),
        render_inactivity_timeout(
            language="ko",
            snapshot=confirmed,
        ),
    )

    for rendered in rendered_notices:
        assert "**현재 단계:**" in rendered
        assert "**확인:**" in rendered
        assert "**다음:**" in rendered
        for forbidden in (
            "terminal",
            "iteration",
            "7/25",
            "Working —",
            "Current stage",
            "Confirmed:",
            "Next:",
            "password=secret",
            "token=secret",
        ):
            assert forbidden not in rendered

    # Warning and timeout retain the last confirmed evidence rather than
    # replacing it with an implementation detail or an elapsed timer.
    assert "검증을 완료했습니다" in rendered_notices[1]
    assert "검증을 완료했습니다" in rendered_notices[2]
