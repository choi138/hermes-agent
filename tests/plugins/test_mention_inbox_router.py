"""Deterministic work-thread routing for approval, revision, and questions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox.proposals import ProposalStatus, build_work_proposal
from plugins.mention_inbox.router import (
    InboxDiscordMessage,
    InboxProposalRouter,
    InboxRouteResult,
)
from plugins.mention_inbox.store import MentionInboxStore

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
SUBJECT = "github:R_repo:PR_7"
BOT_MENTION = "<@1525050525641805886>"
USER = "396159160201658368"
OTHER_USER = "123456789012345678"


class _Discord:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.reply_to_message_ids: list[str | None] = []

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        for message_id, existing in self.messages[-limit:]:
            if existing == content:
                return message_id
        return None

    async def send_to_thread(
        self,
        thread_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        message_id = f"bot-{len(self.messages) + 1}"
        self.messages.append((message_id, content))
        self.reply_to_message_ids.append(reply_to_message_id)
        return message_id


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[InboxDiscordMessage, object]] = []

    async def approve(self, message, proposal) -> InboxRouteResult:
        self.calls.append((message, proposal))
        return InboxRouteResult(True, "approval_queued", proposal, "queued-message")


def _seed(
    path: Path, *, approval_offered: bool = True
) -> tuple[MentionInboxStore, object]:
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
    store.record_proposal_message(
        proposal.proposal_id,
        1,
        "proposal-message-1",
        approval_offered=approval_offered,
    )
    return store, proposal


def _message(
    text: str,
    *,
    message_id: str = "user-message-1",
    thread_id: str = "thread-1",
    reply_to: str | None = None,
    user_id: str = USER,
) -> InboxDiscordMessage:
    return InboxDiscordMessage(
        thread_id=thread_id,
        message_id=message_id,
        user_id=user_id,
        text=text,
        reply_to_message_id=reply_to,
    )


def _router(
    store: MentionInboxStore,
    discord: _Discord,
    *,
    handler: object | None = None,
) -> InboxProposalRouter:
    return InboxProposalRouter(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({USER}),
        approval_handler=handler,
    )


@pytest.mark.asyncio
async def test_only_explicit_revision_command_creates_new_proposal(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    handler = _Handler()

    result = await _router(store, discord, handler=handler).handle_message(
        _message(f"{BOT_MENTION} 제안 수정: 테스트 범위에 API 회귀도 포함해줘")
    )

    latest = store.get_latest_proposal(SUBJECT)
    assert result.kind == "proposal_revised"
    assert latest is not None and latest.revision == 2
    assert latest.status is ProposalStatus.PENDING
    assert "API 회귀" in latest.goal
    binding = store.get_proposal_message_binding(latest.proposal_id, latest.revision)
    assert binding is not None and binding.approval_offered is True
    assert f"{BOT_MENTION} 승인" in discord.messages[-1][1]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_ordinary_message_does_not_revise_and_explains_revision_command(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()

    result = await _router(store, discord).handle_message(
        _message("테스트 범위에 API 회귀도 포함해줘")
    )

    assert result.kind == "proposal_instruction_required"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "제안 수정:" in discord.messages[-1][1]


@pytest.mark.asyncio
async def test_question_does_not_revise_proposal(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()

    result = await _router(store, discord).handle_message(
        _message("그 코멘트가 정확히 뭐야?")
    )

    assert result.kind == "proposal_question"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "질문으로 확인했어요" in discord.messages[-1][1]


@pytest.mark.asyncio
async def test_distinct_messages_with_same_question_each_receive_reply(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    router = _router(store, discord)

    first = await router.handle_message(
        _message("그 코멘트가 정확히 뭐야?", message_id="user-message-1")
    )
    second = await router.handle_message(
        _message("그 코멘트가 정확히 뭐야?", message_id="user-message-2")
    )

    assert first.response_message_id == "bot-1"
    assert second.response_message_id == "bot-2"
    assert len(discord.messages) == 2
    assert discord.messages[0][1] == discord.messages[1][1]
    assert discord.reply_to_message_ids == ["user-message-1", "user-message-2"]
    assert store.get_latest_proposal(SUBJECT).revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        f"{BOT_MENTION} 승인",
        "승인",
        "승인할게요",
    ),
)
async def test_non_reply_approval_like_text_never_becomes_feedback(
    tmp_path: Path,
    text: str,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    handler = _Handler()

    result = await _router(store, discord, handler=handler).handle_message(
        _message(text)
    )

    assert result.kind == "approval_reply_required"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "최신 제안 메시지에 답장" in discord.messages[-1][1]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_wrong_reply_reference_is_visible_and_does_not_revise(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    handler = _Handler()

    result = await _router(store, discord, handler=handler).handle_message(
        _message(f"{BOT_MENTION} 승인", reply_to="older-proposal-message")
    )

    assert result.kind == "approval_reference_mismatch"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "최신 제안 메시지와 일치하지 않아요" in discord.messages[-1][1]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_review_only_proposal_rejects_exact_reply_before_handler(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db", approval_offered=False)
    discord = _Discord()
    handler = _Handler()

    result = await _router(store, discord, handler=handler).handle_message(
        _message(f"{BOT_MENTION} 승인", reply_to="proposal-message-1")
    )

    assert result.kind == "approval_not_offered"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "검토용으로 게시돼 승인할 수 없어요" in discord.messages[-1][1]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_distinct_review_only_approval_attempts_each_receive_notice(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db", approval_offered=False)
    discord = _Discord()
    handler = _Handler()
    router = _router(store, discord, handler=handler)

    first = await router.handle_message(
        _message(
            f"{BOT_MENTION} 승인",
            message_id="approval-message-1",
            reply_to="proposal-message-1",
        )
    )
    second = await router.handle_message(
        _message(
            f"{BOT_MENTION} 승인",
            message_id="approval-message-2",
            reply_to="proposal-message-1",
        )
    )

    assert first.kind == second.kind == "approval_not_offered"
    assert first.response_message_id == "bot-1"
    assert second.response_message_id == "bot-2"
    assert discord.reply_to_message_ids == [
        "approval-message-1",
        "approval-message-2",
    ]
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert handler.calls == []


@pytest.mark.asyncio
async def test_absent_handler_returns_visible_execution_unavailable(
    tmp_path: Path,
) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()

    result = await _router(store, discord).handle_message(
        _message(f"{BOT_MENTION} 승인", reply_to="proposal-message-1")
    )

    assert result.kind == "approval_not_enabled"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "실행 기능이 현재 꺼져 있어" in discord.messages[-1][1]


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_approve_or_revise(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    handler = _Handler()

    result = await _router(store, discord, handler=handler).handle_message(
        _message(
            f"{BOT_MENTION} 승인",
            reply_to="proposal-message-1",
            user_id=OTHER_USER,
        )
    )

    assert result.kind == "approval_unauthorized"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert "승인할 권한이 없어요" in discord.messages[-1][1]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_exact_authorized_reply_routes_to_handler(tmp_path: Path) -> None:
    store, proposal = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    handler = _Handler()
    message = _message(f"{BOT_MENTION} 승인", reply_to="proposal-message-1")

    result = await _router(store, discord, handler=handler).handle_message(message)

    assert result.kind == "approval_queued"
    assert handler.calls == [(message, proposal)]
    assert discord.messages == []


@pytest.mark.asyncio
async def test_unregistered_thread_is_not_intercepted(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    router = _router(store, _Discord())

    result = await router.handle_message(
        _message("일반 대화", thread_id="another-thread")
    )
    assert result.handled is False
    assert result.kind == "not_work_thread"


@pytest.mark.asyncio
async def test_empty_reply_is_handled_without_revision(tmp_path: Path) -> None:
    store, _ = _seed(tmp_path / "inbox.db")
    discord = _Discord()
    result = await _router(store, discord).handle_message(_message("   "))
    assert result.handled is True
    assert result.kind == "empty_message"
    assert store.get_latest_proposal(SUBJECT).revision == 1
    assert discord.messages == []
