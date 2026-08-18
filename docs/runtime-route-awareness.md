# Runtime route awareness

## Problem

Hermes has two runtime facts that can drift:

1. The live runtime that will receive the next LLM call
2. The route that policy intended for the current turn

A user-message-only router is not enough. Many requests stay ambiguous until
the agent reads memory, files, session history, web results, or repository
state. A pre-dispatch route can therefore be wrong, and a prior escalation can
be stale on the next ordinary turn. The agent must not infer its current model
from an old session header or memory.

`model_status` remains useful as a diagnostic tool, but it should not be the
normal way for the model to discover which runtime is active. Runtime truth is
supplied in the effective system prompt for every LLM call.

## Principles

### CurrentRuntime and DesiredRoute are different contracts

`CurrentRuntime` is what this LLM call is actually using after provider
resolution, session overrides, fallback activation, and reasoning-effort
resolution.

`DesiredRoute` is what trusted policy intended for this turn. It carries the
route source and strictness so later layers can distinguish user intent from an
automatic policy decision.

### Prompt-time truth beats cached truth

Runtime awareness is built immediately before API dispatch, not when the
cached session system prompt is built. The cached prompt remains byte-stable;
the volatile runtime block is appended at call time so model switches,
fallbacks, and reasoning changes are visible without stale headers.

Provider failover recomposes the same effective prompt, so the retry sees the
new runtime. Summary and post-compression calls use the same composition path.

### Routing is bidirectional

Routing can escalate or downgrade:

- ordinary chat to a development or reasoning runtime
- a development runtime back to ordinary, research, or writing work
- research to code and code back to research
- a fallback target back to primary when recoverable

A stronger model is not inherently a correct route. Stale escalation is still
a mismatch.

### User-strict overrides win

If the user explicitly chooses a model, provider, or reasoning level,
automatic routing must not override it. Route metadata therefore includes its
source and strictness.

## Prompt contract

The runtime block is appended to the effective system message at API-call
time:

```text
# Runtime/Route State
CurrentRuntime: provider={provider_alias_or_type} model={model} reasoning={reasoning_effort} api={api_mode} endpoint={sanitized_base_url} source={runtime_source}
DesiredRoute: label={route_label} target={target_provider}/{target_model}/{target_reasoning} strictness={strictness} confidence={confidence} source={route_source} reason="{short_reason}"
Policy: This block is authoritative for this LLM call; do not infer current runtime from stale session headers, memory, or prior turns. model_status is diagnostic fallback only. Compare CurrentRuntime and DesiredRoute; if mismatched and not user_strict, treat as a routing anomaly before substantive work. Routing is bidirectional and may be re-evaluated after context discovery.
```

This port does not ship the optional `hermes_cli.model_routes` catalog. When
there is no current-turn route decision, the block degrades to
`CurrentRuntime` plus its runtime-truth policy and omits `DesiredRoute`. A
trusted `pre_gateway_dispatch` `runtime_override` still supplies a one-shot
`DesiredRoute`; catalog absence does not hide a route that was actually chosen.

## Phase 1 scope: CurrentRuntime

Included:

- Build runtime state at API-call time rather than cached-prompt build time
- Use the same secret-free state source as `model_status`
- Include model, provider, API mode, reasoning effort, endpoint, and source
- Apply the block to the main loop, summary path, compression rebuild, and
  provider-failover retry
- Preserve the cached stable prompt bytes

Not included:

- learned routing
- post-tool rerouting
- mutating-tool boundary guards
- NEED_CONTEXT scout mode
- ambiguous-verb route heuristics

## Phase 2 scope: DesiredRoute

Included:

- Normalize trusted `pre_gateway_dispatch` `runtime_override` metadata into
  pending per-session route state
- Carry label, target provider/model/reasoning, source, strictness, confidence,
  and reason
- Consume pending state once per gateway message so route intent cannot leak
  into a later turn
- Clear pending state at every conversation boundary through the shared
  conversation-state funnel
- Attach route state to the live `AIAgent` before its conversation loop starts

Not included:

- automatic downgrade execution
- context-discovery reroute hooks
- mutation-boundary enforcement
- user-facing route-correction prompts
- classifier prompt, schema, or regex changes

## Later phases

A later phase can re-evaluate routing after memory, file, web, or repository
inspection and before substantive writes. That requires an evidence model, not
a short ambiguous-verb list. It must account for mixed research/code tasks,
user pins, fallback recovery, streaming turns, retries, tool-only continuation,
and stale cached agents.
