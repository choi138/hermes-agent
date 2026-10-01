# Task branch: 2. Build a Stage-Level Baseline

Read this branch only for 2. build a stage-level baseline. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

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

