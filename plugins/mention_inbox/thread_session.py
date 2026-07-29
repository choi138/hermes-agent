"""Durable Discord thread and no-tools proposal bootstrap for work items."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol

from plugins.mention_inbox.contract import MentionEvent
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


def _proposal_content(event: MentionEvent) -> dict[str, object]:
    metadata = _metadata(event)
    kind = str(metadata.get("actionable_kind") or event.untrusted.action_detail)
    repository = _bounded(metadata.get("repository") or "repository", 100)
    title = _bounded(event.untrusted.title, 140)
    goals = {
        "review_requested": "요청된 review 범위를 확인하고 필요한 검토 결과를 준비한다.",
        "direct_review_requested": "요청된 review 범위를 확인하고 필요한 검토 결과를 준비한다.",
        "team_review_requested": "team review 요청 범위를 확인하고 필요한 검토 결과를 준비한다.",
        "assigned": "할당된 항목의 범위를 확인하고 필요한 변경안을 준비한다.",
        "direct_assigned": "할당된 항목의 범위를 확인하고 필요한 변경안을 준비한다.",
        "own_pr_changes_requested": "요청된 변경 사항을 확인하고 범위 내 수정안을 준비한다.",
    }
    goal = goals.get(kind, "요청된 GitHub 항목을 확인하고 범위 내 대응안을 준비한다.")
    return {
        "goal": f"{repository}의 ‘{title}’에서 {goal}",
        "steps": (
            "원본 event와 최신 repository 상태를 읽기 전용으로 확인한다.",
            "요청 범위와 변경 필요성을 정리한다.",
            "승인된 범위 안에서만 수정하고 대상 테스트로 검증한다.",
        ),
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
            "변경 diff와 승인 범위 대조",
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
    ) -> None:
        if executor_hint not in {"direct", "kanban"}:
            raise ValueError("executor_hint must be direct or kanban")
        if auto_archive_duration not in {60, 1440, 4320, 10080}:
            raise ValueError("invalid Discord auto archive duration")
        self._store = store
        self._discord = discord
        self._bot_mention = bot_mention
        self._executor_hint = executor_hint
        self._auto_archive_duration = auto_archive_duration
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, subject_key: str) -> asyncio.Lock:
        lock = self._locks.get(subject_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[subject_key] = lock
        return lock

    def _proposal_for(
        self, event: MentionEvent, *, source_revision: str
    ) -> tuple[WorkProposal, WorkProposal | None]:
        subject = _subject_key(event)
        head_sha = _head_sha(event)
        latest = self._store.get_latest_proposal(subject)
        content = _proposal_content(event)
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
                parts.append(render_proposal(proposal, self._bot_mention))
                content = "\n\n".join(parts)
                message_id = await self._discord.find_message_content(
                    thread_id, content, limit=100
                )
                if message_id is None:
                    message_id = await self._discord.send_to_thread(thread_id, content)
                self._store.record_proposal_message(
                    proposal.proposal_id, proposal.revision, message_id
                )

            restored = self._store.get_active_work_item_session(subject)
            if restored is None:
                raise RuntimeError("work item session disappeared")
            return restored
