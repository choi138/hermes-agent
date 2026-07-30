from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.contract import event_to_json
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    GatewayDiscordTransport,
    MentionInboxConfig,
    MentionInboxGatewayService,
    MentionInboxRuntime,
    parse_mention_inbox_config,
    render_discord_event,
)

NOW = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1531851208858275860"
WORK_INBOX_DESTINATION = DESTINATION


def _event(*, title: str = "Review requested", body: str = "Please review", event_id: str = "n1"):
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": event_id},
        "actor": {"actor_id": "github:actor", "kind": "user"},
        "target": {"target_id": "github:me", "kind": "user"},
        "thread": {"thread_id": "github:thread", "container_id": "repo-node"},
        "requested_action": "review",
        "deadline": None,
        "untrusted": {
            "title": title,
            "body": body,
            "action_detail": "review_requested",
            "source_url": "https://github.com/silviahealth/content/pull/7",
            "metadata": {"repository": "silviahealth/content"},
        },
    })


def _enabled_config() -> MentionInboxConfig:
    return MentionInboxConfig(
        enabled=True,
        credential_env="GITHUB_PAT_TOKEN",
        repositories=("silviahealth/content",),
        destination=DESTINATION,
        retention_days=30,
        lease_seconds=60,
    )


def test_config_defaults_disabled_and_validates_fail_closed() -> None:
    disabled = parse_mention_inbox_config({})
    assert disabled.enabled is False
    assert disabled.destination == WORK_INBOX_DESTINATION
    assert disabled.team_mentions is False
    assert disabled.team_review_requests is False
    config = parse_mention_inbox_config({"mention_inbox": {
        "enabled": True,
        "credential_env": "GITHUB_PAT_TOKEN",
        "repositories": ["silviahealth/content"],
        "destination": DESTINATION,
        "retention_days": 30,
    }})
    assert config == _enabled_config()
    for invalid in (
        {"enabled": "true"},
        {"enabled": True, "credential_env": "TOKEN"},
        {"enabled": True, "repositories": ["other/repo"]},
        {"enabled": True, "destination": "discord:not-a-channel"},
        {"enabled": True, "retention_days": 0},
    ):
        with pytest.raises(ValueError):
            parse_mention_inbox_config({"mention_inbox": invalid})


def test_renderer_bounds_untrusted_text_escapes_mentions_and_has_marker() -> None:
    event = _event(
        title="@everyone **title** " + "x" * 1000,
        body="<@123> @here [click](https://evil.example) " + "y" * 5000,
    )
    rendered = render_discord_event(event, revision_number=4, destination=DESTINATION)
    assert len(rendered.content) <= 1900
    assert "@everyone" not in rendered.content
    assert "@here" not in rendered.content
    assert "<@123>" not in rendered.content
    assert "silviahealth/content" in rendered.content
    assert "Requested action: review" in rendered.content
    assert "https://github.com/silviahealth/content/pull/7" in rendered.content
    assert rendered.marker.startswith("[hermes-inbox:")
    assert rendered.marker in rendered.content
    assert rendered.allowed_mentions == {"parse": [], "users": [], "roles": [], "replied_user": False}


def test_outbox_first_send_same_revision_retry_and_changed_revision(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    event = _event()
    first = store.upsert(event, source_revision="2026-07-29T08:00:00Z")
    assert first.created
    assert store.pending_delivery_count() == 1
    claimed = store.claim_delivery(DESTINATION, lease_seconds=60)
    assert claimed is not None and claimed.revision_number == 1
    store.release_delivery(claimed.delivery_id, error_category="discord_send")
    retry = store.claim_delivery(DESTINATION, lease_seconds=60)
    assert retry is not None and retry.delivery_id == claimed.delivery_id
    store.mark_delivery_sent(retry.delivery_id, message_id="m1")
    assert store.pending_delivery_count() == 0
    same = store.upsert(event, source_revision="2026-07-29T08:05:00Z")
    assert not same.content_changed
    assert store.pending_delivery_count() == 0
    changed = store.upsert(_event(body="changed"), source_revision="2026-07-29T08:10:00Z")
    assert changed.revision_number == 2
    assert store.pending_delivery_count() == 1


def test_concurrent_claim_and_restart_use_single_durable_delivery(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    first = MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60)
    second = MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60)
    assert first is not None
    assert second is None
    MentionInboxStore(db, clock=lambda: NOW).mark_delivery_sent(first.delivery_id, message_id="m1")
    assert MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60) is None


