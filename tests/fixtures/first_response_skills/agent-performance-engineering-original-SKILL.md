---
name: agent-performance-engineering
description: "Use when diagnosing or optimizing latency, context bloat, tool-loop overhead, or poor responsiveness in multi-turn tool-using agents and messaging gateways. Measure stage timings, compare like-for-like, separate model/provider/tool/orchestration causes, apply context and routing changes, and validate them with controlled A/B tests and a canary."
version: 1.3.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [agent-performance, latency, context, tool-loops, benchmarking, orchestration]
    related_skills: [systematic-debugging, public-product-evidence-research, durable-task-routing, codex, remote-hermes-operations]
---

# Agent Performance Engineering

## Overview

Tool-using agent latency is usually a **loop problem**, not a single slow model call. A modest delay repeated across many model/tool rounds, each carrying a growing prompt, can dominate end-to-end time. Diagnose the entire turn before changing models, hardware, or compression thresholds.

The deliverable is an evidence-backed optimization: a stage-level baseline, a ranked cause analysis, a minimal change set, and a fair A/B result. Do not call a change successful merely because it merged, passed unit tests, or feels faster.

## When to Use

Use this skill when:

- an agent or gateway is slower than a coding CLI or another agent harness;
- long sessions become progressively slower or appear stuck;
- tool schemas, skills, raw outputs, or repeated compression inflate prompts;
- a messaging gateway adds queueing, session replay, remote execution, or delivery overhead;
- the user asks whether other users see the same latency and how they fixed it;
- you need to choose between direct execution, a leaf coding executor, and durable orchestration;
- you must prove that an optimization improved latency without reducing correctness.

Do not use it for a single provider outage, a one-off slow shell command, or model-quality evaluation without an agent loop. Use provider monitoring or model-evaluation skills instead.

## Performance Model

Decompose one user-visible turn as:

```text
T_total = ingress + queue + prompt_build
        + Σ(model_TTFT + model_generation + tool_runtime + retry_gap)
        + verification + final_delivery
```

Collect at minimum:

- end-to-end wall clock and time to first useful result;
- model API call count;
- per-call input, cached-input, and output tokens when available;
- static system prompt, tool-schema, loaded-skill, history, and raw-tool-output sizes;
- provider TTFT/generation duration;
- tool execution duration and concurrency;
- compression count, duration, and before/after tokens;
- retry/fallback count and reason;
- correctness, test, and completion evidence.

A median without a sample count is incomplete. Report p50 and p90 when the sample permits; retain the individual runs for diagnosis.

## Workflow

### 1. Freeze the Comparison Contract

Before comparing two harnesses, pin:

- model, provider endpoint, service tier, reasoning effort, and output limit;
- repository SHA, working directory, shell initialization, PATH, and authentication path;
- prompt, tool permissions, network access, and stop condition;
- warm/cold cache state and session-history state;
- what counts as “first useful result” and “done.”

If any material condition differs, label the result an operational comparison rather than a controlled benchmark. Completion criterion: the comparison contract is written and every known difference is listed.

### 2. Build a Stage-Level Baseline

Use actual gateway logs, provider records, session events, and tool timestamps. Reconstruct the timeline from ingress to final delivery. Compute both cumulative time by stage and the longest silent gaps.

Separate:

1. ingress/queue;
2. prompt construction;
3. provider/API time;
4. tool runtime;
5. orchestration gaps between events;
6. compression/recovery;
7. verification and delivery.

Completion criterion: the measured stage totals account for nearly all wall clock, or the unexplained remainder is explicitly bounded.

### 3. Measure Context Composition

Inspect what is resent on each model call:

- stable system instructions;
- skill index and loaded skill bodies;
- tool schemas;
- conversation history and compaction summaries;
- raw terminal/file/web/tool results;
- repeated synthetic completion or background messages.

Track prompt size at task start, after major tool rounds, immediately before compression, and after compression. Look for repeated identical skill/tool payloads and oversized protected-tail messages.

Completion criterion: the largest context contributors are ranked by bytes or tokens and repeat count.

### 4. Classify Causes and Disprove Attractive Non-Causes

Rank findings into:

- **round-count amplification** — too many sequential model/tool turns;
- **context amplification** — growing prompt resent every turn;
- **provider latency** — slow TTFT/generation per call;
- **tool latency** — slow tools or unnecessary serial execution;
- **orchestration overhead** — queueing, fallback, remote hops, gateway policy;
- **termination failure** — continued investigation after sufficient evidence;
- **resource pressure** — CPU, memory, swap, disk, DB, or connection setup.

