# Hermes Agent Latency Evidence Bank — July 2026

## Purpose

This is a dated, session-specific evidence bank for applying the class-level `agent-performance-engineering` workflow to a persistent Hermes messaging gateway. It records measurements, public cases, and a rollout seed. Re-verify live branches, config schemas, and issue states before making changes.

No credential values, private endpoints, or connection strings are included.

## Measured Local Baseline

### Direct Discord work

Recent 48-hour sample of five direct `discord-core` tasks:

```text
sample size                         5 tasks
API calls per task, median          22
end-to-end median                   650.4 s
model API time per task, median     387.9 s
tool/verification/other median      262.2 s
at least 5 minutes                  5/5
at least 10 minutes                 4/5
```

Interpretation: provider time is material, but the larger structural problem is many sequential calls plus substantial non-provider work. A faster model alone cannot remove the repeated rounds.

### Same CI diagnosis through Hermes and Codex CLI

```text
Hermes end-to-end       742.250 s
Codex CLI               272.779 s
ratio                    2.721x
absolute gap             469.471 s
```

The first direct error was obtained only about nine seconds apart. Roughly all of the large gap accumulated after the causal failure was already visible, during alternate access paths, repeated validation, and answer completion. This supports routing contiguous repository investigation to a coding executor and giving the gateway a bounded verification role.

### Context growth during one thread

Approximate prompt sizes observed:

```text
new task start                 15,867 tokens
next question start           116,948 tokens
peak during investigation     202,096 tokens
after compression              94,823 tokens
```

Large operational skills observed:

```text
remote-hermes-operations/SKILL.md              71,028 bytes
hermes-agent/SKILL.md                          51,608 bytes
codebase-architecture-inspection/SKILL.md      42,451 bytes
some installed skills                         >100 KB
```

Interpretation: full skill bodies and raw tool results become multiplicative when resent over many calls. Compression reduced the peak but still left a large working prompt.

### Measured non-primary causes

At inspection time:

- CPU was roughly 97–98% idle.
- The state DB was about 1.4 GB, but a recent-session lookup was about 1.18 ms.
- SSH already used a persistent control connection.
- The small remote host had roughly 3.7 GiB RAM and substantial swap use; this can worsen tail latency under concurrency but did not explain the observed median.

Do not generalize these values to a future run; repeat the probes. The reusable lesson is to measure resource hypotheses before prioritizing hardware or DB maintenance.

## Public Fix and Benchmark Cases

### 1. Progressive prompt and skill loading — controlled before/after

Evidence grade: **1 — measured before/after with comparable answer quality reported**

Issue: <https://github.com/NousResearch/hermes-agent/issues/10164>

Qwen3.5-9B local benchmark:

```text
                               stock       progressive
system prompt tokens           10,033      106
single-call wall clock         38.8 s      13.4 s
four-roundtrip wall clock      155 s       54 s
```

The user removed an accidentally discovered repository `AGENTS.md` from the gateway CWD and replaced the full skill listing with on-demand skill search. The issue remains open; treat this as a reproducible local technique, not an upstream default.

Applicable lesson: keep gateway CWD intentional, keep base instructions small, and load skill details only when needed.

### 2. Gateway hygiene no-op on oversized transcripts — merged functional fix

Evidence grade: **2 — merged fix with regression tests, no public wall-clock retest**

PR: <https://github.com/NousResearch/hermes-agent/pull/60981>

Commit: <https://github.com/NousResearch/hermes-agent/commit/d6a275b735d7bd90472a193f35bc11888fa007ef>

Observed failure:

```text
about 250K-token gateway transcript
hygiene log: 467 -> 467 messages
same oversized history rehydrated on each next turn
```

Fix: bind the hygiene compressor to the gateway SessionDB and compact the active transcript in place while retaining the data-loss guard. Validation reported 26/26 gateway hygiene tests passing.

Applicable lesson: verify that a claimed compression changed the retained prompt or transcript; “compression ran” is not enough.

### 3. Proactive tool-result pruning below the full-compression threshold

Evidence grade: **2 — merged implementation and extensive tests, no public end-to-end latency number**

PR: <https://github.com/NousResearch/hermes-agent/pull/70254>

Problem: on 512K/1M context models, a 50% full-compression trigger fires too late, so old terminal/file/web results are repeatedly billed.

Opt-in configuration introduced:

```yaml
compression:
  proactive_prune_tokens: 48000
  proactive_prune_min_result_chars: 8000
  proactive_prune_min_reclaim_tokens: 4096
```

The prune is deterministic and does not invoke an LLM. It preserves the recent tail and commits only when measured reclamation clears the configured minimum, reducing prompt-cache churn. Reported validation included a 444-test targeted battery and an 8,822-test `agent` + `run_agent` sweep.

Applicable lesson: deterministic pruning should precede earlier full-LLM compression on large-window models.

### 4. Compression-attempt and summarizer-input bounds

Evidence grade: **2 — merged fixes with targeted regression tests**

