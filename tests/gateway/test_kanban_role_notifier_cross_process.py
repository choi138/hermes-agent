import asyncio
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from gateway.config import Platform
from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_role_notifier import (
    RecordingAdapter,
    _make_discord_runner,
    _run_one_notifier_tick,
)


def _run_isolated_gateway(code: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env={**os.environ, **env, "PYTHONPATH": os.getcwd()},
        check=True,
        capture_output=True,
        text=True,
    )


class ArtifactRecordingAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.documents = []

    def extract_local_files(self, text):
        return [], text

    async def send_document(self, chat_id, file_path, metadata=None):
        self.documents.append({
            "chat_id": chat_id,
            "file_path": file_path,
            "metadata": metadata or {},
        })


class PartialFailureArtifactAdapter(ArtifactRecordingAdapter):
    def __init__(self, fail_once_path):
        super().__init__()
        self.fail_once_path = str(fail_once_path)
        self.failed = False

    async def send_document(self, chat_id, file_path, metadata=None):
        if str(file_path) == self.fail_once_path and not self.failed:
            self.failed = True
            raise OSError("transient upload failure")
        await super().send_document(chat_id, file_path, metadata)


async def _run_one_role_delivery_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_role_delivery_watcher(interval=1)


def test_separate_gateway_process_sends_completion_from_run_profile_once(
    tmp_path, monkeypatch,
):
    """The dispatch owner and role bot share only SQLite, never adapters."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "cross-process.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="cross-process role delivery", assignee="shinei",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="guild-channel",
            thread_id="origin-thread",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="sent by the real Shinei bot")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    owner = _make_discord_runner(coordinator, active_profile="default")
    # Model a separate OS process: this runner owns Shinei's real adapter, but
    # neither runner has a reference to the other's adapter or registry.
    shinei = RecordingAdapter()
    sender = _make_discord_runner(shinei, active_profile="shinei")

    asyncio.run(_run_one_notifier_tick(monkeypatch, owner))
    sender._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))
    sender._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))

    assert len(shinei.sent) == 1
    assert "sent by the real Shinei bot" in shinei.sent[0]["text"]
    assert shinei.sent[0]["metadata"] == {"thread_id": "origin-thread"}
    assert coordinator.sent == []


def test_two_isolated_interpreters_share_only_sqlite_for_role_delivery(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "isolated.db"
    output_path = tmp_path / "shinei-send.json"
    env = {"HERMES_KANBAN_DB": str(db_path)}
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="real subprocesses", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="crossed the process boundary")
    finally:
        conn.close()

    producer = r'''
import asyncio
from pathlib import Path
from hermes_cli import profiles
from tests.gateway.test_kanban_role_notifier import RecordingAdapter, _make_discord_runner
profiles.profiles_to_serve = lambda multiplex: [("default", Path("/tmp/default")), ("shinei", Path("/tmp/shinei"))]
runner = _make_discord_runner(RecordingAdapter(), active_profile="default")
real_sleep = asyncio.sleep
async def sleep(delay):
    if delay == 5: return
    runner._running = False
    await real_sleep(0)
asyncio.sleep = sleep
asyncio.run(runner._kanban_notifier_watcher(interval=1))
'''
    consumer = r'''
import asyncio, json, os
from tests.gateway.test_kanban_role_notifier import RecordingAdapter, _make_discord_runner
adapter = RecordingAdapter()
runner = _make_discord_runner(adapter, active_profile="shinei")
real_sleep = asyncio.sleep
async def sleep(delay):
    if delay == 5: return
    runner._running = False
    await real_sleep(0)
asyncio.sleep = sleep
asyncio.run(runner._kanban_role_delivery_watcher(interval=1))
with open(os.environ["ROLE_OUTPUT"], "w", encoding="utf-8") as handle:
    json.dump(adapter.sent, handle)
'''
    _run_isolated_gateway(producer, env)
    _run_isolated_gateway(consumer, {**env, "ROLE_OUTPUT": str(output_path)})

    sent = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(sent) == 1
    assert "crossed the process boundary" in sent[0]["text"]


def test_concurrent_subprocess_claimant_reclaims_expired_lease_with_fencing(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "lease-race.db"
    marker = tmp_path / "first-claimed"
    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"
    env = {"HERMES_KANBAN_DB": str(db_path)}
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="lease race", assignee="shinei")
        assert kb.claim_task(conn, task_id) is not None
        event = conn.execute(
            "SELECT id, kind FROM task_events WHERE task_id = ? AND run_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        kb.enqueue_role_delivery(
            conn, event_id=event["id"], task_id=task_id,
            event_kind=event["kind"], platform="discord", chat_id="channel",
            thread_id=None, sender_profile="shinei", notifier_profile="default",
            message="lease race",
        )
    finally:
        conn.close()

    first = r'''
import json, os, time
from pathlib import Path
from hermes_cli import kanban_db as kb
conn = kb.connect()
row = kb.claim_role_deliveries(conn, sender_profile="shinei", platform="discord", claimant="first", lease_seconds=1)[0]
Path(os.environ["MARKER"]).write_text("claimed")
conn.close()
second_output = Path(os.environ["SECOND_OUTPUT"])
while not second_output.exists(): time.sleep(0.01)
conn = kb.connect()
ack = kb.complete_role_delivery(conn, delivery_id=row["id"], claim_token=row["claim_token"])
Path(os.environ["OUTPUT"]).write_text(json.dumps({"ack": ack, "id": row["id"], "token": row["claim_token"], "db": str(kb.kanban_db_path())}))
conn.close()
'''
    second = r'''
import json, os, time
from pathlib import Path
from hermes_cli import kanban_db as kb
marker = Path(os.environ["MARKER"])
while not marker.exists(): time.sleep(0.01)
time.sleep(2.1)
conn = kb.connect()
rows = kb.claim_role_deliveries(conn, sender_profile="shinei", platform="discord", claimant="second", lease_seconds=60)
ack = bool(rows) and kb.complete_role_delivery(conn, delivery_id=rows[0]["id"], claim_token=rows[0]["claim_token"])
Path(os.environ["OUTPUT"]).write_text(json.dumps({"count": len(rows), "ack": ack, "id": rows[0]["id"] if rows else None, "token": rows[0]["claim_token"] if rows else None, "db": str(kb.kanban_db_path())}))
conn.close()
'''
    common = {**os.environ, **env, "PYTHONPATH": os.getcwd(), "MARKER": str(marker)}
    first_process = subprocess.Popen(
        [sys.executable, "-c", first], cwd=os.getcwd(),
        env={
            **common,
            "OUTPUT": str(first_output),
            "SECOND_OUTPUT": str(second_output),
        },
    )
    second_process = subprocess.Popen(
        [sys.executable, "-c", second], cwd=os.getcwd(),
        env={**common, "OUTPUT": str(second_output)},
    )
    assert first_process.wait(timeout=10) == 0
    assert second_process.wait(timeout=10) == 0

    stale = json.loads(first_output.read_text(encoding="utf-8"))
    reclaimed = json.loads(second_output.read_text(encoding="utf-8"))
    assert reclaimed["count"] == 1, reclaimed
    assert reclaimed["ack"] is True
    assert stale["db"] == reclaimed["db"] == str(db_path)
    assert stale["id"] == reclaimed["id"]
    assert stale["token"] != reclaimed["token"], (stale, reclaimed)
    assert stale["ack"] is False


def test_authorized_delivery_survives_sender_disconnect_and_process_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "restart.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="restart-safe", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="survived reconnect")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, active_profile="default"),
    ))

    # The first Shinei process is alive but Discord is disconnected. It must not
    # consume or lose the queued row.
    disconnected = _make_discord_runner(RecordingAdapter(), active_profile="shinei")
    disconnected.adapters = {}
    disconnected._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, disconnected))

    # A fresh process after reconnect consumes the durable row exactly once.
    shinei = RecordingAdapter()
    restarted = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, restarted))
    restarted._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, restarted))

    assert [item["text"] for item in shinei.sent] == [
        next(item["text"] for item in shinei.sent if "survived reconnect" in item["text"])
    ]
    assert coordinator.sent == []


def test_cross_process_delivery_keeps_immutable_run_profile_after_reassignment(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "reassignment.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
            ("raiden", tmp_path / "profiles" / "raiden"),
        ],
    )
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="immutable run", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        conn.execute("UPDATE tasks SET assignee = 'raiden' WHERE id = ?", (task_id,))
        conn.commit()
        kb.complete_task(conn, task_id, summary="original run finished")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, active_profile="default"),
    ))
    shinei = RecordingAdapter()
    raiden = RecordingAdapter()
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(shinei, active_profile="shinei"),
    ))
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(raiden, active_profile="raiden"),
    ))

    assert len(shinei.sent) == 1
    assert "@shinei" not in shinei.sent[0]["text"]
    assert shinei.sent[0]["text"].startswith("### Completed")
    assert raiden.sent == []
    assert coordinator.sent == []


def test_cross_process_completion_uploads_artifact_in_origin_thread(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "artifact.db"))
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    artifact = tmp_path / "handoff.txt"
    artifact.write_text("verified artifact")

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="artifact", assignee="shinei")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="channel",
            thread_id="thread-42",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(
            conn,
            task_id,
            summary="artifact ready",
            metadata={"artifacts": [str(artifact)]},
        )
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, active_profile="default"),
    ))
    shinei = ArtifactRecordingAdapter()
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(shinei, active_profile="shinei"),
    ))

    assert len(shinei.sent) == 1
    assert shinei.documents == [{
        "chat_id": "channel",
        "file_path": str(artifact),
        "metadata": {"thread_id": "thread-42"},
    }]
    assert coordinator.sent == []


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        ("spawned", None, "running"),
        ("heartbeat", {"note": "halfway"}, "halfway"),
        ("blocked", {"reason": "human input"}, "blocked"),
        ("dependency_wait", {"kind": "dependency", "reason": "parent"}, "waiting on"),
        ("gave_up", {"error": "spawn failed"}, "stopped"),
        ("crashed", None, "retrying"),
        ("timed_out", {"limit_seconds": 90}, "timed out"),
    ],
)
def test_cross_process_routes_each_execution_kind_to_run_profile(
    tmp_path, monkeypatch, kind, payload, expected,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / f"{kind}.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=kind, assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None and task.current_run_id is not None
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, kind, payload, run_id=task.current_run_id,
            )
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(
        monkeypatch, _make_discord_runner(coordinator, active_profile="default"),
    ))
    shinei = RecordingAdapter()
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(shinei, active_profile="shinei"),
    ))

    assert len(shinei.sent) == 1
    assert expected in shinei.sent[0]["text"].lower()
    assert coordinator.sent == []


def test_cross_process_artifact_retry_resumes_without_duplicate_text(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "artifact-retry.db"))
    monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="partial retry", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            thread_id="thread", notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(
            conn, task_id, summary="two artifacts",
            metadata={"artifacts": [str(first), str(second)]},
        )
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    shinei = PartialFailureArtifactAdapter(second)
    sender = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, text_delivered, next_artifact_index "
            "FROM kanban_role_deliveries"
        ).fetchone()
        assert tuple(row) == ("pending", 1, 1)
        conn.execute("UPDATE kanban_role_deliveries SET available_at = 0")
        conn.commit()
    finally:
        conn.close()

    # Progress indexes address the immutable staged manifest. Removing an
    # already-sent file and mutating live task state must not shift artifact 2.
    first.unlink()
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            ("result changed after staging", task_id),
        )
        conn.commit()
    finally:
        conn.close()

    sender._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))

    assert len(shinei.sent) == 1
    assert [item["file_path"] for item in shinei.documents] == [
        str(first), str(second),
    ]
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, text_delivered, next_artifact_index "
            "FROM kanban_role_deliveries"
        ).fetchone()
        assert tuple(row) == ("delivered", 1, 2)
    finally:
        conn.close()


def test_active_profile_discovery_fails_closed(monkeypatch):
    runner = _make_discord_runner(RecordingAdapter())
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "",
    )
    assert runner._active_profile_name() == ""

    def unavailable():
        raise OSError("profile store unavailable")

    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        unavailable,
    )
    assert runner._active_profile_name() == ""


def test_named_gateway_dispatcher_gate_precedes_config_and_db(monkeypatch):
    runner = _make_discord_runner(RecordingAdapter(), active_profile="shinei")
    runner._active_profile_name = lambda: "shinei"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("named gateway touched dispatcher config or DB")

    monkeypatch.setattr("hermes_cli.config.load_config", forbidden)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", forbidden)
    asyncio.run(runner._kanban_dispatcher_watcher())


def test_named_dispatcher_gate_uses_boot_captured_profile(monkeypatch):
    runner = _make_discord_runner(RecordingAdapter(), active_profile="shinei")
    # Runtime profile discovery must not be able to turn a named gateway into
    # the dispatcher after boot.
    runner._active_profile_name = lambda: "default"
    calls = []

    def record_config_load():
        calls.append("config")
        return {"kanban": {"dispatch_in_gateway": True}}

    def forbidden_db(*_args, **_kwargs):
        raise AssertionError("boot-captured named gateway touched dispatcher DB")

    monkeypatch.setattr("hermes_cli.config.load_config", record_config_load)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", forbidden_db)
    asyncio.run(runner._kanban_dispatcher_watcher())
    assert calls == []


def test_dispatcher_gate_fails_closed_when_boot_profile_is_unknown(monkeypatch):
    runner = _make_discord_runner(RecordingAdapter(), active_profile="")
    runner._active_profile_name = lambda: ""
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("touched")
        raise AssertionError("unknown boot identity touched dispatcher config or DB")

    monkeypatch.setattr("hermes_cli.config.load_config", forbidden)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", forbidden)
    asyncio.run(runner._kanban_dispatcher_watcher())
    assert calls == []


def test_producer_never_resolves_or_sends_through_foreign_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "producer-local-only.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="no foreign lookup", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="stage only")
    finally:
        conn.close()

    owner = _make_discord_runner(
        RecordingAdapter(),
        profile_adapters={"shinei": {Platform.DISCORD: RecordingAdapter()}},
        active_profile="default",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("producer performed role adapter lookup")

    owner._kanban_execution_adapter = forbidden
    asyncio.run(_run_one_notifier_tick(monkeypatch, owner))
    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT sender_profile, status FROM kanban_role_deliveries"
        ).fetchone()
        assert tuple(row) == ("shinei", "pending")
    finally:
        conn.close()


def test_role_sender_uses_only_local_adapter_map(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "local-only.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="local adapter", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="local adapter only")
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    shinei = RecordingAdapter()
    sender = _make_discord_runner(shinei, active_profile="shinei")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("role sender performed foreign adapter lookup")

    sender._kanban_execution_adapter = forbidden
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))
    assert len(shinei.sent) == 1


def test_legacy_event_without_run_id_is_not_staged_for_cross_process_send(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy-no-run.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "completed",
                {"summary": "must not cross process"},
                run_id=None,
            )
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    conn = kb.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_role_deliveries"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_accept_pre_ack_window_is_explicitly_at_least_once(
    tmp_path, monkeypatch,
):
    """A crash after Discord accepts text but before SQLite ack may duplicate."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "ack-window.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ack window", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="accepted before local ack")
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    shinei = RecordingAdapter()
    first_process = _make_discord_runner(shinei, active_profile="shinei")
    first_process._kanban_advance_role_delivery_progress = (
        lambda *_args, **_kwargs: False
    )
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, first_process))

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT status, text_delivered FROM kanban_role_deliveries"
        ).fetchone()
        assert tuple(row) == ("pending", 0)
        conn.execute("UPDATE kanban_role_deliveries SET available_at = 0")
        conn.commit()
    finally:
        conn.close()

    restarted = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, restarted))
    assert len(shinei.sent) == 2
    assert shinei.sent[0]["text"] == shinei.sent[1]["text"]


