"""Tests for tools/notes_tool.py — the notes-tier tool family (ADR-004 Phase 1).

Covers: the notes_write two-step contract through the tool surface, the
notes_read read/list surface (reads count as retrieval hits), the
memory_propose → pending-WAL record shape (durable, unconsumed until acked),
and the tool schemas' behavioral guidance.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from tools.notes_tool import (
    MEMORY_PROPOSE_SCHEMA,
    NOTES_READ_SCHEMA,
    NOTES_WRITE_SCHEMA,
    handle_notes_tool,
    memory_propose_tool,
    notes_read_tool,
    notes_write_tool,
)

EP = [{"type": "episode", "uuid": "a" * 32}]
SESSION = "sess-tool-1"


def _call_write(**kw) -> Dict[str, Any]:
    kw.setdefault("session_id", SESSION)
    return json.loads(notes_write_tool(**kw))


def _propose(content="jun prefers concise replies", kind="preference"):
    return _call_write(step="propose", content=content, kind=kind, evidence=EP)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_notes_write_schema_teaches_the_two_step_contract(self):
        desc = NOTES_WRITE_SCHEMA["description"]
        assert "propose" in desc and "confirm" in desc
        assert "NOOP" in desc
        assert "skills" in desc  # procedural routing is stated
        assert NOTES_WRITE_SCHEMA["parameters"]["required"] == ["step"]

    def test_memory_propose_schema_is_fire_and_forget(self):
        desc = MEMORY_PROPOSE_SCHEMA["description"]
        assert "curator" in desc
        assert "non-blocking" in desc

    def test_notes_read_schema_actions(self):
        props = NOTES_READ_SCHEMA["parameters"]["properties"]
        assert props["action"]["enum"] == ["read", "list"]


# ---------------------------------------------------------------------------
# notes_write two-step flow via the tool surface
# ---------------------------------------------------------------------------

class TestNotesWriteTool:
    def test_propose_then_confirm_add(self):
        proposed = _propose()
        assert proposed["success"] is True
        assert proposed["token"]
        assert proposed["neighbors"] == []

        confirmed = _call_write(
            step="confirm",
            token=proposed["token"],
            verdict="ADD",
            topic_key="jun.reply.style",
        )
        assert confirmed["success"] is True
        assert confirmed["note"]["ref"] == "preference/jun.reply.style"

        read = json.loads(
            notes_read_tool(action="read", kind="preference",
                            topic_key="jun.reply.style")
        )
        assert read["success"] is True
        assert read["note"]["body"] == "jun prefers concise replies"

    def test_confirm_requires_step_token(self):
        res = _call_write(step="confirm", verdict="ADD", topic_key="a.b")
        assert res["success"] is False

    def test_missing_step_is_an_error(self):
        res = _call_write()
        assert res.get("success") is False

    def test_second_propose_sees_first_note_as_neighbor(self):
        first = _propose()
        _call_write(
            step="confirm", token=first["token"], verdict="ADD",
            topic_key="jun.reply.style",
        )
        second = _propose(content="jun likes replies concise and direct")
        refs = [n["ref"] for n in second["neighbors"]]
        assert "preference/jun.reply.style" in refs

    def test_handle_notes_tool_routes_by_name(self):
        res = json.loads(
            handle_notes_tool(
                "notes_write",
                {"step": "propose", "content": "x is y", "kind": "fact",
                 "evidence": EP},
                session_id=SESSION,
            )
        )
        assert res["success"] is True
        unknown = json.loads(handle_notes_tool("notes_nonsense", {}))
        assert unknown["success"] is False


class _RecordingManager:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def sync_note_backfill(self, note_path, content, *, session_id="", metadata=None):
        self.calls.append({
            "note_path": note_path, "content": content,
            "session_id": session_id, "metadata": metadata or {},
        })


class TestNotesWriteBackfillSeam:
    def test_confirm_with_flag_off_never_touches_the_manager(self):
        manager = _RecordingManager()
        proposed = _propose()
        confirmed = _call_write(
            step="confirm", token=proposed["token"], verdict="ADD",
            topic_key="jun.reply.style", memory_manager=manager,
        )
        assert confirmed["success"] is True
        assert manager.calls == []  # memory.notes_backfill_enabled default OFF

    def test_confirm_with_flag_on_enqueues_backfill(self, monkeypatch):
        import agent.memory_pipeline as mp

        monkeypatch.setattr(mp, "notes_backfill_enabled", lambda: True)
        manager = _RecordingManager()
        proposed = _propose()
        _call_write(
            step="confirm", token=proposed["token"], verdict="ADD",
            topic_key="jun.reply.style", memory_manager=manager,
        )
        assert len(manager.calls) == 1
        call = manager.calls[0]
        assert call["metadata"]["source_name"] == "hermes-notes"
        assert call["metadata"]["source_id"] == call["note_path"]
        assert call["session_id"] == SESSION


# ---------------------------------------------------------------------------
# notes_read
# ---------------------------------------------------------------------------

class TestNotesReadTool:
    def test_read_counts_a_retrieval_hit(self):
        proposed = _propose()
        _call_write(
            step="confirm", token=proposed["token"], verdict="ADD",
            topic_key="jun.reply.style",
        )
        notes_read_tool(action="read", kind="preference",
                        topic_key="jun.reply.style")
        second = json.loads(
            notes_read_tool(action="read", kind="preference",
                            topic_key="jun.reply.style")
        )
        assert second["note"]["usage"]["search_hits"] >= 1

    def test_list_renders_index_lines(self):
        proposed = _propose()
        _call_write(
            step="confirm", token=proposed["token"], verdict="ADD",
            topic_key="jun.reply.style",
        )
        res = json.loads(notes_read_tool(action="list"))
        assert res["success"] is True
        assert res["count"] == 1
        assert res["index"][0].startswith("- preference/jun.reply.style:")

    def test_read_missing_note_is_a_recoverable_error(self):
        res = json.loads(
            notes_read_tool(action="read", kind="fact", topic_key="no.such")
        )
        assert res["success"] is False


# ---------------------------------------------------------------------------
# memory_propose → pending WAL record shape
# ---------------------------------------------------------------------------

class TestMemoryProposeTool:
    def _wal_records(self) -> List[Dict[str, Any]]:
        from hermes_constants import get_hermes_home

        path = (
            get_hermes_home() / "state" / "memory-pending" / f"{SESSION}.jsonl"
        )
        assert path.exists()
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_proposal_record_shape(self):
        res = json.loads(
            memory_propose_tool(
                content="soju07 daemon deploy is still pending",
                kind_hint="project",
                evidence=EP,
                session_id=SESSION,
            )
        )
        assert res["success"] is True and res["queued"] is True

        recs = self._wal_records()
        assert len(recs) == 1
        rec = recs[0]
        assert rec["type"] == "proposal"
        assert rec["kind"] == "proposal"
        assert rec["id"] == res["entry_id"]
        assert rec["session_id"] == SESSION
        assert rec["content"] == "soju07 daemon deploy is still pending"
        assert rec["kind_hint"] == "project"
        assert rec["evidence_refs"] == EP
        assert rec["origin"] == "user"
        assert isinstance(rec["ts"], float)

    def test_proposals_count_as_unconsumed_in_the_startup_scan(self):
        from agent.memory_journal import PendingTurnWAL

        memory_propose_tool(content="candidate fact", session_id=SESSION)
        stats = PendingTurnWAL().scan_and_gc()
        assert stats["unconsumed_entries"] == 1

    def test_secrets_are_scrubbed_from_queued_proposals(self):
        memory_propose_tool(
            content="token is ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            session_id=SESSION,
        )
        recs = self._wal_records()
        assert "A1b2C3d4E5f6G7h8I9j0" not in recs[-1]["content"]

    def test_empty_content_is_rejected(self):
        res = json.loads(memory_propose_tool(content="", session_id=SESSION))
        assert res["success"] is False
