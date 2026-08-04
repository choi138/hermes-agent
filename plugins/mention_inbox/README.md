# Unified Mention Inbox event contract

This package defines the versioned domain contract shared by GitHub, Slack, and
Notion mention collectors. The P3-M2 core remains a GET-only GitHub Notifications
collector with a profile-scoped SQLite store; the bounded gateway integration,
Discord work threads, and separately gated execution path are described below.
External writes are never authorized by source content alone.

## Public API

- `ingest_event(payload)` validates an adapter-produced ingress object. It
  always creates a `pending` event and computes the dedupe key internally.
- `event_to_dict(event)` and `event_to_json(event)` produce the canonical
  storage representation.
- `restore_event(payload)` validates only the canonical storage
  representation. It accepts persisted approval state and verifies the stored
  dedupe key against the immutable identity fields.
- `transition_approval(event, state)` returns a new event for one of the two
  allowed local decisions.

All contract dataclasses are frozen. Serializers clone and revalidate mutable
metadata before returning it.

## Canonical fields

| Field | Type | Rule |
|---|---|---|
| `schema_version` | string | Exactly `"1"` for this contract. |
| `source.platform` | enum | `github`, `slack`, or `notion`. |
| `source.event_id` | string | Immutable identity for one source mention event. |
| `actor.actor_id` | string | Stable platform actor ID, never a display name. |
| `actor.kind` | enum | `user`, `bot`, `app`, or `unknown`. |
| `target.target_id` | string | Stable mentioned user or team ID. |
| `target.kind` | enum | `user` or `team`. |
| `thread.thread_id` | string | Stable conversation/object identity. |
| `thread.container_id` | string or null | Stable repository/channel/page container ID. |
| `requested_action` | enum | `reply`, `review`, `acknowledge`, `investigate`, or `unknown`. |
| `deadline` | RFC 3339 string or null | Must include an offset; stored in UTC with `Z`. Never grants execution authority. |
| `dedupe_key` | string | Computed `umi:v1:<sha256>` identity; ingress cannot supply it. |
| `approval_state` | enum | `pending`, `approved`, or `rejected`; ingress always creates `pending`. |
| `untrusted` | object | Raw external text, links, labels, and JSON metadata. |

Stable IDs must be non-empty strings without surrounding whitespace or control
characters. All string fields and metadata keys/values must be valid UTF-8 text;
isolated Unicode surrogates are rejected. Mutable labels, display names,
titles, permalinks, and bodies must not be used as stable IDs.

The ingress and canonical objects use exact key sets. Optional values are
represented by `null`; their keys are not omitted. Nested objects also reject
missing and extra keys.

## Trust boundary

External content is data, not an instruction channel.

`UntrustedPayload` contains:

- `title: string | null`
- `body: string`
- `action_detail: string | null`
- `source_url: string | null`
- `metadata: JSON object`

Keys such as `instructions`, `system_prompt`, `developer_prompt`, `tool`,
`permission`, or `approval_state` inside `metadata` remain untrusted strings and
objects. Their names do not grant meaning or authority. Arbitrary Python
objects, non-string object keys, tuples, NaN, and Infinity are rejected.

`requested_action` and `deadline` are normalized adapter classifications. They
may describe an external request, but neither authorizes a tool call, reply, or
write. Only a local approval transition can change `approval_state`, and M1 has
no external action consumer.

There is intentionally no `to_prompt()` helper. A later LLM consumer must place
raw content in a clearly delimited data block under fixed trusted instructions;
it must not interpolate external text into system or developer instructions.

## Ingress versus storage

| Property | `ingest_event` | `restore_event` |
|---|---|---|
| Caller | GitHub/Slack/Notion adapter | Trusted local event store only |
| Shape | Ingress exact key set | Canonical exact key set |
| Accepts `dedupe_key` | No; extra key is rejected | Yes; recomputed and compared |
| Accepts `approval_state` | No; extra key is rejected | Yes; enum is revalidated |
| Initial approval | Always `pending` | Restores persisted state |
| Raw webhook/poll payload | No; adapter must normalize first | Never |