def test_role_sender_survives_transient_completion_db_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "complete-error.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="transient ack", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="ack after retry")
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    adapter = RecordingAdapter()
    sender = _make_discord_runner(adapter, active_profile="shinei")

    def fail_complete(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    sender._kanban_complete_role_delivery = fail_complete

    # A transient local acknowledgement failure must not terminate the
    # long-lived sender coroutine. The lease remains recoverable after expiry.
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_role_lease_maintenance_converts_transient_db_error_to_lost_signal(
    monkeypatch,
):
    runner = _make_discord_runner(RecordingAdapter(), active_profile="shinei")
    delivery = {"id": 1}
    stop = asyncio.Event()
    lost = asyncio.Event()

    def fail_renew(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    runner._kanban_renew_role_delivery_lease = fail_renew

    async def immediate_timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    await runner._kanban_maintain_role_delivery_lease(
        delivery, "token", None, stop, lost,
    )
    assert lost.is_set()


def test_unsubscribe_after_staging_does_not_drop_role_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "unsubscribe.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="durable unsubscribe", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        kb.claim_task(conn, task_id)
        kb.complete_task(conn, task_id, summary="already staged")
    finally:
        conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    conn = kb.connect()
    try:
        kb.remove_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
        )
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()

    shinei = RecordingAdapter()
    asyncio.run(_run_one_role_delivery_tick(
        monkeypatch, _make_discord_runner(shinei, active_profile="shinei"),
    ))
    assert len(shinei.sent) == 1
    assert "already staged" in shinei.sent[0]["text"]


def test_missing_run_profile_fails_closed_without_impersonating(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "missing-profile.db"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", tmp_path / "default")],
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="missing profile", assignee="shinei")
        kb.add_notify_sub(
            conn, task_id=task_id, platform="discord", chat_id="channel",
            notifier_profile="default",
        )
        # Completing without a claimed run intentionally produces no run profile.
        kb.complete_task(conn, task_id, summary="secret worker-authored body")
    finally:
        conn.close()

    coordinator = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(coordinator, active_profile="default"),
    ))
    conn = kb.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM kanban_role_deliveries"
        ).fetchone()[0] == 0
    finally:
        conn.close()
    assert all("secret worker-authored body" not in item["text"] for item in coordinator.sent)


def test_cross_process_delivery_scans_two_boards(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [
            ("default", tmp_path / "default"),
            ("shinei", tmp_path / "profiles" / "shinei"),
        ],
    )
    task_ids = []
    for board in ("alpha", "beta"):
        kb.init_db(board=board)
        kb.write_board_metadata(board, name=board.title())
        conn = kb.connect(board=board)
        try:
            task_id = kb.create_task(conn, title=f"{board} task", assignee="shinei")
            task_ids.append(task_id)
            kb.add_notify_sub(
                conn, task_id=task_id, platform="discord", chat_id="channel",
                notifier_profile="default",
            )
            kb.claim_task(conn, task_id)
            kb.complete_task(conn, task_id, summary=f"{board} complete")
        finally:
            conn.close()

    asyncio.run(_run_one_notifier_tick(
        monkeypatch,
        _make_discord_runner(RecordingAdapter(), active_profile="default"),
    ))
    shinei = RecordingAdapter()
    sender = _make_discord_runner(shinei, active_profile="shinei")
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))
    sender._running = True
    asyncio.run(_run_one_role_delivery_tick(monkeypatch, sender))
    assert len(shinei.sent) == 2
    assert all(any(task_id in item["text"] for item in shinei.sent) for task_id in task_ids)
