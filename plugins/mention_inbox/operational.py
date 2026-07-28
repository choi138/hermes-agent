"""Operational GitHub-to-Discord mention inbox runtime.

The runtime is disabled by default. It performs GitHub GETs through the existing
poller and uses a durable SQLite outbox before posting deterministic, bounded
Discord views. Discord does not expose an idempotency key for ordinary channel
posts, so an expired send lease is reconciled against bounded bot-authored
channel history using the deterministic marker before any retry.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from plugins.mention_inbox.contract import MentionEvent
from plugins.mention_inbox.github_client import GitHubNotificationsClient
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.runtime import GitHubMentionPoller
from plugins.mention_inbox.store import DEFAULT_DESTINATION, MentionInboxStore

_ALLOWED_REPOSITORY = "silviahealth/content"
_ALLOWED_ENV = "GITHUB_PAT_TOKEN"
_COLLECTOR_KEY = "github.notifications"
_ALLOWED_MENTIONS_NONE: dict[str, Any] = {
    "parse": [], "users": [], "roles": [], "replied_user": False,
}


@dataclass(frozen=True)
class MentionInboxConfig:
    enabled: bool = False
    credential_env: str = _ALLOWED_ENV
    repositories: tuple[str, ...] = (_ALLOWED_REPOSITORY,)
    destination: str = DEFAULT_DESTINATION
    retention_days: int = 30
    lease_seconds: int = 60


@dataclass(frozen=True)
class RenderedDiscordEvent:
    content: str
    marker: str
    allowed_mentions: dict[str, Any]


class DiscordDeliveryTransport(Protocol):
    async def find_marker(self, channel_id: str, marker: str, *, limit: int) -> str | None: ...
    async def send(self, channel_id: str, content: str, *, allowed_mentions: dict[str, Any]) -> str: ...


def _positive_int(value: object, name: str, default: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"mention_inbox.{name} must be a positive integer")
    return value


def parse_mention_inbox_config(config: Mapping[str, Any]) -> MentionInboxConfig:
    raw = config.get("mention_inbox", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("mention_inbox must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("mention_inbox.enabled must be a boolean")
    credential_env = raw.get("credential_env", _ALLOWED_ENV)
    repositories = raw.get("repositories", [_ALLOWED_REPOSITORY])
    destination = raw.get("destination", DEFAULT_DESTINATION)
    if credential_env != _ALLOWED_ENV:
        raise ValueError("mention_inbox.credential_env is not allowed")
    if not isinstance(repositories, list) or not repositories or any(
        not isinstance(item, str) for item in repositories
    ):
        raise ValueError("mention_inbox.repositories must be a non-empty string list")
    if set(repositories) != {_ALLOWED_REPOSITORY}:
        raise ValueError("mention_inbox.repositories contains a repository outside the allowlist")
    if not isinstance(destination, str) or re.fullmatch(r"discord:[1-9][0-9]{5,24}", destination) is None:
        raise ValueError("mention_inbox.destination must be a Discord channel destination")
    return MentionInboxConfig(
        enabled=enabled,
        credential_env=credential_env,
        repositories=tuple(repositories),
        destination=destination,
        retention_days=_positive_int(raw.get("retention_days"), "retention_days", 30),
        lease_seconds=_positive_int(raw.get("lease_seconds"), "lease_seconds", 60),
    )


def _neutralize(value: str, limit: int) -> str:
    value = value.replace("@", "@\u200b")
    value = re.sub(r"([\\`*_{}\[\]()<>#+\-.!|])", r"\\\1", value)
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _marker(event: MentionEvent, revision_number: int, destination: str) -> str:
    import hashlib
    identity = f"{event.dedupe_key}\0{revision_number}\0{destination}"
    return "[hermes-inbox:" + hashlib.sha256(identity.encode()).hexdigest()[:24] + "]"


def render_discord_event(
    event: MentionEvent, *, revision_number: int, destination: str
) -> RenderedDiscordEvent:
    metadata = event.untrusted.metadata
    repository = metadata.get("repository") if isinstance(metadata, Mapping) else None
    repository = repository if isinstance(repository, str) else "unknown"
    marker = _marker(event, revision_number, destination)
    lines = [
        "GitHub mention inbox",
        f"Source: GitHub",
        f"Repository: {_neutralize(repository, 160)}",
        f"Requested action: {event.requested_action.value}",
        f"Title (untrusted data): {_neutralize(event.untrusted.title, 400)}",
        f"Body preview (untrusted data): {_neutralize(event.untrusted.body, 900)}",
    ]
    if event.untrusted.source_url:
        safe_url = event.untrusted.source_url.replace("@", "@\u200b")[:300]
        lines.append(f"Source URL: {safe_url}")
    lines.append(marker)
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[: 1900 - len(marker) - 2].rstrip() + "\n" + marker
    return RenderedDiscordEvent(
        content=content,
        marker=marker,
        allowed_mentions=dict(_ALLOWED_MENTIONS_NONE),
    )


class DiscordMentionDelivery:
    def __init__(self, *, store: MentionInboxStore, discord: DiscordDeliveryTransport,
                 destination: str, lease_seconds: int) -> None:
        self._store = store
        self._discord = discord
        self._destination = destination
        self._channel_id = destination.split(":", 1)[1]
        self._lease_seconds = lease_seconds

    async def deliver_once(self) -> str:
        claim = self._store.claim_delivery(
            self._destination, lease_seconds=self._lease_seconds
        )
        if claim is None:
            return "idle"
        rendered = render_discord_event(
            claim.event,
            revision_number=claim.revision_number,
            destination=claim.destination,
        )
        try:
            if claim.requires_reconciliation:
                existing = await self._discord.find_marker(
                    self._channel_id, claim.marker, limit=100
                )
                if existing is not None:
                    self._store.mark_delivery_sent(claim.delivery_id, message_id=existing)
                    return "reconciled"
            message_id = await self._discord.send(
                self._channel_id,
                rendered.content,
                allowed_mentions=rendered.allowed_mentions,
            )
        except Exception:
            self._store.release_delivery(
                claim.delivery_id, error_category="discord_send"
            )
            return "error"
        self._store.mark_delivery_sent(claim.delivery_id, message_id=message_id)
        return "sent"


Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class MentionInboxRuntime:
    """Cancellation-safe singleton poll/delivery loop for one profile DB."""
    def __init__(self, *, config: MentionInboxConfig, store: MentionInboxStore,
                 poller: Any, delivery: Any, clock: Clock | None = None,
                 sleep: Sleep = asyncio.sleep) -> None:
        self.config = config
        self.store = store
        self.poller = poller
        self.delivery = delivery
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="mention-inbox-runtime")
        return self._task

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _drain_delivery(self) -> None:
        for _ in range(100):
            result = await self.delivery.deliver_once()
            if result in {"idle", "error"}:
                return

    async def _run(self) -> None:
        while True:
            await self._drain_delivery()
            status = self.store.get_collector_status(_COLLECTOR_KEY)
            now = self._clock().astimezone(timezone.utc)
            if status is not None and status.next_poll_at > now:
                await self._sleep((status.next_poll_at - now).total_seconds())
                continue
            await asyncio.to_thread(self.poller.poll_once)
            await self._drain_delivery()
            await asyncio.to_thread(
                self.store.prune, retention_days=self.config.retention_days
            )

    def health(self) -> dict[str, object]:
        return self.store.health(_COLLECTOR_KEY)


class GatewayDiscordTransport:
    """Narrow Discord adapter bridge with bot-only history reconciliation."""
    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def _channel(self, channel_id: str) -> Any:
        client = self._adapter._client
        channel = client.get_channel(int(channel_id))
        return channel or await client.fetch_channel(int(channel_id))

    async def find_marker(self, channel_id: str, marker: str, *, limit: int) -> str | None:
        channel = await self._channel(channel_id)
        bot_id = getattr(getattr(self._adapter._client, "user", None), "id", None)
        async for message in channel.history(limit=limit):
            if getattr(getattr(message, "author", None), "id", None) == bot_id and marker in str(getattr(message, "content", "")):
                return str(message.id)
        return None

    async def send(self, channel_id: str, content: str, *, allowed_mentions: dict[str, Any]) -> str:
        result = await self._adapter.send(
            channel_id, content,
            metadata={"nonconversational": True, "mention_inbox_no_mentions": True},
        )
        if not result.success or not result.message_id:
            raise RuntimeError("discord_send_failed")
        return str(result.message_id)


class _LazyGitHubNotificationCollector:
    """Resolve the authenticated stable target ID inside poller error handling."""
    def __init__(self, client: GitHubNotificationsClient, repositories: tuple[str, ...]) -> None:
        self._client = client
        self._repositories = repositories
        self._collector = GitHubNotificationCollector(
            target_id="github:authenticated-user",
            allowed_repositories=repositories,
        )
        self._resolved = False

    def accepts(self, notification: Mapping[str, Any]) -> bool:
        return self._collector.accepts(notification)

    def normalize(self, notification: Mapping[str, Any], detail: Mapping[str, Any] | None):
        if not self._resolved:
            target_id = self._client.get_authenticated_user_id()
            self._collector = GitHubNotificationCollector(
                target_id=target_id,
                allowed_repositories=self._repositories,
            )
            self._resolved = True
        return self._collector.normalize(notification, detail)


class MentionInboxGatewayService:
    """Fail-isolated launcher; only this class reads the named credential env."""
    def __init__(self, config: MentionInboxConfig, discord_adapter: Any,
                 *, environ: Mapping[str, str] | None = None,
                 db_path: Path | None = None) -> None:
        self.config = config
        self._discord_adapter = discord_adapter
        self._environ = environ
        self._db_path = db_path
        self._runtime: MentionInboxRuntime | None = None
        self._degraded_category: str | None = "disabled" if not config.enabled else None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._environ is None:
            from agent.secret_scope import get_secret
            token = get_secret(self.config.credential_env)
        else:
            token = self._environ.get(self.config.credential_env)
        if not token:
            self._degraded_category = "missing_credential"
            return
        if self._discord_adapter is None:
            self._degraded_category = "discord_unavailable"
            return
        try:
            client = GitHubNotificationsClient(token=token)
            store = MentionInboxStore(
                self._db_path,
                delivery_destinations=(self.config.destination,),
            )
            collector = _LazyGitHubNotificationCollector(
                client,
                self.config.repositories,
            )
            poller = GitHubMentionPoller(client=client, collector=collector, store=store)
            delivery = DiscordMentionDelivery(
                store=store,
                discord=GatewayDiscordTransport(self._discord_adapter),
                destination=self.config.destination,
                lease_seconds=self.config.lease_seconds,
            )
            self._runtime = MentionInboxRuntime(
                config=self.config, store=store, poller=poller, delivery=delivery
            )
            self._runtime.start()
            self._degraded_category = None
        except Exception:
            self._degraded_category = "startup_failed"

    async def stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop()

    def health(self) -> dict[str, object]:
        if self._runtime is not None:
            return self._runtime.health()
        return {
            "status": "disabled" if self._degraded_category == "disabled" else "degraded",
            "error_category": self._degraded_category,
            "last_attempt_at": None,
            "last_success_at": None,
            "next_poll_at": None,
            "consecutive_failures": 0,
            "pending_delivery_count": 0,
        }
