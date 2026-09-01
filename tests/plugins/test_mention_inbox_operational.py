from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.approval import (
    ApprovedExecutionRequest,
    GatewayExecutionDispatcher,
)
from plugins.mention_inbox.contract import event_to_json
from plugins.mention_inbox.github_collector import GitHubNotificationCollector
from plugins.mention_inbox.operational import (
    DiscordMentionDelivery,
    GatewayDiscordTransport,
    MentionInboxConfig,
    MentionInboxGatewayService,
    MentionInboxRuntime,
    NotionInboxConfig,
    parse_mention_inbox_config,
    render_discord_event,
)
from plugins.mention_inbox.store import SCHEMA_VERSION
from plugins.mention_inbox.thread_session import ThreadDestinationMismatchError

NOW = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
DESTINATION = "discord:1531851208858275860"
WORK_INBOX_DESTINATION = DESTINATION


def _event(*, title: str = "Review requested", body: str = "Please review", event_id: str = "n1"):
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": event_id},
        "actor": {"actor_id": "github:actor", "kind": "user"},
        "target": {"target_id": "github:me", "kind": "user"},
        "thread": {"thread_id": "github:thread", "container_id": "repo-node"},
        "requested_action": "review",
        "deadline": None,
        "untrusted": {
            "title": title,
            "body": body,
            "action_detail": "review_requested",
            "source_url": "https://github.com/silviahealth/content/pull/7",
            "metadata": {"repository": "silviahealth/content"},
        },
    })


def _enabled_config() -> MentionInboxConfig:
    return MentionInboxConfig(
        enabled=True,
        credential_env="GITHUB_PAT_TOKEN",
        repositories=("silviahealth/content",),
        destination=DESTINATION,
        retention_days=30,
        lease_seconds=60,
    )


def test_config_defaults_disabled_and_validates_fail_closed() -> None:
    disabled = parse_mention_inbox_config({})
    assert disabled.enabled is False
    assert disabled.destination == WORK_INBOX_DESTINATION
    assert disabled.team_mentions is False
    assert disabled.team_review_requests is False
    assert disabled.read_replay_lookback_minutes == 1440
    assert disabled.read_replay_max_pages == 2
    assert disabled.include_public_actionable_activity is False
    assert disabled.external_repository_actions == "disabled"
    assert disabled.user_message_mode == "proposal_router"
    config = parse_mention_inbox_config({"mention_inbox": {
        "enabled": True,
        "credential_env": "GITHUB_PAT_TOKEN",
        "repositories": ["silviahealth/content"],
        "destination": DESTINATION,
        "retention_days": 30,
    }})
    assert config == _enabled_config()
    public_config = parse_mention_inbox_config({"mention_inbox": {
        "include_public_actionable_activity": True,
        "external_repository_actions": "inspect_only",
    }})
    assert public_config.include_public_actionable_activity is True
    assert public_config.external_repository_actions == "inspect_only"
    standard_agent_config = parse_mention_inbox_config({"mention_inbox": {
        "action_sessions": {"user_message_mode": "standard_agent"},
    }})
    assert standard_agent_config.user_message_mode == "standard_agent"
    for invalid in (
        {"enabled": "true"},
        {"enabled": True, "credential_env": "TOKEN"},
        {"enabled": True, "destination": "discord:not-a-channel"},
        {"enabled": True, "retention_days": 0},
        {"enabled": True, "read_replay_lookback_minutes": 10081},
        {"enabled": True, "read_replay_max_pages": 11},
        {"include_public_actionable_activity": "true"},
        {"external_repository_actions": "write"},
        {"action_sessions": {"user_message_mode": "unrestricted"}},
        {"action_sessions": {"user_message_mode": ["standard_agent"]}},
    ):
        with pytest.raises(ValueError):
            parse_mention_inbox_config({"mention_inbox": invalid})


def test_config_accepts_multiple_trusted_repositories() -> None:
    """The allowlist is operator-controlled config, not a hardcoded constant."""
    config = parse_mention_inbox_config({"mention_inbox": {
        "enabled": True,
        "repositories": [
            "silviahealth/content",
            "silviahealth/library",
            "choi138/stock-research-agent",
        ],
    }})

    assert config.repositories == (
        "silviahealth/content",
        "silviahealth/library",
        "choi138/stock-research-agent",
    )


@pytest.mark.parametrize(
    "repositories",
    (
        [],
        ["silviahealth/content", "silviahealth/content"],
        ["not-a-repo"],
        ["owner/repo/extra"],
        ["/repo"],
        ["owner/"],
        ["owner/re po"],
        ["owner/repo", 7],
    ),
)
def test_config_rejects_malformed_repository_allowlists(
    repositories: list[object],
) -> None:
    """Widening the allowlist must not weaken its shape or duplicate checks."""
    with pytest.raises(ValueError, match="repositories"):
        parse_mention_inbox_config({
            "mention_inbox": {"enabled": True, "repositories": repositories}
        })


