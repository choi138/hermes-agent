"""P2-M2: deterministic policy router for direct agent orchestration.

M1 produced a description of a request. This module is the single place that
turns that description into an execution decision -- the lane, host, working
directory, permissions, timeout, and whether the user must approve first.

Two properties matter more than convenience here:

*Hints are re-derived, never trusted.* A classifier can suggest `lane_hint:
codex`, but the lane is decided from the intent and the resolved workdir. If a
hint cannot be verified it is discarded, and `policy_trace` records why.

*Refusal is a value, not an exception.* `lane="refuse"` travels through the same
return path as any other decision, so a caller cannot forget to handle it the
way it might forget an `except` clause. A refusal carries no workdir, the
narrowest permissions, and `approval="required"`.

Routing is a pure function of its inputs: no clock, no filesystem, no network,
no model call. Same classification in, same decision out -- which is what makes
the M5 evidence check possible.

Out of scope for M2 (lands in M3/M4): starting Codex or Claude, gateway wiring,
reading the allowlist from live config, and the approval prompt itself.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict

from agent.direct_agent_classification import RequestClassification

__all__ = [
    "ALLOWED_WORKDIRS",
    "APPROVAL_CATEGORIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExecutionDecision",
    "MAX_TIMEOUT_SECONDS",
    "PolicyConfigError",
    "PolicySettings",
    "READ_ONLY_TIMEOUT_SECONDS",
    "route_classification",
]

Lane = Literal["codex", "claude", "hermes", "refuse"]
Host = Literal["mac", "remote"]
Permissions = Literal["read_only", "write_workdir", "write_workdir_network"]
Approval = Literal["not_required", "required"]

# Held as code constants for M2 so this milestone stays self-contained and does
# not touch live config. Config wiring is M3's job.
ALLOWED_WORKDIRS: Tuple[str, ...] = (
    "/Users/choegeun-won/Documents/hermes-agent",
    "/Users/choegeun-won/Documents/content",
    "/Users/choegeun-won/Documents/sub-project",
)

# Paths that identify the Mac. Anything else resolves to the remote host.
_MAC_PATH_PREFIXES: Tuple[str, ...] = ("/Users/",)

# Sensitive work needs a human regardless of how mild the classifier judged it.
APPROVAL_CATEGORIES: frozenset = frozenset(
    {
        "filesystem_delete",
        "external_send",
        "deployment",
        "data_migration",
        "credential_access",
        "shared_state",
    }
)

DEFAULT_TIMEOUT_SECONDS = 900
READ_ONLY_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600

# Which lane handles which intent. `other` is absent on purpose: an
# unclassifiable request goes to Hermes rather than to an autonomous agent.
_INTENT_LANES: Dict[str, Lane] = {
    "code": "codex",
    "docs": "claude",
    "research": "hermes",
    "ops": "hermes",
    "question": "hermes",
    "other": "hermes",
}

# Lanes that run an external agent against a checkout, and so require a
# verified workdir.
_WORKDIR_REQUIRED_LANES: frozenset = frozenset({"codex", "claude"})


class PolicyConfigError(ValueError):
    """Raised when policy settings themselves are unusable."""


@dataclass(frozen=True)
class PolicySettings:
    """Policy inputs. Invalid settings raise rather than silently degrade.

    A plain frozen dataclass rather than a Pydantic model, so a bad setting
    surfaces as PolicyConfigError directly. Pydantic would wrap it in a
    ValidationError, and a caller trying to catch a misconfiguration should not
    have to unwrap a validation report to find out the allowlist was empty.
    """

    allowed_workdirs: Tuple[str, ...] = ALLOWED_WORKDIRS
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    read_only_timeout_seconds: int = READ_ONLY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_workdirs, tuple):
            raise PolicyConfigError("allowed_workdirs must be a tuple")
        for entry in self.allowed_workdirs:
            if not isinstance(entry, str) or not entry.strip():
                raise PolicyConfigError("allowlist entries must be non-blank strings")
            if not entry.startswith("/"):
                raise PolicyConfigError(f"allowlist entries must be absolute: {entry!r}")
            if entry != posixpath.normpath(entry):
                raise PolicyConfigError(f"allowlist entries must be normalized: {entry!r}")

        for name in ("default_timeout_seconds", "read_only_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise PolicyConfigError(f"{name} must be an int")
            if value <= 0:
                raise PolicyConfigError(f"{name} must be positive")
            if value > MAX_TIMEOUT_SECONDS:
                raise PolicyConfigError(
                    f"{name} must not exceed {MAX_TIMEOUT_SECONDS} seconds"
                )


class ExecutionDecision(BaseModel):
    """The resolved execution decision.

    Deliberately carries no command, argv, credential, token, or environment: it
    says *where and under what limits* work may happen, never *what to run*.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    lane: Lane
    host: Host
    workdir: Optional[str]
    permissions: Permissions
    timeout_seconds: int
    approval: Approval
    refusal_reason: Optional[str]
    policy_trace: Tuple[str, ...]


