"""Discord adapter mention-inbox anchored-thread API."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from gateway.platforms.base import Platform
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