@pytest.mark.parametrize(
    "invalid_user_id",
    (str(2**64), "9999999999999999999999999"),
)
def test_config_rejects_out_of_range_discord_snowflakes(
    invalid_user_id: str,
) -> None:
    with pytest.raises(ValueError, match="authorized_approver_ids"):
        parse_mention_inbox_config({
            "mention_inbox": {
                "action_sessions": {
                    "enabled": True,
                    "bot_mention": "<@1525050677381279865>",
                    "authorized_approver_ids": [invalid_user_id],
                }
            }
        })


def test_execution_config_requires_scoped_workspace_and_terminal_cwd() -> None:
    raw = {
        "terminal": {"cwd": "/Users/test"},
        "mention_inbox": {
            "action_sessions": {
                "enabled": True,
                "bot_mention": "<@1525050677381279865>",
                "authorized_approver_ids": ["396159160201658368"],
                "execution_enabled": True,
                "workspace": "Documents/hermes-workspaces/silviahealth-content",
            }
        },
    }

    parsed = parse_mention_inbox_config(raw)

    assert (
        parsed.execution_workspace
        == "Documents/hermes-workspaces/silviahealth-content"
    )
    assert parsed.terminal_cwd == "/Users/test"

    for workspace in (None, "../content", "/Users/test/content", "content//repo"):
        invalid = {
            **raw,
            "mention_inbox": {
                "action_sessions": {
                    **raw["mention_inbox"]["action_sessions"],
                    "workspace": workspace,
                }
            },
        }
        with pytest.raises(ValueError):
            parse_mention_inbox_config(invalid)

    without_cwd = {**raw, "terminal": {}}
    with pytest.raises(ValueError):
        parse_mention_inbox_config(without_cwd)


def test_external_execution_uses_repository_workspace_root() -> None:
    parsed = parse_mention_inbox_config({
        "terminal": {"cwd": "/Users/test"},
        "mention_inbox": {
            "include_public_actionable_activity": True,
            "external_repository_actions": "own_pr_write",
            "action_sessions": {
                "enabled": True,
                "bot_mention": "<@1525050677381279865>",
                "authorized_approver_ids": ["396159160201658368"],
                "execution_enabled": True,
                "workspace_root": "Documents/hermes-workspaces",
            },
        },
    })

    assert parsed.execution_workspace is None
    assert parsed.execution_workspace_root == "Documents/hermes-workspaces"
    assert parsed.external_repository_actions == "own_pr_write"


def test_renderer_bounds_untrusted_text_escapes_mentions_and_has_marker() -> None:
    event = _event(
        title="@everyone **title** " + "x" * 1000,
        body="<@123> @here [click](https://evil.example) " + "y" * 5000,
    )
    rendered = render_discord_event(event, revision_number=4, destination=DESTINATION)
    assert len(rendered.content) <= 1900
    assert "@everyone" not in rendered.content
    assert "@here" not in rendered.content
    assert "<@123>" not in rendered.content
    assert "silviahealth/content" in rendered.content
    assert "Requested action: review" in rendered.content
    assert "https://github.com/silviahealth/content/pull/7" in rendered.content
    assert rendered.marker.startswith("[hermes-inbox:")
    assert rendered.marker in rendered.content
    assert rendered.allowed_mentions == {"parse": [], "users": [], "roles": [], "replied_user": False}


def test_outbox_first_send_same_revision_retry_and_changed_revision(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    event = _event()
    first = store.upsert(event, source_revision="2026-07-29T08:00:00Z")
    assert first.created
    assert store.pending_delivery_count() == 1
    claimed = store.claim_delivery(DESTINATION, lease_seconds=60)
    assert claimed is not None and claimed.revision_number == 1
    store.release_delivery(
        claimed.delivery_id,
        claim_token=claimed.token,
        error_category="discord_send",
    )
    retry = store.claim_delivery(DESTINATION, lease_seconds=60)
    assert retry is not None and retry.delivery_id == claimed.delivery_id
    store.mark_delivery_sent(
        retry.delivery_id,
        claim_token=retry.token,
        message_id="m1",
    )
    assert store.pending_delivery_count() == 0
    same = store.upsert(event, source_revision="2026-07-29T08:05:00Z")
    assert not same.content_changed
    assert store.pending_delivery_count() == 0
    changed = store.upsert(_event(body="changed"), source_revision="2026-07-29T08:10:00Z")
    assert changed.revision_number == 2
    assert store.pending_delivery_count() == 1


def test_concurrent_claim_and_restart_use_single_durable_delivery(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    first = MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60)
    second = MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60)
    assert first is not None
    assert second is None
    MentionInboxStore(db, clock=lambda: NOW).mark_delivery_sent(
        first.delivery_id,
        claim_token=first.token,
        message_id="m1",
    )
    assert MentionInboxStore(db, clock=lambda: NOW).claim_delivery(DESTINATION, lease_seconds=60) is None