def _resolve_workdir(
    hint: Optional[str], allowed: Sequence[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Return (resolved_workdir, refusal_reason).

    Containment is checked after normalization, and only at a path boundary, so
    `/repo-evil` cannot ride in on the `/repo` prefix.
    """
    if hint is None:
        return None, None
    candidate = hint.strip()
    if not candidate:
        return None, "workdir hint was blank"
    if not candidate.startswith("/"):
        return None, f"workdir must be absolute: {candidate!r}"

    normalized = posixpath.normpath(candidate)
    for root in allowed:
        if normalized == root or normalized.startswith(root.rstrip("/") + "/"):
            return normalized, None
    return None, f"workdir is outside the allowlist: {normalized!r}"


def _derive_host(workdir: Optional[str]) -> Host:
    """Host follows the verified workdir, not the hint.

    A workdir is evidence about where the work lives; a hint is not.
    """
    if workdir is not None and workdir.startswith(_MAC_PATH_PREFIXES):
        return "mac"
    if workdir is not None:
        return "remote"
    return "mac"


def _derive_permissions(categories: Sequence[str]) -> Permissions:
    """Least privilege from what was actually classified.

    Delete is intentionally not a wider grant than write: removal happens inside
    the workdir, and the extra protection is the approval gate.
    """
    write = {"filesystem_write", "filesystem_delete", "data_migration"}
    if not any(category in write for category in categories):
        return "read_only"
    if "network_egress" in categories:
        return "write_workdir_network"
    return "write_workdir"


def _derive_approval(level: str, categories: Sequence[str]) -> Approval:
    if level == "high":
        return "required"
    if any(category in APPROVAL_CATEGORIES for category in categories):
        return "required"
    return "not_required"


def _refuse(reason: str, trace: List[str]) -> ExecutionDecision:
    trace.append(f"lane=refuse: {reason}")
    trace.append("host=mac (default; nothing will run)")
    trace.append("workdir=None (refused)")
    trace.append("permissions=read_only (refused)")
    trace.append("timeout=0 not applicable; refused before execution")
    trace.append("approval=required (a refusal never proceeds unattended)")
    return ExecutionDecision(
        lane="refuse",
        host="mac",
        workdir=None,
        permissions="read_only",
        timeout_seconds=1,
        approval="required",
        refusal_reason=reason,
        policy_trace=tuple(trace),
    )


def route_classification(
    classification: RequestClassification,
    settings: Optional[PolicySettings] = None,
) -> ExecutionDecision:
    """Turn a validated classification into an execution decision.

    Raises TypeError if given anything other than a parsed
    RequestClassification: accepting a bare mapping here would let a caller
    bypass the M1 contract entirely.
    """
    if not isinstance(classification, RequestClassification):
        raise TypeError(
            "route_classification requires a parsed RequestClassification, got "
            f"{type(classification).__name__}"
        )
    active = settings if settings is not None else PolicySettings()

    trace: List[str] = []

    hinted_lane = classification.execution_target.lane_hint
    lane = _INTENT_LANES.get(classification.intent.kind, "hermes")
    if hinted_lane != lane:
        trace.append(
            f"lane={lane} derived from intent={classification.intent.kind}; "
            f"discarded lane_hint={hinted_lane}"
        )
    else:
        trace.append(f"lane={lane} derived from intent={classification.intent.kind}")

    workdir, refusal = _resolve_workdir(
        classification.execution_target.workdir_hint, active.allowed_workdirs
    )
    if refusal is not None:
        return _refuse(refusal, trace)
    if lane in _WORKDIR_REQUIRED_LANES and workdir is None:
        return _refuse(f"lane {lane} requires an allowlisted workdir", trace)
    trace.append(
        f"workdir={workdir!r} verified against {len(active.allowed_workdirs)} allowlisted root(s)"
    )

    host = _derive_host(workdir)
    hinted_host = classification.execution_target.host_hint
    if hinted_host != host:
        trace.append(f"host={host} derived from workdir; discarded host_hint={hinted_host}")
    else:
        trace.append(f"host={host} derived from workdir")

    categories = list(classification.risk.categories)
    permissions = _derive_permissions(categories)
    trace.append(f"permissions={permissions} derived from risk categories={categories}")

    approval = _derive_approval(classification.risk.level, categories)
    trace.append(
        f"approval={approval} from risk level={classification.risk.level} and categories"
    )

    if permissions == "read_only":
        timeout = active.read_only_timeout_seconds
        trace.append(f"timeout={timeout}s (read-only ceiling)")
    else:
        timeout = active.default_timeout_seconds
        trace.append(f"timeout={timeout}s (write default)")

    return ExecutionDecision(
        lane=lane,
        host=host,
        workdir=workdir,
        permissions=permissions,
        timeout_seconds=timeout,
        approval=approval,
        refusal_reason=None,
        policy_trace=tuple(trace),
    )
