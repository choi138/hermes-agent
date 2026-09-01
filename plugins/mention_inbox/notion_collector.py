"""Normalize bounded Notion rich-text mentions into the shared contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from plugins.mention_inbox.contract import MentionEvent, ingest_event

_COVERAGE_LABEL = "selected accessible pages / polling / best-effort"
_MAX_BODY_CHARS = 4000


def _required_id(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{location} must be a non-empty trimmed string")
    return value


def _source_revision(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a timezone-aware ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{location} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{location} must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _has_target_mention(rich_text: Any, target_user_id: str) -> bool:
    if not isinstance(rich_text, list):
        return False
    for item in rich_text:
        if not isinstance(item, Mapping) or item.get("type") != "mention":
            continue
        mention = item.get("mention")
        if not isinstance(mention, Mapping) or mention.get("type") != "user":
            continue
        user = mention.get("user")
        if isinstance(user, Mapping) and user.get("id") == target_user_id:
            return True
    return False


def _plain_text(rich_text: Any) -> str:
    if not isinstance(rich_text, list):
        return ""
    value = "".join(
        item.get("plain_text", "")
        for item in rich_text
        if isinstance(item, Mapping) and isinstance(item.get("plain_text", ""), str)
    )
    return value if len(value) <= _MAX_BODY_CHARS else value[: _MAX_BODY_CHARS - 1] + "…"


def _actor(created_by: Any) -> tuple[str, str]:
    if not isinstance(created_by, Mapping):
        return "notion:unknown", "unknown"
    actor_id = created_by.get("id")
    if not isinstance(actor_id, str) or not actor_id:
        return "notion:unknown", "unknown"
    kind = "bot" if created_by.get("type") == "bot" else "user"
    return actor_id, kind


@dataclass(frozen=True)
class NotionCollectedEvent:
    event: MentionEvent
    source_revision: str


class NotionMentionCollector:
    """Pure normalizer with no Notion network or mutation capability."""

    def __init__(self, *, target_user_id: str) -> None:
        self._target_user_id = _required_id(target_user_id, "target_user_id")

    def normalize_comment(
        self,
        comment: Mapping[str, Any],
        *,
        root_page_id: str,
    ) -> NotionCollectedEvent | None:
        if not isinstance(comment, Mapping):
            raise ValueError("comment must be an object")
        rich_text = comment.get("rich_text")
        if not _has_target_mention(rich_text, self._target_user_id):
            return None
        if comment.get("object") != "comment":
            raise ValueError("comment.object must be 'comment'")
        comment_id = _required_id(comment.get("id"), "comment.id")
        root_id = _required_id(root_page_id, "root_page_id")
        discussion_id = comment.get("discussion_id")
        thread_id = (
            discussion_id
            if isinstance(discussion_id, str) and discussion_id
            else f"notion-comment:{comment_id}"
        )
        revision_value = comment.get("last_edited_time", comment.get("created_time"))
        revision = _source_revision(revision_value, "comment.last_edited_time")
        actor_id, actor_kind = _actor(comment.get("created_by"))
        event = ingest_event({
            "schema_version": "1",
            "source": {"platform": "notion", "event_id": f"comment:{comment_id}"},
            "actor": {"actor_id": actor_id, "kind": actor_kind},
            "target": {"target_id": self._target_user_id, "kind": "user"},
            "thread": {"thread_id": thread_id, "container_id": root_id},
            "requested_action": "reply",
            "deadline": None,
            "untrusted": {
                "title": "Notion comment mention",
                "body": _plain_text(rich_text),
                "action_detail": "notion_user_mention",
                "source_url": None,
                "metadata": {
                    "coverage": _COVERAGE_LABEL,
                    "object_kind": "comment",
                },
            },
        })
        return NotionCollectedEvent(event=event, source_revision=revision)

    def normalize_block(
        self,
        block: Mapping[str, Any],
        *,
        root_page_id: str,
    ) -> NotionCollectedEvent | None:
        if not isinstance(block, Mapping):
            raise ValueError("block must be an object")
        block_type = block.get("type")
        typed_value = block.get(block_type) if isinstance(block_type, str) else None
        rich_text = typed_value.get("rich_text") if isinstance(typed_value, Mapping) else None
        if not _has_target_mention(rich_text, self._target_user_id):
            return None
        if block.get("object") != "block":
            raise ValueError("block.object must be 'block'")
        block_id = _required_id(block.get("id"), "block.id")
        root_id = _required_id(root_page_id, "root_page_id")
        revision = _source_revision(block.get("last_edited_time"), "block.last_edited_time")
        actor_id, actor_kind = _actor(block.get("created_by"))
        event = ingest_event({
            "schema_version": "1",
            "source": {"platform": "notion", "event_id": f"block:{block_id}"},
            "actor": {"actor_id": actor_id, "kind": actor_kind},
            "target": {"target_id": self._target_user_id, "kind": "user"},
            "thread": {
                "thread_id": f"notion-page:{root_id}",
                "container_id": root_id,
            },
            "requested_action": "acknowledge",
            "deadline": None,
            "untrusted": {
                "title": "Notion block mention",
                "body": _plain_text(rich_text),
                "action_detail": "notion_user_mention",
                "source_url": None,
                "metadata": {
                    "coverage": _COVERAGE_LABEL,
                    "object_kind": "block",
                },
            },
        })
        return NotionCollectedEvent(event=event, source_revision=revision)
