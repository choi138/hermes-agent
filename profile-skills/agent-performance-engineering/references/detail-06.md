# Task branch: Observability-First Rollout

Read this branch only for observability-first rollout. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

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