A JSON string must be decoded into an object before `restore_event` is called.
Do not call `restore_event` on a webhook body, polling response, Discord
interaction, or any other untrusted ingress path.

## Dedupe invariant

`build_dedupe_key(source, target)` hashes canonical UTF-8 JSON containing only:

1. schema version,
2. source platform,
3. source event ID,
4. target kind, and
5. target ID.

The output is `umi:v1:` followed by 64 lowercase hexadecimal SHA-256 digits.
Actor details, thread context, body, title, metadata, requested-action detail,
deadline, and approval state cannot change the key. A different source event or
target does change it.

This makes source retries and mutable-content refreshes idempotent for the same
target inbox item. Storage restore rejects any key that does not recompute to
the same value.

## Expected adapter identity mapping

GitHub is implemented by P3-M2; Slack and Notion remain constraints for M3/M4.

| Source | `source.event_id` | `actor.actor_id` | `target.target_id` | Thread/container |
|---|---|---|---|---|
| GitHub | Notification/event stable ID | User or app node ID | User/team node ID | Issue/PR node ID; repository node ID |
| Slack | Events API `event_id`, not a delivery envelope ID | Slack user/bot/app ID | User or user-group ID | `thread_ts` or event `ts`; channel ID |
| Notion | Comment ID, block ID, or page ID + property ID | Notion user ID, or `unknown` when unavailable | Owner Notion user ID | Discussion/block/page ID; page/database ID |

For Notion polling, content revision time belongs in the M2/M4 store cursor and
must not be folded into the v1 event identity. The target ID is already a
separate dedupe input, so adapters must not duplicate it inside
`source.event_id`. Multiple observations of the same target mention in the same
stable Notion object remain one inbox item.

If an adapter cannot classify an actor or requested action confidently, it must
use the explicit `unknown` enum rather than inventing a new value.

## Approval state machine

```text
                 ┌──────────┐
                 │ pending  │
                 └────┬─────┘
                      │
             ┌────────┴────────┐
             ▼                 ▼
       ┌──────────┐      ┌──────────┐
       │ approved │      │ rejected │
       └──────────┘      └──────────┘
          terminal           terminal
```

Allowed transitions:

- `pending -> approved`
- `pending -> rejected`

No-op transitions, reversal, and every transition from a terminal state are
rejected. The function is pure: it returns a new event and performs no I/O.
Approval actor identity, audit records, Discord UI, and any later external
action belong to P3-M5.

## Versioning

Version `1` is strict and is never silently reinterpreted. Bump the schema
version when changing required fields, enum meaning, stable identity mapping,
dedupe inputs or encoding, trust placement, or approval semantics.

A future version must add explicit migration/dual-read behavior before stores
containing older events are accepted. Until then, unknown versions fail closed.
Do not change the `umi:v1:` computation in place.

## P3-M1 boundary

Included:

- pure stdlib domain types and validation,
- deterministic dedupe,
- storage round-trip and tamper detection,
- local approval transition semantics,
- hostile-input regression tests.

Not included:

- GitHub, Slack, or Notion API calls,
- credentials or configuration,
- SQLite/event cursors,
- plugin/CLI/gateway registration,
- Discord Approval Inbox,
- LLM summarization or reply drafting,
- any acknowledgement, comment, review, message, or external write,
- deployment, gateway restart, or live canary.

## P3-M2 GitHub Notifications pilot

### Components

- `github_client.py` is a GET-only stdlib client for GitHub REST API version
  `2026-03-10`.
- `github_collector.py` filters and normalizes notification fixtures into the
  v1 contract.
- `store.py` persists canonical events, source revisions, cursors, and
  secret-safe collector health.
- `runtime.py` executes one bounded poll. It does not sleep, schedule itself,
  register with the gateway, or start a background process.

The public construction sequence is explicit:

