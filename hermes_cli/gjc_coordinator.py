"""Coordinator MCP adapter for narrow GJC escalation lanes.

GJC is not a model provider and not a fallback worker.  This module is the
small execution boundary used after smart routing has selected a GJC lane and
Hermes' durable kanban state says the turn is approved, unanswered questions
are clear, and evidence/audit records can be written.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Mapping


GJC_LANES = {"gjc_ralplan", "gjc_team", "gjc_visible_session"}
GJC_APPROVAL_TYPE = "gjc_escalation"


@dataclass(frozen=True)
class CoordinatorConfig:
    enabled: bool
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    tool_name: str
    connect_timeout: float
    timeout: float
    session_command: str

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.command and self.tool_name)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _routing_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    raw = config.get("smart_model_routing")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _coordinator_block(config: Mapping[str, Any] | None) -> dict[str, Any]:
    smr = _routing_config(config)
    raw = smr.get("gjc_coordinator") or smr.get("coordinator_mcp") or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def coordinator_config(config: Mapping[str, Any] | None) -> CoordinatorConfig:
    raw = _coordinator_block(config)
    default_command = str(raw.get("command") or "gjc").strip()
    args = raw.get("args")
    if isinstance(args, str):
        args_tuple = tuple(part for part in args.split(" ") if part)
    elif isinstance(args, (list, tuple)):
        args_tuple = tuple(str(part) for part in args)
    else:
        args_tuple = ("mcp-serve", "coordinator")

    env: dict[str, str] = {}
    raw_env = raw.get("env")
    if isinstance(raw_env, Mapping):
        env = {str(k): str(v) for k, v in raw_env.items() if v is not None}
    session_command = str(
        raw.get("session_command")
        or os.environ.get("GJC_COORDINATOR_MCP_SESSION_COMMAND")
        or ""
    ).strip()
    if session_command:
        env.setdefault("GJC_COORDINATOR_MCP_SESSION_COMMAND", session_command)

    try:
        timeout = float(raw.get("timeout", 300))
    except (TypeError, ValueError):
        timeout = 300.0
    try:
        connect_timeout = float(raw.get("connect_timeout", 60))
    except (TypeError, ValueError):
        connect_timeout = 60.0

    return CoordinatorConfig(
        enabled=_truthy(raw.get("enabled", False)),
        command=default_command,
        args=args_tuple,
        env=env,
        tool_name=str(raw.get("tool") or raw.get("tool_name") or "gjc_delegate_plan").strip(),
        connect_timeout=max(1.0, connect_timeout),
        timeout=max(1.0, timeout),
        session_command=session_command,
    )


def _current_run_id() -> int | None:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _workflow_for_lane(selected_lane: str, metadata: Mapping[str, Any]) -> str:
    explicit = str(metadata.get("gjc_workflow") or "").strip()
    if explicit:
        return explicit
    if selected_lane == "gjc_team":
        return "team"
    if selected_lane == "gjc_visible_session":
        return "visible_session"
    return "ralplan"


def _blocker(
    blocker: str,
    message: str,
    *,
    required_gates: tuple[str, ...] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "blocked": True,
        "blockers": [blocker],
        "required_gates": list(required_gates),
        "message": message,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _question_external_id(question: Any) -> Any:
    metadata = getattr(question, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        for key in (
            "external_question_id",
            "externalQuestionId",
            "question_id",
            "questionId",
            "id",
        ):
            value = metadata.get(key)
            if value is not None:
                return value
    return getattr(question, "id", None)


def _answered_questions_for_session(
    conn: Any,
    task_id: str,
    question_ids: list[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    if not question_ids:
        return [], []

    from hermes_cli import kanban_db as kb

    by_id = {q.id: q for q in kb.list_task_questions(conn, task_id)}
    answered: list[dict[str, Any]] = []
    unresolved: list[int] = []
    for question_id in question_ids:
        question = by_id.get(int(question_id))
        if question is None or question.status != "answered" or not question.answer:
            unresolved.append(int(question_id))
            continue
        answered.append(
            {
                "id": question.id,
                "external_question_id": _question_external_id(question),
                "question": question.question,
                "answer": question.answer,
                "answer_shape": question.answer_shape,
                "metadata": dict(question.metadata or {}),
            }
        )
    return answered, unresolved


def _resume_payload_if_allowed(
    conn: Any,
    task_id: str,
    active: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not active.get("gjc_session_id") or not active.get("gjc_turn_id"):
        return None

    status = str(active.get("turn_status") or "").strip()
    question_ids = [int(qid) for qid in active.get("question_ids") or []]
    if status in {"waiting", "waiting_for_answer", "approval_waiting"} and question_ids:
        answered, unresolved = _answered_questions_for_session(conn, task_id, question_ids)
        if unresolved:
            return None
        return {
            "mode": "submit_answers",
            "gjc_record_id": active.get("id"),
            "gjc_session_id": active.get("gjc_session_id"),
            "gjc_turn_id": active.get("gjc_turn_id"),
            "question_ids": question_ids,
            "answered_questions": answered,
        }

    if status in {"waiting", "waiting_for_coordinator", "timeout"}:
        return {
            "mode": "await",
            "gjc_record_id": active.get("id"),
            "gjc_session_id": active.get("gjc_session_id"),
            "gjc_turn_id": active.get("gjc_turn_id"),
            "question_ids": question_ids,
            "answered_questions": [],
        }

    return None


def prepare_current_task_gjc_execution(
    *,
    config: Mapping[str, Any] | None,
    routing_metadata: Mapping[str, Any],
    prompt: Any,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Return a GJC execution payload or a fail-closed blocker."""
    selected_lane = str(routing_metadata.get("selected_lane") or "")
    if selected_lane not in GJC_LANES:
        return {}

    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_id = _current_run_id()
    workflow = _workflow_for_lane(selected_lane, routing_metadata)
    coord_cfg = coordinator_config(config)

    if not task_id:
        return _blocker(
            "gjc_task_state_required",
            "GJC escalation requires a kanban task so approval, questions, evidence, and turns are durable.",
            required_gates=("kanban_task", "approval_gate", "evidence_store"),
        )
    if not coord_cfg.configured:
        return _blocker(
            "coordinator_mcp_not_configured",
            "GJC escalation is approved by policy only when smart_model_routing.gjc_coordinator is enabled and configured.",
            required_gates=("coordinator_mcp",),
        )

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        approval = kb.ensure_task_approval_requested(
            conn,
            task_id,
            approval_type=GJC_APPROVAL_TYPE,
            reason=f"Allow GJC {workflow} execution for lane {selected_lane}.",
            metadata={
                "selected_lane": selected_lane,
                "workflow": workflow,
                "mutation_classes": list(routing_metadata.get("mutation_classes") or []),
            },
            run_id=run_id,
            requested_by="smart_model_routing",
        )
        state = kb.gjc_coordination_state(
            conn,
            task_id,
            approval_type=GJC_APPROVAL_TYPE,
        )
        if not state["approved"]:
            return _blocker(
                "gjc_approval_required",
                (
                    f"GJC escalation for task {task_id} is waiting for approval "
                    f"{approval.id}. Approve it with `hermes kanban approval approve {approval.id}`."
                ),
                required_gates=("approved_plan", "mutation_approval"),
                extra={"approval_id": approval.id, "task_id": task_id},
            )
        if state["open_questions"]:
            question_ids = [q["id"] for q in state["open_questions"]]
            return _blocker(
                "gjc_questions_unanswered",
                (
                    f"GJC escalation for task {task_id} has unanswered question(s): "
                    + ", ".join(str(qid) for qid in question_ids)
                ),
                required_gates=("question_answers",),
                extra={"question_ids": question_ids, "task_id": task_id},
            )
        active = state.get("active_session")
        if active and active.get("active_turn_policy", "reject") == "reject":
            resume = _resume_payload_if_allowed(conn, task_id, active)
            if resume:
                return {
                    "enabled": True,
                    "task_id": task_id,
                    "run_id": run_id,
                    "lane": selected_lane,
                    "workflow": workflow,
                    "prompt": prompt,
                    "cwd": cwd or os.getcwd(),
                    "routing": dict(routing_metadata),
                    "resume": resume,
                    "coordinator": {
                        "command": coord_cfg.command,
                        "args": list(coord_cfg.args),
                        "env": dict(coord_cfg.env),
                        "tool": coord_cfg.tool_name,
                        "connect_timeout": coord_cfg.connect_timeout,
                        "timeout": coord_cfg.timeout,
                        "session_command": coord_cfg.session_command,
                        "question_answer_tool": str(
                            _coordinator_block(config).get("question_answer_tool")
                            or "gjc_coordinator_submit_question_answer"
                        ),
                        "bounded_await_tool": str(
                            _coordinator_block(config).get("bounded_await_tool")
                            or "gjc_coordinator_await_turn"
                        ),
                    },
                }
            return _blocker(
                "gjc_active_turn_exists",
                (
                    f"GJC escalation for task {task_id} already has active turn "
                    f"{active.get('id')}."
                ),
                required_gates=("active_turn_policy",),
                extra={"gjc_record_id": active.get("id"), "task_id": task_id},
            )

    return {
        "enabled": True,
        "task_id": task_id,
        "run_id": run_id,
        "lane": selected_lane,
        "workflow": workflow,
        "prompt": prompt,
        "cwd": cwd or os.getcwd(),
        "routing": dict(routing_metadata),
        "coordinator": {
            "command": coord_cfg.command,
            "args": list(coord_cfg.args),
            "env": dict(coord_cfg.env),
            "tool": coord_cfg.tool_name,
            "connect_timeout": coord_cfg.connect_timeout,
            "timeout": coord_cfg.timeout,
            "session_command": coord_cfg.session_command,
            "question_answer_tool": str(
                _coordinator_block(config).get("question_answer_tool")
                or "gjc_coordinator_submit_question_answer"
            ),
            "bounded_await_tool": str(
                _coordinator_block(config).get("bounded_await_tool")
                or "gjc_coordinator_await_turn"
            ),
        },
    }


