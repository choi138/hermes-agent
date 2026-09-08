---
title: Codex progress registration and Discord bridge
---

The opt-in chain is runnable: `run_codex_task.py` registers a durable local
baseline before Codex starts; `delegation_progress_bridge.py` observes it and
sends safe Korean text over SSH stdin; a separately staged
`delegation_progress_discord_send.py` POSTs to Discord and GETs the exact
message back. Nothing starts on import. The default runner argv, effort,
sandbox, authority and single final status record are unchanged.

## Mac launch

Live reporting requires separate operator approval and server staging.
Implementation tests used local fixtures, not real SSH or Discord.
Supply these six operator inputs in order: approved absolute SPEC path inside
the worktree, numeric thread ID, verified SSH host or user@host, absolute
remote Python path, absolute remote Hermes repository root, absolute staged
helper path. No token is requested or copied to the Mac.

```bash
cd /Users/choegeun-won/Documents/hermes-agent-worktrees/codex-progress-integration-20260908
PROGRESS_WORKTREE="$PWD"
read -r PROGRESS_SPEC
read -r PROGRESS_THREAD
read -r PROGRESS_SSH_HOST
read -r PROGRESS_REMOTE_PYTHON
read -r PROGRESS_RUNTIME_ROOT
read -r PROGRESS_HELPER_PATH

umask 077
mkdir -p "$HOME/.local/state/hermes-progress"
PROGRESS_SESSION="$(mktemp -d "$HOME/.local/state/hermes-progress/run.XXXXXX")"
mkdir "$PROGRESS_SESSION/artifacts"
PROGRESS_MANIFEST="$PROGRESS_SESSION/manifest.json"
PROGRESS_STATE="$PROGRESS_SESSION/state"

.venv/bin/python scripts/run_codex_task.py \
  --spec "$PROGRESS_SPEC" --workdir "$PROGRESS_WORKTREE" \
  --allowed-root "$PROGRESS_WORKTREE" --output-dir "$PROGRESS_SESSION/artifacts" \
  --sandbox workspace-write --tier standard \
  --progress-manifest "$PROGRESS_MANIFEST" --progress-state-dir "$PROGRESS_STATE" \
  --progress-thread "$PROGRESS_THREAD" --progress-label '진행 보고 통합' \
  --dry-run
```

Use the sandbox/tier already approved for the task. Remove `--dry-run` to
launch in a foreground terminal or a terminal tool with `background=True`.
The runner flushes a `progress_registered` JSON record **before** spawning
Codex, including UUID run ID, known manifest path, `baseline_complete` and
fixed coverage error codes. The usual final status follows on exit.

The baseline and event cursor are fsynced before exclusive manifest
publication. The fresh events file is empty at registration, and short
events are flushed while the worker runs. Existing manifests are never
replaced. Invalid routing/state I/O/git/event baseline fails before launch,
with nonzero status (`registration_failed` for callback failure). Inventory
limits and excluded symlinks are explicitly partial coverage; their durable
gaps appear in registration output and reports. Omitted paths that become
visible later are not retroactively claimed as edits.

After registration, run the bridge in another foreground terminal or
`terminal(background=True)`, preserving/reassigning the same exact variables:

```bash
.venv/bin/python scripts/delegation_progress_bridge.py \
  --manifest "$PROGRESS_MANIFEST" --state-dir "$PROGRESS_STATE" \
  --ssh-host "$PROGRESS_SSH_HOST" --remote-python "$PROGRESS_REMOTE_PYTHON" \
  --runtime-root "$PROGRESS_RUNTIME_ROOT" --helper-path "$PROGRESS_HELPER_PATH" \
  --allow-thread "$PROGRESS_THREAD" --dry-run
```

Remove `--dry-run` for delivery. Defaults: 300-second reports, 10-second polls,
40-second SSH timeout, 24-hour foreground lifetime. CLI success/failure and
explicit user-wait state are eligible next poll, independent of the report
cadence. Collection and in-flight sends add bounded latency. CLI success
means **“Codex 실행 종료, 레나 검증 대기”**; reporting continues during review.
Reporting failure never kills the worker.

## Server helper and credential boundary

