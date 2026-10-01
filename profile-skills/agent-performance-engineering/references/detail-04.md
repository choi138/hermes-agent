# Task branch: Output Silence vs. Agent Busyness

Read this branch only for output silence vs. agent busyness. Original content below is verbatim. Resolve its embedded relative paths from the skill directory (the parent of references/), as in the original SKILL.md.

## Output Silence vs. Agent Busyness

A gateway can force-kill a *healthy* long turn and discard its accumulated
conversation context while every tool call is still succeeding. Before treating
this as a hang, a crash, or a model problem, establish which clock fired:

- **Idle timeouts** (e.g. `agent.gateway_timeout`) are reset by any tool call or
  API response. A tool-heavy turn keeps them satisfied indefinitely.
- **Output-silence rules** (e.g. `agent_health.silence_timeout` warning and
  `agent_health.turn_deadline` kill) are reset **only by confirmed
  platform-delivered content** — assistant message ACKs and delivered
  clarify/approval prompts. Tool-progress echoes, typing indicators, status
  phrases, heartbeats, and subagent chatter deliberately do not count, so busy
  retry loops cannot mask real silence.

These two definitions of progress are opposites, and that is the whole failure
mode: a turn can be maximally busy and simultaneously in violation. Never infer
"the clock is fine because the agent is working."

Diagnose from delivery evidence, not from what the channel looks like: count the
platform adapter's flush/delivery log lines inside the turn window, read the
turn's terminal reason from the agent log, and check the observed silence against
each configured threshold separately. An interrupted-during-API-call ending with
a high tool-turn count and normal per-call latencies is an output-silence kill,
not provider latency or budget exhaustion.

The primary fix is behavioral and belongs in how you run long work: **emit a
short substantive interim report every 5–8 minutes of tool work**, and checkpoint
collected data to disk so a kill costs conversation context but not artifacts.
Raising the deadline is a last resort that delays detection of genuinely wedged
turns; changing the enforcement path so it stops discarding context is a
live-runtime change and takes the PR path.

For the verified watchdog table, the exact set of call sites that advance the
delivery clock, the context-discard chain, and a step-by-step log-forensics
recipe, read `references/output-silence-turn-deadline-forensics.md`.

