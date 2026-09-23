"""Discord typing loops must remain owned across stop and reconnect."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_stale_typing_cleanup_keeps_replacement_task():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    entered = asyncio.Event()
    old_cancelled = asyncio.Event()
    allow_old_cleanup = asyncio.Event()
    replacement_request = asyncio.Event()
    old_task = None

    async def request(_route):
        if asyncio.current_task() is old_task:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                old_cancelled.set()
                await allow_old_cleanup.wait()
                raise
        replacement_request.set()
        await asyncio.Event().wait()

    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request))
    await adapter.send_typing("123")
    old_task = adapter._typing_tasks["123"]
    await entered.wait()

    # stop_typing removes the old owner before cancellation finishes. A new
    # owner can occupy the same channel while the old task's finally runs.
    stop = asyncio.create_task(adapter.stop_typing("123"))
    await old_cancelled.wait()
    await adapter.send_typing("123")
    replacement = adapter._typing_tasks["123"]
    assert replacement is not old_task
    await replacement_request.wait()
    allow_old_cleanup.set()
    await stop
    assert adapter._typing_tasks["123"] is replacement

    await adapter.stop_typing("123")
    assert adapter._typing_tasks == {}


@pytest.mark.asyncio
async def test_disconnect_cancels_typing_and_rejects_restart():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    entered = asyncio.Event()

    async def request(_route):
        entered.set()

    async def close():
        return None

    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request), close=close)
    await adapter.send_typing("123")
    task = adapter._typing_tasks["123"]
    await entered.wait()
    await adapter.disconnect()

    assert task.done()
    assert adapter._typing_tasks == {}
    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request))
    await adapter.send_typing("123")
    assert adapter._typing_tasks == {}
