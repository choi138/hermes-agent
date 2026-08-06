"""Tests for the R3 work-lane axis."""

from __future__ import annotations

import contextvars

import pytest

from hermes_cli.observability import work_lane
from hermes_cli.observability.shared_metrics_contract import (
    EXECUTION_SURFACES,
    WORK_LANES,
)


@pytest.fixture(autouse=True)
def isolated_lane_context(monkeypatch):
    """Each test starts with no routing lane and no kanban env markers."""
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    context = contextvars.copy_context()
    context.run(work_lane.set_routing_lane, "")
    work_lane.set_routing_lane("")
    yield
    work_lane.set_routing_lane("")


def test_work_lanes_exclude_the_dispatch_surface_axis():
    assert "scheduled" not in WORK_LANES
    assert "batch" not in WORK_LANES
    # The surface axis lives here instead, and is on every observation row.
    assert {"scheduled_task", "batch"} <= EXECUTION_SURFACES


def test_research_lane_wins_over_the_dispatch_surface(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    work_lane.set_routing_lane("research_readonly")

    assert work_lane.current_work_lane(platform="batch") == "research"
    assert work_lane.current_work_lane(platform="cli") == "research"


@pytest.mark.parametrize(
    "lane",
    ["gjc_team", "gjc_ralplan", "gjc_visible_session"],
)
def test_gjc_routing_lanes_map_to_one_lane(lane):
    work_lane.set_routing_lane(lane)

    assert work_lane.current_work_lane(platform="cli") == "gjc"


def test_delegated_wins_over_an_inherited_kanban_env(monkeypatch):
    # tools/kanban_tools.py warns that inherited HERMES_KANBAN_* vars are NOT
    # proof of dispatcher ownership, so a delegate spawned inside a Kanban
    # worker must not be relabelled "kanban".
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")

    assert work_lane.current_work_lane(platform="cli", is_subagent=True) == "delegated"
    assert (
        work_lane.current_work_lane(platform="cli", parent_session_id="parent")
        == "delegated"
    )
    assert work_lane.current_work_lane(platform="subagent") == "delegated"


def test_kanban_session_source_and_task_env(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    assert work_lane.current_work_lane(platform="cli") == "kanban"

    monkeypatch.delenv("HERMES_SESSION_SOURCE")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-9")
    # Kanban workers are spawned as plain CLI processes, which is exactly why
    # they are invisible in platform-only metrics today.
    assert work_lane.current_work_lane(platform="cli") == "kanban"


def test_primary_routing_lane_and_known_platforms_resolve_to_direct():
    work_lane.set_routing_lane("primary")
    assert work_lane.current_work_lane(platform="cli") == "direct"

    work_lane.set_routing_lane("")
    for platform in ("cli", "tui", "gateway", "api", "python", "desktop"):
        assert work_lane.current_work_lane(platform=platform) == "direct"


def test_no_platform_and_no_markers_is_unknown():
    assert work_lane.current_work_lane() == "unknown"
    assert work_lane.current_work_lane(platform="") == "unknown"


def test_explicit_hint_overrides_everything(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
    work_lane.set_routing_lane("research_readonly")

    assert work_lane.current_work_lane(platform="cli", hint="direct") == "direct"
    # A junk hint falls through to the normal precedence.
    assert work_lane.current_work_lane(platform="cli", hint="nonsense") == "research"


@pytest.mark.parametrize(
    "junk",
    [None, 0, 1, object(), b"cli", ["cli"], {"a": 1}, "", "  ", "RESEARCH"],
)
def test_every_result_is_a_member_of_work_lanes(junk):
    work_lane.set_routing_lane("")
    assert (
        work_lane.current_work_lane(
            platform=junk,  # type: ignore[arg-type]
            is_subagent=junk,  # type: ignore[arg-type]
            parent_session_id="",
            hint=junk,  # type: ignore[arg-type]
        )
        in WORK_LANES
    )


def test_routing_lane_does_not_leak_across_turns():
    """A turn whose routing raises must not inherit the previous lane.

    Regression guard for the conditional ``route["smart_routing"]`` assignment:
    the real set is inside ``if smart_routing is not None``, so the reset to ""
    at the top of route resolution is what stops an unbounded run of later
    turns from reporting "research".
    """
    work_lane.set_routing_lane("research_readonly")
    assert work_lane.current_work_lane(platform="cli") == "research"

    # Simulate the next turn: the unconditional reset runs, routing then raises
    # so no lane is published.
    work_lane.set_routing_lane("")
    assert work_lane.current_work_lane(platform="cli") == "direct"


def test_snapshot_reports_the_raw_routing_lane():
    work_lane.set_routing_lane("codex_implementation")
    assert work_lane.snapshot() == "codex_implementation"
    # An unmapped routing lane is real work on a known surface.
    assert work_lane.current_work_lane(platform="cli") == "direct"


def test_cli_route_resolution_resets_the_lane_unconditionally():
    """The CLI and gateway route resolvers must both reset before routing."""
    import inspect

    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    source = inspect.getsource(CLIAgentSetupMixin._resolve_turn_agent_config)
    assert 'set_routing_lane("")' in source
    assert "selected_lane" in source


@pytest.mark.parametrize(
    "env,platform",
    [
        ({}, "cli"),
        ({"HERMES_SESSION_SOURCE": "kanban"}, "cli"),
        ({"HERMES_SESSION_SOURCE": "gateway"}, "tui"),
        ({"HERMES_KANBAN_TASK": "t-1"}, "cli"),
        ({}, ""),
    ],
)
def test_session_source_matches_run_agent(monkeypatch, env, platform):
    """work_lane reimplements the resolution to avoid importing run_agent.

    Importing run_agent from inside a lifecycle hook drags in plugin discovery
    and the whole agent module graph, so parity is pinned by a test instead.
    """
    from run_agent import _session_source_for_agent

    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    expected = str(_session_source_for_agent(platform) or "").strip().lower()
    assert work_lane._session_source(platform) == expected


def test_work_lane_does_not_import_run_agent(monkeypatch):
    """The hook path must not pull run_agent into sys.modules."""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "from hermes_cli.observability import work_lane\n"
        "work_lane.set_routing_lane('')\n"
        "assert work_lane.current_work_lane(platform='cli') == 'direct'\n"
        "assert 'run_agent' not in sys.modules, sorted(sys.modules)[:5]\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
