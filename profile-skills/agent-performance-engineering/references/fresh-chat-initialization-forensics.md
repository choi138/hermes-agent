# Fresh-chat initialization forensics

Use when an empty-history gateway chat has slow first answers. Keep model/provider/effort fixed for any controlled comparison; historical samples describe operational behavior, not A/B speedup.

## Evidence order

1. Resolve service PID -> executable/source directory -> profile before reading source. A deployed artifact can have no `.git`; report that and retain relevant source hashes rather than inventing a SHA. Historical turns predating the current PID may have different executing source.
2. Join platform user creation time, gateway inbound, fresh-agent construction, tool-policy completion, turn_context, each API completion/latency, and final-delivery confirmation. Use SQLite message rows to distinguish first-ever user turn from warm conversation, tool calls from text, and later model switches from the model used on the first turn.
3. Read back the exact platform messages. Streaming `created_at` measures first visible content; `edited_at`/delivery completion measures final content. Exclude title/status text from first useful answer. A delayed/backfilled user message must not pollute live-ingress p50/p90; retain it as a separately labeled sample.
4. `API completion - rounded latency` estimates API start, not TTFT. Provider queue, reasoning, network, and generation remain unresolved unless stream-event telemetry exists. Do not manufacture a TTFT from whole-response latency.
5. If pre-API time dominates, profile the same installed initialization functions in an isolated diagnostic process. Measure cold and warm process caches separately; record profile/toolset differences and never call this a full gateway replay. No inference/config/restart is needed for capability-check profiling.

## Capability checks can be the bottleneck

Inspect both registry check-function memoization and outer schema memoization. Distinct browser action closures can defeat per-function deduplication while calling the same backend dependency resolver. Cache scope can vary with profile, registry generation, config-file signature, or execution role; a warm boot cache is not proof that the next fresh chat will hit it.

A superficially cheap schema-time `validate=False` path can still execute through a fallback. Trace nested calls and subprocesses, not comments or function names. Verified example (2026-10-01 koharu): browser schema checks called `_find_agent_browser(validate=False)` -> `_resolve_npx_bin()` -> `node_tool_runnable()` -> `_version_probe_ok()` -> subprocess `npx --version`. A diagnostic schema build took 51.344s; 13 browser checks accounted for 49.848s, while immediate cached reuse took 0.000406s. A separate single-check cProfile measured 3.165s total / 2.410s subprocess version probe. This establishes the mechanism, not a promised 50-second production improvement.

Prefer non-executing schema discovery and one profile/backend-scoped shared prerequisite result, with real validation retained at tool invocation. Do not globally cache browser ownership/credential availability, collapse session security scopes, or lengthen timeouts to hide repeated work. Installing/pinning a direct browser CLI is an alternative only after proving the live fallback and with user approval; it is not the first default fix.

## Disprove convenient explanations

- Auto-loaded skill bodies versus mandatory reloads: inspect the persisted prompt at the sample time. A historical skill_view round may have been necessary; current auto_load may already remove that round.
- Graph memory initialize != recall. Trace both and check trivial/smalltalk gates against the actual gateway envelope, including Korean text. A core English-only trivial check may differ from a plugin's Korean smalltalk gate.
- Title inference may be a daemon-thread sidecar; a contemporaneous title-start log does not prove it blocks the main API.
- Stable prompt tier ordering may already be implemented. Inspect Responses cache_scope/key derivation and first-call cache-read tokens before suggesting prefix reordering. New/reset sessions can intentionally have distinct affinity scopes even with identical text; a routing hint is not a hard guarantee of cache isolation/hit behavior.
- `priority` in config != `service_tier` on the wire != priority honored by an upstream load balancer. Trace each boundary separately, especially custom endpoints.
- Collect host I/O/swap/load alongside function timings. Current pressure can amplify process startup but does not prove the cause of a historical turn; memory available and swap used alone do not establish active thrashing.

Finish with measured stage totals, confirmed mechanisms, unresolved gaps, ranked minimal changes, rollback and a fixed-condition fresh-session validation contract. Configuration and code changes require separate approval when the user requested diagnosis only.
