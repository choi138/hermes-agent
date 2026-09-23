"""Pure policy, classification, and formatting for gateway health alerts.

This module intentionally performs no I/O and does not import the gateway or
platform adapters.  It is safe to exercise in small unit tests and safe for a
logging handler to call before the asyncio gateway has started.

The one deliberate exception is a *function-local* import of ``agent.redact``
inside the formatter.  Alert text crosses an external boundary (Discord), so
every textual field is credential-masked and control-character stripped
immediately before rendering.  Keeping the import local preserves this
module's import-time purity.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Iterable, Optional


# Discord's hard message limit is 2000.  Leave a little room for adapter
# formatting while retaining the remediation lines at the front.
_MAX_ALERT_CHARS = 1950

# Upper bound on how much of a single field is handed to the redactor.  The
# rendered alert can never exceed ``_MAX_ALERT_CHARS`` anyway, so anything past
# this bound is unreachable output; capping it keeps a 20k-character log blob
# from turning every alert into a regex workout.
_REDACT_INPUT_LIMIT = 4000


@dataclass(frozen=True)
class HealthEvent:
    """One operator-facing health event.

    ``mention`` is a policy bit, not the mention text itself.  The sink injects
    the configured mention only for A/C events so the pure formatter remains
    reusable across deployments.

    The ``first_*`` / ``route_*`` / ``last_*`` / counter fields are optional
    structured failover context.  They are populated all-or-nothing by
    :func:`classify_log_record` when a retry record carries a complete,
    well-formed failover suffix, and stay empty otherwise so a partial parse
    can never surface half-known routing to an operator.
    """

    rule: str
    category: str
    title: str
    reason: str
    action: str = ""
    session_id: str = ""
    session_key: str = ""
    platform: str = ""
    resource: str = ""
    jump_url: str = ""
    details: tuple[str, ...] = field(default_factory=tuple)
    mention: bool = False
    occurred_at: float = field(default_factory=time.time)
    suppressed_count: int = 0
    first_endpoint: str = ""
    first_reason: str = ""
    route_from: str = ""
    route_to: str = ""
    last_endpoint: str = ""
    last_reason: str = ""
    retry_count: Optional[int] = None
    message_count: Optional[int] = None
    token_estimate: Optional[int] = None

    @property
    def budget_key(self) -> str:
        """Stable per-session key used by the cooldown budget.

        Infrastructure events generally have no session, so they share the
        ``global`` bucket for a rule.  Resource/platform must not widen the
        key: one backend outage can surface through several logical MCP
        servers at once, and the cooldown is intentionally ``rule+session``.
        """
        return self.session_key or self.session_id or "global"


def should_emit_output_silence(
    *,
    silence_seconds: float,
    threshold: float,
    turn_live: bool,
    already_notified: bool,
    waiting_on_user: bool = False,
) -> bool:
    """Return whether A's output-silence warning should fire."""
    return bool(
        turn_live
        and not waiting_on_user
        and threshold > 0
        and silence_seconds >= threshold
        and not already_notified
    )


def should_enforce_turn_deadline(
    *,
    silence_seconds: float,
    deadline: float,
    turn_live: bool,
    already_enforced: bool = False,
    waiting_on_user: bool = False,
) -> bool:
    """Return whether A's hard wall-clock output deadline should fire."""
    return bool(
        turn_live
        and not waiting_on_user
        and deadline > 0
        and silence_seconds >= deadline
        and not already_enforced
    )


