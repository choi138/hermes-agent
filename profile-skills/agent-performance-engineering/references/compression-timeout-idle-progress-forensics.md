# Compression Timeout Forensics: Reading `last progress` as TTFT Evidence

Applies to the user-visible warning:

```
⚠ Context compression timed out after 120.0s with no output from the summary
model. No messages were dropped — continuing without compression.
Run /compress to retry, /new for a clean session, or check auxiliary.compression.
```

This is emitted by `run_agent.py::_on_timeout` (the `compress_context`
progress-aware wrapper). It is almost never a compression *logic* bug. It is a
statement about the summary model's **time to first token**.

## The one log line that decides the diagnosis

Every compression attempt that crosses the idle window logs:

```
agent.conversation_compression: Context compression still streaming after 120s
(last progress <N>s ago) — extending wait (ceiling 600s)
```

`<N>` is the whole diagnosis:

| `last progress` | Meaning | Outcome |
|---|---|---|
| `≈ idle_timeout` (e.g. `119.4s`, `117.2s`) | **Not one token arrived.** The only progress tick was request dispatch. | Timeout fires immediately after this line |
| `≈ 0s` (e.g. `0.4s`, `0.2s`) | Stream is alive and delivering | Wait extends to the ceiling; usually completes |

**Why `119.4s` means "zero tokens"**: `agent/auxiliary_client.py::_create_with_progress`
calls `_notify_aux_progress()` on dispatch (`# request dispatched counts as
progress`), then `_ChatStreamAccumulator.feed()` ticks it again per streamed
chunk. So the idle clock starting at dispatch and never being reset proves the
upstream produced no first event at all. A value near zero proves the opposite.

Do not read "still streaming after 120s" as evidence that data is flowing — that
message is emitted on the idle-check path regardless of whether anything arrived.

## Recipe

1. Resolve the live runtime path from the service `ExecStart` before reading
   source (sibling clones exist — see Pitfall 20 in SKILL.md).
2. Correlate every attempt in one pass:
   ```bash
   grep -h "context compression started\|still streaming\|made no progress\|context compression done\|Auxiliary compression:" \
     ~/.hermes/logs/agent.log | grep "$(date +%F)" | cut -c1-200
   ```
   Pair each `started` with its `still streaming` line and its terminal
   (`done` vs `made no progress`). The `last progress` value should predict the
   outcome every time; if it does not, the mechanism above has changed.
3. Establish whether "today" is actually anomalous before theorizing. Count
   upstream errors per day across rotated logs rather than trusting the current
   file:
   ```bash
   for f in ~/.hermes/logs/agent.log*; do
     echo "--- $f"
     grep -h "temporarily busy" "$f" | awk '{print $1}' | sort | uniq -c
   done
   ```
   A step change (e.g. 0 → 33 → 137 over three days) attributes the timeouts to
   an upstream capacity spike, not to a config or code change.
4. Bucket the spike by hour (`awk '{print substr($2,1,2)}'`) and confirm the
   timeouts fall inside the same window.

## `cooldown:<n>` is a symptom, not a cause

The follow-on warning — "compression is currently blocked (cooldown:60)" — comes
from `_TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)` in `agent/context_compressor.py`.
It is the designed backoff after a timeout, escalating on consecutive failures so
the full idle budget is not re-burned every turn. Never "fix" the cooldown;
fix what prevented the first token.

## Two config shapes that manufacture this failure

Check both before touching timeouts:

1. **All auxiliary tasks pinned to one provider.** When every
   `auxiliary.<task>.provider` and `model.provider` resolve to the same origin
   with no fallback configured, an upstream wobble takes down conversation,
   compression, and vision together. This is the same-failure-domain problem
   from `references/provider-failover-retry-exhaustion.md`, seen from the
   auxiliary side. Splitting compression onto a different origin is the
   highest-leverage fix.

