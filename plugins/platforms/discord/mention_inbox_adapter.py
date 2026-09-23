"""Discord-side Work Inbox routing, anchored threads, and approved execution admission."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import secrets
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)
_DISCORD_MAX_SNOWFLAKE = (1 << 64) - 1


def _is_discord_snowflake(value: str) -> bool:
    return (
        re.fullmatch(r"[1-9][0-9]{5,19}", value) is not None
        and int(value) <= _DISCORD_MAX_SNOWFLAKE
    )


class DiscordMentionInboxMixin:
    async def send_mention_inbox_proposal(
        self,
        thread_id: str,
        content: str,
        *,
        proposal_id: str,
        proposal_revision: int,
        approval_offered: bool,
    ) -> SendResult:
        """Post a text-only proposal with a revision-specific Discord nonce."""
        from . import adapter as adapter_module

        if not self._client or not adapter_module.DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")
        if getattr(self, "_mention_inbox_router", None) is None:
            return SendResult(success=False, error="Mention-inbox router unavailable")
        try:
            channel = self._client.get_channel(int(thread_id))
            if channel is None:
                channel = await self._client.fetch_channel(int(thread_id))
            nonce = hashlib.sha256(
                f"mention-inbox-proposal\0{proposal_id}\0{proposal_revision}".encode()
            ).hexdigest()[:25]
            message = await channel.send(
                content=self.format_message(content),
                allowed_mentions=adapter_module.discord.AllowedMentions.none(),
                nonce=nonce,
            )
            message_id = str(message.id)
            marked = self._nonconversational_messages.mark_many([message_id])
            if inspect.isawaitable(marked):
                await marked
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.warning("[%s] send_mention_inbox_proposal failed: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))

    def remember_mention_inbox_parent(
        self, parent_message_id: str, parent_channel_id: str
    ) -> None:
        """Remember where a sent/reconciled inbox alert can be fetched."""
        message_id = str(parent_message_id)
        channel_id = str(parent_channel_id)
        if not message_id.isdigit() or not channel_id.isdigit():
            raise ValueError("Discord parent message and channel IDs must be numeric")
        mapping = getattr(self, "_mention_inbox_parent_channels", None)
        if mapping is None:
            mapping = {}
            self._mention_inbox_parent_channels = mapping
        mapping[message_id] = channel_id
        while len(mapping) > 2000:
            mapping.pop(next(iter(mapping)))

    async def _mention_inbox_parent_message(self, parent_message_id: str) -> Any:
        message_id = str(parent_message_id)
        if not message_id.isdigit():
            raise ValueError("Discord parent message ID must be numeric")
        mapping = getattr(self, "_mention_inbox_parent_channels", {})
        channel_id = mapping.get(message_id)
        if channel_id is None:
            raise ValueError("Discord parent channel is unknown")
        client = getattr(self, "_client", None)
        if client is None:
            raise RuntimeError("Discord adapter is not connected")
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        if channel is None or not hasattr(channel, "fetch_message"):
            raise RuntimeError("Discord parent channel is unavailable")
        return await channel.fetch_message(int(message_id))

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        """Return the public thread already anchored to an inbox alert, if any."""
        message = await self._mention_inbox_parent_message(parent_message_id)
        thread = getattr(message, "thread", None)
        if thread is not None and getattr(thread, "id", None) is not None:
            return str(thread.id)
        client = getattr(self, "_client", None)
        cached = client.get_channel(int(parent_message_id)) if client is not None else None
        if cached is not None and getattr(cached, "id", None) is not None:
            return str(cached.id)
        return None

    async def create_anchored_thread(
        self,
        parent_message_id: str,
        name: str,
        auto_archive_duration: int = 1440,
    ) -> str:
        """Idempotently create a public thread from a bot-authored parent alert."""
        message_id = str(parent_message_id)
        thread_name = " ".join(str(name).split())
        if not message_id.isdigit():
            raise ValueError("Discord parent message ID must be numeric")
        if not thread_name or len(thread_name) > 100:
            raise ValueError("Discord thread name must contain 1..100 characters")
        from .adapter import VALID_THREAD_AUTO_ARCHIVE_MINUTES
        if auto_archive_duration not in VALID_THREAD_AUTO_ARCHIVE_MINUTES:
            raise ValueError("unsupported Discord auto archive duration")
        locks = getattr(self, "_mention_inbox_thread_locks", None)
        if locks is None:
            locks = {}
            self._mention_inbox_thread_locks = locks
        lock = locks.setdefault(message_id, asyncio.Lock())
        async with lock:
            existing = await self.find_anchored_thread(message_id)
            if existing is not None:
                return existing
            message = await self._mention_inbox_parent_message(message_id)
            try:
                thread = await message.create_thread(
                    name=thread_name,
                    auto_archive_duration=auto_archive_duration,
                )
            except Exception:
                # Discord can report an already-created race. Re-fetch the
                # parent and accept only a real anchored thread before raising.
                existing = await self.find_anchored_thread(message_id)
                if existing is not None:
                    return existing
                raise
            thread_id = getattr(thread, "id", None)
            if thread_id is None:
                raise RuntimeError("Discord thread creation returned no thread ID")
            return str(thread_id)

    async def ensure_mention_inbox_thread_participants(
        self,
        thread_id: str,
        user_ids: frozenset[str],
    ) -> None:
        value = str(thread_id)
        if not _is_discord_snowflake(value):
            raise ValueError("Discord thread ID must be a valid snowflake")
        participants = tuple(sorted(str(user_id) for user_id in user_ids))
        if any(
            not _is_discord_snowflake(user_id)
            for user_id in participants
        ):
            raise ValueError("Discord participant user ID is invalid")
        if not participants:
            return
        client = self._client
        if client is None:
            raise RuntimeError("Discord client is unavailable")
        thread: Any = client.get_channel(int(value))
        if thread is None:
            thread = await client.fetch_channel(int(value))
        add_user = getattr(thread, "add_user", None)
        if not callable(add_user):
            raise RuntimeError("Discord thread participant API is unavailable")
        from .adapter import _Snowflake
        for user_id in participants:
            result = add_user(_Snowflake(int(user_id)))
            if not inspect.isawaitable(result):
                raise RuntimeError("Discord thread participant API is not async")
            await result

    async def is_mention_inbox_thread_active(self, thread_id: str) -> bool:
        value = str(thread_id)
        if not _is_discord_snowflake(value):
            raise ValueError("Discord thread ID must be a valid snowflake")
        client = self._client
        if client is None:
            raise RuntimeError("Discord client is unavailable")
        thread: Any = client.get_channel(int(value))
        if thread is None:
            thread = await client.fetch_channel(int(value))
        return (
            getattr(thread, "archived", None) is False
            and getattr(thread, "locked", None) is False
        )

    async def mention_inbox_thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool:
        value = str(thread_id)
        parent = str(parent_channel_id)
        if not _is_discord_snowflake(value) or not _is_discord_snowflake(parent):
            raise ValueError(
                "Discord thread and parent IDs must be valid snowflakes"
            )
        client = self._client
        if client is None:
            raise RuntimeError("Discord client is unavailable")
        thread: Any = client.get_channel(int(value))
        if thread is None:
            thread = await client.fetch_channel(int(value))
        return str(getattr(thread, "parent_id", "") or "") == parent

    async def activate_mention_inbox_thread(self, thread_id: str) -> None:
        value = str(thread_id)
        if not _is_discord_snowflake(value):
            raise ValueError("Discord thread ID must be a valid snowflake")
        client = self._client
        if client is None:
            raise RuntimeError("Discord client is unavailable")
        thread: Any = client.get_channel(int(value))
        if thread is None:
            thread = await client.fetch_channel(int(value))
        if getattr(thread, "locked", None) is not False:
            raise RuntimeError("Discord work thread is locked")
        if getattr(thread, "archived", None) is False:
            return
        edit = getattr(thread, "edit", None)
        if not callable(edit):
            raise RuntimeError("Discord thread activation API is unavailable")
        result = edit(archived=False)
        if not inspect.isawaitable(result):
            raise RuntimeError("Discord thread activation API is not async")
        await result

    def mark_mention_inbox_thread_participation(self, thread_id: str) -> None:
        value = str(thread_id)
        if not _is_discord_snowflake(value):
            raise ValueError("Discord thread ID must be a valid snowflake")
        tracker = getattr(self, "_threads", None)
        if tracker is None:
            raise RuntimeError("Discord thread participation tracker is unavailable")
        tracker.mark(value)

    async def handle_message(self, event: MessageEvent) -> bool:
        """Replay queued Discord ingress through late-bound thread routers."""
        source = getattr(event, "source", None)
        is_startup_replay = bool(
            getattr(event, "_hermes_startup_restore_replay", False)
        )
        is_external_discord_message = bool(
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and not getattr(event, "internal", False)
        )
        is_external_discord_thread = bool(
            is_external_discord_message
            and getattr(source, "chat_type", None) == "thread"
        )
        if is_startup_replay and is_external_discord_message:
            route_channel_id = str(
                (
                    getattr(source, "thread_id", None)
                    if is_external_discord_thread
                    else None
                )
                or getattr(source, "chat_id", "")
                or ""
            )
            route_parent_id = getattr(source, "parent_chat_id", None)
            raw_message = getattr(event, "raw_message", None)
            metadata = getattr(event, "metadata", {})
            route_result = None
            if route_channel_id and raw_message is None:
                router = getattr(self, "_mention_inbox_router", None)
                if router is not None:
                    try:
                        surface_checker = getattr(router, "is_agent_surface", None)
                        agent_surface = bool(
                            surface_checker(route_channel_id, route_parent_id)
                            if callable(surface_checker)
                            else False
                        )
                        registered_thread = bool(
                            is_external_discord_thread
                            and not agent_surface
                            and router.is_work_thread(route_channel_id)
                        )
                    except Exception:
                        logger.warning(
                            "[%s] Startup replay work-surface validation failed closed",
                            self.name,
                            exc_info=True,
                        )
                        return True
                    if registered_thread or agent_surface:
                        logger.warning(
                            "[%s] Dropping startup-replayed Work Inbox message "
                            "without raw Discord human-admission evidence",
                            self.name,
                        )
                        return True
            if route_channel_id and raw_message is not None:
                raw_channel = getattr(raw_message, "channel", None)
                raw_parent_id = getattr(raw_channel, "parent_id", None)
                if raw_parent_id is None:
                    raw_parent_id = getattr(
                        getattr(raw_channel, "parent", None),
                        "id",
                        None,
                    )
                if raw_parent_id is not None:
                    route_parent_id = str(raw_parent_id)
                replay_content = str(getattr(raw_message, "content", "") or "")
                stored_content = (
                    metadata.get("discord_original_content")
                    if isinstance(metadata, dict)
                    else None
                )
                if isinstance(stored_content, str):
                    comparable_content = stored_content
                    bot_id = getattr(getattr(self, "_client", None), "user", None)
                    bot_id = getattr(bot_id, "id", None)
                    if bot_id is not None:
                        comparable_content = comparable_content.replace(
                            f"<@{bot_id}>", ""
                        ).replace(f"<@!{bot_id}>", "")
                    if comparable_content.strip() == replay_content.strip():
                        replay_content = stored_content
                route_result = await self._route_mention_inbox_message_result(
                    raw_message,
                    thread_id=route_channel_id,
                    parent_channel_id=(
                        None if route_parent_id is None else str(route_parent_id)
                    ),
                    raw_content=replay_content,
                    check_registered_thread=is_external_discord_thread,
                )
            if route_result is not None and bool(route_result.handled):
                return True
            agent_text = (
                None
                if route_result is None
                else getattr(route_result, "agent_text", None)
            )
            if isinstance(agent_text, str) and agent_text.strip():
                event.text = agent_text
                event.message_type = MessageType.TEXT
                event.channel_context = None
                event.media_urls = []
                event.media_types = []
                event.reply_to_message_id = None
                event.reply_to_text = None
                event.reply_to_author_id = None
                event.reply_to_author_name = None
                event.reply_to_is_own_message = False
                if isinstance(metadata, dict):
                    metadata["discord_original_content"] = replay_content
                if event.source is not None:
                    event.source.chat_name = "Work Inbox"

        # Internal approved-execution events and ordinary Discord events stay
        # on the shared adapter rail. Returning True records successful
        # admission for enqueue_mention_inbox_execution().
        await super().handle_message(event)
        return True

    def set_mention_inbox_router(self, router: Any | None) -> None:
        """Install or clear the dedicated registered-work-thread router."""
        self._mention_inbox_router = router

    def set_mention_inbox_execution_observer(self, observer: Any | None) -> None:
        """Install or clear the in-process approved-execution lifecycle observer."""
        self._mention_inbox_execution_observer = observer

    async def enqueue_mention_inbox_execution(
        self, request: Any, prompt: str
    ) -> str:
        """Admit one approved envelope to the existing thread session rail."""
        execution_id = str(getattr(request, "execution_id", ""))
        proposal_hash = str(getattr(request, "proposal_hash", ""))
        recovery_token = str(getattr(request, "recovery_token", ""))
        mode = str(getattr(request, "executor_hint", ""))
        approval_message_id = str(getattr(request, "approval_message_id", ""))
        approver_user_id = str(getattr(request, "approver_user_id", ""))
        thread_id = str(getattr(request, "thread_id", ""))
        if not execution_id or len(execution_id) > 80:
            raise ValueError("execution_id is invalid")
        if len(proposal_hash) != 64 or any(
            char not in "0123456789abcdef" for char in proposal_hash
        ):
            raise ValueError("proposal_hash is invalid")
        if mode not in {"direct", "kanban"}:
            raise ValueError("execution mode is invalid")
        if not recovery_token or len(recovery_token) > 80:
            raise ValueError("execution recovery token is invalid")
        owner_id = secrets.token_hex(16)
        if not all(
            value.isdigit()
            for value in (approval_message_id, approver_user_id, thread_id)
        ):
            raise ValueError("Discord execution identities must be numeric")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("approved execution prompt is invalid")
        client = getattr(self, "_client", None)
        if client is None:
            raise RuntimeError("Discord adapter is not connected")
        channel = client.get_channel(int(thread_id))
        if channel is None:
            channel = await client.fetch_channel(int(thread_id))
        if channel is None:
            raise RuntimeError("approved execution thread is unavailable")
        parent = getattr(channel, "parent", None)
        parent_id = str(getattr(parent, "id", "") or "")
        guild = getattr(channel, "guild", None)
        guild_name = str(getattr(guild, "name", "") or "")
        thread_name = str(getattr(channel, "name", "") or "work thread")
        chat_name = f"{guild_name} / {thread_name}" if guild_name else thread_name
        chat_topic = getattr(parent, "topic", None)
        source = self.build_source(
            chat_id=thread_id,
            chat_name=chat_name,
            chat_type="thread",
            user_id=approver_user_id,
            user_name="approved mention-inbox user",
            thread_id=thread_id,
            chat_topic=chat_topic if isinstance(chat_topic, str) else None,
            guild_id=(None if guild is None else str(getattr(guild, "id", "") or "")),
            parent_chat_id=parent_id or None,
            message_id=approval_message_id,
            role_authorized=True,
        )
        has_config = getattr(self, "config", None) is not None
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=None,
            auto_skill=(
                self._resolve_channel_skills(thread_id, parent_id or None)
                if has_config
                else None
            ),
            channel_prompt=(
                self._resolve_channel_prompt(thread_id, parent_id or None)
                if has_config
                else None
            ),
            internal=True,
            metadata={
                "mention_inbox_execution": {
                    "execution_id": execution_id,
                    "proposal_hash": proposal_hash,
                    "mode": mode,
                    "recovery_token": recovery_token,
                    "owner_id": owner_id,
                }
            },
        )
        lock = getattr(
            self,
            "_mention_inbox_execution_admission_lock",
            None,
        )
        if lock is None:
            lock = asyncio.Lock()
            self._mention_inbox_execution_admission_lock = lock
        bindings = getattr(
            self,
            "_mention_inbox_execution_admissions",
            None,
        )
        if bindings is None:
            bindings = {}
            self._mention_inbox_execution_admissions = bindings
        binding = (proposal_hash, mode, thread_id, recovery_token)
        dispatch_id = f"{mode}:{execution_id}"
        async with lock:
            existing = bindings.get(execution_id)
            if existing == binding:
                return dispatch_id
            if existing is not None:
                raise ValueError(
                    "execution_id is already bound to another approved execution"
                )
            bindings[execution_id] = binding
            try:
                if not await self.handle_message(event):
                    raise RuntimeError(
                        "approved execution event was not admitted"
                    )
            except BaseException:
                if bindings.get(execution_id) == binding:
                    del bindings[execution_id]
                raise
        return dispatch_id

    async def _route_mention_inbox_message_result(
        self,
        message: Any,
        *,
        thread_id: str,
        raw_content: str,
        parent_channel_id: str | None = None,
        check_registered_thread: bool = True,
    ) -> Any | None:
        router = getattr(self, "_mention_inbox_router", None)
        if router is None:
            return None
        from plugins.mention_inbox.router import (
            InboxDiscordMessage,
            InboxRouteResult,
        )

        try:
            surface_checker = getattr(router, "is_agent_surface", None)
            agent_surface = bool(
                surface_checker(thread_id, parent_channel_id)
                if callable(surface_checker)
                else False
            )
        except Exception:
            logger.warning(
                "[%s] Mention-inbox work-surface validation failed closed",
                self.name,
                exc_info=True,
            )
            return InboxRouteResult(True, "agent_surface_validation_failed")
        if agent_surface:
            registered_thread = False
        elif not check_registered_thread:
            return None
        else:
            try:
                registered_thread = bool(router.is_work_thread(thread_id))
            except Exception:
                logger.warning(
                    "[%s] Mention-inbox registered-thread validation failed closed",
                    self.name,
                    exc_info=True,
                )
                return InboxRouteResult(True, "registered_thread_validation_failed")
        if not registered_thread and not agent_surface:
            return None

        reference = getattr(message, "reference", None)
        reply_to = getattr(reference, "message_id", None)
        if reply_to is None:
            reply_to = getattr(getattr(reference, "resolved", None), "id", None)
        user_id = str(getattr(getattr(message, "author", None), "id", ""))
        message_id = str(getattr(message, "id", ""))
        author = getattr(message, "author", None)
        admitted_human = (
            author is not None
            and getattr(author, "bot", None) is False
            and getattr(message, "webhook_id", None) is None
        )
        content = raw_content
        bot_id = getattr(getattr(self, "_client", None), "user", None)
        bot_id = getattr(bot_id, "id", None)
        if bot_id is not None:
            content = content.replace(f"<@!{bot_id}>", f"<@{bot_id}>")
        try:
            return await router.handle_message(
                InboxDiscordMessage(
                    thread_id=thread_id,
                    message_id=message_id,
                    user_id=user_id,
                    text=content,
                    reply_to_message_id=(None if reply_to is None else str(reply_to)),
                    parent_channel_id=parent_channel_id,
                    admitted_human=admitted_human,
                )
            )
        except Exception:
            logger.warning(
                "[%s] Mention-inbox work-thread router failed closed",
                self.name,
                exc_info=True,
            )
            try:
                from . import adapter as adapter_module
                await message.channel.send(
                    "이 work thread의 상태를 확인하지 못해 요청을 실행하지 않았어요. 잠시 뒤 다시 시도해 주세요.",
                    allowed_mentions=adapter_module.discord.AllowedMentions.none(),
                )
            except Exception:
                pass
            return InboxRouteResult(True, "router_failed_closed")

    async def _route_mention_inbox_message(
        self, message: Any, *, thread_id: str, raw_content: str
    ) -> bool:
        """Compatibility wrapper for deterministic handled/not-handled checks."""

        result = await self._route_mention_inbox_message_result(
            message,
            thread_id=thread_id,
            raw_content=raw_content,
        )
        return bool(result is not None and result.handled)
