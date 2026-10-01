# Harness Comparison: Batched Tool Execution (measured 2026-09-01)

Evidence bank for the **"parallelize independent tools"** lever, and a worked
example of Step 4's *disprove attractive non-causes*. A user reported that
another local harness (`omo`, `omo-ai@5.0.0-0.beta.31`) out-performed this agent
on the same prompt — "write a plan for claude-lb M3" — and asked how to close
the gap.

## How to audit another harness's session

Harness session transcripts are usually append-only JSONL. This one lived at
`~/.omo/agent/sessions/<slugified-cwd>/<ISO>_<uuid>.jsonl` with this shape:

| Record | Meaning |
|---|---|
| `{"type":"session", ...}` | session id, cwd, start time |
| `{"type":"model_change", provider, modelId, reason}` | model + fallback events |
| `{"type":"thinking_level_change", thinkingLevel}` | reasoning effort |
| `{"type":"compaction", summary}` | context compression events |
| `{"type":"message"}` → `message.role` | `user` / `assistant` / `toolResult` |

Assistant tool invocations are content parts with `type: "toolCall"` carrying
`name` + `arguments`; results arrive as separate messages with
`role: "toolResult"` and `toolName`. Counting `toolCall` parts and grouping
`toolResult` by `toolName` gives an exact tool mix with no estimation. Reading
each `arguments.code` body reveals what actually happened inside a
code-execution tool — which is where the real work hides.

Do this before theorizing. The transcript is ground truth; a user's recollection
of "it delegated to lots of agents" was wrong here (see below).

## Measured result

Single session, 1,118 messages: `assistant` 560, `toolResult` 497, `user` 61,
with **one** compaction event across roughly five hours.

Runtime — note this is the *opposite* of a like-for-like advantage:

```
faster harness : provider=openai  modelId=gpt-5.6-sol-fast  thinkingLevel=medium
slower harness : claude-opus-5    reasoning=high
```

Tool mix by `toolName` (497 results):

```
195  eval          122  bash          110  apply_patch     45  read
  8  monitor         7  bash_output     5  todo             2  bash_input
  2  web_search      1  webfetch
```

The decisive number is what sat *inside* the `eval` cells:

```
total model-visible toolCalls          507
eval cells                             195
  └ containing parallel()/Promise.all  180  (92%)
tool.X() calls made INSIDE eval code   532
  bash 320 · todo 120 · read 89 · kill_bash 2 · bash_output 1
```

So ~1,039 operations were driven by 507 model-visible tool calls — roughly a 2×
reduction in round count, and a far larger reduction in context, because
reduction happened in-kernel before anything reached the model. A representative
first cell:

```js
const results = await parallel([
  () => tool.read({path:'.../understand/SKILL.md'}),
  () => tool.read({path:'.../CLAUDE.md'}),
  () => tool.read({path:'.../rules/git-convention.md'}),
  () => tool.read({path:'.../rules/memory-recall.md'}),
  () => tool.read({path:'.../rules/code-comment.md'}),
  () => tool.bash({command:'find ... -name "*claude-lb*"'}),
  () => tool.todo({op:'init', list:[...]})
]);
print(JSON.stringify({skill: skill?.slice?.(0,5000), ...}))
```

Seven operations plus checklist initialization plus output truncation, in one
model round. The `slice(0,5000)` matters as much as the `parallel()`: raw dumps
never entered the prompt.

## The attractive non-causes, disproved

Both plausible explanations were wrong, and the transcript settled it in one
pass:

- **"It won by delegating."** `task`, `team_create`, `task_send`, and
  `create_goal` were called **0 times**, directly and inside `eval`. The harness
  advertises subagent and team tooling and used none of it. The parallel lanes
  the user remembered were lanes *they* had launched by hand in other sessions.
- **"It won on model strength."** It ran a smaller, `medium`-reasoning model
  against `claude-opus-5` at `high`.

Report this pattern honestly when it appears: a harness gap can be entirely a
**tool-surface and round-count** difference. Had either non-cause been accepted,
the resulting "fix" (add delegation topology / upgrade the model) would have
addressed nothing.

## Transferable lever

The analogue here is `execute_code`, which imports real tools
(`from hermes_tools import terminal, read_file, search_files, patch, write_file`)
and can sequence, branch, loop, and reduce before printing. Treat it as the
**default** surface for any bounded wave of two or more independent operations,
not only for cases needing conditional logic:

- fan out independent reads/searches/commands in one script;
- filter, join, dedupe, and truncate in-process; print only decision-relevant
  facts;
- when one result feeds the next, keep it in the same script and branch on the
  intermediate value.

Known limits to design around — they are narrower than the compared harness, so
size waves accordingly rather than assuming parity: 5-minute timeout, 50 tool
calls per script, 50KB stdout cap, and `terminal()` is foreground-only (no
background or pty). Step outside `execute_code` when the whole step is one tiny
call, when semantic judgment sits between calls, or when approvals and side
effects are involved.

## Procedural findings worth reusing

The same audit surfaced two execution disciplines with independent evidence in
that harness's directive text and its own run ledger:

- **Append-only ledger that outlives the context window.** The directive
  requires re-reading the whole notepad after any compaction notice before
  resuming, instead of re-planning. Captured as a rule in the `plan` skill.
- **A green suite is not proof of done.** Its verification ledger recorded
  `"class":"misleading_success_output"` with a compiled probe forging a route
  past 23 passing tests, yielding `"verdict":"needs-fix"`. This matches the
  existing `adversarial-verification-gating` skill — reproduce before believing
  a green run.
