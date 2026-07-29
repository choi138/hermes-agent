#!/usr/bin/env python3
"""Notes tools — the declarative-gist memory tier (ADR-004 Phase 1).

Three tools in the memory family:

* ``notes_write`` — the §③ write pipeline's two-step contract:
  ``propose`` (scrub → taint check → kind routing → neighbor retrieval,
  returns neighbors + a TTL'd token) then ``confirm`` (caller's
  ADD/UPDATE/SUPERSEDE/NOOP verdict + mechanical quote-grounding). A
  verdict without a token is impossible, so no caller can write a note
  without first seeing its neighbors.
* ``notes_read`` — read one note (counts as a retrieval hit) or list the
  index.
* ``memory_propose`` — fire-and-forget: queue a candidate fact into the
  pending WAL (``type: proposal``) for the Phase-2 curator. 0 LLM calls,
  non-blocking.

Dispatch mirrors ``tools/memory_tool.py``: registered in the tool registry
(schemas + standalone contexts) AND dispatched inline by
``agent/tool_executor.py`` so agent-level state (session id, the
MemoryManager for the flag-gated notes→graph backfill) can be injected.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# One pipeline per HERMES_HOME: the propose→confirm token map lives on the
# pipeline instance, so it must be process-stable across tool calls (a
# fresh pipeline per call would forget every issued token).
_pipelines: Dict[str, Any] = {}
_pipelines_lock = threading.Lock()


def _get_pipeline():
    from hermes_constants import get_hermes_home

    key = str(get_hermes_home())
    with _pipelines_lock:
        pipeline = _pipelines.get(key)
        if pipeline is None:
            from agent.memory_pipeline import MemoryWritePipeline

            pipeline = MemoryWritePipeline()
            _pipelines[key] = pipeline
        return pipeline


def notes_write_tool(
    step: Optional[str] = None,
    content: Optional[str] = None,
    kind: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    topic_key: Optional[str] = None,
    token: Optional[str] = None,
    verdict: Optional[str] = None,
    target: Optional[str] = None,
    evidence_overrides: Optional[Dict[Any, str]] = None,
    session_id: str = "",
    memory_manager: Any = None,
) -> str:
    """Two-step notes write. See module docstring for the contract."""
    pipeline = _get_pipeline()
    step = (step or "").strip().lower()

    if step == "propose":
        # origin="agent": this surface is the main agent deciding to write
        # mid-session — NOT the user pinning a fact. Honest writer
        # provenance keeps §①-5b live for the hot path (agent-origin
        # one-shot decision/incident lands status=unconfirmed). Curator/
        # dream callers use the pipeline API directly with their own origin.
        result = pipeline.propose(
            content or "",
            kind_hint=kind or "",
            evidence_refs=evidence,
            origin="agent",
            session_id=session_id,
            caller="agent-tool",
            topic_key_hint=topic_key or "",
        )
        return json.dumps(result, ensure_ascii=False)

    if step == "confirm":
        result = pipeline.confirm(
            token or "",
            verdict or "",
            topic_key=topic_key or "",
            kind=kind or "",
            target=target or "",
            evidence_overrides=evidence_overrides,
            session_id=session_id,
            caller="agent-tool",
        )
        # §③ step 7 — typed episode backfill for landed writes. Inert unless
        # memory.notes_backfill_enabled (default OFF: daemon pass-through not
        # yet deployed) AND the caller supplied an ingest-allowed manager.
        if result.get("success") and result.get("note") and result.get(
            "verdict"
        ) in ("ADD", "UPDATE", "SUPERSEDE"):
            # (staged results carry no "note" — nothing was written yet, so
            # there is nothing to backfill; the approval replay path is
            # local-only by design.)
            try:
                from agent.memory_pipeline import maybe_enqueue_note_backfill

                ref = (result.get("note") or {}).get("ref") or ""
                n_kind, _, n_key = ref.partition("/")
                note = pipeline.store.read(n_kind, n_key)
                maybe_enqueue_note_backfill(
                    memory_manager, note, session_id=session_id
                )
            except Exception:
                logger.debug("note backfill hook failed (fail-open)", exc_info=True)
        return json.dumps(result, ensure_ascii=False)

    return tool_error(
        "notes_write requires step='propose' (content+kind+evidence) or "
        "step='confirm' (token+verdict).",
        success=False,
    )


def notes_read_tool(
    action: Optional[str] = None,
    kind: Optional[str] = None,
    topic_key: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Read one note or list the notes index."""
    from agent.notes_store import (
        NoteNotFoundError,
        NotesStore,
        NoteValidationError,
    )

    store = _get_pipeline().store
    action = (action or "list").strip().lower()

    if action == "read":
        if not kind or not topic_key:
            return tool_error(
                "notes_read(action='read') requires kind and topic_key.",
                success=False,
            )
        try:
            note = store.read(kind, topic_key)
        except NoteNotFoundError:
            return tool_error(
                f"No note {kind}/{topic_key}. Use notes_read(action='list').",
                success=False,
            )
        except NoteValidationError as e:
            return tool_error(str(e), success=False)
        # A tombstone leaves every read surface (permanent removal marker;
        # the body stays on disk for offline audit only) — and it must not
        # accrue usage, or reading a removed note would feed the promotion
        # signal.
        if note.get("status") == "tombstoned":
            return tool_error(
                f"Note {kind}/{topic_key} is tombstoned (permanently "
                f"removed).",
                success=False,
            )
        # An explicit read is a retrieval event — it feeds the usage signal
        # the dream promotion pass consumes (ADR-004 §⑤). Fail-open.
        store.bump_usage(kind, topic_key)
        return json.dumps({"success": True, "note": note}, ensure_ascii=False)

    if action == "list":
        try:
            notes = store.list_notes(kind=kind or None, status=status or None)
        except NoteValidationError as e:
            return tool_error(str(e), success=False)
        # Tombstones leave every read surface unless explicitly requested
        # (status='tombstoned' is the audit escape hatch); count and index
        # cover the SAME filtered set so they can never disagree.
        if status != "tombstoned":
            notes = [n for n in notes if n.get("status") != "tombstoned"]
        index = [NotesStore.render_index_line(n) for n in notes]
        return json.dumps(
            {"success": True, "count": len(notes), "index": index},
            ensure_ascii=False,
        )

    return tool_error(
        f"Unknown action {action!r}. Use 'read' or 'list'.", success=False
    )


