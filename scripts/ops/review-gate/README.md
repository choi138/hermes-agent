# review-gate

Local review ledger and a POSIX magi entry guard. Python 3.9+, SQLite, standard library only.
No model calls, network, or installation are needed to test this repository.

```sh
./review_gate.py open --item change-123 --goal 'Fix the login redirect'
T=$(./review_gate.py start --item change-123 --reviewer magi --commit abc1234)
./review_gate.py run --ticket "$T" --timeout 600 -- magi review --target HEAD
./review_gate.py close --ticket "$T" --verdict BLOCK --blockers 1 --evidence ./review.txt
./review_gate.py status --item change-123
./review_gate.py audit --since 2026-10-19 --json
```

Use `--help` on any command. `start` prints only the ticket ID; `status --json` includes full
ticket IDs, timestamps, execution details, and recorded evidence/reasons.

## Rules

- Two rounds **per item across pre/post-PR**, processes, CLIs and bot passes. Reopening an ID
  is refused. Every further round requires `start --owner-approval <15–22 digit Discord ID>`.
  Approval is recorded on that ticket and authorizes that round only.
- `start --join` adds a parallel reviewer only while the current round has an `open` or
  `running` ticket, up to three total tickets. Its commit must match the round's first ticket.
  `start --fallback` retries the latest closed UNREVIEWED ticket once, using a different CLI
  and the failed ticket's commit. For both flags, one commit SHA must be a prefix of the other.
  These flags are mutually exclusive. A fallback PASS resolves only its linked UNREVIEWED
  ticket. Other unresolved failures prevent PASS; a BLOCK always wins within its round.
- `close --verdict UNREVIEWED --reason 'auth failed'` records a failed review, never a PASS.
  A fallback UNREVIEWED stops the item. Round 2+ BLOCK reaches the cap. Both print an owner
  notice and require approval for another round. Old-round closes preserve the current round.
- `run` claims an open ticket atomically, streams the child without a shell, and records
  execution separately from verdict. Nonzero exits and timeouts still require `close`.
  Timeout kills the child's process group and returns 124. Every ticket must be `closed`
  with a verdict before a new round can start, including `ran` tickets and approved rounds.
- One ticket authorizes one guarded magi execution, only while `running` inside `run`.
  The shim uses `check-ticket --consume T` to claim it atomically; simultaneous claims have
  exactly one winner. A hand-exported `open` ticket or any repeated claim is refused (exit 2).
  `check-ticket T` remains read-only validation of an unexpired `open`/`running` ticket,
  without consuming it. Refused claims are logged as `rejected`; reuse details say `ticket-reuse`.
- The shim allows help/version and `lanes`, `doctor`, `report`, `report-plan`, `plan`, `publish`.
  Everything else needs an unconsumed running ticket issued less than six hours ago, a bypass
  reason of at least eight characters, or a resolved `--repo-dir` strictly below
  `$HOME/.fido-automation/`. Exemptions are logged; only explicit development bypasses may
  continue on logging failure (with a warning). Original capture/PATH behavior is preserved.

DB: `$REVIEW_GATE_DB`, default `~/.local/state/review-gate/ledger.sqlite`. Newly created parent
directories have mode 700. WAL, a 10-second busy timeout and `BEGIN IMMEDIATE` serialize writes.
Timestamps are UTC ISO-8601. `audit` lists events, stopped items and open/running/ran tickets
older than six hours; `--since` filters events by UTC date while always showing current issues.
Events include detail (argv summary or ticket refusal), cwd, `$USER`, and timestamp.
Reuse refusals appear in both plain and JSON audit output with `ticket-reuse` in their detail.

Exit codes: **0** success, **2** policy refusal, **3** usage/validation/storage error,
**4** item/ticket not found (`check-ticket` returns **2** for unknown tickets).
`run` returns the child's exit code instead; signal exits use 128+signal, missing executables
127, non-executable children 126, timeout 124, keyboard interruption 130.

## Install and rollback (coordinator only)