def test_expired_uncertain_lease_requires_reconciliation(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    now[0] += timedelta(seconds=11)
    recovered = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert recovered is not None
    assert recovered.delivery_id == claim.delivery_id
    assert recovered.requires_reconciliation is True


class _Discord:
    def __init__(self, *, history: list[dict[str, str]] | None = None, fail: bool = False):
        self.history = history or []
        self.fail = fail
        self.sends: list[tuple[str, str, dict[str, Any]]] = []

    async def find_marker(self, channel_id: str, marker: str, *, limit: int) -> str | None:
        for item in self.history[:limit]:
            if marker in item["content"]:
                return item["id"]
        return None

    async def send(self, channel_id: str, content: str, *, allowed_mentions: dict[str, Any]) -> str:
        self.sends.append((channel_id, content, allowed_mentions))
        if self.fail:
            raise RuntimeError("private discord failure body")
        return "message-1"


@pytest.mark.asyncio
async def test_uncertain_send_reconciles_marker_without_duplicate(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    original = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert original is not None
    rendered = render_discord_event(original.event, revision_number=1, destination=DESTINATION)
    now[0] += timedelta(seconds=11)
    discord = _Discord(history=[{"id": "existing", "content": rendered.content}])
    delivery = DiscordMentionDelivery(store=store, discord=discord, destination=DESTINATION, lease_seconds=10)
    assert await delivery.deliver_once() == "reconciled"
    assert discord.sends == []
    assert store.pending_delivery_count() == 0


class _GatewayAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any]]] = []

    async def send(
        self,
        thread_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any],
    ) -> SimpleNamespace:
        self.calls.append((thread_id, content, reply_to, metadata))
        return SimpleNamespace(success=True, message_id=f"message-{len(self.calls)}")


