"""Dedicated routing for registered mention-inbox Discord work threads."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from plugins.mention_inbox.conversation import (
    ReadOnlyConversationResponder,
    build_conversation_context,
    normalize_conversation_response,
)
from plugins.mention_inbox.proposals import (
    ProposalStatus,
    WorkProposal,
    revise_work_proposal,
)
from plugins.mention_inbox.store import MentionInboxStore, ProposalMessageBinding
from plugins.mention_inbox.voice import (
    render_approval_not_enabled,
    render_approval_not_offered,
    render_approval_reference_mismatch,
    render_approval_reply_required,
    render_approval_unauthorized,
    render_conversation_fallback,
    render_proposal,
    render_revision_instruction,
    render_revision_unauthorized,
)

logger = logging.getLogger(__name__)

_APPROVAL_LIKE_COMMANDS = frozenset(
    {
        "승인",
        "승인해",
        "승인해줘",
        "승인해 주세요",
        "승인할게",
        "승인할게요",
        "승인합니다",
    }
)


@dataclass(frozen=True)
class InboxDiscordMessage:
    thread_id: str
    message_id: str
    user_id: str
    text: str
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class InboxRouteResult:
    handled: bool
    kind: str
    proposal: WorkProposal | None = None
    response_message_id: str | None = None


class ProposalReplyTransport(Protocol):
    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None: ...

    async def send_to_thread(
        self,
        thread_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str: ...


def _feedback_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= 300 else text[:299].rstrip() + "…"


class InboxProposalRouter:
    """Keep deterministic work-thread control out of the general agent/tool path."""

    def __init__(
        self,
        *,
        store: MentionInboxStore,
        discord: ProposalReplyTransport,
        bot_mention: str,
        authorized_approver_ids: frozenset[str],
        approval_handler: object | None = None,
        conversation_responder: ReadOnlyConversationResponder | None = None,
    ) -> None:
        self._store = store
        self._discord = discord
        self._bot_mention = bot_mention
        self._authorized_approver_ids = authorized_approver_ids
        self._approval_handler = approval_handler
        self._conversation_responder = conversation_responder

    def _is_exact_approval(self, message: InboxDiscordMessage) -> bool:
        return " ".join(message.text.split()) == f"{self._bot_mention} 승인"

    def _is_approval_like(self, feedback: str) -> bool:
        command = feedback
        if command.startswith(self._bot_mention):
            command = command[len(self._bot_mention) :].strip()
        command = command.rstrip(" \t.!?")
        return command in _APPROVAL_LIKE_COMMANDS

    def _revision_text(self, feedback: str) -> str | None:
        prefix = f"{self._bot_mention} 제안 수정:"
        if not feedback.startswith(prefix):
            return None
        return feedback[len(prefix) :].strip()

    async def _post_notice(
        self, message: InboxDiscordMessage, content: str
    ) -> str:
        # Discord ingress deduplicates replayed message IDs. Content matching
        # here would instead hide legitimate repeated requests from the user.
        return await self._discord.send_to_thread(
            message.thread_id,
            content,
            reply_to_message_id=message.message_id,
        )

    async def _notice_result(
        self,
        message: InboxDiscordMessage,
        *,
        kind: str,
        proposal: WorkProposal,
        content: str,
    ) -> InboxRouteResult:
        response_message_id = await self._post_notice(message, content)
        return InboxRouteResult(True, kind, proposal, response_message_id)

    async def _post_proposal(
        self,
        thread_id: str,
        proposal: WorkProposal,
        *,
        approval_offered: bool,
    ) -> str:
        content = render_proposal(
            proposal,
            self._bot_mention,
            approval_offered=approval_offered,
            approval_unavailable_reason=(
                None if approval_offered else "approval_unavailable"
            ),
        )
        message_id = await self._discord.find_message_content(
            thread_id, content, limit=100
        )
        if message_id is None:
            message_id = await self._discord.send_to_thread(thread_id, content)
        self._store.record_proposal_message(
            proposal.proposal_id,
            proposal.revision,
            message_id,
            approval_offered=approval_offered,
        )
        return message_id

    def is_work_thread(self, thread_id: str) -> bool:
        return self._store.get_work_item_session_by_thread(str(thread_id)) is not None

    async def _route_approval(
        self,
        message: InboxDiscordMessage,
        latest: WorkProposal,
        binding: ProposalMessageBinding | None,
    ) -> InboxRouteResult:
        if message.user_id not in self._authorized_approver_ids:
            return await self._notice_result(
                message,
                kind="approval_unauthorized",
                proposal=latest,
                content=render_approval_unauthorized(),
            )
        if message.reply_to_message_id is None:
            return await self._notice_result(
                message,
                kind="approval_reply_required",
                proposal=latest,
                content=render_approval_reply_required(self._bot_mention),
            )
        if binding is None or message.reply_to_message_id != binding.message_id:
            return await self._notice_result(
                message,
                kind="approval_reference_mismatch",
                proposal=latest,
                content=render_approval_reference_mismatch(self._bot_mention),
            )
        if not binding.approval_offered:
            return await self._notice_result(
                message,
                kind="approval_not_offered",
                proposal=latest,
                content=render_approval_not_offered(),
            )
        handler = self._approval_handler
        if handler is None:
            return await self._notice_result(
                message,
                kind="approval_not_enabled",
                proposal=latest,
                content=render_approval_not_enabled(),
            )
        return await handler.approve(message, latest)

    async def _route_approval_like(
        self,
        message: InboxDiscordMessage,
        latest: WorkProposal,
        binding: ProposalMessageBinding | None,
    ) -> InboxRouteResult:
        if message.user_id not in self._authorized_approver_ids:
            return await self._notice_result(
                message,
                kind="approval_unauthorized",
                proposal=latest,
                content=render_approval_unauthorized(),
            )
        if message.reply_to_message_id is None:
            return await self._notice_result(
                message,
                kind="approval_reply_required",
                proposal=latest,
                content=render_approval_reply_required(self._bot_mention),
            )
        if binding is None or message.reply_to_message_id != binding.message_id:
            return await self._notice_result(
                message,
                kind="approval_reference_mismatch",
                proposal=latest,
                content=render_approval_reference_mismatch(self._bot_mention),
            )
        if not binding.approval_offered:
            return await self._notice_result(
                message,
                kind="approval_not_offered",
                proposal=latest,
                content=render_approval_not_offered(),
            )
        return await self._notice_result(
            message,
            kind="approval_format_invalid",
            proposal=latest,
            content=render_approval_reply_required(self._bot_mention),
        )

    async def _route_revision(
        self,
        message: InboxDiscordMessage,
        latest: WorkProposal,
        binding: ProposalMessageBinding | None,
        revision_text: str,
    ) -> InboxRouteResult:
        if message.user_id not in self._authorized_approver_ids:
            return await self._notice_result(
                message,
                kind="revision_unauthorized",
                proposal=latest,
                content=render_revision_unauthorized(),
            )
        if not revision_text:
            return await self._notice_result(
                message,
                kind="revision_instruction_required",
                proposal=latest,
                content=render_revision_instruction(self._bot_mention),
            )
        if latest.status is not ProposalStatus.PENDING:
            return await self._notice_result(
                message,
                kind="proposal_not_pending",
                proposal=latest,
                content="현재 제안은 대기 상태가 아니라 revision을 바꾸지 않았어요.",
            )

        suffix = f" 사용자 요청 반영: {revision_text}"
        base = latest.goal[: max(1, 500 - len(suffix))].rstrip()
        revised = revise_work_proposal(
            latest,
            source_revision=latest.source_revision,
            head_sha=latest.head_sha,
            goal=f"{base}{suffix}",
            steps=latest.steps,
            allowed_actions=latest.allowed_actions,
            forbidden_actions=latest.forbidden_actions,
            verification=latest.verification,
            executor_hint=latest.executor_hint,
        )
        self._store.create_proposal(revised)
        approval_offered = bool(
            binding is not None
            and binding.approval_offered
            and self._approval_handler is not None
        )
        response_message_id = await self._post_proposal(
            message.thread_id,
            revised,
            approval_offered=approval_offered,
        )
        return InboxRouteResult(
            True,
            "proposal_revised",
            revised,
            response_message_id,
        )

    async def _route_conversation(
        self,
        message: InboxDiscordMessage,
        latest: WorkProposal,
        binding: ProposalMessageBinding | None,
        feedback: str,
    ) -> InboxRouteResult:
        stored = self._store.get(latest.source_dedupe_key)
        context = build_conversation_context(
            stored=stored,
            proposal=latest,
            approval_offered=bool(binding is not None and binding.approval_offered),
            execution_available=self._approval_handler is not None,
        )
        content = ""
        responder = self._conversation_responder
        if responder is not None:
            try:
                content = normalize_conversation_response(
                    await responder.answer(
                        message=feedback,
                        context=context,
                        bot_mention=self._bot_mention,
                    )
                )
            except Exception:
                logger.warning(
                    "Mention-inbox read-only conversation responder failed",
                    exc_info=True,
                )
        kind = "conversation_response"
        if not content:
            kind = "conversation_fallback"
            content = render_conversation_fallback(latest, self._bot_mention)
        return await self._notice_result(
            message,
            kind=kind,
            proposal=latest,
            content=content,
        )

    async def handle_message(self, message: InboxDiscordMessage) -> InboxRouteResult:
        if not isinstance(message, InboxDiscordMessage):
            raise ValueError("message must be an InboxDiscordMessage")
        session = self._store.get_work_item_session_by_thread(message.thread_id)
        if session is None:
            return InboxRouteResult(False, "not_work_thread")
        feedback = _feedback_text(message.text)
        if not feedback:
            return InboxRouteResult(True, "empty_message")

        latest = self._store.get_latest_proposal(session.subject_key)
        if latest is None:
            return InboxRouteResult(True, "proposal_missing")
        binding = self._store.get_proposal_message_binding(
            latest.proposal_id, latest.revision
        )

        if self._is_exact_approval(message):
            return await self._route_approval(message, latest, binding)

        revision_text = self._revision_text(feedback)
        if revision_text is not None:
            return await self._route_revision(
                message, latest, binding, revision_text
            )

        if self._is_approval_like(feedback):
            return await self._route_approval_like(message, latest, binding)

        return await self._route_conversation(
            message, latest, binding, feedback
        )
