"""Durable one-thread-per-subject mention inbox coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox import MentionInboxStore, ingest_event
from plugins.mention_inbox.proposals import ProposalStatus, build_work_proposal
from plugins.mention_inbox.thread_session import MentionInboxThreadCoordinator

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
BOT_MENTION = "<@1525050525641805886>"
PARENT_CHANNEL = "1531851208858275860"
ALT_PARENT_CHANNEL = "1531851208858275861"


def _event(
    *,
    event_id: str = "RC_123",
    subject_key: str = "github:R_repo:PR_7",
    source_revision: str = "2026-07-29T10:01:00Z",
    head_sha: str = "head-1",
    body: str = "이 줄을 확인해 주세요.",
    actionable_kind: str = "own_pr_review_comment",
    disposition: str = "review_needed",
    include_brief: bool = True,
    repository: str = "silviahealth/content",
    subject_owned_by_target: bool = False,
):
    metadata: dict[str, object] = {
        "actionable_kind": actionable_kind,
        "repository": repository,
        "repository_private": False,
        "subject_owned_by_target": subject_owned_by_target,
        "subject_type": "PullRequest",
        "subject_number": 7,
        "subject_key": subject_key,
        "subject_head_sha": head_sha,
        "source_revision": source_revision,
        "actor_login": "alice",
    }
    if include_brief:
        metadata["preapproval_brief"] = {
            "schema_version": 1,
            "disposition": disposition,
            "summary": body[:350],
            "findings": [
                {
                    "source_event_id": event_id,
                    "body": body[:250],
                    "source_url": (
                        "https://github.com/silviahealth/content/pull/7"
                        "#discussion_r123"
                    ),
                    "path": "plugins/mention_inbox/voice.py",
                    "line": 181,
                    "review_id": "991",
                    "commit_id": head_sha or None,
                }
            ],
            "source_revision": source_revision,
            "head_sha": head_sha or None,
            "approvable": disposition in {"action_required", "review_needed"},
        }
    return ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": event_id},
        "actor": {"actor_id": "U_alice", "kind": "user"},
        "target": {"target_id": "U_recent", "kind": "user"},
        "thread": {"thread_id": subject_key, "container_id": "R_repo"},
        "requested_action": "reply",
        "deadline": None,
        "untrusted": {
            "title": "Inbox contract",
            "body": body,
            "action_detail": actionable_kind,
            "source_url": "https://github.com/silviahealth/content/pull/7#discussion_r123",
            "metadata": metadata,
        },
    })


class _Discord:
    def __init__(self) -> None:
        self.threads: dict[str, str] = {}
        self.active_threads: dict[str, bool] = {}
        self.thread_parents: dict[str, str] = {}
        self.archived_threads: set[str] = set()
        self.locked_threads: set[str] = set()
        self.activated_threads: list[str] = []
        self.created: list[tuple[str, str, int]] = []
        self.participant_syncs: list[tuple[str, frozenset[str]]] = []
        self.participant_failures: set[str] = set()
        self.thread_members: dict[str, set[str]] = {}
        self.marked: list[str] = []
        self.messages: dict[str, list[tuple[str, str]]] = {}
        self.proposal_controls: list[dict[str, object]] = []
        self.operations: list[str] = []
        self.next_message = 1

    def remember_parent_message(
        self, parent_message_id: str, parent_channel_id: str
    ) -> None:
        self.operations.append(
            f"remember_parent:{parent_message_id}:{parent_channel_id}"
        )

    async def find_anchored_thread(self, parent_message_id: str) -> str | None:
        return self.threads.get(parent_message_id)

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str:
        existing = self.threads.get(parent_message_id)
        if existing is not None:
            return existing
        thread_id = f"thread-{len(self.threads) + 1}"
        self.threads[parent_message_id] = thread_id
        self.active_threads[thread_id] = True
        self.thread_parents[thread_id] = PARENT_CHANNEL
        self.created.append((parent_message_id, name, auto_archive_duration))
        self.operations.append("create_thread")
        return thread_id

    async def ensure_thread_participants(
        self,
        thread_id: str,
        user_ids: frozenset[str],
    ) -> None:
        self.participant_syncs.append((thread_id, user_ids))
        self.operations.append("sync_participants")
        if thread_id in self.archived_threads or thread_id in self.locked_threads:
            raise RuntimeError("thread is not active")
        if thread_id in self.participant_failures:
            raise RuntimeError("participant sync failed")
        self.thread_members.setdefault(thread_id, set()).update(user_ids)

    async def is_thread_active(self, thread_id: str) -> bool:
        return (
            self.active_threads.get(thread_id, False)
            and thread_id not in self.archived_threads
            and thread_id not in self.locked_threads
        )

    async def thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool:
        return (
            self.thread_parents.get(thread_id, PARENT_CHANNEL)
            == parent_channel_id
        )

    async def activate_thread(self, thread_id: str) -> None:
        if thread_id in self.locked_threads:
            raise RuntimeError("thread is locked")
        if (
            thread_id not in self.archived_threads
            and self.active_threads.get(thread_id, False)
        ):
            return
        self.archived_threads.discard(thread_id)
        self.active_threads[thread_id] = True
        self.activated_threads.append(thread_id)

    def mark_thread_participation(self, thread_id: str) -> None:
        self.marked.append(thread_id)
        self.operations.append("mark_participation")

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None:
        for message_id, existing in self.messages.get(thread_id, [])[-limit:]:
            if existing == content:
                return message_id
        return None

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        self.operations.append("send_to_thread")
        message_id = f"proposal-message-{self.next_message}"
        self.next_message += 1
        self.messages.setdefault(thread_id, []).append((message_id, content))
        return message_id

    async def send_proposal_to_thread(
        self,
        thread_id: str,
        content: str,
        *,
        proposal_id: str,
        proposal_revision: int,
        approval_offered: bool,
    ) -> str:
        self.proposal_controls.append({
            "proposal_id": proposal_id,
            "proposal_revision": proposal_revision,
            "approval_offered": approval_offered,
        })
        return await self.send_to_thread(thread_id, content)


def _coordinator(
    path: Path,
    discord: _Discord,
    *,
    approval_available: bool = True,
    external_repository_actions: str = "disabled",
    participant_user_ids: frozenset[str] = frozenset(),
    participant_parent_channel_id: str | None = None,
    advisor: object | None = None,
) -> MentionInboxThreadCoordinator:
    return MentionInboxThreadCoordinator(
        store=MentionInboxStore(path, clock=lambda: NOW),
        discord=discord,
        bot_mention=BOT_MENTION,
        approval_available=approval_available,
        trusted_repositories=frozenset({"silviahealth/content"}),
        external_repository_actions=external_repository_actions,
        participant_user_ids=participant_user_ids,
        participant_parent_channel_id=(
            PARENT_CHANNEL
            if participant_parent_channel_id is None
            else participant_parent_channel_id
        ),
        advisor=advisor,
    )


@pytest.mark.asyncio
async def test_same_subject_creates_one_thread_and_one_proposal(tmp_path: Path) -> None:
    discord = _Discord()
    path = tmp_path / "inbox.db"
    coordinator = _coordinator(path, discord)
    event = _event()

    first = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    second = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert first.discord_thread_id == second.discord_thread_id == "thread-1"
    assert len(discord.created) == 1
    assert len(discord.messages["thread-1"]) == 1
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    assert discord.proposal_controls == [{
        "proposal_id": proposal.proposal_id,
        "proposal_revision": 1,
        "approval_offered": True,
    }]
    assert discord.marked == ["thread-1", "thread-1"]


@pytest.mark.asyncio
async def test_new_thread_syncs_participants_before_proposal(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(
        tmp_path / "inbox.db",
        discord,
        participant_user_ids=frozenset({"222222", "111111"}),
    )

    session = await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert discord.participant_syncs == [
        (session.discord_thread_id, frozenset({"222222", "111111"}))
    ]
    assert discord.operations.index("sync_participants") < discord.operations.index(
        "send_to_thread"
    )


@pytest.mark.asyncio
async def test_recovered_thread_syncs_participants_before_proposal(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    discord.threads["parent-1"] = "thread-existing"
    coordinator = _coordinator(
        tmp_path / "inbox.db",
        discord,
        participant_user_ids=frozenset({"111111"}),
    )

    session = await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert session.discord_thread_id == "thread-existing"
    assert discord.created == []
    assert discord.participant_syncs == [
        ("thread-existing", frozenset({"111111"}))
    ]


@pytest.mark.asyncio
async def test_later_revision_resyncs_existing_thread_participants(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    coordinator = _coordinator(
        tmp_path / "inbox.db",
        discord,
        participant_user_ids=frozenset({"111111"}),
    )

    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    discord.archived_threads.add("thread-1")
    discord.active_threads["thread-1"] = False
    await coordinator.ensure_thread(
        _event(
            event_id="RC_124",
            source_revision="2026-07-29T10:02:00Z",
            body="두 번째 줄도 확인해 주세요.",
        ),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )

    assert len(discord.created) == 1
    assert discord.activated_threads == ["thread-1"]
    assert discord.participant_syncs == [
        ("thread-1", frozenset({"111111"})),
        ("thread-1", frozenset({"111111"})),
    ]


@pytest.mark.asyncio
async def test_empty_participant_set_is_explicit_no_op(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(tmp_path / "inbox.db", discord)

    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert discord.participant_syncs == []


@pytest.mark.asyncio
async def test_startup_reconciliation_repairs_only_active_threads(
    tmp_path: Path,
) -> None:
    store = MentionInboxStore(tmp_path / "inbox.db", clock=lambda: NOW)

    def record_session(subject: str, thread_id: str | None) -> None:
        event = _event(event_id=f"event-{subject}", subject_key=subject)
        store.reserve_work_item_session(
            subject,
            event.dedupe_key,
            "2026-07-29T10:01:00Z",
        )
        store.prepare_work_item_parent(
            subject, f"parent-{subject}", PARENT_CHANNEL
        )
        if thread_id is not None:
            store.record_work_item_thread(
                subject,
                f"parent-{subject}",
                PARENT_CHANNEL,
                thread_id,
            )

    record_session("github:R_repo:PR_active", "thread-active")
    record_session("github:R_repo:PR_archived", "thread-archived")
    record_session("github:R_repo:PR_failed", "thread-failed")
    record_session("github:R_repo:PR_missing", None)
    record_session("github:R_repo:PR_other_destination", "thread-other")
    discord = _Discord()
    discord.active_threads.update({
        "thread-active": True,
        "thread-archived": False,
        "thread-failed": True,
        "thread-other": True,
    })
    discord.thread_parents.update({
        "thread-active": PARENT_CHANNEL,
        "thread-archived": PARENT_CHANNEL,
        "thread-failed": PARENT_CHANNEL,
        "thread-other": ALT_PARENT_CHANNEL,
    })
    discord.participant_failures.add("thread-failed")
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
        participant_parent_channel_id=PARENT_CHANNEL,
    )

    first = await coordinator.reconcile_thread_participants()

    assert first.examined == 5
    assert first.repaired == 1
    assert first.skipped == 3
    assert first.failed == 1
    assert discord.thread_members == {
        "thread-active": {"789391209067446323"}
    }

    discord.participant_failures.clear()
    second = await coordinator.reconcile_thread_participants()

    assert second.examined == 5
    assert second.repaired == 2
    assert second.skipped == 3
    assert second.failed == 0
    assert discord.thread_members == {
        "thread-active": {"789391209067446323"},
        "thread-failed": {"789391209067446323"},
    }


@pytest.mark.asyncio
async def test_startup_reconciliation_reports_sessions_beyond_limit(
    tmp_path: Path,
) -> None:
    class SnapshotStore(MentionInboxStore):
        def active_work_item_session_count(self) -> int:
            raise AssertionError(
                "overflow must share the session-list query snapshot"
            )

    store = SnapshotStore(tmp_path / "inbox.db", clock=lambda: NOW)
    discord = _Discord()
    for index in range(1001):
        subject = f"github:R_repo:PR_{index:04d}"
        event = _event(event_id=f"event-{index}", subject_key=subject)
        parent = f"parent-{index}"
        thread = f"thread-{index}"
        store.reserve_work_item_session(
            subject,
            event.dedupe_key,
            "2026-07-29T10:01:00Z",
        )
        store.prepare_work_item_parent(subject, parent, PARENT_CHANNEL)
        store.record_work_item_thread(subject, parent, PARENT_CHANNEL, thread)
        discord.active_threads[thread] = True
    coordinator = MentionInboxThreadCoordinator(
        store=store,
        discord=discord,
        bot_mention=BOT_MENTION,
        trusted_repositories=frozenset({"silviahealth/content"}),
        participant_user_ids=frozenset({"789391209067446323"}),
    )

    result = await coordinator.reconcile_thread_participants(limit=1000)

    assert result.examined == 1000
    assert result.repaired == 1000
    assert result.skipped == 0
    assert result.failed == 0
    assert result.overflow == 1


@pytest.mark.asyncio
async def test_approval_offer_is_bound_only_when_execution_and_preflight_allow_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "enabled.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=True).ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" not in content
    assert "`리뷰 반영해줘`" in content
    assert binding is not None and binding.approval_offered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "inspect_only"])
async def test_external_inspection_modes_never_offer_host_execution(
    tmp_path: Path,
    mode: str,
) -> None:
    path = tmp_path / f"{mode}.db"
    discord = _Discord()

    await _coordinator(
        path,
        discord,
        approval_available=True,
        external_repository_actions=mode,
    ).ensure_thread(
        _event(repository="external/project", subject_owned_by_target=True),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id,
        proposal.revision,
    )
    assert binding is not None and binding.approval_offered is False
    assert proposal.allowed_actions == ("read_repository",)
    assert "수정 시작" not in discord.messages["thread-1"][0][1]


@pytest.mark.asyncio
async def test_external_own_pr_write_excludes_repository_code_execution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "own-write.db"
    discord = _Discord()

    await _coordinator(
        path,
        discord,
        approval_available=True,
        external_repository_actions="own_pr_write",
    ).ensure_thread(
        _event(repository="external/project", subject_owned_by_target=True),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id,
        proposal.revision,
    )
    assert binding is not None and binding.approval_offered is True
    assert "edit_scoped_files" in proposal.allowed_actions
    assert "push_current_branch" in proposal.allowed_actions
    assert "run_targeted_tests" not in proposal.allowed_actions
    assert "run_tests" not in proposal.allowed_actions


@pytest.mark.asyncio
async def test_execution_unavailable_renders_review_only_and_binds_false(
    tmp_path: Path,
) -> None:
    path = tmp_path / "disabled.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=False).ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" not in content
    assert "자동 실행이 연결되지 않아" in content
    assert binding is not None and binding.approval_offered is False


@pytest.mark.asyncio
async def test_execution_activation_reconciles_same_head_to_one_new_revision(
    tmp_path: Path,
) -> None:
    path = tmp_path / "activation.db"
    event = _event()
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    await _coordinator(path, discord, approval_available=False).ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    coordinator = _coordinator(path, discord, approval_available=True)
    assert await coordinator.reconcile_execution_activation() == 1
    assert await coordinator.reconcile_execution_activation() == 0

    restored = MentionInboxStore(path)
    latest = restored.get_latest_proposal(event.thread.thread_id)
    assert latest is not None and latest.revision == 2
    previous = restored.get_proposal(latest.proposal_id, 1)
    assert previous is not None
    assert previous.status is ProposalStatus.NEEDS_REAPPROVAL
    assert {
        "switch_to_pr_branch",
        "commit_changes",
        "push_current_branch",
    }.issubset(latest.allowed_actions)
    binding = restored.get_proposal_message_binding(
        latest.proposal_id, latest.revision
    )
    assert binding is not None and binding.approval_offered is True
    assert len(discord.messages["thread-1"]) == 2
    assert "실행 기능이 활성화" in discord.messages["thread-1"][-1][1]


@pytest.mark.asyncio
async def test_execution_activation_skips_archived_thread_at_startup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archived-activation.db"
    event = _event()
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    await _coordinator(path, discord, approval_available=False).ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    discord.archived_threads.add("thread-1")
    discord.active_threads["thread-1"] = False

    coordinator = _coordinator(path, discord, approval_available=True)

    assert await coordinator.reconcile_execution_activation() == 0
    latest = MentionInboxStore(path).get_latest_proposal(event.thread.thread_id)
    assert latest is not None and latest.revision == 1
    assert discord.activated_threads == []
    assert len(discord.messages["thread-1"]) == 1


@pytest.mark.asyncio
async def test_execution_activation_skips_thread_from_other_destination(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cross-destination-activation.db"
    event = _event()
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.upsert(event, source_revision="2026-07-29T10:01:00Z")
    discord = _Discord()
    await _coordinator(path, discord, approval_available=False).ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    discord.thread_parents["thread-1"] = "other-destination"

    coordinator = _coordinator(
        path,
        discord,
        approval_available=True,
        participant_parent_channel_id=PARENT_CHANNEL,
    )

    assert await coordinator.reconcile_execution_activation() == 0
    latest = MentionInboxStore(path).get_latest_proposal(event.thread.thread_id)
    assert latest is not None and latest.revision == 1
    assert len(discord.messages["thread-1"]) == 1


@pytest.mark.asyncio
async def test_existing_thread_delivery_fails_closed_after_destination_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cross-destination-delivery.db"
    discord = _Discord()
    coordinator = _coordinator(
        path,
        discord,
        participant_parent_channel_id=PARENT_CHANNEL,
    )
    event = _event()
    await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    discord.thread_parents["thread-1"] = "other-destination"

    with pytest.raises(
        RuntimeError,
        match="configured Discord destination",
    ):
        await coordinator.deliver_to_existing_thread(
            _event(
                body="새 목적지에서만 전달되어야 합니다.",
                source_revision="2026-07-29T10:02:00Z",
            ),
            source_revision="2026-07-29T10:02:00Z",
        )

    assert len(discord.messages["thread-1"]) == 1


@pytest.mark.asyncio
async def test_participant_writes_are_preceded_by_delivery_checkpoints(
    tmp_path: Path,
) -> None:
    checkpoints: list[int] = []

    class CheckpointDiscord(_Discord):
        async def activate_thread(self, thread_id: str) -> None:
            assert len(checkpoints) >= 2
            await super().activate_thread(thread_id)

        async def ensure_thread_participants(
            self,
            thread_id: str,
            user_ids: frozenset[str],
        ) -> None:
            assert len(checkpoints) >= 3
            await super().ensure_thread_participants(thread_id, user_ids)

    async def checkpoint() -> None:
        checkpoints.append(len(checkpoints) + 1)

    discord = CheckpointDiscord()
    coordinator = _coordinator(
        tmp_path / "participant-checkpoints.db",
        discord,
        participant_user_ids=frozenset({"789391209067446323"}),
    )

    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
        delivery_checkpoint=checkpoint,
    )

    assert len(checkpoints) >= 4


@pytest.mark.asyncio
async def test_non_approvable_preflight_stays_review_only_when_execution_exists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stale.db"
    discord = _Discord()
    await _coordinator(path, discord, approval_available=True).ensure_thread(
        _event(disposition="possibly_stale"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    proposal = MentionInboxStore(path).get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    binding = MentionInboxStore(path).get_proposal_message_binding(
        proposal.proposal_id, proposal.revision
    )
    assert f"{BOT_MENTION} 승인" not in content
    assert "현재 근거만으로 자동 변경하지 않고" in content
    assert binding is not None and binding.approval_offered is False


@pytest.mark.asyncio
async def test_review_summary_proposal_uses_concrete_review_evidence(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(actionable_kind="own_pr_review_summary"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "확인한 내용" in content
    assert "이 줄을 확인해 주세요" in content
    assert "plugins/mention_inbox/voice.py:181" in content
    assert "현재 HEAD에서 확인이 필요한 리뷰 요청" in content
    assert "원본 event와 최신 repository 상태" not in content


@pytest.mark.asyncio
async def test_stale_review_explains_that_mutation_is_not_yet_approvable(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(disposition="possibly_stale"),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "이전 commit의 의견일 수 있어" in content
    assert "먼저 읽기 전용으로 현재 상태를 다시 확인" in content


@pytest.mark.asyncio
async def test_missing_preapproval_evidence_fails_closed_in_proposal(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(include_brief=False),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    content = discord.messages["thread-1"][0][1]
    assert "상세를 안전하게 확인하지 못했어요" in content
    assert "먼저 읽기 전용으로 원문과 현재 HEAD를 다시 확인" in content


@pytest.mark.asyncio
async def test_interrupted_creation_recovers_existing_anchored_thread(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.db"
    store = MentionInboxStore(path, clock=lambda: NOW)
    event = _event()
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T10:01:00Z",
    )
    store.prepare_work_item_parent(
        event.thread.thread_id, "parent-1", PARENT_CHANNEL
    )

    discord = _Discord()
    discord.threads["parent-1"] = "existing-thread"
    session = await _coordinator(path, discord).ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert session.discord_thread_id == "existing-thread"
    assert session.parent_channel_id == PARENT_CHANNEL
    assert discord.created == []
    assert discord.operations[0] == (
        f"remember_parent:parent-1:{PARENT_CHANNEL}"
    )
    assert len(discord.messages["existing-thread"]) == 1


@pytest.mark.asyncio
async def test_restart_rehydrates_durable_parent_before_reusing_recorded_thread(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recorded-thread-restart.db"
    event = _event()
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T10:01:00Z",
    )
    store.record_work_item_thread(
        event.thread.thread_id,
        "parent-1",
        PARENT_CHANNEL,
        "existing-thread",
    )
    discord = _Discord()

    session = await _coordinator(path, discord).ensure_thread(
        event,
        parent_message_id="parent-1",
        parent_channel_id=PARENT_CHANNEL,
        source_revision="2026-07-29T10:01:00Z",
    )

    assert session.discord_thread_id == "existing-thread"
    assert discord.operations[0] == (
        f"remember_parent:parent-1:{PARENT_CHANNEL}"
    )
    assert discord.created == []


@pytest.mark.asyncio
async def test_durable_parent_channel_config_mismatch_fails_before_discord_work(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parent-channel-mismatch.db"
    event = _event()
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T10:01:00Z",
    )
    store.prepare_work_item_parent(
        event.thread.thread_id, "parent-1", ALT_PARENT_CHANNEL
    )
    discord = _Discord()

    with pytest.raises(
        RuntimeError,
        match="configured Discord destination",
    ):
        await _coordinator(path, discord).ensure_thread(
            event,
            parent_message_id="parent-1",
            parent_channel_id=PARENT_CHANNEL,
            source_revision="2026-07-29T10:01:00Z",
        )

    assert discord.operations == []
    assert discord.created == []


@pytest.mark.asyncio
async def test_new_source_revision_reuses_thread_and_posts_r2(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    discord = _Discord()
    coordinator = _coordinator(path, discord)
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    revised_event = _event(
        event_id="RC_124",
        source_revision="2026-07-29T10:02:00Z",
        head_sha="head-2",
    )
    await coordinator.ensure_thread(
        revised_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )
    await coordinator.ensure_thread(
        revised_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )

    store = MentionInboxStore(path, clock=lambda: NOW)
    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None
    assert latest.revision == 2
    assert latest.source_dedupe_key == revised_event.dedupe_key
    assert latest.head_sha == "head-2"
    assert latest.status is ProposalStatus.PENDING
    assert len(discord.created) == 1
    assert len(discord.messages["thread-1"]) == 2


@pytest.mark.asyncio
async def test_same_rendered_text_does_not_reuse_an_older_revision_message(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inbox.db"
    discord = _Discord()
    coordinator = _coordinator(path, discord)
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    await coordinator.ensure_thread(
        _event(
            event_id="RC_124",
            source_revision="2026-07-29T10:02:00Z",
            head_sha="head-2",
        ),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:02:00Z",
    )
    third_event = _event(
        event_id="RC_125",
        source_revision="2026-07-29T10:03:00Z",
        head_sha="head-3",
    )
    await coordinator.ensure_thread(
        third_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:03:00Z",
    )
    await coordinator.ensure_thread(
        third_event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:03:00Z",
    )

    store = MentionInboxStore(path, clock=lambda: NOW)
    latest = store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None and latest.revision == 3
    previous = store.get_proposal(latest.proposal_id, 2)
    assert previous is not None
    previous_binding = store.get_proposal_message_binding(
        previous.proposal_id, previous.revision
    )
    latest_binding = store.get_proposal_message_binding(
        latest.proposal_id, latest.revision
    )
    assert previous_binding is not None
    assert latest_binding is not None
    assert latest_binding.message_id != previous_binding.message_id
    assert len(discord.messages["thread-1"]) == 3
    assert (
        discord.messages["thread-1"][1][1]
        == discord.messages["thread-1"][2][1]
    )


@pytest.mark.asyncio
async def test_different_subjects_create_different_threads(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(tmp_path / "inbox.db", discord)
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    await coordinator.ensure_thread(
        _event(
            event_id="IC_8",
            subject_key="github:R_repo:I_8",
            head_sha="",
        ),
        parent_message_id="parent-2",
        source_revision="2026-07-29T10:01:00Z",
    )
    assert {value for value in discord.threads.values()} == {"thread-1", "thread-2"}


@pytest.mark.asyncio
async def test_pending_proposal_is_local_no_tools_and_omits_full_body(
    tmp_path: Path,
) -> None:
    body = "앞부분 " + ("x" * 1000) + " FULL_BODY_SENTINEL"
    discord = _Discord()
    await _coordinator(tmp_path / "inbox.db", discord).ensure_thread(
        _event(body=body),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    content = discord.messages["thread-1"][0][1]
    assert "FULL_BODY_SENTINEL" not in content
    assert "제 추천" in content
    assert "`리뷰 반영해줘`" in content
    assert BOT_MENTION not in content
    assert not hasattr(discord, "run_agent")
    assert not hasattr(discord, "execute_tool")

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_available", "expected_new_offer"),
    ((False, False), (True, True)),
)
async def test_legacy_pending_is_reconciled_once_from_new_hydration(
    tmp_path: Path,
    approval_available: bool,
    expected_new_offer: bool,
) -> None:
    path = tmp_path / f"legacy-{approval_available}.db"
    store = MentionInboxStore(path, clock=lambda: NOW)
    event = _event()
    store.reserve_work_item_session(
        event.thread.thread_id,
        event.dedupe_key,
        "2026-07-29T09:00:00Z",
    )
    store.record_work_item_thread(
        event.thread.thread_id, "parent-1", PARENT_CHANNEL, "thread-1"
    )
    legacy = build_work_proposal(
        revision=1,
        source_dedupe_key=event.dedupe_key,
        source_revision="2026-07-29T09:00:00Z",
        subject_key=event.thread.thread_id,
        head_sha="old-head",
        goal="과거 일반 제안",
        steps=("과거 요청을 확인한다.",),
        allowed_actions=("read_repository",),
        forbidden_actions=("edit_files", "merge", "deploy", "read_secrets"),
        verification=("새 hydration 필요",),
        executor_hint="direct",
    )
    store.create_proposal(legacy)
    store.record_proposal_message(
        legacy.proposal_id,
        legacy.revision,
        "legacy-message",
        approval_offered=False,
    )
    discord = _Discord()
    coordinator = _coordinator(
        path,
        discord,
        approval_available=approval_available,
    )

    first = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    second = await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    restarted = MentionInboxStore(path)
    old_binding = restarted.get_proposal_message_binding(
        legacy.proposal_id, legacy.revision
    )
    old_proposal = restarted.get_proposal(legacy.proposal_id, legacy.revision)
    latest = restarted.get_latest_proposal(event.thread.thread_id)
    assert first == second
    assert old_binding is not None and old_binding.approval_offered is False
    assert old_proposal is not None
    assert old_proposal.status is ProposalStatus.NEEDS_REAPPROVAL
    assert latest is not None and latest.revision == 2
    latest_binding = restarted.get_proposal_message_binding(
        latest.proposal_id, latest.revision
    )
    assert latest_binding is not None
    assert latest_binding.approval_offered is expected_new_offer
    assert len(discord.messages["thread-1"]) == 1
    rendered = discord.messages["thread-1"][0][1]
    expected_notice = (
        "실행 기능이 활성화되어"
        if expected_new_offer
        else "이전 실행 요청은 사용하지 않고"
    )
    assert expected_notice in rendered
    assert f"{BOT_MENTION} 승인" not in rendered
    assert ("`리뷰 반영해줘`" in rendered) is expected_new_offer


class _StubAdvisor:
    """Records calls and returns a fixed advisory."""

    _LABELLED = (
        "요청: 명세와 import가 불일치한다는 지적이에요.\n"
        "판정: 수용 권장\n"
        "근거: \u201cimport spec\u201d이 명세와 다른 이름을 가리켜요.\n"
        "해야 할 일: import 경로를 명세에 맞춰요."
    )

    def __init__(self, text: str | None = None) -> None:
        text = self._LABELLED if text is None else text
        self.text = text
        self.calls: list[tuple[str, int]] = []

    async def advise(self, *, context: object) -> str:
        proposal_actions = getattr(context, "allowed_actions", ())
        self.calls.append((getattr(context, "repository", ""), len(proposal_actions)))
        return self.text


class _FailingAdvisor:
    def __init__(self) -> None:
        self.calls = 0

    async def advise(self, *, context: object) -> str:
        self.calls += 1
        raise RuntimeError("model unreachable")


@pytest.mark.asyncio
async def test_narrative_is_generated_once_and_rendered_in_the_proposal(
    tmp_path: Path,
) -> None:
    discord = _Discord()
    advisor = _StubAdvisor()
    coordinator = _coordinator(tmp_path / "inbox.db", discord, advisor=advisor)
    event = _event()

    await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    # A second identical delivery must neither re-post nor regenerate.
    await coordinator.ensure_thread(
        event,
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    messages = discord.messages["thread-1"]
    # One message now: the narrative lives inside the proposal instead of beside it.
    assert len(messages) == 1
    assert len(advisor.calls) == 1
    proposal_text = messages[0][1]
    assert "현재 요청" in proposal_text
    assert "명세와 import가 불일치한다는 지적이에요." in proposal_text
    assert "제 추천" in proposal_text
    assert "판정: 수용 권장" in proposal_text
    assert "근거:" in proposal_text
    # The deterministic boilerplate it replaced is gone.
    assert "먼저 읽기 전용으로" not in proposal_text
    assert "참고 분석" not in proposal_text

    stored = MentionInboxStore(tmp_path / "inbox.db")
    proposal = stored.get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    narrative = stored.get_proposal_narrative(proposal.proposal_id, proposal.revision)
    assert narrative is not None
    assert narrative.summary == "명세와 import가 불일치한다는 지적이에요."
    assert narrative.verdict.startswith("판정: 수용 권장")


@pytest.mark.asyncio
async def test_proposal_is_delivered_when_the_advisory_fails(tmp_path: Path) -> None:
    discord = _Discord()
    advisor = _FailingAdvisor()
    coordinator = _coordinator(tmp_path / "inbox.db", discord, advisor=advisor)

    session = await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert advisor.calls == 1
    assert session.discord_thread_id == "thread-1"
    # Exactly the deterministic proposal, and the binding was still recorded.
    assert len(discord.messages["thread-1"]) == 1
    assert "제 추천" in discord.messages["thread-1"][0][1]
    # The failure itself is persisted as an empty narrative, so a retry renders
    # the same deterministic body instead of generating fresh text and
    # double-sending the proposal.
    failed_store = MentionInboxStore(tmp_path / "inbox.db")
    latest = failed_store.get_latest_proposal("github:R_repo:PR_7")
    assert latest is not None
    narrative = failed_store.get_proposal_narrative(
        latest.proposal_id, latest.revision
    )
    assert narrative is not None
    assert narrative.summary == ""
    assert narrative.verdict == ""
    body = discord.messages["thread-1"][0][1]
    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )
    assert advisor.calls == 1
    assert len(discord.messages["thread-1"]) == 1
    assert discord.messages["thread-1"][0][1] == body
    stored = MentionInboxStore(tmp_path / "inbox.db").get_latest_proposal(
        "github:R_repo:PR_7"
    )
    assert stored is not None
    assert (
        MentionInboxStore(tmp_path / "inbox.db").get_proposal_message_id(
            stored.proposal_id, stored.revision
        )
        is not None
    )


@pytest.mark.asyncio
async def test_narrative_is_persisted_so_a_varying_model_cannot_repost(
    tmp_path: Path,
) -> None:
    """The narrative is now inside the deduped proposal body.

    A model that answers differently every time would otherwise render a
    different body on each delivery, miss the find_message_content recovery
    lookup, and post the proposal again. Persisting the first answer and
    rendering only from storage is what prevents that.
    """

    discord = _Discord()

    class _VaryingAdvisor:
        def __init__(self) -> None:
            self.count = 0

        async def advise(self, *, context: object) -> str:
            self.count += 1
            return (
                f"요청: 매번 다른 요약 {self.count}\n"
                f"판정: 부분 수용\n"
                f"근거: 매번 다른 근거 {self.count}\n"
                f"해야 할 일: 매번 다른 조치 {self.count}"
            )

    advisor = _VaryingAdvisor()
    coordinator = _coordinator(tmp_path / "inbox.db", discord, advisor=advisor)
    event = _event()
    for _ in range(3):
        await coordinator.ensure_thread(
            event,
            parent_message_id="parent-1",
            source_revision="2026-07-29T10:01:00Z",
        )

    store = MentionInboxStore(tmp_path / "inbox.db")
    proposal = store.get_latest_proposal("github:R_repo:PR_7")
    assert proposal is not None
    assert proposal.revision == 1
    assert len(discord.messages["thread-1"]) == 1
    # Generated exactly once, then read back from storage.
    assert advisor.count == 1
    assert "매번 다른 요약 1" in discord.messages["thread-1"][0][1]
    assert "매번 다른 요약 2" not in discord.messages["thread-1"][0][1]


@pytest.mark.asyncio
async def test_no_advisor_configured_changes_nothing(tmp_path: Path) -> None:
    discord = _Discord()
    coordinator = _coordinator(tmp_path / "inbox.db", discord, advisor=None)

    await coordinator.ensure_thread(
        _event(),
        parent_message_id="parent-1",
        source_revision="2026-07-29T10:01:00Z",
    )

    assert len(discord.messages["thread-1"]) == 1
    assert "참고 분석" not in discord.messages["thread-1"][0][1]


def test_thread_coordinator_requires_explicit_trusted_repositories() -> None:
    """No hardcoded tenant default: the trust boundary must come from config."""
    import inspect

    signature = inspect.signature(MentionInboxThreadCoordinator.__init__)
    default = signature.parameters["trusted_repositories"].default

    assert default is inspect.Parameter.empty, (
        "trusted_repositories must be a required keyword so a deployment "
        "cannot silently inherit a hardcoded repository allowlist"
    )


def test_subject_state_resolver_requires_explicit_allowed_repositories() -> None:
    """Approval-time repository trust must also be injected, never defaulted."""
    import inspect

    from plugins.mention_inbox.approval import GitHubSubjectStateResolver

    signature = inspect.signature(GitHubSubjectStateResolver.__init__)
    default = signature.parameters["allowed_repositories"].default

    assert default is inspect.Parameter.empty, (
        "allowed_repositories must be a required keyword so approval cannot "
        "silently inherit a hardcoded repository allowlist"
    )