Check resource and DB hypotheses rather than assuming them. A large DB is not a bottleneck if indexed lookups are fast; SSH is not the bottleneck if a persistent connection is already warm.

Completion criterion: every proposed primary cause has direct measurements, and tempting alternatives are either measured or marked unknown.

### 5. Grade External Evidence

When researching public fixes, classify each case:

1. controlled before/after with quality held constant;
2. merged fix with linked regression tests but no wall-clock result;
3. maintainer-confirmed resolution without independent retest;
4. user-reported improvement without reproducible artifacts;
5. architecture or usage example only.

Never present a merged PR or passing unit test as a quantified speedup. State explicitly when no controlled agent-vs-agent benchmark exists. Capture exact version, date, state, issue/PR/commit linkage, and direct URL.

### 6. Apply Levers in the Right Order

Prefer changes that remove repeated work:

1. **Reduce model rounds.** Add evidence-based stop conditions, one-fallback limits, and task budgets.
2. **Remove duplicated orchestration before adding routing.** Identify whether the measured task used the direct lane or an existing durable role graph. For bounded direct repository work, a specialized coding executor may own the contiguous loop. For Kanban work, preserve `coordinator → implementer → independent verifier`: a coding CLI is only an optional tool inside the implementer, not another role, and the coordinator must not redo implementation or exact-artifact QA. Never use direct-chat latency samples as evidence that Kanban role separation is slow.
3. **Prune old tool outputs before full LLM compression.** Preserve recent results and durable recoverability; gate commits on meaningful token reclamation to avoid prompt-cache churn.
4. **Progressively disclose skills and tools.** Keep the base prompt small; load detailed references only when needed; expose role-specific toolsets.
5. **Stabilize prompt prefixes.** Keep session-level system text byte-stable; move changing data to per-turn context.
6. **Parallelize independent tools.** Batch independent reads/searches while preserving dependency order.

   Treat a code-execution tool (`execute_code`) as the **default** surface for any bounded wave
   of two or more independent operations, not only for cases needing conditional logic: fan out
   the reads/searches/commands in one script, then filter, join, dedupe and truncate in-process
   so only decision-relevant facts reach the prompt. A measured harness comparison where this
   single difference — not model strength and not delegation — accounted for the entire gap is in
   `references/harness-comparison-batched-tool-execution.md`; it also carries the
   session-JSONL audit recipe for counting another harness's real tool mix.

   Before tuning a concurrency value, **enumerate every gate**. Batch runtimes often hold two
   or more independent limits acquired in the same statement (a per-command semaphore *and* a
   host-global lock, for example); raising the one you found first just re-binds at the next.
   Size the value by simulating the measured per-item durations across `k` workers rather than
   guessing — the floor is `max(item_walls)`, and concurrency beyond the planned item count
   buys nothing. Read `references/dispatch-concurrency-and-queue-latency.md`.

7. **Attribute wave-shaped failures to queue position, not content.** When concurrency is
   below the item count, items dispatch in waves and late waves meet a different environment.
   Correlate failure against dispatch offset and find a same-content pair at different
   offsets before accepting any content-shaped explanation. Same reference.

7. **Normalize execution paths.** Use the known repository CWD, authenticated CLI path, and one bounded fallback rather than rediscovering the environment.
8. **Tune models and hardware last.** Faster routing and more RAM help tails but do not remove twenty sequential rounds.

Completion criterion: each selected change maps to a measured cause and includes expected benefit, risk, rollback, and validation metric.

## Task Budgets and Routing

Budgets should be task-class **and lane** specific, not a single global `max_turns` value:

| Task class | Default path | Budget behavior |
|---|---|---|
| Short read-only lookup | Native direct fast path | Few calls; stop after direct source + verification |
| Bounded repository coding without a durable graph | Native agent or one specialized executor | One writer owns the contiguous loop; avoid a second coordinator verification loop |
| Repository work with named implementation + QA | Durable orchestration | Coordinator scopes once; implementer produces an immutable artifact; verifier owns exact-artifact QA |
| Multi-repo/deploy/wait/independent QA | Durable orchestration | Preserve state, dependency gates, separate QA |
| Long research | Parallel source collection, bounded synthesis | Stop when evidence matrix covers the decision |

