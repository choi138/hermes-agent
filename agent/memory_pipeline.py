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
        substring-match the (scrubbed) WAL / L0-mirror content. Quotes must
        be substantive (minimum effective length, not redaction-mask
        material) and must not contain secrets — a quote the scrubber would
        alter is refused outright, so secret-bearing quotes can neither
        ground nor be persisted. Episode UUIDs are format-validated (graph
        existence checks are the graph side's job).
     5b. write-approval gate — mutating verdicts respect
        ``memory.write_approval`` (the same switch that gates MEMORY.md
        writes): gate on → the fully-resolved plan is staged for
        out-of-band review and replayed token-free by
        :func:`apply_notes_pending` on approval.
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
    validate_kind,
    validate_topic_key,
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

_HANGUL_RE = re.compile(r"[가-힣]")

# Term splitter shared by neighbor retrieval and the UPDATE conflict check.
_TERM_SPLIT_RE = re.compile(r"[^0-9A-Za-z가-힣_.-]+")

# Redaction-mask shapes the scrubber emits into journals: «redacted:…»
# sentinels, bare *** placeholders, and head...tail masks (ghp_A1...Q7r8).
# A quote made of these is "grounding" against redaction artifacts, not
# against content — it must not count toward quote substance.
_MASK_TOKEN_RE = re.compile(r"«[^»]{0,120}»|\*{3,}|\S{1,16}\.\.\.\S{1,16}")

# Minimum effective quote length. Hangul characters count double (Korean
# packs roughly twice the information per character), so e.g. an 8-char
# Korean span passes while a 2-char English fragment ("is") — which
# substring-matches virtually any record — cannot ground anything.
MIN_QUOTE_EFFECTIVE_LEN = 15
# Minimum effective length remaining after mask tokens are stripped.
_MIN_QUOTE_RESIDUAL_LEN = 8


def _effective_len(text: str) -> int:
    return len(text) + len(_HANGUL_RE.findall(text))


def _quote_admissibility_error(quote: str) -> Optional[str]:
    """Substance checks for a wal/l0 evidence quote (ADR-004 §③ step 5).

    Returns an error string when the quote cannot serve as a citation:
    * it contains secret material (the scrub would alter it) — refusing at
      the door means a raw secret can never ride a quote into a proposal,
      the ledger, or note frontmatter;
    * it is too short to identify a record (trivial substrings match
      everything, which made grounding vacuous);
    * after stripping redaction-mask tokens too little content remains
      (a mask is a redaction artifact, not evidence).
    """
    normalized = " ".join(quote.split())
    if _scrub(normalized) != normalized:
        return (
            "quote contains secret material (the redactor would alter it) — "
            "citations must never carry secrets; quote the non-secret span "
            "of the record instead"
        )
    if _effective_len(normalized) < MIN_QUOTE_EFFECTIVE_LEN:
        return (
            f"quote is too short to identify a record (needs effective "
            f"length >= {MIN_QUOTE_EFFECTIVE_LEN}; Hangul counts double) — "
            f"cite a longer verbatim span"
        )
    residual = " ".join(_MASK_TOKEN_RE.sub(" ", normalized).split())
    if _effective_len(residual) < _MIN_QUOTE_RESIDUAL_LEN:
        return (
            "quote is (mostly) redaction-mask material — a redaction "
            "placeholder is not evidence; cite the surrounding non-secret "
            "text instead"
        )
    return None


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
    """Compact string form stored in note frontmatter ``evidence`` lists.

    The quote is scrubbed before serialization: frontmatter re-enters
    prompts via notes_read, so it is a safety boundary in its own right —
    even though _quote_admissibility_error already refuses secret-bearing
    quotes upstream, this serializer must be safe for ANY caller.
    """
    rtype = ref.get("type")
    if rtype == "episode":
        return f"episode:{ref.get('uuid', '')}"
    quote = _scrub(str(ref.get("quote") or "").strip())
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
        # 2-char tokens count when they contain Hangul: common Korean nouns
        # are two syllables, and dropping them starved neighbor recall on the
        # primary (Korean) corpus — the exact parallel-ADD risk §③'s
        # canonicalization eval exists to catch.
        terms = [
            t
            for t in _TERM_SPLIT_RE.split(content)
            if len(t) >= 3 or (len(t) == 2 and _HANGUL_RE.search(t))
        ]
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
        snapshot. ``kind`` may only differ from the proposed kind on
        SUPERSEDE (the sanctioned re-typing path). Mutating verdicts
        require a session-bound token (non-empty session_id at propose).
        Returns the written note metadata (or the NOOP/staged record)."""
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

        # Mutating verdicts need a real session binding: with an empty
        # session id the propose/confirm session check is vacuously true, so
        # a token could be confirmed from any empty-session context. NOOP
        # (above) stays allowed — it writes nothing.
        if not proposal.session_id:
            return self._reject(
                "confirm",
                "mutating verdicts require a session-bound token: the "
                "proposal was created without a session_id, so the "
                "propose/confirm session binding cannot be enforced. "
                "Re-propose with a session_id.",
                caller=caller,
                check="token",
            )

        # kind is bound at propose time: §①-5b status routing keys on the
        # proposed kind+origin, so re-typing a 'decision' proposal into a
        # 'fact' at confirm would dodge the unconfirmed landing. SUPERSEDE's
        # new_kind is the one sanctioned re-typing path.
        kind_arg = (kind or "").strip().lower()
        if kind_arg and kind_arg != proposal.kind and verdict != "SUPERSEDE":
            return self._reject(
                "confirm",
                f"kind is fixed at propose ({proposal.kind!r}); confirm "
                f"cannot re-type it to {kind_arg!r}. Re-propose with the "
                f"right kind, or use SUPERSEDE (the sanctioned re-typing "
                f"path).",
                caller=caller,
                check="kind-binding",
            )

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
        # neighbor-snapshot contract). First resolve the full write plan —
        # every input the store mutation needs — WITHOUT mutating anything,
        # so the write-approval gate can stage a token-free replayable
        # payload (the token TTL is far shorter than a human review cycle).
        write_kind = (kind_arg or proposal.kind)
        plan: Dict[str, Any] = {
            "tool": "notes_write",
            "verdict": verdict,
            "content": proposal.content,
            "evidence": evidence_strs,
            "origin": proposal.origin,
        }
        update_conflict = False
        if verdict == "ADD":
            if not topic_key:
                return self._reject(
                    "confirm", "ADD requires topic_key.", caller=caller
                )
            # Validate identity fields BEFORE the gate: an invalid plan must
            # fail now, not after a human approved a staged copy of it.
            try:
                validate_kind(write_kind)
                validate_topic_key(topic_key)
            except NoteValidationError as e:
                return self._reject("confirm", str(e), caller=caller)
            # One-shot kinds land unconfirmed until corroborated
            # (ADR-004 §①-5b: visible, lower confidence — not hidden).
            status = (
                "unconfirmed"
                if write_kind in ("decision", "incident")
                and proposal.origin != "user"
                else "active"
            )
            plan.update(kind=write_kind, topic_key=topic_key, status=status)
        else:  # UPDATE / SUPERSEDE (VERDICTS + NOOP handled above)
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
            plan.update(target_kind=t_kind, target_topic_key=t_key)
            if verdict == "UPDATE":
                # Conflict brake (ADR-004 §①-6): UPDATE wholesale-replaces
                # the body, so a replacement sharing NO content terms with
                # the stored gist is a contradiction candidate — it lands
                # with confidence 'contested' (a conflict flag, always
                # applicable) and is ledgered distinctly. The full
                # stored-quote contradiction check is Phase-2 curator work.
                try:
                    old_note = self._store.read(t_kind, t_key)
                    update_conflict = not self._shares_content_terms(
                        old_note.get("body") or "", proposal.content
                    )
                except (NoteNotFoundError, NoteValidationError):
                    pass  # store.update below produces the real error
                plan["update_conflict"] = update_conflict
            else:
                try:
                    if kind_arg:
                        validate_kind(write_kind)
                    if topic_key:
                        validate_topic_key(topic_key)
                except NoteValidationError as e:
                    return self._reject("confirm", str(e), caller=caller)
                plan.update(
                    new_kind=write_kind if kind_arg else None,
                    new_topic_key=topic_key or None,
                )

        # Durable-write approval gate — same subsystem flag as the memory
        # tool's MEMORY.md writes (memory.write_approval), so the notes tier
        # is never less supervised than the instruction tier beside it.
        gated = self._apply_write_gate(plan, grounding=grounding,
                                       token=token, caller=caller,
                                       proposal=proposal)
        if gated is not None:
            return gated

        try:
            note = apply_notes_plan(plan, store=self._store)
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

        ledger_record = {
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
        }
        if verdict == "UPDATE":
            # Body-replacing UPDATEs are ledgered distinctly (§①-6 audit
            # trail for the deferred contradiction check).
            ledger_record["update"] = {
                "body_replaced": True,
                "conflict_flagged": update_conflict,
            }
        self._ledger(ledger_record)
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
            quote = str(ref.get("quote") or "").strip()
            if not quote:
                return "wal ref requires a verbatim quote"
            return _quote_admissibility_error(quote)
        if rtype == "l0":
            if not _MONTH_RE.match(str(ref.get("month") or "")):
                return f"l0 ref has invalid month {ref.get('month')!r} (YYYY-MM)"
            quote = str(ref.get("quote") or "").strip()
            if not quote:
                return "l0 ref requires a verbatim quote"
            return _quote_admissibility_error(quote)
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

    # -- write-approval gate (memory.write_approval) ------------------------------

    @staticmethod
    def _shares_content_terms(old_body: str, new_body: str) -> bool:
        """True when the two bodies share at least one content term
        (deterministic: same splitter as neighbor retrieval, casefolded,
        len >= 2). Zero overlap on a body replacement = contradiction
        candidate."""
        def _terms(text: str) -> set:
            return {
                t.casefold() for t in _TERM_SPLIT_RE.split(text) if len(t) >= 2
            }
        return bool(_terms(old_body) & _terms(new_body))

    def _apply_write_gate(
        self,
        plan: Dict[str, Any],
        *,
        grounding: List[Dict[str, Any]],
        token: str,
        caller: str,
        proposal: _Proposal,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate the durable-write approval gate for a resolved plan.

        Returns None when the write should proceed; otherwise the tool
        result to return (blocked, or staged for out-of-band approval).
        Reuses the ``memory`` subsystem flag (``memory.write_approval``) —
        one switch supervises both durable memory tiers. The staged payload
        is the fully-resolved plan (scrubbed content, serialized evidence,
        grounding already passed), replayed token-free by
        :func:`apply_notes_pending` on approval. Gate-module import failure
        fails open, mirroring tools/memory_tool.py.
        """
        try:
            from tools import write_approval as wa
        except Exception:
            return None

        verdict = plan["verdict"]
        if verdict == "ADD":
            ref = note_ref(plan["kind"], plan["topic_key"])
        elif verdict == "SUPERSEDE" and (plan.get("new_kind") or plan.get("new_topic_key")):
            ref = (
                f"{plan['target_kind']}/{plan['target_topic_key']} -> "
                f"{plan.get('new_kind') or plan['target_kind']}/"
                f"{plan.get('new_topic_key') or plan['target_topic_key']}"
            )
        else:
            ref = f"{plan['target_kind']}/{plan['target_topic_key']}"
        summary = f"notes {verdict.lower()}: {ref}"
        decision = wa.evaluate_gate(
            wa.MEMORY, inline_summary=summary, inline_detail=plan["content"]
        )
        if decision.allow:
            return None

        if decision.blocked:
            self._ledger({
                "event": "confirm",
                "verdict": verdict,
                "token": token,
                "result": "blocked",
                "reason": "write denied by user (write_approval gate)",
                "caller": caller,
                "session_id": proposal.session_id,
                "checks": {"grounding": grounding},
            })
            return {"success": False, "step": "confirm",
                    "error": decision.message}

        # "action" is display-only (pending-list rendering); replay routing
        # keys on plan["tool"] in tools.memory_tool.apply_memory_pending.
        plan.setdefault("action", f"notes_{verdict.lower()}")
        record = wa.stage_write(
            wa.MEMORY, plan,
            summary=f"{summary}: {plan['content'][:120]}",
            origin=wa.current_origin(),
        )
        self._ledger({
            "event": "confirm",
            "verdict": verdict,
            "token": token,
            "result": "staged",
            "pending_id": record.get("id"),
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
            "staged": True,
            "pending_id": record.get("id"),
            "message": decision.message,
        }

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
# Resolved-plan applier — the single store-mutation point for confirm and for
# the write-approval replay path.
# ---------------------------------------------------------------------------

def apply_notes_plan(
    plan: Dict[str, Any], *, store: Optional[NotesStore] = None
) -> Dict[str, Any]:
    """Apply a fully-resolved notes write plan to the store.

    The plan is produced by :meth:`MemoryWritePipeline.confirm` AFTER scrub,
    grounding, and neighbor-binding all passed — this function only performs
    the store mutation (the store re-validates body/evidence/caps itself).
    Raises NoteValidationError / NoteNotFoundError on store refusal.
    """
    if store is None:
        store = NotesStore(max_entries=notes_max_entries())
    verdict = plan.get("verdict")
    if verdict == "ADD":
        return store.create(
            plan["kind"],
            plan["topic_key"],
            plan["content"],
            evidence=plan["evidence"],
            origin=plan["origin"],
            status=plan.get("status") or "active",
        )
    if verdict == "UPDATE":
        return store.update(
            plan["target_kind"],
            plan["target_topic_key"],
            body=plan["content"],
            evidence_add=plan["evidence"],
            confidence="contested" if plan.get("update_conflict") else None,
        )
    if verdict == "SUPERSEDE":
        return store.supersede(
            plan["target_kind"],
            plan["target_topic_key"],
            body=plan["content"],
            evidence=plan["evidence"],
            origin=plan["origin"],
            new_kind=plan.get("new_kind") or None,
            new_topic_key=plan.get("new_topic_key") or None,
        )
    raise NoteValidationError(f"unknown staged notes verdict {verdict!r}")


def apply_notes_pending(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Replay an approved staged notes write (write-approval pending store).

    Called by ``tools.memory_tool.apply_memory_pending`` when a pending
    ``memory``-subsystem record carries ``tool: notes_write``. Token-free by
    design: the staged payload is the resolved plan, and human review is the
    admission control at this point. Returns a store-style result dict.
    """
    pipeline = MemoryWritePipeline()
    try:
        note = apply_notes_plan(payload, store=pipeline.store)
    except (NoteValidationError, NoteNotFoundError) as e:
        pipeline._ledger({
            "event": "apply-pending",
            "verdict": payload.get("verdict"),
            "result": "rejected",
            "reason": str(e),
            "caller": "write-approval",
        })
        return {"success": False, "error": str(e)}
    pipeline._ledger({
        "event": "apply-pending",
        "verdict": payload.get("verdict"),
        "result": "written",
        "kind": note["kind"],
        "topic_key": note["topic_key"],
        "caller": "write-approval",
        "origin": payload.get("origin"),
    })
    return {
        "success": True,
        "note": {
            "ref": note_ref(note["kind"], note["topic_key"]),
            "path": note["path"],
            "status": note["status"],
            "confidence": note["confidence"],
        },
    }


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
