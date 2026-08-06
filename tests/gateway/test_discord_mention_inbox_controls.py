from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_proposal_carries_no_interactive_controls() -> None:
    """The proposal message is text-only.

    The inspect/later buttons were removed: they duplicated what the thread
    already says, and approving is deliberately text-only so a stray click can
    never commit and push. Attaching no view keeps the rendered body — the key
    that ``find_message_content`` matches on for crash recovery — unchanged.
    """

    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    message = SimpleNamespace(id=555)
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client
    adapter._mention_inbox_router = SimpleNamespace()

    result = await adapter.send_mention_inbox_proposal(
        "123456",
        "현재 요청\n리뷰를 반영해 주세요.",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        approval_offered=True,
    )

    assert result.success is True
    kwargs = channel.send.await_args.kwargs
    assert "view" not in kwargs
    assert kwargs["content"]
    assert kwargs["nonce"]


@pytest.mark.asyncio
async def test_installing_router_registers_no_persistent_view() -> None:
    """No control survives a restart because no control is created."""

    binding = SimpleNamespace(
        thread_id="123456",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        proposal_message_id="555",
        approval_offered=True,
    )
    router = SimpleNamespace(
        persistent_control_bindings=MagicMock(return_value=(binding,)),
        handle_action=AsyncMock(),
    )
    client = MagicMock()
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._client = client

    adapter.set_mention_inbox_router(router)

    assert adapter._mention_inbox_router is router
    client.add_view.assert_not_called()
    router.persistent_control_bindings.assert_not_called()
