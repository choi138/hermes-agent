---
slug: work-inbox-thread-membership
status: completed
intent: clear
review_required: false
pending-action: none
approach: synchronize configured authorized approvers into every durable work-inbox Discord thread
---

# Work Inbox Thread Membership Remediation

## TL;DR

Hermes already creates one public Discord thread per GitHub PR, but it only marks
the bot's internal thread tracker and never joins the configured human approvers.
The fix will reuse `mention_inbox.action_sessions.authorized_approver_ids`, add an
idempotent Discord participant-sync transport operation, run it before proposal
delivery, and reconcile active pre-existing sessions at startup. No new config
key, database migration, PR grouping change, or execution-permission expansion is
required.

## Approved scope

### Goal

Ensure every configured authorized approver is a Discord member of each active
work-inbox PR thread so that the thread appears in the user's joined-thread
surface and produces the expected notifications.

### Requirements

- Keep one durable public Discord thread per GitHub PR subject.
- Reuse `mention_inbox.action_sessions.authorized_approver_ids`.
- Join authorized approvers on both newly created and recovered threads.
- Repair active threads created before this fix during gateway startup.
- Preserve durable outbox retry and marker reconciliation without duplicate
  parent alerts, threads, or proposals.
- Surface participant-sync failures as operational failures rather than silently
  reporting a successful delivery.
- Preserve `inspect_only`, approval authorization, proposal capabilities, and
  all existing execution restrictions.

### Constraints

- Discord writes must remain idempotent.
- Numeric Discord IDs continue to be validated at the config boundary.
- The bot's internal `_threads` tracker and Discord thread membership remain
  separate concepts.
- No secret values may enter logs or receipts.
- No production deployment, Discord canary, commit, or push occurs without
  explicit execution authorization.

### Exclusions

- Do not create one thread per notification.
- Do not change GitHub event selection or hydration.
- Do not change who may approve or execute proposals.
- Do not add a second participant configuration field.
- Do not add a database migration unless implementation proves the existing
  `work_item_sessions` data is insufficient.
- Do not auto-unarchive old threads merely to backfill membership.

## Grounded current behavior

- `plugins/mention_inbox/operational.py:43` already stores
  `authorized_approver_ids` in `MentionInboxConfig`.
- `plugins/mention_inbox/operational.py:908` constructs the thread coordinator
  without those IDs, even though adjacent router and approval components receive
  them.
- `plugins/mention_inbox/thread_session.py:423` recovers or creates the durable
  thread.
- `plugins/mention_inbox/thread_session.py:448` calls thread creation.
- `plugins/mention_inbox/thread_session.py:456` only marks internal
  participation.
- `plugins/platforms/discord/adapter.py:5613` successfully calls
  `message.create_thread(...)`.
- `plugins/platforms/discord/adapter.py:5629` calls only
  `_threads.mark(thread_id)` and never Discord `Thread.add_user(...)`.
- `plugins/mention_inbox/store.py:1045` already provides bounded active-session
  enumeration for startup reconciliation.

## Target contracts

### Thread transport

Extend `ThreadSessionTransport` with:

```python
async def ensure_thread_participants(
    self,
    thread_id: str,
    user_ids: frozenset[str],
) -> None: ...
```

Contract:

- Validate the thread ID and every user ID before issuing Discord calls.
- Resolve the existing thread channel through cache then API, matching the
  current channel-resolution behavior.
- Call Discord's idempotent add-thread-member operation once per configured
  user in deterministic order.
- Treat an already-present member as success.
- Do not substitute the bot's internal thread tracker for the Discord API call.
- Propagate Discord/network/permission failures with a stable categorized error
  so the delivery outbox can retry.

### Thread coordinator

Add immutable `participant_user_ids: frozenset[str]` to
`MentionInboxThreadCoordinator`.

For both recovered and newly created threads:

1. Resolve or create the anchored thread.
2. Call `ensure_thread_participants(...)`.
3. Mark the internal Hermes thread tracker.
4. Reconcile or post the proposal message.
5. Mark delivery sent only after all required bootstrap work succeeds.

### Startup reconciliation

Add a bounded coordinator reconciliation method that:

- reads `MentionInboxStore.list_active_work_item_sessions(limit=1000)`;
- selects sessions with a persisted `discord_thread_id`;
- resolves current Discord thread metadata;
- synchronizes participants only for active, unlocked threads;
- skips archived threads without reopening them;
- returns counts for examined, repaired, skipped, and failed sessions;
- is safe to repeat after every gateway restart.