2. **A very large `model.context_length` multiplied by `compression.threshold`.**
   `context_length: 1000000` with `threshold: 0.5` means compression does not
   start until ~500K tokens have accumulated — observed inputs of
   `messages=832 tokens=~484,799`. A ~500K-token summarization prompt has a TTFT
   of tens of seconds on a healthy server, so the default 120s idle budget is
   already thin; add upstream retry delay and it is consumed before generation
   starts. Successful attempts in the same session still took 2–3 minutes.

   This is the documented exception to SKILL.md Pitfall 5 ("Lowering compression
   thresholds first"). The pitfall guards against compression *thrash* from
   compressing too often. It does not apply when the threshold is so high that
   each individual summarization request is itself enormous. Diagnose which
   regime you are in from the logged `tokens=~N` on `context compression started`
   before choosing a direction.

## Levers, in order

1. Move `auxiliary.compression` to a different resolved origin than the main
   conversation model (widen the failure domain).
2. Lower the effective compression trigger so each request is smaller — reduce
   `compression.threshold`, or correct a `model.context_length` that overstates
   the real limit.
3. Raise `compression.context_timeout_seconds` (default
   `DEFAULT_CONTEXT_TIMEOUT_SECONDS = 120.0`; ceiling
   `DEFAULT_CONTEXT_TOTAL_CEILING_SECONDS = 600.0`, resolved in
   `agent/conversation_compression.py::resolve_context_compression_timeouts`).
   Mitigation only — it buys TTFT headroom without reducing request size.

Config changes require a gateway restart, which drops the live session. Say so
when proposing them.

## Commit overrun is a different warning class

Do not apply the no-output/TTFT diagnosis above to this warning:

```
Context compression commit is taking unusually long (600s, ceiling 600s).
Waiting for it to finish safely — if this persists, check SessionDB health
(disk / lock contention).
```

### Read-only diagnosis

1. Resolve the running gateway's source tree from service/process metadata.
   Locate the exact warning, its callback arguments, and the timer's origin.
   Do not infer the elapsed interval from the word `commit`.
2. Correlate one session/attempt across `context compression started`, progress,
   overrun, `context compression done`, and `compression_attempt` telemetry.
   Include the terminal record: a warning may already have been followed by a
   successful commit before the user asks about it. Keep unrelated sessions and
   quoted user copies of the warning out of the evidence window.
3. Compare `total_duration_ms`, `commit_ms`, `commit_status`, and `split_status`.
   Inspect `_emit_compression_attempt_telemetry`, the `_commit_started_at`
   assignment, and `CompressionCommitFence.begin_commit/finish_commit` in
   `agent/conversation_compression.py`. In the observed implementation,
   `wait_started` measures the whole attempt; crossing its ceiling while the
   commit fence is active emits the warning. The fence begins before prompt
   rebuilding, while the `commit_ms` timer starts later and includes memory and
   session bookkeeping. Neither timer is automatically a SQLite-only duration.
4. Check resource hypotheses with bounded observations: disk free space, DB/WAL
   sizes, and a timed minimal query on a `mode=ro` SQLite connection. These are
   present-state checks, not proof that an earlier writer never waited on a
   lock. Avoid full-table scans or maintenance commands for this diagnosis.
5. Report total time, measured commit interval, and terminal outcome separately.
   Call the remainder summary/preparation/other unseparated work unless finer
   telemetry exists; subtraction alone cannot attribute all of it to the model.
   Do not restart or abandon an in-flight commit merely because the ceiling was
   crossed. A warning-text improvement is not a demonstrated latency fix.

### Measured counterexample — 2026-09-08

Read-only source/log inspection of running revision
`24fdfb8cc3787d297fd2572f22badf4b1f9d31dc`, session
`20260908_091929_343e4e18`, found:

- Start: `20:18:25.625`; 221 messages, approximately 358,793 tokens.
- Warning: `20:28:25.555`; displayed wait 600 seconds.
- Completion: `20:28:30.475`, 4.92 seconds after the warning.
- Telemetry: `total_duration_ms=604904`, `commit_ms=2558`,
  `commit_status=committed`, `split_status=in_place_committed`.
- Contemporaneous checks: 86.48 GiB free; a minimal read-only DB query completed
  in 29.93 ms. These did not establish the absence of historical write locks.

Thus "DB storage hung for ten minutes" was not supported. The whole compression
attempt exceeded its ceiling while the fence was active, and the completed
commit interval was only 2.558 seconds. Most time lay outside that interval;
the model-versus-local-preparation split remained unmeasured. This validates the
diagnostic method, not a configuration change or performance remediation.

## Pitfalls

- Reporting the timeout as a compression bug or a "dropped messages" incident.
  The warning explicitly states no messages were dropped; the transcript is
  intact and the turn continues uncompressed.
- Treating `still streaming after 120s` as proof of streaming.
- Raising the timeout as the first lever. It hides an upstream/sizing problem
  and lengthens every future stall.
- Concluding "compression is broken" when the same log shows successful
  attempts minutes apart. Count successes and failures before characterizing it.
