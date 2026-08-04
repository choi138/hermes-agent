"""Resolve the R3 work-lane axis for local observation metrics.

Hermes computes a routing ``selected_lane`` in ``hermes_cli.smart_model_routing``
and surfaces it into ``route["smart_routing"]``, but the only consumer today is
a ``startswith("gjc_")`` test — nothing carries a lane down to the model call.
This module holds the lane in a ContextVar so the per-turn resolution below can
read it on the turn thread.

Axis split (deliberate): ``work_lane`` is the work-TYPE + dispatch-owner axis.
The dispatch SURFACE (``scheduled_task``, ``batch``, ``cli``, ``gateway``, ...)
is carried by the separate ``execution_surface`` dimension, which is present on
every observation row. That is why "scheduled" and "batch" are NOT lanes:
resolving them first would make a scheduled research run report
work_lane="scheduled", indistinguishable from a scheduled direct run, defeating
the one lane distinction R3 explicitly names.

HONEST COVERAGE LIMIT: ``smart_model_routing.enabled`` defaults to False, so
with routing off no ``research_readonly`` lane is ever computed and research
work resolves to "direct". There is no other runtime marker for research work
(the "research" toolset is a static tool list with no runtime selection
signal). Fixed-condition baselines that need the research lane separated MUST
run with smart_model_routing enabled, or must pin the lane through ``hint``.
Kanban, delegated and direct separate correctly regardless of routing config.
"""

from __future__ import annotations

import contextvars
import logging
import os

from .shared_metrics_contract import WORK_LANES, execution_surface

logger = logging.getLogger(__name__)

_ROUTING_LANE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hermes_routing_lane",
    default="",
)


def set_routing_lane(selected_lane: str) -> None:
    """Publish (or clear) the smart-routing lane for the current context.

    Callers MUST reset this to "" unconditionally at the top of every turn's
    route resolution: the real assignment is conditional on smart routing
    having produced a decision, so without a reset a turn whose routing raised
    or was disabled would inherit the previous turn's lane indefinitely.
    """
    try:
        _ROUTING_LANE.set(str(selected_lane or ""))
    except Exception:
        logger.debug("Unable to publish the routing lane", exc_info=True)


def snapshot() -> str:
    """Return the raw routing lane for the current context ("" when unset)."""
    try:
        return str(_ROUTING_LANE.get() or "")
    except Exception:
        return ""


def _session_source(platform: str) -> str:
    """Resolve the Hermes session source the same way ``run_agent`` does.

    Deliberately reimplemented rather than imported from ``run_agent``: this
    runs inside a lifecycle hook, and importing ``run_agent`` from there drags
    in plugin discovery and the whole agent module graph. A test pins parity
    with ``run_agent._session_source_for_agent``.
    """
    try:
        try:
            from gateway.session_context import get_session_env

            source = get_session_env("HERMES_SESSION_SOURCE", "")
        except Exception:
            source = os.environ.get("HERMES_SESSION_SOURCE", "")
        source = str(source or "").strip()
        if source:
            return source.lower()
        if str(os.environ.get("HERMES_KANBAN_TASK") or "").strip():
            return "kanban"
        return str(platform or "cli").strip().lower()
    except Exception:
        logger.debug("Unable to resolve the session source", exc_info=True)
        return ""


def current_work_lane(
    *,
    platform: str = "",
    is_subagent: bool = False,
    parent_session_id: str = "",
    hint: str = "",
) -> str:
    """Return a WORK_LANES member for the current execution context.

    Precedence (work TYPE beats dispatch owner beats surface):
      a. an explicit ``hint`` that is already a WORK_LANES member;
      b. "research"  — routing lane == "research_readonly";
      c. "gjc"       — routing lane starts with "gjc_";
      d. "delegated" — subagent / parent session / platform == "subagent".
         This MUST precede the kanban check: inherited HERMES_KANBAN_* env vars
         are not proof of dispatcher ownership, so a delegate spawned inside a
         Kanban worker would otherwise be mislabelled "kanban".
      e. "kanban"    — the session source resolves to "kanban";
      f. "direct"    — a known platform;
      g. "unknown".
    """
    try:
        candidate = str(hint or "").strip().lower()
        if candidate in WORK_LANES:
            return candidate

        routing_lane = snapshot().strip().lower()
        if routing_lane == "research_readonly":
            return "research"
        if routing_lane.startswith("gjc_"):
            return "gjc"

        platform_value = str(platform or "").strip().lower()
        if is_subagent or parent_session_id or platform_value == "subagent":
            return "delegated"

        if _session_source(platform_value) == "kanban":
            return "kanban"

        # A recognised dispatch surface means real user-facing work with no
        # more specific lane. The surface itself (cli / gateway / batch /
        # scheduled_task / ...) travels on the separate execution_surface
        # dimension, so it is not folded into the lane here.
        if execution_surface({"platform": platform_value}) not in {
            "unknown",
            "other",
        }:
            return "direct"
        return "unknown"
    except Exception:
        logger.debug("Unable to resolve the work lane", exc_info=True)
        return "unknown"
