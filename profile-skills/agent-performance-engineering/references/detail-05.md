# Task branch: Context pruning and compression attribution

Read this branch only for context pruning and compression attribution. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

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