The parent stages **one file**, `scripts/delegation_progress_discord_send.py`,
at the operator's helper path. It is stdlib-only; the supplied server Python
must already have the Hermes runtime's dependencies. No runtime source
change, service, restart, or cron is needed. The bridge executes the equivalent
of this command **on the server**, with validated message JSON on stdin:

```bash
"$PROGRESS_REMOTE_PYTHON" "$PROGRESS_HELPER_PATH" \
  --runtime-root "$PROGRESS_RUNTIME_ROOT" --allow-thread "$PROGRESS_THREAD"
```

`--help` and `--dry-run` never load credentials or use HTTP. The sending path
pins `HERMES_HOME` to `~/.hermes`, rejects inherited nondefault profile
markers, and uses the existing named
`hermes_cli.send_cmd._load_hermes_env()` opaquely with loader output suppressed.
Only the named `DISCORD_BOT_TOKEN` is obtained for sending. Do not run that
credential-loading path on the Mac. Tokens never enter Mac argv/stdin/stdout,
manifests, outbox, receipts or logs.

Local SSH is fixed `/usr/bin/ssh`, `shell=False`, `BatchMode=yes`,
`ConnectTimeout=10`, `StrictHostKeyChecking=yes`. Every fixed remote argv
element is shell-quoted. Existing verified trust is required; no SSH config
or host-key changes occur. Host, remote Python, runtime root, helper path and
allowlist are explicit operator arguments, never manifest/log instructions.
Combined SSH output is bounded to 16 KiB, and stderr is never forwarded.
The helper has a 30-second overall deadline, 10-second HTTP socket timeouts,
bounded stdin and response bodies, and no redirects.

POST targets only `https://discord.com/api/v10/channels/ID/messages`.
Threads are channels: no channel-name lookup, Home fallback, forum creation,
attachments or media interpretation. The payload explicitly contains:

```json
{"content":"safe rendered text","allowed_mentions":{"parse":[],"replied_user":false}}
```

Then GET `/channels/ID/messages/MESSAGE_ID` must match exact content, channel
and POST message ID. The response contains only status, run ID, sequence,
thread ID, content digest and message ID. Empty/malformed JSON, extra keys,
`skipped=true`, failed process exit, ID mismatch or readback failure cannot
acknowledge delivery. No remote exception, HTTP body or header is returned.

## Recovery and explicit stop

Use one stable state root per run. Lifetime watcher fencing prevents
concurrent bridge instances; a separate delivery lock fences send/receipt/ack.
Before sending, the bridge fsyncs `uncertain`. After exact readback it fsyncs
`(run_id, sequence, thread_id, content_digest, message_id)` **before** ack.
Restart after ack failure retries ack only. A Discord message ID cannot
satisfy two different sequences. There is **no exactly-once or guaranteed
at-least-once claim**.

| Result | Concrete policy |
| --- | --- |
| Verified receipt, failed local ack | Restart same bridge/state: ack only, no POST. |
| Explicit POST rejection: 400/401/403/404/405/413/429 | Pending retained, exit 75. Operator fixes cause or waits for rate limits, then explicitly restarts. No automatic retry loop. |
| Timeout, connection loss, empty/malformed response, server error, readback failure, crash before receipt fsync | Pending `uncertain`, exit 75. Restart alone never POSTs this sequence again. |
| Operator locates exact earlier message ID | Restart with `--reconcile-message ID`: GET only for the pending head. Content/channel/ID must match. Failed reconciliation stays uncertain. |
| Uncertainty cannot be reconciled | Leave pending and report unresolved delivery. Never delete/reset state to force a repost. |

All pending messages, including the final notice, must be verified and acked
before exit 0. A failed final delivery exits nonzero; restarting resumes
delivery even if the collector has already stopped. SIGINT/SIGTERM reports
interruption and preserves state without signaling the worker. The finite
`--max-runtime` exits 75 with preserved state, plus any bounded current work.

Explicit reporting stop (does not stop Codex):

```bash
.venv/bin/python scripts/delegation_progress.py set-stage \
  --manifest "$PROGRESS_MANIFEST" --state-dir "$PROGRESS_STATE" --stage stopped
```

Use `--stage verifying` during coordinator review and `--stage final_verified`
only after acceptance. `--dry-run` does not mutate the manifest. Neither CLI
success nor filenames/test output/model prose can set final verification.
The coordinator may atomically set `cli_status: needs_user` from a trusted
wait signal. Routing identity, roots and artifact paths remain bound.

