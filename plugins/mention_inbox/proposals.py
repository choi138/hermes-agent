"""Locally-authored, hash-bound work proposals for the mention inbox."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REAPPROVAL = "needs_reapproval"
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkProposal:
    proposal_id: str
    revision: int
    source_dedupe_key: str
    source_revision: str
    subject_key: str
    head_sha: str | None
    goal: str
    steps: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    verification: tuple[str, ...]
    executor_hint: str
    status: ProposalStatus
    content_hash: str

    def __post_init__(self) -> None:
        _validate_fields(
            proposal_id=self.proposal_id,
            revision=self.revision,
            source_dedupe_key=self.source_dedupe_key,
            source_revision=self.source_revision,
            subject_key=self.subject_key,
            head_sha=self.head_sha,
            goal=self.goal,
            steps=self.steps,
            allowed_actions=self.allowed_actions,
            forbidden_actions=self.forbidden_actions,
            verification=self.verification,
            executor_hint=self.executor_hint,
        )
        if not isinstance(self.status, ProposalStatus):
            raise ValueError("status must be a ProposalStatus")
        expected = _hash_payload(_content_payload(self))
        if self.content_hash != expected:
            raise ValueError("proposal content hash does not match canonical content")


_JSON_KEYS = frozenset({
    "proposal_id",
    "revision",
    "source_dedupe_key",
    "source_revision",
    "subject_key",
    "head_sha",
    "goal",
    "steps",
    "allowed_actions",
    "forbidden_actions",
    "verification",
    "executor_hint",
    "status",
    "content_hash",
})


def _text(value: Any, name: str, *, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if len(value) > limit or any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(f"{name} is invalid or too long")
    return value


def _items(value: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of strings")
    items = tuple(_text(item, f"{name} item", limit=500) for item in value)
    if not allow_empty and not items:
        raise ValueError(f"{name} must not be empty")
    if len(items) > 32 or len(set(items)) != len(items):
        raise ValueError(f"{name} must be bounded and contain unique values")
    return items


def _source_timestamp(value: Any) -> str:
    text = _text(value, "source_revision", limit=64)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("source_revision must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("source_revision must include a timezone")
    return text


def _validate_fields(**fields: Any) -> None:
    proposal_id = _text(fields["proposal_id"], "proposal_id", limit=80)
    if not proposal_id.startswith("wp_"):
        raise ValueError("proposal_id must be locally namespaced")
    revision = fields["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("revision must be a positive integer")
    _text(fields["source_dedupe_key"], "source_dedupe_key", limit=500)
    _source_timestamp(fields["source_revision"])
    _text(fields["subject_key"], "subject_key", limit=500)
    head_sha = fields["head_sha"]
    if head_sha is not None:
        _text(head_sha, "head_sha", limit=128)
    _text(fields["goal"], "goal", limit=2000)
    _items(fields["steps"], "steps")
    _items(fields["allowed_actions"], "allowed_actions")
    _items(fields["forbidden_actions"], "forbidden_actions", allow_empty=True)
    _items(fields["verification"], "verification")
    _text(fields["executor_hint"], "executor_hint", limit=80)


def _content_payload(proposal: WorkProposal) -> dict[str, Any]:
    return {
        "proposal_id": proposal.proposal_id,
        "revision": proposal.revision,
        "source_dedupe_key": proposal.source_dedupe_key,
        "source_revision": proposal.source_revision,
        "subject_key": proposal.subject_key,
        "head_sha": proposal.head_sha,
        "goal": proposal.goal,
        "steps": list(proposal.steps),
        "allowed_actions": list(proposal.allowed_actions),
        "forbidden_actions": list(proposal.forbidden_actions),
        "verification": list(proposal.verification),
        "executor_hint": proposal.executor_hint,
    }


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _proposal_id(subject_key: str) -> str:
    digest = hashlib.sha256(f"mention-inbox\0{subject_key}".encode("utf-8")).hexdigest()
    return f"wp_{digest[:24]}"


def _build(
    *,
    proposal_id: str,
    revision: int,
    source_dedupe_key: str,
    source_revision: str,
    subject_key: str,
    head_sha: str | None,
    goal: str,
    steps: Sequence[str],
    allowed_actions: Sequence[str],
    forbidden_actions: Sequence[str],
    verification: Sequence[str],
    executor_hint: str,
) -> WorkProposal:
    normalized = {
        "proposal_id": proposal_id,
        "revision": revision,
        "source_dedupe_key": source_dedupe_key,
        "source_revision": source_revision,
        "subject_key": subject_key,
        "head_sha": head_sha,
        "goal": goal,
        "steps": _items(steps, "steps"),
        "allowed_actions": _items(allowed_actions, "allowed_actions"),
        "forbidden_actions": _items(
            forbidden_actions, "forbidden_actions", allow_empty=True
        ),
        "verification": _items(verification, "verification"),
        "executor_hint": executor_hint,
    }
    _validate_fields(**normalized)
    content_hash = _hash_payload({
        **normalized,
        "steps": list(normalized["steps"]),
        "allowed_actions": list(normalized["allowed_actions"]),
        "forbidden_actions": list(normalized["forbidden_actions"]),
        "verification": list(normalized["verification"]),
    })
    return WorkProposal(
        **normalized,
        status=ProposalStatus.PENDING,
        content_hash=content_hash,
    )


def build_work_proposal(
    *,
    revision: int,
    source_dedupe_key: str,
    source_revision: str,
    subject_key: str,
    head_sha: str | None,
    goal: str,
    steps: Sequence[str],
    allowed_actions: Sequence[str],
    forbidden_actions: Sequence[str],
    verification: Sequence[str],
    executor_hint: str,
) -> WorkProposal:
    subject = _text(subject_key, "subject_key", limit=500)
    return _build(
        proposal_id=_proposal_id(subject),
        revision=revision,
        source_dedupe_key=source_dedupe_key,
        source_revision=source_revision,
        subject_key=subject,
        head_sha=head_sha,
        goal=goal,
        steps=steps,
        allowed_actions=allowed_actions,
        forbidden_actions=forbidden_actions,
        verification=verification,
        executor_hint=executor_hint,
    )


def revise_work_proposal(
    proposal: WorkProposal,
    *,
    source_revision: str,
    head_sha: str | None,
    goal: str,
    steps: Sequence[str],
    allowed_actions: Sequence[str],
    forbidden_actions: Sequence[str],
    verification: Sequence[str],
    executor_hint: str,
) -> WorkProposal:
    if not isinstance(proposal, WorkProposal):
        raise ValueError("proposal must be a WorkProposal")
    return _build(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision + 1,
        source_dedupe_key=proposal.source_dedupe_key,
        source_revision=source_revision,
        subject_key=proposal.subject_key,
        head_sha=head_sha,
        goal=goal,
        steps=steps,
        allowed_actions=allowed_actions,
        forbidden_actions=forbidden_actions,
        verification=verification,
        executor_hint=executor_hint,
    )


def verify_proposal_hash(proposal: WorkProposal) -> bool:
    if not isinstance(proposal, WorkProposal):
        return False
    return proposal.content_hash == _hash_payload(_content_payload(proposal))


def proposal_to_dict(proposal: WorkProposal) -> dict[str, Any]:
    if not verify_proposal_hash(proposal):
        raise ValueError("proposal content hash is invalid")
    return {
        **_content_payload(proposal),
        "status": proposal.status.value,
        "content_hash": proposal.content_hash,
    }


def proposal_to_json(proposal: WorkProposal) -> str:
    return json.dumps(
        proposal_to_dict(proposal),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def restore_proposal(value: str | Mapping[str, Any]) -> WorkProposal:
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("proposal JSON is invalid") from exc
    else:
        payload = dict(value)
    if not isinstance(payload, dict) or set(payload) != _JSON_KEYS:
        raise ValueError("proposal payload has invalid keys")
    try:
        status = ProposalStatus(payload["status"])
    except (TypeError, ValueError) as exc:
        raise ValueError("proposal status is invalid") from exc
    for key in ("steps", "allowed_actions", "forbidden_actions", "verification"):
        if not isinstance(payload[key], list):
            raise ValueError(f"proposal {key} must be a list")
    return WorkProposal(
        proposal_id=payload["proposal_id"],
        revision=payload["revision"],
        source_dedupe_key=payload["source_dedupe_key"],
        source_revision=payload["source_revision"],
        subject_key=payload["subject_key"],
        head_sha=payload["head_sha"],
        goal=payload["goal"],
        steps=tuple(payload["steps"]),
        allowed_actions=tuple(payload["allowed_actions"]),
        forbidden_actions=tuple(payload["forbidden_actions"]),
        verification=tuple(payload["verification"]),
        executor_hint=payload["executor_hint"],
        status=status,
        content_hash=payload["content_hash"],
    )
