import pytest

from agent.codex_worker_policy import PolicyInput, decide_worker


def test_frontier_class_requires_deeper_analysis_evidence():
    blocked = decide_worker(PolicyInput(task_class="frontier"))
    assert blocked.action == "blocked"
    assert blocked.next_action == "supply_deeper_analysis_evidence"


def test_removed_models_are_rejected():
    for model in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        with pytest.raises(ValueError):
            PolicyInput(model_override=model)
        with pytest.raises(ValueError):
            PolicyInput(pinned_model=model)


@pytest.mark.parametrize(
    "task_class,expected",
    [
        ("mechanical", ("gpt-6-luna", "low")),
        ("bounded", ("gpt-6-luna", "medium")),
        ("general", ("gpt-6.1-sol", "medium")),
        ("integration", ("gpt-6.1-sol", "high")),
        ("complex", ("gpt-6.1-sol", "high")),
        ("frontier", ("gpt-6.1-sol", "xhigh")),
    ],
)
def test_policy_matrix(task_class, expected):
    decision = decide_worker(PolicyInput(task_class=task_class, deeper_analysis_evidence="recorded failure analysis"))
    assert (decision.model, decision.effort) == expected
    assert decision.action == "spawn"
    assert decision.override_source == "policy"
    assert decision.policy_version


@pytest.mark.parametrize(
    "values,expected",
    [
        ({"task_class": "mechanical", "risk": "high"}, ("gpt-6.1-sol", "high")),
        ({"task_class": "bounded", "ambiguity": "ambiguous"}, ("gpt-6.1-sol", "high")),
        ({"task_class": "mechanical", "ambiguity": "unknown"}, ("gpt-6.1-sol", "medium")),
        ({"task_class": "frontier", "risk": "high"}, ("gpt-6.1-sol", "xhigh")),
    ],
)
def test_risk_and_ambiguity_floor_precedes_cheap_class(values, expected):
    decision = decide_worker(PolicyInput(**values, deeper_analysis_evidence="recorded failure analysis"))
    assert (decision.model, decision.effort) == expected


def test_deeper_effort_and_max_are_evidence_gated_but_authorized_pin_is_labeled():
    xhigh = decide_worker(PolicyInput(
        model_override="gpt-6.1-sol", effort_override="xhigh",
        choice_reason="hard analysis requested",
    ))
    assert xhigh.action == "blocked"
    assert xhigh.next_action == "supply_deeper_analysis_evidence"

    supported_xhigh = decide_worker(PolicyInput(
        model_override="gpt-6.1-sol", effort_override="xhigh",
        deeper_analysis_evidence="high-effort analysis missed a cross-system invariant",
        choice_reason="retry the revised hypothesis with deeper analysis",
    ))
    assert (supported_xhigh.action, supported_xhigh.model, supported_xhigh.effort) == (
        "spawn", "gpt-6.1-sol", "xhigh",
    )

    maximum = decide_worker(PolicyInput(
        model_override="gpt-6.1-sol", effort_override="max",
        deeper_analysis_evidence="failed high-effort design review",
        hard_judgment=True, high_failure_cost=True,
        choice_reason="direct max selection is justified",
    ))
    assert (maximum.action, maximum.model, maximum.effort) == ("spawn", "gpt-6.1-sol", "max")
    assert maximum.override_source == "caller_override"

    pin = decide_worker(PolicyInput(
        pinned_model="gpt-6.1-sol", pinned_effort="max",
        choice_reason="authorized user pin",
    ))
    assert (pin.action, pin.model, pin.effort) == ("spawn", "gpt-6.1-sol", "max")
    assert pin.override_source == "user_pin"

    caller_model = decide_worker(PolicyInput(model_override="gpt-6.1-sol"))
    assert caller_model.override_source == "caller_override"
    assert caller_model.override_source != "user_pin"


@pytest.mark.parametrize(
    "values",
    [
        {"task_class": "tiny"},
        {"risk": "critical"},
        {"ambiguity": "maybe"},
        {"model_override": "gpt-4"},
        {"effort_override": "ultra"},
        {"attempt": 0},
        {"attempt": 4, "max_attempts": 3},
    ],
)
def test_invalid_or_unavailable_values_fail_explicitly(values):
    with pytest.raises(ValueError):
        PolicyInput(**values)


@pytest.mark.parametrize(
    "flag,action,next_action",
    [
        ("missing_context", "blocked", "supply_missing_context"),
        ("broken_environment", "blocked", "repair_environment"),
        ("deterministic_tool_sufficient", "skipped", "use_deterministic_tool"),
    ],
)
def test_prerequisites_do_not_spawn(flag, action, next_action):
    decision = decide_worker(PolicyInput(**{flag: True}))
    assert decision.action == action
    assert decision.next_action == next_action


