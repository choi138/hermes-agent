"""Discord adapter mention-inbox anchored-thread API."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from gateway.platforms.base import Platform
from plugins.mention_inbox.proposals import build_work_proposal
from plugins.mention_inbox.router import InboxProposalRouter, InboxRouteResult
from plugins.mention_inbox.store import MentionInboxStore
from plugins.platforms.discord.adapter import DiscordAdapter


class _Tracker:
    def __init__(self) -> None:
        self.marked: list[str] = []

    def mark(self, thread_id: str) -> None:
        self.marked.append(thread_id)


class _Thread:
    def __init__(self, thread_id: int) -> None:
        self.id = thread_id


class _Message:
    def __init__(self, message_id: int, *, thread: _Thread | None = None) -> None:
        self.id = message_id
        self.thread = thread
        self.created: list[tuple[str, int]] = []

    async def create_thread(self, *, name: str, auto_archive_duration: int):
        self.created.append((name, auto_archive_duration))
        self.thread = _Thread(self.id)
        return self.thread


class _Channel:
    def __init__(self, channel_id: int, message: _Message) -> None:
        self.id = channel_id
        self.message = message
        self.fetches: list[int] = []

    async def fetch_message(self, message_id: int) -> _Message:
        self.fetches.append(message_id)
        assert message_id == self.message.id
        return self.message


class _Client:
    def __init__(self, channel: _Channel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)


def _adapter(message: _Message) -> DiscordAdapter:
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter._client = _Client(_Channel(55, message))
    adapter._threads = _Tracker()
    adapter._mention_inbox_parent_channels = {}
    adapter._mention_inbox_thread_locks = {}
    return adapter


@pytest.mark.asyncio
async def test_create_anchored_thread_reuses_existing_message_thread() -> None:
    message = _Message(99, thread=_Thread(99))
    adapter = _adapter(message)
    adapter.remember_mention_inbox_parent("99", "55")

    found = await adapter.find_anchored_thread("99")
    created = await adapter.create_anchored_thread("99", "work item", 1440)

    assert found == created == "99"
    assert message.created == []


@pytest.mark.asyncio
async def test_create_anchored_thread_creates_once_from_parent_message() -> None:
    message = _Message(99)
    adapter = _adapter(message)
    adapter.remember_mention_inbox_parent("99", "55")

    first = await adapter.create_anchored_thread("99", "work item", 1440)
    second = await adapter.create_anchored_thread("99", "work item", 1440)

    assert first == second == "99"
    assert message.created == [("work item", 1440)]


def test_mark_thread_participation_uses_existing_tracker() -> None:
    adapter = _adapter(_Message(99))
    adapter.mark_mention_inbox_thread_participation("99")
    assert adapter._threads.marked == ["99"]


def test_parent_mapping_and_archive_duration_fail_closed() -> None:
    adapter = _adapter(_Message(99))
    with pytest.raises(ValueError):
        adapter.remember_mention_inbox_parent("not-an-id", "55")
    adapter.remember_mention_inbox_parent("99", "55")

    async def invalid() -> None:
        await adapter.create_anchored_thread("99", "work item", 123)

    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(invalid())


class _Router:
    def __init__(self, *, registered: bool = True) -> None:
        self.registered = registered
        self.messages = []

    def is_work_thread(self, thread_id: str) -> bool:
        return self.registered and thread_id == "99"

    async def handle_message(self, message):
        self.messages.append(message)
        return SimpleNamespace(handled=True)


@pytest.mark.asyncio
async def test_registered_thread_reply_is_routed_with_exact_reply_target() -> None:
    adapter = _adapter(_Message(99))
    adapter._client.user = SimpleNamespace(id=777)
    router = _Router()
    adapter.set_mention_inbox_router(router)
    raw = SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=456),
        reference=SimpleNamespace(message_id=321),
        channel=SimpleNamespace(),
    )

    handled = await adapter._route_mention_inbox_message(
        raw,
        thread_id="99",
        raw_content="<@!777> 승인",
    )

    assert handled is True
    assert len(router.messages) == 1
    envelope = router.messages[0]
    assert envelope.thread_id == "99"
    assert envelope.message_id == "123"
    assert envelope.user_id == "456"
    assert envelope.reply_to_message_id == "321"
    assert envelope.text == "<@777> 승인"


@pytest.mark.asyncio
async def test_unregistered_thread_reply_falls_through() -> None:
    adapter = _adapter(_Message(99))
    adapter.set_mention_inbox_router(_Router(registered=False))
    raw = SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=456),
        reference=None,
        channel=SimpleNamespace(),
    )
    assert (
        await adapter._route_mention_inbox_message(
            raw, thread_id="99", raw_content="일반 대화"
        )
        is False
    )


@pytest.mark.asyncio
async def test_approved_execution_is_admitted_as_internal_thread_event() -> None:
    message = _Message(99, thread=_Thread(99))
    adapter = _adapter(message)
    thread = adapter._client.channel
    thread.name = "approved work"
    thread.guild = SimpleNamespace(id=42, name="86--EIGHTY-SIX")
    thread.parent = SimpleNamespace(id=55, topic="work inbox")
    admitted_events = []

    async def admit(event):
        admitted_events.append(event)
        return True

    adapter.handle_message = admit
    request = SimpleNamespace(
        execution_id="wx_123",
        proposal_hash="a" * 64,
        executor_hint="direct",
        approval_message_id="555",
        approver_user_id="456",
        thread_id="55",
    )

    dispatch_id = await adapter.enqueue_mention_inbox_execution(
        request, "approved execution envelope"
    )

    assert dispatch_id == "direct:wx_123"
    assert len(admitted_events) == 1
    event = admitted_events[0]
    assert event.text == "approved execution envelope"
    assert event.internal is True
    assert event.raw_message is None
    assert event.source.chat_id == "55"
    assert event.source.thread_id == "55"
    assert event.source.user_id == "456"
    assert event.source.message_id == "555"
    assert event.metadata == {
        "mention_inbox_execution": {
            "execution_id": "wx_123",
            "proposal_hash": "a" * 64,
            "mode": "direct",
        }
    }

class _ProposalTransport:
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
        message_id = f"notice-{len(self.messages) + 1}"
        self.messages.append((message_id, content))
        self.reply_to_message_ids.append(reply_to_message_id)
        return message_id


class _ApprovalHandler:
    def __init__(self) -> None:
        self.calls = []

    async def approve(self, message, proposal) -> InboxRouteResult:
        self.calls.append((message, proposal))
        return InboxRouteResult(True, "approval_queued", proposal)


def _real_router(tmp_path, transport, handler):
    subject = "github:R_repo:PR_7"
    store = MentionInboxStore(tmp_path / "inbox.db")
    store.reserve_work_item_session(
        subject, "github:RC_123:U_recent", "2026-07-29T10:01:00Z"
    )
    store.record_work_item_thread(subject, "parent-1", "99")
    proposal = build_work_proposal(
        revision=1,
        source_dedupe_key="github:RC_123:U_recent",
        source_revision="2026-07-29T10:01:00Z",
        subject_key=subject,
        head_sha="head-1",
        goal="리뷰 의견을 확인한다.",
        steps=("diff를 확인한다.",),
        allowed_actions=("read_repository", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과",),
        executor_hint="direct",
    )
    store.create_proposal(proposal)
    store.record_proposal_message(
        proposal.proposal_id,
        proposal.revision,
        "321",
        approval_offered=True,
    )
    router = InboxProposalRouter(
        store=store,
        discord=transport,
        bot_mention="<@777>",
        authorized_approver_ids=frozenset({"456"}),
        approval_handler=handler,
    )
    return store, proposal, router


@pytest.mark.asyncio
async def test_adapter_reference_reaches_real_router_and_exact_handler(tmp_path) -> None:
    adapter = _adapter(_Message(99))
    adapter._client.user = SimpleNamespace(id=777)
    transport = _ProposalTransport()
    handler = _ApprovalHandler()
    store, proposal, router = _real_router(tmp_path, transport, handler)
    adapter.set_mention_inbox_router(router)
    raw = SimpleNamespace(
        id=123,
        author=SimpleNamespace(id=456),
        reference=SimpleNamespace(message_id=321),
        channel=SimpleNamespace(),
    )

    handled = await adapter._route_mention_inbox_message(
        raw,
        thread_id="99",
        raw_content="<@!777> 승인",
    )

    assert handled is True
    assert len(handler.calls) == 1
    envelope, routed_proposal = handler.calls[0]
    assert envelope.reply_to_message_id == "321"
    assert envelope.text == "<@777> 승인"
    assert routed_proposal == proposal
    assert store.get_latest_proposal(proposal.subject_key).revision == 1
    assert transport.messages == []


@pytest.mark.asyncio
async def test_adapter_wrong_reference_is_visible_without_revision(tmp_path) -> None:
    adapter = _adapter(_Message(99))
    adapter._client.user = SimpleNamespace(id=777)
    transport = _ProposalTransport()
    handler = _ApprovalHandler()
    store, proposal, router = _real_router(tmp_path, transport, handler)
    adapter.set_mention_inbox_router(router)
    raw = SimpleNamespace(
        id=124,
        author=SimpleNamespace(id=456),
        reference=SimpleNamespace(
            message_id=None,
            resolved=SimpleNamespace(id=999),
        ),
        channel=SimpleNamespace(),
    )

    handled = await adapter._route_mention_inbox_message(
        raw,
        thread_id="99",
        raw_content="<@777> 승인",
    )

    assert handled is True
    assert handler.calls == []
    assert store.get_latest_proposal(proposal.subject_key).revision == 1
    assert "최신 제안 메시지와 일치하지 않아요" in transport.messages[-1][1]


@pytest.mark.asyncio
async def test_adapter_distinct_questions_receive_distinct_replies(tmp_path) -> None:
    adapter = _adapter(_Message(99))
    adapter._client.user = SimpleNamespace(id=777)
    transport = _ProposalTransport()
    handler = _ApprovalHandler()
    store, proposal, router = _real_router(tmp_path, transport, handler)
    adapter.set_mention_inbox_router(router)
    first = SimpleNamespace(
        id=124,
        author=SimpleNamespace(id=456),
        reference=None,
        channel=SimpleNamespace(),
    )
    second = SimpleNamespace(
        id=125,
        author=SimpleNamespace(id=456),
        reference=None,
        channel=SimpleNamespace(),
    )

    first_handled = await adapter._route_mention_inbox_message(
        first,
        thread_id="99",
        raw_content="그 코멘트가 정확히 뭐야?",
    )
    second_handled = await adapter._route_mention_inbox_message(
        second,
        thread_id="99",
        raw_content="그 코멘트가 정확히 뭐야?",
    )

    assert first_handled is second_handled is True
    assert len(transport.messages) == 2
    assert transport.messages[0][1] == transport.messages[1][1]
    assert transport.reply_to_message_ids == ["124", "125"]
    assert store.get_latest_proposal(proposal.subject_key).revision == 1
    assert handler.calls == []
