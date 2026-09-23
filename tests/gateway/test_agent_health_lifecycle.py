"""RED contract for the ``GatewayRunner`` agent-health sink lifecycle.

Pins only the integration surface: the ``_agent_health_sink`` field, the two
lifecycle helpers, and the two call sites inside ``start``/``stop``. No network,
no environment reads, no Discord — the sink factory is patched on the real class
so ``HERMES_HEALTH_*`` is never consulted.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
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


def test_lifecycle_state_initializes_agent_health_sink_field():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_health_sink = object()

    runner._init_lifecycle_state()

    assert runner._agent_health_sink is None


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


@pytest.mark.asyncio
async def test_start_wires_router_before_sink_and_marks_running_after_sink():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {"discord": object()}
    runner.delivery_router = SimpleNamespace(adapters=None)
    runner._running = False
    runner._start_install_faulthandler = MagicMock()
    runner._start_log_startup_environment = AsyncMock()
    runner._abort_startup_if_shutdown_requested = AsyncMock(return_value=False)
    runner._start_check_access_policy = MagicMock(return_value=False)
    runner._start_recover_previous_run = AsyncMock()
    runner._run_free_tier_bootstrap = AsyncMock()
    runner._start_startup_warmup = MagicMock()
    runner._start_prefilter_platforms = AsyncMock(return_value=(False, 1, [], []))
    runner._start_connect_pending = AsyncMock(return_value=[object()])
    runner._start_aggregate_connect_results = AsyncMock(return_value=1)
    runner._start_secondary_profiles = AsyncMock(return_value=(False, 1))
    runner._start_handle_no_connections = MagicMock(return_value=False)
    runner._wire_teams_pipeline_runtime = MagicMock()
    runner._install_plugin_message_injector = MagicMock()
    runner._serving_state = MagicMock(return_value="running")
    runner._update_runtime_status = MagicMock()
    runner._start_finish_wiring = AsyncMock()
    runner._start_spawn_background_watchers = MagicMock()

    def check_sink_start():
        assert runner.delivery_router.adapters is runner.adapters
        assert runner._running is False

    runner._start_agent_health_sink = MagicMock(side_effect=check_sink_start)

    assert await runner._start_impl() is True
    runner._start_agent_health_sink.assert_called_once_with()
    assert runner._running is True


@pytest.mark.asyncio
async def test_stop_stops_sink_before_adapter_teardown():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = object()
    runner.adapters = {"discord": adapter}
    runner._profile_adapters = {}
    runner._restart_requested = False
    runner._finalize_shutdown_agents = AsyncMock()
    runner._stop_mention_inbox_services = AsyncMock()
    runner._stop_agent_health_sink = AsyncMock()

    async def check_adapter_teardown(candidate, _platform):
        assert candidate is adapter
        runner._stop_agent_health_sink.assert_awaited_once_with()

    runner._bounded_adapter_teardown = AsyncMock(side_effect=check_adapter_teardown)

    await runner._stop_finalize_agents_and_adapters(
        SimpleNamespace(active_agents=[], elapsed=lambda: 0.0)
    )

    runner._bounded_adapter_teardown.assert_awaited_once()


def test_adapter_inventory_deduplicates_default_and_profile_maps():
    runner = GatewayRunner.__new__(GatewayRunner)
    discord = SimpleNamespace(platform="discord")
    slack = SimpleNamespace(platform="slack")
    runner.adapters = {"discord": discord}
    runner._profile_adapters = {"second": {"discord": discord, "slack": slack}}

    assert list(runner._iter_gateway_adapters()) == [discord, slack]
