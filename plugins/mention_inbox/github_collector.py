"""Normalize GitHub notification payloads into the shared mention contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from plugins.mention_inbox.contract import MentionEvent, ingest_event

_SELECTED_REASONS = frozenset({"assign", "mention", "review_requested", "team_mention"})
_ACTION_BY_REASON = {
    "assign": "investigate",
    "mention": "reply",
    "review_requested": "review",
    "team_mention": "reply",
}
_MAX_TITLE_CHARS = 500
_MAX_BODY_CHARS = 4000
_MAX_URL_CHARS = 500


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _source_revision(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(
            "notification.updated_at must be a timezone-aware ISO timestamp"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "notification.updated_at must be a timezone-aware ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("notification.updated_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GitHubCollectedEvent:
    """A canonical event paired with its source-side revision marker."""

    event: MentionEvent
    source_revision: str


class GitHubNotificationCollector:
    """Pure GitHub payload normalizer with no network or write capabilities."""

    def __init__(
        self,
        *,
        target_id: str,
        allowed_repositories: Iterable[str],
    ) -> None:
        self._target_id = target_id
        self._allowed_repositories = frozenset(allowed_repositories)

    def _is_selected(self, notification: Mapping[str, Any]) -> bool:
        if not isinstance(notification, Mapping):
            return False
        reason = notification.get("reason")
        repository = notification.get("repository")
        if reason not in _SELECTED_REASONS or not isinstance(repository, Mapping):
            return False
        full_name = repository.get("full_name")
        return isinstance(full_name, str) and full_name in self._allowed_repositories

    def _validate_notification(self, notification: Mapping[str, Any]) -> None:
        notification_id = notification.get("id")
        if (
            not isinstance(notification_id, str)
            or not notification_id
            or notification_id != notification_id.strip()
        ):
            raise ValueError("notification.id must be a non-empty trimmed string")
        _source_revision(notification.get("updated_at"))
        repository = notification.get("repository")
        if not isinstance(repository, Mapping):
            raise ValueError("notification.repository must be an object")
        repository_node_id = repository.get("node_id")
        if not isinstance(repository_node_id, str) or not repository_node_id:
            raise ValueError("notification.repository.node_id must be a string")
        subject = notification.get("subject")
        if not isinstance(subject, Mapping):
            raise ValueError("notification.subject must be an object")
        if not isinstance(subject.get("title"), str) or not isinstance(
            subject.get("type"), str
        ):
            raise ValueError("notification.subject title/type must be strings")
        subject_url = subject.get("url")
        if subject_url is not None and not isinstance(subject_url, str):
            raise ValueError("notification.subject.url must be a string or null")
        if not isinstance(notification.get("unread"), bool):
            raise ValueError("notification.unread must be a boolean")

    def accepts(self, notification: Mapping[str, Any]) -> bool:
        if not self._is_selected(notification):
            return False
        try:
            self._validate_notification(notification)
        except ValueError:
            return False
        return True

    def normalize(
        self,
        notification: Mapping[str, Any],
        subject_detail: Mapping[str, Any] | None,
    ) -> GitHubCollectedEvent | None:
        if not self._is_selected(notification):
            return None
        self._validate_notification(notification)

        repository = notification["repository"]
        subject = notification["subject"]
        source_revision = _source_revision(notification["updated_at"])
        if subject_detail is None:
            actor_id = "github:unknown"
            actor_kind = "unknown"
            thread_id = f"github-notification:{notification['id']}"
            body = ""
            source_url = None
        else:
            actor = subject_detail.get("user")
            actor_id = "github:unknown"
            actor_kind = "unknown"
            if isinstance(actor, Mapping):
                candidate_id = actor.get("node_id")
                if isinstance(candidate_id, str) and candidate_id:
                    actor_id = candidate_id
                    actor_kind = {
                        "App": "app",
                        "Bot": "bot",
                        "User": "user",
                    }.get(actor.get("type"), "unknown")
            candidate_thread_id = subject_detail.get("node_id")
            thread_id = (
                candidate_thread_id
                if isinstance(candidate_thread_id, str) and candidate_thread_id
                else f"github-notification:{notification['id']}"
            )
            candidate_body = subject_detail.get("body")
            body = _bounded(candidate_body, _MAX_BODY_CHARS) if isinstance(candidate_body, str) else ""
            candidate_url = subject_detail.get("html_url")
            source_url = (
                _bounded(candidate_url, _MAX_URL_CHARS)
                if isinstance(candidate_url, str)
                else None
            )

        event = ingest_event({
            "schema_version": "1",
            "source": {
                "platform": "github",
                "event_id": notification["id"],
            },
            "actor": {
                "actor_id": actor_id,
                "kind": actor_kind,
            },
            "target": {
                "target_id": self._target_id,
                "kind": "user",
            },
            "thread": {
                "thread_id": thread_id,
                "container_id": repository["node_id"],
            },
            "requested_action": _ACTION_BY_REASON[notification["reason"]],
            "deadline": None,
            "untrusted": {
                "title": _bounded(subject["title"], _MAX_TITLE_CHARS),
                "body": body,
                "action_detail": notification["reason"],
                "source_url": source_url,
                "metadata": {
                    "reason": notification["reason"],
                    "repository": repository["full_name"],
                    "subject_type": subject["type"],
                    "unread": notification["unread"],
                },
            },
        })
        return GitHubCollectedEvent(event=event, source_revision=source_revision)
