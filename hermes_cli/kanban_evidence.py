"""Fail-closed evidence contracts for durable Kanban terminal transitions.

Only identifiers, enums, timestamps, and cryptographic digests cross this
boundary. Raw prompts, commands, tool output, environment values, headers, and
credentials are deliberately not representable by the manifest schema.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

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

_LOCKFILE_NAMES = (
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "Pipfile.lock",
    "requirements.txt",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)
_MAX_EVIDENCE_FRESHNESS_SECONDS = 604_800
_PRODUCER_POLICY_VERSION = "runtime-evidence-v1"


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("terminal producer input must be JSON serializable") from exc


def _domain_digest(label: str, value: Any) -> str:
    payload = f"{label}\0{_canonical_json(value)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_set_digest(label: str, paths: list[Path]) -> str:
    """Hash file names and contents without persisting either in evidence."""
    digest = hashlib.sha256(f"{label}\0".encode("utf-8"))
    found = False
    for path in paths:
        try:
            if not path.is_file():
                continue
            found = True
            digest.update(path.name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(128 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        except OSError:
            digest.update(f"unreadable:{path.name}".encode("utf-8"))
            digest.update(b"\0")
    if not found:
        digest.update(b"none")
    return digest.hexdigest()


def _git_provenance(workspace: Optional[str], fallback_seed: str) -> tuple[str, str, str]:
    """Return commit, committed tree, and a digest of the working-tree state.

    Git metadata is observational only: failure or a non-repository workspace
    falls back to deterministic object-shaped identifiers. Raw status output is
    hashed in memory and never crosses the evidence boundary.
    """
    root = Path(workspace).expanduser() if workspace else None
    if root is not None and root.is_dir():
        try:
            resolved = subprocess.run(
                [
                    "git", "-C", str(root), "rev-parse",
                    "HEAD^{commit}", "HEAD^{tree}",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            resolved = None
        if resolved is not None:
            lines = resolved.stdout.decode("ascii", errors="ignore").splitlines()
        else:
            lines = []
        if (
            resolved is not None
            and resolved.returncode == 0
            and len(lines) >= 2
            and _GIT_OBJECT_RE.fullmatch(lines[0].strip())
            and _GIT_OBJECT_RE.fullmatch(lines[1].strip())
        ):
            commit, tree = lines[0].strip(), lines[1].strip()
            try:
                status = subprocess.run(
                    [
                        "git", "-C", str(root), "status", "--porcelain=v1",
                        "-z", "--untracked-files=normal",
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
                state_digest = hashlib.sha256(status.stdout).hexdigest()
            except (OSError, subprocess.SubprocessError):
                state_digest = _domain_digest(
                    "worktree-state-unavailable", fallback_seed,
                )
            return commit, tree, state_digest

    commit = hashlib.sha1(f"fallback-commit\0{fallback_seed}".encode()).hexdigest()
    tree = hashlib.sha1(f"fallback-tree\0{fallback_seed}".encode()).hexdigest()
    state = _domain_digest("fallback-worktree", fallback_seed)
    return commit, tree, state


def terminal_intent_id(
    *,
    claim_lock: str,
    task_id: str,
    run_id: int,
    action: str,
    decision: str,
    failure_class: str,
    block_kind: Optional[str],
    handoff: Mapping[str, Any],
) -> str:
    """Derive a response-loss-safe terminal id from the claim and handoff."""
    if not isinstance(claim_lock, str) or not claim_lock:
        raise ValueError("terminal producer requires a claim capability")
    payload = _canonical_json(
        {
            "action": action,
            "block_kind": block_kind,
            "decision": decision,
            "failure_class": failure_class,
            "handoff": dict(handoff),
            "run_id": int(run_id),
            "task_id": task_id,
        }
    ).encode("utf-8")
    digest = hmac.new(claim_lock.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"ti_{digest}"


def produce_terminal_evidence(
    *,
    claim_lock: str,
    task_id: str,
    run_id: int,
    action: str,
    decision: str,
    failure_class: str,
    block_kind: Optional[str],
    handoff: Mapping[str, Any],
    workspace: Optional[str] = None,
    config_path: Optional[str] = None,
    backend_kind: Optional[str] = None,
    evidence_at: Optional[int] = None,
) -> dict[str, Any]:
    """Produce the strict manifest internally for a dispatcher-owned worker.

    The model supplies only the human handoff fields already present on
    ``kanban_complete`` / ``kanban_block``. The producer derives the immutable
    intent id, observes safe local provenance, and hashes every other input so
    prompts, commands, environment values, and credentials are never stored in
    the evidence lane.
    """
    intent_id = terminal_intent_id(
        claim_lock=claim_lock,
        task_id=task_id,
        run_id=run_id,
        action=action,
        decision=decision,
        failure_class=failure_class,
        block_kind=block_kind,
        handoff=handoff,
    )
    canonical_handoff = _canonical_json(dict(handoff))
    fallback_seed = _canonical_json(
        {"intent": intent_id, "run_id": int(run_id), "task_id": task_id}
    )
    source_commit, source_tree, worktree_digest = _git_provenance(
        workspace, fallback_seed,
    )

    root = Path(workspace).expanduser() if workspace else None
    lock_paths = [root / name for name in _LOCKFILE_NAMES] if root else []
    config_paths = [Path(config_path).expanduser()] if config_path else []
    normalized_backend = re.sub(
        r"[^A-Za-z0-9_.:-]+", "-", (backend_kind or "local").strip(),
    ).strip("-.")[:128]
    if not normalized_backend or not _ID_RE.fullmatch(normalized_backend):
        normalized_backend = "local"

    metadata = handoff.get("metadata") if isinstance(handoff, Mapping) else None
    test_plan = {}
    if isinstance(metadata, Mapping):
        for key in ("checks", "tests", "tests_run", "verification"):
            if key in metadata:
                test_plan[key] = metadata[key]

    observed_at = int(evidence_at if evidence_at is not None else time.time())
    manifest = {
        "schema_version": 1,
        "task_id": task_id,
        "run_id": int(run_id),
        "terminal_intent_id": intent_id,
        "action": action,
        "block_kind": block_kind,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "config_digest": _file_set_digest("config", config_paths),
        "lockfile_digest": _file_set_digest("lockfiles", lock_paths),
        "toolchain_digest": _domain_digest(
            "toolchain",
            {
                "implementation": platform.python_implementation(),
                "machine": platform.machine(),
                "python": list(sys.version_info[:3]),
                "system": platform.system(),
            },
        ),
        "backend_kind": normalized_backend,
        "backend_digest": _domain_digest(
            "backend",
            {
                "kind": normalized_backend,
                "profile": os.environ.get("HERMES_PROFILE") or "worker",
            },
        ),
        "command_digest": _domain_digest(
            "terminal-call", {"action": action, "handoff": canonical_handoff},
        ),
        "test_plan_digest": _domain_digest("test-plan", test_plan),
        "fixture_digest": _domain_digest("fixtures", {"intent": intent_id}),
        "seed_digest": _domain_digest("producer-seed", {"intent": intent_id}),
        "policy_version": _PRODUCER_POLICY_VERSION,
        "evidence_at": observed_at,
        "freshness_seconds": _MAX_EVIDENCE_FRESHNESS_SECONDS,
        "failure_class": failure_class,
        "checkpoint_digest": _domain_digest(
            "checkpoint",
            {
                "handoff": canonical_handoff,
                "source_commit": source_commit,
                "source_tree": source_tree,
                "worktree_digest": worktree_digest,
            },
        ),
        "side_effect": "unknown",
    }
    provenance_digest = evidence_manifest_digest(manifest)
    return {
        "terminal_intent_id": intent_id,
        "decision": decision,
        "failure_class": failure_class,
        "manifest": manifest,
        "provenance_digest": provenance_digest,
    }


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