def _safe_env(extra: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "SHELL",
        "TMPDIR",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith("XDG_")}
    env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _mcp_text_result(result: Any) -> dict[str, Any]:
    is_error = bool(getattr(result, "isError", False))
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    text_result = "\n".join(parts)
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, Mapping):
            payload = dict(structured)
        else:
            payload = {"result": structured}
        if text_result:
            payload.setdefault("text", text_result)
    elif text_result:
        try:
            parsed = json.loads(text_result)
            payload = parsed if isinstance(parsed, dict) else {"result": parsed}
        except json.JSONDecodeError:
            payload = {"result": text_result}
    else:
        payload = {}
    if is_error:
        payload["error"] = payload.get("error") or text_result or "MCP tool returned an error"
    return payload


def _call_mcp_tool(
    coord: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    command = os.path.expanduser(str(coord.get("command") or "").strip())
    if not command:
        return {"error": "Coordinator MCP command is not configured."}
    if os.sep not in command and shutil.which(command) is None:
        return {"error": f"Coordinator MCP command {command!r} was not found on PATH."}

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        return {"error": "The mcp package is not installed; Coordinator MCP cannot run."}

    args = [str(a) for a in (coord.get("args") or [])]
    tool_name = str(coord.get("tool") or "start_workflow")
    connect_timeout = float(coord.get("connect_timeout") or 60)
    timeout = float(coord.get("timeout") or 300)
    env = _safe_env(coord.get("env") or {})

    async def _run() -> dict[str, Any]:
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments=dict(payload)),
                    timeout=timeout,
                )
                return _mcp_text_result(result)

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        # If an event loop is already running, use a short-lived thread via the
        # default executor pattern would be overkill here. Surface a clear error;
        # gateway calls this helper through run_in_executor.
        return {"error": f"Coordinator MCP runtime error: {exc}"}
    except Exception as exc:
        return {"error": f"Coordinator MCP call failed: {type(exc).__name__}: {exc}"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _result_status(result: Mapping[str, Any], *, question_ids: list[int]) -> str:
    status = str(
        _first_present(result, "turn_status", "turnStatus", "status", "terminal_status")
        or ""
    ).strip()
    if status:
        return status
    if question_ids:
        return "waiting_for_answer"
    return "done"


def _submit_answered_questions(
    coord: Mapping[str, Any],
    execution: Mapping[str, Any],
    resume: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    answered_questions = [
        q for q in _as_list(resume.get("answered_questions")) if isinstance(q, Mapping)
    ]
    if not answered_questions:
        return [], None

    answer_tool = str(
        coord.get("question_answer_tool")
        or coord.get("answer_tool")
        or "gjc_coordinator_submit_question_answer"
    ).strip()
    if not answer_tool:
        return [], "Coordinator MCP question answer tool is not configured."

    answer_coord = dict(coord)
    answer_coord["tool"] = answer_tool
    submitted: list[dict[str, Any]] = []
    for question in answered_questions:
        payload = {
            "task_id": execution.get("task_id"),
            "run_id": execution.get("run_id"),
            "session_id": resume.get("gjc_session_id"),
            "turn_id": resume.get("gjc_turn_id"),
            "gjc_session_id": resume.get("gjc_session_id"),
            "gjc_turn_id": resume.get("gjc_turn_id"),
            "question_id": question.get("external_question_id") or question.get("id"),
            "hermes_question_id": question.get("id"),
            "answer": question.get("answer"),
            "answer_shape": question.get("answer_shape"),
            "allow_mutation": bool(
                (execution.get("routing") or {}).get("allow_mutation")
                if isinstance(execution.get("routing"), Mapping)
                else False
            ),
        }
        result = _call_mcp_tool(answer_coord, payload)
        error = str(result.get("error") or "").strip()
        if error:
            return submitted, error
        submitted.append(
            {
                "question_id": question.get("id"),
                "external_question_id": payload["question_id"],
                "result": result,
            }
        )
    return submitted, None


def run_gjc_execution(execution: Mapping[str, Any]) -> dict[str, Any]:
    """Execute an approved GJC turn through Coordinator MCP and persist state."""
    if not execution.get("enabled"):
        return {"final_response": execution.get("message") or "GJC execution is not enabled."}

    from hermes_cli import kanban_db as kb

    task_id = str(execution.get("task_id") or "")
    run_id = execution.get("run_id")
    lane = str(execution.get("lane") or "gjc_ralplan")
    workflow = str(execution.get("workflow") or "ralplan")
    coord = execution.get("coordinator") or {}
    routing = execution.get("routing") if isinstance(execution.get("routing"), Mapping) else {}
    resume = execution.get("resume") if isinstance(execution.get("resume"), Mapping) else {}
    gjc_record_id = int(resume.get("gjc_record_id") or 0) if resume else 0

    with kb.connect_closing() as conn:
        if gjc_record_id:
            kb.update_gjc_session(
                conn,
                gjc_record_id,
                turn_status="running",
                blocker=None,
                metadata={"routing": dict(routing), "resume": dict(resume)},
            )
        else:
            gjc_record_id = kb.create_gjc_session(
                conn,
                task_id,
                run_id=run_id if isinstance(run_id, int) else None,
                lane=lane,
                workflow=workflow,
                turn_status="starting",
                active_turn_policy="reject",
                approval_gate=GJC_APPROVAL_TYPE,
                metadata={"routing": dict(routing)},
            )

    payload = {
        "task_id": task_id,
        "run_id": run_id,
        "lane": lane,
        "workflow": workflow,
        "prompt": execution.get("prompt"),
        "task": execution.get("prompt"),
        "cwd": execution.get("cwd"),
        "allow_mutation": bool(routing.get("allow_mutation")),
        "policy": dict(routing),
    }
    if resume:
        payload["resume"] = dict(resume)
        payload["gjc_session_id"] = resume.get("gjc_session_id")
        payload["gjc_turn_id"] = resume.get("gjc_turn_id")
        payload["session_id"] = resume.get("gjc_session_id")
        payload["turn_id"] = resume.get("gjc_turn_id")
        submitted_answers, submit_error = _submit_answered_questions(coord, execution, resume)
        if submit_error:
            result = {"error": submit_error, "submitted_answers": submitted_answers}
        else:
            await_coord = dict(coord)
            await_coord["tool"] = str(
                coord.get("bounded_await_tool") or "gjc_coordinator_await_turn"
            )
            result = _call_mcp_tool(await_coord, payload)
            result.setdefault("submitted_answers", submitted_answers)
    else:
        result = _call_mcp_tool(coord, payload)

    now = int(time.time())
    error = str(result.get("error") or "").strip()
    question_ids: list[int] = []
    evidence_paths: list[str] = []
    artifact_refs: list[str] = []

    with kb.connect_closing() as conn:
        if error:
            if resume:
                error_status = (
                    "waiting_for_answer"
                    if resume.get("mode") == "submit_answers"
                    else "waiting_for_coordinator"
                )
                terminal_status = "coordination_error"
            else:
                error_status = "blocked"
                terminal_status = "error"
            kb.update_gjc_session(
                conn,
                gjc_record_id,
                turn_status=error_status,
                blocker=error,
                terminal_status=terminal_status,
                metadata={"result": result, "routing": dict(routing)},
            )
            return {
                "failed": True,
                "final_response": f"GJC Coordinator MCP failed: {error}",
                "gjc_record_id": gjc_record_id,
                "result": result,
            }

        for item in _as_list(result.get("questions")):
            if isinstance(item, Mapping):
                question = str(
                    item.get("question") or item.get("prompt") or item.get("text") or ""
                ).strip()
                answer_shape = str(
                    item.get("answer_shape") or item.get("answerSchema") or "text"
                )
                metadata = dict(item)
            else:
                question = str(item).strip()
                answer_shape = "text"
                metadata = {}
            if question:
                question_ids.append(
                    kb.request_task_question(
                        conn,
                        task_id,
                        question=question,
                        answer_shape=answer_shape,
                        metadata=metadata,
                        run_id=run_id if isinstance(run_id, int) else None,
                    )
                )

        approval_request = result.get("approval") or result.get("approval_request")
        if isinstance(approval_request, Mapping):
            kb.request_task_approval(
                conn,
                task_id,
                approval_type=str(approval_request.get("type") or GJC_APPROVAL_TYPE),
                reason=str(approval_request.get("reason") or "GJC requested approval."),
                metadata=dict(approval_request),
                run_id=run_id if isinstance(run_id, int) else None,
                requested_by="gjc_coordinator",
            )

        for path in _as_list(
            _first_present(result, "evidence_paths", "evidencePaths")
        ):
            path_text = str(path or "").strip()
            if path_text:
                evidence_paths.append(path_text)
                kb.record_task_evidence(
                    conn,
                    task_id,
                    kind="gjc_evidence",
                    path=path_text,
                    run_id=run_id if isinstance(run_id, int) else None,
                )

        for ref in _as_list(
            _first_present(result, "artifact_refs", "artifactRefs", "artifacts")
        ):
            ref_text = str(ref or "").strip()
            if ref_text:
                artifact_refs.append(ref_text)
                kb.record_task_evidence(
                    conn,
                    task_id,
                    kind="gjc_artifact",
                    ref=ref_text,
                    run_id=run_id if isinstance(run_id, int) else None,
                )

        final_response = str(
            _first_present(
                result,
                "final_response",
                "finalResponse",
                "response",
                "result",
            )
            or ""
        ).strip()
        status = _result_status(result, question_ids=question_ids)
        kb.update_gjc_session(
            conn,
            gjc_record_id,
            gjc_session_id=_first_present(
                result, "gjc_session_id", "gjcSessionId", "session_id", "sessionId"
            )
            or resume.get("gjc_session_id"),
            gjc_turn_id=_first_present(
                result, "gjc_turn_id", "gjcTurnId", "turn_id", "turnId"
            )
            or resume.get("gjc_turn_id"),
            turn_status=status,
            event_after_seq=_first_present(result, "event_after_seq", "eventAfterSeq"),
            question_ids=question_ids or list(resume.get("question_ids") or []),
            evidence_paths=evidence_paths,
            artifact_refs=artifact_refs,
            final_response_ref=_first_present(
                result, "final_response_ref", "finalResponseRef"
            ),
            terminal_status=result.get("terminal_status") or status,
            report_status_written_at=now
            if _first_present(result, "report_status_written", "reportStatusWritten")
            else None,
            metadata={"result": result, "routing": dict(routing)},
        )

    if question_ids:
        final_response = (
            "GJC Coordinator MCP is waiting for question answer(s): "
            + ", ".join(str(qid) for qid in question_ids)
        )
    return {
        "final_response": final_response or "GJC Coordinator MCP turn completed.",
        "gjc_record_id": gjc_record_id,
        "question_ids": question_ids,
        "evidence_paths": evidence_paths,
        "artifact_refs": artifact_refs,
        "result": result,
    }
