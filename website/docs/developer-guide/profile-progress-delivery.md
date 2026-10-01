# Profile-bound automatic progress

This candidate is for **new runs only**. The sender identity is trusted operator
input: `--progress-sender-profile` and `--progress-expected-bot-id` on
`scripts/run_codex_task.py`, then matching `--sender-profile` and
`--expected-bot-id` on the bridge/helper. Only `default` and `koharu` are allowed.
`koharu` requires an independently audited public bot ID. Task text, labels,
worker output and inherited profile environment never select the sender.

Omission preserves the legacy **default** contract and binding digest. Explicit
`default` opts into the new contract; its expected bot ID may be omitted, in
which case the first observed bot is frozen. Use an audited ID whenever possible.
Never add profile fields to an existing manifest or reuse a historical run ID.

The new contract binds profile, expected/observed bot, thread and run across the
manifest, outbox, SSH argv, payload, creator receipt and private server journal.
The helper loads credentials only on the server from the fixed selected home
(`~/.hermes` or `~/.hermes/profiles/koharu`) using the deployed runtime's
`hermes_cli.env_loader.load_hermes_dotenv`. It clears inherited credentials,
passes no project fallback, disables external secret fetching, and uses the
canonical key-name metadata to require a selected-profile token assignment.
It never parses or prints secret values itself. A machine-wide managed `.env`
overlay is refused before loading: the canonical loader applies that overlay
last, so selected-home-only provenance could not otherwise be guaranteed.
Selected `.env`/`.op.env` assignments to `HERMES_MANAGED_DIR`, `HERMES_HOME` or
`HERMES_PROFILE`, and symlinked profile/credential/config paths, are refused.
Canonical managed-overlay, file-repair and terminal-config hooks are suppressed only during this
short-lived credential load and restored afterward; preflight cannot rewrite
profile files or create profile directories. Canonical credential parsing stays
in use. Post-load scope checks also reject selector syntax the key-name scanner
does not recognize (quoted keys, export with a tab, or a BOM). The legacy omitted-profile route retains its existing trusted default
environment behavior; these new isolation rules require an explicit profile.

Before any new write the helper GETs `/users/@me` and the exact channel. Before
PATCH it GETs the existing card and checks its actual author against the pinned
bot. The write response and final message GET must also match bot, channel,
message ID and content. Allowed mentions remain disabled. Credentials and raw
remote errors never travel to the Mac. New failures contain a fixed reason code
and, only when observed, bounded numeric HTTP/Discord codes.

`--help` and `--dry-run` do not load credentials, connect, or write state.
Bridge `--preflight` performs only server identity/target GETs; it never creates
a journal or ACKs a message. The supervisor performs this preflight before
installing a new profile-bound monitor. Exit 78 stops rejected profile delivery
on the first attempt; the reason appears in the bridge's structured output.
Exit 75 preserves uncertainty. Failed recovery never enables another POST.

The durable journal still records uncertainty before requests and the observed
write message ID before readback. An unknown POST result without an observed
ID remains pending for operator investigation; it cannot be recovered by
guessing an ID or matching content. Known responses use GET-only recovery.
The existing 30-second helper deadline, 30-second card update spacing,
20-minute warning policy, model/effort policy and validation truth are retained.

## Parent deployment and one-card acceptance

These are **parent-only instructions, not executed by the implementation worker**.
Offline fixture results are not evidence of Discord success.

1. Review the candidate and results. Pin the Mac entry points to
   `/Users/choegeun-won/Documents/hermes-workspaces/koharu-progress-profile-20261001/source`.
   Stage only `scripts/delegation_progress_discord_send.py` at a **new versioned
   server candidate path** using the parent's approved transfer route. Verify its
   SHA-256 against `.hermes/profile-progress-results.md`. Keep the installed
   release, aliases, Gateway and existing jobs unchanged. Resolve the actual
   server runtime/Python/SSH host and a new private server journal directory from
   audited operator configuration; do not guess paths or read/export tokens.
2. Create one new private acceptance worktree and manifest using the snippet
   below. Its public destination is thread `1554994350755414139`; its expected
   koharu bot is `1554717538376753244`. Do not use original failed run
   `02d725a15c15469f8d5a2c2fd4fc21fb`, its manifest, outbox or journal.
3. Run the bridge with the explicit candidate paths and `--dry-run`, then
   `--preflight`. Require `ready` with the exact profile, bot, thread and new run.
   A failure stops acceptance before a message write; preserve the reason and
   numeric codes. GET target access alone does not prove permission to POST.
4. Run the same bridge with `--once`: require a `CARD_CREATE` verified receipt.
   Change only the scratch canary source, wait at least 31 seconds, and tick its
   progress state. Save the pending PATCH wire payload using the snippet below.
   Run the bridge with `--once` again. Require `CARD_PATCH`, the same message ID,
   and the same bot/profile/thread/run. Both operations already include actual
   author-checked message GET readback. Then send the saved PATCH payload to the
   candidate helper with `--recover-journal` over the same approved SSH route;
   this must perform GET only and return the same verified receipt.
