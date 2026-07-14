import asyncio
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


class FailingAdapter(RecordingAdapter):
    async def send(self, chat_id, text, metadata=None):
        raise PermissionError("missing channel permission")


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_discord_runner(
    coordinator, profile_adapters=None, *, active_profile="default",
):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: coordinator}
    runner._profile_adapters = profile_adapters or {}
    runner._kanban_notifier_profile = active_profile
    runner._kanban_sub_fail_counts = {}
    return runner


def test_discord_completion_uses_event_run_profile_after_reassignment(tmp_path, monkeypatch):
    """A retry/reassignment cannot make an old run speak as the new assignee."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "run-profile-routing.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="role routing", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        conn.execute("UPDATE tasks SET assignee = 'raiden' WHERE id = ?", (tid,))
        conn.commit()
        kb.complete_task(conn, tid, summary="implemented by the original run")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    shinei = RecordingAdapter()
    raiden = RecordingAdapter()
    runner = _make_discord_runner(
        coordinator,
        profile_adapters={
            "shinei": {Platform.DISCORD: shinei},
            "raiden": {Platform.DISCORD: raiden},
        },
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert "@shinei" in shinei.sent[0]["text"]
    assert coordinator.sent == []
    assert raiden.sent == []


def test_discord_execution_uses_active_named_profile_adapter(tmp_path, monkeypatch):
    """The active profile lives in self.adapters, not _profile_adapters."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "active-profile.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="active worker", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="active profile completed")
    finally:
        conn.close()

    shinei = RecordingAdapter()
    runner = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert "active profile completed" in shinei.sent[0]["text"]


def test_discord_execution_uses_active_default_profile_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "active-default.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="default worker", assignee="default")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="default profile completed")
    finally:
        conn.close()

    default = RecordingAdapter()
    runner = _make_discord_runner(default)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(default.sent) == 1
    assert "default profile completed" in default.sent[0]["text"]


@pytest.mark.parametrize("profile", [None, "", "   "])
def test_execution_adapter_rejects_blank_profile_without_resolver(monkeypatch, profile):
    active = RecordingAdapter()
    runner = _make_discord_runner(active, active_profile="shinei")
    resolver = Mock(return_value=RecordingAdapter())
    monkeypatch.setattr(runner, "_authorization_adapter", resolver)

    assert runner._kanban_execution_adapter(Platform.DISCORD, profile) is None
    resolver.assert_not_called()


def test_execution_adapter_delegates_secondary_default_to_authorization_resolver(
    monkeypatch,
):
    active = RecordingAdapter()
    secondary_default = RecordingAdapter()
    runner = _make_discord_runner(active, active_profile="shinei")
    resolver = Mock(return_value=secondary_default)
    monkeypatch.setattr(runner, "_authorization_adapter", resolver)

    assert (
        runner._kanban_execution_adapter(Platform.DISCORD, "default")
        is secondary_default
    )
    resolver.assert_called_once_with(Platform.DISCORD, "default")


def test_execution_adapter_real_resolver_routes_secondary_default_without_active_fallback(
    monkeypatch,
):
    active = RecordingAdapter()
    secondary_default = RecordingAdapter()
    runner = _make_discord_runner(
        active,
        {"default": {Platform.DISCORD: secondary_default}},
        active_profile="shinei",
    )
    monkeypatch.setattr(runner, "_active_profile_name", lambda: "shinei")

    assert (
        runner._kanban_execution_adapter(Platform.DISCORD, "default")
        is secondary_default
    )


@pytest.mark.parametrize(
    ("active_profile", "event_profile"),
    [("shinei", "shinei"), ("default", "default")],
)
def test_execution_adapter_active_profile_bypasses_authorization_resolver(
    monkeypatch, active_profile, event_profile,
):
    active = RecordingAdapter()
    runner = _make_discord_runner(active, active_profile=active_profile)
    resolver = Mock(side_effect=AssertionError("active profile must bypass resolver"))
    monkeypatch.setattr(runner, "_authorization_adapter", resolver)

    assert runner._kanban_execution_adapter(Platform.DISCORD, event_profile) is active
    resolver.assert_not_called()