- Attempt cap: <https://github.com/NousResearch/hermes-agent/pull/69315>
- Bounded summarizer input: <https://github.com/NousResearch/hermes-agent/pull/70249>
- Raw `skill_view` body handling: <https://github.com/NousResearch/hermes-agent/pull/70275>

These changes address different failure modes:

- repeated preflight/post-tool compression attempts within one turn;
- the compression summary request itself becoming oversized;
- large skill content surviving or reappearing in hot context after compaction.

Applicable lesson: context hygiene needs attempt caps, bounded summary input, and explicit treatment of skill/tool payloads. Any one mechanism alone is incomplete.

### 5. Prefix-cache invalidation and extreme local slowdown

Evidence grade: **3 — maintainer-confirmed resolved on main, no fresh independent wall-clock retest in the closure**

Issue: <https://github.com/NousResearch/hermes-agent/issues/13442>

The report described a 314x llama.cpp slowdown caused by unstable conversation-prefix behavior. The maintainer closed it as resolved on current main through a byte-stable cached system prompt, forked background review, stripping internal fields before API calls, and moving changing content outside the cached prefix.

Applicable lesson: a prompt may be prepended on every request and still benefit from prefix caching if it is byte-stable. Dynamic timestamps, environment prose, or review state in the system prefix can invalidate caches.

### 6. Hermes as coordinator with a coding executor

Evidence grade: **5 — public architecture/use-case evidence, not a latency benchmark**

Public examples include Hermes managing tickets while Claude Code implements them, and a Linux Hermes instance invoking coding CLIs on a Mac over SSH. These support the topology:

```text
gateway: classify, authorize, retain memory, report progress
coding executor: contiguous repository loop
Hermes: final diff/test/result verification
```

Do not claim a speedup from these examples alone. In the local environment, the Hermes-vs-Codex timing above is the relevant quantitative evidence.

## Dated Applicability Snapshot

At the 2026-07-24 inspection:

```text
custom branch            hermes/all-work
HEAD                     72612fc69a48eb34238c928ec1b4d19d60270c02
HEAD date                2026-07-21
working tree             clean
direct max_turns         90
specialist max_turns     30–40
compression threshold    0.5
protect_last_n           20
protect_first_n          3
smart model routing      disabled
profile toolsets         unrestricted/default
```

The in-place gateway hygiene commit was already an ancestor. Source comparison showed that the later proactive-prune, absolute-threshold, common attempt-cap, and ghost-skill handling features were not present. Therefore merely adding the new config keys would not work; code integration must precede configuration.

Because the custom branch carried its own commits and lagged a rapidly changing upstream, isolated feature cherry-picks were likely to conflict with prerequisite compression changes. Prefer an integration worktree, full dependency-aware merge/port, targeted tests, and a one-profile canary.

## Rollout Seed

### Phase A — remove repeated reasoning work

1. Route repository coding to Codex/Claude Code or another leaf executor.
2. Give direct gateway work a task-class budget, such as 10–12 model calls and a 3–4 minute warning boundary.
3. Permit one retry of the same access strategy.
4. Stop when direct cause, execution path, and one independent confirmation agree.
5. Promote over-budget work to durable orchestration rather than lowering quality.

### Phase B — integrate context fixes

Port or merge the upstream context feature set in a sandbox worktree, then initially enable:

```yaml
compression:
  enabled: true
  threshold: 0.5
  target_ratio: 0.2
  protect_last_n: 20
  protect_first_n: 3
  proactive_prune_tokens: 48000
  proactive_prune_min_result_chars: 8000
  proactive_prune_min_reclaim_tokens: 4096
  max_attempts: 3
```

Do not simultaneously lower the full-compression threshold. After measuring proactive pruning, A/B an absolute cap such as 96K or 128K and optional idle compaction separately.

### Phase C — shrink always-visible context

- Refactor large `SKILL.md` files into an 8–12KB operational core plus `references/`.
- Deduplicate same-skill loads by content hash where supported.
- Preserve skill identity/path/version while removing raw old bodies from hot history.
- Configure coordinator, coder, and researcher with distinct toolsets; verify provider request schemas actually shrink.

### Phase D — controlled A/B and canary

Representative task classes:

1. short read-only lookup;
2. CI failure diagnosis;
3. small repository change;
4. tool-output-heavy investigation;
5. follow-up in a large existing thread.

Keep model, provider, service tier, reasoning, prompt, repo SHA, CWD, authentication, permissions, and stop condition fixed.

Initial acceptance gates:

```text
direct work p50                 240–360 s
model API calls per direct task <= 12
coding task overhead            <= executor time + 90 s
compression attempts per turn   within configured cap
correctness/test regression     none
active task preserved           yes
```

These are targets, not promises. Evaluate p90, retries, summary integrity, and correctness before expanding beyond one profile/thread.

## Evidence Caveats

- GitHub issues overrepresent failures; curated showcases overrepresent success.
- A merged PR and passing tests demonstrate implementation safety, not a wall-clock improvement.
- Public Hermes-vs-Codex controlled benchmarks were not found in this research.
- Version and branch state change quickly; re-check issue status, commit ancestry, and current source before applying this reference.
