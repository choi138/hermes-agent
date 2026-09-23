"""Gateway boundary for code-owned, approved Discord Work Inbox executions.

The Discord adapter owns admission and the Work Inbox owns durable approval
state. This module only validates the internal envelope and binds its receipts
to the ordinary gateway turn without giving a conversational thread authority
over an approved execution.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any


logger = logging.getLogger("gateway.run")
_EXECUTION_ID = re.compile(r"wx_[0-9a-f]{24}\Z")
_PROPOSAL_HASH = re.compile(r"[0-9a-f]{64}\Z")
_OWNER_ID = re.compile(r"[0-9a-f]{32}\Z")


def _has_mention_inbox_execution_marker(event: Any) -> bool:
    metadata = getattr(event, "metadata", None)
    return isinstance(metadata, dict) and "mention_inbox_execution" in metadata


def _mention_inbox_execution_context(
    event: Any,
) -> tuple[str, str, str, str, str] | None:
    """Accept only a complete code-owned internal execution envelope."""
    if getattr(event, "internal", False) is not True:
        return None
    metadata = getattr(event, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    context = metadata.get("mention_inbox_execution")
    if not isinstance(context, dict):
        return None
    execution_id = context.get("execution_id")
    proposal_hash = context.get("proposal_hash")
    mode = context.get("mode")
    recovery_token = context.get("recovery_token")
    owner_id = context.get("owner_id")
    if not isinstance(execution_id, str) or _EXECUTION_ID.fullmatch(execution_id) is None:
        return None
    if not isinstance(proposal_hash, str) or _PROPOSAL_HASH.fullmatch(proposal_hash) is None:
        return None
    if mode not in {"direct", "kanban"}:
        return None
    if not isinstance(recovery_token, str) or not recovery_token or len(recovery_token) > 80:
        return None
    if not isinstance(owner_id, str) or _OWNER_ID.fullmatch(owner_id) is None:
        return None
    return execution_id, proposal_hash, mode, recovery_token, owner_id


def _mention_inbox_execution_id(event: Any) -> str | None:
    context = _mention_inbox_execution_context(event)
    return None if context is None else context[0]


def _mention_inbox_session_source(event: Any, source: Any) -> Any:
    """Use a private transcript lane; keep the original source for delivery."""
    execution_id = _mention_inbox_execution_id(event)
    if execution_id is None:
        if _has_mention_inbox_execution_marker(event):
            raise ValueError("invalid approved mention-inbox execution envelope")
        return source
    lane = str(getattr(source, "thread_id", None) or getattr(source, "chat_id", None) or "root")
    return dataclasses.replace(source, thread_id=f"{lane}:approved:{execution_id}")


def _validated_mention_inbox_execution(event: Any, adapter: Any) -> tuple[str | None, Any]:
    """Bind the envelope to a pending durable approval before agent construction."""
    context = _mention_inbox_execution_context(event)
    if context is None:
        if _has_mention_inbox_execution_marker(event):
            raise ValueError("invalid approved mention-inbox execution envelope")
        return None, None
    observer = getattr(adapter, "_mention_inbox_execution_observer", None)
    if observer is None:
        raise RuntimeError("approved mention-inbox execution observer is unavailable")
    execution_id, proposal_hash, mode, recovery_token, owner_id = context
    observer.validate_execution_context(
        execution_id,
        proposal_hash=proposal_hash,
        mode=mode,
        recovery_token=recovery_token,
        owner_id=owner_id,
    )
    return execution_id, observer


def _constrain_mention_inbox_toolsets(
    *, configured: Any, disabled: Any, approved: Any
) -> list[str]:
    def names(values: Any) -> set[str]:
        return {
            value.strip() for value in (values or ())
            if isinstance(value, str) and value.strip()
        }

    approved_set = names(approved)
    if not approved_set:
        raise RuntimeError("approved execution toolsets unavailable")
    effective = (names(configured) - names(disabled)) & approved_set
    if effective != approved_set:
        raise RuntimeError("approved execution toolsets unavailable")
    return sorted(effective)


def _compose_mention_inbox_execution_callbacks(
    *, execution_id: str, observer: Any, voice_callback: Any = None
) -> tuple[Any, Any]:
    if _EXECUTION_ID.fullmatch(execution_id) is None:
        raise ValueError("invalid mention-inbox execution id")

    def on_start(call_id: str, tool_name: str, args: Any) -> None:
        if callable(voice_callback):
            try:
                voice_callback(call_id, tool_name, args)
            except Exception:
                logger.debug("mention-inbox voice callback failed", exc_info=True)

    def on_complete(call_id: str, tool_name: str, args: Any, result: Any) -> None:
        del call_id
        try:
            observer.tool_completed(execution_id, tool_name, result, args=args)
        except Exception:
            logger.exception("mention-inbox tool-complete receipt failed")

    return on_start, on_complete


def _install_mention_inbox_pretool_guard(agent: Any, execution_id: str, observer: Any) -> None:
    if _EXECUTION_ID.fullmatch(execution_id) is None:
        raise ValueError("invalid mention-inbox execution id")
    base = getattr(
        agent, "_mention_inbox_base_tool_guardrails",
        getattr(agent, "_tool_guardrails", None),
    )
    if base is None or not callable(getattr(base, "before_call", None)):
        raise RuntimeError("agent tool guardrails are unavailable")
    agent._mention_inbox_base_tool_guardrails = base

    class _ReceiptGuard:
        def __getattr__(self, name: str) -> Any:
            return getattr(base, name)

        def before_call(self, tool_name: str, args: Any) -> Any:
            decision = base.before_call(tool_name, args)
            if not getattr(decision, "allows_execution", False):
                return decision
            try:
                authorizer = getattr(observer, "authorize_tool_start", None)
                if callable(authorizer):
                    authorizer(execution_id, tool_name, args)
                else:
                    observer.tool_started(execution_id, tool_name)
            except Exception:
                from agent.tool_guardrails import ToolGuardrailDecision

                return ToolGuardrailDecision(
                    action="block",
                    code="mention_inbox_receipt_failed",
                    message="approved execution receipt could not be committed",
                    tool_name=tool_name,
                )
            return decision

    agent._tool_guardrails = _ReceiptGuard()
