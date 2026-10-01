# Output-Silence Watchdog: Turn Kills That Discard Conversation Context

A long, healthy, tool-heavy turn can be **force-killed and have its accumulated
conversation context discarded** by the gateway agent-health watchdog even
though every tool call succeeded. This is designed behavior, not a crash. The
trap is that the watchdog's progress clock and the agent's own sense of progress
measure different things.

Verified against the live gateway runtime path (resolve it with
`systemctl --user cat hermes-gateway | grep ExecStart`; do not assume a
same-named sibling checkout is the running one). Anchor on symbol names — line
numbers drift between revisions.

## The three independent watchdogs

Do not conflate these. They have different progress definitions, and only one of
them kills a turn while discarding context.

| Watchdog | Config key | Default | Progress signal | Effect on a live turn |
|---|---|---|---|---|
| Agent idle timeout | `agent.gateway_timeout` | `1800` | any tool call / API response | Kills only when *completely idle* |
| Session stall watcher | (stall timeout) | — | `get_activity_summary()` | **Notify-only**, never kills |
| Agent-health rule A warning | `agent_health.silence_timeout` | `600` | confirmed channel content ACK | Emits `A.output_silence` alert |
| Agent-health rule A deadline | `agent_health.turn_deadline` | `1500` | confirmed channel content ACK | **Kills turn + invalidates generation** |

Consequence: a turn that calls tools continuously for 25 minutes without
emitting assistant prose keeps `gateway_timeout` happy forever and still gets
killed at `turn_deadline`. Busy ≠ alive, as far as rule A is concerned.

Defaults live in `hermes_cli/config_defaults.py` under the `agent_health` block;
a deployment overrides them in `~/.hermes/config.yaml` (`agent_health.enabled`,
`channel`, `mention`, `silence_timeout`, `turn_deadline`). Both clocks are
polled by `_session_stall_watcher` (30s interval) which calls
`_check_output_silence(silence, deadline)`.

## What actually advances the output clock

Only `_record_content_delivered(session_key, run_generation)` moves the clock,
and it is wired to a deliberately narrow set of call sites:

1. `GatewayStreamConsumer(..., on_content_delivered=...)` — confirmed platform
   ACK for **assistant message content** (both the normal turn path and the
   proxy path).
2. `_mark_interactive_prompt_delivered` — a delivered `clarify` / approval
   prompt.
3. `adapter.set_content_delivered_handler(self._record_content_delivered)` at
   adapter attach time.

The source states the exclusion as intentional:

> Only concrete gateway primitives count. Agent activity, heartbeat touches,
> and free-form status text intentionally remain outside A's policy so busy
> retry loops cannot hide real output silence.

So these do **not** reset the clock, no matter how much they fill the channel:

- tool-progress echoes (the `💻 terminal` / fenced-command cards visible in a
  Discord thread) — these are the biggest trap, because the thread *looks* busy;
- typing indicators, status-phrase rotations, heartbeat touches;
- reasoning/thinking deltas when `thinking_progress` is off;
- subagent activity and background job output.

Two secondary mechanisms:

- `_session_waiting_on_user` pauses the clock **only** for concrete blocking
  primitives — pending `clarify` (`tools.clarify_gateway.get_pending_for_session`)
  or a blocking approval (`tools.approval.has_blocking_approval`). Prose that
  merely asks a question does not count as waiting.
- `_resume_output_silence_clock` restarts the clock and clears the
  `_output_silence_notified` / `_turn_deadline_enforced` latches once an observed
  explicit user wait ends.

Stale-generation guards mean a late send from an already-interrupted turn cannot
retroactively satisfy the clock.

## Why the context disappears

On enforcement, `should_enforce_turn_deadline` → `_interrupt_and_clear_session`
(`interrupt_reason="agent-health output deadline"`,
`invalidation_reason="agent_health_turn_deadline"`), which:

1. cancels session background work via `_cancel_session_background_work` —
   `process_registry.cancel_for_session` **and**
   `async_delegation.interrupt_for_session`. This is why a user perceives
   "my background job was still running and it got shut down": backgrounded
   `terminal(background=true)` processes and detached `delegate_task` runs for
   that session are terminated, not just the foreground turn,
2. `request_hard_interrupt` on the running agent,
3. `_invalidate_session_run_generation` — **this is what loses the work**,
4. reaps turn-spawned processes, consumes/discards pending inbound,
5. posts a user-facing notice, then emits the `A.turn_deadline` health event.

The in-flight result is then dropped with
`Discarding stale agent result for <session_key> — generation N is no longer current`,
and the follow-up `/reset` starts a fresh session. **On-disk artifacts written by
the killed turn survive** — check them before concluding the work is gone.

## Forensics recipe

Given a health alert naming a session id and an origin thread:

0. **Disambiguate "the gateway died" from "the turn was killed" FIRST.** Users
   report this symptom as *"게이트웨이가 꺼진다" / "the gateway shuts down"*, but
   rule A never stops the process. Prove which one happened before diagnosing
   anything else:
   ```bash
   systemctl --user show hermes-gateway.service \
     -p NRestarts -p ActiveEnterTimestamp -p ExecMainStartTimestamp
   grep -c "agent_health_turn_deadline" ~/.hermes/logs/gateway.log
   ```
   A low, unchanged `NRestarts` with an `ExecMainStartTimestamp` older than the
   incident, combined with a nonzero `agent_health_turn_deadline` count, means
   the process stayed up and only turns were killed. Say so explicitly and
   correct the user's framing — the rest of the investigation (and the fix) is
   completely different for a real process restart. Cross-check
   `~/.hermes/state/restart-requests/history.log` for externally dispatched
   restarts before attributing one to the watchdog.

