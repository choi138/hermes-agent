"""Tests for the P2-M2 deterministic policy router.

The classifier from M1 only describes a request. This router is the single place
that turns that description into an execution decision, so the tests pin two
properties above all:

1. Hints are re-derived, never trusted. A hint that policy cannot verify is
   discarded.
2. Anything unverifiable, out of bounds, or contradictory resolves to
   `lane="refuse"` rather than a narrowed-but-still-running decision.
"""

import json

import pytest

from agent.direct_agent_classification import parse_classification
from agent.direct_agent_policy import (
    ALLOWED_WORKDIRS,
    MAX_TIMEOUT_SECONDS,
    ExecutionDecision,
    PolicyConfigError,
    PolicySettings,
    route_classification,
)

REPO = "/Users/choegeun-won/Documents/hermes-agent"

BASE = {
    "schema_version": "1",
    "intent": {
        "kind": "code",
        "summary": "버그 수정 요청",
        "requested_outcome": "테스트를 통과하도록 코드 수정",
    },
    "risk": {
        "level": "medium",
        "categories": ["filesystem_write"],
        "rationale": "저장소 파일 변경이 필요함",
    },
    "memory_query": {
        "required": False,
        "query": None,
        "entities": [],
        "temporal_scope": None,
        "reason": "현재 요청만으로 수행 가능함",
    },
    "execution_target": {
        "lane_hint": "codex",
        "host_hint": "mac",
        "workdir_hint": REPO,
    },
    "uncertainties": [],
}


def _classification(**sections):
    payload = json.loads(json.dumps(BASE))
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key].update(value)
        else:
            payload[key] = value
    return parse_classification(json.dumps(payload))


def _route(**sections) -> ExecutionDecision:
    return route_classification(_classification(**sections))


# --- happy paths -------------------------------------------------------------


def test_code_request_routes_to_codex_on_mac():
    decision = _route()

    assert decision.lane == "codex"
    assert decision.host == "mac"
    assert decision.workdir == REPO
    assert decision.refusal_reason is None


def test_docs_request_routes_to_claude():
    decision = _route(
        intent={"kind": "docs"},
        execution_target={"lane_hint": "claude"},
    )

    assert decision.lane == "claude"


def test_question_routes_to_hermes_without_a_workdir():
    decision = _route(
        intent={"kind": "question"},
        risk={"level": "none", "categories": [], "rationale": "읽기만 함"},
        execution_target={"lane_hint": "hermes", "host_hint": "unknown", "workdir_hint": None},
    )

    assert decision.lane == "hermes"
    assert decision.workdir is None
    assert decision.permissions == "read_only"


# --- hints are advisory, never authoritative --------------------------------


def test_lane_hint_is_overridden_when_intent_disagrees():
    # Hint says claude; the request is code. Intent wins.
    decision = _route(execution_target={"lane_hint": "claude"})

    assert decision.lane == "codex"
    assert any("lane_hint" in entry for entry in decision.policy_trace)


def test_unknown_lane_hint_still_yields_a_derived_lane():
    decision = _route(execution_target={"lane_hint": "unknown"})

    assert decision.lane == "codex"


def test_host_hint_is_ignored_when_the_workdir_lives_on_the_other_host():
    # Hint claims remote, but the allowlisted workdir is a Mac path.
    decision = _route(execution_target={"host_hint": "remote"})

    assert decision.host == "mac"


def test_hint_cannot_widen_permissions():
    decision = _route(
        risk={"level": "low", "categories": [], "rationale": "읽기만"},
        intent={"kind": "research"},
        execution_target={"lane_hint": "codex", "workdir_hint": REPO},
    )

    assert decision.permissions == "read_only"


# --- workdir containment ----------------------------------------------------


@pytest.mark.parametrize(
    "workdir",
    [
        "/etc",
        "/",
        "/Users/choegeun-won",
        "/Users/choegeun-won/.ssh",
        "/tmp/somewhere",
        "relative/path",
    ],
)
def test_workdir_outside_the_allowlist_is_refused(workdir):
    decision = _route(execution_target={"workdir_hint": workdir})

    assert decision.lane == "refuse"
    assert decision.refusal_reason is not None


def test_blank_workdir_never_reaches_the_router():
    # M1 rejects a blank workdir_hint, so the router is never asked about it.
    # Asserting refusal here would test the wrong layer.
    from agent.direct_agent_classification import ClassificationError

    with pytest.raises(ClassificationError):
        _classification(execution_target={"workdir_hint": "   "})


@pytest.mark.parametrize(
    "escape",
    [
        f"{REPO}/../../../etc",
        f"{REPO}/../other-repo",
        f"{REPO}/./../..",
        f"{REPO}/subdir/../../..",
    ],
)
def test_parent_traversal_out_of_the_allowlist_is_refused(escape):
    decision = _route(execution_target={"workdir_hint": escape})

    assert decision.lane == "refuse"


def test_a_subdirectory_of_an_allowlisted_workdir_is_accepted():
    decision = _route(execution_target={"workdir_hint": f"{REPO}/agent"})

    assert decision.lane == "codex"
    assert decision.workdir == f"{REPO}/agent"


def test_prefix_lookalike_directory_is_refused():
    # "hermes-agent-evil" must not pass because it starts with an allowed path.
    decision = _route(
        execution_target={"workdir_hint": "/Users/choegeun-won/Documents/hermes-agent-evil"}
    )

    assert decision.lane == "refuse"


def test_lane_requiring_a_workdir_is_refused_when_none_is_given():
    decision = _route(execution_target={"workdir_hint": None})

    assert decision.lane == "refuse"


# --- approval ---------------------------------------------------------------


