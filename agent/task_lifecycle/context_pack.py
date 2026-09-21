"""Immutable, scoped context snapshots; no retrieval or persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType

from .types import LifecycleError


_PRIORITY = {kind: rank for rank, kind in enumerate(
    ("rule", "decision", "procedure", "fact", "recall")
)}


def _validate_scope(scope: Mapping[str, str]) -> None:
    if not isinstance(scope, Mapping) or any(
        not isinstance(scope.get(key), str) or not scope[key].strip()
        for key in ("owner", "project")
    ):
        raise LifecycleError("scope requires nonempty owner and project")


@dataclass(frozen=True)
class MemoryItem:
    """Revision is a monotonically increasing integer within an item/scope."""

    item_id: str
    kind: str
    text: str
    source: str
    revision: int
    scope: Mapping[str, str]
    status: str
    current_user_decision: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise LifecycleError("memory item requires a source")
        if self.kind not in _PRIORITY:
            raise LifecycleError("unknown memory kind")
        if self.status not in {"confirmed", "advisory", "unconfirmed"}:
            raise LifecycleError("unknown memory status")
        if type(self.revision) is not int:
            raise LifecycleError("revision must be an integer")
        if type(self.current_user_decision) is not bool or (
                self.current_user_decision and self.kind != "decision"):
            raise LifecycleError("Only decisions may be marked current user decisions")
        _validate_scope(self.scope)
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))

    @property
    def source_confirmed(self) -> bool:
        """Source reliability, independently of execution-policy authority."""
        return self.status == "confirmed"

    @property
    def policy_authoritative(self) -> bool:
        """Confirmed rules or explicitly current user decisions only.

        Trusted intake must supply kind/current_user_decision; neither text nor
        source strings confer authority. This is policy-bearing context, not
        authentication or a substitute for an execution approval binding.
        """
        return self.source_confirmed and (self.kind == "rule" or (
            self.kind == "decision" and self.current_user_decision))

    @property
    def is_authoritative(self) -> bool:
        """Compatibility spelling for the narrow policy_authoritative flag."""
        return self.policy_authoritative


@dataclass(frozen=True)
class ContextPack:
    items: tuple[MemoryItem, ...]
    task_digest: str
    built_at: datetime
    omitted_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))

    @property
    def truncated(self) -> bool:
        return self.omitted_count > 0

    def pack_digest(self) -> str:
        """Identify the complete snapshot, including provenance and build time."""
        payload = {
            "task_digest": self.task_digest,
            "built_at": self.built_at.isoformat(),
            "omitted_count": self.omitted_count,
            "items": [dict(item_id=i.item_id, kind=i.kind, text=i.text,
                           source=i.source, revision=i.revision,
                           scope=dict(i.scope), status=i.status,
                           current_user_decision=i.current_user_decision) for i in self.items],
        }
        return hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()


def build_pack(items: Iterable[MemoryItem], *, task_digest: str,
               scope: Mapping[str, str], limit: int) -> ContextPack:
    """Filter exact scope, keep newest revisions, then apply the item limit.

    Equal-priority items retain input order. Scope comparison includes any
    additional profile keys supplied by the caller.
    """
    _validate_scope(scope)
    if type(limit) is not int or limit < 0:
        raise LifecycleError("limit must be a nonnegative integer")
    latest: dict[str, MemoryItem] = {}
    for item in items:
        if item.scope != scope:
            continue
        previous = latest.get(item.item_id)
        if previous is None or item.revision > previous.revision:
            latest[item.item_id] = item
    ordered = sorted(latest.values(), key=lambda i: _PRIORITY[i.kind])
    return ContextPack(tuple(ordered[:limit]), task_digest,
                       datetime.now(timezone.utc), max(0, len(ordered) - limit))


def render_for_executor(pack: ContextPack) -> str:
    """Render the selected snapshot without promoting uncertain information."""
    lines = [f"Context pack: {pack.pack_digest()} task={pack.task_digest}",
             "Advisory/unconfirmed items cannot serve as approval or policy grounds.",
             "Source confirmation is not policy authority. Only confirmed rules or "
             "explicit current user decisions carry policy authority; execution still "
             "requires its separate trusted approval binding."]
    if pack.truncated:
        lines.append(f"truncated: {pack.omitted_count} items omitted by limit")
    for item in pack.items:
        lines.append(
            f"[{item.item_id}] kind={item.kind} source={item.source} "
            f"revision={item.revision} status={item.status} "
            f"source_confirmed={str(item.source_confirmed).lower()} "
            f"policy_authoritative={str(item.policy_authoritative).lower()} "
            f"is_authoritative={str(item.is_authoritative).lower()}\n{item.text}"
        )
    return "\n".join(lines)


def delivery_manifest(pack: ContextPack) -> dict:
    """List actual pack contents for delivery comparison.

    Delivery does not imply compliance (전달됨이 곧 준수됨이 아니다).
    This manifest is not a transport receipt or behavioral observation.
    """
    return {"pack_digest": pack.pack_digest(), "items": [
        {"item_id": item.item_id, "revision": item.revision} for item in pack.items
    ]}