```bash
.venv/bin/python scripts/delegation_progress.py snapshot \
  --manifest "$PROGRESS_MANIFEST" --state-dir "$PROGRESS_STATE" --format text
.venv/bin/python scripts/delegation_progress.py peek \
  --manifest "$PROGRESS_MANIFEST" --state-dir "$PROGRESS_STATE"
```

`snapshot` and all `--dry-run` paths are read-only. `tick` persists evidence;
legacy `watch` only fills the outbox and can exit with a final notice pending.
Use the **bridge** for delivery and final-ack shutdown. Manual `ack --id`
remains an operator tool; do not substitute it for verified delivery.

## Factual Korean evidence and limits

Git status, index/HEAD IDs and source fingerprints distinguish new files,
content edits, staging, commits, reverts and deletion. Timestamps and line
counts are insufficient. A valid unborn symbolic branch is observable with
an empty or populated index, through staging and the initial commit.
Preexisting staged content remains baseline, not new progress. Invalid HEAD,
missing commit objects and failed git commands remain unavailable.
At most five safe relative source names accompany
actual observed operations, for example:

```text
• agent/runner.py: 파일 내용 변경.
• tests/test\_pin.py: 테스트 파일 추가.
```

Names identify files, **not implemented behavior**. Task labels remain local
metadata; per-file arbitrary prose labels are not accepted. No prompt,
command text, reasoning, raw stdout or source bytes are rendered. An ASCII
path allowlist, secret-name/credential-pattern filtering and Markdown
escaping suppress unsafe names. Mentions, controls, URLs and MEDIA syntax
fall back to `안전한 이름 표시 불가`; paths never become uploads. Unreadable
evidence says `상태 확인 불가`, not unchanged. Default maximum is 1,200
characters, with disclosure of omitted names or truncated text.

Only matching command-completion events supply test results. Direct pytest
and exact canonical `bash scripts/run_tests.sh -j 2 tests/...` (or direct
`scripts/run_tests.sh`) are recognized with restricted simple arguments.
Canonical success requires integer exit 0 and exactly one complete summary
at 100% completion, positive passes, zero failures; skips are reported.
Canonical trailing statistics are allowed. Echoes, quoted source, compound
commands, incomplete summaries and unsupported wrappers remain unknown.
Identifying a newer test attempt is separate from trusting its result:
unsupported pytest options, malformed arguments and known execution wrappers
invalidate an older pass immediately on start; an ambiguous completion stays
unknown. Text printed by echo/printf or supplied as Python source is not a
test invocation. Matching item IDs and command fingerprints prevent older
overlapping completions or mismatched start/completion commands from restoring
a stale pass. Completed-only events remain supported, with the existing
bounded event-dedup window. Arbitrary custom wrappers whose executable intent
cannot be identified are outside this parser's coverage.
The last individual test result never accepts later edits or whole-task
coverage. Runner events are trusted artifacts, not cryptographic attestation.

Each observed `running` → `needs_user` episode queues immediately at the next
poll, including before an earlier wait notice is acknowledged and across
watcher restarts. The same wait is deduplicated; transient read failures do
not prove resumed work. A complete running observation rearms the next wait.
Terminal failure/completion dedup, monotonic sequence IDs, pending FIFO and
exact-head acknowledgment remain independent of the repeatable wait state.

Bounds remain: 1,024 files, 512 KiB/file, 8 MiB source bytes/observation;
4 MiB combined git output and 10 seconds/git command; 1 MiB JSONL increment;
32 KiB manifest/worker status; 16 MiB state; 32 pending messages. Current
changes are prioritized. State uses private 0700 directories, 0600 files,
atomic replace and fsync. Symlinks, special/hardlinked files, secret/config
internals, caches and raw logs are excluded. Partial coverage is disclosed;
edits wholly reverted between polls are not observable. Same-UID writers of
manifests/state/runner artifacts are trusted; this is not an OS sandbox.

Tests use disposable git repositories, actual runner/bridge CLIs, a fake
Codex child, a fixture SSH executable and HTTP connection/socket fixtures.
The implementation evidence directory includes generated examples for new
files, unchanged, failed tests, pending review, unavailable, and final
verified. Live transport and server credentials remain for a separately
approved operator check.
