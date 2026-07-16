"""One-tool, gateway-authenticated asynchronous Kanban intake surface."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from gateway.session_context import get_trusted_gateway_source
from hermes_cli.config import load_config
from tools.registry import registry, tool_error


logger = logging.getLogger(__name__)


KANBAN_TASK_SCHEMA = {
    "name": "kanban_task",
    "description": (
        "Create needs title; status/update/retry need task_id; "
        "update allows title/body/priority."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["create", "status", "update", "retry"],
                "default": "create",
            },
            "task_id": {
                "type": "string",
            },
            "title": {
                "type": "string",
            },
            "body": {
                "type": "string",
            },
            "priority": {
                "type": "integer",
                "description": "Higher dispatches first (default 0).",
            },
            "goal_mode": {
                "type": "boolean",
                "description": "Continue until judged done (default false).",
            },
            "goal_max_turns": {
                "type": "integer",
                "minimum": 1,
            },
            "max_retries": {
                "type": "integer",
                "minimum": 1,
            },
            "max_runtime_seconds": {
                "type": "integer",
                "minimum": 1,
            },
        },
    },
}


def _configured_string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _kanban_enabled_for_discord(config: dict[str, Any]) -> bool:
    if "kanban" in _configured_string_set(config.get("toolsets")):
        return True
    platform_toolsets = config.get("platform_toolsets")
    return isinstance(platform_toolsets, dict) and (
        "kanban" in _configured_string_set(platform_toolsets.get("discord"))
    )


def _positive_int(args: dict[str, Any], name: str) -> tuple[Optional[int], Optional[str]]:
    raw = args.get(name)
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, f"{name} must be a positive integer"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{name} must be a positive integer"
    if value < 1:
        return None, f"{name} must be >= 1"
    return value, None


def _priority(args: dict[str, Any]) -> tuple[int, Optional[str]]:
    raw = args.get("priority", 0)
    if isinstance(raw, bool):
        return 0, "priority must be an integer"
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return 0, "priority must be an integer"


def _goal_mode(args: dict[str, Any]) -> tuple[bool, Optional[str]]:
    raw = args.get("goal_mode", False)
    if isinstance(raw, bool):
        return raw, None
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return False, "goal_mode must be a boolean"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _resolve_assignee(config: dict[str, Any], source: Any) -> str:
    kanban_cfg = config.get("kanban")
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}
    candidate = (
        str(kanban_cfg.get("intake_assignee") or "").strip()
        or str(kanban_cfg.get("default_assignee") or "").strip()
        or str(getattr(source, "profile", "") or "").strip()
    )
    if not candidate:
        raise ValueError("no server-owned Kanban intake assignee is configured")

    from hermes_cli.profiles import (
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )

    candidate = normalize_profile_name(candidate)
    validate_profile_name(candidate)
    if not profile_exists(candidate):
        raise ValueError(f"Kanban intake assignee profile does not exist: {candidate}")
    return candidate


def _trusted_source_context(source: Any) -> dict[str, Any]:
    """Return restart-stable ingress identity for durable deduplication."""

    return {
        "platform": str(source.platform),
        "profile": str(source.profile),
        "scope_id": str(source.scope_id or ""),
        "parent_chat_id": str(source.parent_chat_id or ""),
        "chat_id": str(source.chat_id),
        "thread_id": str(source.thread_id or ""),
        "user_id": str(source.user_id),
        "message_id": str(source.message_id),
    }


def _handle_kanban_task(args: dict[str, Any], **_kwargs: Any) -> str:
    source = get_trusted_gateway_source()
    if source is None or source.platform != "discord":
        return tool_error("kanban_task requires an authenticated Discord gateway turn")
    if source.is_bot:
        return tool_error("kanban_task refuses bot-authored Discord messages")
    if not source.profile or not source.chat_id or not source.user_id or not source.message_id:
        return tool_error("kanban_task trusted Discord source is incomplete")

    operation = str(args.get("operation") or "create").strip().lower()
    if operation not in {"create", "status", "update", "retry"}:
        return tool_error("operation must be create, status, update, or retry")
    task_id = str(args.get("task_id") or "").strip()
    if operation != "create" and not task_id:
        return tool_error(f"task_id is required for {operation}")
    if operation == "create" and task_id:
        return tool_error("create does not accept field(s): task_id")

    unknown = set(args) - set(KANBAN_TASK_SCHEMA["parameters"]["properties"])
    if unknown:
        return tool_error(
            "kanban_task does not accept server-owned field(s): "
            + ", ".join(sorted(str(name) for name in unknown))
        )

    source_context = _trusted_source_context(source)
    idempotency_key = f"gateway-intake:{_json_digest(source_context)}"

    if operation == "status":
        invalid = set(args) - {"operation", "task_id"}
        if invalid:
            return tool_error(
                "status does not accept field(s): "
                + ", ".join(sorted(str(name) for name in invalid))
            )
        try:
            config = load_config()
            if not _kanban_enabled_for_discord(config):
                return tool_error("Kanban intake is not enabled for Discord")

            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_intake

            with kb.connect_closing() as conn:
                payload = kanban_intake.status_task(
                    conn,
                    task_id=task_id,
                    source_context=source_context,
                    idempotency_key=idempotency_key,
                    request_hash=_json_digest(
                        {"operation": "status", "task_id": task_id}
                    ),
                )
            return json.dumps(
                {"ok": True, "operation": "status", **payload},
                ensure_ascii=False,
            )
        except ValueError as exc:
            return tool_error(f"kanban_task: {exc}")
        except Exception as exc:
            logger.exception("kanban_task status failed")
            return tool_error(f"kanban_task: {exc}")

    if operation == "update":
        invalid = set(args) - {"operation", "task_id", "title", "body", "priority"}
        if invalid:
            return tool_error(
                "update does not accept field(s): "
                + ", ".join(sorted(str(name) for name in invalid))
            )
        changes: dict[str, Any] = {}
        if "title" in args:
            title = str(args.get("title") or "").strip()
            if not title:
                return tool_error("title is required")
            changes["title"] = title
        if "body" in args:
            body = str(args.get("body") or "").strip()
            changes["body"] = body or None
        if "priority" in args:
            priority, error = _priority(args)
            if error:
                return tool_error(error)
            changes["priority"] = priority
        if not changes:
            return tool_error("update requires title, body, or priority")

        try:
            config = load_config()
            if not _kanban_enabled_for_discord(config):
                return tool_error("Kanban intake is not enabled for Discord")

            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_intake

            with kb.connect_closing() as conn:
                payload = kanban_intake.update_task(
                    conn,
                    task_id=task_id,
                    source_context=source_context,
                    changes=changes,
                    idempotency_key=idempotency_key,
                    request_hash=_json_digest(
                        {"operation": "update", "task_id": task_id, **changes}
                    ),
                )
            return json.dumps(
                {"ok": True, "operation": "update", **payload},
                ensure_ascii=False,
            )
        except ValueError as exc:
            return tool_error(f"kanban_task: {exc}")
        except Exception as exc:
            logger.exception("kanban_task update failed")
            return tool_error(f"kanban_task: {exc}")

    if operation == "retry":
        invalid = set(args) - {"operation", "task_id"}
        if invalid:
            return tool_error(
                "retry does not accept field(s): "
                + ", ".join(sorted(str(name) for name in invalid))
            )
        try:
            config = load_config()
            if not _kanban_enabled_for_discord(config):
                return tool_error("Kanban intake is not enabled for Discord")

            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_intake

            with kb.connect_closing() as conn:
                payload = kanban_intake.retry_task(
                    conn,
                    task_id=task_id,
                    source_context=source_context,
                    idempotency_key=idempotency_key,
                    request_hash=_json_digest(
                        {"operation": "retry", "task_id": task_id}
                    ),
                )

            dispatch_woken = False
            if source.dispatch_wake is not None:
                try:
                    source.dispatch_wake()
                    dispatch_woken = True
                except Exception:
                    logger.warning("kanban_task retry dispatcher wake failed", exc_info=True)
            return json.dumps(
                {
                    "ok": True,
                    "operation": "retry",
                    **payload,
                    "dispatcher_woken": dispatch_woken,
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            return tool_error(f"kanban_task: {exc}")
        except Exception as exc:
            logger.exception("kanban_task retry failed")
            return tool_error(f"kanban_task: {exc}")

    title = str(args.get("title") or "").strip()
    if not title:
        return tool_error("title is required")
    body_value = args.get("body")
    body = str(body_value).strip() if body_value is not None else None
    if body == "":
        body = None

    priority, error = _priority(args)
    if error:
        return tool_error(error)
    goal_mode, error = _goal_mode(args)
    if error:
        return tool_error(error)
    goal_max_turns, error = _positive_int(args, "goal_max_turns")
    if error:
        return tool_error(error)
    max_retries, error = _positive_int(args, "max_retries")
    if error:
        return tool_error(error)
    max_runtime_seconds, error = _positive_int(args, "max_runtime_seconds")
    if error:
        return tool_error(error)

    request = {
        "title": title,
        "body": body,
        "priority": priority,
        "goal_mode": goal_mode,
        "goal_max_turns": goal_max_turns,
        "max_retries": max_retries,
        "max_runtime_seconds": max_runtime_seconds,
    }
    request_hash = _json_digest(request)
    # One admitted Discord message owns at most one intake receipt. Keep the
    # request digest separate so a response-loss retry deduplicates, while a
    # second call from the same message with changed content fails closed
    # instead of silently creating another card.
    try:
        config = load_config()
        if not _kanban_enabled_for_discord(config):
            return tool_error("Kanban intake is not enabled for Discord")
        assignee = _resolve_assignee(config, source)

        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            task_id, created = kb.create_intake_task(
                conn,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                actor_profile=str(source.profile),
                assignee=assignee,
                source_context=source_context,
                platform="discord",
                chat_id=str(source.chat_id),
                thread_id=str(source.thread_id or "") or None,
                user_id=str(source.user_id),
                notifier_profile=str(source.profile),
                title=title,
                body=body,
                priority=priority,
                max_runtime_seconds=max_runtime_seconds,
                max_retries=max_retries,
                goal_mode=goal_mode,
                goal_max_turns=goal_max_turns,
                session_id=str(source.session_id or "") or None,
            )
            task = kb.get_task(conn, task_id)

        dispatch_woken = False
        if source.dispatch_wake is not None:
            try:
                source.dispatch_wake()
                dispatch_woken = True
            except Exception:
                logger.warning("kanban_task dispatcher wake failed", exc_info=True)

        return json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "status": task.status if task is not None else "ready",
                "assignee": assignee,
                "deduplicated": not created,
                "subscribed": True,
                "dispatcher_woken": dispatch_woken,
            },
            ensure_ascii=False,
        )
    except ValueError as exc:
        return tool_error(f"kanban_task: {exc}")
    except Exception as exc:
        logger.exception("kanban_task failed")
        return tool_error(f"kanban_task: {exc}")


registry.register(
    name="kanban_task",
    toolset="kanban_submit",
    schema=KANBAN_TASK_SCHEMA,
    handler=_handle_kanban_task,
    emoji="📥",
)
