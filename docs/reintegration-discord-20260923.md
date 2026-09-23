# Discord-first v2026.9.21 reintegration

This branch starts from the official `v2026.9.21` tag (`d337b736aa`). This
branch is not deployed. The production checkout was observed at
`9502b418bd` on 2026-09-23; recheck its SHA immediately before any rollout.

## Production contract observed on 2026-09-23

- Multiplexed gateway profiles: `default`, `anju`, `raiden`, `shinei`.
- Default profile uses `memory.provider: graphiti_canonical` and an enforced
  `model_routes` router with `dev` and `chat` routes. Both routes use the
  configured `codex-lb` provider with one fallback each.
- Gateway Kanban dispatch is enabled for the default profile; other profiles
  have their own configurations. The separate company Discord bridge runs as
  its own service.
- Production config and services have only been inspected, not modified.

## Completed on this branch

- Ported the Discord typing-task ownership fix from `9fbefcae91` into the
  new adapter. Disconnect cancels channel loops; old cleanup cannot untrack a
  replacement; a disconnecting adapter cannot start a new loop.
- `scripts/run_tests.sh -j 4 tests/gateway/test_discord*.py` passed:
  56 files, 353 tests, zero failures.
- Ported the profile-bound read-only MCP capability used by the staged external
  Graphiti provider. Raw YAML is checked without interpolation, loopback URLs
  and explicit `follow_redirects: false` are required, hidden MCP tools stay
  out of model schemas, and the exact live server/session/registry/config are
  attested before and after a deadline-bound call.
- A synthetic profile loaded the external `graphiti_canonical` plugin through
  `plugins.memory.load_memory_provider`; safe config was available and a
  redirect-enabled config was rejected. No production plugin was installed.
- On 2026-09-23, the live Graphiti service and Neo4j connection both reported
  healthy through the read-only Graphiti CLI. A temporary SSH local forward
  connected an isolated profile to the live `/mcp` endpoint at a loopback IP
  literal. The real Hermes MCP discovery registered exactly five allowlisted
  tools, exposed zero of them to the model, and loaded the staged external
  provider. Its deadline-bound `search_memory_facts` path returned `ok` with
  19 candidates; `prefetch` produced a nonempty recall without an error status.
  No recall content, credentials, or production configuration were changed.
- Installed the three-file external plugin candidate into an isolated local
  staging home at `../hermes-graphiti-staging-20260923/plugins/graphiti_canonical`;
  installed file hashes match the candidate. Its config enables only the
  loopback Graphiti MCP connection and no messaging platforms. With an
  ephemeral SSH local forward, `hermes gateway run` started and reported a
  running PID; gateway startup discovered the five allowlisted Graphiti tools.
  From the installed provider path, discovery exposed zero MCP tools to the
  model, `search_memory_facts` returned `ok` with 19 candidates, and `prefetch`
  returned nonempty context without an error status. The test gateway shut
  down cleanly and the SSH forward was stopped; no service was installed.
  This proves startup/MCP/plugin recall in the staging profile, not a live
  Discord turn or LLM response. The isolated profile has no Discord token or
  Nous authentication. The local release interpreter also warned about its
  SQLite 3.50.4 WAL bug (it used DELETE journal mode) and missing optional
  `snowballstemmer`; neither blocked this Graphiti check.
- `scripts/run_tests.sh -j 4` across eight focused MCP test files passed:
  173 tests, zero failures. These cover live/lazy/adopted hidden registration,
  no-follow HTTP redirects, profile isolation, deadline-bound calls, and
  rejection when session, registry, or tool provenance changes.
- Reintegrated the default profile's `model_routes` catalog and validation,
  gateway pre-dispatch routing and shadow evaluation, mood handling, route-first
  fallback, and passive provider-health recording. The turn event is forwarded
  into the runner so shadow evaluation can observe the actual inbound turn.
  These paths have automated coverage, but no real Discord/LLM turn yet.
- Fixed Discord adapter shutdown when typing state has not been initialized.
  Corrected two Gateway tests that depended on host disk space or a shared
  virtual environment instead of their isolated fixtures.
- Earlier full Gateway sweep (before the later mention-inbox and health ports):
  `scripts/run_tests.sh -j 12 tests/gateway/ -q --tb=short --disable-warnings`
  completed across 993 files in 1004.8 seconds: **9,926 passed, 0 failed,
  34 skipped**. This supersedes the earlier incomplete baseline sweep, but
  does not validate subsequent changes to this working tree.
- A subsequent focused rerun of model routing, route fallback, config, and
  Discord typing tests passed: **377 passed, 0 failed, 1 skipped** across four
  files. `git diff --check` is clean and `git ls-files -u` contains zero paths.
- Read-only SSH inspection of the production host found the multiplexed
  gateway running with `default`, `anju`, `raiden`, and `shinei` served and
  aggregate Discord platform state `connected`. A secret-free projection of
  the actual production `model_routes` configuration passed this branch's
  validator with `chat` and `dev` routes, `enforce` mode, and zero issues.
  The three per-profile gateway-state files have old timestamps, so they are
  not independent proof of each bot's current connection.
