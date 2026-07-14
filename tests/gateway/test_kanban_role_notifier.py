import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import _truncate_kanban_markdown
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


async def _run_one_role_delivery_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_role_delivery_watcher(interval=1)


def _authorize_profiles(monkeypatch, tmp_path, *profiles):
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            (profile, tmp_path / "profiles" / profile) for profile in profiles
        ],
    )


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


def _role_message(
    kind,
    payload=None,
    *,
    task_id="t_123",
    title="Lifecycle formatting",
    result=None,
):
    return GatewayRunner._kanban_role_delivery_message(
        {"task_id": task_id},
        SimpleNamespace(kind=kind, payload=payload),
        SimpleNamespace(title=title, result=result),
        "engineering",
        "shinei",
    )


def test_discord_role_delivery_running_uses_compact_markdown_without_faux_role():
    message = _role_message(
        "spawned",
        title="Implement lifecycle formatting",
    )

    assert message == (
        "### Running · `t_123` · `[engineering]`\n"
        "**Task:** Implement lifecycle formatting"
    )
    assert "@shinei" not in message


def test_discord_role_delivery_progress_preserves_structured_markdown_and_legacy_note():
    structured = _role_message(
        "heartbeat",
        {
            "note": "**Current:** formatter\n**Evidence:** 3 tests passed\n**Next:** outbox"
        },
    )
    legacy = _role_message("heartbeat", {"note": "unit tests are green"})

    assert structured == (
        "### Progress · `t_123` · `[engineering]`\n"
        "**Current:** formatter\n**Evidence:** 3 tests passed\n**Next:** outbox"
    )
    assert legacy == (
        "### Progress · `t_123` · `[engineering]`\n**Update:** unit tests are green"
    )
    assert _role_message("heartbeat", {"note": "  "}) is None
    assert "@shinei" not in structured + legacy


def test_discord_progress_preserves_indented_code_and_trailing_markdown_spaces():
    note = "    indented code\nnext line  "

    message = _role_message("heartbeat", {"note": note})

    assert message == f"### Progress · `t_123` · `[engineering]`\n{note}"


def test_discord_role_delivery_neutralizes_real_mention_tokens():
    message = _role_message(
        "heartbeat",
        {"note": "users <@123> <@!456>, role <@&789>, @everyone and @here"},
    )

    for active_token in (
        "<@123>",
        "<@!456>",
        "<@&789>",
        "@everyone",
        "@here",
    ):
        assert active_token not in message
    assert "<@\u200b123>" in message
    assert "<@\u200b!456>" in message
    assert "<@\u200b&789>" in message
    assert "@\u200beveryone" in message
    assert "@\u200bhere" in message


@pytest.mark.parametrize(
    ("event_kind", "block_kind", "label"),
    [
        ("blocked", "needs_input", "Needs input"),
        ("dependency_wait", "dependency", "Waiting on"),
        ("blocked", "transient", "Retry issue"),
        ("blocked", "capability", "Limitation"),
        ("blocked", None, "Blocked by"),
    ],
)
def test_discord_role_delivery_blocked_kind_has_truthful_label(
    event_kind,
    block_kind,
    label,
):
    message = _role_message(
        event_kind,
        {"kind": block_kind, "reason": "The upstream result is unavailable."},
    )

    assert message == (
        f"### Blocked · `t_123` · `[engineering]`\n"
        f"**Task:** Lifecycle formatting\n"
        f"**{label}:** The upstream result is unavailable."
    )
    assert ("Needs input" in message) is (block_kind == "needs_input")
    assert "@shinei" not in message


def test_discord_blocked_preserves_reason_markdown_whitespace():
    reason = "    indented blocker\nnext line  "

    message = _role_message("blocked", {"kind": "dependency", "reason": reason})

    assert message.endswith(f"**Waiting on:**\n{reason}")


