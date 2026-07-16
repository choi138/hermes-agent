from __future__ import annotations

import asyncio
import time

from gateway.kanban_watchers import _wait_for_dispatcher_wake


def test_dispatcher_wait_breaks_immediately_when_intake_wakes_it():
    async def scenario():
        event = asyncio.Event()

        async def wake_soon():
            await asyncio.sleep(0.01)
            event.set()

        wake_task = asyncio.create_task(wake_soon())
        started = time.monotonic()
        woken = await _wait_for_dispatcher_wake(event, 60.0, lambda: True)
        elapsed = time.monotonic() - started
        await wake_task
        return woken, elapsed

    woken, elapsed = asyncio.run(scenario())
    assert woken is True
    assert elapsed < 0.5


def test_dispatcher_wait_preserves_periodic_timeout_without_intake():
    async def scenario():
        event = asyncio.Event()
        return await _wait_for_dispatcher_wake(event, 0.01, lambda: True)

    assert asyncio.run(scenario()) is False


def test_dispatcher_wait_stops_without_sleep_when_runner_is_down():
    async def scenario():
        event = asyncio.Event()
        return await _wait_for_dispatcher_wake(event, 60.0, lambda: False)

    assert asyncio.run(scenario()) is False
