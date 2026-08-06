# NeMo Relay Shared Metrics

Hermes includes NeMo Relay as a normal runtime dependency on platforms for
which Relay publishes a native wheel. The shared-metrics integration is built
into Hermes and does not require `hermes plugins enable
observability/nemo_relay`. Hermes remains importable without Relay on other
native targets. Those targets use an explicit reduced-capability no-op host:
Hermes execution remains available, while Relay scopes, middleware, plugins,
and subscribers are unavailable. The `hermes-agent[nemo-relay]` extra remains
as a no-op compatibility alias for existing installation commands.

Hermes requires NeMo Relay 0.6.0 or later within the 0.6 release line. That
release establishes the lossless provider-codec contract used for Anthropic
Messages, OpenAI Chat Completions, and OpenAI Responses requests.

## Runtime Dependency and Data Boundary

Hermes installs the platform-specific `nemo-relay` native wheel from the
bounded `>=0.6.0,<0.7` dependency range. The published package is built from
the [NVIDIA NeMo Relay repository](https://github.com/NVIDIA/NeMo-Relay).
Unsupported platforms use the explicit no-op runtime described above rather
than downloading a different implementation.

When Relay managed execution is active, the provider request and response pass
through that native module in the Hermes process so configured interceptors can
operate on the real call. This is separate from the shared-metrics data
contract. Shared-metrics mode installs no network exporter and its subscriber
accepts only the versioned, allowlisted projection described below. Enabling a
separately configured rich-observability or dynamic plugin can create a
different data path and requires its own policy review.

Collection remains off unless Hermes policy enables it:

```yaml
telemetry:
  shared_metrics:
    enabled: true
```

This choice is read from the profile's own `config.yaml`. A machine-managed
configuration overlay cannot enable or disable shared metrics on the profile's
behalf.

The existing `observability/nemo_relay` plugin remains separate. Enable that
plugin only for its opt-in rich observability exporters, adaptive execution,
or dynamic Relay plugins.

Hermes core owns one Relay host and one isolated Relay session scope per Hermes
session. Core lifecycle producers use
`hermes_cli.observability.relay_runtime` to obtain the shared session handle or
run Relay scope, LLM, tool, and mark APIs in that session context. New product
marks do not require Hermes plugin registration. Shared-metrics marks must
still contain only fields approved by the versioned allowlist; the hard
dependency does not change the collection or privacy policy.

## Current Slices

The current vertical slices record logical model calls and top-level task runs:

```text
Hermes turn, API, and tool hooks
  -> Relay session, task, and LLM lifecycle
  -> Hermes shared-metrics subscriber
  -> SQLite counters
  -> immutable JSON delta package
```

Hermes sends an empty `LLMRequest` into the metrics-owned lifecycle. This does
not describe the separate managed-execution call through the native runtime
documented above. The terminal metrics event contains only bounded model
family, provider family, locality, call role, and outcome values. Prompts,
responses, exact model IDs, endpoints, errors, session IDs, task IDs, and
request IDs are not included in the metrics event or package.

Each task run is a Relay `Function` scope named `hermes.task_run`, parented to
the owning Hermes session. The start counter contains only bounded execution
surface and entrypoint values. The terminal counter contains bounded outcome,
end reason, termination status, duration, logical model-call count, terminal
tool-call count, and provider-retry count buckets. Retries are additional
provider attempts for the same Hermes API request ID; they do not inflate the
logical model-call count. Tool calls are deduplicated by their Hermes tool-call
ID after a terminal tool result is observed. The outer `AIAgent` execution
boundary closes the task for normal returns, early returns, exceptions, and
cancellations. Active task ownership follows the task ID if Hermes rotates its
conversation session during context compression.

Local state is written under:

```text
$HERMES_HOME/telemetry/shared_metrics/metrics.sqlite3
$HERMES_HOME/telemetry/shared_metrics/outbox/*.json
```

The database keeps transactional aggregate and package-outbox state. Package
files are immutable delta documents that conform to a closed JSON schema and
are written with atomic replacement. Fully packaged aggregate rows and
successfully exported package rows and files are retained locally for 30 days.
Pending package rows and counters with unexported deltas are never pruned.

### Local observation samples

The same database file additionally holds an `observation_samples` table of RAW
per-event numeric samples used for local latency analysis:

| Metric | Unit | Meaning |
| --- | --- | --- |
| `hermes.model_call.ttft_ms` | ms | True model time-to-first-token: one row per physical wire attempt that produced a first frame, measured from the instant that same attempt's request went out on the wire. |
| `hermes.model_call.duration_ms` | ms | One row per physical wire attempt, success **and** failure, from wire-request-issued to that attempt's terminal. |
| `hermes.turn.first_useful_result_ms` | ms | Earliest of the turn's first successful tool result or first model call that produced assistant text, measured from the turn's earliest observed lifecycle hook. |
| `hermes.compression.duration_ms` | ms | Wall time of one compression attempt, including the auxiliary summariser round trip. |
| `hermes.compression.aux_duration_ms` | ms | Auxiliary-model time inside that window, so compression CPU is separable from summariser latency. |
| `hermes.compression.tokens_before` / `.tokens_after` | tokens | Estimated message-only token counts entering and leaving one compression pass, written in one transaction so they are joinable per invocation. |
| `hermes.model_call.retry_attempt` | count | One row per increment of the conversation loop's `retry_count`, with a bounded `retry_reason`. |
| `hermes.model_call.fallback_activation` | count | One row per successful fallback chain advance; the value is the 1-based fallback ordinal. |

Every row carries `work_lane` (`direct`, `research`, `gjc`, `kanban`,
`delegated`, `unknown`) and `execution_surface`, so the direct, Kanban and
research lanes stay separately labelled. All values are numeric and every
dimension is validated against a closed allowlist on write, exactly like the
counters.

These rows are **never packaged and never exported** — the packaging path reads
only `counter_aggregates`. They are bounded by the same 30-day retention window
plus a hard 250,000-row cap, trimmed by a Relay-independent pass that runs at
most once per UTC day (the Relay export path is not reachable on hosts without
an importable Relay wheel). Recording is gated by
`telemetry.shared_metrics.enabled` **and** the additive
`telemetry.shared_metrics.local_observations` key (default `true`), so raw-sample
retention can be turned off without turning the counters off. Raw samples do
raise the entropy of what is stored locally compared with the bucketed
counters; their exclusion from exports is asserted by a test rather than left
implicit.

Two documented asymmetries apply when comparing raw runs:

* `hermes.model_call.*` rows carry a truthful `call_role`
  (`primary` / `fallback` / `delegated` / `auxiliary`), whereas the exported
  `hermes.model_call.count` counter still hardcodes `call_role="primary"` for
  every call. That exported series is deliberately unchanged, because fixing it
  would shift the denominator of an already-published metric.
* `bedrock_converse` TTFT is botocore-retry-**inclusive** (the Bedrock runtime
  client uses botocore's default retry policy, unlike the OpenAI-wire and
  Anthropic clients which pin `max_retries=0`). Within that
  `api_mode_family` a throttled run and a genuinely slow model are not
  separable, so bedrock p95 TTFT must not be compared across load conditions.

Each package contains an `install_id` generated as a random UUID. Despite the
schema field name, its current scope is one `HERMES_HOME`, so it is more
precisely a persistent pseudonymous profile identifier. It is not derived from
hardware, account, host, path, or credential data. It remains stable across
packages from that profile and can therefore link those local packages.
Deleting `$HERMES_HOME/telemetry/shared_metrics` resets the identifier together
with all aggregates and package files.

This slice has no remote-delivery path. A future remote exporter must not reuse
the persistent local identifier by default. It requires a separate product and
privacy decision covering consent, identity scope, rotation or keyed
pseudonymization, reset behavior, retention, and deletion.

## Smoke Test

Run a real Hermes CLI turn against the deterministic local model server:

```bash
./.venv/bin/python scripts/smoke_nemo_relay_shared_metrics.py
```

The script uses the installed `nemo-relay` dependency by default. Pass
`--relay-python ../nemo-relay/python` only when testing a locally built Relay
binding.

The smoke verifies the model request reached the local server, model and task
counters were stored, one package was exported, and prompt, response, and
exact-model canaries are absent from the package.