def memory_propose_tool(
    content: Optional[str] = None,
    kind_hint: Optional[str] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    session_id: str = "",
) -> str:
    """Queue a proposal record into the pending WAL (0 LLM calls)."""
    content = (content or "").strip()
    if not content:
        return tool_error("content is required.", success=False)
    try:
        from agent.memory_journal import PendingTurnWAL

        # A fresh instance is cheap (no I/O at construction) and pins the
        # currently-active HERMES_HOME; appends serialize on the module-level
        # per-path locks either way.
        entry_id = PendingTurnWAL().append_proposal(
            session_id,
            content,
            kind_hint=kind_hint or "",
            evidence_refs=list(evidence or []),
            origin="agent",  # honest provenance: the agent flagged it
        )
    except Exception as e:  # pragma: no cover - append_proposal is fail-open
        return tool_error(f"proposal queueing failed: {e}", success=False)
    if not entry_id:
        return tool_error(
            "proposal was not queued (memory journals disabled or "
            "unavailable).",
            success=False,
        )
    return json.dumps(
        {
            "success": True,
            "queued": True,
            "entry_id": entry_id,
            "message": (
                "Proposal queued for the ingest curator (durable, no LLM "
                "calls). Do not repeat it and do not block on it."
            ),
        },
        ensure_ascii=False,
    )