```python
client = GitHubNotificationsClient(token=token_from_trusted_launcher)
target_id = client.get_authenticated_user_id()
collector = GitHubNotificationCollector(
    target_id=target_id,
    allowed_repositories={"silviahealth/content"},
)
store = MentionInboxStore()
poller = GitHubMentionPoller(client=client, collector=collector, store=store)
result = poller.poll_once()
```

The package never reads an environment variable. An operational launcher may
pass the existing named `GITHUB_PAT_TOKEN` without exposing its value, but token
selection and secret loading remain outside this package.

### Authentication limitation

The authenticated-user Notifications endpoint currently requires a classic
personal access token with `notifications` or `repo` scope. GitHub's official
REST documentation states that this endpoint does not support fine-grained PATs
or GitHub App user/installation tokens. Subject enrichment can additionally
require repository access. A future credential change must pass the same live
read-only gates; it must not silently widen repository or write permissions.

### Read request allowlist

The client can issue only these requests:

- `GET https://api.github.com/user`
- `GET https://api.github.com/notifications?participating=true&per_page=50`
- pagination URLs with the exact same HTTPS origin and `/notifications` path
- subject enrichment for exact
  `/repos/{owner}/{repo}/issues/{number}` or
  `/repos/{owner}/{repo}/pulls/{number}` paths

Look-alike hosts, userinfo URLs, HTTP URLs, fragments, other subject paths, and
subject repositories that do not match the accepted notification repository are
rejected before transport. `subject.url` 404 uses the no-detail fallback; it
does not trigger a broader lookup.

There is no method for notification read-state mutation, thread subscription,
issue/comment creation, PR review submission, reaction, acknowledgement, or any
other external write.

### Selection and mapping

The collector requires an explicit trusted-repository allowlist. By default,
repositories outside that list are rejected. When
`include_public_actionable_activity=true`, public pull requests from other
repositories may enter hydration, but they are accepted only after the current
subject proves exact user targeting or exact ownership. P3-M2 hydrates the
current GitHub subject and emits only current, user-directed actions:

- an explicit direct mention in the selected comment/review -> `reply`
- a current direct review request whose notification reason is `review_requested`
  -> `review`
- a current direct assignment whose notification reason is `assign` ->
  `investigate`
- an external human comment, review, or change request on the target user's own
  pull request -> `reply` or `investigate`
- a non-empty inline review comment or submitted review summary on the target
  user's own pull request from exactly one of `coderabbitai[bot]`,
  `chatgpt-codex-connector[bot]`, `openai-codex[bot]`, or `codex[bot]` ->
  `reply` or `investigate`

All other bot/self activity, sticky reviewer/assignee state on unrelated
notifications,
missing/deleted hydration, CI/state/subscription noise, and unverified team
activity fail closed. Team mentions and team review requests are disabled by
default; when explicitly enabled they still require verified active team
membership. With team mentions disabled, pull-request `team_mention` candidates
are hydrated only far enough to distinguish the narrow allowlisted AI review
activity above; ordinary team activity remains suppressed and performs no team
membership lookup.

`notification.id` is `source.event_id`. `updated_at` is kept only as the
out-of-band `source_revision`; it is deliberately absent from the canonical
content and dedupe input. Issue/PR and repository node IDs provide thread and
container identities. Missing/deleted subject detail falls back to an unknown
actor, a notification-derived thread ID, empty body, and no source URL.

Raw title, body, reason, URL, and JSON metadata remain under `untrusted`.
Neither GitHub response data nor a forged field can set approval state.

### SQLite behavior

The default file is profile-scoped at:

```text
$HERMES_HOME/mention_inbox/inbox.db
```

The store enables WAL, a bounded busy timeout, and owner-only `0600` file mode
where the platform supports POSIX permissions. The dedupe key is the event
primary key. Store schema version 10 binds each Discord proposal message to an
immutable `approval_offered` boolean and each approved execution to its verified
PR head repository, head branch, and absolute workspace root. Migration of
older proposal rows defaults `approval_offered` to false; an older execution
without the new scope fields cannot be resumed under a newly configured scope.

