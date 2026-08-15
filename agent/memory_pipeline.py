"""Single write-pipeline entry point for durable memory (ADR-004 §③, Phase 1).

Every durable *notes* write flows through here::

    candidate(content, kind_hint, evidence_refs, origin)
     0. deterministic scrub + injection-pattern hard reject
     1. origin-taint check (Phase-1 interface: caller-marked taint refused;
        full n-gram tainting is Phase 2 — see evidence_ref_is_tainted)
     2. kind routing   instruction→memory tool | declarative→notes |
                       procedural→REJECT (skills are gated separately) |
                       evidence→existing graph ingest path
     3. neighbor retrieval (NotesStore.neighbor_search, deterministic)
     4. verdict        ADD | UPDATE | SUPERSEDE | NOOP — decided by the
                       CALLER, but only after receiving the neighbor list:
                       the two-step propose→confirm contract binds the
                       neighbor snapshot to a TTL'd token, so a caller can
                       never write without having seen its neighbors first.
     5. grounded admission — every evidence ref is format-validated and,
        where a local journal holds the record, its quote must
        substring-match the (scrubbed) WAL / L0-mirror content. Episode
        UUIDs are format-validated (graph existence checks are the graph
        side's job).
     6. ledger append  ~/.hermes/state/memory-notes-ledger.jsonl
                       (verdict, checks, caller) — fail-open telemetry.
     7. backfill seam  notes create/update enqueue a typed episode backfill
        (source_name="hermes-notes", source_id=note_path) via the existing
        mem-sync path — config-gated OFF by default (see
        notes_backfill_enabled) until the graphiti daemon pass-through
        deploy.

The *instruction* tier (MEMORY.md/USER.md via the ``memory`` tool + its
skill-gate) and the *evidence* tier (per-turn graph ingest) keep their
existing paths untouched — step 2 only routes, it does not reimplement
them.

Evidence-ref wire format (dicts, from tools or curator/dream callers)::

    {"type": "episode", "uuid": "<uuid|32hex>"}
    {"type": "wal", "session_id": ..., "entry_id": ..., "quote": "..."}
    {"type": "l0",  "month": "YYYY-MM", "wal_entry_id": ...?, "quote": "..."}

Any ref may carry ``"tainted": true`` — the Phase-1 origin-taint marker for
spans that came from ``<memory-context>`` injection / ``memory_search``
results (or paraphrases thereof). Tainted refs are quote-ineligible.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_journal import (
    _append_jsonl,
    _iter_jsonl_records,
    _safe_session_filename,
    _scrub,
)
from agent.notes_store import (
    DEFAULT_NOTES_MAX_ENTRIES,
    NOTE_KINDS,
    NoteNotFoundError,
    NotesStore,
    NoteValidationError,
    note_ref,
)

logger = logging.getLogger(__name__)

# Proposal tokens bind the neighbor snapshot for a bounded window: long
# enough for a propose→confirm round trip inside one agent turn, short
# enough that a stale snapshot can't authorize a write much later.
PROPOSAL_TTL_S = 10 * 60

VERDICTS = ("ADD", "UPDATE", "SUPERSEDE", "NOOP")

# Non-notes kind hints that step 2 routes elsewhere.
KIND_INSTRUCTION = "instruction"
KIND_PROCEDURAL = "procedural"
KIND_EVIDENCE = "evidence"

_EPISODE_UUID_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$",
    re.IGNORECASE,
)

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _memory_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return (load_config() or {}).get("memory", {}) or {}
    except Exception:
        return {}


def notes_backfill_enabled() -> bool:
    """Config gate for the §③ step-7 typed-episode backfill (default OFF).

    The graphiti daemon's IngestRequest pass-through
    (saga/episode_type/source_name/source_id/episode_metadata) is MERGED
    upstream but NOT YET DEPLOYED to the live daemon — until that deploy,
    nothing may send the new ingest fields, so the backfill enqueue is
    inert unless ``memory.notes_backfill_enabled: true`` is set explicitly.
    """
    return bool(_memory_config().get("notes_backfill_enabled", False))


def notes_max_entries() -> int:
    """Notes index cap — config ``memory.notes_max_entries`` is the SoT."""
    try:
        return int(
            _memory_config().get("notes_max_entries", DEFAULT_NOTES_MAX_ENTRIES)
        )
    except Exception:
        return DEFAULT_NOTES_MAX_ENTRIES


def evidence_ref_is_tainted(ref: Dict[str, Any]) -> bool:
    """Phase-1 origin-taint interface (ADR-004 §① echo-chamber rule).

    Callers MUST set ``tainted: true`` on any evidence ref whose span
    originates from injected memory context (``<memory-context>`` prefetch
    fences, ``memory_search`` results) or an assistant paraphrase of one —
    such spans are quote-ineligible: memory citing itself is not evidence.

    Documented Phase-2 seam: caller marking is replaced by mechanical
    span tainting — the WAL writer records taint spans per turn (n-gram
    overlap with the injected text) and this predicate consults the stored
    span tags instead of trusting the caller. The signature is stable so
    grounding call sites do not change.
    """
    return bool(ref.get("tainted"))


def serialize_evidence_ref(ref: Dict[str, Any]) -> str:
    """Compact string form stored in note frontmatter ``evidence`` lists."""
    rtype = ref.get("type")
    if rtype == "episode":
        return f"episode:{ref.get('uuid', '')}"
    quote = str(ref.get("quote") or "").strip()
    suffix = f" :: {quote[:160]}" if quote else ""
    if rtype == "wal":
        return f"wal:{ref.get('session_id', '')}:{ref.get('entry_id', '')}{suffix}"
    if rtype == "l0":
        return (
            f"l0:{ref.get('month', '')}:{ref.get('wal_entry_id') or '-'}{suffix}"
        )
    return f"unknown:{rtype}"


class _Proposal:
    """A propose() result awaiting its confirm() — binds the neighbor
    snapshot, the scrubbed content, and the validated evidence refs."""

    __slots__ = (
        "token", "content", "content_sha", "kind", "evidence_refs",
        "neighbor_refs", "session_id", "origin", "caller", "created_ts",
        "expires_ts",
    )

    def __init__(
        self,
        *,
        content: str,
        kind: str,
        evidence_refs: List[Dict[str, Any]],
        neighbor_refs: List[str],
        session_id: str,
        origin: str,
        caller: str,
    ):
        self.token = _uuid.uuid4().hex
        self.content = content
        self.content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        self.kind = kind
        self.evidence_refs = evidence_refs
        self.neighbor_refs = neighbor_refs
        self.session_id = session_id
        self.origin = origin
        self.caller = caller
        self.created_ts = time.time()
        self.expires_ts = self.created_ts + PROPOSAL_TTL_S


class MemoryWritePipeline:
    """ADR-004 §③ steps 0–6 for the notes tier (Phase 1 implementation)."""

    def __init__(
        self,
        store: Optional[NotesStore] = None,
        *,
        hermes_home: Optional[Path] = None,
    ):
        if hermes_home is not None:
            home = Path(hermes_home)
        else:
            from hermes_constants import get_hermes_home

            home = get_hermes_home()
        self._store = store or NotesStore(
            home / "notes", max_entries=notes_max_entries()
        )
        self._wal_dir = home / "state" / "memory-pending"
        self._mirror_dir = home / "memory" / "l0-mirror"
        self._ledger_path = home / "state" / "memory-notes-ledger.jsonl"
        self._proposals: Dict[str, _Proposal] = {}
        self._proposals_lock = threading.Lock()

    @property
    def store(self) -> NotesStore:
        return self._store

    # -- step 6: ledger (fail-open telemetry) ----------------------------------

    def _ledger(self, record: Dict[str, Any]) -> None:
        try:
            record = {"ts": round(time.time(), 3), **record}
            _append_jsonl(self._ledger_path, record)
        except Exception:
            logger.debug("notes ledger append failed (fail-open)", exc_info=True)

    # -- steps 0-3: propose ----------------------------------------------------

    def propose(
        self,
        content: str,
        *,
        kind_hint: str,
        evidence_refs: Optional[List[Dict[str, Any]]] = None,
        origin: str = "user",
        session_id: str = "",
        caller: str = "agent",
        topic_key_hint: str = "",
    ) -> Dict[str, Any]:
        """Steps 0–3. On success returns the neighbor list plus a TTL'd
        token; the caller must decide the verdict and call :meth:`confirm`
        with that token — there is no single-step write path."""
        evidence_refs = list(evidence_refs or [])

        # Step 0 — deterministic scrub + injection-pattern hard reject.
        content = (content or "").strip()
        if not content:
            return self._reject("propose", "empty content", caller=caller)
        from tools.threat_patterns import first_threat_message

        threat = first_threat_message(content, scope="strict")
        if threat:
            return self._reject("propose", threat, caller=caller, check="injection")
        content = _scrub(content)

        # Step 1 — origin-taint (Phase-1 interface; see evidence_ref_is_tainted).
        tainted = [r for r in evidence_refs if evidence_ref_is_tainted(r)]
        if tainted:
            return self._reject(
                "propose",
                "evidence refs marked tainted=memory-derived are "
                "quote-ineligible (ADR-004 §① echo-chamber rule): memory "
                "citing injected memory is not corroboration. Cite the "
                "user/tool span the fact actually came from.",
                caller=caller,
                check="origin-taint",
            )

        # Evidence must be present and format-valid before a token is issued
        # (fail fast; existence/quote grounding runs again at confirm).
        if not evidence_refs:
            return self._reject(
                "propose",
                "evidence_refs is required (>=1): every note write must cite "
                "an episode UUID or a local journal record.",
                caller=caller,
                check="grounding",
            )
        for ref in evidence_refs:
            err = self._ref_format_error(ref)
            if err:
                return self._reject("propose", err, caller=caller, check="grounding")

        # Step 2 — kind routing. Only declarative kinds proceed to notes.
        kind = (kind_hint or "").strip().lower()
        if kind == KIND_INSTRUCTION:
            return self._reject(
                "propose",
                "instruction-tier content routes to the memory tool "
                "(MEMORY.md/USER.md, existing skill-gate path) — not notes.",
                caller=caller,
                check="kind-routing",
                reroute="memory",
            )
        if kind == KIND_PROCEDURAL:
            return self._reject(
                "propose",
                "procedural content is rejected here: skills are gated "
                "separately (skill write gate). Notes hold declarative gist "
                "only.",
                caller=caller,
                check="kind-routing",
            )
        if kind == KIND_EVIDENCE:
            return self._reject(
                "propose",
                "raw evidence flows through the existing per-turn graph "
                "ingest path (it already journals to the WAL/L0-mirror); "
                "use memory_propose to flag it for curation instead.",
                caller=caller,
                check="kind-routing",
                reroute="graph-ingest",
            )
        if kind not in NOTE_KINDS:
            return self._reject(
                "propose",
                f"unknown kind {kind_hint!r}: declarative notes take one of "
                f"{sorted(NOTE_KINDS)}; 'instruction' routes to the memory "
                f"tool, 'procedural' to skills (gated), 'evidence' to graph "
                f"ingest.",
                caller=caller,
                check="kind-routing",
            )

        # Step 3 — neighbor retrieval (deterministic, ADR-004 §⑨-12).
        terms = [t for t in re.split(r"[^0-9A-Za-z가-힣_.-]+", content) if len(t) >= 3]
        neighbors = self._store.neighbor_search(
            terms[:16], topic_key=topic_key_hint or None, limit=5
        )
        neighbor_refs = [note_ref(n["kind"], n["topic_key"]) for n in neighbors]

        proposal = _Proposal(
            content=content,
            kind=kind,
            evidence_refs=evidence_refs,
            neighbor_refs=neighbor_refs,
            session_id=session_id or "",
            origin=origin,
            caller=caller,
        )
        with self._proposals_lock:
            self._prune_expired_locked()
            self._proposals[proposal.token] = proposal

        self._ledger({
            "event": "propose",
            "token": proposal.token,
            "kind": kind,
            "caller": caller,
            "origin": origin,
            "session_id": session_id or "",
            "content_sha": proposal.content_sha,
            "neighbors": neighbor_refs,
        })
        return {
            "success": True,
            "step": "propose",
            "token": proposal.token,
            "token_ttl_seconds": PROPOSAL_TTL_S,
            "kind": kind,
            "neighbors": [
                {
                    "ref": note_ref(n["kind"], n["topic_key"]),
                    "status": n.get("status"),
                    "confidence": n.get("confidence"),
                    "valid_from": n.get("valid_from"),
                    "gist": n.get("body_preview", ""),
                    "match_score": n.get("match_score"),
                }
                for n in neighbors
            ],
            "instructions": (
                "Decide the verdict against these neighbors, then call "
                "notes_write(step='confirm', token=..., verdict=...). "
                "NOOP is the expected most-frequent verdict — if a neighbor "
                "already covers this fact, confirm NOOP. Use UPDATE/SUPERSEDE "
                "on a listed neighbor instead of ADD when the topic matches."
            ),
        }

    # -- steps 4-6: confirm ------------------------------------------------------

    def confirm(
        self,
        token: str,
        verdict: str,
        *,
        topic_key: str = "",
        kind: str = "",
        target: str = "",
        session_id: str = "",
        caller: str = "agent",
    ) -> Dict[str, Any]:
        """Steps 4–6. ``target`` (\"kind/topic_key\") names the neighbor an
        UPDATE/SUPERSEDE acts on and MUST come from the token's neighbor
        snapshot. Returns the written note metadata (or the NOOP record)."""
        verdict = (verdict or "").strip().upper()
        if verdict not in VERDICTS:
            return self._reject(
                "confirm", f"unknown verdict {verdict!r}: use one of {list(VERDICTS)}.",
                caller=caller,
            )

        with self._proposals_lock:
            self._prune_expired_locked()
            proposal = self._proposals.get(token or "")
            if proposal is not None and verdict != "NOOP":
                # One write per token: pop on any mutating verdict attempt.
                self._proposals.pop(token, None)
        if proposal is None:
            return self._reject(
                "confirm",
                "unknown or expired token — call notes_write(step='propose') "
                "again; the neighbor snapshot a verdict is based on must be "
                f"fresh (TTL {PROPOSAL_TTL_S}s).",
                caller=caller,
                check="token",
            )
        if (session_id or "") != proposal.session_id:
            return self._reject(
                "confirm",
                "token was issued to a different session; propose again from "
                "this session.",
                caller=caller,
                check="token",
            )

        if verdict == "NOOP":
            with self._proposals_lock:
                self._proposals.pop(token, None)
            self._ledger({
                "event": "confirm",
                "verdict": "NOOP",
                "token": token,
                "kind": proposal.kind,
                "caller": caller,
                "origin": proposal.origin,
                "session_id": proposal.session_id,
                "content_sha": proposal.content_sha,
                "checks": {"grounding": "skipped (no write)"},
            })
            return {
                "success": True,
                "step": "confirm",
                "verdict": "NOOP",
                "message": "No write performed (fact already covered). "
                           "NOOP discipline is healthy — this is the "
                           "expected most-frequent outcome.",
            }

        # Step 5 — grounded admission (mechanical, zero LLM calls).
        grounding: List[Dict[str, Any]] = []
        for ref in proposal.evidence_refs:
            check = self._ground_ref(ref)
            grounding.append(check)
            if not check["ok"]:
                self._ledger({
                    "event": "confirm",
                    "verdict": verdict,
                    "token": token,
                    "result": "rejected",
                    "reason": check["detail"],
                    "caller": caller,
                    "session_id": proposal.session_id,
                    "checks": {"grounding": grounding},
                })
                return {
                    "success": False,
                    "step": "confirm",
                    "error": f"grounding failed: {check['detail']}",
                    "grounding": grounding,
                }
        evidence_strs = [serialize_evidence_ref(r) for r in proposal.evidence_refs]

        # Step 4 verdict application (the caller decided; we enforce the
        # neighbor-snapshot contract).
        write_kind = (kind or proposal.kind).strip().lower()
        try:
            if verdict == "ADD":
                if not topic_key:
                    return self._reject(
                        "confirm", "ADD requires topic_key.", caller=caller
                    )
                # One-shot kinds land unconfirmed until corroborated
                # (ADR-004 §①-5b: visible, lower confidence — not hidden).
                status = (
                    "unconfirmed"
                    if write_kind in ("decision", "incident")
                    and proposal.origin != "user"
                    else "active"
                )
                note = self._store.create(
                    write_kind,
                    topic_key,
                    proposal.content,
                    evidence=evidence_strs,
                    origin=proposal.origin,
                    status=status,
                )
            elif verdict in ("UPDATE", "SUPERSEDE"):
                target = (target or "").strip()
                if target not in proposal.neighbor_refs:
                    return self._reject(
                        "confirm",
                        f"{verdict} target {target!r} is not in the token's "
                        f"neighbor snapshot {proposal.neighbor_refs} — verdicts "
                        f"only act on neighbors the caller was shown.",
                        caller=caller,
                        check="neighbor-binding",
                    )
                t_kind, _, t_key = target.partition("/")
                if verdict == "UPDATE":
                    note = self._store.update(
                        t_kind,
                        t_key,
                        body=proposal.content,
                        evidence_add=evidence_strs,
                    )
                else:
                    note = self._store.supersede(
                        t_kind,
                        t_key,
                        body=proposal.content,
                        evidence=evidence_strs,
                        origin=proposal.origin,
                        new_kind=write_kind if kind else None,
                        new_topic_key=topic_key or None,
                    )
            else:  # pragma: no cover - VERDICTS guard above
                raise NoteValidationError(f"unhandled verdict {verdict}")
        except (NoteValidationError, NoteNotFoundError) as e:
            self._ledger({
                "event": "confirm",
                "verdict": verdict,
                "token": token,
                "result": "rejected",
                "reason": str(e),
                "caller": caller,
                "session_id": proposal.session_id,
                "checks": {"grounding": grounding},
            })
            return {"success": False, "step": "confirm", "error": str(e)}

        self._ledger({
            "event": "confirm",
            "verdict": verdict,
            "token": token,
            "result": "written",
            "kind": note["kind"],
            "topic_key": note["topic_key"],
            "caller": caller,
            "origin": proposal.origin,
            "session_id": proposal.session_id,
            "content_sha": proposal.content_sha,
            "checks": {"grounding": grounding},
        })
        return {
            "success": True,
            "step": "confirm",
            "verdict": verdict,
            "note": {
                "ref": note_ref(note["kind"], note["topic_key"]),
                "path": note["path"],
                "status": note["status"],
                "confidence": note["confidence"],
            },
            "message": "Note written. This update is complete — do not repeat it.",
        }

    # -- grounding helpers -------------------------------------------------------

    @staticmethod
    def _ref_format_error(ref: Any) -> Optional[str]:
        if not isinstance(ref, dict):
            return f"evidence ref must be an object, got {type(ref).__name__}"
        rtype = ref.get("type")
        if rtype == "episode":
            if not _EPISODE_UUID_RE.match(str(ref.get("uuid") or "")):
                return f"episode ref has invalid uuid {ref.get('uuid')!r}"
            return None
        if rtype == "wal":
            if not ref.get("session_id") or not ref.get("entry_id"):
                return "wal ref requires session_id and entry_id"
            if not str(ref.get("quote") or "").strip():
                return "wal ref requires a verbatim quote"
            return None
        if rtype == "l0":
            if not _MONTH_RE.match(str(ref.get("month") or "")):
                return f"l0 ref has invalid month {ref.get('month')!r} (YYYY-MM)"
            if not str(ref.get("quote") or "").strip():
                return "l0 ref requires a verbatim quote"
            return None
        return (
            f"unknown evidence ref type {rtype!r}: use 'episode', 'wal', or 'l0'"
        )

    def _ground_ref(self, ref: Dict[str, Any]) -> Dict[str, Any]:
        """Step 5 for one ref. Mechanical: existence + quote substring match
        against the local journals where possible; episode UUIDs are
        format-only (their existence lives graph-side)."""
        err = self._ref_format_error(ref)
        if err:
            return {"ref": serialize_evidence_ref(ref) if isinstance(ref, dict) else "?",
                    "ok": False, "checked": "format", "detail": err}
        rtype = ref["type"]
        if rtype == "episode":
            return {
                "ref": serialize_evidence_ref(ref),
                "ok": True,
                "checked": "format-only",
                "detail": "episode uuid format valid (graph-side existence "
                          "is checked by ingest/dream)",
            }
        # Quotes are matched against scrubbed journal content, so scrub the
        # quote the same way first — a quote containing a secret can never
        # match (and must not).
        quote = _scrub(str(ref.get("quote") or "").strip())
        if rtype == "wal":
            path = self._wal_dir / _safe_session_filename(str(ref["session_id"]))
            if not path.exists():
                return {"ref": serialize_evidence_ref(ref), "ok": False,
                        "checked": "wal", "detail": f"no WAL file for session "
                        f"{ref['session_id']!r}"}
            try:
                for rec in _iter_jsonl_records(path):
                    if rec.get("id") != ref["entry_id"]:
                        continue
                    if rec.get("type") == "turn":
                        haystacks = [
                            str(m.get("content") or "")
                            for m in rec.get("records") or []
                        ]
                    else:
                        haystacks = [str(rec.get("content") or "")]
                    if any(quote in h for h in haystacks):
                        return {"ref": serialize_evidence_ref(ref), "ok": True,
                                "checked": "wal-quote", "detail": "quote matched"}
                    return {"ref": serialize_evidence_ref(ref), "ok": False,
                            "checked": "wal-quote",
                            "detail": "quote is not a substring of the WAL "
                                      "record — citations must be verbatim"}
                return {"ref": serialize_evidence_ref(ref), "ok": False,
                        "checked": "wal",
                        "detail": f"WAL entry {ref['entry_id']!r} not found"}
            except Exception as e:
                # Fail-open would admit ungrounded writes; grounding fails CLOSED.
                return {"ref": serialize_evidence_ref(ref), "ok": False,
                        "checked": "wal", "detail": f"WAL read failed: {e}"}
        # rtype == "l0"
        path = self._mirror_dir / f"{ref['month']}.jsonl"
        if not path.exists():
            return {"ref": serialize_evidence_ref(ref), "ok": False,
                    "checked": "l0", "detail": f"no L0-mirror file for "
                    f"{ref['month']}"}
        try:
            want_entry = ref.get("wal_entry_id")
            for rec in _iter_jsonl_records(path):
                if want_entry and rec.get("wal_entry_id") != want_entry:
                    continue
                body = rec.get("body") or {}
                haystacks = [str(v) for v in body.values()] if isinstance(body, dict) else [str(body)]
                if any(quote in h for h in haystacks):
                    return {"ref": serialize_evidence_ref(ref), "ok": True,
                            "checked": "l0-quote", "detail": "quote matched"}
            return {"ref": serialize_evidence_ref(ref), "ok": False,
                    "checked": "l0-quote",
                    "detail": "quote is not a substring of any matching "
                              "L0-mirror record — citations must be verbatim"}
        except Exception as e:
            return {"ref": serialize_evidence_ref(ref), "ok": False,
                    "checked": "l0", "detail": f"L0-mirror read failed: {e}"}

    # -- misc ---------------------------------------------------------------------

    def _prune_expired_locked(self) -> None:
        now = time.time()
        for tok in [t for t, p in self._proposals.items() if p.expires_ts < now]:
            self._proposals.pop(tok, None)

    def _reject(
        self,
        step: str,
        reason: str,
        *,
        caller: str,
        check: str = "validation",
        reroute: str = "",
    ) -> Dict[str, Any]:
        self._ledger({
            "event": step,
            "result": "rejected",
            "reason": reason,
            "check": check,
            "caller": caller,
        })
        out = {"success": False, "step": step, "error": reason}
        if reroute:
            out["reroute"] = reroute
        return out


# ---------------------------------------------------------------------------
# Backfill seam (§③ step 7) — typed idempotent episode for every notes write.
# Gated OFF by default; see notes_backfill_enabled().
# ---------------------------------------------------------------------------

def maybe_enqueue_note_backfill(
    manager: Any,
    note: Dict[str, Any],
    *,
    session_id: str = "",
) -> bool:
    """Enqueue the typed episode backfill for a notes create/update.

    Inert unless BOTH a MemoryManager is supplied (the caller passes the
    session's manager only when its ingest path is allowed) AND
    ``memory.notes_backfill_enabled`` is true. Returns True only when the
    backfill was actually submitted. Fail-open: never raises.
    """
    if manager is None:
        return False
    try:
        if not notes_backfill_enabled():
            return False
        note_path = str(note.get("path") or "")
        ref = note_ref(str(note.get("kind")), str(note.get("topic_key")))
        content = f"[note {ref}] {note.get('body') or ''}".strip()
        manager.sync_note_backfill(
            note_path,
            content,
            session_id=session_id,
            metadata={
                "source_name": "hermes-notes",
                "source_id": note_path,
                "episode_type": "text",
                "note_ref": ref,
                "note_status": note.get("status"),
                "note_confidence": note.get("confidence"),
            },
        )
        return True
    except Exception:
        logger.debug("note backfill enqueue failed (fail-open)", exc_info=True)
        return False
