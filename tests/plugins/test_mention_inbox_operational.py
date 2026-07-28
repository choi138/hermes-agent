from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    MentionInboxConfig,
    MentionInboxGatewayService,
    MentionInboxRuntime,
    parse_mention_inbox_config,
    render_discord_event,
)

NOW = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1526407515313668247"


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
    assert parse_mention_inbox_config({}).enabled is False
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
    assert "event_json" not in audit


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