@pytest.mark.asyncio
async def test_gateway_transport_correlates_notice_to_inbound_message() -> None:
    adapter = _GatewayAdapter()
    transport = GatewayDiscordTransport(adapter)

    message_id = await transport.send_to_thread(
        "thread-1",
        "notice",
        reply_to_message_id="user-message-1",
    )

    assert message_id == "message-1"
    assert adapter.calls == [
        (
            "thread-1",
            "notice",
            "user-message-1",
            {
                "thread_id": "thread-1",
                "nonconversational": True,
                "mention_inbox_no_mentions": True,
                "notify": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_discord_failure_releases_claim_secret_safely(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    discord = _Discord(fail=True)
    delivery = DiscordMentionDelivery(store=store, discord=discord, destination=DESTINATION, lease_seconds=10)
    assert await delivery.deliver_once() == "error"
    health = store.health("github.notifications")
    assert health["pending_delivery_count"] == 1
    assert "private discord failure body" not in str(health)


class _Poller:
    def __init__(self, store: MentionInboxStore):
        self.store = store
        self.calls = 0

    def poll_once(self):
        self.calls += 1
        self.store.record_poll_success("github.notifications", next_poll_at=NOW + timedelta(hours=1))


class _Delivery:
    def __init__(self):
        self.calls = 0

    async def deliver_once(self):
        self.calls += 1
        return "idle"


@pytest.mark.asyncio
async def test_runtime_singleton_restores_schedule_and_cancels(tmp_path: Path) -> None:
    now = [NOW]
    sleeps: list[float] = []
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.record_poll_success("github.notifications", next_poll_at=NOW + timedelta(seconds=40))
    poller = _Poller(store)
    delivery = _Delivery()
    unblock = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await unblock.wait()

    runtime = MentionInboxRuntime(config=_enabled_config(), store=store, poller=poller, delivery=delivery, clock=lambda: now[0], sleep=sleep)
    first = runtime.start()
    second = runtime.start()
    assert first is second
    await asyncio.sleep(0)
    assert sleeps == [40.0]
    assert poller.calls == 0
    await runtime.stop()
    assert first.cancelled()


def test_retention_prunes_payload_but_preserves_delivery_audit(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    store.mark_delivery_sent(claim.delivery_id, message_id="m1")
    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    assert store.get(_event().dedupe_key) is None
    audit = store.delivery_audit(claim.delivery_id)
    assert audit is not None
    assert audit["status"] == "sent"
    assert audit["marker"] == claim.marker
    assert audit["message_id"] == "m1"
    assert audit["revision_number"] == 1
    assert "event_json" not in audit
    connection = sqlite3.connect(tmp_path / "inbox.db")
    assert connection.execute(
        "SELECT event_json FROM delivery_outbox WHERE delivery_id = ?",
        (claim.delivery_id,),
    ).fetchone()[0] is None
    assert connection.execute(
        "SELECT latest_revision, latest_source_revision FROM mention_event_lineage "
        "WHERE dedupe_key = ?",
        (_event().dedupe_key,),
    ).fetchone() == (1, "2026-07-29T08:00:00Z")
    connection.close()


def test_outbox_claims_each_queued_revision_from_immutable_snapshot(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    original = _event(body="revision one")
    revised = _event(body="revision two")

    store.upsert(original, source_revision="2026-07-29T08:00:00Z")
    store.upsert(revised, source_revision="2026-07-29T08:05:00Z")

    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first is not None
    assert first.revision_number == 1
    assert first.event.untrusted.body == "revision one"
    store.mark_delivery_sent(first.delivery_id, message_id="m1")

    second = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert second is not None
    assert second.revision_number == 2
    assert second.event.untrusted.body == "revision two"
    assert second.delivery_id != first.delivery_id
    store.mark_delivery_sent(second.delivery_id, message_id="m2")
    assert store.claim_delivery(DESTINATION, lease_seconds=10) is None


def test_prune_then_same_source_revision_is_noop(tmp_path: Path) -> None:
    now = [NOW]
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: now[0])
    source_revision = "2026-07-29T08:00:00Z"
    original = _event(body="original")
    first_result = store.upsert(original, source_revision=source_revision)
    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first_result.revision_number == 1
    assert first is not None
    store.mark_delivery_sent(first.delivery_id, message_id="m1")

    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    same_revision = store.upsert(
        _event(body="changed body must not be trusted"),
        source_revision=source_revision,
    )

    assert same_revision.created is False
    assert same_revision.content_changed is False
    assert same_revision.stale is True
    assert same_revision.revision_number == 1
    assert store.get(original.dedupe_key) is None
    assert store.pending_delivery_count() == 0
    assert store.claim_delivery(DESTINATION, lease_seconds=10) is None
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 1
    assert connection.execute(
        "SELECT latest_revision, latest_source_revision FROM mention_event_lineage "
        "WHERE dedupe_key = ?",
        (original.dedupe_key,),
    ).fetchone() == (1, source_revision)
    connection.close()


def test_prune_then_newer_source_revision_uses_monotonic_delivery_identity(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    original = _event(body="original")
    first_result = store.upsert(original, source_revision="2026-07-29T08:00:00Z")
    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first_result.revision_number == 1
    assert first is not None
    store.mark_delivery_sent(first.delivery_id, message_id="m1")
    first_audit = store.delivery_audit(first.delivery_id)

    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    reingested = store.upsert(
        _event(body="new after retention"),
        source_revision="2026-08-29T08:00:00Z",
    )

    assert reingested.created is True
    assert reingested.revision_number == 2
    assert store.pending_delivery_count() == 1
    assert store.delivery_audit(first.delivery_id) == first_audit
    second = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert second is not None
    assert second.delivery_id != first.delivery_id
    assert second.revision_number == 2
    assert second.marker != first.marker
    assert second.event.untrusted.body == "new after retention"


def test_pre_snapshot_schema_migrates_idempotently_without_losing_delivery_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "inbox.db"
    pending = _event(event_id="pending", body="pending payload")
    sending = _event(event_id="sending", body="sending payload")
    sent = _event(event_id="sent", body="sent payload")
    timestamp = "2026-07-29T08:00:00Z"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE mention_events (
            dedupe_key TEXT PRIMARY KEY,
            source_platform TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            event_json TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE collector_cursors (
            collector_key TEXT PRIMARY KEY,
            cursor_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delivery_outbox (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            destination TEXT NOT NULL,
            marker TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            lease_until TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(dedupe_key, revision_number, destination)
        );
    """)
    for event in (pending, sending, sent):
        connection.execute(
            "INSERT INTO mention_events VALUES (?, 'github', ?, ?, 'hash', ?, 1, ?, ?)",
            (
                event.dedupe_key,
                event.source.event_id,
                timestamp,
                event_to_json(event),
                timestamp,
                timestamp,
            ),
        )
    connection.execute(
        "INSERT INTO collector_cursors VALUES ('github.notifications', 'cursor-1', ?)",
        (timestamp,),
    )
    rows = (
        (pending.dedupe_key, "pending", 0, None, None),
        (sending.dedupe_key, "sending", 2, None, "2026-07-29T10:00:00Z"),
        (sent.dedupe_key, "sent", 1, "message-sent", None),
    )
    for dedupe_key, status, attempts, message_id, lease_until in rows:
        connection.execute(
            """INSERT INTO delivery_outbox (
                dedupe_key, revision_number, destination, marker, status, attempts,
                message_id, lease_until, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedupe_key,
                DESTINATION,
                f"marker-{status}",
                status,
                attempts,
                message_id,
                lease_until,
                timestamp,
                timestamp,
            ),
        )
    connection.commit()
    connection.close()

    store = MentionInboxStore(db, clock=lambda: NOW)
    MentionInboxStore(db, clock=lambda: NOW)

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(delivery_outbox)")}
    assert "event_json" in columns
    assert "source_revision" in columns
    migrated = connection.execute(
        "SELECT status, attempts, message_id, lease_until, event_json, source_revision "
        "FROM delivery_outbox "
        "ORDER BY delivery_id"
    ).fetchall()
    assert [(row["status"], row["attempts"]) for row in migrated] == [
        ("pending", 0),
        ("sending", 2),
        ("sent", 1),
    ]
    assert migrated[1]["lease_until"] == "2026-07-29T10:00:00Z"
    assert migrated[2]["message_id"] == "message-sent"
    assert all(row["event_json"] is not None for row in migrated)
    assert all(row["source_revision"] == timestamp for row in migrated)
    assert connection.execute("SELECT COUNT(*) FROM mention_event_lineage").fetchone()[0] == 3
    connection.close()
    assert store.get_cursor("github.notifications") == "cursor-1"
    assert store.pending_delivery_count() == 2
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    assert claim.event.untrusted.body == "pending payload"
    assert claim.source_revision == timestamp


def test_future_schema_version_fails_closed_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE future_data (value TEXT NOT NULL);
        INSERT INTO future_data VALUES ('preserve-me');
        PRAGMA user_version = 99;
    """)
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported schema version 6"):
        MentionInboxStore(db, clock=lambda: NOW)

    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    assert connection.execute("SELECT value FROM future_data").fetchall() == [("preserve-me",)]
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall() == [("future_data",)]
    connection.close()


def test_migration_supersedes_unreconstructible_older_revision(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    revision_two = _event(body="revision two")
    timestamp = "2026-07-29T08:00:00Z"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE mention_events (
            dedupe_key TEXT PRIMARY KEY,
            source_platform TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            event_json TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE collector_cursors (
            collector_key TEXT PRIMARY KEY,
            cursor_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delivery_outbox (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            destination TEXT NOT NULL,
            marker TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            lease_until TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(dedupe_key, revision_number, destination)
        );
        PRAGMA user_version = 1;
    """)
    connection.execute(
        "INSERT INTO mention_events VALUES (?, 'github', 'n1', ?, 'hash', ?, 2, ?, ?)",
        (
            revision_two.dedupe_key,
            "2026-07-29T08:05:00Z",
            event_to_json(revision_two),
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        "INSERT INTO collector_cursors VALUES ('github.notifications', 'cursor-2', ?)",
        (timestamp,),
    )
    connection.execute(
        """INSERT INTO delivery_outbox (
            dedupe_key, revision_number, destination, marker, status, attempts,
            lease_until, created_at, updated_at
        ) VALUES (?, 1, ?, 'marker-rev1', 'sending', 3, ?, ?, ?)""",
        (revision_two.dedupe_key, DESTINATION, "2026-07-29T10:00:00Z", timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO delivery_outbox (
            dedupe_key, revision_number, destination, marker, status, attempts,
            created_at, updated_at
        ) VALUES (?, 2, ?, 'marker-rev2', 'pending', 0, ?, ?)""",
        (revision_two.dedupe_key, DESTINATION, timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    store = MentionInboxStore(db, clock=lambda: NOW)
    MentionInboxStore(db, clock=lambda: NOW)

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT revision_number, marker, status, attempts, lease_until, event_json "
        "FROM delivery_outbox ORDER BY revision_number"
    ).fetchall()
    assert dict(rows[0]) == {
        "revision_number": 1,
        "marker": "marker-rev1",
        "status": "superseded",
        "attempts": 3,
        "lease_until": None,
        "event_json": None,
    }
    assert rows[1]["status"] == "pending"
    assert rows[1]["event_json"] == event_to_json(revision_two)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    connection.close()
    assert store.get_cursor("github.notifications") == "cursor-2"
    assert store.pending_delivery_count() == 1
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    assert claim.revision_number == 2
    assert claim.source_revision == "2026-07-29T08:05:00Z"
    assert claim.event.untrusted.body == "revision two"


@pytest.mark.asyncio
async def test_normal_send_posts_once_with_mentions_disabled(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    discord = _Discord()
    delivery = DiscordMentionDelivery(
        store=store, discord=discord, destination=DESTINATION, lease_seconds=10
    )
    assert await delivery.deliver_once() == "sent"
    assert await delivery.deliver_once() == "idle"
    restarted = DiscordMentionDelivery(
        store=MentionInboxStore(db, clock=lambda: NOW), discord=discord,
        destination=DESTINATION, lease_seconds=10,
    )
    assert await restarted.deliver_once() == "idle"
    assert len(discord.sends) == 1
    assert discord.sends[0][2]["parse"] == []


@pytest.mark.asyncio
async def test_disabled_and_missing_credential_are_degraded_not_fatal(tmp_path: Path) -> None:
    disabled = MentionInboxGatewayService(
        MentionInboxConfig(), None, environ={}, db_path=tmp_path / "disabled.db"
    )
    await disabled.start()
    assert disabled.health()["status"] == "disabled"
    missing = MentionInboxGatewayService(
        _enabled_config(), object(), environ={}, db_path=tmp_path / "missing.db"
    )
    await missing.start()
    assert missing.health()["status"] == "degraded"
    assert missing.health()["error_category"] == "missing_credential"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_enabled", "expected_handler"),
    ((False, False), (True, True)),
)
async def test_execution_handler_is_wired_only_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_enabled: bool,
    expected_handler: bool,
) -> None:
    import plugins.mention_inbox.operational as operational

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None
            self.execution_observer = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router

        def set_mention_inbox_execution_observer(self, observer) -> None:
            self.execution_observer = observer

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    adapter = Adapter()
    config = MentionInboxConfig(
        enabled=True,
        repositories=("silviahealth/content",),
        destination=DESTINATION,
        action_sessions_enabled=True,
        proposal_bot_mention="<@1525050677381279865>",
        authorized_approver_ids=("396159160201658368",),
        execution_enabled=execution_enabled,
        execution_mode="kanban",
    )
    service = MentionInboxGatewayService(
        config,
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / f"wired-{execution_enabled}.db",
    )

    await service.start()

    assert adapter.router is not None
    assert (adapter.router._approval_handler is not None) is expected_handler
    assert (adapter.execution_observer is not None) is execution_enabled
    assert service._runtime is not None
    coordinator = service._runtime.delivery._thread_coordinator
    assert coordinator._executor_hint == "kanban"
    assert coordinator._approval_available is expected_handler


def test_collector_bounds_external_title_body_before_persistence() -> None:
    collector = GitHubNotificationCollector(
        target_id="github:me", allowed_repositories={"silviahealth/content"}
    )
    notification = {
        "id": "n1", "reason": "mention", "unread": True,
        "updated_at": "2026-07-29T08:00:00Z",
        "subject": {"title": "t" * 5000, "type": "Issue", "url": None},
        "repository": {
            "node_id": "repo-node", "full_name": "silviahealth/content"
        },
    }
    detail = {
        "node_id": "issue-node", "body": "b" * 50000,
        "html_url": "https://github.com/silviahealth/content/issues/1",
        "user": {"node_id": "actor", "type": "User"},
    }
    collected = collector.normalize(notification, detail)
    assert collected is not None
    assert len(collected.event.untrusted.title) <= 500
    assert len(collected.event.untrusted.body) <= 4000