_LOG_PREFIX_PATTERN = r"(?:\[[^\]\n]{1,64}\]\s*)*"
_STREAM_STALE_RE = re.compile(
    r"^"
    + _LOG_PREFIX_PATTERN
    + r"Stream stale for (?P<seconds>\d+(?:\.\d+)?)s\b"
    r"[^\n]*?no chunks received",
    re.IGNORECASE,
)
_API_RETRIES_RE = re.compile(
    r"API call failed after (?P<retries>\d+) retries\.",
    re.IGNORECASE,
)
_GRAPHITI_NAME_RE = re.compile(
    r"MCP server ['\"](?P<name>[^'\"]*graphiti[^'\"]*)['\"]",
    re.IGNORECASE,
)
_MCP_PARKED_RE = re.compile(
    r"\bparking\b"
    r"|state:\s*[a-z]+\s*(?:->|=>|→|➜)\s*parked",
    re.IGNORECASE,
)
_MCP_RECOVERED_RE = re.compile(
    r"state:\s*parked\s*(?:->|=>|→|➜)\s*connected",
    re.IGNORECASE,
)
_MCP_REVIVAL_RE = re.compile(r"\breviv(?:e|ed|es|ing|al|als)\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Structured failover suffix
# ---------------------------------------------------------------------------
#
# Emitted alongside the legacy prefix as:
#
#   first=provider/model:reason route=provider/model -> provider/model
#   last=provider/model:reason retries=<int> msgs=<int> tokens~=<int>
#
# Every atom is length-bounded and the whole group is required.  A partial or
# malformed suffix must not produce structured fields: the alert then degrades
# to the legacy rendering, which is still correct, just less specific.
_ENDPOINT_PATTERN = (
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}/[A-Za-z0-9][A-Za-z0-9._+-]{0,63}"
)
_REASON_PATTERN = r"[A-Za-z][A-Za-z0-9_.-]{0,47}"
_ARROW_PATTERN = r"(?:->|=>|\u2192|\u279c)"
_COUNT_PATTERN = r"\d{1,9}"
_TOKENS_PATTERN = r"\d{1,3}(?:,\d{3})+|\d{1,12}"

_FAILOVER_RE = re.compile(
    r"first=(?P<first_endpoint>" + _ENDPOINT_PATTERN + r")"
    r":(?P<first_reason>" + _REASON_PATTERN + r")"
    r"\s+route=(?P<route_from>" + _ENDPOINT_PATTERN + r")"
    r"\s*" + _ARROW_PATTERN + r"\s*"
    r"(?P<route_to>" + _ENDPOINT_PATTERN + r")"
    r"\s+last=(?P<last_endpoint>" + _ENDPOINT_PATTERN + r")"
    r":(?P<last_reason>" + _REASON_PATTERN + r")"
    r"\s+retries=(?P<retries>" + _COUNT_PATTERN + r")"
    r"\s+msgs=(?P<msgs>" + _COUNT_PATTERN + r")"
    r"\s+tokens~=(?P<tokens>" + _TOKENS_PATTERN + r")"
)

_ROUTE_ARROW = "\u2192"


def _parse_failover_fields(message: str) -> dict:
    """Return bounded structured failover fields, or ``{}`` when absent.

    All-or-nothing on purpose.  Operators triage by "where did it start" versus
    "where did it end"; a half-parsed chain answers neither question and would
    be worse than the legacy blob it replaces.
    """
    try:
        match = _FAILOVER_RE.search(message)
    except (TypeError, re.error):
        return {}
    if not match:
        return {}
    try:
        retries = int(match.group("retries"))
        messages = int(match.group("msgs"))
        tokens = int(match.group("tokens").replace(",", ""))
    except (AttributeError, TypeError, ValueError):
        return {}
    return {
        "first_endpoint": match.group("first_endpoint"),
        "first_reason": match.group("first_reason"),
        "route_from": match.group("route_from"),
        "route_to": match.group("route_to"),
        "last_endpoint": match.group("last_endpoint"),
        "last_reason": match.group("last_reason"),
        "retry_count": retries,
        "message_count": messages,
        "token_estimate": tokens,
    }