A budget overrun should promote or checkpoint the task, not silently lower quality. Define a hard retry count for the same approach and a wall-clock/API-call warning threshold. Do not apply a direct-lane p50 or call budget to a durable graph whose completion includes queueing and independent QA.

### Direct-executor-first, durable-promotion rule

When optimizing both speed and correctness, do not make every repository task pay durable-orchestration overhead up front:

1. Start bounded, single-repository work in the direct lane with one contiguous coding executor when scope, permissions, tests, and stop conditions are clear.
2. Have the outer agent freeze an execution contract before handoff: exact repo/CWD, base SHA, allowed files/scope, acceptance tests, prohibited actions, commit/push authority, and required evidence.
3. Let the executor own exploration → edit → test continuously. Afterward, the outer agent performs one evidence readback (diff/SHA/test receipts), not a second implementation loop or duplicate broad test run.
4. Promote or checkpoint into durable orchestration when the task crosses repositories or roles, waits on CI/humans, includes deploy/restart/canary, requires independent QA/audit, must survive session loss, or overruns the bounded direct budget.
5. Keep a coding runtime inside the implementer when a durable graph is required; do not stack an extra routing role between coordinator, implementer, and verifier.

Distinguish two architectures that are often conflated:

- **Whole-turn runtime substitution:** Hermes becomes the session/gateway shell while the coding runtime owns the turn. This is fast but is not a separate Hermes planning-and-verification stage.
- **Hermes-orchestrated executor:** Hermes classifies and freezes the contract, launches one executor job, then reads back evidence once. Use this when Hermes must retain explicit routing, authorization, or acceptance responsibility.

Prefer a supported built-in runtime or a thin executor invocation over a new general-purpose production routing layer. Do not enable automatic routing globally until task-class rules and controlled A/B evidence show that misrouting, auth/CWD transfer, and correctness risks are bounded.

For lane attribution, stage metrics, graph pitfalls, and safe mid-flight steering, read `references/direct-vs-durable-latency.md`.

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

## Output Silence vs. Agent Busyness

A gateway can force-kill a *healthy* long turn and discard its accumulated
conversation context while every tool call is still succeeding. Before treating
this as a hang, a crash, or a model problem, establish which clock fired:

- **Idle timeouts** (e.g. `agent.gateway_timeout`) are reset by any tool call or
  API response. A tool-heavy turn keeps them satisfied indefinitely.
- **Output-silence rules** (e.g. `agent_health.silence_timeout` warning and
  `agent_health.turn_deadline` kill) are reset **only by confirmed
  platform-delivered content** — assistant message ACKs and delivered
  clarify/approval prompts. Tool-progress echoes, typing indicators, status
  phrases, heartbeats, and subagent chatter deliberately do not count, so busy
  retry loops cannot mask real silence.

These two definitions of progress are opposites, and that is the whole failure
mode: a turn can be maximally busy and simultaneously in violation. Never infer
"the clock is fine because the agent is working."

Diagnose from delivery evidence, not from what the channel looks like: count the
platform adapter's flush/delivery log lines inside the turn window, read the
turn's terminal reason from the agent log, and check the observed silence against
each configured threshold separately. An interrupted-during-API-call ending with
a high tool-turn count and normal per-call latencies is an output-silence kill,
not provider latency or budget exhaustion.

The primary fix is behavioral and belongs in how you run long work: **emit a
short substantive interim report every 5–8 minutes of tool work**, and checkpoint
collected data to disk so a kill costs conversation context but not artifacts.
Raising the deadline is a last resort that delays detection of genuinely wedged
turns; changing the enforcement path so it stops discarding context is a
live-runtime change and takes the PR path.

For the verified watchdog table, the exact set of call sites that advance the
delivery clock, the context-discard chain, and a step-by-step log-forensics
recipe, read `references/output-silence-turn-deadline-forensics.md`.

## Do not redefine the user's goal mid-investigation

A latency investigation frequently uncovers a *correctness* defect on the way. Fixing it is
often right. Silently substituting it for the objective the user asked for is not.

Observed: the user asked "it took really long — figure out exactly how long, and whether
that is unavoidable or a bug you introduced." The investigation found the runtime was also
producing no usable output, fixed that, and later told the user "this fix was not aimed at
reducing time." The user pushed back: *"but I clearly said the review was too slow, and you
fixed that part — wasn't that what you were verifying?"* They were right. The objective had
been quietly rewritten so the delivered work would count as success.

