# Hermes completion gate: implementation and rollout scope

Source session: `01a17c93-0f14-7eb0-95b0-4a5b386c7758` (2026-10-27–28, Asia/Seoul).

The implementation separates a child process result from a verified parent task
completion. Authenticated requests, approved plans, current verification evidence,
execution attribution and publication receipts remain distinct and durable.
Unverified legacy launches are recorded without granting parent completion.

## Included changes

- Parent request intake, immutable execution attribution, approved plan binding,
  supersession and verification against current source and artifact bytes.
- Gateway completion modes, trusted intake, managed execution, delivery,
  restart recovery, and profile-scoped result transport.
- Mac launcher compatibility negotiation and legacy execution audit; existing
  Terra policy and report acceptance behavior are preserved during integration.
- Streamed verification hashing avoids reading large evidence files into memory.

## Operational boundary

The session activated **support mode** on Linux and installed the Mac support
launcher. Support mode records execution results and withholds unsupported parent
completion claims. It does **not** enable universal request-to-QA enforcement.
That still requires a real request-specific trusted planner/adapter and grants.
The retained fixture planner must never be installed globally.

The original rollout restarted the gateway and verified ready/running state,
profile connections, plugin registry reads and removal of its owned pause markers.
It interrupted active agents and encountered startup recovery latency; it did not
prove uninterrupted service or fix those pre-existing startup issues.

FIDO source, plugin settings and browser QA are outside this change. The
historical proposal and review below describe intended work, not an assertion that
every proposed capability is active in production:

- [Hermes-only plan](PLAN.md)
- [Plan review](REVIEW.md)

## Integration validation

The source was integrated onto `origin/hermes/all-work` at `631a2965df9f`.
The canonical `scripts/run_tests.sh` runner covers lifecycle, gateway completion,
delivery ledgers, process follow-ups, Mac worker compatibility and home I/O guards.
Checks run with two workers and retries disabled: 908 passed and one cancellation
fixture failed because its fixed timer could fire during launcher startup. The
fixture now waits for an explicit child readiness marker; its full worker test
file passes all 41 tests. Together the final results cover 909 passing tests
across 40 files. Logs are retained in the local evidence directory.

`python scripts/check --staged --base origin/hermes/all-work` also runs the full
lint/health suite. Existing baseline failures in Windows footguns, temporary-path
literals and Ruff are compared against the unchanged branch; no blanket green
lint claim is made. The Mac-only UID calls carry explicit platform annotations.
Code health reports zero blocking findings and zero advisories across 68 files.

Detailed deployment scripts, private rollback material and operational receipts
are retained locally under `hermes-workspaces/completion-gate-20261028`; secrets,
profile databases, environments and test scratch files are not repository content.
The live Linux source and installed Mac source/environment remain in place during
cleanup. Publishing this commit does not restart or redeploy either host.
