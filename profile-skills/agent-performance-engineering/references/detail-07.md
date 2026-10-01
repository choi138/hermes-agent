# Task branch: Controlled A/B and Canary

Read this branch only for controlled a/b and canary. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

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