- First observation inserts revision 1 in `pending` state.
- Same content updates source revision and `last_seen` without duplicating the
  row or overwriting a local approval.
- Changed canonical content increments the revision and replaces the event in
  `pending` state, invalidating any prior approval.
- Older source revisions cannot overwrite newer content or approval.
- `Last-Modified` is persisted separately as the collector cursor.
- Review summaries, inline review comments, and issue comments are selected
  before administrative timeline events. Labels, commits, and cross-references
  cannot hide a semantic event merely because they have a later timestamp.
- Up to 20 semantic events in the bounded 10-minute notification activity
  window are classified independently. An inline comment already represented
  by an actionable review summary is folded into that review's preapproval
  brief instead of creating a duplicate alert.

GitHub title/body/URL fields are bounded before canonical persistence (500/4000/
500 characters). Sent event payloads are pruned after the configured conservative
retention period (30 days by default); pending deliveries are retained, and the
payload-free delivery audit ledger remains after event pruning.

### Polling and failure policy

- The first page sends the stored `If-Modified-Since` value exactly.
- `304 Not Modified` performs no JSON parse and preserves the cursor.
- `Link: rel="next"` is followed with a default maximum of 20 pages and a hard
  configurable bound of 100.
- The greatest observed `X-Poll-Interval` controls the next success delay;
  missing or invalid values default to 60 seconds.
- Cursor commit happens only after all pages succeed.
- A separate read-replay cursor queries recent participating notifications with
  `all=true&since=...`. The default window is 1440 minutes with a five-minute
  overlap and a two-page cap. Stable GitHub event IDs and the SQLite outbox make
  repeated replay idempotent.
- 401, forbidden 403, rate-limited 403, transport failure, 5xx, malformed JSON,
  and pagination-limit failures become bounded category-only status records.
- Retryable failures use 60-second exponential backoff capped at 3600 seconds.
  Valid `Retry-After` or `X-RateLimit-Reset` timing takes precedence. Other
  client/auth failures use a 300-second delay.

Exceptions and persisted status never include the token, response body, title,
or raw source content.

### P3-M2.5 operational configuration

The gateway lifecycle starts one runtime per served profile and cancels it before
Discord adapter teardown. Configuration is fail-closed and disabled by default:

```yaml
terminal:
  cwd: /Users/choegeun-won

mention_inbox:
  enabled: false
  credential_env: GITHUB_PAT_TOKEN
  repositories: [silviahealth/content]
  include_public_actionable_activity: false
  external_repository_actions: disabled
  destination: discord:1531851208858275860
  team_mentions: false
  team_review_requests: false
  read_replay_lookback_minutes: 1440
  read_replay_max_pages: 2
  action_sessions:
    enabled: false
    execution_enabled: false
    authorized_approver_ids: []
    bot_mention: null
    execution_mode: direct
    workspace: Documents/hermes-workspaces/silviahealth-content
    workspace_root: Documents/hermes-workspaces
  retention_days: 30
  lease_seconds: 60
```

`action_sessions.authorized_approver_ids` has two coupled responsibilities:
those Discord users are allowed to approve eligible proposals, and they are
idempotently added as members of every durable work-item thread before a
proposal is posted. This membership synchronization also applies when
`execution_enabled=false`, so review-only threads remain visible to the
configured reviewers. The Discord bot therefore needs permission to view the
destination, create public threads, send messages in threads, and add guild
members to those threads. Configured Discord IDs must be positive uint64
snowflakes; impossible out-of-range values fail configuration before any API
write.

On gateway startup, the runtime reconciles at most 1,000 active work-item
sessions whose Discord parent matches the currently configured destination. It
repairs membership for active, unlocked threads without creating messages or
replacement threads. Sessions without a thread, sessions from another
destination, and archived or locked threads are skipped rather than reopened
solely for startup backfill. A genuine later PR revision may unarchive its
unlocked durable thread before membership synchronization and proposal
delivery; a locked thread fails closed. Discord's add-member operation is
idempotent, so repeating reconciliation is safe. More than 1,000 active
sessions degrades startup with
`discord_thread_participant_reconciliation_incomplete` instead of silently
reporting full coverage. Metadata, activation, or membership failures use
`discord_thread_participant_sync_failed`.

