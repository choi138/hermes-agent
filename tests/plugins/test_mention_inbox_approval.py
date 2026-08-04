"""Deterministic mention-inbox approval and execution promotion."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from plugins.mention_inbox.store import MentionInboxStore, work_execution_id
from plugins.mention_inbox.workspace import WorktreeRequest

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=timezone.utc)
SUBJECT = "github:R_repo:PR_7"
DEDUPE = build_dedupe_key(
    SourceRef(MentionSource.GITHUB, "IC_99"),
    TargetRef("U_recent", TargetKind.USER),
)
SOURCE_REVISION = "2026-07-29T10:01:00Z"
APPROVER = "396159160201658368"
BOT_MENTION = "<@1525050677381279865>"
HEAD_REF = "feature/review-fix"
HEAD_REPOSITORY = "silviahealth/content"
WORKSPACE = "/Users/test/Documents/hermes-workspaces/silviahealth-content"
COMMIT_SHA = "abc1234" + ("0" * 33)


def _proposal(
    *,
    executor_hint: str = "direct",
    publish: bool = False,
    head_sha: str = "head-1",
):
    allowed_actions = [
        "read_repository",
        "edit_scoped_files",
        "run_tests",
    ]
    verification = ["대상 테스트 통과", "diff 검토"]
    if publish:
        allowed_actions.extend(
            ("switch_to_pr_branch", "commit_changes", "push_current_branch")
        )
        verification.extend(("commit SHA 확인", "non-force push 성공"))
    return build_work_proposal(
        revision=1,
        source_dedupe_key=DEDUPE,
        source_revision=SOURCE_REVISION,
        subject_key=SUBJECT,
        head_sha=head_sha,
        goal="요청된 PR 변경을 확인하고 범위 안에서 수정한다.",
        steps=("diff를 읽는다.", "범위 내 수정을 한다.", "테스트한다."),
        allowed_actions=tuple(allowed_actions),
        forbidden_actions=("merge", "deploy", "delete", "read_secrets"),
        verification=tuple(verification),
        executor_hint=executor_hint,
    )


def _state(
    head_sha: str = "head-1",
    *,
    source_revision: str = SOURCE_REVISION,
) -> ResolvedSourceState:
    return ResolvedSourceState(
        source_revision=source_revision,
        head_sha=head_sha,
        head_ref=HEAD_REF,
        head_repository=HEAD_REPOSITORY,
        repository_node_id="R_repo",
        base_repository="owner/repo",
    )


def _seed(
    path: Path,
    *,
    executor_hint: str = "direct",
    publish: bool = False,
    head_sha: str = "head-1",
):
    store = MentionInboxStore(path, clock=lambda: NOW)
    store.reserve_work_item_session(SUBJECT, DEDUPE, SOURCE_REVISION)
    store.record_work_item_thread(
        SUBJECT, "parent-1", "1531851208858275860", "thread-1"
    )
    proposal = _proposal(
        executor_hint=executor_hint,
        publish=publish,
        head_sha=head_sha,
    )
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
        self.edits: list[tuple[str, str, str]] = []

    async def send_to_thread(self, thread_id: str, content: str) -> str:
        self.sent.append((thread_id, content))
        return f"reply-{len(self.sent)}"

    async def edit_thread_message(
        self,
        thread_id: str,
        message_id: str,
        content: str,
    ) -> None:
        self.edits.append((thread_id, message_id, content))

    async def find_marker(
        self, thread_id: str, marker: str, *, limit: int
    ) -> str | None:
        del thread_id, marker, limit
        return None


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


class _WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[WorktreeRequest] = []

    def workspace_for(self, execution_id: str) -> Path:
        return self.root / "executions" / execution_id

    def prepare(self, request: WorktreeRequest) -> Path:
        self.requests.append(request)
        workspace = self.workspace_for(request.execution_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace


def _handler(
    store,
    resolver,
    dispatcher,
    discord,
    *,
    workspace_manager=None,
    recovery_lease_seconds: int = 60,
):
    return ApprovalHandler(
        store=store,
        source_resolver=resolver,
        dispatcher=dispatcher,
        discord=discord,
        bot_mention=BOT_MENTION,
        authorized_approver_ids=frozenset({APPROVER}),
        workspace=WORKSPACE,
        workspace_manager=workspace_manager,
        recovery_lease_seconds=recovery_lease_seconds,
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
    resolver = _Resolver(_state())
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
    assert request.head_ref == HEAD_REF
    assert request.head_repository == HEAD_REPOSITORY
    assert request.workspace == WORKSPACE
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
async def test_approval_prepares_execution_owned_worktree_before_dispatch(
    tmp_path: Path,
) -> None:
    head_sha = "a" * 40
    store, proposal = _seed(tmp_path / "inbox.db", head_sha=head_sha)
    dispatcher = _Dispatcher(store)
    manager = _WorktreeManager(tmp_path / "workspaces")

    result = await _handler(
        store,
        _Resolver(_state(head_sha)),
        dispatcher,
        _Discord(),
        workspace_manager=manager,
    ).approve(_approval_message(), proposal)

    assert result.kind == "execution_queued"
    assert len(manager.requests) == 1
    request = dispatcher.requests[0]
    assert manager.requests[0].execution_id == request.execution_id
    assert request.workspace == str(
        (tmp_path / "workspaces" / "executions" / request.execution_id).resolve()
    )
    execution = store.get_execution(request.execution_id)
    assert execution is not None
    assert execution.workspace == request.workspace


@pytest.mark.asyncio
async def test_unauthorized_user_never_resolves_or_dispatches(tmp_path: Path) -> None:
    store, proposal = _seed(tmp_path / "unauthorized.db")
    resolver = _Resolver(_state())
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
    resolver = _Resolver(_state("head-2"))
    dispatcher = _Dispatcher(store)
    discord = _Discord()

    result = await _handler(store, resolver, dispatcher, discord).approve(
        _approval_message(message_id="approval-stale-head"), proposal
    )

    assert result.kind == "head_changed"
    assert dispatcher.requests == []
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL
    assert len(discord.sent) == 1
    assert "이전 실행 요청은 사용하지 않고" in discord.sent[0][1]


@pytest.mark.asyncio
async def test_duplicate_approval_reuses_queued_execution_without_dispatch(
    tmp_path: Path,
) -> None:
    store, proposal = _seed(tmp_path / "duplicate.db")
    resolver = _Resolver(_state())
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
    resolver = _Resolver(_state())
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
    assert "시작하지 않았" in discord.sent[1][1]
    assert "private-token-value" not in "\n".join(
        content for _, content in discord.sent
    )


def _store_source_event(
    store: MentionInboxStore,
    *,
    api_url: str,
    repository: str = "silviahealth/content",
) -> None:
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
                "repository": repository,
                "repository_private": False,
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
    def __init__(self, payload=None) -> None:
        self.urls: list[str] = []
        self.payload = payload

    def fetch_subject(self, url: str):
        self.urls.append(url)
        if self.payload is not None:
            return self.payload
        return {
            "number": 7,
            "updated_at": "2026-07-29T10:03:00Z",
            "user": {"node_id": "U_recent"},
            "base": {
                "repo": {
                    "node_id": "R_repo",
                    "full_name": "silviahealth/content",
                    "private": False,
                }
            },
            "head": {
                "sha": "head-3",
                "ref": HEAD_REF,
                "repo": {
                    "full_name": HEAD_REPOSITORY,
                    "private": False,
                },
            },
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
        source_revision="2026-07-29T10:03:00Z",
        head_sha="head-3",
        head_ref=HEAD_REF,
        head_repository=HEAD_REPOSITORY,
        repository_node_id="R_repo",
        base_repository="silviahealth/content",
    )
    assert client.urls == [api_url]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        ("base_private", True),
        ("head_private", True),
        ("base_node_id", "R_changed"),
        ("base_name", "changed/project"),
        ("author_node_id", "U_someone_else"),
    ),
)
async def test_external_resolver_rejects_fresh_private_or_changed_scope(
    tmp_path: Path, mutation: tuple[str, object]
) -> None:
    store, proposal = _seed(tmp_path / f"external-{mutation[0]}.db")
    repository = "external/project"
    api_url = f"https://api.github.com/repos/{repository}/pulls/7"
    _store_source_event(store, api_url=api_url, repository=repository)
    payload = {
        "number": 7,
        "updated_at": SOURCE_REVISION,
        "user": {"node_id": "U_recent"},
        "base": {
            "repo": {
                "node_id": "R_repo",
                "full_name": repository,
                "private": False,
            }
        },
        "head": {
            "sha": "head-1",
            "ref": HEAD_REF,
            "repo": {"full_name": "contributor/project", "private": False},
        },
    }
    key, value = mutation
    if key == "base_private":
        payload["base"]["repo"]["private"] = value
    elif key == "head_private":
        payload["head"]["repo"]["private"] = value
    elif key == "base_node_id":
        payload["base"]["repo"]["node_id"] = value
    elif key == "base_name":
        payload["base"]["repo"]["full_name"] = value
    else:
        payload["user"]["node_id"] = value
    resolver = GitHubSubjectStateResolver(
        store=store,
        client=_GitHubClient(payload),
        include_public_actionable_activity=True,
        external_repository_actions="own_pr_write",
    )

    with pytest.raises(ValueError):
        await resolver.resolve(proposal)


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
        recovery_token="recovery-token-1",
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
        head_ref=HEAD_REF,
        head_repository=HEAD_REPOSITORY,
        workspace=WORKSPACE,
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
    assert WORKSPACE in prompt
    assert HEAD_REF in prompt
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


def _seed_queued(
    path: Path,
    *,
    executor_hint: str = "direct",
    publish: bool = False,
    workspace: str = WORKSPACE,
    workspace_manager: _WorktreeManager | None = None,
    head_sha: str = "head-1",
    active_owner: bool = True,
    owner_id: str = "test-owner",
):
    store, proposal = _seed(
        path,
        executor_hint=executor_hint,
        publish=publish,
        head_sha=head_sha,
    )
    if workspace_manager is not None:
        workspace = str(
            workspace_manager.workspace_for(
                work_execution_id(proposal.proposal_id, proposal.revision)
            ).resolve()
        )
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
        head_ref=HEAD_REF,
        head_repository=HEAD_REPOSITORY,
        workspace=workspace,
    )
    execution = store.mark_execution_dispatched(
        execution.execution_id, f"{executor_hint}:{execution.execution_id}"
    )
    assert execution.recovery_token is not None
    if active_owner:
        assert store.admit_execution_owner(
            execution.execution_id,
            recovery_token=execution.recovery_token,
            owner_id=owner_id,
        )
    else:
        assert store.release_execution_recovery(
            execution.execution_id,
            recovery_token=execution.recovery_token,
        )
    execution = store.get_execution(execution.execution_id)
    assert execution is not None
    if active_owner:
        assert execution.owner_id == owner_id
    else:
        assert execution.recovery_token is None
        assert execution.owner_id is None
    store.transition_proposal_status(
        approved.proposal_id,
        approved.revision,
        ProposalStatus.QUEUED,
        expected_statuses=(ProposalStatus.APPROVED,),
    )
    return store, proposal, execution


def _stage_terminal_receipt(
    store: MentionInboxStore,
    execution,
    *,
    content: str = "✅ terminal completion",
) -> None:
    assert execution.recovery_token is not None
    assert execution.owner_id is not None
    store.mark_execution_running(
        execution.execution_id,
        tool_name="terminal",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    store.record_execution_tool_completion(
        execution.execution_id,
        tool_name="terminal",
        success=True,
        exit_code=0,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
        action="verification",
    )
    store.mark_execution_verifying(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    store.complete_execution_with_terminal_receipt(
        execution.execution_id,
        content=content,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )


@pytest.mark.asyncio
async def test_lifecycle_completes_only_with_successful_verification_receipt(
    tmp_path: Path,
) -> None:
    store, proposal, execution = _seed_queued(tmp_path / "lifecycle.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )
    observer.tool_started(execution.execution_id, "terminal")
    observer.tool_completed(
        execution.execution_id,
        "terminal",
        {"exit_code": 0, "output": "private-token-value"},
        args={
            "command": "python -m pytest tests/unit -q",
            "workdir": WORKSPACE,
        },
    )
    await observer.run_completed(
        execution.execution_id, {"completed": True, "final_response": "done"}
    )

    latest = store.get_latest_proposal(SUBJECT)
    receipt = store.get_execution(execution.execution_id)
    assert latest is not None and latest.status is ProposalStatus.COMPLETED
    assert receipt is not None and receipt.status == "completed"
    assert "시작" in discord.sent[0][1]
    assert "검증" in discord.edits[0][2]
    assert "마무리" in discord.edits[-1][2]
    assert discord.sent[1][1].startswith("✅")
    assert receipt.status_message_id == "reply-1"
    assert receipt.terminal_receipt_message_id == "reply-2"
    rendered = "\n".join(
        [content for _, content in discord.sent]
        + [content for _, _, content in discord.edits]
    )
    assert execution.execution_id not in rendered
    assert proposal.proposal_id not in rendered
    assert "private-token-value" not in rendered


@pytest.mark.asyncio
async def test_completed_execution_recovers_receipt_after_crash_before_send(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "receipt-before-send.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )
    assert execution.recovery_token is not None
    assert execution.owner_id is not None
    store.mark_execution_running(
        execution.execution_id,
        tool_name="terminal",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    store.record_execution_tool_completion(
        execution.execution_id,
        tool_name="terminal",
        success=True,
        exit_code=0,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
        action="verification",
    )
    store.mark_execution_verifying(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    completed = store.complete_execution_with_terminal_receipt(
        execution.execution_id,
        content="✅ recovered completion",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )

    assert completed.status == "completed"
    assert completed.terminal_receipt_message_id is None
    assert await observer.reconcile_terminal_receipts() == 1
    assert len(discord.sent) == 1
    assert "[hermes-execution-receipt:" in discord.sent[0][1]
    assert store.get_execution(execution.execution_id).terminal_receipt_message_id == (
        "reply-1"
    )


@pytest.mark.asyncio
async def test_completed_execution_reconciles_uncertain_send_without_duplicate(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store, _, execution = _seed_queued(tmp_path / "receipt-after-send.db")
    store._clock = lambda: now[0]
    assert execution.recovery_token is not None
    assert execution.owner_id is not None
    store.mark_execution_running(
        execution.execution_id,
        tool_name="terminal",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    store.record_execution_tool_completion(
        execution.execution_id,
        tool_name="terminal",
        success=True,
        exit_code=0,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
        action="verification",
    )
    store.mark_execution_verifying(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    store.complete_execution_with_terminal_receipt(
        execution.execution_id,
        content="✅ uncertain completion",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    claim = store.claim_terminal_receipt(lease_seconds=10)
    assert claim is not None
    now[0] += timedelta(seconds=11)

    class DiscordWithHistory(_Discord):
        async def find_marker(
            self, thread_id: str, marker: str, *, limit: int
        ) -> str | None:
            del thread_id, limit
            assert marker in claim.content
            return "already-sent"

    discord = DiscordWithHistory()
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )

    assert await observer.reconcile_terminal_receipts() == 1
    assert discord.sent == []
    assert store.get_execution(execution.execution_id).terminal_receipt_message_id == (
        "already-sent"
    )


def test_stale_terminal_receipt_claim_cannot_mark_after_replacement(
    tmp_path: Path,
) -> None:
    now = [NOW]
    store, _, execution = _seed_queued(tmp_path / "receipt-token-fence.db")
    store._clock = lambda: now[0]
    _stage_terminal_receipt(store, execution)

    stale = store.claim_terminal_receipt(lease_seconds=10)
    assert stale is not None
    assert stale.claim_token not in {execution.recovery_token, execution.owner_id}
    connection = sqlite3.connect(store.path)
    persisted_token = connection.execute(
        "SELECT terminal_receipt_claim_token FROM work_executions "
        "WHERE execution_id = ?",
        (execution.execution_id,),
    ).fetchone()[0]
    connection.close()
    assert persisted_token == stale.claim_token
    now[0] += timedelta(seconds=11)
    replacement = store.claim_terminal_receipt(lease_seconds=10)
    assert replacement is not None
    assert replacement.requires_reconciliation is True
    assert replacement.claim_token != stale.claim_token

    with pytest.raises(ValueError, match="stale or foreign"):
        store.mark_terminal_receipt_sent(
            stale.execution_id,
            claim_token=stale.claim_token,
            message_id="stale-message",
        )
    marked = store.mark_terminal_receipt_sent(
        replacement.execution_id,
        claim_token=replacement.claim_token,
        message_id="replacement-message",
    )
    assert marked.terminal_receipt_message_id == "replacement-message"
    with pytest.raises(ValueError, match="stale or foreign"):
        store.mark_terminal_receipt_sent(
            stale.execution_id,
            claim_token=stale.claim_token,
            message_id="replacement-message",
        )


class _TerminalReceiptHeartbeatStore(MentionInboxStore):
    def __init__(self, path: Path, *, clock) -> None:
        super().__init__(path, clock=clock)
        self.receipt_renewed = asyncio.Event()

    def renew_terminal_receipt_lease(
        self,
        execution_id: str,
        *,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        renewed = super().renew_terminal_receipt_lease(
            execution_id,
            claim_token=claim_token,
            lease_seconds=lease_seconds,
        )
        self.receipt_renewed.set()
        return renewed


@pytest.mark.asyncio
async def test_terminal_receipt_heartbeat_keeps_blocked_send_exclusive(
    tmp_path: Path,
) -> None:
    now = [NOW]
    db = tmp_path / "receipt-heartbeat.db"
    seeded, _, execution = _seed_queued(db)
    seeded._clock = lambda: now[0]
    _stage_terminal_receipt(seeded, execution)
    store = _TerminalReceiptHeartbeatStore(db, clock=lambda: now[0])

    send_started = asyncio.Event()
    send_release = asyncio.Event()

    class BlockingDiscord(_Discord):
        async def send_to_thread(self, thread_id: str, content: str) -> str:
            self.sent.append((thread_id, content))
            send_started.set()
            await send_release.wait()
            return "terminal-message"

    discord = BlockingDiscord()
    first_observer = ExecutionLifecycleObserver(
        store=store,
        discord=discord,
        workspace=WORKSPACE,
        terminal_receipt_lease_seconds=1,
    )
    first = asyncio.create_task(first_observer.reconcile_terminal_receipts())
    await asyncio.wait_for(send_started.wait(), timeout=2)

    second_observer = ExecutionLifecycleObserver(
        store=MentionInboxStore(db, clock=lambda: now[0]),
        discord=discord,
        workspace=WORKSPACE,
        terminal_receipt_lease_seconds=1,
    )
    assert await second_observer.reconcile_terminal_receipts() == 0

    store.receipt_renewed.clear()
    now[0] += timedelta(milliseconds=500)
    await asyncio.wait_for(store.receipt_renewed.wait(), timeout=2)
    now[0] += timedelta(milliseconds=600)
    assert await second_observer.reconcile_terminal_receipts() == 0
    assert len(discord.sent) == 1

    send_release.set()
    assert await asyncio.wait_for(first, timeout=2) == 1
    assert store.get_execution(execution.execution_id).terminal_receipt_message_id == (
        "terminal-message"
    )


@pytest.mark.asyncio
async def test_terminal_receipt_reconciliation_propagates_cancellation(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "receipt-cancel.db")
    _stage_terminal_receipt(store, execution)
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockingDiscord(_Discord):
        async def send_to_thread(self, thread_id: str, content: str) -> str:
            del thread_id, content
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

    observer = ExecutionLifecycleObserver(
        store=store,
        discord=BlockingDiscord(),
        workspace=WORKSPACE,
        terminal_receipt_lease_seconds=10,
    )
    reconcile = asyncio.create_task(observer.reconcile_terminal_receipts())
    await asyncio.wait_for(send_started.wait(), timeout=2)
    reconcile.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reconcile, timeout=2)
    await asyncio.wait_for(send_cancelled.wait(), timeout=2)
    assert (
        store.get_execution(execution.execution_id).terminal_receipt_message_id is None
    )


@pytest.mark.asyncio
async def test_lifecycle_blocks_agent_turn_without_tool_activity(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "no-tool.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )
    await observer.run_completed(
        execution.execution_id,
        {"completed": True, "final_response": "claimed completion"},
    )
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.BLOCKED
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert len(discord.sent) == 1
    assert "시작하지 않았" in discord.sent[0][1]
    assert "no_tool_activity" in discord.sent[0][1]


@pytest.mark.asyncio
async def test_lifecycle_blocks_failed_verification_receipt(tmp_path: Path) -> None:
    store, _, execution = _seed_queued(tmp_path / "failed-verify.db")
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )
    observer.tool_started(execution.execution_id, "terminal")
    observer.tool_completed(
        execution.execution_id,
        "terminal",
        {"exit_code": 1, "output": "secret failing output"},
        args={
            "command": "python -m pytest tests/unit -q",
            "workdir": WORKSPACE,
        },
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
    observer = ExecutionLifecycleObserver(
        store=store, discord=discord, workspace=WORKSPACE
    )

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
    rendered = "\n".join(
        [content for _, content in discord.sent]
        + [content for _, _, content in discord.edits]
    )
    assert "Kanban" in rendered
    assert "구현 완료가 아니라" in rendered
    assert "private-card-id" not in rendered
    assert proposal.proposal_id not in rendered


@pytest.mark.asyncio
async def test_execution_context_must_match_reserved_proposal_and_mode(
    tmp_path: Path,
) -> None:
    store, proposal, execution = _seed_queued(tmp_path / "context-policy.db")
    observer = ExecutionLifecycleObserver(
        store=store, discord=_Discord(), workspace=WORKSPACE
    )

    assert execution.recovery_token is not None
    assert execution.owner_id is not None
    observer.validate_execution_context(
        execution.execution_id,
        proposal_hash=proposal.content_hash,
        mode="direct",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )
    with pytest.raises(ValueError, match="proposal hash"):
        observer.validate_execution_context(
            execution.execution_id,
            proposal_hash="0" * 64,
            mode="direct",
            recovery_token=execution.recovery_token,
            owner_id=execution.owner_id,
        )
    with pytest.raises(ValueError, match="mode"):
        observer.validate_execution_context(
            execution.execution_id,
            proposal_hash=proposal.content_hash,
            mode="kanban",
            recovery_token=execution.recovery_token,
            owner_id=execution.owner_id,
        )


@pytest.mark.asyncio
async def test_lifecycle_accepts_only_execution_worktree_under_workspace_root(
    tmp_path: Path,
) -> None:
    worktree = f"{WORKSPACE}/executions/wx_1234567890abcdef12345678"
    store, proposal, execution = _seed_queued(
        tmp_path / "worktree-context.db",
        workspace=worktree,
    )
    observer = ExecutionLifecycleObserver(
        store=store,
        discord=_Discord(),
        workspace=WORKSPACE,
    )

    assert execution.recovery_token is not None
    assert execution.owner_id is not None
    observer.validate_execution_context(
        execution.execution_id,
        proposal_hash=proposal.content_hash,
        mode="direct",
        recovery_token=execution.recovery_token,
        owner_id=execution.owner_id,
    )


@pytest.mark.asyncio
async def test_direct_pretool_policy_blocks_forbidden_commands_and_paths(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "direct-policy.db")
    observer = ExecutionLifecycleObserver(
        store=store, discord=_Discord(), workspace=WORKSPACE
    )

    with pytest.raises(ValueError, match="outside approved actions"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "git push origin main", "workdir": WORKSPACE},
        )
    with pytest.raises(ValueError, match="outside the approved workspace"):
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
            {"path": f"{WORKSPACE}/safe.py", "cross_profile": True},
        )
    with pytest.raises(ValueError, match="Git metadata"):
        observer.authorize_tool_start(
            execution.execution_id,
            "write_file",
            {"path": f"{WORKSPACE}/.Git/hooks/pre-commit"},
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
    with pytest.raises(ValueError, match="not approved"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "python -c 'print(1)'", "workdir": WORKSPACE},
        )
    with pytest.raises(ValueError, match="shell composition"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {
                "command": "python -m pytest tests/unit -q; echo $GITHUB_PAT_TOKEN",
                "workdir": WORKSPACE,
            },
        )
    with pytest.raises(ValueError, match="Git metadata"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {
                "command": "python -m pytest .git/hooks/test_hook.py -q",
                "workdir": WORKSPACE,
            },
        )

    assert store.get_execution(execution.execution_id).status == "queued"
    observer.authorize_tool_start(
        execution.execution_id,
        "terminal",
        {"command": "python -m pytest tests/unit -q", "workdir": WORKSPACE},
    )
    assert store.get_execution(execution.execution_id).status == "running"


@pytest.mark.asyncio
async def test_direct_pretool_policy_blocks_symlink_workspace_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    escape = workspace / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    store, _, execution = _seed_queued(
        tmp_path / "symlink-policy.db",
        workspace=str(workspace),
    )
    observer = ExecutionLifecycleObserver(
        store=store,
        discord=_Discord(),
        workspace=str(workspace),
    )

    with pytest.raises(ValueError, match="outside the approved workspace"):
        observer.authorize_tool_start(
            execution.execution_id,
            "patch",
            {
                "mode": "replace",
                "path": str(escape / "outside.py"),
            },
        )
    with pytest.raises(ValueError, match="outside the approved workspace"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {
                "command": "python -m pytest tests/unit -q",
                "workdir": str(escape),
            },
        )
    with pytest.raises(ValueError, match="outside the approved workspace"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {
                "command": "python -m pytest escape/test_outside.py -q",
                "workdir": str(workspace),
            },
        )


@pytest.mark.asyncio
async def test_publish_policy_allows_only_scoped_non_force_git_flow(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(
        tmp_path / "publish-policy.db",
        publish=True,
    )
    _store_source_event(
        store,
        api_url="https://api.github.com/repos/silviahealth/content/pulls/7",
    )
    observer = ExecutionLifecycleObserver(
        store=store,
        discord=_Discord(),
        workspace=WORKSPACE,
    )

    allowed_commands = (
        f"git fetch origin {HEAD_REF}",
        f"git switch {HEAD_REF}",
        "git add -- plugins/mention_inbox/approval.py",
        "git commit -m 'fix: address review'",
        f"git push origin HEAD:{HEAD_REF}",
    )
    for command in allowed_commands:
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": command, "workdir": WORKSPACE},
        )

    for command in (
        f"git push --force origin HEAD:{HEAD_REF}",
        "git push origin HEAD:other-branch",
        "git add -- .",
        "git reset --hard HEAD",
    ):
        with pytest.raises(ValueError):
            observer.authorize_tool_start(
                execution.execution_id,
                "terminal",
                {"command": command, "workdir": WORKSPACE},
            )


@pytest.mark.asyncio
async def test_publish_completion_requires_bound_git_receipts_and_redacts_summary(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(
        tmp_path / "publish-complete.db",
        publish=True,
    )
    _store_source_event(
        store,
        api_url="https://api.github.com/repos/silviahealth/content/pulls/7",
    )
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store,
        discord=discord,
        workspace=WORKSPACE,
    )

    receipts = (
        ("git status --porcelain", ""),
        ("git remote get-url origin", "https://github.com/silviahealth/content.git"),
        (f"git switch {HEAD_REF}", ""),
        ("git rev-parse --abbrev-ref HEAD", HEAD_REF),
        ("git rev-parse HEAD", "head-1"),
        ("python -m pytest tests/unit -q", "1 passed"),
        ("git add -- plugins/mention_inbox/approval.py", ""),
        ("git commit -m 'fix: address review'", "[feature abc1234] fix"),
        ("git rev-parse HEAD", COMMIT_SHA),
        (f"git push origin HEAD:{HEAD_REF}", "updated"),
    )
    for command, output in receipts:
        args = {"command": command, "workdir": WORKSPACE}
        observer.authorize_tool_start(execution.execution_id, "terminal", args)
        observer.tool_completed(
            execution.execution_id,
            "terminal",
            {"exit_code": 0, "output": output},
            args=args,
        )

    outcome = await observer.run_completed(
        execution.execution_id,
        {
            "completed": True,
            "final_response": (
                "수정·테스트·push 완료. commit abc1234 "
                "github_pat_abcdefghijklmnopqrstuvwxyz"
            ),
        },
    )

    assert outcome == "completed"
    rendered = "\n".join(content for _, content in discord.sent)
    assert "abc1234" in rendered
    assert COMMIT_SHA in rendered
    assert "github_pat_abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "non-force push 성공" in rendered


@pytest.mark.asyncio
async def test_publish_completion_requires_post_commit_sha_receipt(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(
        tmp_path / "publish-missing-commit-sha.db",
        publish=True,
    )
    _store_source_event(
        store,
        api_url="https://api.github.com/repos/silviahealth/content/pulls/7",
    )
    discord = _Discord()
    observer = ExecutionLifecycleObserver(
        store=store,
        discord=discord,
        workspace=WORKSPACE,
    )

    receipts = (
        ("git status --porcelain", ""),
        ("git remote get-url origin", "https://github.com/silviahealth/content.git"),
        ("git rev-parse --abbrev-ref HEAD", HEAD_REF),
        ("git rev-parse HEAD", "head-1"),
        ("python -m pytest tests/unit -q", "1 passed"),
        ("git add -- plugins/mention_inbox/approval.py", ""),
        ("git commit -m 'fix: address review'", "[feature abc1234] fix"),
        (f"git push origin HEAD:{HEAD_REF}", "updated"),
    )
    for command, output in receipts:
        args = {"command": command, "workdir": WORKSPACE}
        observer.authorize_tool_start(execution.execution_id, "terminal", args)
        observer.tool_completed(
            execution.execution_id,
            "terminal",
            {"exit_code": 0, "output": output},
            args=args,
        )

    outcome = await observer.run_completed(
        execution.execution_id,
        {"completed": True, "final_response": "commit SHA 확인 없이 완료"},
    )

    assert outcome == "blocked"
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.BLOCKED
    assert "verification_missing" in discord.edits[-1][2]


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
    store, _, execution = _seed_queued(
        tmp_path / "recover.db", active_owner=False
    )
    resolver = _Resolver(_state())
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
async def test_overlapping_recovery_instances_atomically_claim_execution(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recover-claim.db"
    store, _, execution = _seed_queued(db, active_owner=False)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingResolver(_Resolver):
        async def resolve(self, proposal):
            self.calls += 1
            entered.set()
            await release.wait()
            return self.state

    first_dispatcher = _RecoveryDispatcher()
    second_dispatcher = _RecoveryDispatcher()
    first = _handler(
        MentionInboxStore(db, clock=lambda: NOW),
        BlockingResolver(_state()),
        first_dispatcher,
        _Discord(),
    )
    second = _handler(
        MentionInboxStore(db, clock=lambda: NOW),
        _Resolver(_state()),
        second_dispatcher,
        _Discord(),
    )

    first_task = asyncio.create_task(first.recover_queued())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert await second.recover_queued() == 0
    release.set()
    assert await first_task == 1
    assert [request.execution_id for request in first_dispatcher.requests] == [
        execution.execution_id
    ]
    assert second_dispatcher.requests == []


@pytest.mark.asyncio
async def test_released_execution_is_not_reclaimed_in_same_recovery_pass(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(
        tmp_path / "recover-release-pass.db", active_owner=False
    )
    resolver = _Resolver(None)
    recovery_store = MentionInboxStore(
        store.path,
        clock=lambda: NOW + timedelta(seconds=61),
    )
    handler = _handler(
        recovery_store,
        resolver,
        _RecoveryDispatcher(),
        _Discord(),
    )

    assert await handler.recover_queued() == 0
    assert resolver.calls == 1
    assert store.get_execution(execution.execution_id).status == "queued"
    later_claims = recovery_store.claim_recoverable_executions(
        limit=1,
        lease_seconds=60,
    )
    assert [claim.execution.execution_id for claim in later_claims] == [
        execution.execution_id
    ]


@pytest.mark.asyncio
async def test_fresh_run_instances_have_one_durable_side_effect_owner(
    tmp_path: Path,
) -> None:
    first_owner = "adapter-instance-a"
    store, _, execution = _seed_queued(
        tmp_path / "durable-run-owner.db",
        owner_id=first_owner,
    )
    assert execution.recovery_token is not None
    second_owner = "adapter-instance-b"

    first_store = MentionInboxStore(store.path, clock=lambda: NOW)
    second_store = MentionInboxStore(store.path, clock=lambda: NOW)
    assert first_store.admit_execution_owner(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id=first_owner,
    )
    assert not second_store.admit_execution_owner(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id=second_owner,
    )

    observer = ExecutionLifecycleObserver(
        store=store,
        discord=_Discord(),
        workspace=WORKSPACE,
    )
    with pytest.raises(ValueError, match="owner"):
        observer.authorize_tool_start(
            execution.execution_id,
            "terminal",
            {"command": "python -m pytest tests/unit -q", "workdir": WORKSPACE},
            recovery_token=execution.recovery_token,
            owner_id=second_owner,
        )
    observer.authorize_tool_start(
        execution.execution_id,
        "terminal",
        {"command": "python -m pytest tests/unit -q", "workdir": WORKSPACE},
        recovery_token=execution.recovery_token,
        owner_id=first_owner,
    )
    assert store.get_execution(execution.execution_id).status == "running"


def test_expired_owner_is_replaced_and_stale_owner_cannot_finalize(
    tmp_path: Path,
) -> None:
    db = tmp_path / "owner-expiry.db"
    now = [NOW]
    _, _, execution = _seed_queued(db)
    store = MentionInboxStore(db, clock=lambda: now[0])
    assert execution.recovery_token is not None
    assert store.admit_execution_owner(
        execution.execution_id,
        recovery_token=execution.recovery_token,
        owner_id="test-owner",
    )

    now[0] += timedelta(seconds=60)
    replacement = store.claim_recoverable_executions(limit=1, lease_seconds=60)[0]
    assert replacement.recovery_token is not None
    assert replacement.recovery_token != execution.recovery_token
    assert store.admit_execution_owner(
        execution.execution_id,
        recovery_token=replacement.recovery_token,
        owner_id="replacement-owner",
    )
    with pytest.raises(ValueError, match="owner"):
        store.mark_execution_blocked(
            execution.execution_id,
            evidence_category="agent_failed",
            recovery_token=execution.recovery_token,
            owner_id="test-owner",
        )


@pytest.mark.asyncio
async def test_recovery_heartbeat_keeps_slow_resolver_exclusive(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recover-heartbeat.db"
    _, _, execution = _seed_queued(db, active_owner=False)
    now = [NOW]

    class HeartbeatStore(MentionInboxStore):
        def __init__(self) -> None:
            super().__init__(db, clock=lambda: now[0])
            self.renewed = asyncio.Event()

        def renew_execution_recovery_lease(
            self,
            execution_id: str,
            *,
            recovery_token: str,
            lease_seconds: int = 60,
        ) -> str | None:
            renewed = super().renew_execution_recovery_lease(
                execution_id,
                recovery_token=recovery_token,
                lease_seconds=lease_seconds,
            )
            if renewed is not None:
                self.renewed.set()
            return renewed

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingResolver(_Resolver):
        async def resolve(self, proposal):
            self.calls += 1
            entered.set()
            await release.wait()
            return self.state

    store = HeartbeatStore()
    first_dispatcher = _RecoveryDispatcher()
    first = _handler(
        store,
        BlockingResolver(_state()),
        first_dispatcher,
        _Discord(),
        recovery_lease_seconds=1,
    )
    second_dispatcher = _RecoveryDispatcher()
    second = _handler(
        MentionInboxStore(db, clock=lambda: now[0]),
        _Resolver(_state()),
        second_dispatcher,
        _Discord(),
        recovery_lease_seconds=1,
    )

    first_task = asyncio.create_task(first.recover_queued())
    await asyncio.wait_for(entered.wait(), timeout=2)
    store.renewed.clear()
    now[0] += timedelta(milliseconds=500)
    await asyncio.wait_for(store.renewed.wait(), timeout=2)
    now[0] += timedelta(milliseconds=600)

    assert await second.recover_queued() == 0
    release.set()
    assert await first_task == 1
    assert [request.execution_id for request in first_dispatcher.requests] == [
        execution.execution_id
    ]
    assert second_dispatcher.requests == []


@pytest.mark.asyncio
async def test_reclaimed_recovery_cancels_stale_worker_without_dispatch(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recover-reclaimed.db"
    _, _, execution = _seed_queued(db, active_owner=False)
    now = [NOW]

    class ReclaimedStore(MentionInboxStore):
        def __init__(self) -> None:
            super().__init__(db, clock=lambda: now[0])
            self.reject_renewal = False
            self.renewal_rejected = asyncio.Event()

        def renew_execution_recovery_lease(
            self,
            execution_id: str,
            *,
            recovery_token: str,
            lease_seconds: int = 60,
        ) -> str | None:
            if self.reject_renewal:
                self.renewal_rejected.set()
                return None
            return super().renew_execution_recovery_lease(
                execution_id,
                recovery_token=recovery_token,
                lease_seconds=lease_seconds,
            )

    entered = asyncio.Event()

    class BlockingResolver(_Resolver):
        async def resolve(self, proposal):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
            return self.state

    stale_store = ReclaimedStore()
    stale_dispatcher = _RecoveryDispatcher()
    stale_handler = _handler(
        stale_store,
        BlockingResolver(_state()),
        stale_dispatcher,
        _Discord(),
        recovery_lease_seconds=1,
    )
    stale_task = asyncio.create_task(stale_handler.recover_queued())
    await asyncio.wait_for(entered.wait(), timeout=2)
    stale_store.reject_renewal = True
    now[0] += timedelta(seconds=2)

    current_store = MentionInboxStore(db, clock=lambda: now[0])
    current_dispatcher = _RecoveryDispatcher()
    current_handler = _handler(
        current_store,
        _Resolver(_state()),
        current_dispatcher,
        _Discord(),
        recovery_lease_seconds=1,
    )

    assert await current_handler.recover_queued() == 1
    await asyncio.wait_for(stale_store.renewal_rejected.wait(), timeout=2)
    assert await asyncio.wait_for(stale_task, timeout=2) == 0
    assert stale_dispatcher.requests == []
    assert [request.execution_id for request in current_dispatcher.requests] == [
        execution.execution_id
    ]
    assert current_store.claim_recoverable_executions(
        limit=1,
        lease_seconds=1,
    ) == ()


@pytest.mark.asyncio
async def test_external_recovery_cancellation_propagates(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recover-cancelled.db"
    _seed_queued(db, active_owner=False)
    entered = asyncio.Event()

    class BlockingResolver(_Resolver):
        async def resolve(self, proposal):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
            return self.state

    store = MentionInboxStore(db, clock=lambda: NOW)
    handler = _handler(
        store,
        BlockingResolver(_state()),
        _RecoveryDispatcher(),
        _Discord(),
        recovery_lease_seconds=1,
    )
    recovery_task = asyncio.create_task(handler.recover_queued())
    await asyncio.wait_for(entered.wait(), timeout=2)

    recovery_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery_task
    assert store.claim_recoverable_executions(
        limit=1,
        lease_seconds=1,
    ) == ()


def test_stale_recovery_claim_cannot_release_new_owner(
    tmp_path: Path,
) -> None:
    db = tmp_path / "recover-token.db"
    _, _, execution = _seed_queued(db, active_owner=False)
    now = [NOW]
    first_store = MentionInboxStore(db, clock=lambda: now[0])
    first = first_store.claim_recoverable_executions(
        limit=1,
        lease_seconds=60,
    )[0]
    assert first.recovery_token is not None
    now[0] += timedelta(seconds=61)
    second_store = MentionInboxStore(db, clock=lambda: now[0])
    second = second_store.claim_recoverable_executions(
        limit=1,
        lease_seconds=60,
    )[0]
    assert second.recovery_token is not None
    assert second.recovery_token != first.recovery_token

    assert (
        first_store.release_execution_recovery(
            execution.execution_id,
            recovery_token=first.recovery_token,
        )
        is False
    )
    assert (
        MentionInboxStore(
            db,
            clock=lambda: now[0],
        ).claim_recoverable_executions(limit=1, lease_seconds=60)
        == ()
    )


@pytest.mark.asyncio
async def test_policy_rollback_durably_invalidates_queued_external_execution(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(tmp_path / "policy-rollback.db")
    _store_source_event(
        store,
        api_url="https://api.github.com/repos/external/project/pulls/7",
        repository="external/project",
    )
    discord = _Discord()
    handler = _handler(
        store,
        _Resolver(_state()),
        _RecoveryDispatcher(),
        discord,
    )

    changed = await handler.reconcile_execution_policy(
        trusted_repositories=frozenset({"silviahealth/content"}),
        external_repository_actions="disabled",
    )

    assert changed == 1
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL
    assert "이전 실행 요청은 사용하지 않고" in discord.sent[0][1]
    assert await handler.recover_queued() == 0


@pytest.mark.asyncio
async def test_recovery_recreates_exact_execution_worktree_before_readmission(
    tmp_path: Path,
) -> None:
    manager = _WorktreeManager(tmp_path / "workspaces")
    head_sha = "a" * 40
    store, _, execution = _seed_queued(
        tmp_path / "recover-worktree.db",
        workspace_manager=manager,
        head_sha=head_sha,
        active_owner=False,
    )
    dispatcher = _RecoveryDispatcher()
    handler = _handler(
        store,
        _Resolver(_state(head_sha)),
        dispatcher,
        _Discord(),
        workspace_manager=manager,
    )

    recovered = await handler.recover_queued()

    assert recovered == 1
    assert len(manager.requests) == 1
    assert manager.requests[0].execution_id == execution.execution_id
    assert dispatcher.requests[0].workspace == execution.workspace


@pytest.mark.asyncio
async def test_recovery_marks_stale_head_for_reapproval_without_dispatch(
    tmp_path: Path,
) -> None:
    store, _, execution = _seed_queued(
        tmp_path / "recover-stale.db", active_owner=False
    )
    resolver = _Resolver(_state("new-head"))
    dispatcher = _RecoveryDispatcher()
    discord = _Discord()
    handler = _handler(store, resolver, dispatcher, discord)

    recovered = await handler.recover_queued()

    assert recovered == 0
    assert dispatcher.requests == []
    assert store.get_execution(execution.execution_id).status == "blocked"
    assert store.get_latest_proposal(SUBJECT).status is ProposalStatus.NEEDS_REAPPROVAL
    assert len(discord.sent) == 1
    assert "이전 실행 요청은 사용하지 않고" in discord.sent[0][1]


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
        head_ref=HEAD_REF,
        head_repository=HEAD_REPOSITORY,
        workspace=WORKSPACE,
    )
    dispatcher = _RecoveryDispatcher()
    discord = _Discord()
    handler = _handler(
        store,
        _Resolver(_state()),
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
