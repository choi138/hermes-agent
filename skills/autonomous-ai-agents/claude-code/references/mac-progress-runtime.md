# Mac Claude progress reporting (v3)

Scope: existing parent-managed read-only Claude stream-json adapter on the logged-in Mac. This does not intercept raw CLI/TUI runs.

1. Read the shared operation reference `../../codex/references/mac-effort-progress-runtime.md` for resolved paths, origin-thread allowlisting, private registration, supervisor ownership, trigger policy, verification, uncertain delivery and rollback. Check the selected release/receipt before launch.
2. Use the same `run_codex_task.py` entry with `--cli claude --model <approved installed Claude model> --tier <light|standard|deep|max> --sandbox read-only`. The historical script name supports both CLIs. Do not supply Codex-only task/policy/direct-effort flags. Keep `--spec`, approved workdir/root, private output-dir, finite timeout, progress-manifest/state/thread/label and complete code-scope arguments. Dry-run must return planned before execution.
3. After registration, start `delegation_progress_supervisor.py start -- <the shared bridge arguments>` in terminal background mode with completion notification. The supervisor normally waits; do not use `--no-wait` for ordinary delegation because the coordinator must receive its attention/completion event. Resolve paths once per run.
4. `Read/Glob/Grep` tool events and response arrival are activity evidence only. Claude response text is never trusted as test evidence. An exit0 receipt means the CLI ended, not accepted completion. Set verifying only when real coordinator verification starts; begin a validation ticket before fresh gates, record the actual log/exit status, and require final card + notice ACKs, pending0 and supervisor finished.

The adapter retains read-only `dontAsk`, strict empty MCP configuration, no session persistence and the existing allowed tools. Interactive/write-enabled work uses the established tmux workflow with parent monitoring; do not silently broaden this adapter's permissions.

Live verification on 2026-09-29: Claude Sonnet4.6 read the canary file through Read, returned its token, and the shared reporting path created/edited one Discord card. Final gate/readback receipts are in `hermes-workspaces/progress-v3-20260929/{LIVE_READBACK,SELECTION_RECEIPT}.json`.