def test_expired_uncertain_lease_requires_reconciliation(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    now[0] += timedelta(seconds=11)
    recovered = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert recovered is not None
    assert recovered.delivery_id == claim.delivery_id
    assert recovered.requires_reconciliation is True


@pytest.mark.parametrize(
    ("stored_lease", "comparison_time", "expected"),
    [
        (
            "2026-07-29T09:00:10.500000Z",
            NOW + timedelta(seconds=10),
            True,
        ),
        (
            "2026-07-29T09:00:10Z",
            NOW + timedelta(seconds=10, milliseconds=100),
            False,
        ),
        (
            "2026-07-29T10:00:10+01:00",
            NOW + timedelta(seconds=10),
            False,
        ),
    ],
)
def test_lease_renewal_uses_sqlite_timestamp_order(
    tmp_path: Path,
    stored_lease: str,
    comparison_time: datetime,
    expected: bool,
) -> None:
    now = [NOW]
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE delivery_outbox SET lease_until = ? WHERE delivery_id = ?",
            (stored_lease, claim.delivery_id),
        )
        connection.commit()
    finally:
        connection.close()
    now[0] = comparison_time

    assert store.renew_delivery_lease(
        claim.delivery_id,
        expected_attempt=claim.token,
        lease_seconds=10,
    ) is expected


class _Discord:
    def __init__(self, *, history: list[dict[str, str]] | None = None, fail: bool = False):
        self.history = history or []
        self.fail = fail
        self.sends: list[tuple[str, str, dict[str, Any]]] = []

    async def find_marker(self, channel_id: str, marker: str, *, limit: int) -> str | None:
        for item in self.history[:limit]:
            if marker in item["content"]:
                return item["id"]
        return None

    async def send(
        self,
        channel_id: str,
        content: str,
        *,
        allowed_mentions: dict[str, Any],
        nonce: str,
    ) -> str:
        self.sends.append((channel_id, content, allowed_mentions))
        if self.fail:
            raise RuntimeError("private discord failure body")
        return "message-1"


@pytest.mark.asyncio
async def test_uncertain_send_reconciles_marker_without_duplicate(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    original = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert original is not None
    rendered = render_discord_event(original.event, revision_number=1, destination=DESTINATION)
    now[0] += timedelta(seconds=11)
    discord = _Discord(history=[{"id": "existing", "content": rendered.content}])
    delivery = DiscordMentionDelivery(store=store, discord=discord, destination=DESTINATION, lease_seconds=10)
    assert await delivery.deliver_once() == "reconciled"
    assert discord.sends == []
    assert store.pending_delivery_count() == 0


class _GatewayAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict[str, Any]]] = []
        self.remembered_parents: list[tuple[str, str]] = []
        self.thread_parent_matches = True
        self.parent_checks: list[tuple[str, str]] = []

    async def send(
        self,
        thread_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any],
    ) -> SimpleNamespace:
        self.calls.append((thread_id, content, reply_to, metadata))
        return SimpleNamespace(success=True, message_id=f"message-{len(self.calls)}")

    def remember_mention_inbox_parent(
        self,
        message_id: str,
        channel_id: str,
    ) -> None:
        self.remembered_parents.append((message_id, channel_id))

    async def mention_inbox_thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool:
        self.parent_checks.append((thread_id, parent_channel_id))
        return self.thread_parent_matches


def test_gateway_transport_restores_durable_parent_mapping() -> None:
    adapter = _GatewayAdapter()
    transport = GatewayDiscordTransport(adapter, parent_channel_id="55")

    transport.remember_parent_message("99", "55")

    assert adapter.remembered_parents == [("99", "55")]


@pytest.mark.asyncio
async def test_gateway_transport_forwards_parent_nonce() -> None:
    adapter = _GatewayAdapter()
    transport = GatewayDiscordTransport(adapter)

    message_id = await transport.send(
        "55",
        "parent",
        allowed_mentions={
            "parse": [],
            "users": [],
            "roles": [],
            "replied_user": False,
        },
        nonce="0123456789abcdef01234567",
    )

    assert message_id == "message-1"
    assert adapter.calls == [
        (
            "55",
            "parent",
            None,
            {
                "non_conversational": True,
                "mention_inbox_no_mentions": True,
                "mention_inbox_nonce": "0123456789abcdef01234567",
            },
        )
    ]
    assert adapter.remembered_parents == [("message-1", "55")]