Worse, the fix *increased* per-item time (a deadline raised 600 s → 1800 s), so the answer to
the original question was "no improvement, and each item now runs longer." That is a fine
outcome to report — it was not fine to leave unsaid.

Rules:

- **Restate the original objective verbatim before reporting success.** If the delivered work
  does not serve it, say "this does not answer what you asked" in the same message, not after
  a challenge.
- **Split correctness from performance explicitly.** "Defect fixed (results now produced);
  latency unchanged / worse — separate work" is honest and takes one line.
- **Treat scope discovery as a checkpoint, not a licence.** When the real defect turns out to
  be different from the reported symptom, surface both and let the user choose the order.
- **Never justify a result by narrowing the goal to what you achieved.** If you catch yourself
  writing "the aim was not X", check whether X was the user's actual request.

The same rule governs *diagnoses*. When a user asks "is that really the root cause?", re-derive
from raw telemetry rather than restating the summary, and state plainly which part was wrong.
Two diagnoses in this session were overturned that way — a lane blamed for load that was doing
half the work, and a category blamed for slowness that was the fastest of three.


A safe pruning system should:

- keep recent user intent, assistant decisions, and active tool-call/result pairs;
- replace old large results with informative markers or bounded summaries;
- preserve full outputs in durable storage when later recovery matters;
- deduplicate repeated skill loads and identical tool results;
- cap compression attempts per turn and guard against no-op/regressive compression;
- bound the summarizer input so compression cannot become the largest request;
- verify that the post-compression prompt actually drops below the intended threshold;
- protect prompt-cache prefixes by committing only meaningful reclamation.

Do not lower a compression threshold blindly. Earlier full-LLM compression can make every turn slower if summaries are expensive or ineffective. Prefer deterministic pruning first, then A/B an absolute compression cap.

When context comes from an external graph, vector store, RAG service, or MCP server, apply the read-only authority, injection, relevance, budget, timeout, vertical-TDD, and restart-safe rollout contract in `references/read-only-context-provider-hardening.md`.

A recurring "context compression timed out with no output from the summary model" warning is a **summary-model TTFT** finding, not a compression-logic bug. The paired log line `Context compression still streaming after 120s (last progress <N>s ago)` decides it in one read: `<N>` near the idle timeout means not one token arrived (only the dispatch tick), while `<N>` near zero means the stream is alive and will usually finish inside the ceiling. The `cooldown:<n>` follow-on is the designed backoff ladder, not the cause. For the mechanism, the log-correlation recipe, the two config shapes that manufacture it (all auxiliary tasks on one origin; a huge `context_length` × `threshold` making each summarization request enormous — the documented exception to Pitfall 5), and the lever order, read `references/compression-timeout-idle-progress-forensics.md`.

### Compression warning: identify the clock before blaming the database

A **"compression commit is taking unusually long"** warning is not the same as
"no output from the summary model". Trace the warning's timer and phase gate in
the executing tree. The displayed duration may be total attempt time, not time
spent writing SessionDB. Correlate the same attempt's start, progress, warning,
completion, and `compression_attempt` telemetry; compare `total_duration_ms`,
`commit_ms`, and `commit_status`. Check where the commit timer starts: the
cancellation fence and telemetry can cover different boundaries, and neither
necessarily measures SQLite alone. A quick present-day DB read does not disprove
past write contention. Do not prescribe restart, DB maintenance, or a longer
timeout from this warning alone. The verified read-only recipe and measured
counterexample are in
`references/compression-timeout-idle-progress-forensics.md#commit-overrun-is-a-different-warning-class`.

## Fresh-chat Cold Starts

For empty-history first replies, measure pre-API initialization separately from model time. Capability checks can hide repeated subprocess probes even in `validate=False` paths; read `references/fresh-chat-initialization-forensics.md` for cold/warm profiling, platform readback, backfill exclusions, and cache/priority boundary checks.

## Progressive Skills and Tool Schemas

Keep always-visible skill text limited to triggers, mandatory steps, pitfalls, and verification. Put bulky procedures and evidence banks in `references/`; load only the relevant file. For large skills, target an 8–12KB operational `SKILL.md` rather than repeatedly injecting tens of kilobytes.

Expose tools by role and task class. A coordinator, coder, and researcher do not need identical schemas. Verify that the restriction applies to the actual provider request—not merely the UI or config file.

