"""Compact, code-owned Korean voice for actionable mention inbox work."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from plugins.mention_inbox.contract import MentionEvent
from plugins.mention_inbox.preflight import (
    PreApprovalDisposition,
    brief_from_metadata,
)
from plugins.mention_inbox.presentation import normalize_review_text
from plugins.mention_inbox.proposals import WorkProposal

_ALLOWED_MENTIONS: dict[str, Any] = {
    "parse": [],
    "users": [],
    "roles": [],
    "replied_user": False,
}
_BOT_MENTION_RE = re.compile(r"<@[0-9]{1,30}>")
_STATUS_LABELS = {
    PreApprovalDisposition.ACTION_REQUIRED: "🔴 수정 요청",
    PreApprovalDisposition.REVIEW_NEEDED: "🟠 리뷰 요청",
    PreApprovalDisposition.POSSIBLY_STALE: "🟡 최신 상태 확인",
    PreApprovalDisposition.INFORMATIONAL: "🟢 확인 완료",
    PreApprovalDisposition.INSUFFICIENT_EVIDENCE: "🟡 추가 확인",
}


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
    return normalize_review_text(value, limit=limit)


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


def _status_label(disposition: PreApprovalDisposition | None) -> str:
    return _STATUS_LABELS.get(disposition, "🟠 Review needed")


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
    brief = brief_from_metadata(metadata.get("preapproval_brief"))
    status = _status_label(None if brief is None else brief.disposition)

    lines = [
        f"{status} · {_subject_label(event)}",
        title,
        f"요약: {_compact_untrusted(brief.summary, 400) if brief is not None else excerpt}",
        f"출처: {_action_phrase(kind, actor)}",
    ]
    if brief is not None and brief.findings:
        lines.append("확인된 작업:")
        for finding in brief.findings[:4]:
            location = _compact_untrusted(finding.path, 140) or "review"
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.append(
                f"- {location} · {_compact_untrusted(finding.body, 260)}"
            )
        if len(brief.findings) > 4:
            lines.append(f"- 외 {len(brief.findings) - 4}개")
    if source_url:
        lines.append(f"원문: {source_url}")
    lines.append(marker)
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[: 1900 - len(marker) - 2].rstrip() + "\n" + marker
    return RenderedDiscordEvent(
        content=content,
        marker=marker,
        allowed_mentions=dict(_ALLOWED_MENTIONS),
    )


def render_thread_update(event: MentionEvent) -> str:
    """Render a non-proposal GitHub update inside an existing work thread."""

    metadata = _metadata(event)
    kind = str(metadata.get("actionable_kind") or event.untrusted.action_detail)
    actor = _compact_untrusted(metadata.get("actor_login"), 80)
    brief = brief_from_metadata(metadata.get("preapproval_brief"))
    summary = (
        _compact_untrusted(brief.summary, 400)
        if brief is not None
        else _compact_untrusted(event.untrusted.body, 400)
    )
    lines = [
        f"{_status_label(None if brief is None else brief.disposition)} · GitHub 업데이트",
        f"요약: {summary}",
        f"출처: {_action_phrase(kind, actor)}",
    ]
    source_url = _trusted_github_url(event.untrusted.source_url)
    if source_url:
        lines.append(f"원문: {source_url}")
    return "\n".join(lines)[:1700]


def render_thread_opened(event: MentionEvent) -> str:
    label = _subject_label(event)
    return (
        f"{label}의 새 리뷰와 코멘트는 이 thread에 모아서 이어갈게요."
    )


def _proposal_lines(
    values: tuple[str, ...], *, limit: int = 4, item_limit: int = 220
) -> list[str]:
    lines = [f"- {_compact_untrusted(value, item_limit)}" for value in values[:limit]]
    if len(values) > limit:
        lines.append(f"- 외 {len(values) - limit}개")
    return lines


def _render_bounded_with_footer(
    lines: list[str], footer: tuple[str, ...], *, limit: int = 1700
) -> str:
    footer_text = "\n".join(footer)
    available = limit - len(footer_text) - 2
    if available <= 1:
        raise ValueError("proposal footer exceeds Discord message budget")
    body = "\n".join(lines)
    if len(body) > available:
        body = body[: available - 1].rstrip() + "…"
    return f"{body}\n\n{footer_text}"


def render_advisory(advisory: str) -> str:
    """Render the model-written advisory as its own message.

    Kept out of the proposal message on purpose: that text must render
    identically for one revision so the crash-recovery lookup in
    ``ensure_thread`` can recognize an already-sent proposal, and model output is
    not reproducible. Returns '' when there is nothing usable to post, which the
    caller treats as "skip the advisory".
    """

    if not isinstance(advisory, str):
        return ""
    lines = [" ".join(line.split()) for line in advisory.split("\n")]
    body = "\n".join(line for line in lines if line).strip()
    if not body:
        return ""
    if len(body) > 1200:
        body = body[:1199].rstrip() + "…"
    return "\n".join(
        (
            "참고 분석 (모델 작성, 권한 없음)",
            body,
            "",
            "이 분석은 설명용이며 위 제안의 허용 범위를 바꾸지 않아요.",
        )
    )


def render_approval_reply_required(bot_mention: str) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    return (
        "승인 문구로 확인했지만 reply 대상이 없어요. "
        f"승인 안내가 표시된 최신 제안 메시지에 답장으로 `{bot_mention} 승인`을 남겨 주세요."
    )


def render_approval_reference_mismatch(bot_mention: str) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    return (
        "답장한 메시지가 승인 가능한 최신 제안 메시지와 일치하지 않아요. "
        f"최신 제안에 답장으로 `{bot_mention} 승인`을 남겨 주세요."
    )


def render_approval_not_offered() -> str:
    return (
        "이 제안은 검토용으로 게시돼 승인할 수 없어요. "
        "실행 가능한 새 제안에 승인 안내가 표시될 때까지 실제 변경은 시작되지 않아요."
    )


def render_approval_not_enabled() -> str:
    return (
        "실행 기능이 현재 꺼져 있어 승인을 처리할 수 없어요. "
        "제안 내용은 검토할 수 있지만 실제 변경은 시작되지 않아요."
    )


def render_approval_unauthorized() -> str:
    return "이 제안을 승인할 권한이 없어요. 실제 변경은 시작되지 않았어요."


def render_agent_tools_unauthorized() -> str:
    return (
        "이 work thread에서 코드나 로컬 작업공간을 확인할 권한이 없어요. "
        "요청을 실행하지 않았습니다."
    )


def render_agent_tools_not_enabled() -> str:
    return (
        "이 work thread의 도구 실행 기능이 현재 연결되지 않았어요. "
        "요청을 실행하지 않았습니다."
    )


def render_revision_instruction(bot_mention: str) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    return (
        "일반 대화는 제안을 바꾸지 않아요. 변경할 내용이 있다면 "
        f"`{bot_mention} 제안 수정: 바꿀 내용`처럼 명시해 주세요."
    )


def render_conversation_fallback(
    proposal: WorkProposal,
    bot_mention: str,
    *,
    brief_summary: str | None = None,
    findings: tuple[object, ...] = (),
    failure_reason: str | None = None,
) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    summary = _compact_untrusted(brief_summary, 400)
    evidence_lines: list[str] = []
    if summary:
        evidence_lines.append(f"- preflight 요약: {summary}")
    for index, finding in enumerate(findings[:4], start=1):
        getter = (
            finding.get
            if isinstance(finding, Mapping)
            else lambda key, default=None: getattr(finding, key, default)
        )
        body = _compact_untrusted(getter("body"), 300)
        path = _compact_untrusted(getter("path"), 300)
        raw_line = getter("line")
        line = raw_line if isinstance(raw_line, int) and not isinstance(raw_line, bool) else None
        if body:
            evidence_lines.append(f"- 실제 코멘트 {index}: {body}")
        if path:
            location = f"{path}:{line}" if line is not None else path
            evidence_lines.append(f"  위치: {location}")

    if failure_reason == "timeout":
        intro = (
            "설명 모델 호출이 제한 시간 안에 완료되지 않아 저장된 preflight "
            "근거를 그대로 알려드릴게요."
        )
    elif failure_reason == "error":
        intro = (
            "설명 모델 호출이 실패해 저장된 preflight 근거를 그대로 "
            "알려드릴게요."
        )
    else:
        intro = (
            "지금은 모델 설명을 생성하지 못해 저장된 preflight 근거를 "
            "그대로 알려드릴게요."
        )

    if evidence_lines:
        lines = [
            intro,
            *evidence_lines,
        ]
    else:
        if failure_reason == "timeout":
            fallback_intro = (
                "설명 모델 호출이 제한 시간 안에 완료되지 않았어요. "
                "저장된 현재 제안은 다음과 같아요."
            )
        elif failure_reason == "error":
            fallback_intro = (
                "설명 모델 호출이 실패했어요. 저장된 현재 제안은 "
                "다음과 같아요."
            )
        else:
            fallback_intro = (
                "지금은 추가 설명을 생성하지 못했어요. 저장된 현재 "
                "제안은 다음과 같아요."
            )
        lines = [
            fallback_intro,
            f"- 확인된 내용: {_compact_untrusted(proposal.goal, 500)}",
        ]
    return _render_bounded_with_footer(
        lines,
        (
            f"현재 제안: revision {proposal.revision} · status {proposal.status.value}",
            "이 답변으로 제안이나 실행 상태는 바뀌지 않았어요.",
        ),
    )


def render_revision_unauthorized() -> str:
    return "이 제안을 수정할 권한이 없어 revision을 바꾸지 않았어요."


def render_proposal(
    proposal: WorkProposal,
    bot_mention: str,
    *,
    approval_offered: bool = False,
    approval_unavailable_reason: str | None = None,
    event: MentionEvent | None = None,
) -> str:
    if _BOT_MENTION_RE.fullmatch(bot_mention) is None:
        raise ValueError("bot_mention must be a trusted Discord user mention")
    if not isinstance(approval_offered, bool):
        raise ValueError("approval_offered must be a boolean")
    if approval_offered and approval_unavailable_reason is not None:
        raise ValueError("approval offer cannot have an unavailable reason")
    if approval_unavailable_reason not in {
        None,
        "execution_unavailable",
        "preflight_not_approvable",
        "approval_unavailable",
    }:
        raise ValueError("invalid approval unavailable reason")

    source_url = (
        ""
        if event is None
        else _trusted_github_url(event.untrusted.source_url)
    )
    brief = (
        None
        if event is None
        else brief_from_metadata(_metadata(event).get("preapproval_brief"))
    )
    scopes: list[str] = []
    if brief is not None:
        for finding in brief.findings[:4]:
            location = _compact_untrusted(finding.path, 140)
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            if location:
                scopes.append(location)
    if not scopes:
        scopes.append("PR diff와 관련 테스트")

    lines = [
        "현재 요청",
        _compact_untrusted(proposal.goal, 500),
        "",
        "제 추천",
        *_proposal_lines(proposal.steps),
        "",
        "영향 범위",
        *[f"- {scope}" for scope in scopes],
    ]
    if source_url:
        lines.extend(("", "원문", source_url))
    if approval_offered:
        footer = (
            "제가 수정하고 테스트한 뒤 이 PR 브랜치에 반영할 수 있어요.",
            "이대로 진행하려면 `리뷰 반영해줘`라고 말해 주세요.",
        )
    elif approval_unavailable_reason == "execution_unavailable":
        footer = (
            "현재 자동 실행이 연결되지 않아 분석 결과만 갱신했습니다.",
        )
    elif approval_unavailable_reason == "preflight_not_approvable":
        footer = (
            "현재 근거만으로 자동 변경하지 않고 추가 확인이 필요합니다.",
        )
    else:
        footer = (
            "현재는 분석 결과만 갱신했습니다.",
        )
    return _render_bounded_with_footer(lines, footer)


def render_queued(proposal: WorkProposal) -> str:
    return (
        "작업 요청을 확인했습니다. 실행 순서를 기다리고 있어요. "
        "아직 시작되지 않았고, 실제로 시작되면 이 thread에 바로 남기겠습니다."
    )


def render_running(proposal: WorkProposal) -> str:
    return "지금 작업을 시작했습니다. 요청된 범위와 검증 방법 안에서만 진행하겠습니다."


def render_verifying(proposal: WorkProposal) -> str:
    return "작업 결과를 검증하고 있어요. 확인 근거가 갖춰지기 전에는 완료로 표시하지 않겠습니다."


def render_kanban_registering(proposal: WorkProposal) -> str:
    return "요청된 범위를 Kanban의 durable queue에 등록하고 있어요. 아직 구현을 시작한 상태는 아닙니다."


def render_kanban_queued(proposal: WorkProposal) -> str:
    return (
        "Kanban 작업으로 등록했습니다. 구현 완료가 아니라 실행 대기 상태예요. "
        "실제 진행과 검증 근거는 이 thread의 후속 상태로 확인하겠습니다."
    )


_BLOCKED_REASONS = {
    "no_tool_activity": "실제 도구 실행 기록이 없어 완료로 인정하지 않았습니다.",
    "verification_missing": "필수 검증·commit·push 근거가 모두 확인되지 않았습니다.",
    "agent_failed": "실행 agent가 정상 완료 상태를 반환하지 않았습니다.",
    "kanban_receipt_missing": "durable Kanban 등록 근거를 확인하지 못했습니다.",
    "dispatch_failed": "요청된 실행 session을 시작하지 못했습니다.",
    "recovery_dispatch_failed": "재시작 후 실행 session 복구에 실패했습니다.",
    "execution_scope_changed": "저장된 workspace 또는 PR branch 범위가 달라졌습니다.",
}


def render_blocked(
    proposal: WorkProposal, *, category: str | None = None
) -> str:
    message = "⛔ 지금은 작업을 시작하지 않았어요."
    if category is None:
        return (
            f"{message}\n\n"
            "안전하게 진행할 근거가 부족해 자동으로 다시 시도하지 않았어요.\n\n"
            "필요한 결정\n이 thread에서 상태를 확인한 뒤 다시 요청해 주세요.\n\n"
            "보호 조치\n기존 파일과 Git 상태는 변경하지 않았어요."
        )
    reason = _BLOCKED_REASONS.get(
        category,
        "안전하게 완료를 입증할 실행 근거가 부족했습니다.",
    )
    safe_category = (
        category
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", category)
        else "execution_blocked"
    )
    return (
        f"{message}\n\n"
        f"이유\n{reason}\n\n"
        "필요한 결정\n문제가 해결된 뒤 이 thread에서 다시 요청해 주세요.\n\n"
        "보호 조치\n기존 파일과 Git 상태는 변경하지 않았어요.\n\n"
        f"||기술 정보: `{safe_category}`||"
    )


def render_execution_enabled_reproposal(proposal: WorkProposal) -> str:
    return (
        "실행 기능이 활성화되어 같은 원본과 PR HEAD에 결속된 실행 범위를 갱신합니다."
    )


def render_needs_reapproval(proposal: WorkProposal) -> str:
    return (
        "원본 내용이나 PR HEAD가 달라졌어요. 이전 실행 요청은 사용하지 않고 "
        "최신 상태를 반영해 작업 범위를 다시 계산하겠습니다."
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
    technical = tuple(
        item
        for item in evidence
        if re.search(r"\b(?:commit|sha|branch|execution)\b", item, re.IGNORECASE)
    )
    verification = tuple(item for item in evidence if item not in technical)
    lines = [
        "✅ 반영했어요",
        "",
        "바꾼 내용",
        summary,
        "",
        "확인한 내용",
        *[f"- {item}" for item in verification or ("검증 근거를 확인했어요.",)],
    ]
    if technical:
        lines.extend(("", "기술 정보", *[f"- {item}" for item in technical]))
    return "\n".join(lines)
