"""Operational GitHub-to-Discord mention inbox runtime.

The runtime is disabled by default. It performs GitHub GETs through the existing
poller and uses a durable SQLite outbox before posting deterministic, bounded
Discord views. Discord does not expose an idempotency key for ordinary channel
posts, so an expired send lease is reconciled against bounded bot-authored
channel history using the deterministic marker before any retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, cast

from plugins.mention_inbox.actionable import GitHubHydrationContext
from plugins.mention_inbox.advisory import HostProposalAdvisor
from plugins.mention_inbox.approval import (
    ApprovalHandler,
    ExecutionLifecycleObserver,
    GatewayExecutionDispatcher,
    GitHubSubjectStateResolver,
    normalize_execution_workspace,
    resolve_execution_workspace,
)
from plugins.mention_inbox.github_client import GitHubNotificationsClient
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.runtime import GitHubMentionPoller
from plugins.mention_inbox.store import DEFAULT_DESTINATION, MentionInboxStore
from .thread_session import (
    ThreadDestinationMismatchError,
    ThreadParticipantReconciliationIncompleteError,
    ThreadParticipantSyncError,
)
from plugins.mention_inbox.voice import RenderedDiscordEvent, render_action_alert
from plugins.mention_inbox.workspace import RepositoryWorktreeManager

_ALLOWED_REPOSITORY = "silviahealth/content"
_ALLOWED_ENV = "GITHUB_PAT_TOKEN"
_ALLOWED_NOTION_ENV = "NOTION_TOKEN"
_NOTION_OBJECT_ID = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)
_COLLECTOR_KEY = "github.notifications"
_DISCORD_MAX_SNOWFLAKE = (1 << 64) - 1
_ALLOWED_MENTIONS_NONE: dict[str, Any] = {
    "parse": [], "users": [], "roles": [], "replied_user": False,
}


def _is_discord_snowflake(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{5,19}", value) is not None
        and int(value) <= _DISCORD_MAX_SNOWFLAKE
    )


class _DeliveryLeaseLostError(RuntimeError):
    """The durable outbox claim was reclaimed by another delivery worker."""


@dataclass(frozen=True)
class NotionInboxConfig:
    enabled: bool = False
    credential_env: str = _ALLOWED_NOTION_ENV
    page_ids: tuple[str, ...] = ()
    poll_interval_seconds: int = 300


@dataclass(frozen=True)
class MentionInboxConfig:
    enabled: bool = False
    credential_env: str = _ALLOWED_ENV
    repositories: tuple[str, ...] = (_ALLOWED_REPOSITORY,)
    include_public_actionable_activity: bool = False
    external_repository_actions: str = "disabled"
    destination: str = DEFAULT_DESTINATION
    retention_days: int = 30
    lease_seconds: int = 60
    read_replay_lookback_minutes: int = 1440
    read_replay_max_pages: int = 2
    team_mentions: bool = False
    team_review_requests: bool = False
    action_sessions_enabled: bool = False
    user_message_mode: str = "proposal_router"
    proposal_bot_mention: str | None = None
    authorized_approver_ids: tuple[str, ...] = ()
    thread_auto_archive_minutes: int = 1440
    execution_enabled: bool = False
    execution_mode: str = "direct"
    execution_workspace: str | None = None
    execution_workspace_root: str | None = None
    terminal_cwd: str | None = None
    # Opt-in: adds one model call per new proposal revision. It only appends an
    # explanatory message, so turning it off never changes what a proposal
    # permits — and it stays off by default because the call can add tens of
    # seconds to a delivery.
    advisory_summary: bool = False
    notion: NotionInboxConfig = NotionInboxConfig()


class DiscordDeliveryTransport(Protocol):
    async def find_marker(self, channel_id: str, marker: str, *, limit: int) -> str | None: ...
    async def send(
        self,
        channel_id: str,
        content: str,
        *,
        allowed_mentions: dict[str, Any],
        nonce: str,
    ) -> str: ...


def _positive_int(value: object, name: str, default: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"mention_inbox.{name} must be a positive integer")
    return value


def _parse_notion_config(raw: object) -> NotionInboxConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("mention_inbox.notion must be an object")
    allowed_keys = {
        "enabled",
        "credential_env",
        "page_ids",
        "poll_interval_seconds",
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        raise ValueError("mention_inbox.notion contains unsupported keys")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("mention_inbox.notion.enabled must be a boolean")
    credential_env = raw.get("credential_env", _ALLOWED_NOTION_ENV)
    if credential_env != _ALLOWED_NOTION_ENV:
        raise ValueError("mention_inbox.notion.credential_env is not allowed")
    page_ids = raw.get("page_ids", [])
    if not isinstance(page_ids, list) or any(
        not isinstance(value, str) or _NOTION_OBJECT_ID.fullmatch(value) is None
        for value in page_ids
    ):
        raise ValueError("mention_inbox.notion.page_ids must contain only Notion UUIDs")
    if len(page_ids) > 20 or len(set(page_ids)) != len(page_ids):
        raise ValueError("mention_inbox.notion.page_ids must contain 0 to 20 unique IDs")
    if enabled and not page_ids:
        raise ValueError("mention_inbox.notion.page_ids is required when enabled")
    poll_interval = raw.get("poll_interval_seconds", 300)
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, int)
        or not 120 <= poll_interval <= 300
    ):
        raise ValueError(
            "mention_inbox.notion.poll_interval_seconds must be between 120 and 300"
        )
    return NotionInboxConfig(
        enabled=enabled,
        credential_env=credential_env,
        page_ids=tuple(page_ids),
        poll_interval_seconds=poll_interval,
    )


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
    if any(
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) is None
        for item in repositories
    ):
        raise ValueError("mention_inbox.repositories entries must be owner/repo")
    if len(set(repositories)) != len(repositories):
        raise ValueError("mention_inbox.repositories must not contain duplicates")
    include_public_actionable_activity = raw.get(
        "include_public_actionable_activity", False
    )
    if not isinstance(include_public_actionable_activity, bool):
        raise ValueError(
            "mention_inbox.include_public_actionable_activity must be a boolean"
        )
    external_repository_actions = raw.get("external_repository_actions", "disabled")
    if external_repository_actions not in {"disabled", "inspect_only", "own_pr_write"}:
        raise ValueError("mention_inbox.external_repository_actions is invalid")
    if (
        not isinstance(destination, str)
        or not destination.startswith("discord:")
        or not _is_discord_snowflake(destination.split(":", 1)[1])
    ):
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
    user_message_mode = action_sessions.get("user_message_mode", "proposal_router")
    if (
        not isinstance(user_message_mode, str)
        or user_message_mode not in {"proposal_router", "standard_agent"}
    ):
        raise ValueError("mention_inbox action-session user_message_mode is invalid")
    advisory_summary = raw.get("advisory_summary", False)
    if not isinstance(advisory_summary, bool):
        raise ValueError("mention_inbox.advisory_summary must be a boolean")
    bot_mention = action_sessions.get("bot_mention")
    if bot_mention is not None and (
        not isinstance(bot_mention, str)
        or re.fullmatch(r"<@([1-9][0-9]{5,19})>", bot_mention) is None
        or not _is_discord_snowflake(bot_mention[2:-1])
    ):
        raise ValueError("mention_inbox.action_sessions.bot_mention is invalid")
    approver_ids = action_sessions.get("authorized_approver_ids", [])
    if not isinstance(approver_ids, list) or any(
        not _is_discord_snowflake(value)
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
    workspace_value = action_sessions.get("workspace")
    execution_workspace = (
        None
        if workspace_value is None
        else normalize_execution_workspace(workspace_value)
    )
    workspace_root_value = action_sessions.get("workspace_root")
    execution_workspace_root = (
        None
        if workspace_root_value is None
        else normalize_execution_workspace(workspace_root_value)
    )
    if action_sessions_enabled and (bot_mention is None or not approver_ids):
        raise ValueError("enabled action sessions require bot mention and approvers")
    if execution_enabled and not action_sessions_enabled:
        raise ValueError("execution cannot be enabled while action sessions are disabled")
    terminal_cwd: str | None = None
    if execution_enabled:
        selected_workspace = (
            execution_workspace
            if external_repository_actions == "disabled"
            else execution_workspace_root
        )
        if selected_workspace is None:
            raise ValueError(
                "enabled execution requires an action-session workspace scope"
            )
        terminal = config.get("terminal", {})
        if not isinstance(terminal, Mapping):
            raise ValueError("terminal must be an object for mention-inbox execution")
        terminal_cwd_value = terminal.get("cwd")
        resolve_execution_workspace(terminal_cwd_value, selected_workspace)
        assert isinstance(terminal_cwd_value, str)
        terminal_cwd = terminal_cwd_value
    replay_lookback_minutes = _positive_int(
        raw.get("read_replay_lookback_minutes"),
        "read_replay_lookback_minutes",
        1440,
    )
    replay_max_pages = _positive_int(
        raw.get("read_replay_max_pages"),
        "read_replay_max_pages",
        2,
    )
    if replay_lookback_minutes > 10080:
        raise ValueError("mention_inbox.read_replay_lookback_minutes exceeds 7 days")
    if replay_max_pages > 10:
        raise ValueError("mention_inbox.read_replay_max_pages exceeds 10 pages")
    return MentionInboxConfig(
        enabled=enabled,
        credential_env=credential_env,
        repositories=tuple(repositories),
        include_public_actionable_activity=include_public_actionable_activity,
        external_repository_actions=external_repository_actions,
        destination=destination,
        retention_days=_positive_int(raw.get("retention_days"), "retention_days", 30),
        lease_seconds=_positive_int(raw.get("lease_seconds"), "lease_seconds", 60),
        read_replay_lookback_minutes=replay_lookback_minutes,
        read_replay_max_pages=replay_max_pages,
        team_mentions=team_mentions,
        team_review_requests=team_review_requests,
        action_sessions_enabled=action_sessions_enabled,
        user_message_mode=user_message_mode,
        proposal_bot_mention=bot_mention,
        authorized_approver_ids=tuple(approver_ids),
        thread_auto_archive_minutes=archive_minutes,
        execution_enabled=execution_enabled,
        execution_mode=execution_mode,
        execution_workspace=execution_workspace,
        execution_workspace_root=execution_workspace_root,
        terminal_cwd=terminal_cwd,
        advisory_summary=advisory_summary,
        notion=_parse_notion_config(raw.get("notion")),
    )


def _neutralize(value: str, limit: int) -> str:
    value = value.replace("@", "@\u200b")
    value = re.sub(r"([\\`*_{}\[\]()<>#+\-.!|])", r"\\\1", value)
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _marker(event: MentionEvent, revision_number: int, destination: str) -> str:
    identity = f"{event.dedupe_key}\0{revision_number}\0{destination}"
    return "[hermes-inbox:" + hashlib.sha256(identity.encode()).hexdigest()[:24] + "]"


def _parent_nonce(marker: str) -> str:
    return hashlib.sha256(
        f"mention-inbox-parent\0{marker}".encode()
    ).hexdigest()[:25]


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

    async def _run_claim_operation(
        self,
        claim,
        operation: Callable[[Callable[[], Awaitable[None]]], Awaitable[Any]],
    ) -> Any:
        async def checkpoint() -> None:
            if not self._store.renew_delivery_lease(
                claim.delivery_id,
                expected_attempt=claim.token,
                lease_seconds=self._lease_seconds,
            ):
                raise _DeliveryLeaseLostError("delivery lease is no longer owned")

        await checkpoint()
        stop = asyncio.Event()
        lease_lost = asyncio.Event()
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("delivery lease heartbeat requires an asyncio task")

        async def heartbeat() -> None:
            interval = max(self._lease_seconds / 3, 0.1)
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    if self._store.renew_delivery_lease(
                        claim.delivery_id,
                        expected_attempt=claim.token,
                        lease_seconds=self._lease_seconds,
                    ):
                        continue
                    lease_lost.set()
                    owner.cancel()
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            return await operation(checkpoint)
        except asyncio.CancelledError as exc:
            if lease_lost.is_set():
                raise _DeliveryLeaseLostError(
                    "delivery lease was reclaimed during thread operation"
                ) from exc
            raise
        finally:
            stop.set()
            await heartbeat_task

    async def deliver_once(self) -> str:
        claim = self._store.claim_delivery(
            self._destination, lease_seconds=self._lease_seconds
        )
        if claim is None:
            return "idle"
        thread_coordinator = self._thread_coordinator
        if thread_coordinator is not None:
            route_existing = getattr(
                thread_coordinator,
                "deliver_to_existing_thread",
                None,
            )
            if callable(route_existing):
                typed_route_existing = cast(
                    Callable[..., Awaitable[str | None]],
                    route_existing,
                )
                try:
                    thread_message_id = await self._run_claim_operation(
                        claim,
                        lambda checkpoint: typed_route_existing(
                            claim.event,
                            source_revision=claim.source_revision,
                            delivery_checkpoint=checkpoint,
                        ),
                    )
                except Exception as exc:
                    if isinstance(exc, ThreadDestinationMismatchError):
                        self._store.note_delivery_error(
                            claim.delivery_id,
                            error_category=(
                                "discord_thread_destination_mismatch"
                            ),
                            claim_token=claim.token,
                        )
                    elif isinstance(exc, ThreadParticipantSyncError):
                        self._store.note_delivery_error(
                            claim.delivery_id,
                            error_category="discord_thread_participant_sync_failed",
                            claim_token=claim.token,
                        )
                    # The coordinator is idempotent against proposal bindings.
                    # Keep the lease so an uncertain thread send is reconciled
                    # after expiry instead of posting a second channel card.
                    return "error"
                if thread_message_id is not None:
                    if not self._store.mark_delivery_sent(
                        claim.delivery_id,
                        claim_token=claim.token,
                        message_id=thread_message_id,
                    ):
                        return "error"
                    return (
                        "reconciled"
                        if claim.requires_reconciliation
                        else "threaded"
                    )
        rendered = render_discord_event(
            claim.event,
            revision_number=claim.revision_number,
            destination=claim.destination,
        )
        confirmed_message_id = claim.message_id
        try:
            if confirmed_message_id is not None:
                remember_parent = getattr(
                    self._discord,
                    "remember_parent_message",
                    None,
                )
                if callable(remember_parent):
                    remember_parent(confirmed_message_id, self._channel_id)
                if thread_coordinator is not None:
                    await self._run_claim_operation(
                        claim,
                        lambda checkpoint: thread_coordinator.ensure_thread(
                            claim.event,
                            parent_message_id=confirmed_message_id,
                            parent_channel_id=self._channel_id,
                            source_revision=claim.source_revision,
                            delivery_checkpoint=checkpoint,
                        ),
                    )
                if not self._store.mark_delivery_sent(
                    claim.delivery_id,
                    claim_token=claim.token,
                    message_id=confirmed_message_id,
                ):
                    return "error"
                return "reconciled"
            if claim.requires_reconciliation:
                existing = await self._discord.find_marker(
                    self._channel_id, claim.marker, limit=100
                )
                if existing is not None:
                    confirmed_message_id = existing
                    remember_parent = getattr(
                        self._discord,
                        "remember_parent_message",
                        None,
                    )
                    if callable(remember_parent):
                        remember_parent(existing, self._channel_id)
                    if not self._store.mark_delivery_parent_confirmed(
                        claim.delivery_id,
                        claim_token=claim.token,
                        message_id=existing,
                    ):
                        raise _DeliveryLeaseLostError(
                            "delivery lease was lost before parent confirmation"
                        )
                    if thread_coordinator is not None:
                        await self._run_claim_operation(
                            claim,
                            lambda checkpoint: thread_coordinator.ensure_thread(
                                claim.event,
                                parent_message_id=existing,
                                parent_channel_id=self._channel_id,
                                source_revision=claim.source_revision,
                                delivery_checkpoint=checkpoint,
                            ),
                        )
                    if not self._store.mark_delivery_sent(
                        claim.delivery_id,
                        claim_token=claim.token,
                        message_id=existing,
                    ):
                        return "error"
                    return "reconciled"
            if not self._store.renew_delivery_lease(
                claim.delivery_id,
                expected_attempt=claim.token,
                lease_seconds=self._lease_seconds,
            ):
                raise _DeliveryLeaseLostError(
                    "delivery lease was lost before parent send"
                )
            message_id = await self._run_claim_operation(
                claim,
                lambda checkpoint: self._discord.send(
                    self._channel_id,
                    rendered.content,
                    allowed_mentions=rendered.allowed_mentions,
                    nonce=_parent_nonce(rendered.marker),
                ),
            )
            confirmed_message_id = message_id
            if not self._store.mark_delivery_parent_confirmed(
                claim.delivery_id,
                claim_token=claim.token,
                message_id=message_id,
            ):
                raise _DeliveryLeaseLostError(
                    "delivery lease was lost before parent confirmation"
                )
            remember_parent = getattr(
                self._discord,
                "remember_parent_message",
                None,
            )
            if callable(remember_parent):
                remember_parent(message_id, self._channel_id)
            if thread_coordinator is not None:
                await self._run_claim_operation(
                    claim,
                    lambda checkpoint: thread_coordinator.ensure_thread(
                        claim.event,
                        parent_message_id=message_id,
                        parent_channel_id=self._channel_id,
                        source_revision=claim.source_revision,
                        delivery_checkpoint=checkpoint,
                    ),
                )
        except Exception as exc:
            if confirmed_message_id is None and not claim.requires_reconciliation:
                self._store.note_delivery_error(
                    claim.delivery_id,
                    claim_token=claim.token,
                    error_category="discord_send",
                )
            elif isinstance(exc, ThreadDestinationMismatchError):
                self._store.note_delivery_error(
                    claim.delivery_id,
                    error_category="discord_thread_destination_mismatch",
                    claim_token=claim.token,
                )
            elif isinstance(exc, ThreadParticipantSyncError):
                self._store.note_delivery_error(
                    claim.delivery_id,
                    error_category="discord_thread_participant_sync_failed",
                    claim_token=claim.token,
                )
            # A returned/reconciled message ID proves the parent alert exists.
            # Keep the sending lease intact so the next expired claim resumes
            # from the durable ID, or marker-reconciles if that write failed.
            return "error"
        return (
            "sent"
            if self._store.mark_delivery_sent(
                claim.delivery_id,
                claim_token=claim.token,
                message_id=message_id,
            )
            else "error"
        )


Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
RecoverExecutions = Callable[[], Awaitable[int]]


class MentionInboxRuntime:
    """Cancellation-safe singleton poll/delivery loop for one profile DB."""
    def __init__(self, *, config: MentionInboxConfig, store: MentionInboxStore,
                 poller: Any, delivery: Any, clock: Clock | None = None,
                 sleep: Sleep = asyncio.sleep,
                 recover_executions: RecoverExecutions | None = None) -> None:
        self.config = config
        self.store = store
        self.poller = poller
        self.delivery = delivery
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._recover_executions = recover_executions
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
            if self._recover_executions is not None:
                await self._recover_executions()
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
    def __init__(
        self,
        adapter: Any,
        *,
        parent_channel_id: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._parent_channel_id = parent_channel_id

    def remember_parent_message(
        self, parent_message_id: str, parent_channel_id: str
    ) -> None:
        if (
            self._parent_channel_id is not None
            and self._parent_channel_id != parent_channel_id
        ):
            raise ThreadDestinationMismatchError(
                "Discord work thread does not belong to the configured "
                "Discord destination"
            )
        self._adapter.remember_mention_inbox_parent(
            parent_message_id,
            parent_channel_id,
        )

    async def _require_thread_destination(self, thread_id: str) -> None:
        parent_channel_id = self._parent_channel_id
        if (
            parent_channel_id is not None
            and not await self.thread_has_parent(
                thread_id,
                parent_channel_id,
            )
        ):
            raise ThreadDestinationMismatchError(
                "Discord work thread does not belong to the configured "
                "Discord destination"
            )

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

    async def send(
        self,
        channel_id: str,
        content: str,
        *,
        allowed_mentions: dict[str, Any],
        nonce: str,
    ) -> str:
        if allowed_mentions != _ALLOWED_MENTIONS_NONE:
            raise RuntimeError("mention-inbox parent mentions must be disabled")
        result = await self._adapter.send(
            channel_id,
            content,
            metadata={
                "non_conversational": True,
                "mention_inbox_no_mentions": True,
                "mention_inbox_nonce": nonce,
            },
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

    async def ensure_thread_participants(
        self,
        thread_id: str,
        user_ids: frozenset[str],
    ) -> None:
        await self._require_thread_destination(thread_id)
        await self._adapter.ensure_mention_inbox_thread_participants(
            thread_id,
            user_ids,
        )

    async def is_thread_active(self, thread_id: str) -> bool:
        return await self._adapter.is_mention_inbox_thread_active(thread_id)

    async def thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool:
        return await self._adapter.mention_inbox_thread_has_parent(
            thread_id,
            parent_channel_id,
        )

    async def activate_thread(self, thread_id: str) -> None:
        await self._require_thread_destination(thread_id)
        await self._adapter.activate_mention_inbox_thread(thread_id)

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
        await self._require_thread_destination(thread_id)
        metadata: dict[str, Any] = {
            "thread_id": thread_id,
            "non_conversational": True,
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

    async def edit_thread_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
    ) -> None:
        await self._require_thread_destination(thread_id)
        result = await self._adapter.edit_message(
            thread_id,
            message_id,
            content,
            finalize=True,
            metadata={
                "non_conversational": True,
                "mention_inbox_no_mentions": True,
            },
        )
        if not result.success:
            raise RuntimeError("discord_thread_edit_failed")

    async def send_proposal_to_thread(
        self,
        thread_id: str,
        content: str,
        *,
        proposal_id: str,
        proposal_revision: int,
        approval_offered: bool,
    ) -> str:
        await self._require_thread_destination(thread_id)
        sender = getattr(self._adapter, "send_mention_inbox_proposal", None)
        if not callable(sender):
            return await self.send_to_thread(thread_id, content)
        result = await sender(
            thread_id,
            content,
            proposal_id=proposal_id,
            proposal_revision=proposal_revision,
            approval_offered=approval_offered,
        )
        if not result.success or not result.message_id:
            raise RuntimeError("discord_proposal_send_failed")
        return str(result.message_id)


class _LazyGitHubNotificationCollector:
    """Hydrate candidates after resolving the authenticated stable identity."""

    _TEAM_MENTION_RE = re.compile(
        r"(?<![A-Za-z0-9-])@([A-Za-z0-9-]+)/([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
    )
    _SEMANTIC_EVENT_TYPES = frozenset({
        "issue_comment",
        "comment",
        "review",
        "pull_request_review",
        "review_comment",
        "pull_request_review_comment",
    })

    def __init__(
        self,
        client: GitHubNotificationsClient,
        repositories: tuple[str, ...],
        *,
        team_mentions: bool = False,
        team_review_requests: bool = False,
        include_public_actionable_activity: bool = False,
    ) -> None:
        if not isinstance(team_mentions, bool) or not isinstance(
            team_review_requests, bool
        ):
            raise ValueError("team switches must be boolean")
        self._client = client
        self._repositories = repositories
        self._team_mentions = team_mentions
        self._team_review_requests = team_review_requests
        self._include_public_actionable_activity = include_public_actionable_activity
        self._collector = GitHubNotificationCollector(
            target_id="github:authenticated-user",
            allowed_repositories=repositories,
            include_owned_pr_activity=True,
            include_public_actionable_activity=include_public_actionable_activity,
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
            include_public_actionable_activity=self._include_public_actionable_activity,
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

    @staticmethod
    def _event_type(event: Mapping[str, Any]) -> str:
        value = event.get("event_type") or event.get("event")
        return value.casefold() if isinstance(value, str) else ""

    @classmethod
    def _is_semantic(cls, event: Mapping[str, Any] | None) -> bool:
        return isinstance(event, Mapping) and (
            cls._event_type(event) in cls._SEMANTIC_EVENT_TYPES
        )

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
        semantic_events = tuple(
            event for event in all_events if self._is_semantic(event)
        )
        if reason == "review_requested":
            relevant = tuple(
                event
                for event in timeline
                if event.get("event_type") == "review_requested"
            )
            selected_event = (
                self._latest(relevant)
                or (latest_event if self._is_semantic(latest_event) else None)
                or self._latest(semantic_events)
            )
        elif reason == "assign":
            relevant = tuple(
                event for event in timeline if event.get("event_type") == "assigned"
            )
            selected_event = (
                self._latest(relevant)
                or (latest_event if self._is_semantic(latest_event) else None)
                or self._latest(semantic_events)
            )
        else:
            selected_event = self._latest(semantic_events)

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

    def normalize_many(
        self,
        notification: Mapping[str, Any],
        detail: Mapping[str, Any] | GitHubHydrationContext | None,
    ):
        self._resolve()
        if not isinstance(detail, GitHubHydrationContext):
            return ()
        return self._collector.normalize_many(notification, detail)


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
                include_public_actionable_activity=(
                    self.config.include_public_actionable_activity
                ),
            )
            poller = GitHubMentionPoller(
                client=client,
                collector=collector,
                store=store,
                read_replay_lookback=timedelta(
                    minutes=self.config.read_replay_lookback_minutes
                ),
                max_replay_pages=self.config.read_replay_max_pages,
            )
            destination_channel_id = self.config.destination.split(":", 1)[1]
            discord_transport = GatewayDiscordTransport(
                self._discord_adapter,
                parent_channel_id=destination_channel_id,
            )
            thread_coordinator = None
            if self.config.action_sessions_enabled:
                from plugins.mention_inbox.thread_session import (
                    MentionInboxThreadCoordinator,
                )

                if self.config.proposal_bot_mention is None:
                    raise ValueError("action session bot mention is required")
                from plugins.mention_inbox.conversation import (
                    HostReadOnlyConversationResponder,
                )
                from plugins.mention_inbox.router import InboxProposalRouter

                approval_handler = None
                if self.config.execution_enabled:
                    workspace_scope = (
                        self.config.execution_workspace
                        if self.config.external_repository_actions == "disabled"
                        else self.config.execution_workspace_root
                    )
                    workspace = resolve_execution_workspace(
                        self.config.terminal_cwd,
                        workspace_scope,
                    )
                    workspace_manager = (
                        None
                        if self.config.external_repository_actions == "disabled"
                        else RepositoryWorktreeManager(workspace)
                    )
                    execution_observer = ExecutionLifecycleObserver(
                        store=store,
                        discord=discord_transport,
                        workspace=workspace,
                    )
                    self._execution_observer_installed = True
                    self._discord_adapter.set_mention_inbox_execution_observer(
                        execution_observer
                    )
                    approval_handler = ApprovalHandler(
                        store=store,
                        source_resolver=GitHubSubjectStateResolver(
                            store=store,
                            client=client,
                            allowed_repositories=frozenset(self.config.repositories),
                            include_public_actionable_activity=(
                                self.config.external_repository_actions != "disabled"
                            ),
                            external_repository_actions=(
                                self.config.external_repository_actions
                            ),
                        ),
                        dispatcher=GatewayExecutionDispatcher(
                            self._discord_adapter,
                            thread_destination_validator=lambda thread_id: (
                                discord_transport.thread_has_parent(
                                    thread_id,
                                    destination_channel_id,
                                )
                            ),
                        ),
                        discord=discord_transport,
                        bot_mention=self.config.proposal_bot_mention,
                        authorized_approver_ids=frozenset(
                            self.config.authorized_approver_ids
                        ),
                        workspace=workspace,
                        workspace_manager=workspace_manager,
                    )
                thread_coordinator = MentionInboxThreadCoordinator(
                    store=store,
                    discord=discord_transport,
                    bot_mention=self.config.proposal_bot_mention,
                    executor_hint=self.config.execution_mode,
                    auto_archive_duration=self.config.thread_auto_archive_minutes,
                    approval_available=approval_handler is not None,
                    trusted_repositories=frozenset(self.config.repositories),
                    external_repository_actions=(
                        self.config.external_repository_actions
                    ),
                    participant_user_ids=frozenset(
                        self.config.authorized_approver_ids
                    ),
                    participant_parent_channel_id=destination_channel_id,
                    advisor=(
                        HostProposalAdvisor()
                        if self.config.advisory_summary
                        else None
                    ),
                )
                router = InboxProposalRouter(
                    store=store,
                    discord=discord_transport,
                    bot_mention=self.config.proposal_bot_mention,
                    authorized_approver_ids=frozenset(
                        self.config.authorized_approver_ids
                    ),
                    approval_handler=approval_handler,
                    conversation_responder=HostReadOnlyConversationResponder(),
                    user_message_mode=self.config.user_message_mode,
                    destination_channel_id=destination_channel_id,
                    thread_destination_validator=lambda thread_id: (
                        discord_transport.thread_has_parent(
                            thread_id,
                            destination_channel_id,
                        )
                    ),
                )
                self._router_installed = True
                self._discord_adapter.set_mention_inbox_router(router)
                participant_reconciliation = (
                    await thread_coordinator.reconcile_thread_participants()
                )
                if participant_reconciliation.failed:
                    raise ThreadParticipantSyncError(
                        "Discord thread participant reconciliation failed"
                    )
                if participant_reconciliation.overflow:
                    raise ThreadParticipantReconciliationIncompleteError(
                        "Discord thread participant reconciliation exceeded its limit"
                    )
                if approval_handler is not None:
                    await execution_observer.reconcile_terminal_receipts()
                    await approval_handler.reconcile_execution_policy(
                        trusted_repositories=frozenset(self.config.repositories),
                        external_repository_actions=(
                            self.config.external_repository_actions
                        ),
                    )
                    await thread_coordinator.reconcile_execution_activation()
                    await approval_handler.recover_queued()
            delivery = DiscordMentionDelivery(
                store=store,
                discord=discord_transport,
                destination=self.config.destination,
                lease_seconds=self.config.lease_seconds,
                thread_coordinator=thread_coordinator,
            )
            self._runtime = MentionInboxRuntime(
                config=self.config,
                store=store,
                poller=poller,
                delivery=delivery,
                recover_executions=(
                    None
                    if approval_handler is None
                    else approval_handler.recover_queued
                ),
            )
            self._runtime.start()
            self._degraded_category = None
        except asyncio.CancelledError:
            self._remove_installed_hooks()
            raise
        except ThreadParticipantSyncError:
            self._remove_installed_hooks()
            self._degraded_category = "discord_thread_participant_sync_failed"
        except ThreadParticipantReconciliationIncompleteError:
            self._remove_installed_hooks()
            self._degraded_category = (
                "discord_thread_participant_reconciliation_incomplete"
            )
        except Exception:
            self._remove_installed_hooks()
            self._degraded_category = "startup_failed"

    def _remove_installed_hooks(self) -> None:
        if self._router_installed and self._discord_adapter is not None:
            self._discord_adapter.set_mention_inbox_router(None)
            self._router_installed = False
        if self._execution_observer_installed and self._discord_adapter is not None:
            self._discord_adapter.set_mention_inbox_execution_observer(None)
            self._execution_observer_installed = False

    async def stop(self) -> None:
        if self._runtime is not None:
            await self._runtime.stop()
        self._remove_installed_hooks()

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