def test_discord_role_delivery_completed_preserves_worker_markdown_without_fabrication():
    message = _role_message(
        "completed",
        {"summary": "Implemented formatter.\n\n- 12 tests passed\n- routing unchanged"},
    )
    no_evidence = _role_message("completed", {})

    assert message == (
        "### Completed · `t_123` · `[engineering]`\n"
        "**Task:** Lifecycle formatting\n"
        "**Result:**\n"
        "Implemented formatter.\n\n- 12 tests passed\n- routing unchanged"
    )
    assert no_evidence == (
        "### Completed · `t_123` · `[engineering]`\n**Task:** Lifecycle formatting"
    )
    assert "@shinei" not in message + no_evidence


def test_discord_completion_reserves_budget_for_result_after_long_task_title():
    message = _role_message(
        "completed",
        {"summary": "Verified evidence: 174 tests passed."},
        title="very long task " * 300,
    )

    assert len(message) <= 1800
    assert "**Task:**" in message
    assert "**Result:**\nVerified evidence: 174 tests passed." in message
    assert "…" in message


def test_discord_completion_preserves_result_markdown_whitespace():
    summary = "    indented result\nnext line  "

    message = _role_message("completed", {"summary": summary})

    assert message.endswith(f"**Result:**\n{summary}")


def test_discord_role_delivery_budget_is_unicode_safe_and_closes_truncated_fence():
    note = (
        "**Current:** 검증 완료 🙂\n"
        "```python\n" + ("print('한글🙂')\n" * 400) + "```\n**Next:** outbox"
    )

    message = _role_message("heartbeat", {"note": note})

    assert len(message) <= 1800
    assert "검증 완료 🙂" in message
    assert message.endswith("…")
    assert message.count("```") % 2 == 0
    assert "\ufffd" not in message


def test_markdown_budget_keeps_completed_fence_with_literal_backtick_well_formed():
    note = 'before\n```python\nprint("`")\n```\nafter ' + ("word " * 40)

    truncated = _truncate_kanban_markdown(note, 120)

    assert len(truncated) <= 120
    assert 'print("`")\n```' in truncated
    assert truncated.count("```") == 2
    assert truncated.endswith("…")


def test_markdown_budget_ignores_emphasis_tokens_inside_completed_fence():
    note = 'before\n```python\nprint("**")\n```\nafter ' + ("word " * 40)

    truncated = _truncate_kanban_markdown(note, 120)

    assert 'print("**")\n```' in truncated
    assert truncated.count("```") == 2
    assert truncated.endswith("…")


@pytest.mark.parametrize("token", ["`", "**"])
def test_markdown_budget_drops_unclosed_inline_construct(token):
    note = f"verified words {token}unfinished " + ("more " * 40)

    truncated = _truncate_kanban_markdown(note, 80)

    assert truncated == "verified words…"


@pytest.mark.parametrize("marker", ["*", "_", "__"])
def test_markdown_budget_drops_emphasis_whose_closer_is_beyond_budget(marker):
    note = f"verified {marker}" + ("inside " * 20) + marker + " trailing evidence"

    truncated = _truncate_kanban_markdown(note, 80)

    assert truncated == "verified…"


def test_markdown_budget_ignores_escaped_literal_backtick():
    note = "safe words \\`literal backtick then retained evidence " + ("more " * 30)

    truncated = _truncate_kanban_markdown(note, 80)

    assert len(truncated) <= 80
    assert truncated.startswith("safe words \\`literal backtick then retained evidence")
    assert truncated.endswith("…")


def test_discord_role_delivery_long_unbroken_token_is_omitted_not_mid_token_clipped():
    message = _role_message("heartbeat", {"note": "가" * 3000})

    assert len(message) <= 1800
    assert message.endswith("…")
    assert "가" not in message


@pytest.mark.parametrize(
    ("kind", "payload", "heading"),
    [
        ("gave_up", {"error": "spawn failed"}, "Stopped"),
        ("crashed", None, "Retrying"),
        ("timed_out", {"limit_seconds": 90}, "Timed out"),
    ],
)
def test_discord_role_delivery_failure_events_keep_context_without_faux_role(
    kind,
    payload,
    heading,
):
    message = _role_message(kind, payload)

    assert message.startswith(
        f"### {heading} · `t_123` · `[engineering]`\n**Task:** Lifecycle formatting"
    )
    assert "@shinei" not in message