def handle_notes_tool(
    function_name: str,
    args: Dict[str, Any],
    *,
    session_id: str = "",
    memory_manager: Any = None,
) -> str:
    """Inline-dispatch entry point used by agent/tool_executor.py."""
    args = args or {}
    if function_name == "notes_write":
        return notes_write_tool(
            step=args.get("step"),
            content=args.get("content"),
            kind=args.get("kind"),
            evidence=args.get("evidence"),
            topic_key=args.get("topic_key"),
            token=args.get("token"),
            verdict=args.get("verdict"),
            target=args.get("target"),
            evidence_overrides=args.get("evidence_overrides"),
            session_id=session_id,
            memory_manager=memory_manager,
        )
    if function_name == "notes_read":
        return notes_read_tool(
            action=args.get("action"),
            kind=args.get("kind"),
            topic_key=args.get("topic_key"),
            status=args.get("status"),
        )
    if function_name == "memory_propose":
        return memory_propose_tool(
            content=args.get("content"),
            kind_hint=args.get("kind_hint"),
            evidence=args.get("evidence"),
            session_id=session_id,
        )
    return tool_error(f"Unknown notes tool {function_name!r}.", success=False)


def dispatch_notes_tool_for_agent(
    agent: Any, function_name: str, args: Dict[str, Any]
) -> str:
    """Dispatch with agent-level state resolved (both executor paths use
    this): the session id, and the MemoryManager for the flag-gated
    notes→graph backfill — withheld from ingest-disabled forks for the same
    reason built-in memory writes don't fan out there (ADR-004 Phase 0)."""
    manager = None
    try:
        from agent.memory_manager import memory_ingest_allowed

        candidate = getattr(agent, "_memory_manager", None)
        if candidate is not None and memory_ingest_allowed(agent):
            manager = candidate
    except Exception:
        manager = None
    return handle_notes_tool(
        function_name,
        args,
        session_id=getattr(agent, "session_id", "") or "",
        memory_manager=manager,
    )