@pytest.mark.asyncio
async def test_gateway_transport_correlates_notice_to_inbound_message() -> None:
    adapter = _GatewayAdapter()
    transport = GatewayDiscordTransport(adapter)

    message_id = await transport.send_to_thread(
        "thread-1",
        "notice",
        reply_to_message_id="user-message-1",
    )

    assert message_id == "message-1"
    assert adapter.calls == [
        (
            "thread-1",
            "notice",
            "user-message-1",
            {
                "thread_id": "thread-1",
                "non_conversational": True,
                "mention_inbox_no_mentions": True,
                "notify": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_gateway_transport_rejects_wrong_destination_thread_write() -> None:
    adapter = _GatewayAdapter()
    adapter.thread_parent_matches = False
    transport = GatewayDiscordTransport(
        adapter,
        parent_channel_id="55",
    )

    with pytest.raises(ThreadDestinationMismatchError):
        await transport.send_to_thread("thread-1", "notice")

    assert adapter.parent_checks == [("thread-1", "55")]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_execution_dispatcher_rejects_wrong_destination_thread() -> None:
    class ExecutionTransport:
        def __init__(self) -> None:
            self.calls: list[ApprovedExecutionRequest] = []

        async def enqueue_mention_inbox_execution(
            self,
            request: ApprovedExecutionRequest,
            prompt: str,
        ) -> str:
            self.calls.append(request)
            return "dispatch-1"

    async def validate_destination(thread_id: str) -> bool:
        assert thread_id == "thread-1"
        return False

    transport = ExecutionTransport()
    dispatcher = GatewayExecutionDispatcher(
        transport,
        thread_destination_validator=validate_destination,
    )
    request = ApprovedExecutionRequest(
        execution_id="execution-1",
        proposal_id="proposal-1",
        proposal_revision=1,
        proposal_hash="hash-1",
        recovery_token="recovery-token-1",
        canonical_proposal_json="{}",
        subject_key="github:R_repo:PR_7",
        source_dedupe_key="github:n1:github:me",
        source_revision="2026-07-29T10:01:00Z",
        head_sha=None,
        head_ref=None,
        head_repository=None,
        workspace="/tmp/hermes-workspace",
        goal="Review the change",
        steps=("Inspect",),
        allowed_actions=("read_repository",),
        forbidden_actions=("push_current_branch",),
        verification=("Run tests",),
        executor_hint="direct",
        approval_message_id="approval-1",
        approver_user_id="user-1",
        thread_id="thread-1",
    )

    receipt = await dispatcher.dispatch(request)

    assert receipt.accepted is False
    assert receipt.dispatch_id is None
    assert transport.calls == []


@pytest.mark.asyncio
async def test_discord_failure_releases_claim_secret_safely(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    discord = _Discord(fail=True)
    delivery = DiscordMentionDelivery(store=store, discord=discord, destination=DESTINATION, lease_seconds=10)
    assert await delivery.deliver_once() == "error"
    health = store.health("github.notifications")
    assert health["pending_delivery_count"] == 1
    assert "private discord failure body" not in str(health)


class _Poller:
    def __init__(self, store: MentionInboxStore):
        self.store = store
        self.calls = 0

    def poll_once(self):
        self.calls += 1
        self.store.record_poll_success("github.notifications", next_poll_at=NOW + timedelta(hours=1))


class _Delivery:
    def __init__(self):
        self.calls = 0

    async def deliver_once(self):
        self.calls += 1
        return "idle"


@pytest.mark.asyncio
async def test_runtime_singleton_restores_schedule_and_cancels(tmp_path: Path) -> None:
    now = [NOW]
    sleeps: list[float] = []
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.record_poll_success("github.notifications", next_poll_at=NOW + timedelta(seconds=40))
    poller = _Poller(store)
    delivery = _Delivery()
    unblock = asyncio.Event()
    recovered = asyncio.Event()
    recovery_calls = 0

    async def recover_executions() -> int:
        nonlocal recovery_calls
        recovery_calls += 1
        recovered.set()
        return 0

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await unblock.wait()

    runtime = MentionInboxRuntime(
        config=_enabled_config(),
        store=store,
        poller=poller,
        delivery=delivery,
        clock=lambda: now[0],
        sleep=sleep,
        recover_executions=recover_executions,
    )
    first = runtime.start()
    second = runtime.start()
    assert first is second
    await asyncio.wait_for(recovered.wait(), timeout=2)
    assert recovery_calls == 1
    assert sleeps == [40.0]
    assert poller.calls == 0
    recovered.clear()
    now[0] += timedelta(seconds=40)
    unblock.set()
    await asyncio.wait_for(recovered.wait(), timeout=2)
    assert recovery_calls == 2
    await runtime.stop()
    assert first.cancelled()


def test_retention_prunes_payload_but_preserves_delivery_audit(tmp_path: Path) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    store.mark_delivery_sent(
        claim.delivery_id,
        claim_token=claim.token,
        message_id="m1",
    )
    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    assert store.get(_event().dedupe_key) is None
    audit = store.delivery_audit(claim.delivery_id)
    assert audit is not None
    assert audit["status"] == "sent"
    assert audit["marker"] == claim.marker
    assert audit["message_id"] == "m1"
    assert audit["revision_number"] == 1
    assert "event_json" not in audit
    connection = sqlite3.connect(tmp_path / "inbox.db")
    assert connection.execute(
        "SELECT event_json FROM delivery_outbox WHERE delivery_id = ?",
        (claim.delivery_id,),
    ).fetchone()[0] is None
    assert connection.execute(
        "SELECT latest_revision, latest_source_revision FROM mention_event_lineage "
        "WHERE dedupe_key = ?",
        (_event().dedupe_key,),
    ).fetchone() == (1, "2026-07-29T08:00:00Z")
    connection.close()


def test_outbox_claims_each_queued_revision_from_immutable_snapshot(tmp_path: Path) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)
    original = _event(body="revision one")
    revised = _event(body="revision two")

    store.upsert(original, source_revision="2026-07-29T08:00:00Z")
    store.upsert(revised, source_revision="2026-07-29T08:05:00Z")

    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first is not None
    assert first.revision_number == 1
    assert first.event.untrusted.body == "revision one"
    store.mark_delivery_sent(
        first.delivery_id,
        claim_token=first.token,
        message_id="m1",
    )

    second = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert second is not None
    assert second.revision_number == 2
    assert second.event.untrusted.body == "revision two"
    assert second.delivery_id != first.delivery_id
    store.mark_delivery_sent(
        second.delivery_id,
        claim_token=second.token,
        message_id="m2",
    )
    assert store.claim_delivery(DESTINATION, lease_seconds=10) is None


def test_prune_then_same_source_revision_is_noop(tmp_path: Path) -> None:
    now = [NOW]
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: now[0])
    source_revision = "2026-07-29T08:00:00Z"
    original = _event(body="original")
    first_result = store.upsert(original, source_revision=source_revision)
    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first_result.revision_number == 1
    assert first is not None
    store.mark_delivery_sent(
        first.delivery_id,
        claim_token=first.token,
        message_id="m1",
    )

    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    same_revision = store.upsert(
        _event(body="changed body must not be trusted"),
        source_revision=source_revision,
    )

    assert same_revision.created is False
    assert same_revision.content_changed is False
    assert same_revision.stale is True
    assert same_revision.revision_number == 1
    assert store.get(original.dedupe_key) is None
    assert store.pending_delivery_count() == 0
    assert store.claim_delivery(DESTINATION, lease_seconds=10) is None
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 1
    assert connection.execute(
        "SELECT latest_revision, latest_source_revision FROM mention_event_lineage "
        "WHERE dedupe_key = ?",
        (original.dedupe_key,),
    ).fetchone() == (1, source_revision)
    connection.close()


def test_prune_then_newer_source_revision_uses_monotonic_delivery_identity(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: now[0])
    original = _event(body="original")
    first_result = store.upsert(original, source_revision="2026-07-29T08:00:00Z")
    first = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert first_result.revision_number == 1
    assert first is not None
    store.mark_delivery_sent(
        first.delivery_id,
        claim_token=first.token,
        message_id="m1",
    )
    first_audit = store.delivery_audit(first.delivery_id)

    now[0] += timedelta(days=31)
    assert store.prune(retention_days=30) == 1
    reingested = store.upsert(
        _event(body="new after retention"),
        source_revision="2026-08-29T08:00:00Z",
    )

    assert reingested.created is True
    assert reingested.revision_number == 2
    assert store.pending_delivery_count() == 1
    assert store.delivery_audit(first.delivery_id) == first_audit
    second = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert second is not None
    assert second.delivery_id != first.delivery_id
    assert second.revision_number == 2
    assert second.marker != first.marker
    assert second.event.untrusted.body == "new after retention"


def test_pre_snapshot_schema_migrates_idempotently_without_losing_delivery_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "inbox.db"
    pending = _event(event_id="pending", body="pending payload")
    sending = _event(event_id="sending", body="sending payload")
    sent = _event(event_id="sent", body="sent payload")
    timestamp = "2026-07-29T08:00:00Z"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE mention_events (
            dedupe_key TEXT PRIMARY KEY,
            source_platform TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            event_json TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE collector_cursors (
            collector_key TEXT PRIMARY KEY,
            cursor_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delivery_outbox (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            destination TEXT NOT NULL,
            marker TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            lease_until TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(dedupe_key, revision_number, destination)
        );
    """)
    for event in (pending, sending, sent):
        connection.execute(
            "INSERT INTO mention_events VALUES (?, 'github', ?, ?, 'hash', ?, 1, ?, ?)",
            (
                event.dedupe_key,
                event.source.event_id,
                timestamp,
                event_to_json(event),
                timestamp,
                timestamp,
            ),
        )
    connection.execute(
        "INSERT INTO collector_cursors VALUES ('github.notifications', 'cursor-1', ?)",
        (timestamp,),
    )
    rows = (
        (pending.dedupe_key, "pending", 0, None, None),
        (sending.dedupe_key, "sending", 2, None, "2026-07-29T10:00:00Z"),
        (sent.dedupe_key, "sent", 1, "message-sent", None),
    )
    for dedupe_key, status, attempts, message_id, lease_until in rows:
        connection.execute(
            """INSERT INTO delivery_outbox (
                dedupe_key, revision_number, destination, marker, status, attempts,
                message_id, lease_until, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dedupe_key,
                DESTINATION,
                f"marker-{status}",
                status,
                attempts,
                message_id,
                lease_until,
                timestamp,
                timestamp,
            ),
        )
    connection.commit()
    connection.close()

    now = [NOW]
    store = MentionInboxStore(db, clock=lambda: now[0])
    MentionInboxStore(db, clock=lambda: now[0])

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(delivery_outbox)")}
    assert "event_json" in columns
    assert "source_revision" in columns
    migrated = connection.execute(
        "SELECT status, attempts, message_id, lease_until, event_json, source_revision "
        "FROM delivery_outbox "
        "ORDER BY delivery_id"
    ).fetchall()
    assert [(row["status"], row["attempts"]) for row in migrated] == [
        ("pending", 0),
        ("sending", 2),
        ("sent", 1),
    ]
    assert migrated[1]["lease_until"] == "2026-07-29T10:00:00Z"
    assert migrated[2]["message_id"] == "message-sent"
    assert all(row["event_json"] is not None for row in migrated)
    assert all(row["source_revision"] == timestamp for row in migrated)
    assert connection.execute("SELECT COUNT(*) FROM mention_event_lineage").fetchone()[0] == 3
    connection.close()
    assert store.get_cursor("github.notifications") == "cursor-1"
    assert store.pending_delivery_count() == 2
    now[0] += timedelta(hours=1, seconds=1)
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    assert claim.event.untrusted.body == "pending payload"
    assert claim.source_revision == timestamp


def test_future_schema_version_fails_closed_without_mutation(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE future_data (value TEXT NOT NULL);
        INSERT INTO future_data VALUES ('preserve-me');
        PRAGMA user_version = 99;
    """)
    connection.commit()
    connection.close()

    with pytest.raises(
        RuntimeError,
        match=rf"newer than supported schema version {SCHEMA_VERSION}",
    ):
        MentionInboxStore(db, clock=lambda: NOW)

    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    assert connection.execute("SELECT value FROM future_data").fetchall() == [("preserve-me",)]
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall() == [("future_data",)]
    connection.close()


def test_migration_supersedes_unreconstructible_older_revision(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    revision_two = _event(body="revision two")
    timestamp = "2026-07-29T08:00:00Z"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE mention_events (
            dedupe_key TEXT PRIMARY KEY,
            source_platform TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            event_json TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE collector_cursors (
            collector_key TEXT PRIMARY KEY,
            cursor_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delivery_outbox (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            destination TEXT NOT NULL,
            marker TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            message_id TEXT,
            lease_until TEXT,
            error_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(dedupe_key, revision_number, destination)
        );
        PRAGMA user_version = 1;
    """)
    connection.execute(
        "INSERT INTO mention_events VALUES (?, 'github', 'n1', ?, 'hash', ?, 2, ?, ?)",
        (
            revision_two.dedupe_key,
            "2026-07-29T08:05:00Z",
            event_to_json(revision_two),
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        "INSERT INTO collector_cursors VALUES ('github.notifications', 'cursor-2', ?)",
        (timestamp,),
    )
    connection.execute(
        """INSERT INTO delivery_outbox (
            dedupe_key, revision_number, destination, marker, status, attempts,
            lease_until, created_at, updated_at
        ) VALUES (?, 1, ?, 'marker-rev1', 'sending', 3, ?, ?, ?)""",
        (revision_two.dedupe_key, DESTINATION, "2026-07-29T10:00:00Z", timestamp, timestamp),
    )
    connection.execute(
        """INSERT INTO delivery_outbox (
            dedupe_key, revision_number, destination, marker, status, attempts,
            created_at, updated_at
        ) VALUES (?, 2, ?, 'marker-rev2', 'pending', 0, ?, ?)""",
        (revision_two.dedupe_key, DESTINATION, timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    store = MentionInboxStore(db, clock=lambda: NOW)
    MentionInboxStore(db, clock=lambda: NOW)

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT revision_number, marker, status, attempts, lease_until, event_json "
        "FROM delivery_outbox ORDER BY revision_number"
    ).fetchall()
    assert dict(rows[0]) == {
        "revision_number": 1,
        "marker": "marker-rev1",
        "status": "superseded",
        "attempts": 3,
        "lease_until": None,
        "event_json": None,
    }
    assert rows[1]["status"] == "pending"
    assert rows[1]["event_json"] == event_to_json(revision_two)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    connection.close()
    assert store.get_cursor("github.notifications") == "cursor-2"
    assert store.pending_delivery_count() == 1
    claim = store.claim_delivery(DESTINATION, lease_seconds=10)
    assert claim is not None
    assert claim.revision_number == 2
    assert claim.source_revision == "2026-07-29T08:05:00Z"
    assert claim.event.untrusted.body == "revision two"


@pytest.mark.asyncio
async def test_normal_send_posts_once_with_mentions_disabled(tmp_path: Path) -> None:
    db = tmp_path / "inbox.db"
    store = MentionInboxStore(db, clock=lambda: NOW)
    store.upsert(_event(), source_revision="2026-07-29T08:00:00Z")
    discord = _Discord()
    delivery = DiscordMentionDelivery(
        store=store, discord=discord, destination=DESTINATION, lease_seconds=10
    )
    assert await delivery.deliver_once() == "sent"
    assert await delivery.deliver_once() == "idle"
    restarted = DiscordMentionDelivery(
        store=MentionInboxStore(db, clock=lambda: NOW), discord=discord,
        destination=DESTINATION, lease_seconds=10,
    )
    assert await restarted.deliver_once() == "idle"
    assert len(discord.sends) == 1
    assert discord.sends[0][2]["parse"] == []


@pytest.mark.asyncio
async def test_disabled_and_missing_credential_are_degraded_not_fatal(tmp_path: Path) -> None:
    disabled = MentionInboxGatewayService(
        MentionInboxConfig(), None, environ={}, db_path=tmp_path / "disabled.db"
    )
    await disabled.start()
    assert disabled.health()["status"] == "disabled"
    missing = MentionInboxGatewayService(
        _enabled_config(), object(), environ={}, db_path=tmp_path / "missing.db"
    )
    await missing.start()
    assert missing.health()["status"] == "degraded"
    assert missing.health()["error_category"] == "missing_credential"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_enabled", "expected_handler"),
    ((False, False), (True, True)),
)
async def test_execution_handler_is_wired_only_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_enabled: bool,
    expected_handler: bool,
) -> None:
    import plugins.mention_inbox.operational as operational
    from plugins.mention_inbox.thread_session import (
        MentionInboxThreadCoordinator,
        ThreadParticipantReconciliation,
    )

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None
            self.execution_observer = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router

        def set_mention_inbox_execution_observer(self, observer) -> None:
            self.execution_observer = observer

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    reconciled_participants: list[frozenset[str]] = []

    async def reconcile_participants(
        coordinator: MentionInboxThreadCoordinator,
    ) -> ThreadParticipantReconciliation:
        reconciled_participants.append(coordinator._participant_user_ids)
        return ThreadParticipantReconciliation(0, 0, 0, 0)

    monkeypatch.setattr(
        MentionInboxThreadCoordinator,
        "reconcile_thread_participants",
        reconcile_participants,
    )
    adapter = Adapter()
    config = MentionInboxConfig(
        enabled=True,
        repositories=("silviahealth/content",),
        destination=DESTINATION,
        action_sessions_enabled=True,
        user_message_mode="standard_agent",
        proposal_bot_mention="<@1525050677381279865>",
        authorized_approver_ids=("396159160201658368",),
        execution_enabled=execution_enabled,
        execution_mode="kanban",
        execution_workspace="Documents/hermes-workspaces/silviahealth-content",
        terminal_cwd="/Users/test",
    )
    service = MentionInboxGatewayService(
        config,
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / f"wired-{execution_enabled}.db",
    )

    await service.start()

    assert adapter.router is not None
    assert adapter.router._conversation_responder is not None
    assert adapter.router._user_message_mode == "standard_agent"
    assert (adapter.router._approval_handler is not None) is expected_handler
    assert (adapter.execution_observer is not None) is execution_enabled
    assert service._runtime is not None
    coordinator = service._runtime.delivery._thread_coordinator
    assert coordinator._executor_hint == "kanban"
    assert coordinator._approval_available is expected_handler
    assert coordinator._participant_parent_channel_id == DESTINATION.split(":", 1)[1]
    assert reconciled_participants == [frozenset({"396159160201658368"})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciliation_case", "expected_category"),
    (
        ("failed", "discord_thread_participant_sync_failed"),
        ("overflow", "discord_thread_participant_reconciliation_incomplete"),
    ),
)
async def test_participant_reconciliation_failure_degrades_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconciliation_case: str,
    expected_category: str,
) -> None:
    import plugins.mention_inbox.operational as operational
    from plugins.mention_inbox.thread_session import (
        MentionInboxThreadCoordinator,
        ThreadParticipantReconciliation,
    )

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None
            self.execution_observer = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router

        def set_mention_inbox_execution_observer(self, observer) -> None:
            self.execution_observer = observer

    async def failed_reconciliation(
        coordinator: MentionInboxThreadCoordinator,
    ) -> ThreadParticipantReconciliation:
        return (
            ThreadParticipantReconciliation(1, 0, 0, 1)
            if reconciliation_case == "failed"
            else ThreadParticipantReconciliation(1000, 1000, 0, 0, 1)
        )

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    monkeypatch.setattr(
        MentionInboxThreadCoordinator,
        "reconcile_thread_participants",
        failed_reconciliation,
    )
    adapter = Adapter()
    service = MentionInboxGatewayService(
        MentionInboxConfig(
            enabled=True,
            repositories=("silviahealth/content",),
            destination=DESTINATION,
            action_sessions_enabled=True,
            proposal_bot_mention="<@1525050677381279865>",
            authorized_approver_ids=("396159160201658368",),
            execution_enabled=True,
            execution_mode="kanban",
            execution_workspace="Documents/hermes-workspaces/silviahealth-content",
            terminal_cwd="/Users/test",
        ),
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / "failed-reconciliation.db",
    )

    await service.start()

    assert service.health()["status"] == "degraded"
    assert service.health()["error_category"] == expected_category
    assert adapter.router is None
    assert adapter.execution_observer is None
    assert service._router_installed is False
    assert service._execution_observer_installed is False


@pytest.mark.asyncio
async def test_cancelled_participant_reconciliation_rolls_back_installed_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.mention_inbox.operational as operational
    from plugins.mention_inbox.thread_session import MentionInboxThreadCoordinator

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None
            self.execution_observer = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router

        def set_mention_inbox_execution_observer(self, observer) -> None:
            self.execution_observer = observer

    reconciliation_started = asyncio.Event()

    async def blocked_reconciliation(
        coordinator: MentionInboxThreadCoordinator,
    ):
        reconciliation_started.set()
        await asyncio.Future()

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    monkeypatch.setattr(
        MentionInboxThreadCoordinator,
        "reconcile_thread_participants",
        blocked_reconciliation,
    )
    adapter = Adapter()
    service = MentionInboxGatewayService(
        MentionInboxConfig(
            enabled=True,
            repositories=("silviahealth/content",),
            destination=DESTINATION,
            action_sessions_enabled=True,
            proposal_bot_mention="<@1525050677381279865>",
            authorized_approver_ids=("396159160201658368",),
            execution_enabled=True,
            execution_mode="kanban",
            execution_workspace="Documents/hermes-workspaces/silviahealth-content",
            terminal_cwd="/Users/test",
        ),
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / "cancelled-reconciliation.db",
    )

    start_task = asyncio.create_task(service.start())
    await asyncio.wait_for(reconciliation_started.wait(), timeout=2)
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert adapter.router is None
    assert adapter.execution_observer is None
    assert service._router_installed is False
    assert service._execution_observer_installed is False


@pytest.mark.asyncio
async def test_router_install_failure_rolls_back_published_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.mention_inbox.operational as operational

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router
            if router is not None:
                raise RuntimeError("persistent view restore failed")

        def set_mention_inbox_execution_observer(self, observer) -> None:
            return None

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    adapter = Adapter()
    service = MentionInboxGatewayService(
        MentionInboxConfig(
            enabled=True,
            repositories=("silviahealth/content",),
            destination=DESTINATION,
            action_sessions_enabled=True,
            proposal_bot_mention="<@1525050677381279865>",
            authorized_approver_ids=("396159160201658368",),
        ),
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / "router-install-failed.db",
    )

    await service.start()

    assert service.health()["error_category"] == "startup_failed"
    assert adapter.router is None
    assert service._router_installed is False


@pytest.mark.asyncio
async def test_external_execution_wires_repository_worktree_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.mention_inbox.operational as operational

    class Client:
        def __init__(self, *, token: str) -> None:
            assert token == "opaque-token"

    class Adapter:
        def __init__(self) -> None:
            self.router = None
            self.execution_observer = None

        def set_mention_inbox_router(self, router) -> None:
            self.router = router

        def set_mention_inbox_execution_observer(self, observer) -> None:
            self.execution_observer = observer

    monkeypatch.setattr(operational, "GitHubNotificationsClient", Client)
    monkeypatch.setattr(operational.MentionInboxRuntime, "start", lambda self: None)
    adapter = Adapter()
    config = MentionInboxConfig(
        enabled=True,
        include_public_actionable_activity=True,
        external_repository_actions="inspect_only",
        destination=DESTINATION,
        action_sessions_enabled=True,
        proposal_bot_mention="<@1525050677381279865>",
        authorized_approver_ids=("396159160201658368",),
        execution_enabled=True,
        execution_workspace_root="Documents/hermes-workspaces",
        terminal_cwd="/Users/test",
    )
    service = MentionInboxGatewayService(
        config,
        adapter,
        environ={"GITHUB_PAT_TOKEN": "opaque-token"},
        db_path=tmp_path / "external-worktrees.db",
    )

    await service.start()

    handler = adapter.router._approval_handler
    assert handler is not None
    assert handler._workspace_manager is not None
    assert handler._workspace == "/Users/test/Documents/hermes-workspaces"
    assert adapter.execution_observer._workspace == (
        "/Users/test/Documents/hermes-workspaces"
    )


def test_collector_bounds_external_title_body_before_persistence() -> None:
    collector = GitHubNotificationCollector(
        target_id="github:me", allowed_repositories={"silviahealth/content"}
    )
    notification = {
        "id": "n1", "reason": "mention", "unread": True,
        "updated_at": "2026-07-29T08:00:00Z",
        "subject": {
            "title": "t" * 5000,
            "type": "PullRequest",
            "url": "https://api.github.com/repos/silviahealth/content/pulls/1",
        },
        "repository": {
            "node_id": "repo-node", "full_name": "silviahealth/content"
        },
    }
    detail = {
        "node_id": "issue-node", "body": "b" * 50000,
        "html_url": "https://github.com/silviahealth/content/pull/1",
        "user": {"node_id": "actor", "type": "User"},
    }
    collected = collector.normalize(notification, detail)
    assert collected is not None
    assert len(collected.event.untrusted.title) <= 500
    assert len(collected.event.untrusted.body) <= 4000


def test_config_accepts_bounded_nested_notion_pilot_and_rejects_unsafe_scope() -> None:
    page_id = "11111111-1111-1111-1111-111111111111"
    config = parse_mention_inbox_config({
        "mention_inbox": {
            "notion": {
                "enabled": True,
                "credential_env": "NOTION_TOKEN",
                "page_ids": [page_id],
                "poll_interval_seconds": 180,
            }
        }
    })

    assert config.notion == NotionInboxConfig(
        enabled=True,
        credential_env="NOTION_TOKEN",
        page_ids=(page_id,),
        poll_interval_seconds=180,
    )
    for notion in (
        {"enabled": True, "credential_env": "NOTION_OTHER", "page_ids": [page_id]},
        {"enabled": True, "page_ids": []},
        {"enabled": True, "page_ids": ["not-a-notion-id"]},
        {"enabled": True, "page_ids": [page_id, page_id]},
        {"enabled": True, "page_ids": [page_id], "poll_interval_seconds": 60},
        {"enabled": True, "page_ids": [page_id], "recursive_workspace_scan": True},
    ):
        with pytest.raises(ValueError):
            parse_mention_inbox_config({"mention_inbox": {"notion": notion}})