5. Repeat the GET-only recovery and the bridge `--once` with no new evidence.
   Require no additional message/card and no POST. Only after parent verification
   of the original pre-HTTP failure and actual koharu identity/author may the
   parent assert the notification is solved. Retain receipts and journals.

Use these shell variables only for public paths/IDs, filled from audited operator
configuration. The candidate path is fixed; remote values are intentionally not
invented here:

```bash
PROFILE_PROGRESS_CANDIDATE=/Users/choegeun-won/Documents/hermes-workspaces/koharu-progress-profile-20261001/source
PROFILE_PROGRESS_CANARY=/absolute/new/private/acceptance-directory
# Supply audited SSH_HOST, REMOTE_PYTHON, RUNTIME_ROOT,
# REMOTE_CANDIDATE_HELPER, and PRIVATE_SERVER_STATE before running.
```

Create the acceptance directory privately and export the two path variables to
the local Python process. Run with the candidate's Python; this loads no auth:

```python
import os, subprocess, sys, uuid
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0, os.environ['PROFILE_PROGRESS_CANDIDATE'])
from agent.delegation_progress import Manifest, Progress, _atomic
root = Path(os.environ['PROFILE_PROGRESS_CANARY'])
root.mkdir(mode=0o700)  # Must be NEW and canonical, not an existing run directory.
repo, artifacts = root / 'repo', root / 'artifacts'
repo.mkdir(mode=0o700)
artifacts.mkdir(mode=0o700)
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
(repo / 'canary.py').write_text('value = 1\n')
m = Manifest('profile-canary-' + uuid.uuid4().hex, repo, repo, artifacts,
             '1554994350755414139', '진행 확인', sender_profile='koharu',
             expected_bot_id='1554717538376753244', code_scope=('canary.py',))
Progress(m, root / 'state').tick()
_atomic(root / 'manifest.json', {k: str(v) if isinstance(v, Path) else v
                               for k, v in asdict(m).items()})
print(m.run_id)  # Public run identity only.
```

Use this same base command for `--dry-run`, `--preflight`, and `--once`:

```bash
"$PROFILE_PROGRESS_CANDIDATE/.venv/bin/python" \
  "$PROFILE_PROGRESS_CANDIDATE/scripts/delegation_progress_bridge.py" \
  --manifest "$PROFILE_PROGRESS_CANARY/manifest.json" \
  --state-dir "$PROFILE_PROGRESS_CANARY/state" \
  --ssh-host "$SSH_HOST" --remote-python "$REMOTE_PYTHON" \
  --runtime-root "$RUNTIME_ROOT" --helper-path "$REMOTE_CANDIDATE_HELPER" \
  --server-state-dir "$PRIVATE_SERVER_STATE" \
  --allow-thread 1554994350755414139 \
  --sender-profile koharu --expected-bot-id 1554717538376753244 \
  --once
```

After the create succeeds, prepare exactly one PATCH and its GET-recovery payload
with local source functions. This does not load auth or send anything:

```python
import hashlib, os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ['PROFILE_PROGRESS_CANDIDATE'])
from agent.delegation_progress import Manifest, Progress, _atomic
from agent.delegation_progress_delivery import Delivery
from scripts.delegation_progress_discord_send import validate
root = Path(os.environ['PROFILE_PROGRESS_CANARY'])
m = Manifest.load(root / 'manifest.json')
p = Progress(m, root / 'state')
assert p.peek() is None
(m.worktree / 'canary.py').write_text('value = 2\n')
time.sleep(31)
p.tick()
head = p.peek()
assert head['operation'] == 'CARD_PATCH'
wire = {k: head[k] for k in ('run_id', 'sequence', 'thread_id', 'content', 'operation', 'event_id')}
wire.update(**m.sender_identity(), content_digest=hashlib.sha256(head['content'].encode()).hexdigest(),
            card_receipt=Delivery(p, None)._load()['card'])
validate(wire, {m.thread_id})
_atomic(root / 'patch-wire.json', wire)
```

For GET recovery, feed `patch-wire.json` unchanged to the versioned server helper
through the same SSH transport, using `--runtime-root`, `--delivery-state-dir`,
`--allow-thread`, `--sender-profile koharu`,
`--expected-bot-id 1554717538376753244`, and `--recover-journal`. No credential
argument exists. Do not use direct Discord curl or arbitrary message IDs.

Rollback stops using the candidate **for new runs**. Leave every old job pinned
and every uncertain candidate run journal intact. Never point an active
profile-bound run at the legacy helper or remove its identity fields. No Gateway
restart, alias switch, token operation, permission change or Git publication is
part of this recipe.