@pytest.mark.parametrize("attempt,max_attempts", [(2, 2), (3, 3)])
def test_last_permitted_attempt_can_escalate(attempt, max_attempts):
    decision = decide_worker(PolicyInput(
        failure_kind="shallow_reasoning", attempt=attempt, max_attempts=max_attempts,
        prior_model="gpt-6.1-sol", prior_effort="medium",
    ))
    assert decision.action == "spawn"
    assert (decision.model, decision.effort) == ("gpt-6.1-sol", "high")


def test_retry_adaptation_is_bounded_and_carries_handoff_refs():
    shallow = decide_worker(PolicyInput(
        task_class="general", failure_kind="shallow_reasoning", attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="medium",
        handoff_refs=("SPEC.md", "tests/output.txt"),
    ))
    assert (shallow.model, shallow.effort) == ("gpt-6.1-sol", "high")
    assert shallow.escalated
    assert shallow.handoff_refs == ("SPEC.md", "tests/output.txt")

    misunderstood = decide_worker(PolicyInput(
        failure_kind="misunderstanding", attempt=2,
        prior_model="gpt-6-luna", prior_effort="medium",
    ))
    assert (misunderstood.model, misunderstood.effort) == ("gpt-6.1-sol", "high")

    no_higher = decide_worker(PolicyInput(
        failure_kind="misunderstanding", attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="high",
    ))
    assert no_higher.action == "stop_replan"
    assert no_higher.next_action == "replan_or_stop"

    blind_repeat = decide_worker(PolicyInput(
        failure_kind="repeated_same_defect", attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="high",
    ))
    assert blind_repeat.action == "stop_replan"
    assert blind_repeat.next_action == "add_evidence_informed_correction"

    corrected = decide_worker(PolicyInput(
        failure_kind="repeated_same_defect", attempt=2,
        prior_model="gpt-6-luna", prior_effort="medium",
        correction_evidence="new failing invariant and revised hypothesis",
    ))
    assert (corrected.model, corrected.effort) == ("gpt-6.1-sol", "high")
    assert corrected.action == "spawn"

    top_model = decide_worker(PolicyInput(
        failure_kind="repeated_same_defect", attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="high",
        correction_evidence="new failing invariant and revised hypothesis",
    ))
    assert top_model.action == "stop_replan"

    exhausted = decide_worker(PolicyInput(
        failure_kind="approach_failure", attempt=3, max_attempts=3,
        prior_model="gpt-6.1-sol", prior_effort="max",
    ))
    assert exhausted.action == "stop_replan"
    assert exhausted.next_action == "replan_or_stop"

    max_effort = decide_worker(PolicyInput(
        failure_kind="shallow_reasoning", attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="max",
    ))
    assert max_effort.action == "stop_replan"
    assert max_effort.next_action == "replan_or_stop"


@pytest.mark.parametrize("failure_kind", ["missing_information", "environment"])
def test_non_reasoning_failures_block_without_escalation(failure_kind):
    decision = decide_worker(PolicyInput(
        failure_kind=failure_kind, attempt=2,
        prior_model="gpt-6.1-sol", prior_effort="medium",
    ))
    assert decision.action == "blocked"
    assert not decision.escalated


def test_resolved_implementation_can_downshift_but_risk_floor_remains():
    downshifted = decide_worker(PolicyInput(
        task_class="complex", phase="implementation", contract_resolved=True,
    ))
    assert (downshifted.effective_task_class, downshifted.model, downshifted.effort) == (
        "general", "gpt-6.1-sol", "medium",
    )
    risky = decide_worker(PolicyInput(
        task_class="complex", phase="implementation", contract_resolved=True, risk="high",
    ))
    assert (risky.model, risky.effort) == ("gpt-6.1-sol", "high")


def test_lifecycle_request_round_trip_stays_backwards_compatible_without_policy_fields(tmp_path):
    from agent.codex_task_runner import TaskRequest
    from agent.task_lifecycle.workflow import decode_request, request_data

    spec = tmp_path / "SPEC.md"
    spec.write_text("task")
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    request = TaskRequest(spec, tmp_path, tmp_path, output)
    encoded = request_data(request)
    assert not ({"task_class", "risk", "ambiguity", "policy"} & encoded.keys())
    assert decode_request(encoded) == request
    encoded.pop("cli")  # Receipts written before the Claude overlay remain readable.
    assert decode_request(encoded) == request