1. **Turn shape and terminal cause** — from `~/.hermes/logs/agent.log`:
   ```bash
   grep -n "<session_id>" ~/.hermes/logs/agent.log | head -60
   grep -n "<session_id>" ~/.hermes/logs/agent.log | tail -40
   ```
   Read the `API call #N` cadence and the closing line. A turn killed by rule A
   ends with `Turn ended: reason=interrupted_during_api_call ... tool_turns=N`,
   **not** a budget/`max_turns` exhaustion. Healthy per-call latencies plus a
   high `tool_turns` confirm the agent was working, not wedged.

2. **Count real channel deliveries** — from `~/.hermes/logs/gateway.log`, using
   the platform adapter's flush marker as the ground truth for "content actually
   reached the channel":
   ```bash
   grep -n "<thread_or_chat_id>" ~/.hermes/logs/gateway.log | grep -i "flush" 
   ```
   Zero flush lines inside the turn window is the proof. Timestamps outside the
   window (before turn start, after reset) do not count.

3. **Arithmetic against config** — compare elapsed silence to
   `agent_health.silence_timeout` and `turn_deadline` in `~/.hermes/config.yaml`.
   Both alert bodies quote the observed silence in seconds; confirm
   `observed > threshold` for each rather than asserting the rule fired.

4. **Confirm the discard** — `grep "Discarding stale agent result"` on
   `gateway.log` ties the lost result to the invalidated generation.

5. **Rule out neighbors** — check that `agent.gateway_timeout` and
   `agent.run_budget_seconds` are absent/unbreached in config so you do not
   attribute the kill to the wrong watchdog. An empty grep for
   `run_budget_seconds` means the feature is off, not that it fired.

## Prevention

- **Emit real interim prose during long investigations.** Every 5–8 minutes of
  tool work, send a short substantive update (what is confirmed so far, what is
  next). This is the only cheap, zero-config fix, and it doubles as the behavior
  the user actually wants. Tool-progress cards are not a substitute.
- **Checkpoint to disk.** Long collection loops should persist partial artifacts
  (`/tmp/<task>/page-*.json` style) so a kill costs the conversation context but
  not the data.
- **Treat rule A as a reporting contract, not a bug to be tuned away.** Raising
  `turn_deadline` delays detection of genuinely wedged turns. Prefer the
  behavioral fix; only widen the threshold for a known-long single operation, and
  say so explicitly.
- **If a design change is warranted**, the defensible shape is to gate only the
  hard kill on live background work, leaving the `silence_timeout` warning
  intact so detection is not lost. Do not invent the predicate — the same
  codebase already contains a verified one:
  `_scale_to_zero_has_live_background_work` / `_scale_to_zero_is_idle` in
  `gateway/run.py`. Reuse its sources and its semantics:
  - `cron.scheduler.get_running_job_ids()` — cron jobs run on the scheduler's
    own thread pool and are **outside `_running_agents`** (the documented
    #60432 blind spot). This is precisely the input rule A is missing.
  - `tools.process_registry.process_registry.has_any_active()` +
    `pending_watchers`.
  - `tools.async_delegation.active_count()`.
  - **Fail-alive on unreadable sources.** scale-to-zero counts an exception as
    work (sentinel `1`) so a transient read failure cannot make live work look
    idle. Copy that direction; a deadline gate must fail toward *not killing*.
  - Exclude permanent supervised watchers (`_hermes_supervised_watcher`) or the
    predicate is True forever and the gate never releases.
  Bound the deferral (e.g. a `deadline * N` ceiling) so a genuinely wedged turn
  holding a stale registry entry still dies. The alternative shape — stop
  *discarding context* on enforcement (interrupt the run but keep the session) —
  is more fundamental but touches the stale-generation / duplicate-response
  guards, so it carries a much wider regression surface.
  Either way this is a live-runtime change: PR path, not a direct push.

## Pitfalls

1. **Reading the thread instead of the delivery log.** A Discord thread packed
   with command cards can have zero real deliveries. Always count adapter flush
   lines.
2. **Blaming the model or the provider.** Per-call latencies of 3–16s and 100
   successful tool turns rule that out immediately.
3. **Assuming the wrong checkout.** Multiple sibling clones exist; patching a
   non-running copy produces a "fix" with no effect. Resolve `ExecStart` first.
4. **Calling a long turn "smart" because it kept working.** From the user's side
   a 25-minute silence is indistinguishable from a hang, and the runtime agrees.
5. **Broad `grep -r` over the runtime tree.** It walks `node_modules`-scale
   directories; scope to the specific file or use the ripgrep-backed search tool.
6. **Accepting "the gateway shut down" as the fact to explain.** Rule A kills
   turns, never the process. Run step 0 and correct the premise before you
   start diagnosing — otherwise you investigate a restart that never happened.
7. **Blaming scale-to-zero.** Its idle predicate is the one that *does* count
   cron and background work correctly, and it is armed only on Fly (opt-in
   `HERMES_SCALE_TO_ZERO` + relay-only messaging + a registered wakeUrl). Rule
   it out cheaply with `grep -c "scale-to-zero" ~/.hermes/logs/gateway.log`; a
   zero count means it never armed, so it cannot be the cause.