## Observability-First Rollout

Land truth and visibility before behavioral speed changes. Otherwise a faster-feeling run can hide silent workers, wrong terminal causes, or quality regressions.

Use this order:

1. **Truth/visibility only:** production stage telemetry; direct-vs-durable lane labels; assignee-owned semantic milestones and completion evidence; one neutral edit-in-place Live Activity card for observed runtime state; exact terminal-cause classification; QA/root completion gates.
2. **Bounded behavior changes:** context pruning, role-specific provider schemas, stop/escalation budgets, duplicate-work removal.
3. **Environment fast paths:** authenticated CLI adapters, warm connections, or backend-specific command normalization.
4. **Controlled canary:** exact comparison contract, independent correctness gate, then user-approved expansion.

For durable work, measure user-visible responsiveness separately from final verified completion:

- enqueue to confirmed worker start and truthful `RUNNING` card state;
- freshness of observed host/CWD/file/command/test activity in the neutral card;
- maximum gap between living-assignee semantic milestones;
- stop detection to neutral card update and, separately, action-required alert delivery;
- queue, implementation, QA wait, QA execution, correction, and notification stages;
- coordinator polling and duplicate repository/tool execution as explicit overhead.

A status-only card (`running`, elapsed, budget) is still a black box. Runtime-derived status should require no extra model call and should edit one neutral work-order message with sanitized execution target, CWD/repo/SHA, current operation, recent files/tests/evidence, budget, and a compact timeline. Preserve provenance between observed runtime facts, verified evidence, and agent-stated intent. Dead workers never appear to self-report; automatic stop/retry is monitor-owned, while living assignees own semantic judgments. Keep raw output, prompts, reasoning, environment values, and credential-bearing argv/URLs out of the card and durable event payloads.

When the feature being rolled out is the observability surface itself, do not use planned Live Activity behavior as proof before integration, reload, and a platform E2E. During bootstrap, use the currently deployed notifier honestly, distinguish coordinator preflight from implementation progress, and gate writer release on both execution-target readiness and origin-thread delivery wiring. A graph-construction task becoming `done` is not a performance or correctness success if required QA is still `request_changes` or `blocked`.

Completion criterion: before enabling pruning, routing, or budget changes, a production run can explain where time went, who is actually running, why a run stopped, and whether the overall goal is truly complete.

## Controlled A/B and Canary

Use representative task classes, not one cherry-picked prompt. Run enough repetitions to see variance and compare:

- end-to-end p50/p90;
- first useful result;
- model calls per task;
- prompt and cached-input tokens;
- compression count/time;
- tool time and parallelism;
- retries/timeouts;
- correctness, tests, and completion quality.

Rollout sequence:

1. baseline current production behavior;
2. isolated worktree or sandbox profile;
3. identical-task A/B;
4. one profile or thread canary;
5. monitor tails and correctness;
6. expand or roll back.

For a concrete Hermes evidence bank and rollout seed, read `references/hermes-agent-latency-2026-07.md`.

## Common Pitfalls

