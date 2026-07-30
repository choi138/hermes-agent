"""Durable Discord thread and no-tools proposal bootstrap for work items."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol

from plugins.mention_inbox.contract import MentionEvent
from plugins.mention_inbox.preflight import (
    PreApprovalBrief,
    PreApprovalDisposition,
    brief_from_metadata,
)
from plugins.mention_inbox.proposals import (
    WorkProposal,
    build_work_proposal,
    revise_work_proposal,
)
from plugins.mention_inbox.store import MentionInboxStore, WorkItemSession
from plugins.mention_inbox.voice import (
    render_needs_reapproval,
    render_proposal,
    render_thread_opened,
)


class ThreadSessionTransport(Protocol):
    async def find_anchored_thread(self, parent_message_id: str) -> str | None: ...

    async def create_anchored_thread(
        self, parent_message_id: str, name: str, auto_archive_duration: int
    ) -> str: ...

    def mark_thread_participation(self, thread_id: str) -> None: ...

    async def find_message_content(
        self, thread_id: str, content: str, *, limit: int
    ) -> str | None: ...

    async def send_to_thread(self, thread_id: str, content: str) -> str: ...


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
            "근거가 결속된 새 제안을 만든 뒤에만 변경 승인을 요청한다.",
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
            "변경 시작 전 새 제안 확인",
        ),
        "executor_hint": "direct",
    }


def _proposal_content(
    event: MentionEvent, *, source_revision: str | None = None
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
    steps.append("승인된 범위 안에서만 수정하고 대상 테스트로 검증한다.")
    return {
        "goal": (
            f"{repository}의 ‘{title}’에서 {explanation} "
            f"확인한 내용: {brief.summary}"
        ),
        "steps": tuple(steps),
        "allowed_actions": (
            "read_repository",
            "edit_scoped_files",
            "run_targeted_tests",
        ),
        "forbidden_actions": (
            "merge",
            "deploy",
            "delete",
            "read_secrets",
            "change_live_configuration",
        ),
        "verification": (
            "대상 테스트 통과",
            "변경 diff와 확인된 요청 대조",
            "완료 전 실제 실행 근거 확인",
        ),
        "executor_hint": "direct",
    }


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
    ) -> None:
        if executor_hint not in {"direct", "kanban"}:
            raise ValueError("executor_hint must be direct or kanban")
        if auto_archive_duration not in {60, 1440, 4320, 10080}:
            raise ValueError("invalid Discord auto archive duration")
        if not isinstance(approval_available, bool):
            raise ValueError("approval_available must be a boolean")
        self._store = store
        self._discord = discord
        self._bot_mention = bot_mention
        self._executor_hint = executor_hint
        self._auto_archive_duration = auto_archive_duration
        self._approval_available = approval_available
        self._locks: dict[str, asyncio.Lock] = {}

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
        return True, None

    def _proposal_for(
        self, event: MentionEvent, *, source_revision: str
    ) -> tuple[WorkProposal, WorkProposal | None]:
        subject = _subject_key(event)
        head_sha = _head_sha(event)
        latest = self._store.get_latest_proposal(subject)
        content = _proposal_content(event, source_revision=source_revision)
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
            return proposal, None
        if (
            latest.source_dedupe_key == event.dedupe_key
            and latest.source_revision == source_revision
            and latest.head_sha == head_sha
        ):
            return latest, latest
        proposal = revise_work_proposal(
            latest,
            source_dedupe_key=event.dedupe_key,
            source_revision=source_revision,
            head_sha=head_sha,
            **content,
        )
        return proposal, latest

    async def ensure_thread(
        self,
        event: MentionEvent,
        *,
        parent_message_id: str,
        source_revision: str,
    ) -> WorkItemSession:
        if not isinstance(event, MentionEvent):
            raise ValueError("event must be a MentionEvent")
        subject = _subject_key(event)
        async with self._lock(subject):
            existing = self._store.get_active_work_item_session(subject)
            session = self._store.reserve_work_item_session(
                subject, event.dedupe_key, source_revision
            )
            if existing is None or existing.parent_message_id is None:
                session = self._store.prepare_work_item_parent(
                    subject, parent_message_id
                )
            parent = session.parent_message_id or parent_message_id

            thread_id = session.discord_thread_id
            if thread_id is None:
                thread_id = await self._discord.find_anchored_thread(parent)
                if thread_id is None:
                    thread_id = await self._discord.create_anchored_thread(
                        parent,
                        _thread_name(event),
                        self._auto_archive_duration,
                    )
                session = self._store.record_work_item_thread(
                    subject, parent, thread_id
                )
            self._discord.mark_thread_participation(thread_id)

            proposal, previous = self._proposal_for(
                event, source_revision=source_revision
            )
            approval_offered, unavailable_reason = self._approval_offer(
                event, source_revision=source_revision
            )
            self._store.create_proposal(proposal)
            message_id = self._store.get_proposal_message_id(
                proposal.proposal_id, proposal.revision
            )
            if message_id is None:
                parts: list[str] = []
                if proposal.revision == 1:
                    parts.append(render_thread_opened(event))
                elif previous is not None:
                    parts.append(render_needs_reapproval(previous))
                parts.append(
                    render_proposal(
                        proposal,
                        self._bot_mention,
                        approval_offered=approval_offered,
                        approval_unavailable_reason=unavailable_reason,
                    )
                )
                content = "\n\n".join(parts)
                message_id = await self._discord.find_message_content(
                    thread_id, content, limit=100
                )
                if message_id is None:
                    message_id = await self._discord.send_to_thread(thread_id, content)
                self._store.record_proposal_message(
                    proposal.proposal_id,
                    proposal.revision,
                    message_id,
                    approval_offered=approval_offered,
                )

            restored = self._store.get_active_work_item_session(subject)
            if restored is None:
                raise RuntimeError("work item session disappeared")
            return restored