```sh
./install.sh                           # print destination and complete shim; no writes
./install.sh --apply                   # atomically write; preserve existing shim in a backup
# Override --output, --magi-real-bin, --review-gate, --python for isolated testing.
```

Default output: `~/.local/bin/magi`; real binary:
`/Users/choegeun-won/.local/share/uv/tools/magi/bin/magi`; Python: `/usr/bin/python3`.
The rendered shim points to this checkout's `review_gate.py`; keep it at that path.
Installer refuses symlink destinations and refuses to replace an existing backup. Reapplying
identical content is a no-op. Roll back an existing installation with
`cp -p ~/.local/bin/magi.review-gate-backup ~/.local/bin/magi`. If no shim existed before
installation, remove only the installed shim. Keep the ledger to preserve review counts.

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -v
sh -n magi-shim.sh && /opt/homebrew/bin/shellcheck magi-shim.sh
/usr/bin/python3 review_gate.py --help
```

Tests use repository-scoped temporary directories, a temporary HOME/DB, and fake executables;
they clean their fixtures and processes. Test names 01–20 map to the specification's acceptance
rules; `test_r2_*` covers the revised single-execution, commit and verdict rules, including
five consume races of eight processes. ShellCheck must be available at the path above.

## Hermes hook + weekly audit

`hooks/guard_review_gate.py` is the server-side Hermes `pre_tool_call` hook. It reads one
JSON object from stdin, allows with exit 0 and empty stdout, or blocks with exit 2 and an
`{"action":"block","message":"..."}` response. The coordinator registers/deploys it;
this repository does not change Hermes configuration, cron, or installed commands.

The hook blocks unwrapped `codex review`, read-only `codex exec`/`e` (including the default
sandbox), Claude print-mode reviews without write tools/permissions, `claude ultrareview`,
review commands gated by the magi shim, and Python `run_codex_task.py --sandbox read-only`.
It inspects terminal commands, execution-call arguments in Python `execute_code`, and shell
files written through `write_file`/`patch`. Quoted mentions, docstrings, ordinary file writes,
and write-capable implementation sessions are allowed. Shell/ssh/utility wrappers, cmux
send text, evaluated command substitutions, groups, and control-flow prefixes are parsed.
Heredocs (`<<`, `<<-`, including quoted delimiters and multiple bodies) are attached to
their receiving command. Bodies are data unless an unquoted delimiter permits command
substitutions, or the receiver is a shell without `-c` (including through ssh). Here-strings
(`<<<`) follow the same receiver rule. A ticket-wrapped command's own bodies are exempt;
commands after the bodies are checked independently. Missing delimiters or excessive nesting
use the existing scoped fallback. In Python argv lists/tuples, a constant string program is
required; dynamic arguments, including starred elements, become `__dynamic__` placeholders.
Local `codex --help`, `codex exec --help`, and `claude --help` were checked; compatibility
spellings required by the policy (`--full-auto`, `--yolo`, `--oss-provider`, and
`--ask-for-approval`) remain recognized alongside current options.

Open the item once, start a ticket for each authorized review, and wrap the whole launch:

```sh
review-gate open --item change-123 --goal 'Fix the login redirect'
T=$(review-gate start --item change-123 --reviewer codex --commit abc1234)
review-gate run --ticket "$T" -- codex exec -s read-only 'Review the diff'
# Or: /usr/bin/python3 /path/to/review_gate.py run --ticket "$T" -- <command>
```

The hook does not inspect the wrapped command after `--`; other shell segments still need
their own wrapper. Ticket validity is checked on the Mac by `review-gate run`, not by the
server hook. Smoke tests and research can use an explicit reason scoped to that launch:

```sh
REVIEW_GATE_NOT_REVIEW="auth smoke" codex exec -s read-only 'Reply AUTH_OK'
env REVIEW_GATE_NOT_REVIEW=research claude -p 'Explain this API'
```

Detected launches append JSONL to `$REVIEW_GATE_HOOK_LOG`, default
`~/.hermes/profiles/koharu/logs/review-gate-hook.jsonl`, with file mode 0600. Only timestamp,
session/tool, decision, reviewer kind, ticket, bypass reason (200 characters), parser status,
and exception class are recorded; commands, source, and file contents are never logged.
Wrappers use reviewer `unknown` because their child is intentionally opaque. Literal tickets
and `$VAR` references are preserved; command-substitution tickets are null to avoid logging
command text. Unrecognized calls are not logged. Shell parse failures use conservative,
line-scoped regex detection; internal errors are logged and allowed. Invalid stdin is ignored.

The Mac collector streams session JSONL and reads ledger data through the same interpreter:

```sh
/usr/bin/python3 audit/review_audit_mac.py --since 2026-10-13 --json
/usr/bin/python3 audit/review_audit_report.py --since 2026-10-13
```

`REVIEW_GATE_PY` selects the ledger script (default
`~/.local/share/review-gate/review_gate.py`); `REVIEW_GATE_DB` retains its normal meaning.
The new `audit --run-context --json` query opens the existing SQLite DB read-only and adds
execution windows plus items exceeding two rounds without changing the schema or exposing
run argv. Existing audit output/behavior without that flag is preserved. The original
ledger has no run cwd: those runs match on time only; a supplied run cwd must contain the
session cwd at a path-component boundary. Session starts match from three minutes before
run start through three minutes after run end (unfinished runs extend to the audit time).

`CODEX_SESSIONS_DIR` overrides `~/.codex/sessions`; dated files since the cutoff are
review-like when their first sandbox policy is read-only. `CLAUDE_PROJECTS_DIR` overrides
`~/.claude/projects`; files modified since the cutoff are review-like with `entrypoint:
"sdk-cli"` and no assistant Edit/Write/MultiEdit/NotebookEdit calls. These are heuristics:
read-only research and smoke sessions may also appear. Unmatched sessions below
`~/.fido-automation` or `~/Documents/fido-wt` are grouped as `fido`; others as `other`.
Ledger reporting includes bypass/legacy-exempt/rejected/reuse events, stopped items,
rounds above two, and tickets unclosed for more than 24 hours.

The server report defaults to seven days ago in UTC, reads the hook log, and invokes:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=15 geunwon-mac /usr/bin/python3 \
  ~/.local/share/review-gate/review_audit_mac.py --since YYYY-MM-DD --json
```