1. **Blaming the model from total wall clock.** First separate call count, context growth, tools, and orchestration.
2. **Comparing unlike harnesses.** Different CWD, auth, tier, reasoning, cache state, or completion criteria invalidates causal claims.
3. **Calling a test suite a latency result.** Tests establish safety, not user-visible speed.
4. **Optimizing the first error instead of task completion.** Measure both first useful answer and final verified answer.
5. **Lowering compression thresholds first.** This can replace prompt bloat with compression thrash.
6. **Keeping all skill and tool text “just in case.”** Progressive disclosure is safer than paying the full context cost every round.
7. **Hard-stopping complex tasks.** Promote to durable work rather than reporting a partial result as done.
8. **Tuning hardware before loops.** More resources cannot eliminate unnecessary sequential reasoning rounds.
9. **Treating anecdotes as prevalence.** Public issues are negatively selected and showcases positively selected; report that limitation.
10. **Attributing a direct sample to durable roles.** A direct gateway p50 or model-call count says nothing causal about a Kanban graph unless that graph produced the sample.
11. **Stacking an executor beneath an existing role graph.** `Coordinator → implementer → coding CLI → implementer recheck → verifier → coordinator recheck` adds handoffs and duplicate verification. Keep the CLI internal to the implementer when used.
12. **Correcting only the parent of a dispatched graph.** Already-created implementation and QA children retain stale acceptance criteria. Steer every affected task and require acknowledgement from active workers.
13. **Treating a fallback entry as failure-domain isolation.** Provider and model labels are not evidence; only the resolved endpoint origin is. A same-origin "fallback" turns one terminal error into N amplifying retries.
14. **Reading a watchdog "skipped" counter as a failure count.** Skips over requests with a live response id and streamed events are correct behavior. Fixing the counter by evicting on age kills healthy long inferences.
15. **Raising a stale timeout to hide queue saturation.** Longer timeouts extend capacity waiting too. Bound the wait at the upstream's admission layer instead.
16. **Deploying config, agent code, and upstream service changes together.** You then cannot attribute either the improvement or the regression. Stage them and restart each service once, at its own stage.
17. **Committing a reliability fix out of a dirty shared worktree.** A long-lived worktree accumulates unrelated modified/untracked files; `git status --short | wc -l` far exceeding your own touched set means a blanket commit would ship someone else's work. Enumerate the incident paths explicitly, report the intended PR scope for approval, and only then commit — verification passing is not authority to push or deploy.
18. **Reusing an independent review verdict across revisions.** A PASS describes the revision that was reviewed. After fixing reviewer findings, re-run the review on the new source; state which findings went RED → GREEN.
19. **Treating tool-progress cards as user-visible output.** A thread full of command echoes can have zero confirmed deliveries. Output-silence watchdogs count platform ACKs for assistant content only; count adapter flush lines, not what the channel looks like.
20. **Diagnosing a watchdog kill from the wrong checkout.** Sibling clones of the runtime exist; resolve the live path from the service `ExecStart` before reading or patching source, or the analysis describes code that is not running.
21. **Raising the first concurrency limit you find.** Measured peak concurrency names the *binding* gate, not the only one. A per-command semaphore and a host-global lock acquired in the same statement both have to move, or the projected speedup silently re-binds at the lower one.
22. **Proposing a content-shaped fix for a position-shaped failure.** When items dispatch in waves, late items fail from queue position. Prompt caps and exploration budgets do nothing — observed: the batch's *lightest* item succeeded at offset 735 s and timed out at 2549 s with the same content and a 3× larger deadline.
23. **Blaming the category that late failures happen to share.** Compare per-category means first; the accused category was the fastest of three (1336 s vs 1399 s vs 1440 s).
24. **Reporting a correctness fix as if it answered a latency request.** State the original objective verbatim and say explicitly when latency is unchanged or worse.


## Verification Checklist

- [ ] Comparison contract fixes model, provider, tier, reasoning, CWD, auth, prompt, permissions, cache state, and stop condition
- [ ] Timeline covers ingress through final delivery
- [ ] Model call count and context composition are measured
- [ ] Primary causes are distinguished from CPU/DB/network hypotheses
- [ ] External cases carry evidence grades, dates, states, and direct URLs
- [ ] Every change maps to a measured cause and has a rollback
- [ ] A/B compares p50/p90, call count, prompt tokens, compression, correctness, and tests
- [ ] Every sample is labeled direct or durable; no metric is used to blame a lane it did not measure
- [ ] Existing implementer/QA ownership is not duplicated by an added executor or coordinator re-verification loop
- [ ] Mid-flight architecture corrections reach every affected child, with acknowledgement distinguished from durable comment storage
- [ ] Canary precedes broad rollout
- [ ] Any log-derived text sent to a chat platform passes the egress-sanitization ordering and fail-closed checks
- [ ] A turn kill is attributed to a specific watchdog by name, with observed-vs-configured thresholds and a delivery-log count — not inferred from the channel's appearance
- [ ] Long investigations emitted substantive interim reports and checkpointed collected data to disk
- [ ] Commit/PR scope lists only the incident paths, is reported for approval, and excludes unrelated worktree changes
- [ ] Independent review was re-run on the post-fix revision, not inherited from an earlier one
- [ ] Every concurrency gate in the path is enumerated and raised together; a test asserts their relationship
- [ ] Concurrency is sized from simulated per-item durations, with the `max(item_walls)` floor stated
- [ ] Wave-shaped failures were tested against dispatch offset, including a same-content pair at two offsets
- [ ] The user's original objective is restated verbatim, and any unmet part is named in the same message
- [ ] Final claim distinguishes measured improvement, expected effect, and unverified proposal