Run it during mention-inbox boot after the coordinator is constructed and before
the plugin reports healthy. A participant-sync failure must set an explicit
degraded category and must not be converted to success.

## Implementation waves

### Wave A — Lock the adapter contract with failing tests

- [x] **Add Discord participant synchronization and adapter tests**
  - Files:
    - `plugins/platforms/discord/adapter.py`
    - `tests/plugins/test_discord_mention_inbox_thread.py`
  - Write the failing tests first:
    - newly created thread adds every configured user;
    - recovered thread also adds every configured user;
    - repeated calls are harmless and do not create another thread;
    - malformed IDs fail before API access;
    - Discord permission/network failures remain visible;
    - internal tracker marking alone cannot satisfy the membership assertion.
  - Implement the smallest adapter method using Discord's add-member API.
  - Keep the existing `mark_mention_inbox_thread_participation()` behavior
    unchanged for message-routing semantics.
  - Acceptance:
    - tests fail before the method exists;
    - tests pass after implementation;
    - no send, execute, or approval code changes.

### Wave B — Wire membership through the coordinator

- [x] **Synchronize participants before proposal delivery**
  - Files:
    - `plugins/mention_inbox/thread_session.py`
    - `plugins/mention_inbox/operational.py`
    - `tests/plugins/test_mention_inbox_thread_session.py`
  - Add the async transport protocol method.
  - Add `participant_user_ids` to the coordinator constructor.
  - Pass `MentionInboxConfig.authorized_approver_ids` from runtime boot.
  - Invoke synchronization after both existing-thread recovery and new-thread
    creation, before proposal reconciliation or send.
  - Add failing tests for:
    - new thread;
    - interrupted creation recovered from the parent message;
    - later PR revision reusing the same thread;
    - empty approver set as an explicit no-op.
  - Acceptance:
    - one PR still maps to one parent and one thread;
    - participant sync precedes proposal posting;
    - no capability or approval changes.

### Wave C — Preserve outbox idempotency on failure

- [x] **Retry participant failures without duplicate Discord artifacts**
  - Files:
    - `plugins/mention_inbox/operational.py`
    - `tests/plugins/test_mention_inbox_delivery_thread.py`
  - Extend the existing post-send bootstrap-failure scenario so the first
    participant sync fails and the retry succeeds.
  - Assert that the retry:
    - reuses the persisted parent message ID;
    - reuses the existing anchored thread;
    - adds the participant;
    - creates no second parent alert;
    - creates no second thread;
    - posts or reconciles exactly one proposal revision;
    - marks delivery sent only after participant sync succeeds.
  - Map failures to a stable category such as
    `discord_thread_participant_sync_failed`.

### Wave D — Repair active pre-existing threads

- [x] **Reconcile active thread membership at gateway startup**
  - Files:
    - `plugins/mention_inbox/thread_session.py`
    - `plugins/mention_inbox/operational.py`
    - `tests/plugins/test_mention_inbox_thread_session.py`
    - `tests/plugins/test_mention_inbox_operational.py`
  - Add bounded startup reconciliation using the existing active-session list.
  - Test mixed data:
    - no thread ID;
    - active thread needing repair;
    - already-correct thread;
    - archived thread skipped without unarchiving;
    - one Discord failure producing a degraded result;
    - second startup remaining idempotent.
  - Do not add schema state solely to remember an idempotent Discord PUT.

### Wave E — Documentation and operator evidence

- [x] **Document membership and operational failure behavior**
  - Files:
    - `plugins/mention_inbox/README.md`
    - relevant config example only if one already documents action sessions
  - State that `authorized_approver_ids` controls both approval authorization
    and automatic membership in work-item threads.
  - Document required Discord permissions and the degraded/error category.
  - Document that archived threads are not reopened solely for backfill.

### Review remediation

