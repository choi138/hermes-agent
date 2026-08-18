# ADR-004 Phase 1.5 Implementation Notes

## Files changed

- `agent/memory_pipeline.py`
  - Runs the existing exact `_ground_ref` checks during `propose` and returns
    `grounding_preview`.
  - Searches the referenced journal record, session WAL, and current L0 mirror
    for up to three exact, substantive, taint-clean candidate excerpts.
  - Caches opaque candidate IDs on the TTL-bound proposal and applies selected
    candidates server-side during `confirm` before re-running exact grounding.
  - Defers only current-session WAL/L0 absence or quote-mismatch failures,
    persists them in `state/notes-pending-grounding.jsonl`, and lazily sweeps
    the sidecar at the start of every `propose` and `confirm`.
  - Promotes verified pending-only notes, tombstones exhausted pending-only
    notes, and drops only the failed citation from mixed-evidence notes.
  - Carries deferred records through the existing write-approval staging path.
- `agent/notes_store.py`
  - Adds targeted evidence removal for an exhausted mixed citation.
  - Allows a superseding note to be created as `unconfirmed` while its only
    evidence is pending.
- `tools/notes_tool.py`
  - Exposes `evidence_overrides` on `notes_write` confirm calls.
  - Documents recognition candidates, failed-preview behavior, and deferred
    current-turn grounding in the static tool schema.
- `tests/agent/test_memory_pipeline.py`
  - Covers recognition previews, exact candidate invariants, taint filtering,
    candidate overrides, deferred acceptance, lazy promotion, exhaustion,
    cross-session/secret/taint guardrails, and mixed-evidence survival.
- `tests/tools/test_notes_tool.py`
  - Covers the new tool-schema contract.

## Result shapes

Successful `propose` now always includes `grounding_preview`, with one item per
input evidence ref in the same order. A successful exact ref has no candidates:

```json
{
  "success": true,
  "step": "propose",
  "token": "<32-hex proposal token>",
  "token_ttl_seconds": 600,
  "kind": "preference",
  "grounding_preview": [
    {
      "ref": "wal:sess-1:a1b2c3 :: production API is in europe-west1",
      "ok": true,
      "checked": "wal-quote",
      "detail": "quote matched"
    }
  ],
  "neighbors": [],
  "instructions": "<verdict and grounding guidance>"
}
```

A failed WAL/L0 quote may add `candidates` to that preview item. Candidate
excerpts are exact scrubbed journal substrings and candidate IDs are usable
only with the proposal token that issued them:

```json
{
  "success": true,
  "step": "propose",
  "token": "<32-hex proposal token>",
  "token_ttl_seconds": 600,
  "kind": "fact",
  "grounding_preview": [
    {
      "ref": "wal:sess-1:invented#seq=3 :: paraphrased deployment region",
      "ok": false,
      "checked": "wal",
      "detail": "WAL entry 'invented#seq=3' not found",
      "candidates": [
        {
          "candidate_id": "8f31ab20",
          "source": "wal",
          "session_id": "sess-1",
          "wal_entry_id": "a1b2c3d4e5f6",
          "role": "user",
          "excerpt": "For the billing API, use europe-west1 as the primary region."
        }
      ]
    }
  ],
  "neighbors": [],
  "instructions": "<verdict and grounding guidance>"
}
```

An L0 candidate has the same fields except `month` replaces `session_id` in
the public candidate object. Its cached evidence ref still retains the source
session for exact re-grounding.

Select a candidate during confirm with a zero-based evidence index:

```json
{
  "step": "confirm",
  "token": "<proposal token>",
  "verdict": "ADD",
  "topic_key": "billing.api.region",
  "evidence_overrides": {
    "0": "8f31ab20"
  }
}
```

A clean or candidate-corrected confirm keeps the existing success shape:

```json
{
  "success": true,
  "step": "confirm",
  "verdict": "ADD",
  "note": {
    "ref": "fact/billing.api.region",
    "path": "<HERMES_HOME>/notes/fact/billing.api.region.md",
    "status": "active",
    "confidence": "supported"
  },
  "message": "Note written. This update is complete — do not repeat it."
}
```

Unknown, expired, wrong-index, or wrong-proposal candidate IDs fail closed:

```json
{
  "success": false,
  "step": "confirm",
  "error": "unknown or expired grounding candidate 'deadbeef'"
}
```

A qualifying current-turn race succeeds with a visibly pending result. The
note is `unconfirmed` when none of the caller's refs grounded independently:

```json
{
  "success": true,
  "step": "confirm",
  "verdict": "ADD",
  "note": {
    "ref": "fact/deploy.target.pool",
    "path": "<HERMES_HOME>/notes/fact/deploy.target.pool.md",
    "status": "unconfirmed",
    "confidence": "supported"
  },
  "message": "Note accepted with deferred grounding and queued for re-verification. Do not repeat it.",
  "deferred_grounding": {
    "pending_count": 1,
    "status": "pending"
  }
}
```

The corresponding sidecar line is:

```json
{
  "note_ref": "fact/deploy.target.pool",
  "evidence_ref": {
    "type": "wal",
    "session_id": "sess-1",
    "entry_id": "late-entry",
    "quote": "The current deployment target is the green production pool."
  },
  "quote": "The current deployment target is the green production pool.",
  "created_ts": 1785291000.0,
  "session_id": "sess-1",
  "attempts": 0
}
```

Deferred acceptance ledger records use `result: "accepted-deferred"` and
retain the failed grounding checks. Sweep terminal events are
`deferred-grounding-ok` and `deferred-grounding-failed`.

## Deliberate interpretations

- Candidate selection replaces the cached journal coordinates as well as the
  quote. Replacing only `quote` would leave measured invented entry IDs in
  place, causing exact re-grounding to fail even after a correct candidate was
  selected. The caller still supplies only an opaque candidate ID; all source
  fields and text come from the proposal cache and are re-grounded normally.
- L0 deferral requires an optional `session_id` on the evidence ref and it
  must equal the proposing session. Existing L0 refs without `session_id`
  continue to ground normally, but cannot enter the deferred path because an
  absent mirror record provides no safe way to distinguish a current-turn ref
  from a cross-session ref.
- Candidate excerpts are capped at 160 characters, within the requested
  80–200 target. This matches the existing serialized-evidence quote cap so
  the complete selected excerpt, not a truncated variant, is stored in note
  frontmatter.

No verbatim, admissibility, or taint check was weakened. Fuzzy matching is
used only to rank suggestions; every selected candidate passes the unchanged
exact grounding path before a write is admitted.