- The host has no separate staging home or staging gateway service. Its
  `~/.hermes/deploy-staging/lifecycle-20260908` directory contains an older
  deployment's artifacts, not a staging Discord bot. The local isolated
  Graphiti staging home has no `.env`, no profile directories, and
  `platforms: {}`. Reusing a production bot token concurrently for staging
  risks duplicate inbound processing and is not an acceptable live gate.

## Re-opened validation and production parity gate

- After the completed sweep above, the branch gained additional Discord
  mention-inbox, agent-health, model-routing, and Gateway wiring changes. A
  fresh full Gateway sweep is required against the final tree; the earlier
  9,926-pass result is historical evidence only.
- Production `9502b418bd` and the release tag `d337b736aa` diverge at
  `5a8e8a6b87`: production has 85 unique commits and the tag has 13,055.
  Patch-ID comparison does not establish that production's capabilities are
  already present in the new branch.
- The current worktree now contains `agent/task_lifecycle/`,
  `agent/codex_task_runner.py`, `agent/delegation_progress.py`, and
  `agent/reasoning_pin.py`. The earlier absence claim is obsolete. Focused
  lifecycle, reasoning-pin, and runtime-control tests passed. The newer-tree
  full Gateway sweep completed across 1,005 files in 977.9 seconds:
  **10,030 passed, 6 failed, 35 skipped**. The six failures were in
  `test_choice_picker.py` (1), `test_compress_command.py` (1),
  `test_display_null_turn_wiring.py` (3), and
  `test_warning_wiring_conservation.py` (1). An empty-session `/reasoning`
  persistence bug was fixed, and three test doubles were updated for the
  current persistence/callback contracts. A focused rerun of six affected
  test files passed **31 passed, 0 failed**. A later full sweep reached
  **10,050 passed, 18 failed, 35 skipped**; 17 failures involved direct
  `session_store` access and one was a shutdown time bound. Focused fixes
  passed, but a fresh complete sweep is still required as the release gate.
- Production's normal Discord sessions apply `gateway/tool_policy.py` and
  expose one authenticated `kanban_task` intake surface through
  `tools/kanban_intake_tool.py` and `hermes_cli/kanban_intake.py`. These paths
  and `agent/request_footprint.py` are now present on this branch. The ported
  policy was adjusted to the release's current tool names and file-write
  schema, rather than restoring retired `cross_profile` or append contracts.
  `scripts/run_tests.sh` passed the policy, intake CLI, and intake tool files:
  **56 passed, 0 failed**. This is focused proof, not the final Gateway or
  real-Discord acceptance gate.
- The `notes_write`, `notes_read`, and `memory_propose` tools, journal, write
  pipeline, and taint controls are now wired on this branch. Focused Notes
  wiring passed **207 tests, 1 skipped**; memory WAL/L0, failure recording,
  profile A→B→A, and boundary markers passed **174 tests**. Production's
  optional `agent/ingest_curator.py` is still absent, but
  `curator.ingest_enabled` is `False` in all four observed profiles. Recheck
  that flag at deployment; read-only Graphiti recall alone is not write proof.
- `agent/turn_resume.py`, durable active-turn integration, and its Gateway
  delivery producers are now present. Their focused gate passed **78 tests**.
  This does not substitute for final full Gateway or live interruption/restart
  acceptance.
- SSH prompt context was ported and focused tests across six files passed
  **192 tests, 1 skipped**. A probe-only SSH backend reached the production
  host, observed cwd `/home/justin`, and performed no file sync. Shutdown and
  browser-broker focused tests passed **42 tests** after replacing a
  load-sensitive 0.75-second test bound with the repository's 2-second floor.
- Production's enabled background review process-wide single-flight and
  successful-foreground-turn eligibility policy are ported alongside the
  release's managed-local idle queue. The focused background-review and turn
  lifecycle gate passed **752 tests, 0 failed** across 25 files. Semantic
  Discord progress and Kanban evidence/worker contracts are also ported;
  their focused Discord/Kanban/Graphiti gate passed **263 tests, 0 failed,
  2 platform skips** across 13 files. These are focused proofs, not a full
  Gateway or live-Discord acceptance result. `smart_model_routing.enabled`
  remains `False` in all four observed profiles, so the absent GJC coordinator
  is not an active route.
- The staged external Graphiti provider preserves the production provider
  body with compatible `NotesStore` and `tools.mcp_tool_readonly` imports.
  The candidate and isolated installed copy have matching hashes. A live
  Graphiti call through the integrated `MemoryManager` returned 691 characters
  of fenced recall inside the default eight-second deadline (1.76 seconds);
  five MCP tools remained hidden from the model. Its focused tests passed
  **9/9**. This does not yet prove a live Discord/LLM turn or the provider's
  long-tail latency.
