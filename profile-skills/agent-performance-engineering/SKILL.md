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

Measure first; remove repeated work without lowering model quality, correctness, safety, or verification.

## Authority and operating contract

At entry, record the original user objective and the comparison contract below. Identify the executing host/CWD/repository/profile and whether the sample is direct or durable. Preserve model, provider, effort, personality, safety, auth/privacy, and permissions unless the user explicitly authorizes that change. Never read or expose credentials for a performance probe; sanitize telemetry before any egress. Keep profile config/secrets/terminal scope bound to the owning profile.

Keep conversation system text, catalog scope and schemas byte-stable; stage skill/tool/config changes for a new session. No production config mutation, restart, deploy, commit, PR or push follows merely from successful tests. Enumerate the incident paths and obtain applicable change approval; independent QA belongs to the independent verifier on the final revision, not the writer. Existing named implementer/QA roles must not be duplicated.

Stop when the original acceptance criteria and verification checklist are met; otherwise name the unmet gates and checkpoint evidence. Do not trade quality for a smaller prompt, a faster first error, or an artificial budget. Report correctness, measured improvement, expected effect, and unverified proposals separately. When no controlled A/B ran, say so. Research and canary steps apply only within the authorized task.

## Read only the relevant task branch

References use skill-directory-relative paths (including embedded `references/...` links), even when loaded from a detail file. Do not load every detail or the preservation index for an ordinary task.

- **2. Build a Stage-Level Baseline:** [references/detail-01.md](references/detail-01.md)
- **Task Budgets and Routing:** [references/detail-02.md](references/detail-02.md)
- **Retry, Fallback, and Failure-Domain Reliability:** [references/detail-03.md](references/detail-03.md)
- **Output Silence vs. Agent Busyness:** [references/detail-04.md](references/detail-04.md)
- **Context pruning and compression attribution:** [references/detail-05.md](references/detail-05.md)
- **Observability-First Rollout:** [references/detail-06.md](references/detail-06.md)
- **Controlled A/B and Canary:** [references/detail-07.md](references/detail-07.md)
- **Common Pitfalls:** [references/detail-08.md](references/detail-08.md)

Complete original-byte coverage: [preservation index](references/preservation-index.md). Existing topical references remain available; follow the branch pointers below to them.

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



## Fresh-chat Cold Starts

For empty-history first replies, measure pre-API initialization separately from model time. Capability checks can hide repeated subprocess probes even in `validate=False` paths; read `references/fresh-chat-initialization-forensics.md` for cold/warm profiling, platform readback, backfill exclusions, and cache/priority boundary checks.


## Progressive Skills and Tool Schemas

Keep always-visible skill text limited to triggers, mandatory steps, pitfalls, and verification. Put bulky procedures and evidence banks in `references/`; load only the relevant file. For large skills, target an 8–12KB operational `SKILL.md` rather than repeatedly injecting tens of kilobytes.

Expose tools by role and task class. A coordinator, coder, and researcher do not need identical schemas. Verify that the restriction applies to the actual provider request—not merely the UI or config file.


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


Exit only with verified evidence for the user's original objective; otherwise report the unmet gate.
