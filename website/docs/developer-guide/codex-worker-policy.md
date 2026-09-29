---
title: Codex worker routing policy
---

# Codex worker routing policy

`scripts/run_codex_task.py` applies a deterministic model/effort policy before
it creates a `TaskRequest`. `TaskRequest` also revalidates structured policy at
construction, argv generation and execution, including lifecycle decode. The trusted coordinator supplies structured
metadata; the worker does not classify task prose and does not infer authority
from the SPEC. The policy never changes the approved root, working directory,
sandbox, timeout, or output bounds.

Policy version `2026-09-21.1` starts with these pairs:

| `--task-class` | Model | Effort | Intended use |
| --- | --- | --- | --- |
| `mechanical` | `gpt-5.6-luna` | `low` | Exact repeat changes |
| `bounded` | `gpt-5.6-luna` | `medium` | Fixed narrow work or a known cause |
| `general` | `gpt-5.6-terra` | `medium` | Ordinary development; the default |
| `integration` | `gpt-5.6-terra` | `high` | Known direction with multi-layer verification |
| `complex` | `gpt-5.6-sol` | `high` | Ambiguous design, judgment, or polish |
| `frontier` | `gpt-6-astra` | `high` | Hardest multi-system end-to-end work |

`--risk high` and `--ambiguity ambiguous` floor a selection at Sol/high.
`--ambiguity unknown` floors cheap ordinary work at Terra/medium. These floors
also apply after `--phase implementation --contract-resolved` downshifts a
resolved `complex` or `frontier` implementation phase to `general`; select
`--implementation-class bounded` only when the remaining scope is narrow. A short
task need not be split into phases.

## Normal and dry-run dispatch

All path and authority arguments remain required. `--dry-run` validates them,
builds the real `TaskRequest`, and prints the exact CLI argv without spawning:

```bash
python scripts/run_codex_task.py \
  --spec /approved/repo/SPEC.md \
  --workdir /approved/repo \
  --allowed-root /approved/repo \
  --output-dir /private/worker-receipts \
  --sandbox workspace-write \
  --timeout 900 \
  --task-class integration \
  --risk normal \
  --ambiguity resolved \
  --dry-run
```

Use `--model` and `--effort` for audited caller overrides. A caller-selected
model is recorded as `caller_override`, never as a user pin. The approved
Codex values are:

- Models: `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-6-astra`
- Efforts: `low`, `medium`, `high`, `xhigh`, `max`

An Astra/xhigh selection requires `--deeper-analysis-evidence`. A direct max
selection requires that evidence plus `--hard-judgment`,
`--high-failure-cost`, and `--choice-reason`. Direct selection is allowed; no
compulsory model ladder is imposed:

```bash
python scripts/run_codex_task.py ... \
  --model gpt-6-astra --effort max \
  --deeper-analysis-evidence "high effort missed a cross-system invariant" \
  --hard-judgment --high-failure-cost \
  --choice-reason "failure would corrupt tenant data" \
  --dry-run
```

Trusted, explicitly authorized pins use `--pinned-model` and
`--pinned-effort`; their receipt source is `user_pin`. Do not translate a
coordinator override into a pin. Unsupported values and unsafe floors fail
closed rather than substituting another pair.

## Prerequisites and retry decisions

Exactly one machine-checkable prerequisite flag may be supplied:

- `--missing-context`: block and request context.
- `--broken-environment`: block and request environment repair.
- `--deterministic-tool-sufficient`: skip the worker and use that tool.

Blocked and skipped decisions never create a worker artifact directory or
spawn a CLI. They also never widen permissions to make the task runnable.

The same command is a callable one-attempt retry planner. Supply
`--failure-kind`, `--attempt`, `--max-attempts`, `--prior-model`, and
`--prior-effort`; add `--handoff-ref` for each SPEC, diff, test, or hypothesis
artifact. `--dry-run` returns the next decision without executing it:

```bash
python scripts/run_codex_task.py ... --dry-run \
  --failure-kind approach_failure --attempt 2 --max-attempts 3 \
  --prior-model gpt-5.6-terra --prior-effort medium \
  --prior-action implement \
  --handoff-ref SPEC.md --handoff-ref tests/failing-output.txt
```

`missing_information` and `environment` block without escalation.
`shallow_reasoning` increases effort. `misunderstanding` and
`approach_failure` upgrade the model. `repeated_same_defect` stops unless
`--correction-evidence` identifies an evidence-informed correction; it never
blindly retries the same pair. The attempt bound or an exhausted Astra/max
pair returns `stop_replan`. The executor remains single-run, not a retry daemon.

The coordinator is responsible for deciding whether a phase split is worth
its handoff overhead. The policy never launches unsolicited parallel workers.

## Compatibility and receipts

Existing `--tier`, `--selection`, and `--pinned-tier` calls remain exact,
audited legacy overrides. `light`, `standard`, `deep`, and `max` still map to
`low`, `medium`, `high`, and `max`; max is not remapped to xhigh. The former
`--selection auto` name was tier selection, not an LLM classifier. New code
should use task classes or direct `--effort`.

Legacy flags do not bypass evidence gates. Unpinned `--tier max` requires
the same evidence/judgment/failure-cost/reason inputs as `--effort max`.
API/lifecycle callers carry the validated structured inputs in
`TaskRequest.policy_input` (a JSON-compatible mapping). The model and selection
must match its recomputed decision. A passed receipt is checked against that
decision, never trusted as an execution permit. Old explicit non-max requests
remain readable; evidence-free unpinned xhigh/max requests fail closed.

Claude stays a separate pass-through path:

```bash
python scripts/run_codex_task.py ... \
  --cli claude --model sonnet --tier deep --sandbox read-only --dry-run
```

Claude requires an explicit model, retains read-only enforcement, and rejects
Codex-only policy flags.

Dry-run output and private `status.json` include the policy version, class,
risk, ambiguity, choice reason, override source, prior configuration/action,
next action, attempt bound, escalation, and handoff references. Runtime status
separates requested configuration, the model/effort serialized into CLI argv,
and provider-observed model/effort. Observed values remain `null` unless CLI
events expose them.

Each executed attempt records wall time, actual CLI return code, and summed
input/cached-input/cache-write/output/reasoning counts across `turn.completed`
events. A field missing from any completed turn remains unknown, not zero.
The actual Codex `reasoning_output_tokens` key is normalized to `reasoning_tokens`;
reasoning is not added again to output when costing. Worker dollar
cost remains `null` because `codex-lb` pricing is not verified. Optional
coordinator and review usage remain `null`, not assumed zero. CLI completion
never means acceptance: `acceptance.status` remains `unknown` until an
independent validator records its own result. Raw events and stderr stay in
bounded mode-0600 artifacts; receipts contain no SPEC text or credentials.