Delivery renews its durable outbox lease while thread activation, participant
sync, and proposal reconciliation are in flight. If another worker has already
reclaimed the attempt, the stale worker is cancelled before it can post
proposal controls and cannot overwrite the current attempt's error state.
Proposal sends also use a deterministic revision-specific Discord nonce so an
uncertain concurrent accept converges on one visible message.
Read-only external proposals (`inspect_only`) never route write-like requests
to the full tool-capable agent, even when the requester is an authorized
approver. Membership is additive: removing an ID from configuration or rolling
back this code does not remove members already added to Discord threads.

Read replay is limited to at most seven days and ten pages even when configured
explicitly. It is GET-only and never changes GitHub notification read state.

When `execution_enabled=true`, both `terminal.cwd` and
an action-session workspace scope are required. With
`external_repository_actions=disabled`, `action_sessions.workspace` names the
single trusted clone. With either external mode,
`action_sessions.workspace_root` names the trusted root that contains
repository caches and execution-owned worktrees. Both values must be safe
relative POSIX paths and resolve under `terminal.cwd`; do not point either at
the gateway checkout or a directory with unrelated changes.

External repository authority is configured independently from collection:

- `disabled` keeps every external proposal read-only.
- `inspect_only` explicitly enables the staged read-only rollout and never
  offers host execution.
- `own_pr_write` may offer scoped edit/commit/non-force-push only when fresh
  GitHub hydration proves a public PR authored by the target user. It never
  runs repository code or tests on the host. Cache clone/fetch operations use
  an isolated anonymous Git environment with credential helpers, askpass,
  ambient GitHub tokens, and user/system Git configuration disabled.

Trusted allowlisted repositories retain the existing targeted-test execution
path. Before every external approval, the runtime re-fetches the PR and proves
the base repository name/node ID, public base and head visibility, target
ownership, current revision, head repository, branch, and SHA.

Startup recovery leases each queued execution atomically so overlapping gateway
instances cannot admit the same work twice. Disabling an external write policy
durably blocks its queued executions and moves their proposals back to
reapproval. Completion state and the bounded terminal Discord receipt are
committed together; a deterministic receipt marker reconciles uncertain sends
without duplicating the visible completion message.

Only the launcher resolves `GITHUB_PAT_TOKEN`; status and logs expose category
labels, never credential or source payload values. Missing credentials and an
unavailable Discord adapter produce degraded service health without aborting the
gateway.

Each content revision creates one outbox row per destination. Claims use a SQLite
write transaction and expiring lease. A successful post records its Discord
message ID. If the process can crash after Discord accepts a post but before the
DB acknowledgement, the next lease holder searches a bounded window of
bot-authored channel history for the deterministic marker before retrying. This
closes normal restart/retry duplication, but is not a mathematical exactly-once
guarantee: a marker outside Discord's bounded retrievable history can still be
reposted.

Rendered messages are deterministic, bounded, label title/body as untrusted data,
neutralize mentions/markdown, include source/repository/action/exact permalink, and
force Discord `AllowedMentions.none()`.

### Preapproval brief, review-only mode, and commands

Before a proposal can offer execution approval, the collector builds a bounded,
deterministic preapproval brief from the already fetched GitHub review summary,
matching inline comments, current source revision, and PR HEAD. It does not open
an agent session. User-visible evidence includes the request summary, file and
current line when available, and the exact GitHub permalink.

The conservative dispositions are:

- `action_required`: a current change request is bound to the observed HEAD.
- `review_needed`: current review evidence needs inspection before scoped work.
- `possibly_stale`: a comment is bound to an older commit or current line cannot
  be established; read-only revalidation is required.
