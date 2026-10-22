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
  Approval is recorded on that ticket and authorizes that round only; an approval ID already
  recorded on any ticket in the ledger is refused, so each round needs a new owner message.
- `start --join` adds a parallel reviewer only while the current round has an `open` or
  `running` ticket, up to three total tickets. Its commit must match the round's first ticket.
  `start --fallback` retries the latest closed UNREVIEWED ticket once, using a different CLI
  and the failed ticket's commit. For both flags, one commit SHA must be a prefix of the other.
  These flags are mutually exclusive. A fallback PASS resolves only its linked UNREVIEWED
  ticket. Other unresolved failures prevent PASS; a BLOCK always wins within its round.
- `close` blocker counts follow the verdict: PASS takes `--blockers 0` or no `--blockers`,
  BLOCK requires `--blockers N` (N >= 1), UNREVIEWED requires `--reason`. The coordinator
  records the verdict after reproducing the reviewer's findings; the owner only supplies
  `--owner-approval` for round 3 and later.
- `close --verdict UNREVIEWED --reason 'auth failed'` records a failed review, never a PASS.
  A fallback UNREVIEWED stops the item. Round 2+ BLOCK reaches the cap. Both print an owner
  notice and require approval for another round. Old-round closes preserve the current round.
- `run` claims an open ticket atomically, streams the child without a shell, and records
  execution separately from verdict. Nonzero exits and timeouts still require `close`.
  Timeout kills the child's process group and returns 124. SIGHUP or SIGTERM to `run` kills
  the child's process group, records the ticket as `ran` with exit 128+signal, and returns
  that code, so a cancelled launcher never leaves a `running` ticket. Every ticket must be `closed`
  with a verdict before a new round can start, including `ran` tickets and approved rounds.
- One ticket authorizes one guarded magi execution, only while `running` inside `run`.
  The shim uses `check-ticket --consume T` to claim it atomically; simultaneous claims have
  exactly one winner. A hand-exported `open` ticket or any repeated claim is refused (exit 2).
  `check-ticket T` remains read-only validation of an unexpired `open`/`running` ticket,
  without consuming it. Refused claims are logged as `rejected`; reuse details say `ticket-reuse`.
- The shim allows help/version/`--schema`/`--examples` and `lanes`, `doctor`, `report`,
  `report-plan`, `plan`, `publish`, `sample`, `quality`.
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
127, non-executable children 126, timeout 124, keyboard interruption 130, SIGHUP/SIGTERM to
`run` itself 129/143.

## Deploy, check, and rollback

`hermes/all-work` `scripts/ops/` is the only source for review-gate and the background-continue
hook. Never edit deployed copies in place. Commit changes under `scripts/ops` before applying;
the installer refuses modified, staged, or untracked ops files and records the checkout's HEAD.
Run from the Mac checkout:

```sh
scripts/ops/review-gate/install.sh                       # compare all 8 targets; no writes
scripts/ops/review-gate/install.sh --check               # 0 = clean, 1 = drift, 3 = error
scripts/ops/review-gate/install.sh --apply               # install differences and both manifests
scripts/ops/review-gate/install.sh --host mac --check    # only the 4 Mac targets
scripts/ops/review-gate/install.sh --host server --apply # only the 4 server targets
```

`--host all` is the default; `--server-host` defaults to `choi138-ri`. Server operations use
BatchMode SSH with a 15-second connect timeout and `/usr/bin/python3 -`. For isolated tests,
set `HOME` and `REVIEW_GATE_SSH` (a shlex argv prefix pointing to a fake transport).

The target table in `deploy.py` manages the Mac ledger, Mac audit collector, magi shim and
review-gate wrapper, plus the server's two hooks, audit report and weekly wrapper. Files are
0755; missing parent directories are created as 0700. The default shim and wrapper call the
**deployed** `~/.local/share/review-gate/review_gate.py`. The real magi binary remains
`/Users/choegeun-won/.local/share/uv/tools/magi/bin/magi`; Python remains `/usr/bin/python3`.
`--magi-real-bin`, `--review-gate` and `--python` override rendered paths.

Writes use a destination-local temporary file, fsync, chmod and atomic replace. Symlink
destinations are refused before writing any target on that host. Byte-identical files with the
correct mode retain their mtime. Before replacing different bytes, the installer uses copy2 to
preserve the previous file as `<destination>.bak-<UTC YYYYmmddHHMMSS>`; it never overwrites a
backup. The older `magi.review-gate-backup` is left alone.

Every successful apply writes `~/.local/state/review-gate/deploy-manifest.json` on each selected
host, even if all targets matched. The 0600 manifest records all four absolute target paths,
SHA-256 hashes, modes, source commit and UTC deployment time in a 0700 directory. The weekly
audit compares deployed bytes and modes with these receipts without needing the repository.
A changed review hook causes `hermes hooks doctor` to warn about mtime drift; hooks keep running.
Hook registration, shell-hook approval/allowlist and cron definitions are not managed here.

Rollback: copy the desired `.bak-*` file back with `cp -p`, or run `--apply` from a clean older
commit. Copying a backup back leaves the receipt out of sync until the next installation; the
audit will report that drift. Preserve the ledger to retain review counts.

## Tests

```sh
scripts/run_tests.sh tests/scripts/ops/
sh -n scripts/ops/review-gate/magi-shim.sh
/usr/bin/python3 scripts/ops/review-gate/review_gate.py --help
```

Tests in `tests/scripts/ops/` use temporary Git checkouts, isolated HOME/DB paths and fake SSH/executables;
they clean their fixtures and processes. Test names 01–20 map to the specification's acceptance
rules; `test_r2_*` covers the revised single-execution, commit and verdict rules, including
five consume races of eight processes. ShellCheck must be available at the path above.

