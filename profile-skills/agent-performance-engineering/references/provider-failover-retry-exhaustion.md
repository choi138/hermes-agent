# Provider failover reliability and retry-exhaustion anatomy

Evidence bank from a real Hermes incident (session `20260813_182410_bb2e02`,
2026-08-13 KST) where a turn died as `API call failed after 3 retries`. Use it
when a turn exhausts retries, when a "fallback" chain fails to rescue anything,
or when a self-hosted LLM load balancer is the suspected bottleneck.

## Incident anatomy

Observed sequence:

1. `18:28:50 KST` — primary `custom/gpt-5.6-sol` failed with upstream
   `server_error`.
2. Fallback activated: `codex-lb/gpt-5.5`.
3. Both primary and fallback resolved to the **same** `codex-lb` service on
   `:2455`. Different provider name, different model, identical failure domain.
4. Hermes then hit 150s no-response three times on a ~64k-token request, with
   retry gaps of ~2.6s and ~5.6s — each retry re-entering the same saturated
   capacity pool.
5. `codex-lb /health` returned 200 throughout. The process was alive; the
   request path was wedged.

Config at incident time: `delegation.max_concurrent_children=3`,
`agent.api_max_retries=3`, `delegation.reasoning_effort` unset (inheriting
`xhigh`), fallback chain a single same-origin entry.

The user-visible alert showed only the **final** `timeout`. The originating
`server_error` and the route transition were absent, which is what made the
incident hard to read.

## Lesson 1 — a fallback that resolves to the same origin is not a fallback

Hermes's `try_activate_fallback()` dedup skipped a candidate only when
(a) provider+model matched, or (b) an **explicitly configured** `base_url`
plus model matched. A fallback entry with no `base_url` whose resolver returns
the same endpoint, differing only in model name, passed the check.

Diagnostic move: never judge failure-domain isolation from the config's
provider/model labels. Resolve the candidate client first and compare
canonical `scheme://host:port` of the **actual** `base_url`.

Rule to apply:

```python
INFRASTRUCTURE_FAILURE_REASONS = {timeout, server_error, overloaded}
skip = reason in INFRASTRUCTURE_FAILURE_REASONS and (
    canonical_origin(current_base_url) == canonical_origin(candidate_base_url)
)
```

Scope it to infrastructure reasons only. `model_not_found` and
`content_policy_blocked` are model-level, so a different model on the *same*
endpoint is a legitimate rescue there — a blanket same-origin skip would
destroy that. Compare origin only (normalize case and trailing slash); never
log or hash full URLs with credentials or query strings.

When no independent-origin provider exists, prefer disabling the misleading
fallback over keeping it. A chain that pretends to have a fallback converts a
clean terminal failure into three amplifying retries.

## Lesson 2 — the alert must carry the whole chain, not the last hop

Preserve a bounded failure trace (cap ~8 hops, or first + last + route
transitions) and render it in one alert:

```text
first=custom/gpt-5.6-sol:server_error
route=custom/gpt-5.6-sol -> codex-lb/gpt-5.5
last=codex-lb/gpt-5.5:timeout
retries=3 msgs=23 tokens~=64432
```

Keep the existing leading `API call failed after N retries` prefix intact so
the health sink's regex (`_API_RETRIES_RE`) keeps classifying the line; append
the structured `key=value` fields after it. Cap detail length (the sink's limit
was 1950 chars) and route summaries through the existing redactor.

Capacity fields (`pending_count`, `queued_count`, `available`, `error_code`)
belong in the alert **only** when the LB actually returned them in a bounded
error payload. On a pure no-response timeout, emit `capacity=unavailable`
rather than synthesizing numbers, and do not have a health handler start
reading container logs or leaking account IDs into chat.

## Lesson 3 — long-running streams are not stale requests

The LB's watchdog logs looked alarming: `http_bridge_startup_wait_timeout=6`
and `http_bridge_stuck_watchdog_skipped=6` in a 13-minute window. But the
skipped requests had `response_id=True`, 65–179 streamed events, and ages of
163–172s. Those were healthy long inferences, and
`_http_bridge_pending_state_is_stale()` already excludes any request carrying
`response_id` or `latency_response_created_ms`. That exclusion is a feature.

So:

- `http_bridge_stuck_watchdog_skipped > 0` is **not** by itself a failure
  metric. Do not "fix" it by evicting on age.
- The real failure signature is the *following* cohort: gate holders with
  `response_id=False`, `awaiting=True`, `available=0` — pre-created holders
  with no `response.created` and no visible downstream that never get cleaned
  up, plus queue/gate state surviving a client cancellation.

Assertions worth pinning in a reproduction:

```python
assert leader.response_id is not None and leader_session.closed is False
assert follower not in session.pending_requests
assert session.queued_request_count == 0
assert elapsed <= admission_wait_timeout + 2.0
```

Also require that the next request does not immediately re-stick to the
quarantined bridge/account, and that with no alternative account the caller
gets a bounded `429` / `upstream_unavailable` instead of unbounded internal
waiting.

## Lesson 4 — raising the timeout is the wrong lever here

The incident logged a 150s threshold while the current resolver computed 900s
for the same provider/model, i.e. config or deployed-revision drift. Resolve
that discrepancy as a *fact-finding* item, not by raising timeouts:

- capacity waiting must terminate inside the LB's admission timeout as a
  bounded error;
- only requests with genuine upstream activity should benefit from a longer
  stale timeout;
- if a longer stale timeout also extends queue waiting, revert it and prefer
  LB fail-fast.

Only write a re-resolution test if the non-stream path does *not* already read
`agent.provider`/`agent.model` per call. In this codebase it did, so no new
implementation was warranted — check before coding.

## Lesson 5 — concurrent duplicate delegation amplifies a capacity incident

`max_concurrent_children=3` plus inherited `xhigh` reasoning plus ~64k-token
child requests multiplies pressure on a single saturated endpoint. Two
mitigations, in this order:

1. Operational: serialize heavy children (`max_concurrent_children: 1` on a
   ~3.7 GiB host), pin `reasoning_effort`, cap `max_summary_chars`.
2. Code: reserve an active-delegation fingerprint **before** building the
   child, so a racing identical request returns
   `status="duplicate"` + `existing_subagent_id` rather than spawning a second
   agent.

Fingerprint over `parent_session_id`, resolved workspace, normalized goal,
normalized context, role, provider, model. Store only the SHA-256 digest in the
registry/logs — never copy raw context into a new structure. Registering in the
existing TUI `_active_subagents` map at spawn time is too late to prevent the
race; reserve atomically, and release in `finally` on every sync, background,
batch, and exception path.

## Sequencing that keeps causality legible

Deploy in separated stages so you can attribute both the fix and any
regression: config mitigation → agent code → LB change (only if a reproduction
demanded it) → 24h observation. Restart the gateway and the container **once
each**, at their own approved stage, not repeatedly during investigation.

Aggregate over the observation window:

```text
API call failed after .* retries
fallback_candidate_skipped.*same_failure_domain=true
duplicate delegation suppressed
http_bridge_startup_wait_timeout
response_create_gate_timeout
pending_count / queued_count / available
```