def classify_log_record(logger_name: str, message: str) -> Optional[HealthEvent]:
    """Promote only explicitly allowlisted logger-and-message pairs.

    This must stay a positive allowlist.  In particular, broad WARNING/ERROR
    forwarding would promote routine tool and cwd warnings into Discord.
    """
    logger_name = str(logger_name or "")
    message = str(message or "")

    if logger_name == "agent.chat_completion_helpers":
        match = _STREAM_STALE_RE.search(message)
        if match:
            return HealthEvent(
                rule="C3.stream_stale",
                category="C",
                title="업스트림 스트림 정체",
                reason=(
                    f"{match.group('seconds')}초 동안 모델 스트림에서 "
                    "실제 청크가 도착하지 않았습니다."
                ),
                action="10분 안에 3회 누적되면 운영자에게 장애로 알립니다.",
                resource="upstream-model",
                details=(message[:1200],),
                mention=True,
            )
        return None

    if logger_name == "agent.conversation_loop":
        match = _API_RETRIES_RE.search(message)
        if match:
            return HealthEvent(
                rule="B.api_retries_exhausted",
                category="B",
                title="에이전트 API 재시도 소진",
                reason=f"모델 API 호출이 {match.group('retries')}회 재시도 후 종결되었습니다.",
                action="원 스레드에는 안전한 오류 문구가 표시됩니다. 내부 원인은 아래 로그를 확인하세요.",
                resource="upstream-model",
                details=(message[:1800],),
                mention=False,
                **_parse_failover_fields(message),
            )
        return None

    if logger_name == "tools.mcp_tool":
        graphiti = _GRAPHITI_NAME_RE.search(message)
        if not graphiti:
            return None
        resource = graphiti.group("name")
        # A concrete parked -> connected transition is stronger evidence than
        # the generic revival wording used in surrounding log text.  Classify
        # it first so messages such as "revived ... (state: parked → connected)"
        # are reported as recovery, while revival chatter without a transition
        # remains suppressed below.
        if _MCP_RECOVERED_RE.search(message):
            return HealthEvent(
                rule="C4.graphiti_recovered",
                category="C",
                title="Graphiti 메모리 백엔드 복구",
                reason=f"MCP 서버 {resource}가 parked 상태에서 connected로 복구했습니다.",
                action="추가 조치가 필요하지 않은지 최근 메모리 호출을 확인하세요.",
                resource=resource,
                details=(message[:1800],),
                mention=True,
            )
        if _MCP_REVIVAL_RE.search(message):
            return None
        if _MCP_PARKED_RE.search(message):
            return HealthEvent(
                rule="C4.graphiti_parked",
                category="C",
                title="Graphiti 메모리 백엔드 parked",
                reason=f"MCP 서버 {resource} 연결이 parked 상태로 전이했습니다.",
                action="컨테이너와 MCP 세션 상태를 확인하세요. 복구 전이도 별도로 알립니다.",
                resource=resource,
                details=(message[:1800],),
                mention=True,
            )
        return None

    return None


def _format_timestamp(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return "unknown"


# C0/C1 controls plus the Unicode line/paragraph separators, zero-width marks
# and bidi overrides.  Any of these would let a hostile log record forge or
# reorder alert lines once the text lands in a chat client.
_CONTROL_CHARS_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)
_MULTI_SPACE_RE = re.compile(r" {2,}")


def _strip_control(text: str) -> str:
    """Flatten one field to a single, injection-safe line."""
    if not text:
        return ""
    return _MULTI_SPACE_RE.sub(" ", _CONTROL_CHARS_RE.sub(" ", text))


# Userinfo runs to the *last* "@" in the authority, so "@" is deliberately
# allowed inside the capture: a malformed-but-logged
# "scheme://user:pa@ss@host/path" must not leak the tail of the password.
# Excluding "/", "?" and "#" keeps paths and query parameters untouched.
_URL_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31}://)(?P<userinfo>[^\s/?#]{1,256})@"
)
# Only a snowflake target is a real mention.  Accepting anything else let
# crafted text such as "<@1](url)>" be rewritten into a Discord masked link.
_DISCORD_MENTION_RE = re.compile(r"<(?P<sigil>@[!&]?|#)(?P<target>[0-9]{1,32})>")
_DISCORD_BARE_MENTION_RE = re.compile(r"@(?P<scope>everyone|here)\b", re.IGNORECASE)
_SQUARE_BRACKET_TRANSLATION = str.maketrans({"[": "(", "]": ")"})


def _mask_url_credentials(text: str) -> str:
    """Mask URL userinfo without modifying path or query parameters."""
    if "://" not in text:
        return text

    def _mask(match: "re.Match[str]") -> str:
        userinfo = match.group("userinfo")
        masked = "***:***" if ":" in userinfo else "***"
        return f"{match.group('scheme')}{masked}@"

    return _URL_CREDENTIALS_RE.sub(_mask, text)


def _neutralize_markup(text: str) -> str:
    """Render event-derived mentions, masked links and code spans inert."""
    if not text:
        return ""

    def _mention(match: "re.Match[str]") -> str:
        label = "channel" if match.group("sigil") == "#" else "mention"
        return f"[{label}:{match.group('target')}]"

    text = _DISCORD_MENTION_RE.sub(_mention, text)
    text = _DISCORD_BARE_MENTION_RE.sub(lambda m: f"[{m.group('scope')}]", text)
    # A label inserted above can still be trailed by an attacker-supplied "(",
    # re-forming the masked-link syntax that _safe_text already took apart.
    text = text.replace("](", "] (")
    return text.replace("`", "'")


