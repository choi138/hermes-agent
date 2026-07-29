"""Pure, fail-closed classification of hydrated GitHub notification candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


class GitHubActionKind(str, Enum):
    DIRECT_MENTION = "direct_mention"
    TEAM_MENTION = "team_mention"
    REVIEW_REQUESTED = "review_requested"
    TEAM_REVIEW_REQUESTED = "team_review_requested"
    ASSIGNED = "assigned"
    OWN_PR_COMMENT = "own_pr_comment"
    OWN_PR_REVIEW_COMMENT = "own_pr_review_comment"
    OWN_PR_REVIEW_SUMMARY = "own_pr_review_summary"
    OWN_PR_CHANGES_REQUESTED = "own_pr_changes_requested"


class SuppressionReason(str, Enum):
    STALE_NOTIFICATION_REASON = "stale_notification_reason"
    SELF_AUTHORED = "self_authored"
    BOT_GENERATED_MENTION = "bot_generated_mention"
    NOT_DIRECT_TARGET = "not_direct_target"
    UNVERIFIED_TEAM_MEMBERSHIP = "unverified_team_membership"
    NON_ACTIONABLE = "non_actionable"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class GitHubHydrationContext:
    """Trusted runtime context assembled only from bounded GitHub GET responses."""

    target_login: str
    target_node_id: str
    subject: Mapping[str, Any]
    latest_event: Mapping[str, Any] | None
    timeline: Sequence[Mapping[str, Any]]
    reviews: Sequence[Mapping[str, Any]]
    review_comments: Sequence[Mapping[str, Any]]
    verified_team_slugs: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.target_login or self.target_login != self.target_login.strip():
            raise ValueError("target_login must be a non-empty trimmed string")
        if (
            not self.target_node_id
            or self.target_node_id != self.target_node_id.strip()
        ):
            raise ValueError("target_node_id must be a non-empty trimmed string")
        object.__setattr__(
            self,
            "verified_team_slugs",
            frozenset(team.casefold() for team in self.verified_team_slugs),
        )


@dataclass(frozen=True)
class ActionableGitHubEvent:
    kind: GitHubActionKind
    source_event_id: str
    source_revision: str
    subject_key: str
    repository: str
    subject_type: str
    number: int
    title: str
    actor_login: str | None
    actor_kind: str
    excerpt: str
    source_url: str
    subject_url: str
    subject_head_sha: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class ActionableDecision:
    event: ActionableGitHubEvent | None
    suppression_reason: SuppressionReason | None

    def __post_init__(self) -> None:
        if (self.event is None) == (self.suppression_reason is None):
            raise ValueError("a decision must contain exactly one result")


_CANDIDATE_REASONS = frozenset({
    "mention",
    "team_mention",
    "review_requested",
    "assign",
    "author",
    "comment",
})
_HUMAN_TYPES = frozenset({"user"})
_BOT_TYPES = frozenset({"bot", "app"})
_ALLOWED_AI_REVIEW_BOT_LOGINS = frozenset({
    "chatgpt-codex-connector[bot]",
    "codex[bot]",
    "coderabbitai[bot]",
    "openai-codex[bot]",
})
_ALLOWED_AI_REVIEW_SUMMARY_STATES = frozenset({
    "approved",
    "changes_requested",
    "commented",
})
_TEAM_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9-])@([A-Za-z0-9-]+)/([A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)
_MAX_EXCERPT = 240


def _suppressed(reason: SuppressionReason) -> ActionableDecision:
    return ActionableDecision(event=None, suppression_reason=reason)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identity_matches(value: Any, *, login: str, node_id: str) -> bool:
    candidate = _mapping(value)
    candidate_login = candidate.get("login")
    candidate_node = candidate.get("node_id")
    return (
        isinstance(candidate_login, str)
        and candidate_login.casefold() == login.casefold()
    ) or (isinstance(candidate_node, str) and candidate_node == node_id)


def _actor(event: Mapping[str, Any] | None) -> tuple[str | None, str, str | None]:
    payload = _mapping(event)
    actor = _mapping(payload.get("user") or payload.get("actor"))
    login = actor.get("login")
    node_id = actor.get("node_id")
    raw_kind = actor.get("type")
    kind = raw_kind.casefold() if isinstance(raw_kind, str) else "unknown"
    return (
        login if isinstance(login, str) and login else None,
        kind,
        node_id if isinstance(node_id, str) and node_id else None,
    )


def _is_self_actor(
    event: Mapping[str, Any] | None, *, target_login: str, target_node_id: str
) -> bool:
    return _identity_matches(
        _mapping(event).get("user") or _mapping(event).get("actor"),
        login=target_login,
        node_id=target_node_id,
    )


def _body(event: Mapping[str, Any] | None) -> str:
    value = _mapping(event).get("body")
    return value if isinstance(value, str) else ""


def _has_direct_mention(text: str, login: str) -> bool:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9-])@{re.escape(login)}(?![A-Za-z0-9-])",
        re.IGNORECASE,
    )
    return pattern.search(text) is not None


def _mentioned_teams(text: str) -> tuple[str, ...]:
    return tuple(
        f"{match.group(1)}/{match.group(2)}".casefold()
        for match in _TEAM_MENTION_RE.finditer(text)
    )


def _requested_team_slugs(subject: Mapping[str, Any]) -> tuple[str, ...]:
    raw = subject.get("requested_teams")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    result: list[str] = []
    for item in raw:
        team = _mapping(item)
        slug = team.get("slug")
        organization = _mapping(team.get("organization"))
        organization_login = organization.get("login")
        if not isinstance(organization_login, str):
            organization_login = team.get("organization_login")
        if isinstance(slug, str) and isinstance(organization_login, str):
            result.append(f"{organization_login}/{slug}".casefold())
    return tuple(result)


def _normalize_revision(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_revision(
    event: Mapping[str, Any] | None,
    notification: Mapping[str, Any],
    subject: Mapping[str, Any],
) -> str | None:
    # Approval revalidation performs a fresh GET of this same subject resource,
    # so its updated_at is the one canonical compare-and-swap revision.
    for payload in (subject, notification, _mapping(event)):
        for key in ("updated_at", "submitted_at", "created_at"):
            revision = _normalize_revision(payload.get(key))
            if revision is not None:
                return revision
    return None


def _source_event_id(
    event: Mapping[str, Any] | None,
    notification: Mapping[str, Any],
    kind: GitHubActionKind,
) -> str | None:
    payload = _mapping(event)
    for key in ("node_id", "id"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    notification_id = notification.get("id")
    if isinstance(notification_id, (str, int)) and str(notification_id).strip():
        return f"notification:{str(notification_id).strip()}:{kind.value}"
    return None


def _excerpt(text: str, *, needle: str | None = None) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_EXCERPT:
        return collapsed
    if needle:
        index = collapsed.casefold().find(needle.casefold())
        if index >= 0:
            start = max(0, index - 60)
            end = min(len(collapsed), start + _MAX_EXCERPT)
            start = max(0, end - _MAX_EXCERPT)
            value = collapsed[start:end]
            if start:
                value = "…" + value[1:]
            if end < len(collapsed):
                value = value[:-1] + "…"
            return value
    return collapsed[: _MAX_EXCERPT - 1] + "…"


def _event_type(event: Mapping[str, Any] | None) -> str:
    payload = _mapping(event)
    value = payload.get("event_type") or payload.get("event")
    return value.casefold() if isinstance(value, str) else ""


def _build_event(
    *,
    kind: GitHubActionKind,
    notification: Mapping[str, Any],
    context: GitHubHydrationContext,
    event: Mapping[str, Any] | None,
    team_slug: str | None = None,
) -> ActionableDecision:
    repository = _mapping(notification.get("repository"))
    notification_subject = _mapping(notification.get("subject"))
    repository_name = repository.get("full_name")
    repository_node_id = repository.get("node_id")
    subject_node_id = context.subject.get("node_id")
    number = context.subject.get("number")
    title = context.subject.get("title") or notification_subject.get("title")
    subject_type = notification_subject.get("type")
    subject_url = notification_subject.get("url")
    source_url = _mapping(event).get("html_url") or context.subject.get("html_url")
    revision = _source_revision(event, notification, context.subject)
    source_event_id = _source_event_id(event, notification, kind)
    required_strings = (
        repository_name,
        repository_node_id,
        subject_node_id,
        title,
        subject_type,
        subject_url,
        source_url,
        revision,
        source_event_id,
    )
    if (
        any(not isinstance(value, str) or not value for value in required_strings)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
    ):
        return _suppressed(SuppressionReason.NON_ACTIONABLE)

    actor_login, actor_kind, _ = _actor(event)
    body = _body(event)
    metadata: dict[str, Any] = {
        "candidate_reason": notification.get("reason"),
    }
    if team_slug is not None:
        metadata["team_slug"] = team_slug
    head = _mapping(context.subject.get("head"))
    head_sha = head.get("sha")
    return ActionableDecision(
        event=ActionableGitHubEvent(
            kind=kind,
            source_event_id=source_event_id,
            source_revision=revision,
            subject_key=f"github:{repository_node_id}:{subject_node_id}",
            repository=repository_name,
            subject_type=subject_type,
            number=number,
            title=title,
            actor_login=actor_login,
            actor_kind=actor_kind,
            excerpt=_excerpt(
                body,
                needle=(
                    f"@{context.target_login}"
                    if kind is GitHubActionKind.DIRECT_MENTION
                    else (f"@{team_slug}" if team_slug else None)
                ),
            ),
            source_url=source_url,
            subject_url=subject_url,
            subject_head_sha=head_sha
            if isinstance(head_sha, str) and head_sha
            else None,
            metadata=metadata,
        ),
        suppression_reason=None,
    )


def classify_actionable(
    notification: Mapping[str, Any], context: GitHubHydrationContext
) -> ActionableDecision:
    """Classify a hydrated candidate; a notification reason is never sufficient."""

    if not isinstance(notification, Mapping):
        return _suppressed(SuppressionReason.NON_ACTIONABLE)
    reason = notification.get("reason")
    if reason not in _CANDIDATE_REASONS:
        return _suppressed(SuppressionReason.NON_ACTIONABLE)
    subject = context.subject
    event = context.latest_event
    body = _body(event)
    actor_login, actor_kind, _ = _actor(event)
    self_authored = _is_self_actor(
        event,
        target_login=context.target_login,
        target_node_id=context.target_node_id,
    )
    subject_author = subject.get("user")
    own_pr = _mapping(notification.get("subject")).get(
        "type"
    ) == "PullRequest" and _identity_matches(
        subject_author,
        login=context.target_login,
        node_id=context.target_node_id,
    )
    event_type = _event_type(event)
    allowlisted_ai_reviewer = (
        actor_kind in _BOT_TYPES
        and actor_login is not None
        and actor_login.casefold() in _ALLOWED_AI_REVIEW_BOT_LOGINS
    )
    allowlisted_ai_review_activity = (
        own_pr
        and allowlisted_ai_reviewer
        and event_type
        in {
            "review",
            "pull_request_review",
            "review_comment",
            "pull_request_review_comment",
        }
    )

    # Exact human direct mention in the actual latest event takes precedence.
    # Allowlisted AI review activity is classified by its narrower own-PR rule.
    if (
        _has_direct_mention(body, context.target_login)
        and not allowlisted_ai_review_activity
    ):
        if actor_kind in _BOT_TYPES:
            return _suppressed(SuppressionReason.BOT_GENERATED_MENTION)
        if self_authored:
            return _suppressed(SuppressionReason.SELF_AUTHORED)
        if actor_kind not in _HUMAN_TYPES:
            return _suppressed(SuppressionReason.NON_ACTIONABLE)
        return _build_event(
            kind=GitHubActionKind.DIRECT_MENTION,
            notification=notification,
            context=context,
            event=event,
        )

    mentioned_teams = _mentioned_teams(body)
    if mentioned_teams and not allowlisted_ai_review_activity:
        verified = next(
            (team for team in mentioned_teams if team in context.verified_team_slugs),
            None,
        )
        if verified is None:
            return _suppressed(SuppressionReason.UNVERIFIED_TEAM_MEMBERSHIP)
        if actor_kind in _BOT_TYPES:
            return _suppressed(SuppressionReason.BOT_GENERATED_MENTION)
        if self_authored:
            return _suppressed(SuppressionReason.SELF_AUTHORED)
        if actor_kind not in _HUMAN_TYPES:
            return _suppressed(SuppressionReason.NON_ACTIONABLE)
        return _build_event(
            kind=GitHubActionKind.TEAM_MENTION,
            notification=notification,
            context=context,
            event=event,
            team_slug=verified,
        )

    if reason == "review_requested":
        requested_reviewers = subject.get("requested_reviewers")
        if isinstance(requested_reviewers, Sequence) and not isinstance(
            requested_reviewers, (str, bytes)
        ):
            if any(
                _identity_matches(
                    item,
                    login=context.target_login,
                    node_id=context.target_node_id,
                )
                for item in requested_reviewers
            ):
                if self_authored:
                    return _suppressed(SuppressionReason.SELF_AUTHORED)
                if actor_kind in _BOT_TYPES:
                    return _suppressed(SuppressionReason.NON_ACTIONABLE)
                return _build_event(
                    kind=GitHubActionKind.REVIEW_REQUESTED,
                    notification=notification,
                    context=context,
                    event=event,
                )

        requested_teams = _requested_team_slugs(subject)
        if requested_teams:
            verified = next(
                (
                    team
                    for team in requested_teams
                    if team in context.verified_team_slugs
                ),
                None,
            )
            if verified is None:
                return _suppressed(SuppressionReason.UNVERIFIED_TEAM_MEMBERSHIP)
            if self_authored:
                return _suppressed(SuppressionReason.SELF_AUTHORED)
            if actor_kind in _BOT_TYPES:
                return _suppressed(SuppressionReason.NON_ACTIONABLE)
            return _build_event(
                kind=GitHubActionKind.TEAM_REVIEW_REQUESTED,
                notification=notification,
                context=context,
                event=event,
                team_slug=verified,
            )

    if reason == "assign":
        assignees = subject.get("assignees")
        if isinstance(assignees, Sequence) and not isinstance(assignees, (str, bytes)):
            if any(
                _identity_matches(
                    item,
                    login=context.target_login,
                    node_id=context.target_node_id,
                )
                for item in assignees
            ):
                if self_authored:
                    return _suppressed(SuppressionReason.SELF_AUTHORED)
                if actor_kind in _BOT_TYPES:
                    return _suppressed(SuppressionReason.NON_ACTIONABLE)
                return _build_event(
                    kind=GitHubActionKind.ASSIGNED,
                    notification=notification,
                    context=context,
                    event=event,
                )

    if own_pr and event is not None:
        if self_authored:
            return _suppressed(SuppressionReason.SELF_AUTHORED)
        state = event.get("state")
        normalized_state = state.casefold() if isinstance(state, str) else ""
        if actor_kind in _BOT_TYPES:
            if not allowlisted_ai_reviewer or not _body(event).strip():
                return _suppressed(SuppressionReason.NON_ACTIONABLE)
            if event_type in {"review_comment", "pull_request_review_comment"}:
                kind = GitHubActionKind.OWN_PR_REVIEW_COMMENT
            elif (
                event_type in {"review", "pull_request_review"}
                and normalized_state in _ALLOWED_AI_REVIEW_SUMMARY_STATES
            ):
                kind = (
                    GitHubActionKind.OWN_PR_CHANGES_REQUESTED
                    if normalized_state == "changes_requested"
                    else GitHubActionKind.OWN_PR_REVIEW_SUMMARY
                )
            else:
                return _suppressed(SuppressionReason.NON_ACTIONABLE)
        else:
            if actor_kind not in _HUMAN_TYPES:
                return _suppressed(SuppressionReason.NON_ACTIONABLE)
            if event_type in {"review", "pull_request_review"}:
                if normalized_state == "changes_requested":
                    kind = GitHubActionKind.OWN_PR_CHANGES_REQUESTED
                else:
                    return _suppressed(SuppressionReason.NON_ACTIONABLE)
            elif event_type in {"review_comment", "pull_request_review_comment"}:
                kind = GitHubActionKind.OWN_PR_REVIEW_COMMENT
            elif event_type in {"issue_comment", "comment"}:
                kind = GitHubActionKind.OWN_PR_COMMENT
            else:
                return _suppressed(SuppressionReason.NON_ACTIONABLE)
        return _build_event(
            kind=kind,
            notification=notification,
            context=context,
            event=event,
        )

    if reason in {"mention", "team_mention"}:
        return _suppressed(SuppressionReason.STALE_NOTIFICATION_REASON)
    if reason in {"review_requested", "assign"}:
        return _suppressed(SuppressionReason.NOT_DIRECT_TARGET)
    if actor_login and actor_login.casefold() == context.target_login.casefold():
        return _suppressed(SuppressionReason.SELF_AUTHORED)
    return _suppressed(SuppressionReason.NON_ACTIONABLE)