def test_execution_adapter_delegates_secondary_to_authorization_resolver(monkeypatch):
    active = RecordingAdapter()
    directly_registered = RecordingAdapter()
    authorized = RecordingAdapter()
    runner = _make_discord_runner(
        active,
        {"raiden": {Platform.DISCORD: directly_registered}},
        active_profile="shinei",
    )
    resolver = Mock(return_value=authorized)
    monkeypatch.setattr(runner, "_authorization_adapter", resolver)

    assert runner._kanban_execution_adapter(Platform.DISCORD, "raiden") is authorized
    resolver.assert_called_once_with(Platform.DISCORD, "raiden")


@pytest.mark.parametrize("resolved", [None, "", "   ", False])
def test_execution_adapter_rejects_empty_secondary_resolution(monkeypatch, resolved):
    runner = _make_discord_runner(RecordingAdapter(), active_profile="shinei")
    resolver = Mock(return_value=resolved)
    monkeypatch.setattr(runner, "_authorization_adapter", resolver)

    assert runner._kanban_execution_adapter(Platform.DISCORD, "raiden") is None
    resolver.assert_called_once_with(Platform.DISCORD, "raiden")


@pytest.mark.parametrize("resolver_outcome", ["none", "blank", "exception"])
def test_secondary_resolver_failure_warns_without_event_or_active_fallback(
    tmp_path, monkeypatch, resolver_outcome,
):
    monkeypatch.setenv(
        "HERMES_KANBAN_DB", str(tmp_path / f"resolver-{resolver_outcome}.db")
    )
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="central policy", assignee="raiden")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="sensitive worker result")
    finally:
        conn.close()

    active = RecordingAdapter()
    directly_registered = RecordingAdapter()
    runner = _make_discord_runner(
        active,
        {"raiden": {Platform.DISCORD: directly_registered}},
        active_profile="shinei",
    )
    secondary_calls = []

    def resolve(platform, profile=None):
        if profile is None:
            return active
        secondary_calls.append((platform, profile))
        if resolver_outcome == "exception":
            raise PermissionError("internal resolver detail")
        if resolver_outcome == "blank":
            return "   "
        return None

    monkeypatch.setattr(runner, "_authorization_adapter", resolve)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert secondary_calls == [(Platform.DISCORD, "raiden")]
    assert directly_registered.sent == []
    assert len(active.sent) == 1
    warning = active.sent[0]["text"].lower()
    assert "delivery failed" in warning
    assert "sensitive worker result" not in warning
    assert "internal resolver detail" not in warning


def test_discord_spawned_notifies_running_once_from_run_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "spawned-routing.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="starting", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, 4242)
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    shinei = RecordingAdapter()
    profile_adapters = {"shinei": {Platform.DISCORD: shinei}}
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, profile_adapters),
    ))
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, profile_adapters),
    ))

    assert len(shinei.sent) == 1
    assert "running" in shinei.sent[0]["text"].lower()
    assert coordinator.sent == []


def test_discord_heartbeat_only_sends_nonempty_note(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "heartbeat-routing.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="progress", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        task = kb.claim_task(conn, tid)
        assert task is not None
        assert kb.heartbeat_worker(conn, tid, note=None, expected_run_id=task.current_run_id)
        assert kb.heartbeat_worker(
            conn, tid, note="unit tests are green", expected_run_id=task.current_run_id,
        )
    finally:
        conn.close()

    shinei = RecordingAdapter()
    runner = _make_discord_runner(
        RecordingAdapter(), {"shinei": {Platform.DISCORD: shinei}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert "unit tests are green" in shinei.sent[0]["text"]


def test_missing_discord_run_adapter_warns_via_coordinator_without_impersonation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing-role-adapter.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="missing role bot", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="done but sender unavailable")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    runner = _make_discord_runner(coordinator)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(coordinator.sent) == 1
    warning = coordinator.sent[0]["text"].lower()
    assert "delivery failed" in warning
    assert "shinei" in warning
    assert " done" not in warning


def test_missing_secondary_never_falls_back_to_active_named_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "no-active-fallback.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="secondary unavailable", assignee="raiden")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="must not speak as shinei")
    finally:
        conn.close()

    shinei = RecordingAdapter()
    runner = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    warning = shinei.sent[0]["text"].lower()
    assert "delivery failed" in warning
    assert "raiden" in warning
    assert "must not speak as shinei" not in warning
