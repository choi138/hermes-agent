"""Pending work-thread reply routing without tools or execution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox.proposals import ProposalStatus, build_work_proposal
from plugins.mention_inbox.router import InboxDiscordMessage, InboxProposalRouter
from plugins.mention_inbox.store import MentionInboxStore

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
SUBJECT = "github:R_repo:PR_7"
BOT_MENTION = "<@1525050525641805886>"
USER = "396159160201658368"


class _Discord:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        for message_id, existing in self.messages[-limit:]:
            if existing == content:
                return message_id
        return None

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        message_id = f"bot-{len(self.messages) + 1}"
        self.messages.append((message_id, content))
        return message_id


def _seed(path: Path) -> tuple[MentionInboxStore, object]:
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.reserve_work_item_session(
        SUBJECT, "github:RC_123:U_recent", "2026-07-29T10:01:00Z"
    )
    store.record_work_item_thread(SUBJECT, "parent-1", "thread-1")
    proposal = build_work_proposal(
        revision=1,
        source_dedupe_key="github:RC_123:U_recent",
        source_revision="2026-07-29T10:01:00Z",
        subject_key=SUBJECT,
        head_sha="head-1",
        goal="리뷰 의견을 확인한다.",
        steps=("diff를 확인한다.",),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과",),
        executor_hint="direct",
    )
    store.create_proposal(proposal)
    store.record_proposal_message(proposal.proposal_id, 1, "proposal-message-1")
    return store, proposal


def _message(
    text: str,
    *,
    message_id: str = "user-message-1",
    thread_id: str = "thread-1",
    reply_to: str | None = None,
) -> InboxDiscordMessage:
    return InboxDiscordMessage(
        thread_id=thread_id,
        message_id=message_id,
        user_id=USER,
        text=text,
        reply_to_message_id=reply_to,
    )


@pytest.mark.asyncio
async def test_ordinary_reply_revises_pending_proposal_without_execution(
    tmp_path: Path,
) -> None:
    store, first = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    router = InboxProposalRouter(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({USER}),
    )

    result = await router.handle_message(_message("테스트 범위에 API 회귀도 포함해줘"))

    latest = store.get_latest_proposal(SUBJECT)
    assert result.handled is True
    assert result.kind == "proposal_revised"
    assert latest is not None and latest.revision == 2
    assert latest.status is ProposalStatus.PENDING
    assert "API 회귀" in latest.goal
    assert first.status is ProposalStatus.PENDING
    assert len(discord.messages) == 1
    assert not hasattr(discord, "run_agent")
    assert not hasattr(discord, "execute")


@pytest.mark.asyncio
async def test_non_reply_approval_text_is_feedback_not_approval(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    router = InboxProposalRouter(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({USER}),
    )

    result = await router.handle_message(_message(f"{BOT_MENTION} 승인"))

    assert result.kind == "proposal_revised"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_unregistered_thread_is_not_intercepted(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    router = InboxProposalRouter(
        store=store,
        discord=_Discord(),
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({USER}),
    )

    result = await router.handle_message(
        _message("일반 대화", thread_id="another-thread")
    )
    assert result.handled is False
    assert result.kind == "not_work_thread"


@pytest.mark.asyncio
async def test_empty_reply_is_handled_without_revision(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    router = InboxProposalRouter(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({USER}),
    )
    result = await router.handle_message(_message("   "))
    assert result.handled is True
    assert result.kind == "empty_message"
    assert store.get_latest_proposal(SUBJECT).revision == 1
