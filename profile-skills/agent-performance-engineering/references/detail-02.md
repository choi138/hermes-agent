# Task branch: Task Budgets and Routing

Read this branch only for task budgets and routing. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

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

