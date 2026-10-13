# Delegation notification rollout

## Problem and behavior

SSH delegation could finish without intermediate reports and then publish a second,
late completion notice. New supported worker launches register a gateway-owned
progress outbox. Meaningful progress uses the existing delivery ledger; foreground
and automatic completion reports share one durable owner and receipt.

An admitted progress head remains pinned until its send settles. Cancelled claims,
dead owners, session cancellation, and uncertain receipts cannot permit a late
progress send to race past completion. Unknown transport outcomes are reconciled
without blind replay. Report-only responses remain intentional silence, while
ordinary rereads and literal Markdown examples remain ordinary responses.

## Compatible releases

Deploy the server gateway changes and the Mac V3 worker/progress changes together.
The gateway admission/ACK protocol requires both sides. Preserve existing model
and effort policy, read-only Claude controls, sender identity, state databases,
outboxes, receipts, and older pinned/manual workers. Mac login-shell bootstrap is
explicit and uses the fixed zsh entry point.

This branch integrates onto `hermes/all-work` at `0e5633b98f`, preserving the newer
SQLite contention fixes. The server candidate is based on `f403f566ca`; the Mac
candidate is based on `b604797cf1`. Older server progress tests are not substituted
for the branch's existing V3 behavior tests.

## Validation and deployment

Canonical integration and adjacent gates use `scripts/run_tests.sh`, per-file
isolation, and disabled retries: **688 passed, 0 failed, 18 host-specific skips**
across 25 files. The integration gate passed 389 cases, the policy/registry/stream
gate passed 201, and the checkpoint/observation/SSH gate passed 98. Existing V3
manual behavior, worker policy, durable report ownership, transport cancellation,
receipt retention, and streaming contracts are covered. `git diff --check` passes.

## Production promotion

The compatible immutable server and Mac releases were promoted on 2026-10-13.
All 28 server files and seven Mac files matched their frozen SHA-256 manifests.
Four profile routing documents were promoted to the dated Mac release path. An
idle gateway restart activated the new server release. The new writer PID,
candidate import path, served profile set, and both profile Discord adapter
connections were verified. Graceful cutover and health verification completed
in approximately 47 seconds. Existing durable state and older pinned releases
were preserved.

Staged Mac CLI gates passed 15 cases. Linux validation passed 152 unique cases
with one skip: four complete files passed 140 cases, and 12 selected durable
follow-up cases passed on unchanged source. The full Linux durable follow-up
file exceeded its timeout and did not complete; its captured failing node passed
in the selected rerun. The selected cases also passed on a temporary RAM
filesystem as a diagnostic, without increasing the unique count. This is not
reported as a successful full-file Linux gate.

No new external Discord smoke message was sent. Actual production progress
ordering and a single completion receipt remain an acceptance check.