def check_notes_requirements() -> bool:
    """Notes are file-backed under HERMES_HOME — always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schemas
# =============================================================================

_EVIDENCE_SCHEMA = {
    "type": "array",
    "description": (
        "Evidence references (>=1 for writes). Each item: "
        "{type:'episode', uuid} for a graph episode UUID, or "
        "{type:'wal', session_id, entry_id, quote} / "
        "{type:'l0', month:'YYYY-MM', quote, wal_entry_id?} for a local "
        "journal record — the quote must be a VERBATIM substring of that "
        "record, substantive (a phrase, not a 2-word fragment), and must "
        "never contain secret material. Set tainted:true on any span that "
        "came from injected memory context (it will be refused — memory "
        "citing itself is not evidence)."
    ),
    "items": {"type": "object"},
}

NOTES_WRITE_SCHEMA = {
    "name": "notes_write",
    "description": (
        "Save a durable DECLARATIVE fact (user-independent knowledge: "
        "decisions, incidents, preferences, relationships, project facts) as "
        "a curated note with cited evidence. Two mandatory steps:\n"
        "1. step='propose' with content + kind + evidence → returns existing "
        "NEIGHBOR notes, a grounding_preview, and a short-lived token.\n"
        "2. step='confirm' with token + verdict: NOOP if a neighbor already "
        "covers the fact (this should be your most common verdict), UPDATE/"
        "SUPERSEDE a listed neighbor (target='kind/topic.key'), or ADD with a "
        "new topic_key.\n"
        "Notes are NOT for: instructions to yourself (memory tool), "
        "procedures/workflows (skills — separately gated), raw logs or task "
        "progress (session_search/graph). Every write must cite evidence; "
        "quotes are machine-checked verbatim against local journals. If a "
        "grounding_preview item fails but offers candidates, either select "
        "one at confirm with evidence_overrides={ref_index:candidate_id}, or "
        "fix the quote and propose again. Confirming without resolving a "
        "failed preview is rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "step": {
                "type": "string",
                "enum": ["propose", "confirm"],
                "description": "Which pipeline step to run.",
            },
            "content": {
                "type": "string",
                "description": "propose: the gist text to save (<=4KB).",
            },
            "kind": {
                "type": "string",
                "enum": [
                    "decision", "incident", "preference", "relationship",
                    "project", "fact",
                ],
                "description": "propose: declarative kind of the fact.",
            },
            "evidence": _EVIDENCE_SCHEMA,
            "topic_key": {
                "type": "string",
                "description": (
                    "Dotted lowercase topic key, e.g. 'nas.nfs.access'. "
                    "Optional hint at propose; required for confirm ADD."
                ),
            },
            "token": {
                "type": "string",
                "description": "confirm: the token returned by propose.",
            },
            "verdict": {
                "type": "string",
                "enum": ["ADD", "UPDATE", "SUPERSEDE", "NOOP"],
                "description": (
                    "confirm: your verdict against the returned neighbors. "
                    "NOOP when already covered."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "confirm UPDATE/SUPERSEDE: the neighbor to act on, as "
                    "'kind/topic.key' — must be one of the proposed neighbors."
                ),
            },
            "evidence_overrides": {
                "type": "object",
                "description": (
                    "confirm: map a zero-based evidence-ref index to a "
                    "candidate_id returned in that ref's grounding_preview. "
                    "The server substitutes its cached exact journal excerpt "
                    "and source coordinates; caller-provided quote text is "
                    "never accepted here."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["step"],
    },
}

NOTES_READ_SCHEMA = {
    "name": "notes_read",
    "description": (
        "Read curated notes (the declarative memory tier). action='list' "
        "shows the one-line index (optionally filtered by kind/status); "
        "action='read' loads one full note by kind + topic_key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "list"]},
            "kind": {
                "type": "string",
                "enum": [
                    "decision", "incident", "preference", "relationship",
                    "project", "fact",
                ],
            },
            "topic_key": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["active", "unconfirmed", "demoted", "tombstoned"],
            },
        },
        "required": ["action"],
    },
}

MEMORY_PROPOSE_SCHEMA = {
    "name": "memory_propose",
    "description": (
        "Flag a fact from THIS conversation as memory-worthy without writing "
        "anything yet: queues a durable proposal for the background memory "
        "curator (zero cost, non-blocking). Use when something seems worth "
        "remembering but doesn't merit an immediate note/memory write — the "
        "curator will weigh it with full-session context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The candidate fact, stated declaratively.",
            },
            "kind_hint": {
                "type": "string",
                "description": (
                    "Optional routing hint: decision|incident|preference|"
                    "relationship|project|fact|instruction|procedural|evidence."
                ),
            },
            "evidence": _EVIDENCE_SCHEMA,
        },
        "required": ["content"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="notes_write",
    toolset="memory",
    schema=NOTES_WRITE_SCHEMA,
    handler=lambda args, **kw: notes_write_tool(
        step=args.get("step"),
        content=args.get("content"),
        kind=args.get("kind"),
        evidence=args.get("evidence"),
        topic_key=args.get("topic_key"),
        token=args.get("token"),
        verdict=args.get("verdict"),
        target=args.get("target"),
        evidence_overrides=args.get("evidence_overrides"),
        session_id=kw.get("session_id") or "",
        memory_manager=kw.get("memory_manager"),
    ),
    check_fn=check_notes_requirements,
    emoji="📝",
)

registry.register(
    name="notes_read",
    toolset="memory",
    schema=NOTES_READ_SCHEMA,
    handler=lambda args, **kw: notes_read_tool(
        action=args.get("action"),
        kind=args.get("kind"),
        topic_key=args.get("topic_key"),
        status=args.get("status"),
    ),
    check_fn=check_notes_requirements,
    emoji="📖",
)

registry.register(
    name="memory_propose",
    toolset="memory",
    schema=MEMORY_PROPOSE_SCHEMA,
    handler=lambda args, **kw: memory_propose_tool(
        content=args.get("content"),
        kind_hint=args.get("kind_hint"),
        evidence=args.get("evidence"),
        session_id=kw.get("session_id") or "",
    ),
    check_fn=check_notes_requirements,
    emoji="💡",
)
