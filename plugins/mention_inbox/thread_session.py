"""Durable Discord thread and no-tools proposal bootstrap for work items."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from plugins.mention_inbox.advisory import ProposalAdvisor, build_advisory_context
from plugins.mention_inbox.contract import MentionEvent
from plugins.mention_inbox.preflight import (
    PreApprovalBrief,
    PreApprovalDisposition,
    brief_from_metadata,
)
from plugins.mention_inbox.proposals import (
    ProposalStatus,
    WorkProposal,
    build_work_proposal,
    revise_work_proposal,
)
from plugins.mention_inbox.store import MentionInboxStore, WorkItemSession
from plugins.mention_inbox.voice import (
    render_advisory,
    render_execution_enabled_reproposal,
    render_needs_reapproval,
    render_proposal,
    render_thread_opened,
    render_thread_update,
)

logger = logging.getLogger(__name__)


class ThreadSessionTransport(Protocol):
    def remember_parent_message(
        self, parent_message_id: str, parent_channel_id: str
    ) -> None: ...

    async def find_anchored_thread(self, parent_message_id: str) -> str | None: ...

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str: ...

    async def ensure_thread_participants(
        self,
        thread_id: str,
        user_ids: frozenset[str],
    ) -> None: ...

    async def is_thread_active(self, thread_id: str) -> bool: ...

    async def thread_has_parent(
        self,
        thread_id: str,
        parent_channel_id: str,
    ) -> bool: ...

    async def activate_thread(self, thread_id: str) -> None: ...

    def mark_thread_participation(self, thread_id: str) -> None: ...

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None: ...

    async def send_to_thread(self, thread_id: str, content: str) -> str: ...


class ThreadParticipantSyncError(RuntimeError):
    """Discord rejected synchronization of configured work-thread participants."""


class ThreadDestinationMismatchError(RuntimeError):
    """A durable work thread belongs to a different Discord destination."""


class ThreadParticipantReconciliationIncompleteError(RuntimeError):
    """Startup membership reconciliation exceeded its bounded session window."""


_NO_PARTICIPANT_USER_IDS: frozenset[str] = frozenset()
DeliveryCheckpoint = Callable[[], Awaitable[None]]


async def _checkpoint_delivery(checkpoint: DeliveryCheckpoint | None) -> None:
    if checkpoint is not None:
        await checkpoint()


@dataclass(frozen=True)
class ThreadParticipantReconciliation:
    examined: int
    repaired: int
    skipped: int
    failed: int
    overflow: int = 0


def _metadata(event: MentionEvent) -> Mapping[str, object]:
    value = event.untrusted.metadata
    return value if isinstance(value, Mapping) else {}


def _bounded(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _subject_key(event: MentionEvent) -> str:
    value = _metadata(event).get("subject_key")
    return value if isinstance(value, str) and value else event.thread.thread_id


def _head_sha(event: MentionEvent) -> str | None:
    value = _metadata(event).get("subject_head_sha")
    return value if isinstance(value, str) and value else None


def _thread_name(event: MentionEvent) -> str:
    metadata = _metadata(event)
    repository = _bounded(metadata.get("repository") or "GitHub", 45)
    number = metadata.get("subject_number")
    title = _bounded(event.untrusted.title, 45)
    prefix = f"{repository} #{number}" if isinstance(number, int) else repository
    return _bounded(f"{prefix} · {title}", 90)


_DISPOSITION_TEXT = {
    PreApprovalDisposition.ACTION_REQUIRED: "현재 HEAD에 결속된 변경 요청이라 작업이 필요해요.",
    PreApprovalDisposition.REVIEW_NEEDED: "현재 HEAD에서 확인이 필요한 리뷰 요청이에요.",
    PreApprovalDisposition.POSSIBLY_STALE: "이전 commit의 의견일 수 있어요.",
    PreApprovalDisposition.INFORMATIONAL: "현재 상태에서는 정보성 알림으로 확인됐어요.",
    PreApprovalDisposition.INSUFFICIENT_EVIDENCE: "상세를 안전하게 확인하지 못했어요.",
}
_OWN_PR_ACTION_KINDS = frozenset(
    {
        "own_pr_comment",
        "own_pr_review_comment",
        "own_pr_review_summary",
        "own_pr_changes_requested",
    }
)


def _bound_brief(event: MentionEvent, *, source_revision: str | None) -> PreApprovalBrief | None:
    metadata = _metadata(event)
    brief = brief_from_metadata(metadata.get("preapproval_brief"))
    if brief is None:
        return None
    expected_revision = source_revision or metadata.get("source_revision")
    if not isinstance(expected_revision, str) or brief.source_revision != expected_revision:
        return None
    if brief.head_sha != _head_sha(event):
        return None
    return brief


def _read_only_content(
    *, repository: str, title: str, explanation: str
) -> dict[str, object]:
    return {
        "goal": f"{repository}의 ‘{title}’에서 {explanation}",
        "steps": (
            "먼저 읽기 전용으로 원문과 현재 HEAD를 다시 확인한다.",
            "근거가 결속되면 필요한 작업과 검증 범위를 갱신한다.",
        ),
        "allowed_actions": ("read_repository",),
        "forbidden_actions": (
            "edit_files",
            "run_mutating_commands",
            "merge",
            "deploy",
            "delete",
            "read_secrets",
            "change_live_configuration",
        ),
        "verification": (
            "원문 event와 현재 HEAD 재결속",
            "변경 전 최신 작업 범위 확인",
        ),
        "executor_hint": "direct",
    }


def _external_write_allowed(
    event: MentionEvent,
    *,
    trusted_repositories: frozenset[str],
    external_repository_actions: str,
) -> bool:
    metadata = _metadata(event)
    repository = metadata.get("repository")
    if repository in trusted_repositories:
        return True
    actionable_kind = metadata.get("actionable_kind") or event.untrusted.action_detail
    return bool(
        external_repository_actions == "own_pr_write"
        and metadata.get("repository_private") is False
        and metadata.get("subject_owned_by_target") is True
        and actionable_kind in _OWN_PR_ACTION_KINDS
        and _head_sha(event) is not None
    )


def _proposal_content(
    event: MentionEvent,
    *,
    source_revision: str | None = None,
    host_write_allowed: bool = True,
    repository_code_execution_allowed: bool = True,
) -> dict[str, object]:
    metadata = _metadata(event)
    repository = _bounded(metadata.get("repository") or "repository", 100)
    title = _bounded(event.untrusted.title, 140)
    brief = _bound_brief(event, source_revision=source_revision)
    if brief is None:
        return _read_only_content(
            repository=repository,
            title=title,
            explanation="상세를 안전하게 확인하지 못했어요.",
        )

    explanation = _DISPOSITION_TEXT[brief.disposition]
    if not brief.approvable:
        if brief.disposition is PreApprovalDisposition.POSSIBLY_STALE:
            content = _read_only_content(
                repository=repository,
                title=title,
                explanation=f"{explanation} 확인한 내용: {brief.summary}",
            )
            content["steps"] = (
                "먼저 읽기 전용으로 현재 상태를 다시 확인한다.",
                "현재 HEAD에서 요청이 여전히 유효한지 확인한 뒤 새 제안을 만든다.",
            )
            return content
        return _read_only_content(
            repository=repository,
            title=title,
            explanation=f"{explanation} 확인한 내용: {brief.summary}",
        )

    if not host_write_allowed:
        return _read_only_content(
            repository=repository,
            title=title,
            explanation=f"{explanation} 확인한 내용: {brief.summary}",
        )

    steps: list[str] = []
    seen_urls: set[str] = set()
    for finding in brief.findings:
        location = finding.path or "review"
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        steps.append(
            f"확인된 요청 · {_bounded(location, 140)}: {_bounded(finding.body, 320)}"
        )
        if finding.source_url and finding.source_url not in seen_urls:
            seen_urls.add(finding.source_url)
            steps.append(f"원문: {_bounded(finding.source_url, 480)}")
    if not steps:
        steps.append(f"확인된 요청: {_bounded(brief.summary, 400)}")
    steps.append(
        "확인된 범위 안에서만 수정하고 대상 테스트로 검증한다."
        if repository_code_execution_allowed
        else "확인된 범위 안에서만 수정하고 repository code 실행 없이 검토한다."
    )
    allowed_actions = ["read_repository", "edit_scoped_files"]
    if repository_code_execution_allowed:
        allowed_actions.append("run_targeted_tests")
        verification = ["대상 테스트 통과"]
    else:
        verification = ["repository code를 실행하지 않은 정적 diff 검토"]
    verification.extend(
        ("변경 diff와 확인된 요청 대조", "완료 전 실제 실행 근거 확인")
    )
    actionable_kind = metadata.get("actionable_kind") or event.untrusted.action_detail
    if actionable_kind in _OWN_PR_ACTION_KINDS and _head_sha(event) is not None:
        allowed_actions.extend(
            (
                "switch_to_pr_branch",
                "commit_changes",
                "push_current_branch",
            )
        )
        verification.extend(
            (
                "현재 PR branch와 요청 시점 HEAD 일치",
                "선택 파일 commit SHA 확인",
                "현재 PR branch non-force push 성공",
            )
        )
    return {
        "goal": (
            f"{repository}의 ‘{title}’에서 {explanation} "
            f"확인한 내용: {brief.summary}"
        ),
        "steps": tuple(steps),
        "allowed_actions": tuple(allowed_actions),
        "forbidden_actions": (
            "merge",
            "deploy",
            "delete",
            "read_secrets",
            "change_live_configuration",
        ),
        "verification": tuple(verification),
        "executor_hint": "direct",
    }


def _matches_proposal_content(
    proposal: WorkProposal,
    *,
    head_sha: str | None,
    content: Mapping[str, object],
    executor_hint: str,
) -> bool:
    """Compare user-visible work meaning independently of event identity."""

    return (
        proposal.head_sha == head_sha
        and proposal.goal == content["goal"]
        and proposal.steps == tuple(content["steps"])
        and proposal.allowed_actions == tuple(content["allowed_actions"])
        and proposal.forbidden_actions == tuple(content["forbidden_actions"])
        and proposal.verification == tuple(content["verification"])
        and proposal.executor_hint == executor_hint
    )


class MentionInboxThreadCoordinator:
    """Create/recover one Discord thread and a local pending proposal per subject."""

    def __init__(
        self,
        *,
        store: MentionInboxStore,
        discord: ThreadSessionTransport,
        bot_mention: str,
        executor_hint: str = "direct",
        auto_archive_duration: int = 1440,
        approval_available: bool = False,
        trusted_repositories: frozenset[str] = frozenset({"silviahealth/content"}),
        external_repository_actions: str = "disabled",
        participant_user_ids: frozenset[str] = _NO_PARTICIPANT_USER_IDS,
        participant_parent_channel_id: str | None = None,
        advisor: ProposalAdvisor | None = None,
    ) -> None:
        if executor_hint not in {"direct", "kanban"}:
            raise ValueError("executor_hint must be direct or kanban")
        if auto_archive_duration not in {60, 1440, 4320, 10080}:
            raise ValueError("invalid Discord auto archive duration")
        if not isinstance(approval_available, bool):
            raise ValueError("approval_available must be a boolean")
        if external_repository_actions not in {
            "disabled",
            "inspect_only",
            "own_pr_write",
        }:
            raise ValueError("external_repository_actions is invalid")
        self._store = store
        self._discord = discord
        self._bot_mention = bot_mention
        self._executor_hint = executor_hint
        self._auto_archive_duration = auto_archive_duration
        self._approval_available = approval_available
        self._trusted_repositories = trusted_repositories
        self._external_repository_actions = external_repository_actions
        self._participant_user_ids: frozenset[str] = participant_user_ids
        self._participant_parent_channel_id = participant_parent_channel_id
        self._advisor = advisor
        self._locks: dict[str, asyncio.Lock] = {}

    async def _post_advisory(
        self,
        event: MentionEvent,
        *,
        thread_id: str,
        proposal: WorkProposal,
        source_revision: str,
    ) -> None:
        """Post the model-written advisory beside a just-sent proposal.

        Best effort by design. A slow or unreachable model, or a reply that
        sanitizes down to nothing, must leave the delivered proposal and its
        recorded binding untouched — the advisory is explanatory only. Callers
        reach this exactly once per revision, right after the proposal message
        binding is stored, so a failure here cannot cause a duplicate send.
        """

        advisor = self._advisor
        if advisor is None:
            return
        try:
            context = build_advisory_context(
                proposal=proposal,
                event=event,
                brief=_bound_brief(event, source_revision=source_revision),
                code_execution_allowed=(
                    _metadata(event).get("repository")
                    in self._trusted_repositories
                ),
            )
            if context is None:
                return
            advisory = render_advisory(await advisor.advise(context=context))
            if not advisory:
                return
            await self._discord.send_to_thread(thread_id, advisory)
        except Exception:
            logger.warning(
                "Mention-inbox proposal advisory unavailable; "
                "delivered the deterministic proposal only",
                exc_info=True,
            )

    def _lock(self, subject_key: str) -> asyncio.Lock:
        lock = self._locks.get(subject_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[subject_key] = lock
        return lock

    def _approval_offer(
        self, event: MentionEvent, *, source_revision: str
    ) -> tuple[bool, str | None]:
        if not self._approval_available:
            return False, "execution_unavailable"
        brief = _bound_brief(event, source_revision=source_revision)
        if brief is None or not brief.approvable:
            return False, "preflight_not_approvable"
        if not _external_write_allowed(
            event,
            trusted_repositories=self._trusted_repositories,
            external_repository_actions=self._external_repository_actions,
        ):
            return False, "approval_unavailable"
        return True, None

    def _proposal_for(
        self,
        event: MentionEvent,
        *,
        source_revision: str,
        approval_offered: bool,
    ) -> tuple[WorkProposal, WorkProposal | None, bool]:
        subject = _subject_key(event)
        head_sha = _head_sha(event)
        latest = self._store.get_latest_proposal(subject)
        metadata = _metadata(event)
        repository = metadata.get("repository")
        trusted_repository = repository in self._trusted_repositories
        host_write_allowed = _external_write_allowed(
            event,
            trusted_repositories=self._trusted_repositories,
            external_repository_actions=self._external_repository_actions,
        )
        content = _proposal_content(
            event,
            source_revision=source_revision,
            host_write_allowed=host_write_allowed,
            repository_code_execution_allowed=trusted_repository,
        )
        content["executor_hint"] = self._executor_hint
        if latest is None:
            proposal = build_work_proposal(
                revision=1,
                source_dedupe_key=event.dedupe_key,
                source_revision=source_revision,
                subject_key=subject,
                head_sha=head_sha,
                **content,
            )
            return proposal, None, False
        binding = self._store.get_proposal_message_binding(
            latest.proposal_id, latest.revision
        )
        capability_upgrade = bool(
            approval_offered
            and latest.status is ProposalStatus.PENDING
            and binding is not None
            and not binding.approval_offered
        )
        brief = _bound_brief(event, source_revision=source_revision)
        if (
            brief is not None
            and brief.disposition is PreApprovalDisposition.INFORMATIONAL
            and not capability_upgrade
        ):
            return latest, latest, False
        if _matches_proposal_content(
            latest,
            head_sha=head_sha,
            content=content,
            executor_hint=self._executor_hint,
        ) and not capability_upgrade:
            return latest, latest, False
        if (
            latest.source_dedupe_key == event.dedupe_key
            and latest.source_revision == source_revision
            and latest.head_sha == head_sha
            and not capability_upgrade
        ):
            return latest, latest, False
        if capability_upgrade:
            proposal = revise_work_proposal(
                latest,
                source_dedupe_key=event.dedupe_key,
                source_revision=source_revision,
                head_sha=head_sha,
                **content,
            )
            return proposal, latest, True
        proposal = revise_work_proposal(
            latest,
            source_dedupe_key=event.dedupe_key,
            source_revision=source_revision,
            head_sha=head_sha,
            **content,
        )
        return proposal, latest, False

    async def ensure_thread(
        self,
        event: MentionEvent,
        *,
        parent_message_id: str,
        parent_channel_id: str | None = None,
        source_revision: str,
        delivery_checkpoint: DeliveryCheckpoint | None = None,
    ) -> WorkItemSession:
        if not isinstance(event, MentionEvent):
            raise ValueError("event must be a MentionEvent")
        subject = _subject_key(event)
        async with self._lock(subject):
            configured_channel = self._participant_parent_channel_id
            incoming_channel = parent_channel_id or configured_channel
            if incoming_channel is None:
                raise ValueError("Discord parent channel is required")
            if (
                configured_channel is not None
                and configured_channel != incoming_channel
            ):
                raise ThreadDestinationMismatchError(
                    "Discord work thread does not belong to the configured "
                    "Discord destination"
                )
            existing = self._store.get_active_work_item_session(subject)
            if (
                existing is not None
                and existing.parent_channel_id is not None
                and existing.parent_channel_id != incoming_channel
            ):
                raise ThreadDestinationMismatchError(
                    "Discord work thread does not belong to the configured "
                    "Discord destination"
                )
            session = self._store.reserve_work_item_session(
                subject, event.dedupe_key, source_revision
            )
            if (
                existing is None
                or existing.parent_message_id is None
                or existing.parent_channel_id is None
            ):
                session = self._store.prepare_work_item_parent(
                    subject, parent_message_id, incoming_channel
                )
            parent = session.parent_message_id or parent_message_id
            durable_channel = session.parent_channel_id or incoming_channel
            self._discord.remember_parent_message(parent, durable_channel)

            thread_id = session.discord_thread_id
            if thread_id is None:
                thread_id = await self._discord.find_anchored_thread(parent)
                if thread_id is None and parent != parent_message_id:
                    session = (
                        self._store.replace_unthreaded_work_item_parent(
                            subject,
                            expected_parent_message_id=parent,
                            parent_message_id=parent_message_id,
                            parent_channel_id=durable_channel,
                        )
                    )
                    parent = session.parent_message_id or parent_message_id
                    self._discord.remember_parent_message(parent, durable_channel)
                    thread_id = await self._discord.find_anchored_thread(parent)
                if thread_id is None:
                    await _checkpoint_delivery(delivery_checkpoint)
                    thread_id = await self._discord.create_anchored_thread(
                        parent,
                        _thread_name(event),
                        self._auto_archive_duration,
                    )
            if not await self._discord.thread_has_parent(
                thread_id,
                durable_channel,
            ):
                raise ThreadDestinationMismatchError(
                    "Discord work thread does not belong to the configured "
                    "Discord destination"
                )
            session = self._store.record_work_item_thread(
                subject, parent, durable_channel, thread_id
            )
            if self._participant_user_ids:
                try:
                    await _checkpoint_delivery(delivery_checkpoint)
                    await self._discord.activate_thread(thread_id)
                    await _checkpoint_delivery(delivery_checkpoint)
                    await self._discord.ensure_thread_participants(
                        thread_id,
                        self._participant_user_ids,
                    )
                except Exception as exc:
                    raise ThreadParticipantSyncError(
                        "Discord thread participant synchronization failed"
                    ) from exc
            await _checkpoint_delivery(delivery_checkpoint)
            self._discord.mark_thread_participation(thread_id)

            approval_offered, unavailable_reason = self._approval_offer(
                event, source_revision=source_revision
            )
            proposal, previous, capability_upgrade = self._proposal_for(
                event,
                source_revision=source_revision,
                approval_offered=approval_offered,
            )
            is_new_proposal = (
                previous is None or proposal.revision != previous.revision
            )
            if is_new_proposal:
                self._store.create_proposal(proposal)
            message_id = self._store.get_proposal_message_id(
                proposal.proposal_id, proposal.revision
            )
            if message_id is None:
                parts: list[str] = []
                if proposal.revision == 1:
                    parts.append(render_thread_opened(event))
                elif capability_upgrade and previous is not None:
                    parts.append(render_execution_enabled_reproposal(previous))
                elif previous is not None:
                    parts.append(render_needs_reapproval(previous))
                parts.append(
                    render_proposal(
                        proposal,
                        self._bot_mention,
                        approval_offered=approval_offered,
                        approval_unavailable_reason=unavailable_reason,
                        event=event,
                    )
                )
                content = "\n\n".join(parts)
                message_id = await self._discord.find_message_content(
                    thread_id, content, limit=100
                )
                if message_id is not None:
                    existing_binding = (
                        self._store.get_proposal_message_binding_by_message_id(
                            message_id
                        )
                    )
                    if (
                        existing_binding is not None
                        and (
                            existing_binding.proposal.proposal_id
                            != proposal.proposal_id
                            or existing_binding.proposal.revision
                            != proposal.revision
                        )
                    ):
                        # Two HEAD revisions can intentionally render the same
                        # user-facing text.  A message already bound to the
                        # older revision cannot also identify this proposal.
                        message_id = None
                if message_id is None:
                    await _checkpoint_delivery(delivery_checkpoint)
                    proposal_sender = getattr(
                        self._discord,
                        "send_proposal_to_thread",
                        None,
                    )
                    if callable(proposal_sender):
                        message_id = await proposal_sender(
                            thread_id,
                            content,
                            proposal_id=proposal.proposal_id,
                            proposal_revision=proposal.revision,
                            approval_offered=approval_offered,
                        )
                    else:
                        message_id = await self._discord.send_to_thread(
                            thread_id,
                            content,
                        )
                    await _checkpoint_delivery(delivery_checkpoint)
                self._store.record_proposal_message(
                    proposal.proposal_id,
                    proposal.revision,
                    message_id,
                    approval_offered=approval_offered,
                )
                await self._post_advisory(
                    event,
                    thread_id=thread_id,
                    proposal=proposal,
                    source_revision=source_revision,
                )

            restored = self._store.get_active_work_item_session(subject)
            if restored is None:
                raise RuntimeError("work item session disappeared")
            return restored

    async def deliver_to_existing_thread(
        self,
        event: MentionEvent,
        *,
        source_revision: str,
        delivery_checkpoint: DeliveryCheckpoint | None = None,
    ) -> str | None:
        """Route a later event through the subject's one durable PR thread."""

        if not isinstance(event, MentionEvent):
            raise ValueError("event must be a MentionEvent")
        subject = _subject_key(event)
        session = self._store.get_work_item_session(subject)
        if (
            session is None
            or session.parent_message_id is None
            or session.discord_thread_id is None
        ):
            return None
        before = self._store.get_latest_proposal(subject)
        await self.ensure_thread(
            event,
            parent_message_id=session.parent_message_id,
            parent_channel_id=session.parent_channel_id,
            source_revision=source_revision,
            delivery_checkpoint=delivery_checkpoint,
        )
        latest = self._store.get_latest_proposal(subject)
        if latest is None:
            raise RuntimeError("work item proposal disappeared")
        brief = _bound_brief(event, source_revision=source_revision)
        if (
            before is not None
            and latest.proposal_id == before.proposal_id
            and latest.revision == before.revision
            and brief is not None
            and brief.disposition is PreApprovalDisposition.INFORMATIONAL
        ):
            content = render_thread_update(event)
            message_id = await self._discord.find_message_content(
                session.discord_thread_id, content, limit=100
            )
            if message_id is None:
                await _checkpoint_delivery(delivery_checkpoint)
                message_id = await self._discord.send_to_thread(
                    session.discord_thread_id, content
                )
                await _checkpoint_delivery(delivery_checkpoint)
            return message_id
        binding = self._store.get_proposal_message_binding(
            latest.proposal_id, latest.revision
        )
        if binding is None:
            raise RuntimeError("work item proposal message disappeared")
        return binding.message_id

    async def reconcile_execution_activation(self, *, limit: int = 100) -> int:
        if not self._approval_available:
            return 0
        reconciled = 0
        for session in self._store.list_active_work_item_sessions(limit=limit):
            if (
                session.parent_message_id is None
                or session.discord_thread_id is None
            ):
                continue
            try:
                if (
                    self._participant_parent_channel_id is not None
                    and not await self._discord.thread_has_parent(
                        session.discord_thread_id,
                        self._participant_parent_channel_id,
                    )
                ):
                    continue
                if not await self._discord.is_thread_active(
                    session.discord_thread_id
                ):
                    continue
            except Exception:
                continue
            stored = self._store.get(session.source_dedupe_key)
            if stored is None or _subject_key(stored.event) != session.subject_key:
                continue
            before = self._store.get_latest_proposal(session.subject_key)
            if before is None:
                continue
            try:
                await self.ensure_thread(
                    stored.event,
                    parent_message_id=session.parent_message_id,
                    source_revision=stored.source_revision,
                )
            except Exception:
                continue
            after = self._store.get_latest_proposal(session.subject_key)
            if (
                after is not None
                and after.revision == before.revision + 1
                and after.source_revision == before.source_revision
                and after.head_sha == before.head_sha
            ):
                binding = self._store.get_proposal_message_binding(
                    after.proposal_id, after.revision
                )
                if binding is not None and binding.approval_offered:
                    reconciled += 1
        return reconciled

    async def reconcile_thread_participants(
        self,
        *,
        limit: int = 1000,
    ) -> ThreadParticipantReconciliation:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not self._participant_user_ids:
            return ThreadParticipantReconciliation(0, 0, 0, 0)
        examined = 0
        repaired = 0
        skipped = 0
        failed = 0
        sessions = self._store.list_active_work_item_sessions(
            limit=limit,
            include_overflow=True,
        )
        overflow = max(len(sessions) - limit, 0)
        for session in sessions[:limit]:
            examined += 1
            thread_id = session.discord_thread_id
            if thread_id is None:
                skipped += 1
                continue
            try:
                if (
                    self._participant_parent_channel_id is not None
                    and not await self._discord.thread_has_parent(
                        thread_id,
                        self._participant_parent_channel_id,
                    )
                ):
                    skipped += 1
                    continue
                if not await self._discord.is_thread_active(thread_id):
                    skipped += 1
                    continue
                await self._discord.ensure_thread_participants(
                    thread_id,
                    self._participant_user_ids,
                )
            except Exception:
                failed += 1
            else:
                repaired += 1
        return ThreadParticipantReconciliation(
            examined=examined,
            repaired=repaired,
            skipped=skipped,
            failed=failed,
            overflow=overflow,
        )
