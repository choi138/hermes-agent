# Task branch: Retry, Fallback, and Failure-Domain Reliability

Read this branch only for retry, fallback, and failure-domain reliability. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

## Retry, Fallback, and Failure-Domain Reliability

Retry exhaustion is a routing problem before it is a timeout problem. When a
turn ends as `API call failed after N retries`, establish these before changing
any threshold:

1. **Did the fallback actually change failure domain?** Compare canonical
   `scheme://host:port` of the *resolved* client `base_url`, not the configured
   provider/model labels. A differently-named provider and model can resolve to
   the same endpoint, in which case every retry re-enters the same saturated
   pool and the "fallback chain" only multiplies load. Skip same-origin
   candidates for infrastructure reasons (`timeout`, `server_error`,
   `overloaded`) while still allowing them for model-level reasons
   (`model_not_found`, `content_policy_blocked`).
2. **Is the first cause still visible?** An alert showing only the final hop
   hides the originating error and the route transition. Preserve a bounded
   failure chain (first, route transitions, last, retry count, message/token
   counts) and keep the existing log prefix so downstream health regexes keep
   matching.
3. **Is the upstream wedged or merely busy?** `/health` 200 proves liveness,
   not serviceability. Distinguish requests with real streamed activity from
   holders that never produced a first event — evicting the former by age
   destroys healthy long inferences.
4. **Is concurrency amplifying it?** High child concurrency, inherited maximum
   reasoning effort, and large child prompts converge on the same endpoint.
   Serialize heavy children and suppress concurrent duplicate delegations
   (reserve a work fingerprint *before* building the child, not at spawn
   registration) before touching timeouts.

Raising a stale timeout is the last lever, not the first: capacity waiting
should end as a bounded upstream error inside the admission timeout. Verify
whether the timeout is even re-resolved per call before writing code for it.

Before *adding* a provider or model to a fallback chain to widen the failure
domain, vet the candidate first — real host, free-vs-paid claim, what the token
can actually reach, and whether it returns usable content rather than
reasoning-only empty bodies. See the `llm-provider-model-onboarding` skill; an
unvetted entry belongs at the tail of the chain, never as primary or classifier.

Two scoping rules keep these mitigations from causing their own regressions:

- **Same-origin skipping must not become an origin blacklist.** Gate the skip on
  `infrastructure reason AND same resolved origin AND (same model OR
  private/loopback origin)`. That still blocks a loopback shim from being
  re-entered under a different label, while preserving a legitimate recovery to a
  smaller model on a public provider's shared origin. And if the resolver hands
  back the client the turn is currently using, do not close it.
- **Duplicate-delegation suppression must not eat deliberate N-sampling.** Only
  suppress a fingerprint that is concurrently in flight from a *different* call;
  identical entries the user wrote inside one `tasks=[...]` batch are an N-sample
  request and must all run. Key the fingerprint on canonical origin plus a
  process-local HMAC of the credential (never the raw value), and hand
  reservation ownership from the scheduler to the runner explicitly so acceptance
  does not release it early.

For the full incident anatomy, watchdog-metric interpretation, and the
staged-mitigation sequence, read
`references/provider-failover-retry-exhaustion.md`.

When the mitigation includes a health/incident **alert path that ships
log-derived text to a chat platform**, that formatter is an egress boundary:
mention injection, markup/line forging, masked links, URL-userinfo credentials,
and redactor failure all apply. Read
`references/health-alert-egress-sanitization.md` for the verified ordering
(neutralize markup last, redact over a wider window than you emit), the
fail-closed contract, and the tool-less independent-review harness.

