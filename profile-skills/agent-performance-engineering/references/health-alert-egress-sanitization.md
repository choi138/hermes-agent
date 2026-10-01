# Health-Alert Egress Sanitization (untrusted log content → chat)

Applies when an agent-health / incident-alert path formats **log-derived text**
(model errors, provider URLs, upstream messages) into a chat platform message —
Discord, Slack, Telegram. The alert body is attacker-influenced: an upstream
error string, a hostile hostname, or a delegated child's message can all reach
the formatter verbatim. Treat the formatter as an **egress boundary**, not as
internal logging.

## Threat classes to close

1. **Mention injection** — `@everyone`, `<@123>`, `<#123>` in log text pinging a
   channel from an automated alert.
2. **Markup / line forging** — backticks breaking out of the formatter's own code
   spans; newline-class characters (C0/C1, `U+2028`, `U+2029`), bidi overrides,
   ZWSP, BOM forging or reordering alert lines.
3. **Masked-link disguise** — `[label](https://evil.invalid)` rendering a
   trustworthy label over a hostile URL.
4. **Credential leak via URL userinfo** — `https://user:pass@host/v1` appearing in
   a provider error and being promoted to chat.
5. **Redactor failure** — the redaction helper raising, and the alert shipping the
   raw text anyway.

## Ordering is the whole design

Per-field pipeline that verified clean under independent security review:

```text
strip control chars
  → truncate to (emit_width × SCAN_MULTIPLIER)     # wide window, e.g. 4x
  → flatten event-owned '[' / ']' to parens
  → mask URL userinfo
  → redact secrets
  → strip control chars again
  → neutralize platform markup      # MUST be last
  → truncate to emit width
```

Non-obvious constraints found by review:

- **Neutralize markup strictly last.** The redactor itself inserts bracketed
  placeholders (`[REDACTED]`). If attacker text supplies the following `(`, a
  masked link is formed *after* redaction. The `"](" → "] ("` splitter must run
  after both redaction and any label substitution. `str.replace` on a 2-char
  pattern cannot leave a residual `](` even on overlapping input like `](](`.
- **Redact over a wider window than you emit.** Truncating first can split a
  secret so a length-anchored pattern no longer matches, and a partially masked
  token is still a leak. Scan ~4x the emitted width, then narrow.
  Corollary pitfall: if an upstream **classifier** truncates the raw log line
  (e.g. `message[:1200]`) before handing it to the formatter, that mitigation
  cannot recover what was already cut. Redact before truncating there too, or
  hand the formatter a wider slice.
- **Flatten event-owned brackets before anything else.** Then the only brackets
  reaching the markup step are ones the pipeline itself inserted, which makes the
  masked-link argument closed rather than heuristic.
- **Make the mention regex snowflake-only.** A loose `<@...>` body pattern lets
  `<@1](url)>` survive relabeling and re-form a masked link. Bound it to
  `[0-9]{1,32}`.
- **Greedy userinfo matching handles a raw `@` in the password.**
  `://[^\s/?#]{1,256}@` is greedy, so it settles on the *last* `@` in the
  authority: `http://u:pa@ss@host/v1` → `http://***:***@host/v1`. Mask **before**
  markup neutralization so an `@everyone` smuggled in a hostname is still
  relabeled. Note the bound: userinfo longer than the cap is not masked at all,
  leaving the generic redactor as the only net.
- **Downgrade backticks** (to `'`) rather than escaping them, so the formatter's
  own code spans cannot be escaped.
- **Give every event-derived line a literal prefix** and strip newline-class
  characters on both sides of redaction, so no forged line can impersonate a
  structured field.

## Fail closed on redaction failure

If the redactor raises, clearing only the free-text fields is not enough: any
**structured trace fields** (endpoint, model, provider, route, counters) must be
cleared in the same branch, and the free text replaced with safe placeholders.
Otherwise the structured-line renderer still ships the unredacted values.

Test it by `monkeypatch`ing the redaction module attribute — a function-local
import inside the redact helper resolves the patched attribute at call time,
which makes this cheap to pin.

## Bounds and other real findings

- Cap the joined message below the platform limit (e.g. 1950 < Discord's 2000)
  and cap each field independently. Order structured lines *ahead* of the
  free-text detail block so tail-trimming drops detail, not the diagnosis.
- Operator-supplied `mention_text` is the one legitimately unredacted field:
  still control-strip it, and document it as deployment config.
- Alert-budget dicts keyed by session grow unbounded in a long-lived gateway.
  Prune them alongside the hourly sliding window.
- Timestamp formatters that catch `(OverflowError, OSError, ValueError)` but not
  `TypeError` can raise out of the formatter — and the formatter is often called
  *outside* the `try` that guards `adapter.send`.
- Anchored classifier regexes (`^` without `re.MULTILINE`) only tolerate the
  current log prefix shape. Fail-closed (missed alert, no leak), but leave a
  comment or a negative test at that cliff.

## Verification recipe that worked

1. Wire the sink into the **real** lifecycle (`start`/`stop` of the gateway
   runner), not just the formatter, and pin
   `handler → bounded queue → sink → adapter` with an execution test.
2. Feed the classifier the **verbatim production strings** from the incident.
3. Write one RED test per reviewer finding before fixing, then confirm
   RED → GREEN, then re-run the full health file set.
4. Re-run the independent review after the fix; a first-pass PASS on an earlier
   revision does not carry over.

### Independent review harness (no repo access)

Pipe a self-contained review brief plus the inlined sources into a fresh model
run with tools disabled and a JSON verdict schema:

```bash
claude -p --model opus --effort max --permission-mode dontAsk \
  --tools '' --max-turns 2 \
  --output-format json --json-schema "$(cat verdict-schema.json)" \
  < review-brief.md > verdict.json
```

Build the brief by appending the sources with a shell block, and state up front
what has already been verified (test counts, compile, `git diff --check`), so the
reviewer spends its budget on exploitability instead of restating status. Read
`structured_output`, falling back to `json.loads(result)`. Grade the outcome on
`blocking == []`, and treat `security` / `nonblocking` entries as hardening
candidates rather than release gates.