def test_discord_completion_uses_event_run_profile_after_reassignment(
    tmp_path, monkeypatch
):
    """A retry/reassignment cannot make an old run speak as the new assignee."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "run-profile-routing.db"))
    _authorize_profiles(monkeypatch, tmp_path, "default", "shinei", "raiden")
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
    sender = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))

    assert len(shinei.sent) == 1
    assert "@shinei" not in shinei.sent[0]["text"]
    assert shinei.sent[0]["text"].startswith("### Completed")
    assert coordinator.sent == []
    assert raiden.sent == []


def test_discord_execution_uses_active_named_profile_adapter(tmp_path, monkeypatch):
    """The active profile lives in self.adapters, not _profile_adapters."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "active-profile.db"))
    _authorize_profiles(monkeypatch, tmp_path, "default", "shinei")
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
    runner._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, runner))

    assert len(shinei.sent) == 1
    assert "active profile completed" in shinei.sent[0]["text"]


def test_non_discord_progress_keeps_legacy_message_format(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "telegram-legacy.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy transport", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        task = kb.claim_task(conn, tid)
        assert task is not None
        assert kb.heartbeat_worker(
            conn,
            tid,
            note="legacy update",
            expected_run_id=task.current_run_id,
        )
    finally:
        conn.close()

    telegram = RecordingAdapter()
    runner = _make_discord_runner(RecordingAdapter())
    runner.adapters = {Platform.TELEGRAM: telegram}
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [sent["text"] for sent in telegram.sent] == [
        f"… [default] @shinei Kanban {tid} progress: legacy update"
    ]


def test_discord_execution_uses_active_default_profile_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "active-default.db"))
    _authorize_profiles(monkeypatch, tmp_path, "default")
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
    runner._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, runner))

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

    assert secondary_calls == []
    assert directly_registered.sent == []
    assert len(active.sent) == 1
    warning = active.sent[0]["text"].lower()
    assert "delivery failed" in warning
    assert "sensitive worker result" not in warning
    assert "internal resolver detail" not in warning


def test_discord_spawned_notifies_running_once_from_run_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "spawned-routing.db"))
    _authorize_profiles(monkeypatch, tmp_path, "default", "shinei")
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
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(shinei, active_profile="shinei"),
    ))

    assert len(shinei.sent) == 1
    assert "running" in shinei.sent[0]["text"].lower()
    assert coordinator.sent == []


def test_discord_heartbeat_only_sends_nonempty_note(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "heartbeat-routing.db"))
    _authorize_profiles(monkeypatch, tmp_path, "default", "shinei")
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="progress", assignee="shinei")
        kb.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="thread-1")
        task = kb.claim_task(conn, tid)
        assert task is not None
        assert kb.heartbeat_worker(
            conn, tid, note=None, expected_run_id=task.current_run_id
        )
        structured_note = (
            "**Current:** formatter\n"
            "**Evidence:** producer → SQLite → consumer\n"
            "**Next:** routing regression"
        )
        assert kb.heartbeat_worker(
            conn,
            tid,
            note=structured_note,
            expected_run_id=task.current_run_id,
        )
    finally:
        conn.close()

    shinei = RecordingAdapter()
    runner = _make_discord_runner(
        RecordingAdapter(),
        {"shinei": {Platform.DISCORD: shinei}},
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    asyncio.run(
        _run_one_role_delivery_tick(
            monkeypatch,
            _make_discord_runner(shinei, active_profile="shinei"),
        )
    )

    assert len(shinei.sent) == 1
    assert shinei.sent[0]["text"] == (
        f"### Progress · `{tid}` · `[default]`\n{structured_note}"
    )


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
    assert "@shinei" not in warning
    assert "`shinei`" in warning
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
