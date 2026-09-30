"""Deterministic Codex worker model/effort policy.

Only trusted, structured coordinator metadata is accepted.  This module does
not inspect task prose, classify with a model, or change execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass


POLICY_VERSION = "2026-09-30.1"

MODELS = ("gpt-6-luna", "gpt-6.1-sol")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TASK_CLASSES = ("mechanical", "bounded", "general", "integration", "complex", "frontier")
FAILURE_KINDS = (
    "missing_information", "environment", "shallow_reasoning",
    "misunderstanding", "approach_failure", "repeated_same_defect",
)

CLASS_SELECTIONS = {
    "mechanical": ("gpt-6-luna", "low"),
    "bounded": ("gpt-6-luna", "medium"),
    "general": ("gpt-6.1-sol", "medium"),
    "integration": ("gpt-6.1-sol", "high"),
    "complex": ("gpt-6.1-sol", "high"),
    "frontier": ("gpt-6.1-sol", "xhigh"),
}

_MODEL_RANK = {value: index for index, value in enumerate(MODELS)}
_EFFORT_RANK = {value: index for index, value in enumerate(EFFORTS)}
_MODEL_BASE_EFFORT = {
    "gpt-6-luna": "medium",
    "gpt-6.1-sol": "high",
}


def _one_line(value, name, *, optional=True):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip() or "\n" in value or len(value) > 512:
        raise ValueError(f"{name} must be a nonempty bounded single line")


@dataclass(frozen=True)
class PolicyInput:
    task_class: str = "general"
    risk: str = "normal"
    ambiguity: str = "resolved"
    phase: str = "single"
    contract_resolved: bool = False
    implementation_class: str = "general"

    missing_context: bool = False
    broken_environment: bool = False
    deterministic_tool_sufficient: bool = False

    model_override: str | None = None
    effort_override: str | None = None
    pinned_model: str | None = None
    pinned_effort: str | None = None
    override_source: str = "caller_override"
    choice_reason: str | None = None
    deeper_analysis_evidence: str | None = None
    hard_judgment: bool = False
    high_failure_cost: bool = False

    failure_kind: str | None = None
    prior_model: str | None = None
    prior_effort: str | None = None
    prior_action: str | None = None
    correction_evidence: str | None = None
    handoff_refs: tuple[str, ...] = ()
    attempt: int = 1
    max_attempts: int = 3

    def __post_init__(self):
        if self.task_class not in TASK_CLASSES:
            raise ValueError("Unsupported task class")
        if self.risk not in ("normal", "high"):
            raise ValueError("Unsupported risk")
        if self.ambiguity not in ("resolved", "unknown", "ambiguous"):
            raise ValueError("Unsupported ambiguity")
        if self.phase not in ("single", "analysis", "implementation", "verification"):
            raise ValueError("Unsupported task phase")
        if self.implementation_class not in ("bounded", "general"):
            raise ValueError("Unsupported remaining implementation class")
        for name in ("contract_resolved", "missing_context", "broken_environment",
                     "deterministic_tool_sufficient", "hard_judgment", "high_failure_cost"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if sum((self.missing_context, self.broken_environment,
                self.deterministic_tool_sufficient)) > 1:
            raise ValueError("Prerequisite decisions are mutually exclusive")
        if self.model_override is not None and self.model_override not in MODELS:
            raise ValueError("Unsupported Codex model")
        if self.effort_override is not None and self.effort_override not in EFFORTS:
            raise ValueError("Unsupported Codex effort")
        if self.pinned_model is not None and self.pinned_model not in MODELS:
            raise ValueError("Unsupported pinned Codex model")
        if self.pinned_effort is not None and self.pinned_effort not in EFFORTS:
            raise ValueError("Unsupported pinned Codex effort")
        if self.override_source not in ("caller_override", "legacy_override"):
            raise ValueError("Unsupported override source")
        if self.failure_kind is not None and self.failure_kind not in FAILURE_KINDS:
            raise ValueError("Unsupported failure kind")
        if self.prior_model is not None and self.prior_model not in MODELS:
            raise ValueError("Unsupported prior Codex model")
        if self.prior_effort is not None and self.prior_effort not in EFFORTS:
            raise ValueError("Unsupported prior Codex effort")
        if type(self.attempt) is not int or type(self.max_attempts) is not int:
            raise ValueError("Attempt bounds must be integers")
        if self.attempt < 1 or self.max_attempts < 1 or self.attempt > self.max_attempts:
            raise ValueError("Attempt is outside its bound")
        if self.failure_kind is not None:
            if self.attempt < 2 or self.prior_model is None or self.prior_effort is None:
                raise ValueError("Retry decisions require an attempt and prior configuration")
        elif self.prior_model is not None or self.prior_effort is not None:
            raise ValueError("Prior configuration requires a failure kind")
        if not isinstance(self.handoff_refs, tuple) or any(
            not isinstance(ref, str) or not ref or "\n" in ref or len(ref) > 512
            for ref in self.handoff_refs
        ):
            raise ValueError("Handoff references must be bounded single-line strings")
        for name in ("choice_reason", "deeper_analysis_evidence", "prior_action",
                     "correction_evidence"):
            _one_line(getattr(self, name), name)


@dataclass(frozen=True)
class WorkerDecision:
    policy_version: str
    action: str
    task_class: str
    effective_task_class: str
    risk: str
    ambiguity: str
    model: str
    effort: str
    reason: str
    override_source: str
    prior_model: str | None
    prior_effort: str | None
    prior_action: str | None
    next_action: str
    failure_kind: str | None
    attempt: int
    max_attempts: int
    escalated: bool
    handoff_refs: tuple[str, ...]
    choice_reason: str | None

    def receipt(self):
        return {
            "policy_version": self.policy_version,
            "action": self.action,
            "task_class": self.task_class,
            "effective_task_class": self.effective_task_class,
            "risk": self.risk,
            "ambiguity": self.ambiguity,
            "selected": {"model": self.model, "effort": self.effort},
            "reason": self.reason,
            "choice_reason": self.choice_reason,
            "override_source": self.override_source,
            "prior_configuration": (
                {"model": self.prior_model, "effort": self.prior_effort}
                if self.prior_model is not None else None
            ),
            "prior_action": self.prior_action,
            "next_action": self.next_action,
            "failure_kind": self.failure_kind,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "escalated": self.escalated,
            "handoff_refs": list(self.handoff_refs),
        }


def _decision(values: PolicyInput, *, action, effective_class, model, effort,
              reason, source="policy", next_action, escalated=False):
    return WorkerDecision(
        policy_version=POLICY_VERSION, action=action,
        task_class=values.task_class, effective_task_class=effective_class,
        risk=values.risk, ambiguity=values.ambiguity,
        model=model, effort=effort, reason=reason, override_source=source,
        prior_model=values.prior_model, prior_effort=values.prior_effort,
        prior_action=values.prior_action, next_action=next_action,
        failure_kind=values.failure_kind, attempt=values.attempt,
        max_attempts=values.max_attempts, escalated=escalated,
        handoff_refs=values.handoff_refs, choice_reason=values.choice_reason,
    )


def _at_least(model, effort, floor_model, floor_effort):
    return (
        MODELS[max(_MODEL_RANK[model], _MODEL_RANK[floor_model])],
        EFFORTS[max(_EFFORT_RANK[effort], _EFFORT_RANK[floor_effort])],
    )


def _blocked(values, effective_class, model, effort, reason, next_action, *, source="policy"):
    return _decision(values, action="blocked", effective_class=effective_class,
                     model=model, effort=effort, reason=reason, source=source,
                     next_action=next_action)


def decide_worker(values: PolicyInput) -> WorkerDecision:
    """Return one deterministic dispatch decision without reading task prose."""
    if not isinstance(values, PolicyInput):
        raise ValueError("Validated PolicyInput is required")

    effective_class = values.task_class
    if (values.phase == "implementation" and values.contract_resolved
            and values.task_class in ("complex", "frontier")):
        effective_class = values.implementation_class
    model, effort = CLASS_SELECTIONS[effective_class]
    reasons = [f"{effective_class} task policy"]

    if values.ambiguity == "unknown":
        model, effort = _at_least(model, effort, "gpt-6.1-sol", "medium")
        reasons.append("unknown low-risk work floors at Sol/medium")
    elif values.ambiguity == "ambiguous":
        model, effort = _at_least(model, effort, "gpt-6.1-sol", "high")
        reasons.append("ambiguous approach floors at Sol/high")
    if values.risk == "high":
        model, effort = _at_least(model, effort, "gpt-6.1-sol", "high")
        reasons.append("high-risk work floors at Sol/high")

    if values.missing_context:
        return _blocked(values, effective_class, model, effort,
                        "Required task context is missing", "supply_missing_context")
    if values.broken_environment:
        return _blocked(values, effective_class, model, effort,
                        "The execution environment is broken", "repair_environment")
    if values.deterministic_tool_sufficient:
        return _decision(values, action="skipped", effective_class=effective_class,
                         model=model, effort=effort,
                         reason="A deterministic tool is sufficient; no worker is justified",
                         next_action="use_deterministic_tool")

    source = "policy"
    if values.failure_kind:
        model, effort = values.prior_model, values.prior_effort
        source = "failure_adaptation"
        if values.failure_kind == "missing_information":
            return _blocked(values, effective_class, model, effort,
                            "Retry cannot repair missing information", "supply_missing_context",
                            source=source)
        if values.failure_kind == "environment":
            return _blocked(values, effective_class, model, effort,
                            "Retry cannot repair the environment", "repair_environment",
                            source=source)
        if values.attempt >= values.max_attempts or (model == MODELS[-1] and effort == EFFORTS[-1]):
            return _decision(values, action="stop_replan", effective_class=effective_class,
                             model=model, effort=effort,
                             reason="The bounded attempt budget or hardest configuration is exhausted",
                             source=source, next_action="replan_or_stop")
        if values.failure_kind == "shallow_reasoning":
            if effort == EFFORTS[-1]:
                return _decision(values, action="stop_replan", effective_class=effective_class,
                                 model=model, effort=effort,
                                 reason="Reasoning effort is already at the approved maximum",
                                 source=source, next_action="replan_or_stop")
            effort = EFFORTS[_EFFORT_RANK[effort] + 1]
            reasons = ["Shallow reasoning increases effort without changing the model"]
        elif values.failure_kind in ("misunderstanding", "approach_failure"):
            if model == MODELS[-1]:
                return _decision(values, action="stop_replan", effective_class=effective_class,
                                 model=model, effort=effort,
                                 reason="No higher approved model remains; replan instead of blind retry",
                                 source=source, next_action="replan_or_stop")
            model = MODELS[_MODEL_RANK[model] + 1]
            effort = EFFORTS[max(_EFFORT_RANK[effort], _EFFORT_RANK[_MODEL_BASE_EFFORT[model]])]
            reasons = ["Misunderstanding or approach failure upgrades the model"]
        else:
            if not values.correction_evidence:
                return _decision(values, action="stop_replan", effective_class=effective_class,
                                 model=model, effort=effort,
                                 reason="The same defect repeated without an evidence-informed correction",
                                 source=source, next_action="add_evidence_informed_correction")
            if model == MODELS[-1]:
                return _decision(values, action="stop_replan", effective_class=effective_class,
                                 model=model, effort=effort,
                                 reason="The corrected attempt has no higher approved model",
                                 source=source, next_action="replan_or_stop")
            model = MODELS[_MODEL_RANK[model] + 1]
            effort = EFFORTS[max(_EFFORT_RANK[effort], _EFFORT_RANK[_MODEL_BASE_EFFORT[model]])]
            reasons = ["Evidence-informed correction permits one model escalation"]

    pinned = values.pinned_model is not None or values.pinned_effort is not None
    overridden = values.model_override is not None or values.effort_override is not None
    if overridden:
        model = values.model_override or model
        effort = values.effort_override or effort
        source = values.override_source
        reasons.append("trusted caller override")
    if pinned:
        model = values.pinned_model or model
        effort = values.pinned_effort or effort
        source = ("mixed" if (
            (values.model_override is not None and values.pinned_model is None)
            or (values.effort_override is not None and values.pinned_effort is None)
        ) else "user_pin")
        reasons.append("authorized user pin")

    # Task classes choose defaults; only risk/ambiguity are mandatory floors.
    # This keeps explicit, audited legacy overrides exact.
    floor_model, floor_effort = CLASS_SELECTIONS["mechanical"]
    if values.ambiguity == "unknown":
        floor_model, floor_effort = _at_least(floor_model, floor_effort, "gpt-6.1-sol", "medium")
    if values.ambiguity == "ambiguous" or values.risk == "high":
        floor_model, floor_effort = _at_least(floor_model, floor_effort, "gpt-6.1-sol", "high")
    safe_model, safe_effort = _at_least(model, effort, floor_model, floor_effort)
    if (safe_model, safe_effort) != (model, effort) and (pinned or overridden):
        return _blocked(values, effective_class, model, effort,
                        "Explicit configuration is below the policy floor",
                        "select_minimum_safe_pair", source=source)
    model, effort = safe_model, safe_effort

    effort_is_pinned = values.pinned_effort is not None
    evidence_exempt = effort_is_pinned
    if not evidence_exempt and effort == "xhigh" and not values.deeper_analysis_evidence:
        return _blocked(values, effective_class, model, effort,
                        "xhigh requires explicit deeper-analysis evidence",
                        "supply_deeper_analysis_evidence", source=source)
    if not evidence_exempt and effort == "max" and not (
        values.deeper_analysis_evidence and values.hard_judgment
        and values.high_failure_cost and values.choice_reason
    ):
        return _blocked(values, effective_class, model, effort,
                        "max requires evidence, hard judgment, high failure cost, and a reason",
                        "supply_max_selection_evidence", source=source)

    escalated = False
    if values.prior_model is not None:
        escalated = (_MODEL_RANK[model], _EFFORT_RANK[effort]) > (
            _MODEL_RANK[values.prior_model], _EFFORT_RANK[values.prior_effort]
        )
        if not escalated and values.failure_kind == "repeated_same_defect":
            return _decision(values, action="stop_replan", effective_class=effective_class,
                             model=model, effort=effort,
                             reason="Repeated defects may not retry the same configuration",
                             source=source, next_action="replan_or_stop")
    return _decision(values, action="spawn", effective_class=effective_class,
                     model=model, effort=effort, reason="; ".join(reasons),
                     source=source, next_action="spawn_once", escalated=escalated)