def _redact(text: str) -> str:
    """Mask credentials, failing closed if the redactor is unavailable.

    An unmaskable field is dropped entirely rather than emitted: this text is
    about to leave the process, and a partially masked bearer token is a leak.
    """
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        masked = redact_sensitive_text(text, force=True)
    except Exception:
        return "[redacted]"
    if masked is None:
        return "[redacted]"
    try:
        return str(masked)
    except Exception:
        return "[redacted]"


# Redaction runs over a wider window than we emit, so a credential that
# straddles the emitted width is still matched as one whole pattern.  The
# window keeps the work bounded; the emitted width is applied afterwards.
_REDACT_SCAN_MULTIPLIER = 4


def _safe_text(value: object) -> str:
    """Control-strip, credential-mask and de-fang one outbound field."""
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return "[redacted]"
    if not text:
        return ""
    text = _strip_control(text)[: _REDACT_INPUT_LIMIT * _REDACT_SCAN_MULTIPLIER]
    if not text.strip():
        return ""
    # Discord renders "[label](url)" as a masked link, which would let a
    # hostile record disguise an attacker URL as remediation advice.  Flatten
    # the event's own brackets before the redactor and the labels add theirs.
    text = text.translate(_SQUARE_BRACKET_TRANSLATION)
    text = _mask_url_credentials(text)
    text = _strip_control(_redact(text))
    return _neutralize_markup(text)[:_REDACT_INPUT_LIMIT].strip()


