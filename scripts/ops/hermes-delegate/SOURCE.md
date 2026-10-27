# macOS hermes-delegate source snapshot

This directory preserves the deployed macOS coordinator entry as source in the
fork's `hermes/all-work` branch. Importing it does not install it, select a runner,
change a gateway, or migrate an in-flight execution.

## Provenance

- Entry source commit: `15dabb25212b5f0a6a152db1394cfc49c72e0123`.
- Runner source commit: `998dde80532745f2a7f9199b79e764bb5f73b772`.
- Integration base: `16f828ac8a62ec0c8e4c107b9f19a5a47671f834`.
- The six imported files (`.gitignore`, `README.md`, `hermes_delegate.py`,
  `scope_check.py`, and both files under `tests/`) retain the entry commit's
  exact Git blobs and modes. This provenance document is the only added file.
- Runner commits are retained unchanged, including their original commit IDs.

## Operating constraints

This is the existing owner-specific macOS entry, not a portable installer. It
retains its pinned absolute macOS runner, interpreter, profile and review-gate
paths. Execute it only on the configured host; copying source elsewhere does not
establish that these external tools or credentials exist.

The six source files are intentionally imported without refactoring. Their
previous implementation reviews remain applicable to those identical bytes;
source relocation and the combined runner contract require integration checks.
Scope checking is independent pre/post inspection, not a real-time write sandbox.

## Verification commands

Run the entry's isolated unittest suite from this directory with system Python:

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q
```

Run the runner regression slice from the repository root with the configured
Hermes Python interpreter:

```sh
HERMES_PYTHON=/path/to/hermes/python /bin/bash scripts/run_tests.sh tests/agent/test_codex_task_runner.py tests/agent/test_codex_worker_policy.py tests/scripts/test_codex_worker_policy_cli.py tests/scripts/test_codex_login_shell.py tests/agent/test_delegation_progress_gateway.py --file-retries 0 -j 4
```

The installed entry and versioned runner release remain separate deployment
artifacts. Test results and the exact integrated commit are recorded in the
coordinator's integration evidence; this document does not assert test success.
