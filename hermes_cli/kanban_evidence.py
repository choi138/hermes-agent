"""Fail-closed evidence contracts for durable Kanban terminal transitions.

Only identifiers, enums, timestamps, and cryptographic digests cross this
boundary. Raw prompts, commands, tool output, environment values, headers, and
credentials are deliberately not representable by the manifest schema.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TASK_RE = re.compile(r"^t_[0-9a-f]{8,64}$")
_INTENT_RE = re.compile(r"^ti_[0-9a-f]{16,64}$")

_DIGEST_FIELDS = frozenset(
    {
        "config_digest",
        "lockfile_digest",
        "toolchain_digest",
        "backend_digest",
        "command_digest",
        "test_plan_digest",
        "fixture_digest",
        "seed_digest",
        "checkpoint_digest",
    }
)
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "run_id",
        "terminal_intent_id",
        "action",
        "block_kind",
        "source_commit",
        "source_tree",
        *_DIGEST_FIELDS,
        "backend_kind",
        "policy_version",
        "evidence_at",
        "freshness_seconds",
        "failure_class",
        "side_effect",
    }
)
_ACTIONS = frozenset({"complete", "block"})
_BLOCK_KINDS = frozenset({"needs_input", "capability", "transient"})
_SIDE_EFFECTS = frozenset({"none", "idempotent", "unknown"})
_FAILURE_CLASSES = frozenset(
    {
        "none", "network", "provider", "worker", "terminal_write",
        "assertion", "test", "qa", "spec", "policy", "capability",
        "credential", "approval", "unknown",
    }
)
_TRANSIENT = frozenset({"network", "provider", "worker", "terminal_write"})
_NO_RETRY = frozenset({"assertion", "test", "qa", "spec", "policy"})
_STABLE_BLOCK = frozenset({"capability", "credential", "approval"})
_RISK_LEVELS = frozenset({"low", "medium", "high"})
_DETERMINISTIC_VERDICT_SOURCES = frozenset(
    {"deterministic_test", "deterministic_build", "static_analysis"}
)
_VERDICT_SOURCES = _DETERMINISTIC_VERDICT_SOURCES | frozenset({"llm", "reviewer"})


def canonical_evidence_manifest(manifest: Mapping[str, Any]) -> str:
    """Return deterministic JSON after strict schema validation."""
    _validate_shape(manifest)
    return json.dumps(dict(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def evidence_manifest_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_evidence_manifest(manifest).encode("utf-8")).hexdigest()


def _validate_shape(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping):
        raise ValueError("evidence manifest must be an object")
    keys = frozenset(manifest)
    if keys != _REQUIRED_FIELDS:
        missing = sorted(_REQUIRED_FIELDS - keys)
        unknown = sorted(keys - _REQUIRED_FIELDS)
        raise ValueError(f"invalid evidence manifest fields; missing={missing}, unknown={unknown}")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported evidence manifest schema_version")
    if type(manifest["run_id"]) is not int or manifest["run_id"] <= 0:
        raise ValueError("run_id must be a positive integer")
    if type(manifest["evidence_at"]) is not int or manifest["evidence_at"] <= 0:
        raise ValueError("evidence_at must be a positive integer")
    freshness = manifest["freshness_seconds"]
    if type(freshness) is not int or not 0 < freshness <= 604_800:
        raise ValueError("freshness_seconds must be between 1 and 604800")
    if not isinstance(manifest["task_id"], str) or not _TASK_RE.fullmatch(manifest["task_id"]):
        raise ValueError("invalid task_id")
    if not isinstance(manifest["terminal_intent_id"], str) or not _INTENT_RE.fullmatch(manifest["terminal_intent_id"]):
        raise ValueError("invalid terminal_intent_id")
    action = manifest["action"]
    failure_class = manifest["failure_class"]
    side_effect = manifest["side_effect"]
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ValueError("invalid terminal action")
    if not isinstance(side_effect, str) or side_effect not in _SIDE_EFFECTS:
        raise ValueError("invalid side_effect")
    if not isinstance(failure_class, str) or failure_class not in _FAILURE_CLASSES:
        raise ValueError("invalid failure_class")
    block_kind = manifest["block_kind"]
    if action == "complete":
        if failure_class != "none":
            raise ValueError("complete evidence must have failure_class=none")
        if block_kind is not None:
            raise ValueError("complete evidence must have block_kind=null")
    else:
        if failure_class == "none":
            raise ValueError("block evidence requires a failure class")
        if not isinstance(block_kind, str) or block_kind not in _BLOCK_KINDS:
            raise ValueError("block evidence requires a stable non-dependency block_kind")
    for field in ("source_commit", "source_tree"):
        value = manifest[field]
        if not isinstance(value, str) or not _GIT_OBJECT_RE.fullmatch(value):
            raise ValueError(f"{field} must be a lowercase SHA-1 or SHA-256 object id")
    for field in _DIGEST_FIELDS:
        value = manifest[field]
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    for field in ("backend_kind", "policy_version"):
        value = manifest[field]
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError(f"invalid {field}")


def validate_evidence_manifest(
    manifest: Mapping[str, Any], *, digest: str, task_id: str, run_id: int,
    terminal_intent_id: str, action: str, now: int,
) -> None:
    canonical = canonical_evidence_manifest(manifest)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or digest != expected:
        raise ValueError("evidence manifest digest mismatch")
    bindings = {
        "task_id": task_id, "run_id": run_id,
        "terminal_intent_id": terminal_intent_id, "action": action,
    }
    for field, value in bindings.items():
        if manifest[field] != value:
            raise ValueError(f"evidence manifest {field} binding mismatch")
    if now < manifest["evidence_at"] or now - manifest["evidence_at"] > manifest["freshness_seconds"]:
        raise ValueError("evidence manifest is not fresh")


def recovery_decision(
    failure_class: str, *, verified_checkpoint: bool,
    digest_matches: bool, side_effect: str,
) -> str:
    if not isinstance(failure_class, str) or failure_class not in _FAILURE_CLASSES - {"none"}:
        raise ValueError("invalid recovery failure_class")
    if type(verified_checkpoint) is not bool or type(digest_matches) is not bool:
        raise ValueError("checkpoint verification flags must be booleans")
    if not isinstance(side_effect, str) or side_effect not in _SIDE_EFFECTS:
        raise ValueError("invalid recovery side_effect")
    if failure_class in _STABLE_BLOCK:
        return "stable_block"
    if failure_class in _NO_RETRY:
        return "no_retry"
    if failure_class not in _TRANSIENT:
        return "human_gate"
    if not digest_matches or side_effect == "unknown":
        return "human_gate"
    if not verified_checkpoint:
        return "fresh"
    return "resume"


def evaluate_shadow_verification(
    *, provenance_verified: bool, risk: str, verdict_source: str,
    external_side_effect: bool, stale: bool, flaky: bool,
) -> dict[str, Any]:
    """Classify an observation without ever authorizing a verification skip."""
    for name, value in (
        ("provenance_verified", provenance_verified),
        ("external_side_effect", external_side_effect),
        ("stale", stale),
        ("flaky", flaky),
    ):
        if type(value) is not bool:
            raise ValueError(f"{name} must be a boolean")
    if not isinstance(risk, str) or risk not in _RISK_LEVELS:
        raise ValueError("invalid verification risk")
    if not isinstance(verdict_source, str) or verdict_source not in _VERDICT_SOURCES:
        raise ValueError("invalid verdict_source")

    reason = "eligible_shadow_observation"
    eligible = True
    if verdict_source in {"llm", "reviewer"}:
        reason, eligible = f"{verdict_source}_verdict", False
    elif risk != "low":
        reason, eligible = "risk_not_low", False
    elif external_side_effect:
        reason, eligible = "external_side_effect", False
    elif stale:
        reason, eligible = "stale_evidence", False
    elif flaky:
        reason, eligible = "flaky_evidence", False
    elif not provenance_verified:
        reason, eligible = "unverified_provenance", False
    return {
        "mode": "shadow", "verification_skipped": False,
        "eligible": eligible, "reason": reason,
    }