`REVIEW_AUDIT_MAC_CMD` overrides that argv-style command; `{since}` is replaced with the UTC
date. It is split with `shlex`, without a local shell. The Mac step times out after 120 seconds.
Deploy the standalone report on the server and the updated ledger plus collector on the Mac.
The report prints Korean plain text,
at most ten newest `other` sessions, one `fido` count, and bounded bypass reasons. Mac failure
still prints the hook summary and error class/exit code, and exits 0. Cron/Discord delivery
and installation belong to the coordinator.

Known gaps: launches hidden in script files the coordinator did not write through
`write_file`/`patch` cannot be inspected; other agents' and humans' reviews are seen only by
the audit. Codex `--ephemeral` leaves no session file, so it is visible only in the hook log.
Dynamic command construction, aliases, and imported Python execution aliases are outside
the static parser. The hook is a policy guard, not a shell interpreter or security boundary.

## Limitations

- This is a local policy guard, not a security boundary against the local account: the account
  can edit the DB, invoke the real binary, or supply a bypass. Discord IDs are format-checked
  and recorded; ownership/content are not verified offline. The shim checks eligibility,
  not reviewer/commit/argv identity.
- Verdicts, reviewer names, failure reasons and bot passes are supplied by the caller. There
  is no automatic reviewer selection, fallback execution, verdict interpretation or owner
  notification transport. Owner notices go to stderr; `audit` is the recovery view.
- Killing the ledger process externally can leave a running ticket; inspect and close it
  explicitly. Tickets expire for shim entry, not for recording a verdict. Use one shared DB
  to retain counts across sessions; changing/deleting the DB loses that history.
