"""Behavioral coverage for Discord's persistent typing-task ownership.

The tests import and execute the real ``DiscordAdapter`` implementation.  Only
Discord's HTTP transport and the long refresh sleep are controlled so task
cancellation and event-loop scheduling remain real.
"""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

import plugins.platforms.discord.adapter as discord_platform
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


async def _eventually(predicate, *, turns: int = 100) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


class _ControlledSleeps:
    """Hold adapter refresh sleeps until a test explicitly renews a task."""

    def __init__(self) -> None:
        self.waiters: list[tuple[asyncio.Task, float, asyncio.Event]] = []

    async def sleep(self, delay: float) -> None:
        task = asyncio.current_task()
        assert task is not None
        release = asyncio.Event()
        self.waiters.append((task, delay, release))
        await release.wait()

    def release(self, task: asyncio.Task) -> None:
        for waiting_task, _delay, release in reversed(self.waiters):
            if waiting_task is task and not release.is_set():
                release.set()
                return
        raise AssertionError("task has no controlled refresh sleep")


class _AsyncioProxy:
    """Override only sleep while preserving real asyncio task semantics."""

    def __init__(self, sleeps: _ControlledSleeps) -> None:
        self._sleeps = sleeps

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        await self._sleeps.sleep(delay)


def _typing_adapter(monkeypatch, *, request_effect=None):
    sleeps = _ControlledSleeps()
    monkeypatch.setattr(discord_platform, "asyncio", _AsyncioProxy(sleeps))

    class _Route:
        def __init__(self, method, path, *, channel_id):
            self.method = method
            self.path = path
            self.channel_id = channel_id

    monkeypatch.setattr(discord_platform.discord.http, "Route", _Route)

    requests: list[tuple[str, str, str]] = []

    async def request(route):
        requests.append((route.method, route.path, route.channel_id))
        if request_effect is not None:
            await request_effect(route)

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._client = SimpleNamespace(http=SimpleNamespace(request=request))
    return adapter, sleeps, requests


@pytest.mark.asyncio
async def test_old_stop_cannot_orphan_restarted_typing_task(monkeypatch):
    """An old owner's cleanup must not untrack a new owner on the same thread.

    The stop and restart are queued in that order while the old loop is asleep.
    This reproduces the real event-loop interleave where ``stop_typing`` removes
    and cancels the old task, the new owner installs its task, and the old
    task's ``finally`` then runs.  If that ``finally`` blindly pops by channel,
    the new task survives untracked and posts again after its owner finishes.
    """

    adapter, sleeps, requests = _typing_adapter(monkeypatch)
    channel_id = "987654321"
    replacement = None

    try:
        await adapter.send_typing(channel_id)
        old_task = adapter._typing_tasks[channel_id]
        await _eventually(lambda: len(requests) == 1 and bool(sleeps.waiters))

        replacement_box = []

        async def restart_for_new_owner() -> None:
            await adapter.send_typing(channel_id)
            replacement_box.append(adapter._typing_tasks[channel_id])

        old_stop = asyncio.create_task(adapter.stop_typing(channel_id))
        new_start = asyncio.create_task(restart_for_new_owner())
        await asyncio.gather(old_stop, new_start)

        replacement = replacement_box[0]
        assert replacement is not old_task
        await _eventually(lambda: len(requests) == 2)

        # The new owner completes. Its stop must still be able to find and
        # cancel the replacement before another Discord refresh is emitted.
        await adapter.stop_typing(channel_id)
        sleeps.release(replacement)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        expected_request = (
            "POST",
            "/channels/{channel_id}/typing",
            channel_id,
        )
        assert requests == [expected_request, expected_request], (
            "an untracked typing loop posted after its owner completed"
        )
        assert replacement.done()
        assert channel_id not in adapter._typing_tasks
    finally:
        if replacement is not None and not replacement.done():
            replacement.cancel()
            with suppress(asyncio.CancelledError):
                await replacement


@pytest.mark.asyncio
async def test_stopping_one_channel_preserves_another_active_channel(monkeypatch):
    """Typing ownership and renewable activity remain isolated by channel."""

    adapter, sleeps, requests = _typing_adapter(monkeypatch)
    stopped_channel = "111111111"
    active_channel = "222222222"

    await adapter.send_typing(stopped_channel)
    await adapter.send_typing(active_channel)
    stopped_task = adapter._typing_tasks[stopped_channel]
    active_task = adapter._typing_tasks[active_channel]
    await _eventually(lambda: len(requests) == 2)

    await adapter.stop_typing(stopped_channel)
    assert stopped_task.done()
    assert adapter._typing_tasks == {active_channel: active_task}

    # Releasing several refresh intervals models an arbitrarily long active
    # turn: activity remains renewable rather than expiring at a fixed age.
    for expected_count in range(3, 6):
        sleeps.release(active_task)
        await _eventually(lambda: len(requests) == expected_count)

    expected_stopped = (
        "POST",
        "/channels/{channel_id}/typing",
        stopped_channel,
    )
    expected_active = (
        "POST",
        "/channels/{channel_id}/typing",
        active_channel,
    )
    assert requests.count(expected_stopped) == 1
    assert requests.count(expected_active) == 4

    await adapter.stop_typing(active_channel)
    assert active_task.done()
    assert adapter._typing_tasks == {}


@pytest.mark.asyncio
async def test_disconnect_cancels_typing_before_client_can_be_replaced(monkeypatch):
    """A disconnected adapter must not carry an old typing owner into reconnect."""

    adapter, sleeps, requests = _typing_adapter(monkeypatch)
    channel_id = "333333333"

    async def close_client() -> None:
        return None

    adapter._client.close = close_client

    await adapter.send_typing(channel_id)
    typing_task = adapter._typing_tasks[channel_id]
    await _eventually(lambda: len(requests) == 1)

    await adapter.disconnect()

    replacement_requests = []

    async def replacement_request(route):
        replacement_requests.append((route.method, route.path, route.channel_id))

    adapter._client = SimpleNamespace(
        http=SimpleNamespace(request=replacement_request),
    )
    await adapter.send_typing("555555555")
    assert "555555555" not in adapter._typing_tasks
    assert replacement_requests == []

    adapter._disconnecting = False
    if not typing_task.done():
        sleeps.release(typing_task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    try:
        assert typing_task.done()
        assert channel_id not in adapter._typing_tasks
        assert replacement_requests == []
    finally:
        if not typing_task.done():
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task


@pytest.mark.asyncio
async def test_stop_cancels_typing_during_rate_limit_backoff(monkeypatch):
    """A 429 backoff remains cancellable and cannot emit after completion."""

    class RateLimited(Exception):
        retry_after = 30.0

    async def rate_limited(_route) -> None:
        raise RateLimited("429")

    adapter, sleeps, requests = _typing_adapter(
        monkeypatch,
        request_effect=rate_limited,
    )
    channel_id = "444444444"

    await adapter.send_typing(channel_id)
    typing_task = adapter._typing_tasks[channel_id]
    await _eventually(lambda: len(requests) == 1 and bool(sleeps.waiters))

    await adapter.stop_typing(channel_id)

    assert typing_task.done()
    assert channel_id not in adapter._typing_tasks
    assert requests == [
        ("POST", "/channels/{channel_id}/typing", channel_id),
    ]
