# macOS hermes-delegate source integration

This directory derives from the imported deployed macOS coordinator entry in the
fork's `hermes/all-work` branch. The current refactored tree differs from that
snapshot and is not deployed. The installed entry remains unchanged. Editing
this source does not install it, select a runner, change a gateway, or migrate
an in-flight execution.

## Provenance

- Entry source commit: `15dabb25212b5f0a6a152db1394cfc49c72e0123`.
- Runner source commit: `998dde80532745f2a7f9199b79e764bb5f73b772`.
- Integration base: `16f828ac8a62ec0c8e4c107b9f19a5a47671f834`.
- The original six-file snapshot is the source ancestor, not a claim that the
  current files retain its exact bytes. Owner approval `1564567561796980848`
  authorizes this source refactoring for repository quality rules; deployment
  is outside this change. The imported `.gitignore` protections are retained.
- Runner commits are retained unchanged, including their original commit IDs.

## Operating constraints

This is the existing owner-specific macOS entry, not a portable installer. It
retains its pinned absolute macOS runner, interpreter, profile and review-gate
paths. Execute it only on the configured host; copying source elsewhere does not
establish that these external tools or credentials exist.

`hermes_delegate.py` retains executable CLI dispatch. Topical siblings own
common constants/paths/commands, SPEC validation, Codex contracts, runtime
launching, independent review orchestration, Claude/QA preparation, watching
and cancellation, status, and smoke cleanup. Internal callers and tests import
the owning modules directly; the entry is not a re-export shim.

The existing Mac release, runner and interpreter values are unchanged in
`hermes_delegate_common.py`. Helpers use only the standard library and support
system Python 3.9. Git calls have 120-second deadlines; smoke termination waits
up to five seconds after TERM and another five after KILL, failing explicitly
if reaping fails. Binary patch/log output stays binary. SPEC validation still
reads bytes and decodes strict UTF-8 before validation. The original 218 test
class/method identities are retained across split test modules, with six added
deadline and binary-I/O regressions and one signal-fixture regression.

The refactored bytes require independent review and integration verification.
Scope checking is independent pre/post inspection, not a real-time write sandbox.

## Verification commands

Run the entry's isolated unittest suite from this directory with system Python:

```sh
PYTHONDONTWRITEBYTECODE=1 RUFF_NO_CACHE=true /usr/bin/python3 -m unittest discover -s tests -q
```

Run the runner regression slice from the repository root with the configured
Hermes Python interpreter:

```sh
PYTHONDONTWRITEBYTECODE=1 RUFF_NO_CACHE=true HERMES_PYTHON=/path/to/hermes/python /bin/bash scripts/run_tests.sh tests/agent/test_codex_task_runner.py tests/agent/test_codex_worker_policy.py tests/scripts/test_codex_worker_policy_cli.py tests/scripts/test_codex_login_shell.py tests/agent/test_delegation_progress_gateway.py --file-retries 0 -j 4
```

The installed entry and versioned runner release remain separate deployment
artifacts. Test results and the exact integrated commit are recorded in the
coordinator's integration evidence; this document does not assert test success.
