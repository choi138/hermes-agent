"""RED contract for the ``GatewayRunner`` agent-health sink lifecycle.

Pins only the integration surface: the ``_agent_health_sink`` field, the two
lifecycle helpers, and the two call sites inside ``start``/``stop``. No network,
no environment reads, no Discord — the sink factory is patched on the real class
so ``HERMES_HEALTH_*`` is never consulted.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import agent_health_sink as sink_module

AgentHealthSink = sink_module.AgentHealthSink
set_active_agent_health_sink = sink_module.set_active_agent_health_sink


from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _reset_active_sink():
    set_active_agent_health_sink(None)
    yield
    set_active_agent_health_sink(None)


def _bare_runner():
    """A GatewayRunner shell in its expected post-``__init__`` sink state."""
    runner = object.__new__(GatewayRunner)
    runner._agent_health_sink = None
    return runner


def _fake_sink():
    sink = MagicMock(name="AgentHealthSink")
    sink.start = MagicMock(name="start")
    sink.stop = AsyncMock(name="stop")
    return sink


def _starter(runner):
    starter = getattr(runner, "_start_agent_health_sink", None)
    assert starter is not None, "GatewayRunner._start_agent_health_sink is missing"
    return starter


def _stopper(runner):
    stopper = getattr(runner, "_stop_agent_health_sink", None)
    assert stopper is not None, "GatewayRunner._stop_agent_health_sink is missing"
    return stopper


def _patch_factory(monkeypatch, **kwargs):
    factory = MagicMock(name="from_environment", **kwargs)
    monkeypatch.setattr(AgentHealthSink, "from_environment", factory)
    return factory


def _gateway_arg(factory):
    args, kwargs = factory.call_args
    return args[0] if args else kwargs.get("gateway")


def test_init_declares_agent_health_sink_field():
    source = inspect.getsource(GatewayRunner.__init__)
    assert "self._agent_health_sink = None" in source, (
        "GatewayRunner.__init__ must initialize self._agent_health_sink = None"
    )


def test_lifecycle_helper_kinds():
    starter = getattr(GatewayRunner, "_start_agent_health_sink", None)
    stopper = getattr(GatewayRunner, "_stop_agent_health_sink", None)
    assert callable(starter), "GatewayRunner._start_agent_health_sink is missing"
    assert callable(stopper), "GatewayRunner._stop_agent_health_sink is missing"
    assert not inspect.iscoroutinefunction(starter), (
        "_start_agent_health_sink must be a sync helper"
    )
    assert inspect.iscoroutinefunction(stopper), (
        "_stop_agent_health_sink must be an async helper"
    )


def test_start_builds_sink_from_environment_and_starts_it(monkeypatch):
    runner = _bare_runner()
    sink = _fake_sink()
    factory = _patch_factory(monkeypatch, return_value=sink)

    _starter(runner)()

    assert factory.call_count == 1
    assert _gateway_arg(factory) is runner
    assert runner._agent_health_sink is sink
    sink.start.assert_called_once_with()


def test_start_is_idempotent(monkeypatch):
    runner = _bare_runner()
    sink = _fake_sink()
    factory = _patch_factory(monkeypatch, return_value=sink)
    start = _starter(runner)

    start()
    start()

    assert factory.call_count == 1, "second start must not rebuild the sink"
    assert sink.start.call_count == 1, "second start must not restart the sink"
    assert runner._agent_health_sink is sink


def test_start_swallows_factory_failure(monkeypatch):
    runner = _bare_runner()
    factory = _patch_factory(monkeypatch, side_effect=RuntimeError("factory boom"))

    _starter(runner)()  # must not raise

    assert factory.call_count == 1
    assert runner._agent_health_sink is None


def test_start_swallows_sink_start_failure(monkeypatch):
    runner = _bare_runner()
    sink = _fake_sink()
    sink.start.side_effect = RuntimeError("start boom")
    _patch_factory(monkeypatch, return_value=sink)

    _starter(runner)()  # must not raise

    assert sink.start.call_count == 1
    # Rolled back to None, or retained as a stoppable sink — never junk.
    assert runner._agent_health_sink is None or runner._agent_health_sink is sink


def test_stop_clears_field_before_awaiting_stop():
    runner = _bare_runner()
    sink = _fake_sink()
    observed = {}

    async def _record():
        observed["field"] = runner._agent_health_sink

    sink.stop = AsyncMock(side_effect=_record)
    runner._agent_health_sink = sink

    asyncio.run(_stopper(runner)())

    assert observed["field"] is None, "field must be cleared before awaiting stop"
    assert runner._agent_health_sink is None
    sink.stop.assert_awaited_once_with()


def test_stop_is_idempotent():
    runner = _bare_runner()
    sink = _fake_sink()
    runner._agent_health_sink = sink
    stopper = _stopper(runner)

    async def _drive():
        await stopper()
        await stopper()

    asyncio.run(_drive())

    assert sink.stop.await_count == 1, "second stop must be a no-op"
    assert runner._agent_health_sink is None


def test_stop_swallows_stop_failure():
    runner = _bare_runner()
    sink = _fake_sink()
    sink.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
    runner._agent_health_sink = sink

    asyncio.run(_stopper(runner)())  # must not raise

    assert sink.stop.await_count == 1
    assert runner._agent_health_sink is None


def test_stop_without_sink_is_noop():
    runner = _bare_runner()

    asyncio.run(_stopper(runner)())  # must not raise

    assert runner._agent_health_sink is None


def test_start_callsite_between_router_wiring_and_running_flag():
    source = inspect.getsource(GatewayRunner.start)
    router = source.find("self.delivery_router.adapters = self.adapters")
    call = source.find("self._start_agent_health_sink()")
    running = source.find("self._running = True")

    assert router != -1, "delivery_router adapter wiring landmark missing from start()"
    assert running != -1, "self._running = True landmark missing from start()"
    assert call != -1, "start() must call self._start_agent_health_sink()"
    assert router < call < running, (
        "_start_agent_health_sink() must run after delivery_router.adapters "
        "wiring and before self._running = True"
    )


def test_stop_callsite_precedes_adapter_teardown():
    source = inspect.getsource(GatewayRunner.stop)
    call = source.find("await self._stop_agent_health_sink()")
    teardown = source.find("await self._bounded_adapter_teardown(adapter, platform)")

    assert teardown != -1, "bounded adapter teardown landmark missing from stop()"
    assert call != -1, "stop() must await self._stop_agent_health_sink()"
    assert call < teardown, (
        "await self._stop_agent_health_sink() must run before "
        "await self._bounded_adapter_teardown(adapter, platform)"
    )
