# Hardening Read-Only External Context Providers

Use this pattern when an agent augments its prompt with historical facts from a graph, vector store, RAG service, or MCP server. The goal is **selective, bounded, non-authoritative recall**—not a second instruction channel and not a hidden write path.

## Security and precedence invariants

1. **Context-only surface**
   - Expose no provider mutation schemas to the model.
   - Keep the provider's dispatcher on one constant, audited read call.
   - Require the configured server tool set to contain the needed search operation and to be a subset of a strict read-only allowlist.
   - Reject non-loopback endpoints and URLs with embedded credentials when the design contract is local-only.

2. **Explicit authority order**
   - Current user instruction wins over current built-in profile/memory.
   - Current built-in profile/memory wins over external historical recall.
   - External recall is informational background, never proof of current state.
   - The outer memory fence must say this too; a provider-local disclaimer is insufficient if the shared wrapper calls all memory authoritative.

3. **Treat every returned field as untrusted**
   - Scan fact text for prompt injection, role labels, context-fence delimiters, secret-shaped values, and multilingual variants relevant to the deployment.
   - Sanitize relation names and provenance IDs separately; metadata can carry delimiter or newline injection even when fact text is clean.
   - Render accepted facts as one line so benign multiline input cannot create a new prompt role or list item.

## Selective recall contract

Recall only when the current turn depends on history, preferences, constraints, or prior decisions.

- Strong continuity terms may trigger directly.
- Ambiguous temporal terms should require a work/project/session anchor.
- A sanitized, length-bounded session title may enrich a generic continuation query.
- Suppress recall when the current message corrects, replaces, excludes, or newly declares a preference or requirement. Failing closed to no recall is safer than injecting a stale conflict.
- Optional identity aliases can keep high-signal preference/requirement relations attached to the intended user while still allowing generic `the user` facts and query-overlapping project decisions.

## Client-side filtering and budgets

Do not trust server ranking, invalidation, or `max_results` alone.

- Exclude invalidated, expired, malformed, and not-yet-valid facts.
- Exclude transient operational status that must be live-verified.
- Exclude ingestion artifacts such as message subjects, sender/recipient relations, and unrelated email/newsletter facts.
- Require lexical/query-anchor overlap for low-signal relations; reserve identity-scoped exceptions for durable preferences, requirements, prohibitions, and decisions.
- Deduplicate repeated graph facts and exact facts already present in built-in memory.
- Enforce local caps on candidate count, injected fact count, per-fact characters, and total context characters.
- Parse common nested MCP wrappers (`result`, structured content) defensively; malformed payloads return empty context.
- Bound latency locally with a daemon worker plus a single-flight guard. Timeout, dispatcher failure, or an already-stuck call returns empty context without blocking the turn.

## Vertical TDD matrix

Close one behavior at a time with an observed RED followed by the smallest GREEN:

1. provider discovery and `get_tool_schemas() == []`;
2. mutation-capable allowlist rejected;
3. remote or credential-bearing endpoint rejected;
4. continuity query dispatches exactly the one read-only search;
5. unrelated query performs no search;
6. current correction/declaration performs no search;
7. invalid/expired/future and transient-status facts excluded;
8. ingestion noise and irrelevant low-signal facts excluded;
9. prompt injection, secret values, role labels, delimiters, and malicious provenance excluded;
10. built-in and graph-local deduplication;
11. fact-count, per-fact, and total-character budgets;
12. timeout, exception, malformed JSON, and nested MCP response fail-open behavior;
13. sanitized session-scope enrichment and optional identity scoping;
14. memory-manager integration preserves an informational fence and exposes no mutation tools.

After each focused GREEN, run the provider file. At the end, run the shared memory-manager, turn-context/API-sidecar, context-scrubber, plugin-schema, lint, compile, added-line secret, and static mutation-boundary checks.

## Live rollout and rollback

1. Fetch and integrate onto the exact long-lived deployment ref before final broad review.
2. Verify the live MCP server is healthy, local-only, and configured with the exact read-only allowlist—without printing credential values.
3. Back up live config and any provider settings with restrictive permissions.
4. Activate the provider, then run a fresh-process deterministic canary that:
   - registers only the intended MCP server;
   - loads the selected provider through normal discovery;
   - executes a history-dependent query;
   - asserts bounded output and at least one safe provenance ID;
   - prints counts/IDs, never recalled fact bodies.
5. When useful, add a tool-disabled one-shot agent canary to prove the context reaches the model-facing turn.
6. If the gateway must restart itself, launch an out-of-process transient verifier first. It should wait for PID change/readiness, rerun the deterministic canary, write an atomic report, and restore the backup plus restart again on failure.
7. Report exact deployed SHA, focused/regression exits, backup path, old/new process identity, canary evidence, and whether rollback was merely prepared or actually exercised.

## Pitfalls

- A read-only server configuration is not enough if the provider exposes a generic dispatch or mutation schema.
- A provider header saying “non-authoritative” is defeated by an outer wrapper saying “authoritative reference data.”
- Search ranking can return semantically plausible but unrelated entities; apply local relevance and identity gates.
- Historical `running`, `queued`, `blocked`, or `completed` facts are not live operational evidence.
- Secret policy facts may be safe, but secret **values** and credential-bearing metadata must never enter the prompt.
- Do not turn malformed or slow recall into a user-visible failure; continue without external memory.
- Do not use post-turn sync to write back into a provider whose contract is read-only.
