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

from plugins.mention_inbox.actionable import GitHubHydrationContext
from plugins.mention_inbox.approval import (
    ApprovalHandler,
    ExecutionLifecycleObserver,
    GatewayExecutionDispatcher,
    GitHubSubjectStateResolver,
)
from plugins.mention_inbox.github_client import GitHubNotificationsClient
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.runtime import GitHubMentionPoller
from plugins.mention_inbox.store import DEFAULT_DESTINATION, MentionInboxStore
from plugins.mention_inbox.voice import RenderedDiscordEvent, render_action_alert

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
    team_mentions: bool = False
    team_review_requests: bool = False
    action_sessions_enabled: bool = False
    proposal_bot_mention: str | None = None
    authorized_approver_ids: tuple[str, ...] = ()
    thread_auto_archive_minutes: int = 1440
    execution_enabled: bool = False
    execution_mode: str = "direct"


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
    team_mentions = raw.get("team_mentions", False)
    team_review_requests = raw.get("team_review_requests", False)
    if not isinstance(team_mentions, bool) or not isinstance(team_review_requests, bool):
        raise ValueError("mention_inbox team switches must be booleans")
    action_sessions = raw.get("action_sessions", {})
    if action_sessions is None:
        action_sessions = {}
    if not isinstance(action_sessions, Mapping):
        raise ValueError("mention_inbox.action_sessions must be an object")
    action_sessions_enabled = action_sessions.get("enabled", False)
    execution_enabled = action_sessions.get("execution_enabled", False)
    if not isinstance(action_sessions_enabled, bool) or not isinstance(execution_enabled, bool):
        raise ValueError("mention_inbox action-session switches must be booleans")
    bot_mention = action_sessions.get("bot_mention")
    if bot_mention is not None and (
        not isinstance(bot_mention, str)
        or re.fullmatch(r"<@[1-9][0-9]{5,24}>", bot_mention) is None
    ):
        raise ValueError("mention_inbox.action_sessions.bot_mention is invalid")
    approver_ids = action_sessions.get("authorized_approver_ids", [])
    if not isinstance(approver_ids, list) or any(
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{5,24}", value) is None
        for value in approver_ids
    ):
        raise ValueError("mention_inbox.action_sessions.authorized_approver_ids is invalid")
    if len(set(approver_ids)) != len(approver_ids):
        raise ValueError("mention_inbox action-session approver IDs must be unique")
    archive_minutes = _positive_int(
        action_sessions.get("thread_auto_archive_minutes"),
        "action_sessions.thread_auto_archive_minutes",
        1440,
    )
    if archive_minutes not in {60, 1440, 4320, 10080}:
        raise ValueError("mention_inbox action-session archive duration is unsupported")
    execution_mode = action_sessions.get("execution_mode", "direct")
    if execution_mode not in {"direct", "kanban"}:
        raise ValueError("mention_inbox action-session execution_mode is invalid")
    if action_sessions_enabled and (bot_mention is None or not approver_ids):
        raise ValueError("enabled action sessions require bot mention and approvers")
    if execution_enabled and not action_sessions_enabled:
        raise ValueError("execution cannot be enabled while action sessions are disabled")
    return MentionInboxConfig(
        enabled=enabled,
        credential_env=credential_env,
        repositories=tuple(repositories),
        destination=destination,
        retention_days=_positive_int(raw.get("retention_days"), "retention_days", 30),
        lease_seconds=_positive_int(raw.get("lease_seconds"), "lease_seconds", 60),
        team_mentions=team_mentions,
        team_review_requests=team_review_requests,
        action_sessions_enabled=action_sessions_enabled,
        proposal_bot_mention=bot_mention,
        authorized_approver_ids=tuple(approver_ids),
        thread_auto_archive_minutes=archive_minutes,
        execution_enabled=execution_enabled,
        execution_mode=execution_mode,
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
    if isinstance(metadata, Mapping) and isinstance(
        metadata.get("actionable_kind"), str
    ):
        return render_action_alert(
            event,
            revision_number=revision_number,
            destination=destination,
        )
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
    def __init__(
        self,
        *,
        store: MentionInboxStore,
        discord: DiscordDeliveryTransport,
        destination: str,
        lease_seconds: int,
        thread_coordinator: Any | None = None,
    ) -> None:
        self._store = store
        self._discord = discord
        self._destination = destination
        self._channel_id = destination.split(":", 1)[1]
        self._lease_seconds = lease_seconds
        self._thread_coordinator = thread_coordinator

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
        confirmed_message_id = claim.message_id
        try:
            if confirmed_message_id is not None:
                if self._thread_coordinator is not None:
                    await self._thread_coordinator.ensure_thread(
                        claim.event,
                        parent_message_id=confirmed_message_id,
                        source_revision=claim.source_revision,
                    )
                self._store.mark_delivery_sent(
                    claim.delivery_id, message_id=confirmed_message_id
                )
                return "reconciled"
            if claim.requires_reconciliation:
                existing = await self._discord.find_marker(
                    self._channel_id, claim.marker, limit=100
                )
                if existing is not None:
                    confirmed_message_id = existing
                    self._store.mark_delivery_parent_confirmed(
                        claim.delivery_id, message_id=existing
                    )
                    if self._thread_coordinator is not None:
                        await self._thread_coordinator.ensure_thread(
                            claim.event,
                            parent_message_id=existing,
                            source_revision=claim.source_revision,
                        )
                    self._store.mark_delivery_sent(
                        claim.delivery_id, message_id=existing
                    )
                    return "reconciled"
            message_id = await self._discord.send(
                self._channel_id,
                rendered.content,
                allowed_mentions=rendered.allowed_mentions,
            )
            confirmed_message_id = message_id
            self._store.mark_delivery_parent_confirmed(
                claim.delivery_id, message_id=message_id
            )
            if self._thread_coordinator is not None:
                await self._thread_coordinator.ensure_thread(
                    claim.event,
                    parent_message_id=message_id,
                    source_revision=claim.source_revision,
                )
        except Exception:
            if confirmed_message_id is None and not claim.requires_reconciliation:
                self._store.release_delivery(
                    claim.delivery_id, error_category="discord_send"
                )
            # A returned/reconciled message ID proves the parent alert exists.
            # Keep the sending lease intact so the next expired claim resumes
            # from the durable ID, or marker-reconciles if that write failed.
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
                message_id = str(message.id)
                self._adapter.remember_mention_inbox_parent(message_id, channel_id)
                return message_id
        return None

    async def send(self, channel_id: str, content: str, *, allowed_mentions: dict[str, Any]) -> str:
        result = await self._adapter.send(
            channel_id, content,
            metadata={"nonconversational": True, "mention_inbox_no_mentions": True},
        )
        if not result.success or not result.message_id:
            raise RuntimeError("discord_send_failed")
        message_id = str(result.message_id)
        self._adapter.remember_mention_inbox_parent(message_id, channel_id)
        return message_id

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        return await self._adapter.find_anchored_thread(parent_message_id)

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str:
        return await self._adapter.create_anchored_thread(
            parent_message_id,
            name,
            auto_archive_duration,
        )

    def mark_thread_participation(self, thread_id: str) -> None:
        self._adapter.mark_mention_inbox_thread_participation(thread_id)

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        channel = await self._channel(thread_id)
        bot_id = getattr(getattr(self._adapter._client, "user", None), "id", None)
        formatter = getattr(self._adapter, "format_message", None)
        expected = formatter(content) if callable(formatter) else content
        async for message in channel.history(limit=limit):
            if (
                getattr(getattr(message, "author", None), "id", None) == bot_id
                and str(getattr(message, "content", "")) == expected
            ):
                return str(message.id)
        return None

    async def send_to_thread(
        self,
        thread_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        metadata: dict[str, Any] = {
            "thread_id": thread_id,
            "nonconversational": True,
            "mention_inbox_no_mentions": True,
        }
        if reply_to_message_id is not None:
            metadata["notify"] = True
        result = await self._adapter.send(
            thread_id,
            content,
            reply_to=reply_to_message_id,
            metadata=metadata,
        )
        if not result.success or not result.message_id:
            raise RuntimeError("discord_thread_send_failed")
        return str(result.message_id)


class _LazyGitHubNotificationCollector:
    """Hydrate candidates after resolving the authenticated stable identity."""

    _TEAM_MENTION_RE = re.compile(
        r"(?<![A-Za-z0-9-])@([A-Za-z0-9-]+)/([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
    )

    def __init__(
        self,
        client: GitHubNotificationsClient,
        repositories: tuple[str, ...],
        *,
        team_mentions: bool = False,
        team_review_requests: bool = False,
    ) -> None:
        if not isinstance(team_mentions, bool) or not isinstance(
            team_review_requests, bool
        ):
            raise ValueError("team switches must be boolean")
        self._client = client
        self._repositories = repositories
        self._team_mentions = team_mentions
        self._team_review_requests = team_review_requests
        self._collector = GitHubNotificationCollector(
            target_id="github:authenticated-user",
            allowed_repositories=repositories,
            include_owned_pr_activity=True,
        )
        self._target_login: str | None = None
        self._target_id: str | None = None

    def _resolve(self) -> None:
        if self._target_id is not None:
            return
        user = self._client.get_authenticated_user()
        self._target_login = user.login
        self._target_id = user.node_id
        self._collector = GitHubNotificationCollector(
            target_id=user.node_id,
            target_login=user.login,
            allowed_repositories=self._repositories,
            include_owned_pr_activity=True,
        )

    def accepts(self, notification: Mapping[str, Any]) -> bool:
        if notification.get("reason") == "team_mention" and not self._team_mentions:
            subject = notification.get("subject")
            if not isinstance(subject, Mapping) or subject.get("type") != "PullRequest":
                return False
        return self._collector.accepts(notification)

    @staticmethod
    def _timestamp(event: Mapping[str, Any]) -> str:
        for key in ("updated_at", "submitted_at", "created_at"):
            value = event.get(key)
            if isinstance(value, str):
                return value
        return ""

    @classmethod
    def _latest(cls, events: tuple[Mapping[str, Any], ...]) -> Mapping[str, Any] | None:
        if not events:
            return None
        return max(events, key=cls._timestamp)

    @classmethod
    def _mentioned_teams(cls, body: object) -> set[str]:
        if not isinstance(body, str):
            return set()
        return {
            f"{match.group(1)}/{match.group(2)}".casefold()
            for match in cls._TEAM_MENTION_RE.finditer(body)
        }

    @staticmethod
    def _requested_teams(subject: Mapping[str, Any]) -> set[str]:
        result: set[str] = set()
        raw = subject.get("requested_teams")
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            slug = item.get("slug")
            organization = item.get("organization")
            login = organization.get("login") if isinstance(organization, Mapping) else None
            if isinstance(slug, str) and isinstance(login, str):
                result.add(f"{login}/{slug}".casefold())
        return result

    def hydrate(self, notification: Mapping[str, Any]) -> GitHubHydrationContext | None:
        self._resolve()
        repository_payload = notification.get("repository")
        subject_payload = notification.get("subject")
        if not isinstance(repository_payload, Mapping) or not isinstance(subject_payload, Mapping):
            return None
        repository = repository_payload.get("full_name")
        subject_url = subject_payload.get("url")
        if not isinstance(repository, str) or not isinstance(subject_url, str):
            return None
        subject = self._client.fetch_subject(subject_url)
        if subject is None:
            return None

        latest_event = None
        latest_url = subject_payload.get("latest_comment_url")
        if isinstance(latest_url, str) and latest_url:
            latest_event = self._client.fetch_latest_event(latest_url, repository=repository)

        timeline: tuple[Mapping[str, Any], ...] = ()
        reviews: tuple[Mapping[str, Any], ...] = ()
        review_comments: tuple[Mapping[str, Any], ...] = ()
        if subject_payload.get("type") == "PullRequest":
            timeline = self._client.fetch_pull_timeline(
                subject_url, repository=repository, limit=50
            )
            reviews = self._client.fetch_pull_reviews(
                subject_url, repository=repository, limit=50
            )
            review_comments = self._client.fetch_pull_review_comments(
                subject_url, repository=repository, limit=50
            )

        reason = notification.get("reason")
        all_events = tuple(
            event
            for event in ((latest_event,) + reviews + review_comments + timeline)
            if isinstance(event, Mapping)
        )
        if reason == "review_requested":
            relevant = tuple(
                event
                for event in timeline
                if event.get("event_type") == "review_requested"
            )
            selected_event = self._latest(relevant) or latest_event or self._latest(all_events)
        elif reason == "assign":
            relevant = tuple(
                event for event in timeline if event.get("event_type") == "assigned"
            )
            selected_event = self._latest(relevant) or latest_event or self._latest(all_events)
        else:
            selected_event = self._latest(all_events)

        teams: set[str] = set()
        if self._team_review_requests and reason == "review_requested":
            teams.update(self._requested_teams(subject))
        if (
            self._team_mentions
            and reason == "team_mention"
            and selected_event is not None
        ):
            teams.update(self._mentioned_teams(selected_event.get("body")))
        verified: set[str] = set()
        assert self._target_login is not None
        assert self._target_id is not None
        for team in sorted(teams):
            if self._client.is_active_team_member(team, self._target_login):
                verified.add(team)

        return GitHubHydrationContext(
            target_login=self._target_login,
            target_node_id=self._target_id,
            subject=subject,
            latest_event=selected_event,
            timeline=timeline,
            reviews=reviews,
            review_comments=review_comments,
            verified_team_slugs=frozenset(verified),
        )

    def normalize(
        self,
        notification: Mapping[str, Any],
        detail: Mapping[str, Any] | GitHubHydrationContext | None,
    ):
        self._resolve()
        if not isinstance(detail, GitHubHydrationContext):
            return None
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
        self._router_installed = False
        self._execution_observer_installed = False
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
                team_mentions=self.config.team_mentions,
                team_review_requests=self.config.team_review_requests,
            )
            poller = GitHubMentionPoller(client=client, collector=collector, store=store)
            discord_transport = GatewayDiscordTransport(self._discord_adapter)
            thread_coordinator = None
            if self.config.action_sessions_enabled:
                from plugins.mention_inbox.thread_session import (
                    MentionInboxThreadCoordinator,
                )

                if self.config.proposal_bot_mention is None:
                    raise ValueError("action session bot mention is required")
                from plugins.mention_inbox.router import InboxProposalRouter

                approval_handler = None
                if self.config.execution_enabled:
                    execution_observer = ExecutionLifecycleObserver(
                        store=store,
                        discord=discord_transport,
                    )
                    self._discord_adapter.set_mention_inbox_execution_observer(
                        execution_observer
                    )
                    self._execution_observer_installed = True
                    approval_handler = ApprovalHandler(
                        store=store,
                        source_resolver=GitHubSubjectStateResolver(
                            store=store,
                            client=client,
                            allowed_repositories=frozenset(self.config.repositories),
                        ),
                        dispatcher=GatewayExecutionDispatcher(self._discord_adapter),
                        discord=discord_transport,
                        bot_mention=self.config.proposal_bot_mention,
                        authorized_approver_ids=frozenset(
                            self.config.authorized_approver_ids
                        ),
                    )
                thread_coordinator = MentionInboxThreadCoordinator(
                    store=store,
                    discord=discord_transport,
                    bot_mention=self.config.proposal_bot_mention,
                    executor_hint=self.config.execution_mode,
                    auto_archive_duration=self.config.thread_auto_archive_minutes,
                    approval_available=approval_handler is not None,
                )
                router = InboxProposalRouter(
                    store=store,
                    discord=discord_transport,
                    bot_mention=self.config.proposal_bot_mention,
                    authorized_approver_ids=frozenset(
                        self.config.authorized_approver_ids
                    ),
                    approval_handler=approval_handler,
                )
                self._discord_adapter.set_mention_inbox_router(router)
                self._router_installed = True
                if approval_handler is not None:
                    await approval_handler.recover_queued()
            delivery = DiscordMentionDelivery(
                store=store,
                discord=discord_transport,
                destination=self.config.destination,
                lease_seconds=self.config.lease_seconds,
                thread_coordinator=thread_coordinator,
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
        if self._router_installed and self._discord_adapter is not None:
            self._discord_adapter.set_mention_inbox_router(None)
            self._router_installed = False
        if self._execution_observer_installed and self._discord_adapter is not None:
            self._discord_adapter.set_mention_inbox_execution_observer(None)
            self._execution_observer_installed = False

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
