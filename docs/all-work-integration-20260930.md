# All-work deployment-baseline integration

The `hermes/all-work` source tree adopts the v2026.9.21 release baseline
already used by the deployed gateway, including the reviewed reliability
artifact `fc9dd5ae691c0f513c4ea1441dd7e97f872ac355`. All 41 files in that
deployed artifact retain their SHA-256 hashes. This integration changes Git
source and history; it does not restart or redeploy the gateway.

## Preservation map

| Existing all-work source | Representation in the resulting tree |
| --- | --- |
| Production `9502b418bd` | Release reintegration `65dc4f1f2d` and its subsequent fixes. See the Discord and memory parity documents below for the scope and existing limitations. |
| SSH synchronization `fdc58c4ed2` | Adapted release port `e09d7413ad`, followed by the exact deployed reliability artifact. Old monolithic terminal implementations are not restored over release siblings. |
| Progress supervision `5581ed71c4` | Cherry-pick `46806274af`, preserving status-card supervision, delivery, evidence, and recovery. |
| Worker policy and advisor `abe7a7a884` | Adapted cherry-pick `1d3880af33`, preserving Sol/Luna worker tiers and the per-run Claude advisor. Reasoning code retains deployed Astra/900k and Daybreak support alongside Sol/Luna support. |
| Graphiti tests `4286aaeef2` | Original commit remains in merge ancestry. Its two tests for the old bundled provider are excluded from the core tree because the provider is now an external plugin. This integration does not claim to validate that external plugin. |

Existing baseline parity limitations remain documented in
[the Discord integration record](reintegration-discord-20260923.md) and
[the memory parity map](reintegration-memory-parity-20260923.md).
The retired direct Discord-to-Mac admission path is not reintroduced.

After these semantic ports, an explicit `ours` strategy merge joins the
existing all-work history to the tested release tree. This is a history
bookkeeping merge, not a claim that a mechanical 216-conflict merge was
resolved file by file. The old all-work head becomes an ancestor, allowing
a normal fast-forward push without rewriting its commits.

## Validation

- The relevant gate ran through `scripts/run_tests.sh` across 54 files:
  861 passed, 5 failed, and 31 skipped. The five failures were old SSH test
  assumptions about constructor-time upload and ownership of shared masters.
- `6882f1b472` adapts only the two affected test fixtures: the selective-sync
  fixture performs the initial upload at the execution boundary through a
  local transport; the teardown test marks its fake master as instance-owned.
  The focused recheck passed all 15 tests in those two files.
- A review finding exposed an off-by-one retry-budget check: `attempt` names
  the run being planned, so the last permitted attempt must still execute.
  The policy now permits that attempt; existing validation rejects attempts
  beyond the budget. Two new boundary regressions and the worker-policy CLI
  gate pass: 44 tests across two files.
- The progress policy now requires a successful terminal worker receipt as
  well as applicable validation before freezing a final snapshot. Three
  regressions cover a still-running worker, a later failure, and later success.
  Existing finalization fixtures now supply a real terminal receipt. The
  bridge test waits for its CLI-completion event instead of any initial card.
  A nine-file gate ran 205 passed / 4 failed; all four fixture failures were
  resolved in the final three-file recheck (72 passed / 0 failed).
- The legacy pinned-effort CLI preserves an explicit pinned model; its new
  dry-run regression passes in that final recheck.
- Composed final coverage is 872 passed, zero unresolved failures, and
  31 skipped. This is a 54-file gate plus focused rechecks, not a second
  clean full-suite run. No full repository suite or production-message
  validation is claimed.
- All 49 paths changed on all-work after `9502b418bd` have a preservation
  classification. All 41 deployed reliability file hashes remain unchanged.
- One comprehensive Magi run targeted the 33-file functional delta from the
  deployed artifact (`ba65e07bcac2ffba6670c32b399a80032d3df327` is a review-only
  snapshot). It exited 3 because the hygiene lane hit a
  `structured-protocol-error`; later lanes and adjudication did not complete.
  Contracts and correctness scopes completed, and partial sweep/hygiene results
  were retained. There were 19 raw findings at 14 distinct file/line locations.
  Four locations were addressed above, including the sole reported blocker.
  This is incomplete review evidence, not a successful full Magi review or a
  claim of zero findings. No repeat review was run to drive the count to zero.

## Retained review warnings

These ten reported warning locations remain outside the focused integration
fixes; no production execution of the imported worker/monitor features is
claimed by this Git publication.

| Area | Reported limitation |
| --- | --- |
| Advisor and runtime evidence | An advisor run clears the nonessential-traffic opt-out as described below. A writable artifact directory allows replacing the event log with a FIFO, potentially blocking runtime-evidence rereading; an event-log read error can leave partial token totals without a completeness marker. |
| Producer/reader bounds | Large valid code scopes can exceed the 32 KB manifest reader; long handoff-reference lists can similarly make a terminal receipt exceed the monitor reader limit. |
| Test evidence | Buffered start/completion events can associate a passed worker test with the poll-time fingerprint rather than the code actually tested. Coordinator validation remains a separate evidence path. |
| Supervisor recovery | A SIGTERM can leave an attention state that blocks explicit reconnect; malformed non-object launch-budget JSON can fail before budget accounting and cause repeated launchd restarts. |
| Test isolation | A fixture Git commit can inherit signing settings; a nested test runner can inherit `HERMES_TEST_SLICE` and select an empty slice. Neither occurred in the recorded local gates. |

The previously accepted reliability limitations remain: a known-unsent
post-claim authority failure can require manual reconciliation; a late
transport error after token invalidation loses its precise cause; repeated
finalization-store failure can reset outage accounting and repeat retries
without the five-failure notice/backoff. Durable completion stays unpublished
when receipt persistence fails.

The imported Claude advisor explicitly clears
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` through per-run `--settings` so
advisor feature-flag fetching can work. It does not edit global settings,
but the opt-in advisor run can re-enable unrelated first-party telemetry.
This review warning is documented rather than removing the imported advisor
behavior during baseline integration.