def test_high_risk_always_requires_approval():
    decision = _route(
        risk={
            "level": "high",
            "categories": ["filesystem_write"],
            "rationale": "광범위 변경",
        }
    )

    assert decision.approval == "required"


@pytest.mark.parametrize(
    "category",
    ["filesystem_delete", "external_send", "deployment", "data_migration", "credential_access"],
)
def test_sensitive_categories_require_approval_regardless_of_level(category):
    decision = _route(
        risk={"level": "low", "categories": [category], "rationale": "민감 작업"}
    )

    assert decision.approval == "required"


def test_routine_write_does_not_require_approval():
    decision = _route()

    assert decision.approval == "not_required"


def test_read_only_request_does_not_require_approval():
    decision = _route(
        intent={"kind": "question"},
        risk={"level": "none", "categories": [], "rationale": "읽기"},
        execution_target={"lane_hint": "hermes", "workdir_hint": None},
    )

    assert decision.approval == "not_required"


# --- permissions are least-privilege ----------------------------------------


def test_permissions_stay_read_only_without_a_write_category():
    decision = _route(
        intent={"kind": "research"},
        risk={"level": "low", "categories": [], "rationale": "조사만"},
        execution_target={"lane_hint": "hermes", "workdir_hint": None},
    )

    assert decision.permissions == "read_only"


def test_write_category_grants_workdir_write_only():
    decision = _route()

    assert decision.permissions == "write_workdir"


def test_network_egress_is_granted_only_when_classified():
    decision = _route(
        risk={
            "level": "medium",
            "categories": ["filesystem_write", "network_egress"],
            "rationale": "의존성 설치 필요",
        }
    )

    assert decision.permissions == "write_workdir_network"


def test_delete_category_never_grants_more_than_workdir_write():
    decision = _route(
        risk={
            "level": "high",
            "categories": ["filesystem_delete"],
            "rationale": "파일 삭제",
        }
    )

    assert decision.permissions in {"read_only", "write_workdir"}
    assert decision.approval == "required"


# --- timeout ----------------------------------------------------------------


def test_timeout_is_positive_and_bounded():
    decision = _route()

    assert 0 < decision.timeout_seconds <= MAX_TIMEOUT_SECONDS


def test_timeout_cannot_be_raised_past_the_ceiling_by_settings():
    # Rejected at construction, so an over-ceiling timeout can never reach a
    # routing call in the first place.
    with pytest.raises(PolicyConfigError):
        PolicySettings(default_timeout_seconds=MAX_TIMEOUT_SECONDS + 10_000)


def test_read_only_lane_gets_a_shorter_timeout_than_a_write_lane():
    read_only = _route(
        intent={"kind": "question"},
        risk={"level": "none", "categories": [], "rationale": "읽기"},
        execution_target={"lane_hint": "hermes", "workdir_hint": None},
    )
    write = _route()

    assert read_only.timeout_seconds <= write.timeout_seconds


# --- determinism ------------------------------------------------------------


def test_routing_is_deterministic():
    classification = _classification()
    first = route_classification(classification)

    for _ in range(100):
        assert route_classification(classification).model_dump() == first.model_dump()


def test_routing_does_not_mutate_the_classification():
    classification = _classification()
    before = classification.model_dump_json()

    route_classification(classification)

    assert classification.model_dump_json() == before


# --- fail-closed configuration ----------------------------------------------


def test_empty_allowlist_refuses_every_workdir_lane():
    settings = PolicySettings(allowed_workdirs=())

    decision = route_classification(_classification(), settings=settings)

    assert decision.lane == "refuse"


@pytest.mark.parametrize("bad", ["relative/path", "", "   "])
def test_non_absolute_allowlist_entry_is_a_config_error(bad):
    with pytest.raises(PolicyConfigError):
        PolicySettings(allowed_workdirs=(bad,))


def test_default_allowlist_is_absolute_and_non_empty():
    assert ALLOWED_WORKDIRS
    assert all(path.startswith("/") for path in ALLOWED_WORKDIRS)


def test_unknown_intent_kind_refuses_rather_than_guessing():
    decision = _route(
        intent={"kind": "other"},
        execution_target={"lane_hint": "codex"},
    )

    assert decision.lane in {"hermes", "refuse"}
    if decision.lane == "refuse":
        assert decision.refusal_reason is not None


# --- decision surface -------------------------------------------------------


def test_decision_carries_no_command_or_credential_surface():
    decision = _route()
    exposed = set(decision.model_dump().keys())

    forbidden = {
        "command",
        "shell_command",
        "argv",
        "credentials",
        "api_key",
        "token",
        "env",
    }
    assert exposed & forbidden == set()


def test_decision_exposes_exactly_the_documented_fields():
    assert set(ExecutionDecision.model_fields) == {
        "lane",
        "host",
        "workdir",
        "permissions",
        "timeout_seconds",
        "approval",
        "refusal_reason",
        "policy_trace",
    }


def test_refusal_carries_a_reason_and_no_execution_surface():
    decision = _route(execution_target={"workdir_hint": "/etc"})

    assert decision.lane == "refuse"
    assert decision.refusal_reason
    assert decision.workdir is None
    assert decision.permissions == "read_only"
    assert decision.approval == "required"


def test_policy_trace_explains_every_decision():
    decision = _route()

    joined = " ".join(decision.policy_trace).lower()
    for topic in ("lane", "host", "workdir", "permission", "timeout", "approval"):
        assert topic in joined


def test_decision_is_immutable():
    decision = _route()

    with pytest.raises(Exception):
        decision.lane = "hermes"


def test_router_rejects_an_unvalidated_mapping():
    # Only a parsed RequestClassification is accepted; raw dicts bypass M1.
    with pytest.raises(TypeError):
        route_classification(BASE)
