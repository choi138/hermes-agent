# Unified Mention Inbox event contract

This package defines the versioned domain contract shared by GitHub, Slack, and
Notion mention collectors. P3-M2 also provides an explicitly invoked,
read-only GitHub Notifications pilot and a profile-scoped SQLite store. It
remains unregistered: there is no plugin manifest, scheduler, gateway hook,
credential lookup, prompt builder, or external write path.

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

The collector requires an explicit repository allowlist. An empty allowlist
collects nothing. P3-M2 selects only:

- `mention` and `team_mention` -> `reply`
- `review_requested` -> `review`
- `assign` -> `investigate`

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
primary key.

- First observation inserts revision 1 in `pending` state.
- Same content updates source revision and `last_seen` without duplicating the
  row or overwriting a local approval.
- Changed canonical content increments the revision and replaces the event in
  `pending` state, invalidating any prior approval.
- Older source revisions cannot overwrite newer content or approval.
- `Last-Modified` is persisted separately as the collector cursor.

The database stores raw external body text for this pilot. Retention, truncation,
and deletion policy remain an explicit follow-up decision before broader
production rollout.

### Polling and failure policy

- The first page sends the stored `If-Modified-Since` value exactly.
- `304 Not Modified` performs no JSON parse and preserves the cursor.
- `Link: rel="next"` is followed with a default maximum of 20 pages and a hard
  configurable bound of 100.
- The greatest observed `X-Poll-Interval` controls the next success delay;
  missing or invalid values default to 60 seconds.
- Cursor commit happens only after all pages succeed.
- 401, forbidden 403, rate-limited 403, transport failure, 5xx, malformed JSON,
  and pagination-limit failures become bounded category-only status records.
- Retryable failures use 60-second exponential backoff capped at 3600 seconds.
  Valid `Retry-After` or `X-RateLimit-Reset` timing takes precedence. Other
  client/auth failures use a 300-second delay.

Exceptions and persisted status never include the token, response body, title,
or raw source content.

### P3-M2 boundary

Included:

- read-only `/user`, notification polling, pagination, and issue/PR enrichment,
- explicit repository/reason filtering,
- v1 normalization and source revision separation,
- SQLite dedupe/revision/cursor/status persistence,
- bounded one-shot poll orchestration,
- fixture and request-spy tests.

Not included:

- automatic scheduling or a long-running service,
- plugin/gateway/CLI registration,
- Discord Approval Inbox,
- Slack or Notion collection,
- notification read-state changes or any external write,
- deployment or gateway restart.
