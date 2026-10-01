# Koharu profile-safe progress deployment (2026-10-01)

Approval: Discord message `1555040095239802912`.
Code release: `b604797cf1be5ca3efd62af5b8989c43a05af89b`.
The selected Mac checkout is a clean immutable release. Later documentation-only
commits on `hermes/all-work` do not change its source bytes.

## Selected paths

- Mac selector: `/Users/choegeun-won/Documents/hermes-agent-worktrees/codex-progress-current`
- Resolved Mac release: `/Users/choegeun-won/Documents/hermes-agent-worktrees/koharu-rollout-20261001`
- Mac Python: `/Users/choegeun-won/Documents/hermes-agent/.venv/bin/python`
- SSH host: `choi138-ri`, with BatchMode and StrictHostKeyChecking enabled.
- Server runtime: `/home/justin/hermes-v2026.9.21-reintegration-20260923`
- Server Python: the runtime's `.venv/bin/python`.
- Helper selector: `/home/justin/.local/state/hermes/delegation-progress-current.py`
- Resolved helper: `/home/justin/.local/state/hermes/releases/koharu-rollout-20261001-b604797cf1/scripts/delegation_progress_discord_send.py`
- Helper SHA-256: `7b2543a30b8ac5cb755a73afb607964f9eb8a7491af487bd3157ba77031d7fe1`
- New-run journal: `/home/justin/.local/state/hermes/releases/koharu-rollout-20261001-b604797cf1/delivery`

## New koharu jobs only

Resolve version paths once when launching each job. The runner requires:

```text
--progress-sender-profile koharu --progress-expected-bot-id 1554717538376753244
```

The bridge and supervisor require the matching trusted operator arguments:

```text
--sender-profile koharu --expected-bot-id 1554717538376753244
```

Omission retains the legacy default-profile contract. Do not infer profile from
labels or inherited environment. Existing manifests, journals, resolved paths
and workers remain unchanged. The former Mac release `progress-v5-20260930`
and server helper `progress-v3-20260929` are retained for rollback/new selection.
This is the parent-managed runner workflow, not automatic routing of arbitrary
raw Codex invocations or a new Gateway intake router.

## Evidence and remaining boundary

The integrated canonical gate passed 443 tests with zero failures and one
Linux-only skip on Mac. That Linux readiness case passed the previous Linux gate.
Independent integration review: `deleg_744f1f69`, `APPROVE_SCOPE`.
The selected runner's help parser and bridge dry-run worked; selected-helper
GET-only preflight returned the exact koharu bot and origin thread.
The identical helper candidate already passed live create, same-ID PATCH,
author/content GET and duplicate-free recovery at message `1555037238704869459`.

Private backup/deploy/post-restart receipts live under
`/home/justin/.local/state/hermes/rollouts/koharu-first-response-20261001`.
The post-restart receipt, not this static document, decides whether boot and
runtime-source preparation checks passed. No credentials are copied into Git.
The remaining legacy `레나 검증` template wording is not changed by this rollout.
Preparation timings are not whole Discord first-response latency.
