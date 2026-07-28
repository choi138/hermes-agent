"""Pure domain contract for normalized mention events."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeAlias, TypeVar

SCHEMA_VERSION = "1"

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
EnumT = TypeVar("EnumT", bound=Enum)

_INGRESS_KEYS = frozenset({
    "schema_version",
    "source",
    "actor",
    "target",
    "thread",
    "requested_action",
    "deadline",
    "untrusted",
})
_SOURCE_KEYS = frozenset({"platform", "event_id"})
_ACTOR_KEYS = frozenset({"actor_id", "kind"})
_TARGET_KEYS = frozenset({"target_id", "kind"})
_THREAD_KEYS = frozenset({"thread_id", "container_id"})
_UNTRUSTED_KEYS = frozenset({
    "title",
    "body",
    "action_detail",
    "source_url",
    "metadata",
})
_CANONICAL_KEYS = _INGRESS_KEYS | frozenset({"dedupe_key", "approval_state"})


def _require_exact_keys(
    value: Any,
    expected: frozenset[str],
    location: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} keys must be strings")

    actual = set(value)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected or missing:
        details = []
        if unexpected:
            details.append(f"unexpected keys: {unexpected}")
        if missing:
            details.append(f"missing keys: {missing}")
        raise ValueError(f"{location} has {'; '.join(details)}")
    return value


def _require_string(value: Any, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{location} must be valid UTF-8 text") from exc
    return value


def _require_optional_string(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, location)


def _require_stable_id(value: Any, location: str) -> str:
    text = _require_string(value, location)
    if not text or text != text.strip():
        raise ValueError(f"{location} must be non-empty without surrounding whitespace")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ValueError(f"{location} must not contain control characters")
    return text


def _require_optional_stable_id(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _require_stable_id(value, location)


def _parse_enum(enum_type: type[EnumT], value: Any, location: str) -> EnumT:
    text = _require_string(value, location)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise ValueError(f"{location} has unsupported value {text!r}") from exc


def _clone_json_value(value: Any, location: str) -> JsonValue:
    if isinstance(value, str):
        return _require_string(value, location)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{location} must not contain NaN or Infinity")
        return value
    if isinstance(value, list):
        return [
            _clone_json_value(item, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        cloned: dict[str, JsonValue] = {}
        for key, item in value.items():
            key = _require_string(key, f"{location} object key")
            cloned[key] = _clone_json_value(item, f"{location}.{key}")
        return cloned
    raise ValueError(f"{location} must contain only JSON-compatible values")


class MentionSource(str, Enum):
    GITHUB = "github"
    SLACK = "slack"
    NOTION = "notion"


class ActorKind(str, Enum):
    USER = "user"
    BOT = "bot"
    APP = "app"
    UNKNOWN = "unknown"


class TargetKind(str, Enum):
    USER = "user"
    TEAM = "team"


class RequestedAction(str, Enum):
    REPLY = "reply"
    REVIEW = "review"
    ACKNOWLEDGE = "acknowledge"
    INVESTIGATE = "investigate"
    UNKNOWN = "unknown"


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SourceRef:
    platform: MentionSource
    event_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.platform, MentionSource):
            raise ValueError("source.platform must be a MentionSource")
        _require_stable_id(self.event_id, "source.event_id")


@dataclass(frozen=True)
class ActorRef:
    actor_id: str
    kind: ActorKind

    def __post_init__(self) -> None:
        _require_stable_id(self.actor_id, "actor.actor_id")
        if not isinstance(self.kind, ActorKind):
            raise ValueError("actor.kind must be an ActorKind")


@dataclass(frozen=True)
class TargetRef:
    target_id: str
    kind: TargetKind

    def __post_init__(self) -> None:
        _require_stable_id(self.target_id, "target.target_id")
        if not isinstance(self.kind, TargetKind):
            raise ValueError("target.kind must be a TargetKind")


@dataclass(frozen=True)
class ThreadRef:
    thread_id: str
    container_id: str | None = None

    def __post_init__(self) -> None:
        _require_stable_id(self.thread_id, "thread.thread_id")
        _require_optional_stable_id(self.container_id, "thread.container_id")


@dataclass(frozen=True)
class UntrustedPayload:
    title: str | None
    body: str
    action_detail: str | None
    source_url: str | None
    metadata: dict[str, JsonValue]

    def __post_init__(self) -> None:
        _require_optional_string(self.title, "untrusted.title")
        _require_string(self.body, "untrusted.body")
        _require_optional_string(self.action_detail, "untrusted.action_detail")
        _require_optional_string(self.source_url, "untrusted.source_url")
        cloned = _clone_json_value(self.metadata, "untrusted.metadata")
        if not isinstance(cloned, dict):
            raise ValueError("untrusted.metadata must be an object")
        object.__setattr__(self, "metadata", cloned)


@dataclass(frozen=True)
class MentionEvent:
    schema_version: str
    source: SourceRef
    actor: ActorRef
    target: TargetRef
    thread: ThreadRef
    requested_action: RequestedAction
    deadline: datetime | None
    dedupe_key: str
    approval_state: ApprovalState
    untrusted: UntrustedPayload

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        if not isinstance(self.source, SourceRef):
            raise ValueError("source must be a SourceRef")
        if not isinstance(self.actor, ActorRef):
            raise ValueError("actor must be an ActorRef")
        if not isinstance(self.target, TargetRef):
            raise ValueError("target must be a TargetRef")
        if not isinstance(self.thread, ThreadRef):
            raise ValueError("thread must be a ThreadRef")
        if not isinstance(self.requested_action, RequestedAction):
            raise ValueError("requested_action must be a RequestedAction")
        if self.deadline is not None and (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ValueError("deadline must include a timezone offset")
        expected_key = build_dedupe_key(self.source, self.target)
        if self.dedupe_key != expected_key:
            raise ValueError("dedupe_key does not match source and target identity")
        if not isinstance(self.approval_state, ApprovalState):
            raise ValueError("approval_state must be an ApprovalState")
        if not isinstance(self.untrusted, UntrustedPayload):
            raise ValueError("untrusted must be an UntrustedPayload")


def build_dedupe_key(source: SourceRef, target: TargetRef) -> str:
    identity = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "source_platform": source.platform.value,
            "source_event_id": source.event_id,
            "target_kind": target.kind.value,
            "target_id": target.target_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"umi:v1:{hashlib.sha256(identity).hexdigest()}"


def _parse_deadline(value: Any) -> datetime | None:
    if value is None:
        return None
    text = _require_string(value, "deadline")
    normalized_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized_text)
    except ValueError as exc:
        raise ValueError("deadline must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("deadline must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _serialize_deadline(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline must include a timezone offset")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_source(value: Any) -> SourceRef:
    payload = _require_exact_keys(value, _SOURCE_KEYS, "source")
    return SourceRef(
        platform=_parse_enum(MentionSource, payload["platform"], "source.platform"),
        event_id=_require_stable_id(payload["event_id"], "source.event_id"),
    )


def _parse_actor(value: Any) -> ActorRef:
    payload = _require_exact_keys(value, _ACTOR_KEYS, "actor")
    return ActorRef(
        actor_id=_require_stable_id(payload["actor_id"], "actor.actor_id"),
        kind=_parse_enum(ActorKind, payload["kind"], "actor.kind"),
    )


def _parse_target(value: Any) -> TargetRef:
    payload = _require_exact_keys(value, _TARGET_KEYS, "target")
    return TargetRef(
        target_id=_require_stable_id(payload["target_id"], "target.target_id"),
        kind=_parse_enum(TargetKind, payload["kind"], "target.kind"),
    )


def _parse_thread(value: Any) -> ThreadRef:
    payload = _require_exact_keys(value, _THREAD_KEYS, "thread")
    return ThreadRef(
        thread_id=_require_stable_id(payload["thread_id"], "thread.thread_id"),
        container_id=_require_optional_stable_id(
            payload["container_id"], "thread.container_id"
        ),
    )


def _parse_untrusted(value: Any) -> UntrustedPayload:
    payload = _require_exact_keys(value, _UNTRUSTED_KEYS, "untrusted")
    metadata = _clone_json_value(payload["metadata"], "untrusted.metadata")
    if not isinstance(metadata, dict):
        raise ValueError("untrusted.metadata must be an object")
    return UntrustedPayload(
        title=_require_optional_string(payload["title"], "untrusted.title"),
        body=_require_string(payload["body"], "untrusted.body"),
        action_detail=_require_optional_string(
            payload["action_detail"], "untrusted.action_detail"
        ),
        source_url=_require_optional_string(
            payload["source_url"], "untrusted.source_url"
        ),
        metadata=metadata,
    )


def ingest_event(payload: dict[str, Any]) -> MentionEvent:
    canonical = _require_exact_keys(payload, _INGRESS_KEYS, "top-level payload")
    version = _require_string(canonical["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r}")

    source = _parse_source(canonical["source"])
    target = _parse_target(canonical["target"])
    return MentionEvent(
        schema_version=version,
        source=source,
        actor=_parse_actor(canonical["actor"]),
        target=target,
        thread=_parse_thread(canonical["thread"]),
        requested_action=_parse_enum(
            RequestedAction,
            canonical["requested_action"],
            "requested_action",
        ),
        deadline=_parse_deadline(canonical["deadline"]),
        dedupe_key=build_dedupe_key(source, target),
        approval_state=ApprovalState.PENDING,
        untrusted=_parse_untrusted(canonical["untrusted"]),
    )


def event_to_dict(event: MentionEvent) -> dict[str, JsonValue]:
    if not isinstance(event, MentionEvent):
        raise ValueError("event must be a MentionEvent")
    metadata = _clone_json_value(event.untrusted.metadata, "untrusted.metadata")
    if not isinstance(metadata, dict):
        raise ValueError("untrusted.metadata must be an object")
    return {
        "schema_version": event.schema_version,
        "source": {
            "platform": event.source.platform.value,
            "event_id": event.source.event_id,
        },
        "actor": {
            "actor_id": event.actor.actor_id,
            "kind": event.actor.kind.value,
        },
        "target": {
            "target_id": event.target.target_id,
            "kind": event.target.kind.value,
        },
        "thread": {
            "thread_id": event.thread.thread_id,
            "container_id": event.thread.container_id,
        },
        "requested_action": event.requested_action.value,
        "deadline": _serialize_deadline(event.deadline),
        "dedupe_key": event.dedupe_key,
        "approval_state": event.approval_state.value,
        "untrusted": {
            "title": event.untrusted.title,
            "body": event.untrusted.body,
            "action_detail": event.untrusted.action_detail,
            "source_url": event.untrusted.source_url,
            "metadata": metadata,
        },
    }


def event_to_json(event: MentionEvent) -> str:
    return json.dumps(
        event_to_dict(event),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def restore_event(payload: dict[str, Any]) -> MentionEvent:
    canonical = _require_exact_keys(payload, _CANONICAL_KEYS, "canonical event")
    version = _require_string(canonical["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r}")

    source = _parse_source(canonical["source"])
    target = _parse_target(canonical["target"])
    stored_key = _require_string(canonical["dedupe_key"], "dedupe_key")
    expected_key = build_dedupe_key(source, target)
    if stored_key != expected_key:
        raise ValueError("dedupe_key does not match source and target identity")

    return MentionEvent(
        schema_version=version,
        source=source,
        actor=_parse_actor(canonical["actor"]),
        target=target,
        thread=_parse_thread(canonical["thread"]),
        requested_action=_parse_enum(
            RequestedAction,
            canonical["requested_action"],
            "requested_action",
        ),
        deadline=_parse_deadline(canonical["deadline"]),
        dedupe_key=stored_key,
        approval_state=_parse_enum(
            ApprovalState,
            canonical["approval_state"],
            "approval_state",
        ),
        untrusted=_parse_untrusted(canonical["untrusted"]),
    )


def transition_approval(
    event: MentionEvent,
    requested_state: ApprovalState,
) -> MentionEvent:
    if not isinstance(event, MentionEvent):
        raise ValueError("event must be a MentionEvent")
    if not isinstance(requested_state, ApprovalState):
        raise ValueError("requested_state must be an ApprovalState")
    if event.approval_state is not ApprovalState.PENDING or requested_state not in {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
    }:
        raise ValueError(
            "approval transition must be pending -> approved or pending -> rejected"
        )
    return replace(event, approval_state=requested_state)
