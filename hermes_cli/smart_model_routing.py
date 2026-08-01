"""Policy-first model routing for a single Hermes turn.

This module deliberately chooses a lane before choosing a provider/model.  It is
the small Python counterpart to the Personal Hermes control-plane policy sketch:
non-mutating requests may be routed to configured model profiles, while
mutation-capable or high-risk work fails closed until durable coordination and
approval state exist.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


PLANNING_KEYWORDS = (
    "plan",
    "planning",
    "proposal",
    "architecture",
    "architect",
    "design",
    "requirements",
    "roadmap",
    "approach",
    "how should",
    "how to",
    "계획",
    "설계",
    "아키텍처",
    "어떻게",
    "방향",
)

FRESHNESS_KEYWORDS = (
    "latest",
    "current",
    "today",
    "now",
    "recent",
    "citation",
    "citations",
    "cite",
    "source",
    "sources",
    "search",
    "browse",
    "look up",
    "최신",
    "현재",
    "오늘",
    "검색",
    "출처",
    "인용",
)

MUTATION_KEYWORDS = (
    "implement",
    "modify",
    "edit",
    "write",
    "change",
    "delete",
    "remove",
    "drop",
    "refactor",
    "migrate",
    "migration",
    "port",
    "upgrade",
    "convert",
    "move",
    "apply",
    "fix",
    "add",
    "commit",
    "push",
    "구현",
    "수정",
    "고쳐",
    "변경",
    "바꿔",
    "삭제",
    "제거",
    "추가",
    "리팩터",
    "마이그레이션",
    "포팅",
    "업그레이드",
    "전환",
    "이전",
    "커밋",
    "푸시",
)

RISK_KEYWORDS = (
    "auth",
    "authentication",
    "authorization",
    "permission",
    "security",
    "secret",
    "payment",
    "billing",
    "migration",
    "schema",
    "database",
    "deploy",
    "release",
    "ci",
    "delete",
    "drop",
    "destructive",
    "인증",
    "권한",
    "보안",
    "시크릿",
    "결제",
    "마이그레이션",
    "스키마",
    "데이터베이스",
    "배포",
    "릴리즈",
    "삭제",
)

SIMPLE_KEYWORDS = (
    "what is",
    "who is",
    "when is",
    "where is",
    "define",
    "summarize",
    "explain",
    "translate",
    "무엇",
    "누구",
    "언제",
    "어디",
    "요약",
    "설명",
    "번역",
)

GJC_EXPLICIT_KEYWORDS = (
    "gjc",
    "ralplan",
    "ultragoal",
    "deep-interview",
    "deep interview",
)

GJC_PLAN_KEYWORDS = (
    "plan.md",
    "handoff.md",
    "handoff",
    "pending approval",
    "approved plan",
    "ledger",
    "workflow ledger",
    "existing gjc",
    "gjc session",
)

MIGRATION_OR_PORT_KEYWORDS = (
    "migration",
    "migrate",
    "port",
    "move",
    "convert",
    "from",
    "to",
    "source",
    "target",
    "repo",
    "codebase",
    "package",
    "framework",
    "app",
    "마이그레이션",
    "포팅",
    "이전",
    "전환",
)

SDK_UPGRADE_KEYWORDS = (
    "expo",
    "react native",
    "rn",
    "sdk",
    "storybook",
    "native dependency",
    "package baseline",
    "package boundary",
    "upgrade",
    "dependency upgrade",
)

VISUAL_QA_KEYWORDS = (
    "visual qa",
    "layout",
    "screenshot",
    "still fails",
    "does not match",
    "reverted",
    "again",
    "qa screenshot",
)

READONLY_CLEANUP_ADVICE_KEYWORDS = (
    "safe to delete",
    "can be deleted",
    "can i delete",
    "what can i delete",
    "what to delete",
    "delete candidates",
    "deletion candidates",
    "cleanup candidates",
    "clean up candidates",
    "삭제해도 되는",
    "지워도 되는",
    "삭제 가능한",
    "지울 수 있는",
    "삭제 후보",
    "지울 후보",
    "필요없는",
    "불필요한",
)

READONLY_ADVICE_VERBS = (
    "list",
    "list up",
    "listup",
    "show",
    "tell me",
    "explain",
    "identify",
    "find",
    "check",
    "analyze",
    "audit",
    "recommend",
    "알려줘",
    "알려 줘",
    "목록",
    "리스트",
    "리스트업",
    "찾아",
    "확인",
    "분석",
    "추천",
    "후보",
    "뭐",
    "무엇",
    "어떤",
)

CLEANUP_CONTEXT_KEYWORDS = (
    "cache",
    "caches",
    "disk",
    "storage",
    "space",
    "cleanup",
    "clean up",
    "free up",
    "temporary",
    "temp",
    "downloads",
    "logs",
    "저장공간",
    "저장 공간",
    "공간",
    "디스크",
    "캐시",
    "임시",
    "다운로드",
    "로그",
    "정리",
    "확보",
)

EXPLICIT_DELETE_EXECUTION_KEYWORDS = (
    "delete it",
    "delete them",
    "remove it",
    "remove them",
    "clean it",
    "clean them",
    "wipe",
    "rm -rf",
    "삭제해줘",
    "삭제해 줘",
    "삭제 해줘",
    "지워줘",
    "지워 줘",
    "제거해줘",
    "제거해 줘",
    "제거 해줘",
    "정리해줘",
    "정리해 줘",
    "정리 해줘",
)

PARALLEL_WORKER_KEYWORDS = (
    "parallel workers",
    "team workflow",
    "gjc team",
    "worker team",
)

VISIBLE_SESSION_KEYWORDS = (
    "tmux",
    "visible session",
    "operator-visible",
    "worktree session",
)

LANE_ALIASES = {
    "cheap_chat": ("cheap_chat", "simple", "fast", "small"),
    "reasoning": ("reasoning", "planning", "analysis", "complex"),
    "codex_implementation": ("codex_implementation", "codex", "implementation", "coding"),
    "research_readonly": ("research_readonly", "research", "freshness", "citations"),
    "multimodal": ("multimodal", "vision", "image"),
}


@dataclass(frozen=True)
class RouteTarget:
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    api_mode: str = ""
    max_tokens: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.provider or self.model or self.base_url or self.api_mode)


@dataclass(frozen=True)
class RoutingClassification:
    vague: bool = False
    repo_mutation: bool = False
    high_risk: bool = False
    freshness: bool = False
    multimodal: bool = False
    simple: bool = False
    coding_work: bool = False
    explicit_gjc: bool = False
    large_migration_or_port: bool = False
    sdk_native_upgrade: bool = False
    plan_handoff_continuation: bool = False
    repeated_visual_qa: bool = False
    parallel_workers_requested: bool = False
    visible_session_requested: bool = False
    readonly_cleanup_advice: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    enabled: bool
    selected_lane: str = "primary"
    allowed_lanes: tuple[str, ...] = ()
    reason: str = "Smart model routing is disabled."
    target: RouteTarget | None = None
    classification: RoutingClassification = field(default_factory=RoutingClassification)
    required_gates: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    fail_closed: bool = False
    allow_mutation: bool = False
    mutation_classes: tuple[str, ...] = ()
    gjc_workflow: str = ""
    policy_version: str = "smart-routing-v2"

    @property
    def should_route(self) -> bool:
        return self.enabled and not self.fail_closed and self.target is not None and self.target.configured

    def blocker_message(self) -> str:
        gates = ", ".join(self.required_gates) if self.required_gates else "policy approval"
        return (
            "Smart model routing blocked this turn before model selection. "
            f"{self.reason} Required gate(s): {gates}."
        )


def text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") in {"image_url", "input_image"}:
                    parts.append("[image]")
            else:
                parts.append(str(item))
        return " ".join(part for part in parts if part)
    return "" if message is None else str(message)


def message_has_multimodal(message: Any) -> bool:
    if not isinstance(message, list):
        return False
    for item in message:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"image_url", "input_image", "input_audio", "file"}:
            return True
    return False


def _includes_any(text: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        if keyword.isascii() and re.fullmatch(r"[a-z0-9_-]+", keyword):
            if re.search(rf"(?<![a-z0-9_-]){re.escape(keyword)}(?![a-z0-9_-])", text):
                return True
        elif keyword in text:
            return True
    return False


def _has_source_target_shape(text: str) -> bool:
    return bool(
        re.search(r"\bfrom\b.+\bto\b", text)
        or re.search(r"\bsource\b.+\btarget\b", text)
        or re.search(r"\bbetween\b.+\band\b", text)
        or "에서" in text and ("로" in text or "으로" in text)
    )


def _is_readonly_cleanup_advice(text: str) -> bool:
    """Detect requests asking what could be deleted, not asking to delete it."""
    advisory_shape = (
        _includes_any(text, READONLY_CLEANUP_ADVICE_KEYWORDS)
        or (
            _includes_any(text, CLEANUP_CONTEXT_KEYWORDS)
            and _includes_any(text, READONLY_ADVICE_VERBS)
        )
    )
    if not advisory_shape:
        return False
    return not _includes_any(text, EXPLICIT_DELETE_EXECUTION_KEYWORDS)


def _infer_mutation_classes(text: str, classification: RoutingClassification) -> tuple[str, ...]:
    if not classification.repo_mutation:
        return ()
    classes = {"repo_write"}
    if classification.high_risk:
        classes.add("high_risk")
    if classification.large_migration_or_port:
        classes.add("large_migration_or_port")
    if classification.sdk_native_upgrade:
        classes.add("sdk_native_upgrade")
    if _includes_any(text, ("auth", "authentication", "authorization", "permission", "security", "secret", "인증", "권한", "보안")):
        classes.add("security_sensitive")
    if _includes_any(text, ("database", "schema", "db", "data migration", "데이터베이스", "스키마")):
        classes.add("data_migration")
    if _includes_any(text, ("delete", "remove", "drop", "destructive", "삭제", "제거")):
        classes.add("destructive")
    if _includes_any(text, ("deploy", "release", "ci", "배포", "릴리즈")):
        classes.add("deployment")
    return tuple(sorted(classes))


def _gjc_lane_for(classification: RoutingClassification) -> str | None:
    if classification.parallel_workers_requested:
        return "gjc_team"
    if classification.visible_session_requested:
        return "gjc_visible_session"
    if (
        classification.explicit_gjc
        or classification.large_migration_or_port
        or classification.sdk_native_upgrade
        or classification.plan_handoff_continuation
        or classification.repeated_visual_qa
    ):
        return "gjc_ralplan"
    return None


def _gjc_workflow_for(text: str, lane: str) -> str:
    if lane == "gjc_team":
        return "team"
    if lane == "gjc_visible_session":
        return "visible_session"
    if _includes_any(text, ("deep-interview", "deep interview")):
        return "deep-interview"
    if _includes_any(text, ("ultragoal",)):
        return "ultragoal"
    return "ralplan"


def _routing_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    raw = config.get("smart_model_routing")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _gate_mode(smr: Mapping[str, Any], key: str, default: str = "block") -> str:
    gates = smr.get("gates")
    if not isinstance(gates, Mapping):
        return default
    value = str(gates.get(key) or default).strip().lower()
    return value or default


def _target_from_mapping(raw: Any) -> RouteTarget | None:
    if not isinstance(raw, Mapping):
        return None
    max_tokens = raw.get("max_tokens")
    if not isinstance(max_tokens, int):
        max_tokens = None
    target = RouteTarget(
        provider=str(raw.get("provider") or "").strip(),
        model=str(raw.get("model") or raw.get("default") or "").strip(),
        base_url=str(raw.get("base_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        api_mode=str(raw.get("api_mode") or "").strip(),
        max_tokens=max_tokens,
    )
    return target if target.configured else None


def route_target(smr: Mapping[str, Any], lane: str) -> RouteTarget | None:
    routes = smr.get("routes")
    if not isinstance(routes, Mapping):
        return None
    for key in LANE_ALIASES.get(lane, (lane,)):
        target = _target_from_mapping(routes.get(key))
        if target is not None:
            return target
    return None


def classify_request(
    message: Any,
    *,
    has_multimodal: bool = False,
    repo_mutation: bool | None = None,
) -> RoutingClassification:
    text = text_from_message(message)
    lowered = text.lower()
    readonly_cleanup_advice = _is_readonly_cleanup_advice(lowered)
    mutation = _includes_any(lowered, MUTATION_KEYWORDS)
    if repo_mutation is not None:
        mutation = bool(repo_mutation)
    risk_sensitive = _includes_any(lowered, RISK_KEYWORDS)
    if readonly_cleanup_advice:
        mutation = False
        risk_sensitive = False
    vague = _includes_any(lowered, PLANNING_KEYWORDS) or readonly_cleanup_advice
    freshness = _includes_any(lowered, FRESHNESS_KEYWORDS) and not readonly_cleanup_advice
    multimodal = bool(has_multimodal or message_has_multimodal(message))
    coding_work = mutation or _includes_any(lowered, ("code", "test", "repo", "bug", "코드", "테스트", "버그"))
    simple = (len(lowered.split()) <= 18 or _includes_any(lowered, SIMPLE_KEYWORDS)) and not readonly_cleanup_advice
    explicit_gjc = _includes_any(lowered, GJC_EXPLICIT_KEYWORDS)
    plan_handoff_continuation = _includes_any(lowered, GJC_PLAN_KEYWORDS)
    large_migration_or_port = (
        _includes_any(lowered, ("migration", "migrate", "port", "codebase", "framework", "마이그레이션", "포팅"))
        and (_has_source_target_shape(lowered) or _includes_any(lowered, ("repo", "package", "app", "monorepo", "codebase")))
    )
    sdk_native_upgrade = (
        _includes_any(lowered, SDK_UPGRADE_KEYWORDS)
        and _includes_any(lowered, ("upgrade", "update", "migrate", "bump", "올려", "업그레이드", "마이그레이션"))
    )
    repeated_visual_qa = (
        _includes_any(lowered, VISUAL_QA_KEYWORDS)
        and _includes_any(lowered, ("still", "again", "after", "reverted", "failed", "fails", "does not match"))
    )
    parallel_workers_requested = _includes_any(lowered, PARALLEL_WORKER_KEYWORDS)
    visible_session_requested = _includes_any(lowered, VISIBLE_SESSION_KEYWORDS)
    return RoutingClassification(
        vague=vague,
        repo_mutation=mutation,
        high_risk=bool(risk_sensitive and (mutation or vague)),
        freshness=freshness,
        multimodal=multimodal,
        simple=simple,
        coding_work=coding_work,
        explicit_gjc=explicit_gjc,
        large_migration_or_port=large_migration_or_port,
        sdk_native_upgrade=sdk_native_upgrade,
        plan_handoff_continuation=plan_handoff_continuation,
        repeated_visual_qa=repeated_visual_qa,
        parallel_workers_requested=parallel_workers_requested,
        visible_session_requested=visible_session_requested,
        readonly_cleanup_advice=readonly_cleanup_advice,
    )


def decide_route(
    message: Any,
    *,
    config: Mapping[str, Any] | None = None,
    has_multimodal: bool = False,
    explicit_model: bool = False,
    repo_mutation: bool | None = None,
) -> RoutingDecision:
    smr = _routing_config(config)
    if not _truthy(smr.get("enabled", False)):
        return RoutingDecision(enabled=False)

    classification = classify_request(
        message,
        has_multimodal=has_multimodal,
        repo_mutation=repo_mutation,
    )
    text = text_from_message(message).lower()
    mutation_classes = _infer_mutation_classes(text, classification)

    gjc_lane = _gjc_lane_for(classification)
    if gjc_lane:
        gjc_workflow = _gjc_workflow_for(text, gjc_lane)
        if _gate_mode(smr, "gjc_escalation") != "allow":
            return RoutingDecision(
                enabled=True,
                selected_lane=gjc_lane,
                allowed_lanes=(gjc_lane,),
                reason=(
                    "This request matches the narrow GJC escalation policy. "
                    "GJC escalation is disabled until the policy gate is set to allow."
                ),
                classification=classification,
                required_gates=("gjc_escalation", "coordinator_mcp", "approved_plan"),
                blockers=("gjc_coordination_required",),
                fail_closed=True,
                allow_mutation=False,
                mutation_classes=mutation_classes,
                gjc_workflow=gjc_workflow,
            )
        return RoutingDecision(
            enabled=True,
            selected_lane=gjc_lane,
            allowed_lanes=(gjc_lane,),
            reason=(
                "This request matches the narrow GJC escalation policy. "
                "Coordinator MCP and durable approval state must own the GJC session before work can start."
            ),
            classification=classification,
            required_gates=("coordinator_mcp", "approved_plan", "evidence_store"),
            blockers=(),
            fail_closed=False,
            allow_mutation=classification.repo_mutation,
            mutation_classes=mutation_classes,
            gjc_workflow=gjc_workflow,
        )

    if classification.repo_mutation and _gate_mode(smr, "repo_mutation") != "allow":
        return RoutingDecision(
            enabled=True,
            selected_lane="blocked",
            allowed_lanes=(),
            reason="Repository mutation requires a durable plan, approval, and evidence boundary.",
            classification=classification,
            required_gates=("approved_plan", "mutation_approval", "evidence_store"),
            blockers=("repo_mutation_requires_coordination",),
            fail_closed=True,
            allow_mutation=False,
            mutation_classes=mutation_classes,
        )

    if classification.high_risk and _gate_mode(smr, "high_risk") != "allow":
        return RoutingDecision(
            enabled=True,
            selected_lane="blocked",
            allowed_lanes=(),
            reason="High-risk work requires an explicit planning or approval lane.",
            classification=classification,
            required_gates=("risk_plan", "approval_gate"),
            blockers=("high_risk_requires_coordination",),
            fail_closed=True,
            allow_mutation=False,
            mutation_classes=mutation_classes,
        )

    if _truthy(smr.get("respect_explicit_model", False)) and explicit_model:
        return RoutingDecision(
            enabled=True,
            selected_lane="primary",
            allowed_lanes=("primary",),
            reason="An explicit model is active, so smart routing preserved the primary route.",
            classification=classification,
            allow_mutation=classification.repo_mutation,
            mutation_classes=mutation_classes,
        )

    if classification.multimodal:
        lane = "multimodal"
        reason = "Multimodal input routes to the configured multimodal model."
    elif classification.freshness:
        lane = "research_readonly"
        reason = "Freshness or citations route to the configured read-only research model."
    elif classification.vague:
        lane = "reasoning"
        reason = "Planning or ambiguous work routes to the configured reasoning model."
    elif classification.coding_work:
        lane = "codex_implementation"
        reason = "Ordinary coding work routes to the Codex-default implementation lane."
    elif classification.simple:
        lane = "cheap_chat"
        reason = "Simple read-only chat routes to the configured cheap model."
    else:
        lane = "reasoning"
        reason = "General non-trivial work routes to the configured reasoning model."

    target = route_target(smr, lane)
    if target is None:
        return RoutingDecision(
            enabled=True,
            selected_lane="primary",
            allowed_lanes=("primary", lane),
            reason=f"{reason} No target is configured for lane {lane}; preserving the primary route.",
            classification=classification,
            required_gates=("mutation_approval", "evidence_store") if classification.repo_mutation else (),
            allow_mutation=classification.repo_mutation,
            mutation_classes=mutation_classes,
        )
    return RoutingDecision(
        enabled=True,
        selected_lane=lane,
        allowed_lanes=(lane,),
        reason=reason,
        target=target,
        classification=classification,
        required_gates=("mutation_approval", "evidence_store") if classification.repo_mutation else (),
        allow_mutation=classification.repo_mutation,
        mutation_classes=mutation_classes,
    )


def decision_metadata(decision: RoutingDecision) -> dict[str, Any]:
    target = decision.target
    return {
        "policy_version": decision.policy_version,
        "enabled": decision.enabled,
        "selected_lane": decision.selected_lane,
        "allowed_lanes": list(decision.allowed_lanes),
        "reason": decision.reason,
        "fail_closed": decision.fail_closed,
        "blockers": list(decision.blockers),
        "required_gates": list(decision.required_gates),
        "allow_mutation": decision.allow_mutation,
        "mutation_classes": list(decision.mutation_classes),
        "gjc_workflow": decision.gjc_workflow,
        "classification": {
            "vague": decision.classification.vague,
            "repo_mutation": decision.classification.repo_mutation,
            "high_risk": decision.classification.high_risk,
            "freshness": decision.classification.freshness,
            "multimodal": decision.classification.multimodal,
            "simple": decision.classification.simple,
            "coding_work": decision.classification.coding_work,
            "explicit_gjc": decision.classification.explicit_gjc,
            "large_migration_or_port": decision.classification.large_migration_or_port,
            "sdk_native_upgrade": decision.classification.sdk_native_upgrade,
            "plan_handoff_continuation": decision.classification.plan_handoff_continuation,
            "repeated_visual_qa": decision.classification.repeated_visual_qa,
            "parallel_workers_requested": decision.classification.parallel_workers_requested,
            "visible_session_requested": decision.classification.visible_session_requested,
            "readonly_cleanup_advice": decision.classification.readonly_cleanup_advice,
        },
        "target": None
        if target is None
        else {
            "provider": target.provider,
            "model": target.model,
            "base_url": target.base_url,
            "api_mode": target.api_mode,
            "max_tokens": target.max_tokens,
        },
    }


def record_current_task_policy_decision(metadata: Mapping[str, Any] | None) -> int | None:
    """Best-effort bridge from per-turn policy metadata into Kanban state.

    Dispatcher-spawned workers carry ``HERMES_KANBAN_TASK``/``RUN_ID`` in their
    environment.  When present, persist the decision as a first-class control
    plane record.  Normal foreground CLI/gateway sessions have no task id and
    remain untouched.
    """
    if not isinstance(metadata, Mapping):
        return None
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return None
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    run_id = None
    if raw_run_id:
        try:
            run_id = int(raw_run_id)
        except ValueError:
            run_id = None

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        return kb.record_policy_decision(
            conn,
            task_id,
            dict(metadata),
            run_id=run_id,
        )


def apply_decision_to_runtime(
    decision: RoutingDecision,
    *,
    current_model: str,
    current_runtime: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Return ``(model, runtime, metadata)`` after applying a routing decision.

    Provider resolution is deliberately late and best-effort for non-mutating
    work.  If a configured route target cannot resolve credentials, the caller
    keeps the primary route and receives metadata with ``target_resolution_error``.
    Fail-closed blockers are represented in metadata and never reach provider
    resolution.
    """

    runtime = dict(current_runtime or {})
    metadata = decision_metadata(decision)

    if decision.fail_closed:
        metadata["blocked"] = True
        metadata["message"] = decision.blocker_message()
        return current_model, runtime, metadata

    if not decision.should_route or decision.target is None:
        return current_model, runtime, metadata if decision.enabled else None

    target = decision.target
    target_model = target.model or current_model
    target_provider = target.provider.strip()
    should_resolve_provider = bool(
        target_provider
        and target_provider != "main"
        and target_provider != str(runtime.get("provider") or "").strip()
    ) or bool(target.base_url or target.api_key)

    if should_resolve_provider:
        requested = target_provider or "custom"
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            resolved = resolve_runtime_provider(
                requested=requested,
                explicit_base_url=target.base_url or None,
                explicit_api_key=target.api_key or None,
                target_model=target_model,
            )
            runtime = {
                "api_key": resolved.get("api_key"),
                "base_url": resolved.get("base_url"),
                "provider": resolved.get("provider"),
                "api_mode": resolved.get("api_mode"),
                "command": resolved.get("command"),
                "args": list(resolved.get("args") or []),
                "credential_pool": resolved.get("credential_pool"),
                "max_tokens": runtime.get("max_tokens"),
            }
        except Exception as exc:
            metadata["target_resolution_error"] = str(exc)
            metadata["selected_lane"] = "primary"
            metadata["reason"] = (
                f"{decision.reason} Target provider resolution failed; preserving the primary route."
            )
            return current_model, dict(current_runtime or {}), metadata

    if target.api_mode:
        runtime["api_mode"] = target.api_mode
    if target.base_url:
        runtime["base_url"] = target.base_url
    if target.api_key:
        runtime["api_key"] = target.api_key
    if target.max_tokens is not None:
        runtime["max_tokens"] = target.max_tokens

    # Keep OpenAI-compatible local/custom endpoints usable when no key is needed.
    base_url = str(runtime.get("base_url") or "")
    api_key = runtime.get("api_key")
    if base_url and "openrouter.ai" not in base_url and not api_key:
        runtime["api_key"] = "no-key-required"

    metadata["applied"] = True
    return target_model, runtime, metadata
