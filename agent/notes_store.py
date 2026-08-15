"""NotesStore — the semantic (declarative-gist) memory layer (ADR-004 §②).

Notes are the curated compound-knowledge SoT, held under a citation
contract: every note carries ≥1 evidence reference (episode UUID or a
local-journal quote ref) and lives as a single Markdown file with YAML
frontmatter at::

    ~/.hermes/notes/{kind}/{topic_key}.md

Frontmatter schema (ADR-004 §②)::

    kind: decision|incident|preference|relationship|project|fact
    topic_key: three.dotted.words
    confidence: supported|corroborated|contested
    valid_from: ISO-8601
    superseded_by: null | "kind/topic_key"
    evidence: [ref, ...]            # ≥1 required, serialized ref strings
    origin: user|agent|curator|dream|legacy
    usage: {search_hits: n, last_hit: ts|null}
    status: active|unconfirmed|demoted|tombstoned

Body = the gist text, ≤4KB (bytes, UTF-8) enforced.

Design constraints inherited from the ADR:

* **No vectors** (§⑨-12) — neighbor search is deterministic term +
  topic-key matching over frontmatter+body, auditable by reading this
  file.
* **Interference-based forgetting** (§①-4) — nothing here deletes on a
  timer. ``supersede`` demotes the predecessor (body preserved),
  ``tombstone`` is a permanent marker, and both keep the file on disk.
* **Reconsolidation brake** (§①-6) — raising ``confidence`` requires new
  evidence in the same update; re-reading a note never strengthens it.
* **Scrub + injection scan at the store boundary** — note bodies re-enter
  prompts later (L1 index, lazy-load), so the store itself refuses
  threat-matching content and force-scrubs secrets, independent of what
  the calling pipeline already did.

This module is storage only: no LLM calls, no network, no background
threads. All mutations go through an exclusive file lock + atomic
replace, mirroring ``tools/memory_tool.py``.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from agent.redact import redact_sensitive_text
from utils import atomic_replace

# fcntl is Unix-only; degrade to in-process locking elsewhere (same posture
# as tools/memory_tool.py).
try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific
    fcntl = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema vocabulary (ADR-004 §②)
# ---------------------------------------------------------------------------

NOTE_KINDS = frozenset(
    {"decision", "incident", "preference", "relationship", "project", "fact"}
)
NOTE_CONFIDENCE = ("supported", "corroborated", "contested")
# "user" = the user stated/pinned it; "agent" = the main agent decided to
# write it mid-session (the notes_write tool surface); "curator"/"dream" =
# background writers; "legacy" = migrated seed notes. Writer provenance is
# load-bearing: §①-5b keys one-shot decision/incident status on origin.
NOTE_ORIGINS = frozenset({"user", "agent", "curator", "dream", "legacy"})
NOTE_STATUSES = frozenset({"active", "unconfirmed", "demoted", "tombstoned"})

# Body cap: 4KB of UTF-8 bytes (ADR-004 §② budget column).
MAX_BODY_BYTES = 4096

# Notes index budget (ADR-004 §②: "index ≤200 entries"). Config
# ``memory.notes_max_entries`` is the SoT; this constant is the fallback
# default only — import it, never re-type the literal (Phase 0 lesson).
DEFAULT_NOTES_MAX_ENTRIES = 200

# topic_key: lowercase dotted words, nominally three segments
# ("three.dotted.words"), accepted range 2–4 so canonicalization has a
# little room. The charset doubles as filename safety — a valid topic_key
# can never escape the notes directory.
_TOPIC_KEY_RE = re.compile(
    r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*){1,3}$"
)

_FRONTMATTER_DELIM = "---"

# Directory (per kind) where superseded same-key predecessors are archived.
_SUPERSEDED_DIRNAME = ".superseded"


class NoteValidationError(ValueError):
    """A note write was refused (schema, cap, scrub, or threat violation)."""


class NoteNotFoundError(KeyError):
    """The referenced note does not exist."""


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def validate_topic_key(topic_key: str) -> str:
    key = (topic_key or "").strip()
    if not _TOPIC_KEY_RE.match(key):
        raise NoteValidationError(
            f"Invalid topic_key {topic_key!r}: expected lowercase dotted words "
            f"like 'nas.nfs.access' (2-4 segments of [a-z0-9_-])."
        )
    return key


def validate_kind(kind: str) -> str:
    k = (kind or "").strip()
    if k not in NOTE_KINDS:
        raise NoteValidationError(
            f"Invalid kind {kind!r}: expected one of {sorted(NOTE_KINDS)}."
        )
    return k


def note_ref(kind: str, topic_key: str) -> str:
    """Canonical 'kind/topic_key' reference string for a note."""
    return f"{kind}/{topic_key}"


# One lock per note path, module-level so every store instance in the
# process serializes on the same lock (same pattern as
# agent.memory_journal._path_locks).
_path_locks: Dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


@contextmanager
def _file_lock(path: Path):
    """In-process lock + cross-process flock for read-modify-write safety.

    Uses a sibling ``.lock`` file so the note file itself can still be
    atomically replaced via ``os.replace`` (same rationale as
    ``MemoryStore._file_lock``).
    """
    with _lock_for(path):
        if fcntl is None:
            yield
            return
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            fd.close()


def _scrub(text: str) -> str:
    """Deterministic secret scrub. A durable knowledge store is a safety
    boundary, so ``force=True`` — the user's logging redaction preference
    does not apply (same contract as agent.memory_journal)."""
    try:
        return redact_sensitive_text(text, force=True)
    except Exception:
        logger.debug("notes scrub failed; refusing content", exc_info=True)
        raise NoteValidationError("Secret scrub failed; note content refused.")


def _threat_check(text: str) -> None:
    """Reject injection/exfiltration payloads (strict scope — note bodies
    re-enter prompts via the L1 index and lazy-load reads)."""
    from tools.threat_patterns import first_threat_message

    message = first_threat_message(text, scope="strict")
    if message:
        raise NoteValidationError(message)


def _serialize(frontmatter: Dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    return f"{_FRONTMATTER_DELIM}\n{fm}\n{_FRONTMATTER_DELIM}\n{body.strip()}\n"


def _parse(raw: str) -> Tuple[Dict[str, Any], str]:
    """Parse a note file into (frontmatter, body). Raises on malformed files."""
    lines = raw.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise NoteValidationError("Note file has no frontmatter block.")
    try:
        end = lines[1:].index(_FRONTMATTER_DELIM) + 1
    except ValueError:
        raise NoteValidationError("Note frontmatter block is unterminated.")
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(fm, dict):
        raise NoteValidationError("Note frontmatter is not a mapping.")
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".note_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class NotesStore:
    """File-backed CRUD + deterministic neighbor search over notes/."""

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        *,
        max_entries: int = DEFAULT_NOTES_MAX_ENTRIES,
    ):
        # Eager home resolution on the constructing thread — same rationale
        # as PendingTurnWAL.__init__ (a queued write must not chase a changed
        # HERMES_HOME / profile context).
        if base_dir is not None:
            self._base_dir = Path(base_dir)
        else:
            from hermes_constants import get_hermes_home
            self._base_dir = get_hermes_home() / "notes"
        self._max_entries = max(1, int(max_entries))

    # -- paths ----------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def path_for(self, kind: str, topic_key: str) -> Path:
        kind = validate_kind(kind)
        topic_key = validate_topic_key(topic_key)
        return self._base_dir / kind / f"{topic_key}.md"

    @property
    def _index_guard(self) -> Path:
        """Store-wide lock sentinel serializing cap accounting: concurrent
        creates of DIFFERENT topic_keys share no per-note lock, so without
        this two racing ADDs could both pass ``_count_canonical`` and push
        the index past its cap. Lock order (deadlock-free, fixed): index
        guard FIRST, then per-note locks."""
        return self._base_dir / ".index-guard"

    # -- validation -----------------------------------------------------------

    @staticmethod
    def _validate_evidence(evidence: Sequence[str]) -> List[str]:
        refs = [str(e).strip() for e in (evidence or []) if str(e).strip()]
        if not refs:
            raise NoteValidationError(
                "evidence is required: every note carries >=1 evidence "
                "reference (episode UUID or journal quote ref)."
            )
        return list(dict.fromkeys(refs))

    @staticmethod
    def _validate_body(body: str) -> str:
        body = (body or "").strip()
        if not body:
            raise NoteValidationError("Note body cannot be empty.")
        _threat_check(body)
        body = _scrub(body)
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise NoteValidationError(
                f"Note body is {len(body.encode('utf-8'))} bytes; the cap is "
                f"{MAX_BODY_BYTES} bytes (ADR-004 §②). Split or condense the gist."
            )
        return body

    def _count_canonical(self) -> int:
        """Count canonical (non-archived, non-tombstoned) notes for the cap."""
        count = 0
        for meta in self.list_notes():
            if meta["status"] != "tombstoned":
                count += 1
        return count

    # -- reads ----------------------------------------------------------------

    def read(self, kind: str, topic_key: str) -> Dict[str, Any]:
        path = self.path_for(kind, topic_key)
        if not path.exists():
            raise NoteNotFoundError(note_ref(kind, topic_key))
        fm, body = _parse(path.read_text(encoding="utf-8"))
        fm["body"] = body
        fm["path"] = str(path)
        return fm

    def exists(self, kind: str, topic_key: str) -> bool:
        try:
            return self.path_for(kind, topic_key).exists()
        except NoteValidationError:
            return False

    def list_notes(
        self,
        kind: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Frontmatter-only listing of canonical notes (archived superseded
        versions under ``.superseded/`` are excluded)."""
        kinds = [validate_kind(kind)] if kind else sorted(NOTE_KINDS)
        out: List[Dict[str, Any]] = []
        for k in kinds:
            kind_dir = self._base_dir / k
            if not kind_dir.is_dir():
                continue
            for path in sorted(kind_dir.glob("*.md")):
                try:
                    fm, body = _parse(path.read_text(encoding="utf-8"))
                except Exception:
                    logger.debug("Skipping malformed note %s", path, exc_info=True)
                    continue
                if status and fm.get("status") != status:
                    continue
                fm["path"] = str(path)
                fm["body_preview"] = body.splitlines()[0][:120] if body else ""
                out.append(fm)
        return out

    def list_superseded(self, kind: str, topic_key: str) -> List[Dict[str, Any]]:
        """Archived same-key predecessors, oldest first."""
        kind = validate_kind(kind)
        topic_key = validate_topic_key(topic_key)
        arch_dir = self._base_dir / kind / _SUPERSEDED_DIRNAME
        out: List[Dict[str, Any]] = []
        for path in sorted(arch_dir.glob(f"{topic_key}.*.md")):
            try:
                fm, body = _parse(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            fm["body"] = body
            fm["path"] = str(path)
            out.append(fm)
        return out

    # -- writes ---------------------------------------------------------------

    def create(
        self,
        kind: str,
        topic_key: str,
        body: str,
        *,
        evidence: Sequence[str],
        origin: str,
        confidence: str = "supported",
        status: str = "active",
        valid_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = self.path_for(kind, topic_key)
        if origin not in NOTE_ORIGINS:
            raise NoteValidationError(
                f"Invalid origin {origin!r}: expected one of {sorted(NOTE_ORIGINS)}."
            )
        if confidence not in NOTE_CONFIDENCE:
            raise NoteValidationError(
                f"Invalid confidence {confidence!r}: expected one of {list(NOTE_CONFIDENCE)}."
            )
        if status not in NOTE_STATUSES:
            raise NoteValidationError(
                f"Invalid status {status!r}: expected one of {sorted(NOTE_STATUSES)}."
            )
        body = self._validate_body(body)
        evidence_refs = self._validate_evidence(evidence)
        # Index guard before the note lock: cap accounting must be serialized
        # store-wide (two concurrent ADDs of different keys hold disjoint
        # per-note locks and would otherwise both pass the count check).
        with _file_lock(self._index_guard), _file_lock(path):
            if path.exists():
                raise NoteValidationError(
                    f"Note {note_ref(kind, topic_key)} already exists — use "
                    f"UPDATE or SUPERSEDE, not ADD."
                )
            if self._count_canonical() >= self._max_entries:
                raise NoteValidationError(
                    f"Notes index is at its cap ({self._max_entries} entries, "
                    f"ADR-004 §②). Merge, supersede, or tombstone before adding."
                )
            frontmatter = {
                "kind": validate_kind(kind),
                "topic_key": validate_topic_key(topic_key),
                "confidence": confidence,
                "valid_from": valid_from or _utcnow_iso(),
                "superseded_by": None,
                "evidence": evidence_refs,
                "origin": origin,
                "usage": {"search_hits": 0, "last_hit": None},
                "status": status,
            }
            _atomic_write(path, _serialize(frontmatter, body))
        return self.read(kind, topic_key)

    def update(
        self,
        kind: str,
        topic_key: str,
        *,
        body: Optional[str] = None,
        evidence_add: Sequence[str] = (),
        confidence: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reconsolidation-style UPDATE: merge new gist/evidence into an
        existing note.

        Brake (ADR-004 §①-6): raising confidence requires new evidence in the
        same call — a note is never strengthened by mere re-processing.
        """
        path = self.path_for(kind, topic_key)
        new_evidence = [str(e).strip() for e in (evidence_add or []) if str(e).strip()]
        with _file_lock(path):
            note = self.read(kind, topic_key)
            if note["status"] == "tombstoned":
                raise NoteValidationError(
                    f"Note {note_ref(kind, topic_key)} is tombstoned; tombstones "
                    f"are permanent."
                )
            if note.get("superseded_by"):
                raise NoteValidationError(
                    f"Note {note_ref(kind, topic_key)} was superseded by "
                    f"{note['superseded_by']} — update the successor instead."
                )
            if confidence is not None:
                if confidence not in NOTE_CONFIDENCE:
                    raise NoteValidationError(
                        f"Invalid confidence {confidence!r}: expected one of "
                        f"{list(NOTE_CONFIDENCE)}."
                    )
                old_rank = NOTE_CONFIDENCE.index(note.get("confidence", "supported"))
                # "contested" is a conflict flag, not a strength rank — it can
                # always be applied. Moving supported→corroborated is the
                # strengthening step that demands fresh evidence.
                if (
                    confidence == "corroborated"
                    and old_rank <= NOTE_CONFIDENCE.index("supported")
                    and not new_evidence
                ):
                    raise NoteValidationError(
                        "Raising confidence requires new evidence in the same "
                        "update (reconsolidation brake, ADR-004 §①-6)."
                    )
            if status is not None and status not in NOTE_STATUSES:
                raise NoteValidationError(
                    f"Invalid status {status!r}: expected one of {sorted(NOTE_STATUSES)}."
                )
            new_body = self._validate_body(body) if body is not None else note["body"]
            merged_evidence = list(dict.fromkeys(list(note["evidence"]) + new_evidence))
            frontmatter = {
                "kind": note["kind"],
                "topic_key": note["topic_key"],
                "confidence": confidence or note["confidence"],
                "valid_from": note["valid_from"],
                "superseded_by": note.get("superseded_by"),
                "evidence": merged_evidence,
                "origin": note["origin"],
                "usage": note.get("usage") or {"search_hits": 0, "last_hit": None},
                "status": status or note["status"],
            }
            _atomic_write(path, _serialize(frontmatter, new_body))
        return self.read(kind, topic_key)

    def supersede(
        self,
        kind: str,
        topic_key: str,
        *,
        body: str,
        evidence: Sequence[str],
        origin: str,
        new_kind: Optional[str] = None,
        new_topic_key: Optional[str] = None,
        confidence: str = "supported",
    ) -> Dict[str, Any]:
        """Replace a note with a successor, preserving the predecessor.

        Same-key supersede archives the predecessor under
        ``{kind}/.superseded/{topic_key}.{n}.md``; different-key supersede
        leaves the predecessor at its path. Either way the predecessor gets
        ``superseded_by`` set and ``status: demoted`` (body preserved —
        forgetting is interference, not deletion, ADR-004 §①-4).

        Ordering is non-destructive-first: the successor is fully validated
        (body, evidence, cap, target availability) BEFORE the predecessor is
        touched, and on the cross-key path the successor file is written
        before the predecessor is demoted — a failure or crash at any point
        can duplicate a note (successor written, predecessor not yet
        demoted) but can never orphan the canonical fact.
        """
        succ_kind = validate_kind(new_kind or kind)
        succ_key = validate_topic_key(new_topic_key or topic_key)
        old_path = self.path_for(kind, topic_key)
        same_key = succ_kind == validate_kind(kind) and succ_key == validate_topic_key(topic_key)

        # Validate ALL successor inputs before any predecessor mutation.
        if origin not in NOTE_ORIGINS:
            raise NoteValidationError(
                f"Invalid origin {origin!r}: expected one of {sorted(NOTE_ORIGINS)}."
            )
        if confidence not in NOTE_CONFIDENCE:
            raise NoteValidationError(
                f"Invalid confidence {confidence!r}: expected one of "
                f"{list(NOTE_CONFIDENCE)}."
            )
        new_body = self._validate_body(body)
        new_evidence = self._validate_evidence(evidence)
        successor_ref = note_ref(succ_kind, succ_key)
        succ_frontmatter = {
            "kind": succ_kind,
            "topic_key": succ_key,
            "confidence": confidence,
            "valid_from": _utcnow_iso(),
            "superseded_by": None,
            "evidence": new_evidence,
            "origin": origin,
            "usage": {"search_hits": 0, "last_hit": None},
            "status": "active",
        }
        succ_serialized = _serialize(succ_frontmatter, new_body)

        def _demoted_predecessor(old: Dict[str, Any]) -> str:
            fm = {
                "kind": old["kind"],
                "topic_key": old["topic_key"],
                "confidence": old["confidence"],
                "valid_from": old["valid_from"],
                "superseded_by": successor_ref,
                "evidence": old["evidence"],
                "origin": old["origin"],
                "usage": old.get("usage") or {"search_hits": 0, "last_hit": None},
                "status": "demoted",
            }
            return _serialize(fm, old["body"])

        def _check_old(old: Dict[str, Any]) -> None:
            if old["status"] == "tombstoned":
                raise NoteValidationError(
                    f"Note {note_ref(kind, topic_key)} is tombstoned; tombstones "
                    f"are permanent."
                )
            if old.get("superseded_by"):
                raise NoteValidationError(
                    f"Note {note_ref(kind, topic_key)} was already superseded by "
                    f"{old['superseded_by']}."
                )

        if same_key:
            # Successor replaces the predecessor at the same path — the index
            # count is unchanged, so no cap check and no index guard needed.
            with _file_lock(old_path):
                old = self.read(kind, topic_key)
                _check_old(old)
                arch_dir = old_path.parent / _SUPERSEDED_DIRNAME
                n = len(list(arch_dir.glob(f"{old['topic_key']}.*.md"))) + 1 \
                    if arch_dir.is_dir() else 1
                arch_path = arch_dir / f"{old['topic_key']}.{n}.md"
                # Archive a demoted COPY first (non-destructive), then swap
                # the successor in atomically — the canonical path is never
                # missing and never holds a half-written file.
                _atomic_write(arch_path, _demoted_predecessor(old))
                _atomic_write(old_path, succ_serialized)
            return self.read(succ_kind, succ_key)

        # Cross-key: the demoted predecessor stays canonical, so the index
        # grows by one — cap accounting under the store-wide guard (same
        # lock order as create: index guard, then note locks in sorted order
        # so racing supersedes can't deadlock).
        new_path = self.path_for(succ_kind, succ_key)
        first, second = sorted((old_path, new_path), key=str)
        with _file_lock(self._index_guard), _file_lock(first), _file_lock(second):
            old = self.read(kind, topic_key)
            _check_old(old)
            if new_path.exists():
                raise NoteValidationError(
                    f"Note {successor_ref} already exists — supersede cannot "
                    f"overwrite an existing successor; UPDATE it instead."
                )
            if self._count_canonical() >= self._max_entries:
                raise NoteValidationError(
                    f"Notes index is at its cap ({self._max_entries} entries, "
                    f"ADR-004 §②): a cross-key supersede keeps the demoted "
                    f"predecessor canonical, so it needs a free slot. Merge, "
                    f"supersede same-key, or tombstone before re-keying."
                )
            # Successor first, demotion second: a crash between the two
            # leaves both notes readable (duplicate), never neither.
            _atomic_write(new_path, succ_serialized)
            _atomic_write(old_path, _demoted_predecessor(old))
        return self.read(succ_kind, succ_key)

    def tombstone(self, kind: str, topic_key: str) -> Dict[str, Any]:
        """Permanent removal marker. Body is preserved for audit; the note
        leaves every read surface (list/search filter it out)."""
        return self.update(kind, topic_key, status="tombstoned")

    def bump_usage(
        self, kind: str, topic_key: str, *, hits: int = 1, ts: Optional[str] = None
    ) -> None:
        """Record read-side retrieval hits (filled by the retrieval ledger;
        promotion signals — NOT bumped by pipeline neighbor checks)."""
        path = self.path_for(kind, topic_key)
        try:
            with _file_lock(path):
                note = self.read(kind, topic_key)
                usage = note.get("usage") or {}
                usage["search_hits"] = int(usage.get("search_hits") or 0) + int(hits)
                usage["last_hit"] = ts or _utcnow_iso()
                frontmatter = {
                    k: note[k]
                    for k in (
                        "kind", "topic_key", "confidence", "valid_from",
                        "superseded_by", "evidence", "origin",
                    )
                }
                frontmatter["usage"] = usage
                frontmatter["status"] = note["status"]
                _atomic_write(path, _serialize(frontmatter, note["body"]))
        except Exception:
            # Usage accounting is telemetry — never let it break a read path.
            logger.debug("bump_usage failed (fail-open)", exc_info=True)

    # -- deterministic neighbor search (no vectors, ADR-004 §⑨-12) -------------

    def neighbor_search(
        self,
        terms: Iterable[str],
        *,
        topic_key: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Deterministic term + topic-key match over frontmatter and body.

        Scoring (fixed weights, auditable):
          +3 per shared topic_key segment with the candidate's topic_key,
          +2 per query term appearing in the candidate's topic_key,
          +1 per query term appearing in the body/frontmatter text.
        Tombstoned notes never match. Ties break on valid_from (newer first),
        then path (stable).
        """
        query_terms = [t.casefold() for t in terms if t and str(t).strip()]
        query_segments: List[str] = []
        if topic_key:
            query_segments = [s for s in str(topic_key).casefold().split(".") if s]
        scored: List[Tuple[int, str, str, Dict[str, Any]]] = []
        for meta in self.list_notes():
            if meta.get("status") == "tombstoned":
                continue
            cand_key = str(meta.get("topic_key") or "").casefold()
            cand_segments = set(cand_key.split("."))
            try:
                body = _parse(Path(meta["path"]).read_text(encoding="utf-8"))[1]
            except Exception:
                body = ""
            haystack = " ".join(
                [
                    cand_key,
                    str(meta.get("kind") or ""),
                    str(meta.get("origin") or ""),
                    str(meta.get("status") or ""),
                    str(meta.get("confidence") or ""),
                    " ".join(str(e) for e in meta.get("evidence") or []),
                    body,
                ]
            ).casefold()
            score = 0
            score += 3 * len(cand_segments.intersection(query_segments))
            for term in query_terms:
                if term in cand_key:
                    score += 2
                elif term in haystack:
                    score += 1
            if score > 0:
                scored.append(
                    (score, str(meta.get("valid_from") or ""), meta["path"], meta)
                )
        # Stable multi-pass sort: path asc, then valid_from desc (newer
        # first), then score desc — ISO timestamps sort lexicographically so
        # string reverse-sort is date-correct.
        scored.sort(key=lambda item: item[2])
        scored.sort(key=lambda item: item[1], reverse=True)
        scored.sort(key=lambda item: item[0], reverse=True)
        out = []
        for score, _, _, meta in scored[: max(1, int(limit))]:
            entry = dict(meta)
            entry["match_score"] = score
            out.append(entry)
        return out

    # -- L1 index line renderer (consumed by Phase 3 dream compile) ------------

    @staticmethod
    def render_index_line(note: Dict[str, Any], *, gist_chars: int = 55) -> str:
        """One-line index hook for a note (ADR-004 §6.1 L1 payload arithmetic:
        ~55 chars of gist per line). Phase 3's dream L1 compiler consumes
        these; Phase 1 only renders."""
        body = str(note.get("body") or note.get("body_preview") or "")
        gist = " ".join(body.split())
        if len(gist) > gist_chars:
            gist = gist[: gist_chars - 1] + "…"
        flags = []
        status = note.get("status")
        if status and status != "active":
            flags.append(str(status))
        if note.get("confidence") == "contested":
            flags.append("contested")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        return f"- {note.get('kind')}/{note.get('topic_key')}: {gist}{flag_str}"
