"""Structured eligibility and replay-safety checks for failed Gateway turns."""

from __future__ import annotations

from typing import Any, Dict, Optional
from collections import Counter
import json

from agent.tool_result_classification import tool_may_have_side_effect


RETRY_WAIT = "retry_wait"
BLOCKED = "blocked"


def _tool_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    return str(function.get("name") or "") if isinstance(function, dict) else ""


def reconcile_result_evidence(agent_result: Dict[str, Any], evidence: Dict[str, Any]) -> list:
    """Prove coverage of every current original-turn call/result before sealing.

    Replay presentation fields are not persistence receipts. Compare the actual call payloads,
    result bodies and dispositions with multiplicity; missing coverage must not arm recovery.
    """
    from gateway.session_transcript import RecoveryEvidenceError
    messages = agent_result.get("messages")
    boundary = agent_result.get("current_turn_user_idx")
    if (not isinstance(messages, list) or type(boundary) is not int
            or not 0 <= boundary < len(messages)
            or messages[boundary].get("role") != "user"):
        raise RecoveryEvidenceError("missing_turn_boundary")
    current = messages[boundary:]

    def signatures(rows):
        result = []
        for row in rows:
            if row.get("role") == "assistant" and row.get("tool_calls"):
                payload = {"role": "assistant", "tool_calls": row["tool_calls"]}
            elif row.get("role") == "tool":
                payload = {key: row.get(key) for key in (
                    "role", "tool_call_id", "content", "effect_disposition",
                )}
            else:
                continue
            result.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return Counter(result)

    if signatures(current) - signatures(evidence["rows"]):
        raise RecoveryEvidenceError("unpersisted_result_evidence")
    return current


def failed_turn_recovery(
    agent_result: Any, *, evidence: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """Return a durable recovery decision only for an explicit retryable failure verdict.

    A failed turn containing a possibly effectful tool call is retained as ``blocked``.  Hermes
    cannot prove whether an external write landed from the provider failure alone, so that turn
    must never be replayed automatically.
    """
    if not isinstance(agent_result, dict):
        return None
    if agent_result.get("failed") is not True or agent_result.get("interrupted") is True:
        return None
    if agent_result.get("failure_retryable") is not True:
        return None
    failure_reason = agent_result.get("failure_reason")
    if not isinstance(failure_reason, str) or not failure_reason.strip():
        return None

    messages = evidence.get("rows") if isinstance(evidence, dict) else agent_result.get("messages")
    if not isinstance(messages, list):
        return {"status": BLOCKED, "failure_reason": failure_reason, "blocked_reason": "missing_replay_history"}
    # Persistence offsets address the old durable prefix, not the normalized resumed
    # transcript. Include ALL attempts of the original user turn in the safety proof.
    boundary = agent_result.get("current_turn_user_idx")
    if evidence is not None:
        if not messages or not isinstance(messages[0], dict) or messages[0].get("role") != "user":
            return {"status": BLOCKED, "failure_reason": failure_reason,
                    "blocked_reason": "missing_turn_boundary"}
        turn_messages = list(messages)
        current = agent_result.get("messages")
        if isinstance(current, list) and type(boundary) is int and 0 <= boundary < len(current):
            turn_messages.extend(current[boundary:])
        elif current:
            return {"status": BLOCKED, "failure_reason": failure_reason,
                    "blocked_reason": "missing_turn_boundary"}
    elif "current_turn_user_idx" in agent_result:
        if (type(boundary) is not int or not 0 <= boundary < len(messages)
                or not isinstance(messages[boundary], dict)
                or messages[boundary].get("role") != "user"):
            return {"status": BLOCKED, "failure_reason": failure_reason,
                    "blocked_reason": "missing_turn_boundary"}
        turn_messages = messages[boundary:]
    else:
        # Older producers have no proven coordinate; never use their persistence
        # offset to hide a possibly effectful call. Inspect their whole history.
        turn_messages = messages
    known_call_ids: set[str] = set()
    result_call_ids: set[str] = set()
    seen_calls = {}
    for message in turn_messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for call in message.get("tool_calls") or ():
                name = _tool_name(call)
                call_id = str(call.get("id") or call.get("call_id") or "") if isinstance(call, dict) else ""
                signature = (name, (call.get("function") or {}).get("arguments")) if name else None
                if call_id:
                    if call_id in seen_calls and seen_calls[call_id] != signature:
                        return {"status": BLOCKED, "failure_reason": failure_reason,
                                "blocked_reason": "conflicting_tool_evidence"}
                    seen_calls[call_id] = signature
                    known_call_ids.add(call_id)
                if not name or tool_may_have_side_effect(name):
                    return {
                        "status": BLOCKED,
                        "failure_reason": failure_reason,
                        "blocked_reason": "external_effect_unknown",
                    }
                if not call_id:
                    return {"status": BLOCKED, "failure_reason": failure_reason,
                            "blocked_reason": "missing_tool_call_id"}
        if message.get("role") == "tool":
            if message.get("effect_disposition") == "unknown":
                return {
                    "status": BLOCKED,
                    "failure_reason": failure_reason,
                    "blocked_reason": "external_effect_unknown",
                }
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in known_call_ids:
                return {
                    "status": BLOCKED,
                    "failure_reason": failure_reason,
                    "blocked_reason": "unmatched_tool_result",
                }
            result_call_ids.add(call_id)
    if known_call_ids - result_call_ids:
        return {"status": BLOCKED, "failure_reason": failure_reason,
                "blocked_reason": "missing_tool_result"}
    return {"status": RETRY_WAIT, "failure_reason": failure_reason}


def retry_delay_seconds(resume_count: int) -> float:
    """Bounded exponential delay before the next same-turn attempt."""
    return float(min(30, 2 ** max(0, int(resume_count))))