- [x] **Surface delivery-time membership failures in runtime health**
- [x] **Recover archived threads for real later PR revisions**
- [x] **Rollback partially installed startup hooks on failure or cancellation**
- [x] **Detect and degrade on startup reconciliation overflow**
- [x] **Fence long participant synchronization against expired delivery leases**
- [x] **Fence stale attempts with token checkpoints and proposal nonce**
- [x] **Enforce inspect-only routing authorization before full-agent passthrough**
- [x] **Bind startup reconciliation to the current Discord destination**
- [x] **Reject Discord user IDs outside the uint64 snowflake range**
- [x] **Classify real-delivery thread activation failures consistently**
- [x] **Document review-remediation operational behavior**
- [x] **Rollback hooks when an adapter installer raises after publication**
- [x] **Skip archived threads during startup execution activation**
- [x] **Serialize concurrent delivery rows before canonical parent creation**
- [x] **Enforce destination ownership during execution activation**
- [x] **Fence parent sends with heartbeat and deterministic nonce**
- [x] **Compare delivery lease timestamps independent of text precision**
- [x] **Fence all work-thread writes by configured destination**
- [x] **Validate approval actions and recovered dispatch destinations**
- [x] **Record stable destination-mismatch delivery failures**
- [x] **Detect participant reconciliation overflow from one snapshot**
- [x] **Tokenize and heartbeat execution-recovery lease ownership**
- [x] **Cancel stale recovery workers without swallowing cancellation**
- [x] **Deduplicate process-local execution enqueue by execution ID**
- [x] **Rebind stale unthreaded parents before Discord thread creation**
- [x] **Bound asynchronous test synchronization waits**

## Dependency matrix

| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| Adapter contract | none | Coordinator wiring | Documentation draft |
| Coordinator wiring | Adapter contract | Retry and reconciliation | none |
| Outbox retry | Coordinator wiring | Deployment | Startup reconciliation tests |
| Startup reconciliation | Coordinator wiring | Deployment | Outbox retry |
| Documentation | final contracts | Handoff | final validation |

## Verification

### Focused automated checks

Run once per relevant increment:

```bash
python -m pytest tests/plugins/test_discord_mention_inbox_thread.py -q
python -m pytest tests/plugins/test_mention_inbox_thread_session.py -q
python -m pytest tests/plugins/test_mention_inbox_delivery_thread.py -q
python -m pytest tests/plugins/test_mention_inbox_operational.py -q
```

Then run the repository's formatting, Ruff, Python compilation, and the same
mention-inbox/gateway test groups used for the original deployment. Do not
accept retries, flaky reruns, weakened assertions, or unrelated test deletion.

### Manual QA against a controlled Discord canary

After explicit authorization for external writes:

1. Deploy the verified code to the isolated runtime worktree.
2. Run `hermes config check`.
3. Restart `hermes-gateway.service` and confirm the imported module path and
   source hashes point at the deployment worktree.
4. Inject one controlled public-PR notification through the production
   collector path.
5. Confirm through Discord API and live SQLite:
   - parent delivery status is `sent`;
   - parent message has `HAS_THREAD`;
   - thread is public, active, and anchored to the parent;
   - configured approver IDs and the bot appear in thread members;
   - proposal message channel ID equals the thread ID;
   - repeated poll/restart creates no duplicate parent, thread, or proposal.
6. Confirm startup reconciliation adds the configured approver to the reported
   incident thread `1533679846792757348` without creating any new message.

## Deployment gates

- Source and tests are clean in the development worktree.
- Changed files have no LSP/type diagnostics; if local basedpyright remains
  unavailable, run the project type checker in its managed environment and
  record that limitation.
- Focused and full relevant suites pass once.
- `config check` passes on the target runtime.
- Remote deployment diff contains only the reviewed membership change.
- Commit, push, cherry-pick, service restart, Discord writes, and production
  backfill occur only after explicit authorization.

## Rollback

1. Stop rollout if participant sync creates duplicate parents/threads, changes
   authorization behavior, or degrades the collector.
2. Restore the previously verified runtime revision and restart the gateway.
3. Confirm the previous module hashes, service health, and collector status.
4. Do not delete existing Discord threads or messages.
5. Previously added authorized members may remain: membership addition is an
   external side effect and is not automatically reversed. Remove a member only
   with separate explicit authorization if privacy or access policy requires it.
6. No database rollback is expected because the plan adds no schema migration.

## Success criteria

- The configured user `789391209067446323` is a member of the incident thread
  `1533679846792757348`.
- New work-inbox PR threads include all configured authorized approvers.
- Existing active threads are repaired at startup without being recreated.
- Same-PR revisions continue to reuse one durable thread.
- Participant-sync failures are observable and retried without duplicate
  Discord artifacts.
- `inspect_only` and all execution/approval restrictions remain unchanged.
- Automated checks and controlled live canary are green.