def test_executor_rejects_forged_and_blocked_receipts_before_spawn(tmp_path):
    from agent.codex_task_runner import TaskRequest, run_task
    spec = tmp_path / "SPEC.md"
    spec.write_text("No real worker")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    request = TaskRequest(spec, tmp_path, tmp_path, out)
    calls = []
    def sentinel(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not spawn")
    for values in (PolicyInput(missing_context=True), PolicyInput(task_class="mechanical")):
        with pytest.raises(ValueError):
            run_task(request, popen=sentinel, policy_receipt=decide_worker(values).receipt())
    assert calls == []
    assert list(out.iterdir()) == []


def test_lifecycle_roundtrip_revalidates_policy_and_scope(tmp_path):
    from dataclasses import asdict
    from agent.codex_task_runner import TaskRequest, WorkerSelection
    from agent.task_lifecycle.workflow import request_data, decode_request
    spec = tmp_path / "SPEC.md"
    spec.write_text("No real worker")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    policy = PolicyInput(task_class="complex", risk="high")
    request = TaskRequest(spec, tmp_path, tmp_path, out, model="gpt-6.1-sol",
        selection=WorkerSelection.for_effort("high"), policy_input=asdict(policy), timeout=45)
    import json
    payload = json.loads(json.dumps(request_data(request)))
    decoded = decode_request(payload)
    assert decoded.argv() == request.argv()
    assert decoded.allowed_root == request.allowed_root
    assert decoded.sandbox == request.sandbox
    assert decoded.timeout == request.timeout
    payload["policy_input"]["missing_context"] = True
    with pytest.raises(ValueError):
        decode_request(payload)
    payload.pop("policy_input")
    payload["model"] = "gpt-6.1-sol"
    payload["selection"] = asdict(WorkerSelection.for_effort("max"))
    with pytest.raises(ValueError):
        decode_request(payload)
    payload["selection"] = asdict(WorkerSelection.for_effort("medium"))
    assert decode_request(payload).model == "gpt-6.1-sol"  # old explicit safe pair preserved
    payload["model"] = "gpt-6-astra"
    with pytest.raises(ValueError):
        decode_request(payload)


def test_real_usage_keys_are_summed_and_unknown_remains_unknown(tmp_path):
    import json
    from agent.codex_task_runner import _runtime_evidence
    path = tmp_path / "events.jsonl"
    events = [
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20, "reasoning_output_tokens": 5}},
        {"type": "turn.completed", "usage": {"input_tokens": 200, "output_tokens": 40, "reasoning_output_tokens": 7}},
        {"type": "item.completed", "model": "fake-provider", "usage": {"input_tokens": 999}},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events))
    observed, usage = _runtime_evidence(path)
    assert observed == {"model": None, "effort": None}
    assert usage == {"input_tokens": 300, "output_tokens": 60, "reasoning_tokens": 12,
                     "cached_input_tokens": None, "cache_write_input_tokens": None}
    events.append({"type": "turn.completed", "usage": {"input_tokens": True}})
    path.write_text("\n".join(json.dumps(event) for event in events))
    _, usage = _runtime_evidence(path)
    assert all(value is None for value in usage.values())


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_max_retry_is_bounded_for_every_model(model):
    result = decide_worker(PolicyInput(failure_kind="shallow_reasoning", attempt=2,
                                     prior_model=model, prior_effort="max"))
    assert result.action == "stop_replan"


def test_legacy_flags_do_not_bypass_max_evidence_gate():
    result = decide_worker(PolicyInput(model_override="gpt-6.1-sol", effort_override="max",
                                     override_source="legacy_override"))
    assert result.action == "blocked"


def test_advisor_is_validated_and_survives_lifecycle_round_trip(tmp_path):
    from agent.codex_task_runner import ADVISOR_SETTINGS, TaskRequest
    from agent.task_lifecycle.workflow import decode_request, request_data

    spec = tmp_path / "SPEC.md"
    spec.write_text("task")
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    request = TaskRequest(spec, tmp_path, tmp_path, out, cli="claude", model="sonnet", advisor="opus")
    argv = request.argv()
    assert argv[argv.index("--advisor") + 1] == "opus"
    assert argv[argv.index("--settings") + 1] == ADVISOR_SETTINGS
    assert decode_request(request_data(request)) == request
    with pytest.raises(ValueError):
        TaskRequest(spec, tmp_path, tmp_path, out, advisor="opus")
    with pytest.raises(ValueError):
        TaskRequest(spec, tmp_path, tmp_path, out, cli="claude", model="sonnet", advisor="haiku")