def _counter(value: object) -> Optional[int]:
    """Return a renderable integer counter, or ``None`` for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _structured_lines(event: HealthEvent) -> list[str]:
    """Render the failover chain as separately labelled operator lines.

    Original cause, transition route and final cause are deliberately three
    distinct lines: fusing them reads fine but cannot answer "where did the
    chain start" and "where did it end" at a glance.
    """
    lines: list[str] = []

    first_endpoint = _safe_text(getattr(event, "first_endpoint", ""))
    first_reason = _safe_text(getattr(event, "first_reason", ""))
    if first_endpoint:
        detail = f" ({first_reason})" if first_reason else ""
        lines.append(f"최초 원인: `{first_endpoint}`{detail}")

    route_from = _safe_text(getattr(event, "route_from", ""))
    route_to = _safe_text(getattr(event, "route_to", ""))
    if route_from and route_to:
        lines.append(f"전환 경로: `{route_from}` {_ROUTE_ARROW} `{route_to}`")

    last_endpoint = _safe_text(getattr(event, "last_endpoint", ""))
    last_reason = _safe_text(getattr(event, "last_reason", ""))
    if last_endpoint:
        detail = f" ({last_reason})" if last_reason else ""
        lines.append(f"최종 원인: `{last_endpoint}`{detail}")

    retries = _counter(getattr(event, "retry_count", None))
    if retries is not None:
        lines.append(f"재시도 횟수: {retries}회")

    messages = _counter(getattr(event, "message_count", None))
    if messages is not None:
        lines.append(f"메시지 수: {messages}개")

    tokens = _counter(getattr(event, "token_estimate", None))
    if tokens is not None:
        lines.append(f"누적 토큰(추정): 약 {tokens:,}")

    return lines


def format_health_alert(event: HealthEvent, *, mention_text: str = "") -> str:
    """Format a compact Discord alert with routing and remediation context.

    Every event-derived field is redacted and control-stripped here, because
    this function is the last hop before the text leaves the process.
    """
    lines: list[str] = []

    # The mention is operator-supplied deployment config rather than
    # agent-derived text, so it is control-stripped but not passed through the
    # redactor: a role snowflake is not a secret and must survive verbatim.
    mention = _strip_control(str(mention_text or "")).strip()
    if event.mention and mention:
        lines.append(mention)

    lines.append(f"🚨 [{_safe_text(event.category)}] {_safe_text(event.title)}")
    lines.append(f"사유: {_safe_text(event.reason)}")

    # Structured failover context sits ahead of the raw log blob so it survives
    # the length budget below, which trims from the tail.
    lines.extend(_structured_lines(event))

    session_id = _safe_text(event.session_id)
    session_key = _safe_text(event.session_key)
    if session_id:
        lines.append(f"세션 ID: `{session_id}`")
    elif session_key:
        lines.append(f"세션: `{session_key}`")

    platform = _safe_text(event.platform)
    if platform:
        lines.append(f"플랫폼: `{platform}`")

    resource = _safe_text(event.resource)
    if resource:
        lines.append(f"대상: `{resource}`")

    jump_url = _safe_text(event.jump_url)
    if jump_url:
        lines.append(f"원 스레드: {jump_url}")

    action = _safe_text(event.action)
    if action:
        lines.append(f"조치: {action}")

    for detail in event.details:
        detail_text = _safe_text(detail)
        if detail_text:
            lines.append(f"상세: {detail_text}")

    suppressed = _counter(event.suppressed_count)
    if suppressed:
        lines.append(f"(외 {suppressed}건 억제)")

    lines.append(f"시각(UTC): {_format_timestamp(event.occurred_at)}")
    return "\n".join(lines)[:_MAX_ALERT_CHARS]


class AlertBudget:
    """Cooldown plus a sliding global hourly cap.

    Suppressed alerts are counted.  The next admitted alert carries the count
    so rate limiting remains visible without flooding the channel.
    """

    def __init__(self, cooldown_seconds: float = 900, hourly_cap: int = 12):
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.hourly_cap = max(0, int(hourly_cap))
        self._last_by_rule_session: dict[tuple[str, str], float] = {}
        self._hourly_emissions: Deque[float] = deque()
        self._suppressed = 0

    @property
    def suppressed_count(self) -> int:
        return self._suppressed

    def admit(
        self, event: HealthEvent, *, now: Optional[float] = None
    ) -> Optional[HealthEvent]:
        clock = float(time.time() if now is None else now)
        while self._hourly_emissions and clock - self._hourly_emissions[0] >= 3600:
            self._hourly_emissions.popleft()

        key = (event.rule, event.budget_key)
        last = self._last_by_rule_session.get(key)
        in_cooldown = (
            last is not None
            and self.cooldown_seconds > 0
            and clock - last < self.cooldown_seconds
        )
        over_cap = self.hourly_cap > 0 and len(self._hourly_emissions) >= self.hourly_cap
        if in_cooldown or over_cap:
            self._suppressed += 1
            return None

        suppressed = self._suppressed
        self._suppressed = 0
        self._last_by_rule_session[key] = clock
        self._hourly_emissions.append(clock)
        if suppressed:
            return dataclasses.replace(
                event,
                suppressed_count=event.suppressed_count + suppressed,
            )
        return event


class UpstreamFailureTracker:
    """Collapse upstream samples into one C3 alert at N events per window."""

    def __init__(self, threshold: int = 3, window_seconds: float = 600):
        self.threshold = max(1, int(threshold))
        self.window_seconds = max(1.0, float(window_seconds))
        self._events: Deque[tuple[float, HealthEvent]] = deque()

    def record(
        self, event: HealthEvent, *, now: Optional[float] = None
    ) -> Optional[HealthEvent]:
        clock = float(time.time() if now is None else now)
        while self._events and clock - self._events[0][0] > self.window_seconds:
            self._events.popleft()
        self._events.append((clock, event))
        if len(self._events) < self.threshold:
            return None

        samples = list(self._events)
        self._events.clear()
        rules = ", ".join(sample.rule for _, sample in samples)
        details: list[str] = []
        for _, sample in samples:
            details.extend(sample.details[:1])
        return HealthEvent(
            rule="C3.upstream_failure_streak",
            category="C",
            title="업스트림 모델 장애 임계 초과",
            reason=(
                f"{self.window_seconds / 60:g}분 안에 업스트림 실패가 "
                f"{len(samples)}회 연속 관측되었습니다."
            ),
            action="모델 라우터, 공급자 상태, 네트워크와 최근 배포를 확인하세요.",
            resource="upstream-model",
            details=(f"규칙: {rules}", *tuple(details[-3:])),
            mention=True,
            occurred_at=clock,
        )


def discord_jump_url(
    *, guild_id: str = "", chat_id: str = "", thread_id: str = "", message_id: str = ""
) -> str:
    """Build a Discord jump URL from a SessionSource-like set of ids."""
    channel_id = str(thread_id or chat_id or "").strip()
    if not channel_id:
        return ""
    scope = str(guild_id or "@me").strip()
    url = f"https://discord.com/channels/{scope}/{channel_id}"
    if message_id:
        url += f"/{str(message_id).strip()}"
    return url
