"""Bounded, deterministic pre-approval analysis for hydrated GitHub events.

All source text in this module remains untrusted data.  The brief is deliberately
pure and tool-free so it can be rendered before mutation approval without
opening an agent execution path.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from plugins.mention_inbox.actionable import GitHubActionKind, GitHubHydrationContext

_MAX_SUMMARY_CHARS = 400
_MAX_FINDING_CHARS = 300
_MAX_FINDINGS = 10
_MAX_TEXT_BUDGET = 1600
_MAX_PATH_CHARS = 300
_MAX_URL_CHARS = 500
_MAX_ID_CHARS = 160
_MAX_REVISION_CHARS = 80
_MAX_SHA_CHARS = 128


class PreApprovalDisposition(str, Enum):
    ACTION_REQUIRED = "action_required"
    REVIEW_NEEDED = "review_needed"
    POSSIBLY_STALE = "possibly_stale"
    INFORMATIONAL = "informational"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class ReviewFinding:
    source_event_id: str
    body: str
    source_url: str
    path: str | None
    line: int | None
    review_id: str | None
    commit_id: str | None

    def __post_init__(self) -> None:
        if not _valid_text(self.source_event_id, _MAX_ID_CHARS):
            raise ValueError("finding source_event_id is invalid")
        if not _valid_text(self.body, _MAX_FINDING_CHARS):
            raise ValueError("finding body is invalid")
        if self.source_url and not _trusted_github_url(self.source_url):
            raise ValueError("finding source_url is invalid")
        if self.path is not None and not _valid_text(self.path, _MAX_PATH_CHARS):
            raise ValueError("finding path is invalid")
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int) or self.line <= 0
        ):
            raise ValueError("finding line is invalid")
        if self.review_id is not None and not _valid_text(
            self.review_id, _MAX_ID_CHARS
        ):
            raise ValueError("finding review_id is invalid")
        if self.commit_id is not None and not _valid_text(
            self.commit_id, _MAX_SHA_CHARS
        ):
            raise ValueError("finding commit_id is invalid")


@dataclass(frozen=True)
class PreApprovalBrief:
    disposition: PreApprovalDisposition
    summary: str
    findings: tuple[ReviewFinding, ...]
    source_revision: str
    head_sha: str | None
    approvable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PreApprovalDisposition):
            raise ValueError("brief disposition is invalid")
        if not _valid_text(self.summary, _MAX_SUMMARY_CHARS):
            raise ValueError("brief summary is invalid")
        if not isinstance(self.findings, tuple) or len(self.findings) > _MAX_FINDINGS:
            raise ValueError("brief findings are invalid")
        if any(not isinstance(item, ReviewFinding) for item in self.findings):
            raise ValueError("brief finding is invalid")
        if len(self.summary) + sum(len(item.body) for item in self.findings) > _MAX_TEXT_BUDGET:
            raise ValueError("brief text budget exceeded")
        if not _valid_text(self.source_revision, _MAX_REVISION_CHARS):
            raise ValueError("brief source_revision is invalid")
        if self.head_sha is not None and not _valid_text(self.head_sha, _MAX_SHA_CHARS):
            raise ValueError("brief head_sha is invalid")
        expected_approvable = self.disposition in {
            PreApprovalDisposition.ACTION_REQUIRED,
            PreApprovalDisposition.REVIEW_NEEDED,
        }
        if self.approvable is not expected_approvable:
            raise ValueError("brief approvable flag contradicts disposition")


def _valid_text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= limit
    )


def _compact(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    printable = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    text = " ".join(printable.split())
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _identifier(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    candidate = str(value).strip()
    if not candidate or len(candidate) > _MAX_ID_CHARS:
        return None
    return candidate


def _source_event_id(payload: Mapping[str, Any]) -> str | None:
    return _identifier(payload.get("node_id")) or _identifier(payload.get("id"))


def _actor_login(payload: Mapping[str, Any]) -> str | None:
    actor = _mapping(payload.get("user") or payload.get("actor"))
    return _compact(actor.get("login"), 100) or None


def _trusted_github_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > _MAX_URL_CHARS:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return value


def _positive_line(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _state(payload: Mapping[str, Any]) -> str:
    value = payload.get("state")
    return value.casefold() if isinstance(value, str) else ""


def _commit(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("commit_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= _MAX_SHA_CHARS else None


def _is_outdated(payload: Mapping[str, Any], *, head_sha: str | None) -> bool:
    commit_id = _commit(payload)
    if head_sha is not None and commit_id is not None and commit_id != head_sha:
        return True
    # GitHub nulls the current line while retaining original_line when an inline
    # comment no longer maps to the current diff.
    if (
        payload.get("line") is None
        and _positive_line(payload.get("original_line")) is not None
    ):
        return True
    return False


def _finding_from_comment(
    payload: Mapping[str, Any], *, remaining: int
) -> ReviewFinding | None:
    source_event_id = _source_event_id(payload)
    body = _compact(payload.get("body"), min(_MAX_FINDING_CHARS, remaining))
    path = _compact(payload.get("path"), _MAX_PATH_CHARS) or None
    line = _positive_line(payload.get("line"))
    review_id = _identifier(payload.get("pull_request_review_id"))
    commit_id = _commit(payload)
    if source_event_id is None or not body:
        return None
    if payload.get("path") is not None and path is None:
        return None
    raw_line = payload.get("line")
    if raw_line is not None and line is None:
        return None
    return ReviewFinding(
        source_event_id=source_event_id,
        body=body,
        source_url=_trusted_github_url(payload.get("html_url")),
        path=path,
        line=line,
        review_id=review_id,
        commit_id=commit_id,
    )


def _generic_summary(kind: GitHubActionKind) -> str:
    return {
        GitHubActionKind.REVIEW_REQUESTED: "현재 PR에 review 요청이 왔어요.",
        GitHubActionKind.TEAM_REVIEW_REQUESTED: "현재 PR에 team review 요청이 왔어요.",
        GitHubActionKind.ASSIGNED: "현재 항목의 담당자로 지정됐어요.",
    }.get(kind, "")


def build_preapproval_brief(
    *,
    kind: GitHubActionKind,
    source_event: Mapping[str, Any] | None,
    context: GitHubHydrationContext,
    source_revision: str,
    head_sha: str | None,
) -> PreApprovalBrief:
    """Build a bounded brief from already hydrated GET-only GitHub data."""

    if not isinstance(kind, GitHubActionKind) or not isinstance(
        context, GitHubHydrationContext
    ):
        raise ValueError("invalid pre-approval brief input")
    revision = _compact(source_revision, _MAX_REVISION_CHARS)
    normalized_head = _compact(head_sha, _MAX_SHA_CHARS) or None
    source = _mapping(source_event)
    source_id = _source_event_id(source)
    source_actor = _actor_login(source)
    review_id = _identifier(source.get("id"))
    summary = _compact(source.get("body"), _MAX_SUMMARY_CHARS)
    findings: list[ReviewFinding] = []
    stale = _is_outdated(source, head_sha=normalized_head)

    if kind is GitHubActionKind.OWN_PR_REVIEW_COMMENT:
        finding = _finding_from_comment(
            source, remaining=max(1, _MAX_TEXT_BUDGET - len(summary))
        )
        if finding is not None:
            findings.append(finding)
            summary = summary or finding.body
    elif kind in {
        GitHubActionKind.OWN_PR_REVIEW_SUMMARY,
        GitHubActionKind.OWN_PR_CHANGES_REQUESTED,
    }:
        if source_id is None or review_id is None:
            return _insufficient(revision, normalized_head)
        remaining = _MAX_TEXT_BUDGET - len(summary)
        for raw_comment in _sequence(context.review_comments):
            if len(findings) >= _MAX_FINDINGS or remaining <= 0:
                break
            comment = _mapping(raw_comment)
            if _identifier(comment.get("pull_request_review_id")) != review_id:
                continue
            comment_actor = _actor_login(comment)
            if source_actor and (
                comment_actor is None
                or comment_actor.casefold() != source_actor.casefold()
            ):
                continue
            finding = _finding_from_comment(comment, remaining=remaining)
            if finding is None:
                continue
            findings.append(finding)
            remaining -= len(finding.body)
            stale = stale or _is_outdated(comment, head_sha=normalized_head)
    else:
        # A concrete comment/mention is itself the evidence.  Explicit review
        # request and assignment events can be described without a body.
        if source_id is not None and summary:
            finding = _finding_from_comment(
                source, remaining=max(1, _MAX_TEXT_BUDGET - len(summary))
            )
            if finding is not None:
                findings.append(finding)
        summary = summary or _generic_summary(kind)

    state = _state(source)
    if not revision or not summary:
        return _insufficient(revision or "unknown", normalized_head)
    if stale:
        disposition = PreApprovalDisposition.POSSIBLY_STALE
    elif state == "approved" and not findings:
        disposition = PreApprovalDisposition.INFORMATIONAL
    elif kind is GitHubActionKind.OWN_PR_CHANGES_REQUESTED or state == "changes_requested":
        disposition = PreApprovalDisposition.ACTION_REQUIRED
    else:
        disposition = PreApprovalDisposition.REVIEW_NEEDED
    return PreApprovalBrief(
        disposition=disposition,
        summary=summary,
        findings=tuple(findings),
        source_revision=revision,
        head_sha=normalized_head,
        approvable=disposition
        in {
            PreApprovalDisposition.ACTION_REQUIRED,
            PreApprovalDisposition.REVIEW_NEEDED,
        },
    )


def _insufficient(source_revision: str, head_sha: str | None) -> PreApprovalBrief:
    revision = _compact(source_revision, _MAX_REVISION_CHARS) or "unknown"
    return PreApprovalBrief(
        disposition=PreApprovalDisposition.INSUFFICIENT_EVIDENCE,
        summary="GitHub 원문과 현재 상태를 안전하게 결속하지 못했어요.",
        findings=(),
        source_revision=revision,
        head_sha=head_sha,
        approvable=False,
    )


def brief_to_metadata(brief: PreApprovalBrief) -> dict[str, Any]:
    if not isinstance(brief, PreApprovalBrief):
        raise ValueError("brief must be a PreApprovalBrief")
    return {
        "schema_version": 1,
        "disposition": brief.disposition.value,
        "summary": brief.summary,
        "findings": [
            {
                "source_event_id": finding.source_event_id,
                "body": finding.body,
                "source_url": finding.source_url,
                "path": finding.path,
                "line": finding.line,
                "review_id": finding.review_id,
                "commit_id": finding.commit_id,
            }
            for finding in brief.findings
        ],
        "source_revision": brief.source_revision,
        "head_sha": brief.head_sha,
        "approvable": brief.approvable,
    }


def brief_from_metadata(value: object) -> PreApprovalBrief | None:
    payload = _mapping(value)
    if payload.get("schema_version") != 1:
        return None
    try:
        disposition = PreApprovalDisposition(payload.get("disposition"))
    except (TypeError, ValueError):
        return None
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > _MAX_FINDINGS:
        return None
    findings: list[ReviewFinding] = []
    try:
        for raw in raw_findings:
            item = _mapping(raw)
            if not item:
                return None
            findings.append(
                ReviewFinding(
                    source_event_id=item.get("source_event_id"),
                    body=item.get("body"),
                    source_url=item.get("source_url"),
                    path=item.get("path"),
                    line=item.get("line"),
                    review_id=item.get("review_id"),
                    commit_id=item.get("commit_id"),
                )
            )
        approvable = payload.get("approvable")
        if not isinstance(approvable, bool):
            return None
        head_sha = payload.get("head_sha")
        if head_sha is not None and not isinstance(head_sha, str):
            return None
        return PreApprovalBrief(
            disposition=disposition,
            summary=payload.get("summary"),
            findings=tuple(findings),
            source_revision=payload.get("source_revision"),
            head_sha=head_sha,
            approvable=approvable,
        )
    except (TypeError, ValueError):
        return None
