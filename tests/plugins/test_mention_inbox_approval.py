"""Deterministic mention-inbox approval and execution promotion."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plugins.mention_inbox.approval import (
    ApprovalHandler,
    ApprovedExecutionRequest,
    DispatchReceipt,
    ExecutionLifecycleObserver,
    GatewayExecutionDispatcher,
    GitHubSubjectStateResolver,
    ResolvedSourceState,
    render_approved_execution_prompt,
)
from plugins.mention_inbox.contract import (
    MentionSource,
    SourceRef,
    TargetKind,
    TargetRef,
    build_dedupe_key,
    ingest_event,
)
from plugins.mention_inbox.proposals import (
    ProposalStatus,
    build_work_proposal,
    proposal_to_json,
)
from plugins.mention_inbox.router import InboxDiscordMessage
from plugins.mention_inbox.store import MentionInboxStore

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
SUBJECT = "github:R_repo:PR_7"
DEDUPE = build_dedupe_key(
    SourceRef(MentionSource.GITHUB, "IC_99"),
    TargetRef("U_recent", TargetKind.USER),
)
SOURCE_REVISION = "2026-07-29T10:01:00Z"
APPROVER = "396159160201658368"
BOT_MENTION = "<@1525050677381279865>"


def _proposal(*, executor_hint: str = "direct"):
    return build_work_proposal(
        revision=1,
        source_dedupe_key=DEDUPE,
        source_revision=SOURCE_REVISION,
        subject_key=SUBJECT,
        head_sha="head-1",
        goal="요청된 PR 변경을 확인하고 범위 안에서 수정한다.",
        steps=("diff를 읽는다.", "범위 내 수정을 한다.", "테스트한다."),
        allowed_actions=("read_repository", "edit_scoped_files", "run_tests"),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=("대상 테스트 통과", "diff 검토"),
        executor_hint=executor_hint,
    )


def _seed(path: Path, *, executor_hint: str = "direct"):
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    store.record_work_item_thread(SUBJECT, "parent-1", "thread-1")
    proposal = _proposal(executor_hint=executor_hint)
    store.create_proposal(proposal)
    store.record_proposal_message(
        proposal.proposal_id,
        1,
        "proposal-message-1",
        approval_offered=True,
    )
    return store, proposal


class _Resolver:
    def __init__(self, state: ResolvedSourceState) -> None:
        self.state = state
        self.calls = 0

    async def resolve(self, proposal):
        self.calls += 1
        return self.state


class _Discord:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        self.sent.append((thread_id, content))
        return f"reply-{len(self.sent)}"


class _Dispatcher:
    def __init__(self, store: MentionInboxStore) -> None:
        self.store = store
        self.requests = []
        self.observed_pre_dispatch_state = None

    async def dispatch(self, request):
        self.requests.append(request)
        connection = sqlite3.connect(self.store.path)
        proposal_status = connection.execute(
            "SELECT status FROM work_proposals WHERE proposal_id = ? AND proposal_revision = ?",
            (request.proposal_id, request.proposal_revision),
        ).fetchone()[0]
        approval_count = connection.execute(
            "SELECT COUNT(*) FROM work_approvals WHERE approval_message_id = ?",
            (request.approval_message_id,),
        ).fetchone()[0]
        execution_status = connection.execute(
            "SELECT status FROM work_executions WHERE execution_id = ?",
            (request.execution_id,),
        ).fetchone()[0]
        connection.close()
        self.observed_pre_dispatch_state = (
            proposal_status,
            approval_count,
            execution_status,
        )
        return DispatchReceipt(
            accepted=True, dispatch_id=f"direct:{request.execution_id}"
        )


def _handler(store, resolver, dispatcher, discord):
    return ApprovalHandler(
        store=store,
        source_resolver=resolver,
        dispatcher=dispatcher,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({APPROVER}),
    )


def _approval_message(
    *, message_id: str = "approval-message-1", user_id: str = APPROVER
):
    return InboxDiscordMessage(
        thread_id="thread-1",
        message_id=message_id,
        user_id=user_id,
        text=f"{BOT_MENTION} 승인",
        reply_to_message_id="proposal-message-1",
    )


@pytest.mark.asyncio
async def test_authorized_exact_reply_commits_receipts_before_queued_dispatch(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "inbox.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1"))
    discord = _Discord()
    dispatcher = _Dispatcher(store)

    result = await _handler(store, resolver, dispatcher, discord).approve(
        _approval_message(), proposal
    )

    assert result.handled is True
    assert result.kind == "execution_queued"
    assert resolver.calls == 1
    assert dispatcher.observed_pre_dispatch_state == ("queued", 1, "queued")
    request = dispatcher.requests[0]
    assert request.proposal_hash == proposal.content_hash
    assert request.allowed_actions == proposal.allowed_actions
    assert request.forbidden_actions == proposal.forbidden_actions
    assert request.verification == proposal.verification
    assert request.approval_message_id == "approval-message-1"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.QUEUED
    execution = store.get_execution_for_proposal(proposal.proposal_id, 1)
    assert execution is not None and execution.status == "queued"
    assert execution.dispatch_id == f"direct:{execution.execution_id}"
    assert len(discord.sent) == 1
    assert discord.sent[0][0] == "thread-1"
    assert "아직 시작되지 않았고" in discord.sent[0][1]
    assert "✅" not in discord.sent[0][1]
    assert proposal.proposal_id not in discord.sent[0][1]


@pytest.mark.asyncio
async def test_unauthorized_user_never_resolves_or_dispatches(tmp_path: Path) -> None:
    store, proposal = _seed(tmp_path / "unauthorized.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1"))
    dispatcher = _Dispatcher(store)
    discord = _Discord()

    result = await _handler(store, resolver, dispatcher, discord).approve(
        _approval_message(message_id="approval-unauthorized", user_id="someone-else"),
        proposal,
    )

    assert result.kind == "unauthorized_approver"
    assert resolver.calls == 0
    assert dispatcher.requests == []
    assert discord.sent == []
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_changed_head_requires_new_approval_without_dispatch(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "changed-head.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-2"))
    dispatcher = _Dispatcher(store)
    discord = _Discord()

    result = await _handler(store, resolver, dispatcher, discord).approve(
        _approval_message(message_id="approval-stale-head"), proposal
    )

    assert result.kind == "head_changed"
    assert dispatcher.requests == []
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL
    assert len(discord.sent) == 1
    assert "이전 승인은 사용하지 않고" in discord.sent[0][1]


@pytest.mark.asyncio
async def test_duplicate_approval_reuses_queued_execution_without_dispatch(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "duplicate.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1"))
    dispatcher = _Dispatcher(store)
    discord = _Discord()
    handler = _handler(store, resolver, dispatcher, discord)
    message = _approval_message()

    first = await handler.approve(message, proposal)
    replay = await handler.approve(message, proposal)

    assert first.kind == "execution_queued"
    assert replay.kind == "execution_already_queued"
    assert len(dispatcher.requests) == 1
    assert resolver.calls == 1
    assert len(discord.sent) == 1


class _FailingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, request):
        self.calls += 1
        raise RuntimeError("private-token-value must not leak")


@pytest.mark.asyncio
async def test_dispatch_failure_is_persisted_blocked_without_error_leak(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "dispatch-failure.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1"))
    discord = _Discord()
    dispatcher = _FailingDispatcher()

    result = await _handler(store, resolver, dispatcher, discord).approve(
        _approval_message(message_id="approval-dispatch-failure"), proposal
    )

    assert result.kind == "execution_blocked"
    assert dispatcher.calls == 1
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.BLOCKED
    execution = store.get_execution_for_proposal(proposal.proposal_id, 1)
    assert execution is not None and execution.status == "blocked"
    assert execution.dispatch_id == f"direct:{execution.execution_id}"
    assert len(discord.sent) == 2
    assert "아직 시작되지 않았" in discord.sent[0][1]
    assert "시작하지 못" in discord.sent[1][1]
    assert "private-token-value" not in "\n".join(
        content for _, content in discord.sent
    )


def _store_source_event(store: MentionInboxStore, *, api_url: str) -> None:
    event = ingest_event({
        "schema_version": "1",
        "source": {"platform": "github", "event_id": "IC_99"},
        "actor": {"actor_id": "U_alice", "kind": "user"},
        "target": {"target_id": "U_recent", "kind": "user"},
        "thread": {"thread_id": SUBJECT, "container_id": "R_repo"},
        "requested_action": "review",
        "deadline": None,
        "untrusted": {
            "title": "Review requested",
            "body": "Please review",
            "action_detail": "direct_review_requested",
            "source_url": "https://github.com/silviahealth/content/pull/7",
            "metadata": {
                "repository": "silviahealth/content",
                "subject_type": "PullRequest",
                "subject_number": 7,
                "subject_key": SUBJECT,
                "subject_api_url": api_url,
                "source_revision": SOURCE_REVISION,
                "subject_head_sha": "head-1",
            },
        },
    })
    assert event.dedupe_key == DEDUPE
    store.upsert(event, source_revision=SOURCE_REVISION)


class _GitHubClient:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch_subject(self, url: str):
        self.urls.append(url)
        return {
            "number": 7,
            "updated_at": "2026-07-29T10:03:00Z",
            "head": {"sha": "head-3"},
        }


@pytest.mark.asyncio
async def test_github_resolver_reloads_subject_revision_and_head_from_stored_url(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "resolver.db")
    api_url = "https://api.github.com/repos/silviahealth/content/pulls/7"
    _store_source_event(store, api_url=api_url)
    client = _GitHubClient()

    state = await GitHubSubjectStateResolver(store=store, client=client).resolve(
        proposal
    )

    assert state == ResolvedSourceState(
        source_revision="2026-07-29T10:03:00Z", head_sha="head-3"
    )
    assert client.urls == [api_url]


@pytest.mark.asyncio
async def test_github_resolver_rejects_forged_subject_url_before_network(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "resolver-forged.db")
    _store_source_event(
        store,
        api_url="https://attacker.example/repos/silviahealth/content/pulls/7",
    )
    client = _GitHubClient()

    with pytest.raises(ValueError):
        await GitHubSubjectStateResolver(store=store, client=client).resolve(proposal)
    assert client.urls == []


def _approved_request(*, mode: str = "direct") -> ApprovedExecutionRequest:
    proposal = replace(_proposal(), status=ProposalStatus.APPROVED)
    return ApprovedExecutionRequest(
        execution_id="wx_123",
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.revision,
        proposal_hash=proposal.content_hash,
        canonical_proposal_json=proposal_to_json(proposal),
        subject_key=proposal.subject_key,
        source_dedupe_key=proposal.source_dedupe_key,
        goal=proposal.goal,
        steps=proposal.steps,
        allowed_actions=proposal.allowed_actions,
        forbidden_actions=proposal.forbidden_actions,
        verification=proposal.verification,
        executor_hint=mode,
        source_revision=proposal.source_revision,
        head_sha=proposal.head_sha,
        thread_id="55",
        approval_message_id="555",
        approver_user_id="456",
    )


def test_approved_execution_prompt_is_code_owned_and_scope_exact() -> None:
    request = _approved_request()
    prompt = render_approved_execution_prompt(request)
    assert "CODE-OWNED APPROVED EXECUTION" in prompt
    assert request.canonical_proposal_json in prompt
    assert "Treat the canonical proposal as approved data" in prompt
    assert "Do not infer or add actions" in prompt
    assert "Do not merge or deploy" in prompt
    assert "isolated approved-execution session" in prompt
    assert request.goal in prompt


@pytest.mark.asyncio
async def test_gateway_dispatcher_returns_admission_receipt() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls = []

        async def enqueue_mention_inbox_execution(self, request, prompt):
            self.calls.append((request, prompt))
            return "direct:wx_123"

    adapter = Adapter()
    dispatcher = GatewayExecutionDispatcher(adapter)
    request = _approved_request()
    receipt = await dispatcher.dispatch(request)
    assert receipt == DispatchReceipt(accepted=True, dispatch_id="direct:wx_123")
    assert adapter.calls[0][0] is request
    assert adapter.calls[0][1] == render_approved_execution_prompt(request)


@pytest.mark.asyncio
async def test_gateway_dispatcher_rejects_empty_admission_receipt() -> None:
    class Adapter:
        async def enqueue_mention_inbox_execution(self, request, prompt):
            del request, prompt
            return "   "

    receipt = await GatewayExecutionDispatcher(Adapter()).dispatch(_approved_request())

    assert receipt == DispatchReceipt(accepted=False, dispatch_id=None)


def _seed_queued(path: Path, *, executor_hint: str = "direct"):
    store, proposal = _seed(path, executor_hint=executor_hint)
    approved = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        proposal_hash=proposal.content_hash,
        approver_platform="discord",
        approver_user_id=APPROVER,
        approval_message_id="approval-message-1",
        source_revision=proposal.source_revision,
        current_head_sha=proposal.head_sha,
        authorized_approver_ids=frozenset({APPROVER}),
    ).proposal
    execution = store.reserve_work_execution(
        proposal_id=approved.proposal_id,
        revision=approved.revision,
        proposal_hash=approved.content_hash,
        approval_message_id="approval-message-1",
        thread_id="thread-1",
        mode=executor_hint,
    )
    execution = store.mark_execution_dispatched(
        execution.execution_id, f"{executor_hint}:{execution.execution_id}"
    )
    store.transition_proposal_status(
        approved.proposal_id,
        approved.revision,
        ProposalStatus.QUEUED,
        expected_statuses=(ProposalStatus.APPROVED,),
    )
    return store, proposal, execution


@pytest.mark.asyncio
async def test_lifecycle_completes_only_with_successful_verification_receipt(
    tmp_path: Path,
) -> None:
    store, proposal, execution = _seed_queued(tmp_path / "lifecycle.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(store=store, discord=discord)
    observer.tool_started(execution.execution_id, "terminal")
    observer.tool_completed(
        execution.execution_id,
        "terminal",
        {"exit_code": 0, "output": "private-token-value"},
    )
    await observer.run_completed(
        execution.execution_id, {"completed": True, "final_response": "done"}
    )

    latest = store.get_latest_proposal(SUBJECT)
    receipt = store.get_execution(execution.execution_id)
    assert latest is not None and latest.status is ProposalStatus.COMPLETED
    assert receipt is not None and receipt.status == "completed"
    assert "시작" in discord.sent[0][1]
    assert "검증" in discord.sent[1][1]
    assert discord.sent[2][1].startswith("완료")
    rendered = "\n".join(content for _, content in discord.sent)
    assert execution.execution_id not in rendered
    assert proposal.proposal_id not in rendered
    assert "private-token-value" not in rendered


@pytest.mark.asyncio
async def test_lifecycle_blocks_agent_turn_without_tool_activity(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "no-tool.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(store=store, discord=discord)
    await observer.run_completed(
        execution.execution_id,
        {"completed": True, "final_response": "claimed completion"},
    )
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.BLOCKED
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert len(discord.sent) == 1
    assert "시작하지 못" in discord.sent[0][1]


@pytest.mark.asyncio
async def test_lifecycle_blocks_failed_verification_receipt(tmp_path: Path) -> None:
    store, _, execution = _seed_queued(tmp_path / "failed-verify.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(store=store, discord=discord)
    observer.tool_started(execution.execution_id, "terminal")
    observer.tool_completed(
        execution.execution_id,
        "terminal",
        {"exit_code": 1, "output": "secret failing output"},
    )
    await observer.run_completed(execution.execution_id, {"completed": True})
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.BLOCKED
    assert store.get_execution(execution.execution_id).status == "blocked"
    rendered = "\n".join(content for _, content in discord.sent)
    assert "secret failing output" not in rendered


@pytest.mark.asyncio
async def test_kanban_lifecycle_records_durable_queue_without_claiming_completion(
    tmp_path: Path,
) -> None:
    store, proposal, execution = _seed_queued(
        tmp_path / "kanban.db", executor_hint="kanban"
    )
    discord = _Discord()
    observer = ExecutionLifecycleObserver(store=store, discord=discord)

    observer.tool_started(execution.execution_id, "kanban_task")
    observer.tool_completed(
        execution.execution_id,
        "kanban_task",
        {"ok": True, "task_id": "private-card-id", "status": "ready"},
    )
    outcome = await observer.run_completed(execution.execution_id, {"completed": True})

    assert outcome == "queued"
    assert store.get_execution(execution.execution_id).status == "completed"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.QUEUED
    rendered = "\n".join(content for _, content in discord.sent)
    assert "Kanban" in rendered
    assert "구현 완료가 아니라" in rendered
    assert "private-card-id" not in rendered
    assert proposal.proposal_id not in rendered


@pytest.mark.asyncio
async def test_execution_context_must_match_reserved_proposal_and_mode(
    tmp_path: Path,
) -> None:
    store, proposal, execution = _seed_queued(tmp_path / "context-policy.db")
    observer = ExecutionLifecycleObserver(store=store, discord=_Discord())

    observer.validate_execution_context(
        execution.execution_id,
        proposal_hash=proposal.content_hash,
        mode="direct",
    )
    with pytest.raises(ValueError, match="proposal hash"):
        observer.validate_execution_context(
            execution.execution_id,
            proposal_hash="0" * 64,
            mode="direct",
        )
    with pytest.raises(ValueError, match="mode"):
        observer.validate_execution_context(
            execution.execution_id,
            proposal_hash=proposal.content_hash,
            mode="kanban",
        )


@pytest.mark.asyncio
async def test_direct_pretool_policy_blocks_forbidden_commands_and_paths(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "direct-policy.db")
    observer = ExecutionLifecycleObserver(store=store, discord=_Discord())

    with pytest.raises(ValueError, match="forbidden terminal action"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "git push origin main"},
        )
    with pytest.raises(ValueError, match="scoped relative path"):
        observer.authorize_tool_start(
            execution.execution_id,
            "patch",
            {"mode": "replace", "path": "/tmp/outside.py"},
        )
    with pytest.raises(ValueError, match="replace mode"):
        observer.authorize_tool_start(
            execution.execution_id,
            "patch",
            {
                "mode": "patch",
                "patch": "*** Begin Patch\n*** Update File: ../outside.py\n*** End Patch",
            },
        )
    with pytest.raises(ValueError, match="cross-profile"):
        observer.authorize_tool_start(
            execution.execution_id,
            "write_file",
            {"path": "safe.py", "cross_profile": True},
        )
    with pytest.raises(ValueError, match="background"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "python -m pytest tests/unit -q", "background": True},
        )
    with pytest.raises(ValueError, match="outside approved scope"):
        observer.authorize_tool_start(
            execution.execution_id,
            "process",
            {"action": "kill", "session_id": "unrelated"},
        )
    with pytest.raises(ValueError, match="approved verification command"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "python -c 'print(1)'"},
        )
    with pytest.raises(ValueError, match="shell composition"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "python -m pytest tests/unit -q; echo $GITHUB_PAT_TOKEN"},
        )

    assert store.get_execution(execution.execution_id).status == "queued"
    observer.authorize_tool_start(
        execution.execution_id,
        "terminal",
        {"command": "python -m pytest tests/unit -q"},
    )
    assert store.get_execution(execution.execution_id).status == "running"


class _RecoveryDispatcher:
    def __init__(self) -> None:
        self.requests = []

    async def dispatch(self, request):
        self.requests.append(request)
        return DispatchReceipt(
            accepted=True,
            dispatch_id=f"{request.executor_hint}:{request.execution_id}",
        )


@pytest.mark.asyncio
async def test_recovery_revalidates_and_readmits_queued_execution(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "recover.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1"))
    dispatcher = _RecoveryDispatcher()
    discord = _Discord()
    handler = _handler(store, resolver, dispatcher, discord)

    recovered = await handler.recover_queued()

    assert recovered == 1
    assert resolver.calls == 1
    assert len(dispatcher.requests) == 1
    assert dispatcher.requests[0].execution_id == execution.execution_id
    assert store.get_execution(execution.execution_id).status == "queued"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.QUEUED
    assert discord.sent == []


@pytest.mark.asyncio
async def test_recovery_marks_stale_head_for_reapproval_without_dispatch(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "recover-stale.db")
    resolver = _Resolver(ResolvedSourceState(SOURCE_REVISION, "new-head"))
    dispatcher = _RecoveryDispatcher()
    discord = _Discord()
    handler = _handler(store, resolver, dispatcher, discord)

    recovered = await handler.recover_queued()

    assert recovered == 0
    assert dispatcher.requests == []
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL
    assert len(discord.sent) == 1
    assert "이전 승인은 사용하지 않고" in discord.sent[0][1]


@pytest.mark.asyncio
async def test_recovery_promotes_reserved_receipt_before_readmission(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "recover-reserved.db")
    approved = store.approve_proposal_cas(
        proposal_id=proposal.proposal_id,
        revision=proposal.revision,
        proposal_hash=proposal.content_hash,
        source_revision=proposal.source_revision,
        current_head_sha=proposal.head_sha,
        approver_platform="discord",
        approver_user_id=APPROVER,
        authorized_approver_ids=frozenset({APPROVER}),
        approval_message_id="approval-message-1",
    ).proposal
    execution = store.reserve_work_execution(
        proposal_id=approved.proposal_id,
        revision=approved.revision,
        proposal_hash=approved.content_hash,
        approval_message_id="approval-message-1",
        thread_id="thread-1",
        mode="direct",
    )
    dispatcher = _RecoveryDispatcher()
    discord = _Discord()
    handler = _handler(
        store,
        _Resolver(ResolvedSourceState(SOURCE_REVISION, "head-1")),
        dispatcher,
        discord,
    )

    recovered = await handler.recover_queued()

    assert recovered == 1
    receipt = store.get_execution(execution.execution_id)
    assert receipt.status == "queued"
    assert receipt.dispatch_id == f"direct:{execution.execution_id}"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.QUEUED
    assert len(discord.sent) == 1
    assert "아직 시작되지 않았" in discord.sent[0][1]
