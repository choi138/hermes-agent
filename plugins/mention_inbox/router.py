"""Dedicated routing for registered mention-inbox Discord work threads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from plugins.mention_inbox.proposals import WorkProposal, revise_work_proposal
from plugins.mention_inbox.store import MentionInboxStore
from plugins.mention_inbox.voice import render_proposal


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

    async def send_to_thread(self, thread_id: str, content: str) -> str: ...


def _feedback_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= 300 else text[:299].rstrip() + "…"


class InboxProposalRouter:
    """Keep pending thread conversation out of the general agent/tool path."""

    def __init__(
        self,
        *,
        store: MentionInboxStore,
        discord: ProposalReplyTransport,
        bot_mention: str,
        authorized_approver_ids: frozenset[str],
        approval_handler: object | None = None,
    ) -> None:
        self._store = store
        self._discord = discord
        self._bot_mention = bot_mention
        self._authorized_approver_ids = authorized_approver_ids
        self._approval_handler = approval_handler

    def _is_exact_approval(self, message: InboxDiscordMessage) -> bool:
        return " ".join(message.text.split()) == f"{self._bot_mention} 승인"

    async def _post_proposal(self, thread_id: str, proposal: WorkProposal) -> str:
        content = render_proposal(proposal, self._bot_mention)
        message_id = await self._discord.find_message_content(
            thread_id, content, limit=100
        )
        if message_id is None:
            message_id = await self._discord.send_to_thread(thread_id, content)
        self._store.record_proposal_message(
            proposal.proposal_id, proposal.revision, message_id
        )
        return message_id

    def is_work_thread(self, thread_id: str) -> bool:
        return self._store.get_work_item_session_by_thread(str(thread_id)) is not None

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
        proposal_message_id = self._store.get_proposal_message_id(
            latest.proposal_id, latest.revision
        )
        if (
            self._is_exact_approval(message)
            and proposal_message_id is not None
            and message.reply_to_message_id == proposal_message_id
        ):
            handler = self._approval_handler
            if handler is None:
                return InboxRouteResult(True, "approval_not_enabled", latest)
            result = await handler.approve(message, latest)
            return result

        revised = revise_work_proposal(
            latest,
            source_revision=latest.source_revision,
            head_sha=latest.head_sha,
            goal=f"{latest.goal} 사용자 보완 요청: {feedback}",
            steps=latest.steps,
            allowed_actions=latest.allowed_actions,
            forbidden_actions=latest.forbidden_actions,
            verification=latest.verification,
            executor_hint=latest.executor_hint,
        )
        self._store.create_proposal(revised)
        response_message_id = await self._post_proposal(message.thread_id, revised)
        return InboxRouteResult(
            True,
            "proposal_revised",
            revised,
            response_message_id,
        )