## Hermes hook + weekly audit

`hooks/guard_review_gate.py` is the server-side Hermes `pre_tool_call` hook. It reads one
JSON object from stdin, allows with exit 0 and empty stdout, or blocks with exit 2 and an
`{"action":"block","message":"..."}` response. The installer deploys it; hook registration, Hermes configuration and cron definitions remain
coordinator-managed.

The hook blocks unwrapped `codex review`, read-only `codex exec`/`e` (including the default
sandbox), Claude print-mode reviews without write tools/permissions, `claude ultrareview`,
review commands gated by the magi shim, and Python `run_codex_task.py --sandbox read-only`.
It inspects terminal commands, execution-call arguments in Python `execute_code`, and shell
files written through `write_file`/`patch`. Quoted mentions, docstrings, ordinary file writes,
and write-capable implementation sessions are allowed, as are help/version-only calls
(`codex exec --help`, `magi --examples VERB`, `magi --schema`) and the non-review `magi sample`
and `magi quality`. Shell/ssh/utility wrappers (`env`, `sudo`, `nice`, `timeout`, `nohup`,
`screen`, `eval`, `xargs`, `stdbuf`, BSD and util-linux `script`, `tmux` new-session/new-window/
split-window/respawn and send-keys, `npx @openai/codex` and `npx -c`), cmux send/--command text,
evaluated command substitutions, groups, and control-flow prefixes are parsed. A program given
as a variable or substitution (`$CODEX exec`, `"$(command -v claude)" -p`) is matched by its
name and by a value assigned earlier in the same script.
Heredocs (`<<`, `<<-`, including quoted delimiters and multiple bodies) are attached to
their receiving command. Bodies are data unless an unquoted delimiter permits command
substitutions, or the receiver is a shell without `-c` (including through ssh). Here-strings
(`<<<`) follow the same receiver rule. A ticket-wrapped command's own bodies are exempt;
commands after the bodies are checked independently. Missing delimiters or excessive nesting
use the existing scoped fallback. In Python argv lists/tuples, the program must be a constant string, a name bound once to one,
or a variable/attribute whose name matches a reviewer; other dynamic arguments, including
starred elements, become `__dynamic__` placeholders. `import subprocess as sp`, `from
subprocess import run`, `import os as o` and `from hermes_tools import terminal as t` aliases
are followed.
Local `codex --help`, `codex exec --help`, and `claude --help` were checked; compatibility
spellings required by the policy (`--full-auto`, `--yolo`, `--oss-provider`, and
`--ask-for-approval`) remain recognized alongside current options.

Block messages route Codex and Claude reviews to `hermes-delegate review --reviewer codex|claude`
(which issues the ticket and wraps the launch), Claude implementation to `hermes-delegate claude`,
auth checks to `hermes-delegate smoke codex|claude`, and magi reviews to the ledger commands below.

The tokenizer consumes plain and quoted runs with precompiled patterns, never slices the
remaining text, and the hook process runs without cyclic GC. A 0.5 MB script dense with
redirections took 15.8-20 s on the server before this and failed open at the 10-second hook
timeout. The server parses about 7x slower than the Mac, so the parse is also bounded: a shell
text over 256 KiB, or a parse still running after 0.25 s, goes to the line fallback instead.
The fallback blocks any unwrapped reviewer launch on a single line, keeps the same-line
`review-gate run --ticket` and `REVIEW_GATE_NOT_REVIEW=` forms, and logs `parse: "budget"`.
With these limits the whole hook process takes about 0.2-0.55 s on the server for any input
up to 1.1 MB. Typical scripts, which are well under 64 KB, still get the full parse. The fallback
differs from the full parse in both directions: it blocks a launch whose wrapper sits on an
earlier line, and it misses launches that only the parser sees, such as a program held in a
variable or a name split across a line continuation.

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
The installer deploys the standalone report on the server and the ledger plus collector on the Mac.
The report includes a `배포 드리프트` section and prints Korean plain text,
at most ten newest `other` sessions, one `fido` count, bounded bypass reasons, and a weekly
`REVIEW_GATE_NOT_REVIEW` bypass metric (this week, of which smoke/auth reasons, and the previous
seven days) that tracks the goal of halving bypasses once `hermes-delegate smoke` exists. Mac failure
still prints the hook summary and error class/exit code, and exits 0. Cron/Discord delivery
belong to the coordinator.

Known gaps: launches hidden in script files the coordinator did not write through
`write_file`/`patch` cannot be inspected; other agents' and humans' reviews are seen only by
the audit. Codex `--ephemeral` leaves no session file, so it is visible only in the hook log.
Shell aliases and functions, command text assembled at run time, and Python programs computed
by calls (`shutil.which("codex")`) or f-strings are outside the static parser. The hook is a policy guard, not a shell interpreter or security boundary.

## Limitations

- This is a local policy guard, not a security boundary against the local account: the account
  can edit the DB, invoke the real binary, or supply a bypass. Discord IDs are format-checked
  and recorded; ownership/content are not verified offline. The shim checks eligibility,
  not reviewer/commit/argv identity.
- Verdicts, reviewer names, failure reasons and bot passes are supplied by the caller. There
  is no automatic reviewer selection, fallback execution, verdict interpretation or owner
  notification transport. Owner notices go to stderr; `audit` is the recovery view.
- SIGKILL of the ledger process (or a crash) can still leave a running ticket; inspect and
  close it explicitly. Tickets expire for shim entry, not for recording a verdict. Use one shared DB
  to retain counts across sessions; changing/deleting the DB loses that history.
