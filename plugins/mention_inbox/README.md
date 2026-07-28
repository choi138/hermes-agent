# Unified Mention Inbox event contract

This package defines the versioned domain contract shared by the future GitHub,
Slack, and Notion mention collectors. It is deliberately inert in P3-M1: there
is no plugin manifest, registration hook, network client, credential lookup,
prompt builder, or external write path.

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

These are M2-M4 implementation constraints, not live integrations in M1.

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
