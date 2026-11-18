"""Discord processing reactions: 👀 while a message is handled, then ✅ or ❌ by its outcome."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.platforms.event import MessageEvent, ProcessingOutcome

logger = logging.getLogger(__name__)


class DiscordReactionsMixin:
    name: str
    _client: Any
    _extra_or_env_flag: Any
    _resolve_channel: Any
    _record_discord_processing_start: Any
    _record_discord_processing_complete: Any
    _record_discord_covered_complete: Any
    _missed_message_backfill_enabled: Any
    _run_processing_hook: Any
    _utc_now_iso: Any
    _background_tasks: Any
    _processing_settlement_tasks: Any

    async def _add_reaction(self, message: Any, emoji: str) -> bool:
        """Add an emoji reaction to a Discord message."""
        if not message or not hasattr(message, "add_reaction"):
            return False
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            logger.debug("[%s] add_reaction failed (%s): %s", self.name, emoji, e)
            return False

    async def _remove_reaction(self, message: Any, emoji: str) -> bool:
        """Remove the bot's own emoji reaction from a Discord message."""
        if not message or not hasattr(message, "remove_reaction") or not self._client or not self._client.user:
            return False
        try:
            await message.remove_reaction(emoji, self._client.user)
            return True
        except Exception as e:
            logger.debug("[%s] remove_reaction failed (%s): %s", self.name, emoji, e)
            return False

    def _reactions_enabled(self) -> bool:
        """Reactions enabled via ``extra.reactions`` (YAML, per profile) or ``DISCORD_REACTIONS``."""
        return self._extra_or_env_flag("reactions", "DISCORD_REACTIONS", "true", truthy=False)

    async def _reaction_target(self, event: MessageEvent) -> Any:
        """The message to react to: the live one, or a partial message built from the event's ids when
        it has none (a queued message restored from the session queue), best effort."""
        message = event.raw_message
        if message is not None or not self._client:
            return message
        source = event.source
        # An auto-thread's starter message lives in the parent channel, not in the thread.
        channel_id = source.parent_chat_id if source.auto_thread_created else source.chat_id
        try:
            channel = await self._resolve_channel(channel_id)
            return channel.get_partial_message(int(event.message_id))
        except Exception:
            logger.debug("[%s] No reaction target for message %s", self.name, event.message_id, exc_info=True)
            return None

    def on_message_absorbed(self, event: MessageEvent) -> None:
        if not self._missed_message_backfill_enabled():
            return
        absorbed_at = self._utc_now_iso()
        for covered in (event, *event.absorbed_events):
            if getattr(covered, '_discord_absorption_task', None) is not None:
                continue
            task = asyncio.create_task(self._run_processing_hook(
                'on_processing_covered', covered, absorbed_at=absorbed_at,
            ))
            covered._discord_absorption_task = task
            self._background_tasks.add(task)
            self._processing_settlement_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._processing_settlement_tasks.discard)

    async def on_processing_covered(
        self, event: MessageEvent, outcome: Optional[ProcessingOutcome] = None, *,
        absorbed_at: Optional[str] = None,
    ) -> None:
        if not self._missed_message_backfill_enabled():
            return
        if outcome is None:
            await asyncio.to_thread(
                self._record_discord_processing_start, event, emoji_ack=False, absorbed_at=absorbed_at,
            )
        else:
            claim = getattr(event, '_discord_absorption_task', None)
            if claim is not None:
                await asyncio.shield(claim)
            await asyncio.to_thread(self._record_discord_covered_complete, event, outcome)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction and record durable handling state."""
        event._discord_coverage_completion_task = None
        acked = False
        if self._reactions_enabled():
            message = await self._reaction_target(event)
            if hasattr(message, "add_reaction"):
                acked = await self._add_reaction(message, "👀")
        await asyncio.to_thread(self._record_discord_processing_start, event, emoji_ack=acked)
        for covered in event.absorbed_events:
            await self._run_processing_hook('on_processing_covered', covered)

    def select_processing_coverage(self, event: MessageEvent, outcome: ProcessingOutcome):
        if not self._missed_message_backfill_enabled():
            return
        task = getattr(event, '_discord_coverage_completion_task', None)
        if task is None:
            covered_events = tuple(event.absorbed_events)

            async def complete():
                for covered in covered_events:
                    await self._run_processing_hook('on_processing_covered', covered, outcome)

            task = asyncio.create_task(complete())
            event._discord_coverage_completion_task = task
            self._background_tasks.add(task)
            self._processing_settlement_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._processing_settlement_tasks.discard)
        return task

    async def on_processing_coverage_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        task = self.select_processing_coverage(event, outcome)
        if task is not None:
            await asyncio.shield(task)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for final reaction and durable state."""
        await self.on_processing_coverage_complete(event, outcome)
        await asyncio.to_thread(self._record_discord_processing_complete, event, outcome)
        if not self._reactions_enabled():
            return
        message = await self._reaction_target(event)
        if hasattr(message, "add_reaction"):
            await self._remove_reaction(message, "👀")
            if outcome == ProcessingOutcome.SUCCESS:
                await self._add_reaction(message, "✅")
            elif outcome == ProcessingOutcome.FAILURE:
                await self._add_reaction(message, "❌")