- The final full Gateway sweep after the Kanban test-contract correction passed
  across all 1,007 files: **10,069 passed, 0 failed, 35 skipped** in 846.1
  seconds (`scripts/run_tests.sh -j 12 tests/gateway/ -q --tb=short
  --disable-warnings`). The skipped OS-specific tests require their Linux or
  Windows lanes; this local macOS result does not cover those lanes. A later
  compatibility check found 41 in-tree references to revert-scheduled plugin
  pointers. Those were replaced with defining-module imports or direct test
  state checks; `scripts/check_compat_pointers.py` now reports zero references
  across its 2,087-pointer manifest. The five affected test files passed
  **63 tests, 0 failed, 1 strict xfail**. Because that fix touched a production
  Kanban tool, a second full Gateway sweep ran against the final code tree:
  **10,069 passed, 0 failed, 35 skipped** across 1,007 files in 958.0 seconds.
  Linux- and Windows-only cases remain for their respective OS lanes.
- All 22 files under `plugins/mention_inbox/` in the current worktree have
  exactly the same Git blob hashes as production `9502b418bd`, including the
  GitHub and Notion collectors. This proves that plugin's files were copied,
  not that its Gateway startup/config wiring works on the new release.
- Production's direct Discord-to-Mac Codex admission commit was reverted at
  `9502b418bd`; do not resurrect it as part of preservation. The durable
  task lifecycle beneath it remains present and must be accounted for.

**Do not deploy or restart the production bot from this branch until the
rollback restore test and reviewed commit are complete.** Focused
parity evidence is not a live acceptance result. The user approved use of the
existing bot for a controlled live canary, not loss of its current capabilities.

## Release gates still open

1. The final full Gateway rerun after the Kanban compatibility fix passed:
   **10,069 passed, 0 failed, 35 skipped** across 1,007 files. The earlier
   one-failure sweep was caused
   by an obsolete expectation that `TERMINAL_ENV` be absent; the dispatcher
   deliberately pins `TERMINAL_ENV=local` for workspace safety. Do not
   resurrect the reverted direct Discord-to-Mac intake.
2. Finish and verify the rollback snapshot of production code, configuration,
   and active SQLite state for all four profiles. The old runtime and service
   units have been copied without changing the running gateway; the live DB
   backups and restore verification are in progress. Check the separate
   company Discord bridge and establish the exact old-service restoration
   path before touching the live gateway.
3. Review and commit the reintegration branch. Prepare a separate runtime on
   the server without starting a second gateway with the existing bot token.
4. The user approved a controlled canary on the **existing bot** instead of
   creating a staging bot. Only after gates 1–3 pass, perform a time-boxed,
   single-gateway rollout (never run a second process with the same bot token),
   install the external plugin, and verify real Discord/LLM turns, Graphiti
   recall, model routes, Kanban intake/dispatch, progress and recovery, all
   four profile identities, and the separate bridge. Roll back immediately
   on failed checks. No live canary or service restart has happened yet.

## Single-process cutover and rollback plan

The old executable is
`/home/justin/hermes-main-runtime-reintegration-20260803/.venv/bin/python`.
The existing user unit and its
`90-main-runtime-reintegration-20260803.conf` drop-in remain in place.
Their copies are under
`/home/justin/.hermes/backups/discord-reintegration-20260923-h8bZ4H/systemd/`;
the old checkout and four-profile configuration are also copied in that backup.
The separate `hermes-company-discord-bridge.service` remains outside this
cutover.

Prepare the new Linux runtime in a separate directory without a Discord
token. Only one gateway process may use the existing bot token. After Linux
preflight and the backup restore drill pass, add
`95-v2026.9.21-reintegration.conf` to the existing gateway unit to override
`ExecStart`, `ExecStopPost`, `PATH`, and `VIRTUAL_ENV` for the new runtime.
Use one `systemctl --user daemon-reload` and one
`systemctl --user restart hermes-gateway.service`; no parallel gateway or
second bot process is allowed. Verify the effective `ExecStart` and PID before
testing Discord.

On a failed canary, move only that 95 drop-in to the backup's `systemd/`
directory, reload systemd, and restart `hermes-gateway.service`. Confirm the
effective executable points back to the old runtime, the four profiles and
Discord reconnect, and the company bridge stays active. Move the newly
installed `~/.hermes/plugins/graphiti_canonical` directory aside as well;
the old runtime has its own bundled provider. Preserve the 90 drop-in and
the old runtime. Restore any `state.db` only if a data migration
has made code-only rollback insufficient: stop the gateway first, then use
the verified profile-specific backup. A DB restore discards writes made after
the snapshot; it is not part of normal code rollback.

Do not interpret the earlier baseline sweep's termination at 19.3% as a full
pass. It stopped with 1,913 passed and zero failures but no final summary; the
completed sweep above is the release-test evidence.