- `informational`: the review contains no actionable finding.
- `insufficient_evidence`: required IDs, revision, HEAD, or evidence are missing
  or malformed; execution approval fails closed.

`action_sessions.enabled=true` may create and maintain the durable work thread,
but `action_sessions.execution_enabled=false` is review-only mode. Review-only
messages contain no approval CTA and are stored with `approval_offered=false`.
Even when execution is enabled, stale, informational, or insufficient evidence
cannot offer approval.

There are two deterministic ways for an authorized user to request execution:

- reply to the latest proposal with exactly `<@bot_id> 승인`; or
- in the registered work thread, use a recognized imperative such as
  `<@bot_id> 이 작업 네가 수행해` or `이거 수정해줘`.

The natural-language form is a small code-owned grammar, not an LLM decision.
When it is not a reply, it binds only to the latest durably stored proposal that
actually offered approval. A question such as `이거 수정해주냐?`, ordinary
conversation, and unrecognized wording remain in the read-only conversation
path.

Execution is accepted only when all of these conditions hold:

1. the author is an authorized approver;
2. the text matches one of the deterministic forms above;
3. an explicit reply, when present, references the latest proposal message;
4. that exact message was durably stored with `approval_offered=true`; and
5. the execution handler is currently available.

A missing or stale reply reference, an approval-like non-reply, an unauthorized
user, or an unavailable execution handler receives a user-visible explanation.
It never becomes proposal feedback and never creates a new revision. Every other
non-empty message is answered by a bounded, stateless completion that receives
only the current proposal and HEAD-bound preapproval evidence as untrusted JSON
data. That completion has no tools, store handle, approval handler, execution
dispatcher, memory, or general-agent fallthrough. A model failure returns a
deterministic current-proposal summary instead of opening a mutation-capable
path. Ordinary conversation always leaves proposal, approval, and execution
state unchanged. To change a proposal, an authorized user must use the explicit
command `<@bot_id> 제안 수정: 바꿀 내용`.

Historical proposal messages are never mutated into executable approvals.
When execution is activated, startup reconciliation examines active work
sessions. If the latest pending proposal still matches the stored source
revision and PR HEAD but its message had `approval_offered=false`, the old
revision moves to `needs_reapproval` and one new approval-capable revision is
posted. Repeated startup is idempotent. A source or HEAD change still requires
fresh hydration and a newly bound proposal.

Action sessions and execution remain separate, disabled-by-default gates. Code
deployment with `execution_enabled=false` and execution activation are separate
rollout stages. Activation requires its own operational approval and canary; it
must not make historical messages executable.

After a valid approval, the runtime revalidates the current GitHub source
revision, base repository identity and visibility, PR ownership when required,
PR HEAD SHA, head repository visibility, and head branch before queueing.
Direct execution runs in an isolated gateway session with only scoped file and
terminal tools. File paths must be absolute, remain under the approved
workspace after symlink resolution, and use only the approved read/write
actions. Terminal calls require an explicit workspace-contained `workdir`;
background and interactive PTY calls, shell composition, and unapproved
programs are blocked.

Only actionable notifications on the authenticated user's own PR add
`switch_to_pr_branch`, `commit_changes`, and `push_current_branch`. The allowed
Git flow is deliberately narrow:

1. prove a clean workspace, base `origin`, current branch, and approved HEAD;
2. fetch and switch only to the verified PR head branch;
3. edit within the workspace and run approved targeted tests;
4. stage explicit paths with `git add -- <file>...`;
5. create one normal `git commit -m <message>`;
6. verify the new commit with `git rev-parse HEAD`; and
7. push only `HEAD` to the verified head branch without force.

Completion requires successful receipts for the applicable branch, HEAD,
verification, commit-SHA, and push steps. The Discord completion message uses a
forced-redacted bounded agent summary and reports the verified commit SHA and
receipt categories. Force push, merge, rebase, deployment, deletion, secret
access, live configuration changes, and pushing another branch remain outside
this approval. Kanban intake exposes only the single `kanban_task` surface.
