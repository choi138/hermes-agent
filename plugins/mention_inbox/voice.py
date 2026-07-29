"""Compact, code-owned Korean voice for actionable mention inbox work."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from plugins.mention_inbox.contract import MentionEvent
from plugins.mention_inbox.proposals import WorkProposal

_ALLOWED_MENTIONS: dict[str, Any] = {
    "parse": [],
    "users": [],
    "roles": [],
    "replied_user": False,
}
_BOT_MENTION_RE = re.compile(r"<@[0-9]{1,30}>")


@dataclass(frozen=True)
class VladilenaInboxVoice:
    name: str = "Vladilena"
    language: str = "ko"
    style: str = "calm_concise_actionable"
    distinguishes_queued_from_running: bool = True
    completion_requires_evidence: bool = True


VLADILENA_INBOX_VOICE = VladilenaInboxVoice()


@dataclass(frozen=True)
class RenderedDiscordEvent:
    content: str
    marker: str
    allowed_mentions: dict[str, Any]


@dataclass(frozen=True)
class CompletionReceipt:
    summary: str
    evidence: tuple[str, ...]
    verified: bool


def _compact_untrusted(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = (
        text
        .replace("@", "@\u200b")
        .replace("<", "‹")
        .replace(">", "›")
        .replace("`", "ˋ")
        .replace("*", "∗")
        .replace("_", "‗")
    )
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _trusted_github_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    return value


def _delivery_marker(
    event: MentionEvent, revision_number: int, destination: str
) -> str:
    material = f"{event.dedupe_key}\0{revision_number}\0{destination}".encode("utf-8")
    return f"[hermes-inbox:{hashlib.sha256(material).hexdigest()[:24]}]"


def _metadata(event: MentionEvent) -> Mapping[str, object]:
    value = event.untrusted.metadata
    return value if isinstance(value, Mapping) else {}


def _subject_label(event: MentionEvent) -> str:
    metadata = _metadata(event)
    repository = _compact_untrusted(metadata.get("repository"), 100) or "GitHub"
    subject_type = str(metadata.get("subject_type") or "")
    number = metadata.get("subject_number")
    if subject_type == "PullRequest" and isinstance(number, int):
        return f"{repository} PR #{number}"
    if subject_type == "Issue" and isinstance(number, int):
        return f"{repository} issue #{number}"
    return repository


def _action_phrase(kind: str, actor: str) -> str:
    actor_name = f"{actor}님이 " if actor else ""
    phrases = {
        "direct_mention": "직접 확인을 요청했어요.",
        "team_mention": "소속 team에 확인을 요청했어요.",
        "review_requested": "review를 요청했어요.",
        "direct_review_requested": "review를 요청했어요.",
        "team_review_requested": "소속 team에 review를 요청했어요.",
        "assigned": "담당자로 지정했어요.",
        "direct_assigned": "담당자로 지정했어요.",
        "own_pr_comment": "작성한 PR에 의견을 남겼어요.",
        "own_pr_review_comment": "작성한 PR에 review 의견을 남겼어요.",
        "own_pr_review_summary": "작성한 PR에 review 요약을 남겼어요.",
        "own_pr_changes_requested": "작성한 PR에 변경을 요청했어요.",
    }
    return actor_name + phrases.get(kind, "확인이 필요한 요청을 남겼어요.")


def render_action_alert(
    event: MentionEvent, *, revision_number: int, destination: str
) -> RenderedDiscordEvent:
    metadata = _metadata(event)
    kind = str(metadata.get("actionable_kind") or event.untrusted.action_detail)
    actor = _compact_untrusted(metadata.get("actor_login"), 80)
    title = _compact_untrusted(event.untrusted.title, 140)
    excerpt = _compact_untrusted(event.untrusted.body, 240)
    source_url = _trusted_github_url(event.untrusted.source_url)
    marker = _delivery_marker(event, revision_number, destination)

    lines = [
        "GitHub에서 확인이 필요한 일이 왔어요.",
        f"대상: {_subject_label(event)} · {title}",
        f"요청: {_action_phrase(kind, actor)}",
    ]
    if excerpt:
        lines.append(f"맥락: {excerpt}")
    if source_url:
        lines.append(f"바로가기: {source_url}")
    lines.extend((
        "아직 변경 작업은 시작하지 않았어요. 이어서 확인할 thread를 열게요.",
        marker,
    ))
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[: 1900 - len(marker) - 2].rstrip() + "\n" + marker
    return RenderedDiscordEvent(
        content=content,
        marker=marker,
        allowed_mentions=dict(_ALLOWED_MENTIONS),
    )


def render_thread_opened(event: MentionEvent) -> str:
    label = _subject_label(event)
    return (
        f"{label} 이야기는 이 thread에서 이어갈게요. "
        "아직 변경 작업은 시작하지 않았어요. 먼저 확인 범위와 검증 방법을 제안하겠습니다."
    )


def _proposal_lines(values: tuple[str, ...], *, limit: int = 5) -> list[str]:
    return [f"- {_compact_untrusted(value, 300)}" for value in values[:limit]]


def render_proposal(proposal: WorkProposal, bot_mention: str) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    lines = [
        "이렇게 진행하면 좋겠습니다.",
        f"이번 제안은 {proposal.revision}번째 검토안입니다.",
        f"목표: {_compact_untrusted(proposal.goal, 500)}",
        "진행 순서:",
        *_proposal_lines(proposal.steps),
        "가능한 작업:",
        *_proposal_lines(proposal.allowed_actions),
        "하지 않을 작업:",
        *_proposal_lines(proposal.forbidden_actions),
        "확인 방법:",
        *_proposal_lines(proposal.verification),
        "",
        f"진행해도 된다면 이 메시지에 답장으로 `{bot_mention} 승인`이라고 남겨 주세요.",
        "그 전에는 읽기와 제안만 하고 실제 변경은 시작하지 않습니다.",
    ]
    return "\n".join(lines)


def render_queued(proposal: WorkProposal) -> str:
    return (
        "승인을 확인했습니다. 실행 순서를 기다리고 있어요. "
        "아직 시작되지 않았고, 실제로 시작되면 이 thread에 바로 남기겠습니다."
    )


def render_running(proposal: WorkProposal) -> str:
    return "지금 작업을 시작했습니다. 승인된 범위와 검증 방법 안에서만 진행하겠습니다."


def render_verifying(proposal: WorkProposal) -> str:
    return "작업 결과를 검증하고 있어요. 확인 근거가 갖춰지기 전에는 완료로 표시하지 않겠습니다."


def render_kanban_registering(proposal: WorkProposal) -> str:
    return "승인된 범위를 Kanban의 durable queue에 등록하고 있어요. 아직 구현을 시작한 상태는 아닙니다."


def render_kanban_queued(proposal: WorkProposal) -> str:
    return (
        "Kanban 작업으로 등록했습니다. 구현 완료가 아니라 실행 대기 상태예요. "
        "실제 진행과 검증 근거는 이 thread의 후속 상태로 확인하겠습니다."
    )


def render_blocked(proposal: WorkProposal) -> str:
    return (
        "승인 기록은 보존했지만 작업을 시작하지 못했어요. "
        "범위를 넓히거나 자동으로 다시 시도하지 않고, 이 thread에서 상태를 확인하겠습니다."
    )


def render_needs_reapproval(proposal: WorkProposal) -> str:
    return (
        "제안 뒤에 원본 내용이나 PR HEAD가 달라졌어요. 이전 승인은 사용하지 않고, "
        "최신 상태를 반영한 제안을 다시 올리겠습니다."
    )


def render_completed(receipt: CompletionReceipt) -> str:
    if not isinstance(receipt, CompletionReceipt) or not receipt.verified:
        raise ValueError("completed voice requires a verified receipt")
    if not receipt.evidence:
        raise ValueError("completed voice requires evidence")
    summary = _compact_untrusted(receipt.summary, 500)
    evidence = tuple(_compact_untrusted(item, 400) for item in receipt.evidence[:8])
    if not summary or any(not item for item in evidence):
        raise ValueError("completed voice requires non-empty evidence")
    return "\n".join((
        f"완료했습니다. {summary}",
        "확인 근거:",
        *[f"- {item}" for item in evidence],
    ))
