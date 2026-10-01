# Direct vs Durable Lane Performance

Use this reference when an agent system has both an interactive/direct path and a durable Kanban or role-DAG path. The two lanes optimize different objectives and must not share unlabeled latency conclusions.

## 1. Attribute every sample to its lane

Before proposing a routing or architecture change, tag each measured task:

- **Direct lane:** one chat/gateway session performs the work synchronously, with optional short-lived delegation.
- **Durable lane:** a coordinator persists a graph; named workers claim tasks; implementation, verification, waits, and delivery are separate stages.

A direct-chat p50, model-call count, or CLI comparison is not evidence that Kanban role separation is slow. Conversely, a Kanban completion time that includes independent QA is not a fair direct-answer benchmark.

Record task/session IDs, source, worker run IDs, and stage timestamps so lane attribution is auditable.

## 2. Preserve the intended topology

### Direct, bounded work

```text
user → direct agent or one specialized executor → result
```

A specialized coding CLI may own a contiguous repository loop when the task is bounded and no durable implementation/QA graph already exists.

### Durable work

```text
user
  → coordinator: scope once, create dependencies, then stop implementing
  → implementer: sole writer, tests, immutable artifact/pushed SHA
  → independent verifier: exact-artifact review and risk-based checks
  → assignee-owned progress/final delivery
```

Do not stack a second `coordinator → coding CLI → coordinator re-verification` loop on top of this graph. A coding CLI can be an implementer's internal tool, but it is not automatically another role. The coordinator must not repeat the implementer's work or the verifier's diff/tests.

## 3. Measure different critical paths

### Direct metrics

- ingress and queue
- prompt build/context size
- provider TTFT/generation and call count
- tool runtime and retry gaps
- synchronous verification
- final delivery
- first useful result and final completion

### Durable metrics

- enqueue → ready
- ready → claim queue
- claim → worker startup
- implementation
- QA dependency wait
- QA execution
- correction-loop count and duration
- assignee notifier/delivery
- coordinator model calls, polling, and duplicate work as a separate overhead bucket

Report both first acknowledgement/progress and final verified completion. Durable work may take longer to finish because it deliberately includes QA while still improving responsiveness, recoverability, and concurrency.

### User-visible durable truth metrics

Treat silent periods and misleading terminal states as measurable orchestration defects, not merely messaging polish. Record:

- worker spawn to assignee-owned `Running` delivery;
- maximum gap between meaningful or runtime-derived progress reports;
- terminal-event detection to an exact cause/recovery report;
- whether the report came from the immutable run profile, with automatic runtime snapshots labeled as such;
- whether required QA was `approve`, `request_changes`, or `blocked` when the overall goal changed state.

Frequent empty lease heartbeats may remain silent. A slower user-visible cadence should be a content-free runtime snapshot that requires no extra model call and contains only current activity, budget, and last proven checkpoint. Never use a coordinator's generic “still working” message as evidence that a named specialist is running.

A coordinator task completing graph construction is not overall-goal completion. If required QA requests changes, the durable latency clock and correctness gate remain open even if the intake card itself is terminal.

## 4. Optimize the graph, not away the roles

If durable work is slow, investigate in this order:

1. coordinator polling instead of event-driven dependency promotion;
2. coordinator re-reading diffs, re-running tests, or reconstructing assignee progress;
3. the same broad test suite repeated by implementer and verifier;
4. research made a mandatory predecessor when it could be skipped or parallelized;
5. independent work serialized unnecessarily;
6. oversized task specifications and inherited context;
7. worker startup, queue, lease, or notifier delays;
8. correction loops caused by vague acceptance criteria.

Do not remove named ownership or independent QA merely because the durable final time exceeds a direct response.

## 5. Mid-flight correction of a dispatched graph

When a user corrects an architectural assumption after dispatch:

1. Inspect the parent and every created child; distinguish `ready/todo` from actually running.
2. Determine whether any mutable implementation has started. Preserve safe read-only research when still useful.
3. Add an explicit durable steering note to the parent **and every affected child**, including the verifier whose acceptance criteria may now be stale.
4. State that the newest user steering overrides the original body.
5. A comment is durable board context, not proof that an active worker received it. Require a worker-authored heartbeat/acknowledgement, or keep unstarted mutable children dependency-gated until they will load the corrected context.
6. Never report the graph as corrected merely because only the parent was commented.
7. Report exact statuses and whether implementation had begun.

If the runtime supports atomic task-spec updates or cancellation, prefer that for unstarted work; do not edit the board database directly when supported steering mechanisms exist.

## 6. Decision rule

Use **direct-executor first, durable promotion** rather than forcing every task through one lane:

1. Start short read-only work and bounded single-repository coding in the direct lane. One executor owns the contiguous loop.
2. Before coding handoff, freeze repo/CWD, base SHA, scope, acceptance tests, prohibited actions, mutation authority, and required evidence.
3. After execution, verify immutable evidence once. Do not repeat implementation or broad tests merely to make the outer agent feel certain.
4. Promote/checkpoint to durable orchestration when any durable trigger appears: multiple repositories or named roles, independent QA, CI/human wait, deploy/restart/canary, approval gates, audit requirements, session-survival needs, or direct-budget overrun.
5. Inside a durable graph, keep the coding runtime internal to the implementer; preserve `coordinator → implementer → verifier` ownership.

Do not confuse **whole-turn runtime substitution** with **Hermes-orchestrated execution**. In the former, the coding runtime owns the whole turn and Hermes is primarily the session/gateway shell. In the latter, Hermes scopes and authorizes, launches one executor, and reads back evidence. Pick explicitly based on whether separate orchestration judgment is required.

Prefer an existing supported runtime or thin executor call over a custom production auto-router. Introduce automatic task-class routing only after a controlled A/B demonstrates speed and correctness with CWD/auth/scope transfer held constant.

Compare direct and durable lanes separately, then optimize each against its own acceptance criteria.
