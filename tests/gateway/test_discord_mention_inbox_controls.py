from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.mention_inbox.router import InboxRouteResult
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_proposal_controls_render_and_route_exact_revision() -> None:
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    response = SimpleNamespace(defer=AsyncMock())

    async def route_after_acknowledgement(**_kwargs) -> InboxRouteResult:
        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        return InboxRouteResult(True, "approval_queued")

    router = SimpleNamespace(
        handle_action=AsyncMock(side_effect=route_after_acknowledgement)
    )
    message = SimpleNamespace(id=555, edit=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client
    adapter._mention_inbox_router = router

    result = await adapter.send_mention_inbox_proposal(
        "123456",
        "현재 요청\n리뷰를 반영해 주세요.",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        approval_offered=True,
    )

    assert result.success is True
    view = channel.send.await_args.kwargs["view"]
    labels = [child.label for child in view.children]
    assert labels == ["수정 시작", "저장된 근거 보기", "나중에"]
    assert view.timeout is None
    assert [child.custom_id for child in view.children] == [
        "mention-inbox:start:wp_478607ef97c9b4875e53aecf:3",
        "mention-inbox:inspect:wp_478607ef97c9b4875e53aecf:3",
        "mention-inbox:later:wp_478607ef97c9b4875e53aecf:3",
    ]

    interaction = SimpleNamespace(
        id=777,
        user=SimpleNamespace(id=42),
        message=message,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.children[0].callback(interaction)

    router.handle_action.assert_awaited_once_with(
        thread_id="123456",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        proposal_message_id="555",
        user_id="42",
        interaction_id="777",
        action="start",
    )
    assert all(child.disabled for child in view.children)
    message.edit.assert_awaited_once_with(view=view)
    interaction.followup.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_only_proposal_omits_start_control() -> None:
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._mention_inbox_router = SimpleNamespace(handle_action=AsyncMock())
    message = SimpleNamespace(id=555)
    channel = SimpleNamespace(send=AsyncMock(return_value=message))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client

    result = await adapter.send_mention_inbox_proposal(
        "123456",
        "현재 요청\n저장된 근거만 볼 수 있어요.",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        approval_offered=False,
    )

    assert result.success is True
    view = channel.send.await_args.kwargs["view"]
    assert [child.label for child in view.children] == ["저장된 근거 보기", "나중에"]


@pytest.mark.asyncio
async def test_reconstructed_adapter_registers_and_routes_existing_control() -> None:
    binding = SimpleNamespace(
        thread_id="123456",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        proposal_message_id="555",
        approval_offered=True,
    )
    response = SimpleNamespace(defer=AsyncMock())

    async def route_after_acknowledgement(**_kwargs) -> InboxRouteResult:
        response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        return InboxRouteResult(True, "proposal_deferred")

    router = SimpleNamespace(
        persistent_control_bindings=MagicMock(return_value=(binding,)),
        handle_action=AsyncMock(side_effect=route_after_acknowledgement),
    )
    client = MagicMock()
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="test-token", extra={})
    )
    adapter._client = client

    adapter.set_mention_inbox_router(router)

    client.add_view.assert_called_once()
    view = client.add_view.call_args.args[0]
    assert client.add_view.call_args.kwargs == {"message_id": 555}
    assert view.timeout is None
    interaction = SimpleNamespace(
        id=888,
        user=SimpleNamespace(id=42),
        message=SimpleNamespace(id=555, edit=AsyncMock()),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )
    later = next(child for child in view.children if ":later:" in child.custom_id)
    await later.callback(interaction)

    router.handle_action.assert_awaited_once_with(
        thread_id="123456",
        proposal_id="wp_478607ef97c9b4875e53aecf",
        proposal_revision=3,
        proposal_message_id="555",
        user_id="42",
        interaction_id="888",
        action="later",
    )
