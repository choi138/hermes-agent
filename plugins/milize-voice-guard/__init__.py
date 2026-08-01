"""Milize voice guard for the default Discord profile.

The plugin adds a compact per-turn Korean voice contract and, only when a
long-form response is overwhelmingly formal, performs one bounded style-only
repair with the host-owned LLM. Repair is fail-open: protected factual anchors
and high lexical similarity must survive, otherwise the original response is
returned unchanged.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
import logging
import re
import threading
import time
from typing import Any, Callable, Mapping

PLUGIN_ID = "milize-voice-guard"
_DEFAULT_PROFILE = "default"
_DISCORD_PLATFORM = "discord"

logger = logging.getLogger("plugins.milize_voice_guard")

VOICE_CONTRACT = """<milize_voice_contract>
사용자에게 보이는 한국어 답변은 따뜻한 해요체를 기본으로 해요.
평범한 설명을 `-습니다/-입니다` 보고체로 연속 작성하지 마세요.
공식 보고서·긴급 경고·정확한 인용을 사용자가 요구한 경우에만 격식체를 제한적으로 허용해요.
사실, 숫자, URL, 코드, 표, 인용문, 식별자는 말투를 위해 바꾸지 마세요.
</milize_voice_contract>"""

_FORMAL_ENDING_RE = re.compile(
    r"(?:아닙니다|바랍니다|드립니다|겠습니다|입니다|합니다|됩니다|십시오|습니다)"
    r"(?=\s*(?:[.!?…]|$))"
)
_HAEYO_ENDING_RE = re.compile(
    r"(?:아니에요|이에요|예요|해요|돼요|드려요|바라요|겠어요|"
    r"있어요|없어요|같아요|맞아요|아요|어요|여요|네요|군요|죠|"
    r"까요|게요|세요|나요|데요|고요)"
    r"(?=\s*(?:[.!?…]|$))"
)
_KOREAN_RE = re.compile(r"[가-힣]")
_FORMAL_INTENT_RE = re.compile(
    r"(?:공식\s*(?:보고서|문서|공지|발표)|보고서\s*(?:형식|체)|격식체|문어체|"
    r"하십시오체|합니다체|습니다체|안전\s*경고|경고문|긴급\s*(?:공지|명령)|"
    r"법률\s*고지|formal\s+(?:report|notice|warning)|incident\s+report)",
    re.IGNORECASE,
)
_FORMAL_NEGATION_RE = re.compile(
    r"(?:격식체|문어체|하십시오체|합니다체|습니다체|보고서체).{0,10}"
    r"(?:말고|아니|않|빼고)",
    re.IGNORECASE,
)
_FENCED_CODE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_URL_RE = re.compile(r"https?://[^\s<>()]+")
_TABLE_LINE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")
_QUOTED_SPAN_RE = re.compile(
    r'"[^"\n]+"|“[^”\n]+”|‘[^’\n]+’|「[^」\n]+」|『[^』\n]+』'
)
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?(?:%|[A-Za-z]{1,4})?"
)
_IDENTIFIER_RE = re.compile(
    r"\b(?:t_[a-z0-9]+|[A-Z][A-Z0-9._/^-]{1,20}|[0-9a-f]{7,40})\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_MARKDOWN_FENCE_WRAPPER_RE = re.compile(
    r"^```(?:markdown|md|text)?\s*\n(?P<body>.*)\n```\s*$",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^(?P<level>#{1,6})\s+")
_MARKDOWN_LIST_RE = re.compile(
    r"(?m)^(?P<indent>\s*)(?P<marker>[-*+]|\d+[.)])\s+"
)
_SAFE_SUFFIX_RULES = (
    ("아니었습니다", "아니었어요"),
    ("없었습니다", "없었어요"),
    ("있었습니다", "있었어요"),
    ("않았습니다", "않았어요"),
    ("하였습니다", "했어요"),
    ("되었습니다", "됐어요"),
    ("하겠습니다", "하겠어요"),
    ("했습니다", "했어요"),
    ("보입니다", "보여요"),
    ("줄입니다", "줄여요"),
    ("높입니다", "높여요"),
    ("붙입니다", "붙여요"),
    ("쓰입니다", "쓰여요"),
    ("모입니다", "모여요"),
    ("아닙니다", "아니에요"),
    ("있습니다", "있어요"),
    ("없습니다", "없어요"),
    ("맞습니다", "맞아요"),
    ("같습니다", "같아요"),
    ("됩니다", "돼요"),
    ("드립니다", "드려요"),
    ("바랍니다", "바라요"),
    ("합니다", "해요"),
    ("겠습니다", "겠어요"),
)
_REPAIR_PROTECTED_PATTERNS = (
    _FENCED_CODE_RE,
    _INLINE_CODE_RE,
    _URL_RE,
    _TABLE_LINE_RE,
    _BLOCKQUOTE_LINE_RE,
    _QUOTED_SPAN_RE,
)
_MARKDOWN_CLOSERS = frozenset("*_~`])}")


@dataclass(frozen=True)
class StyleMetrics:
    sentence_count: int
    formal_count: int
    haeyo_count: int
    formal_share: float
    max_formal_run: int
    should_repair: bool


def _mask_non_prose(text: str) -> str:
    """Remove content whose register must not affect voice detection."""

    masked = text.replace("\r\n", "\n")
    for pattern in (
        _FENCED_CODE_RE,
        _INLINE_CODE_RE,
        _URL_RE,
        _TABLE_LINE_RE,
        _BLOCKQUOTE_LINE_RE,
        _QUOTED_SPAN_RE,
    ):
        masked = pattern.sub(" ", masked)
    return masked


def analyze_style(
    text: str,
    *,
    min_sentences: int = 5,
    formal_share_threshold: float = 0.70,
    max_formal_run: int = 3,
) -> StyleMetrics:
    """Return Korean sentence-ending metrics and the repair decision."""

    prose = _mask_non_prose(text or "")
    sentences = [
        part.strip()
        for part in _SENTENCE_SPLIT_RE.split(prose)
        if _KOREAN_RE.search(part or "")
    ]

    endings: list[tuple[int, str]] = []
    endings.extend((match.start(), "formal") for match in _FORMAL_ENDING_RE.finditer(prose))
    endings.extend((match.start(), "haeyo") for match in _HAEYO_ENDING_RE.finditer(prose))
    endings.sort(key=lambda item: item[0])

    formal_count = sum(1 for _, kind in endings if kind == "formal")
    haeyo_count = sum(1 for _, kind in endings if kind == "haeyo")
    recognized = formal_count + haeyo_count
    formal_share = formal_count / recognized if recognized else 0.0

    run = 0
    longest_run = 0
    for _, kind in endings:
        if kind == "formal":
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 0

    sentence_count = max(len(sentences), recognized)
    should_repair = (
        sentence_count >= max(1, int(min_sentences))
        and formal_count >= 3
        and (
            formal_share >= float(formal_share_threshold)
            or longest_run >= max(1, int(max_formal_run))
        )
    )
    return StyleMetrics(
        sentence_count=sentence_count,
        formal_count=formal_count,
        haeyo_count=haeyo_count,
        formal_share=formal_share,
        max_formal_run=longest_run,
        should_repair=should_repair,
    )


def explicit_formal_intent(user_message: str) -> bool:
    """True only when the user explicitly requests a formal register."""

    text = user_message or ""
    if _FORMAL_NEGATION_RE.search(text):
        return False
    return bool(_FORMAL_INTENT_RE.search(text))


def _protected_anchors(text: str) -> Counter[tuple[str, str]]:
    anchors: Counter[tuple[str, str]] = Counter()
    patterns = (
        ("fenced_code", _FENCED_CODE_RE),
        ("inline_code", _INLINE_CODE_RE),
        ("url", _URL_RE),
        ("table", _TABLE_LINE_RE),
        ("blockquote", _BLOCKQUOTE_LINE_RE),
        ("quote", _QUOTED_SPAN_RE),
        ("number", _NUMBER_RE),
        ("identifier", _IDENTIFIER_RE),
    )
    for kind, pattern in patterns:
        anchors.update((kind, match.group(0)) for match in pattern.finditer(text or ""))
    return anchors


def _style_normalized(text: str) -> str:
    normalized = _FORMAL_ENDING_RE.sub("<ending>", text or "")
    normalized = _HAEYO_ENDING_RE.sub("<ending>", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def _markdown_structure(text: str) -> tuple[tuple[str, str], ...]:
    """Return ordered Markdown markers whose structure may not be restyled."""

    markers: list[tuple[int, str, str]] = []
    markers.extend(
        (match.start(), "heading", match.group("level"))
        for match in _MARKDOWN_HEADING_RE.finditer(text or "")
    )
    markers.extend(
        (
            match.start(),
            "list",
            f"{match.group('indent')}{match.group('marker')}",
        )
        for match in _MARKDOWN_LIST_RE.finditer(text or "")
    )
    markers.sort(key=lambda item: item[0])
    return tuple((kind, marker) for _, kind, marker in markers)


def _unwrap_markdown_fence(candidate: str) -> str:
    raw = candidate or ""
    match = _MARKDOWN_FENCE_WRAPPER_RE.fullmatch(raw.strip())
    return match.group("body") if match else raw


def _repair_protected_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [
        (match.start(), match.end())
        for pattern in _REPAIR_PROTECTED_PATTERNS
        for match in pattern.finditer(text or "")
    ]
    ranges.sort()
    return ranges


def _range_is_protected(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(range_start < end and start < range_end for range_start, range_end in ranges)


def _preceding_visible_char(text: str, index: int) -> str:
    index -= 1
    while index >= 0 and (text[index].isspace() or text[index] in _MARKDOWN_CLOSERS):
        index -= 1
    return text[index] if index >= 0 else ""


def _copula_haeyo(text: str, index: int) -> str:
    previous = _preceding_visible_char(text, index)
    codepoint = ord(previous) - 0xAC00 if previous else -1
    if 0 <= codepoint <= 0xD7A3 - 0xAC00:
        return "이에요" if codepoint % 28 else "예요"
    if previous.isdigit():
        return "이에요"
    return "예요"


def deterministic_repair(text: str) -> str:
    """Convert only allowlisted formal suffixes outside protected Markdown.

    Unknown morphology is deliberately left untouched so the bounded LLM
    fallback (and its invariant validation) can handle it or fail open.
    """

    original = text or ""
    protected = _repair_protected_ranges(original)
    replacements: list[tuple[int, int, str]] = []
    for match in _FORMAL_ENDING_RE.finditer(original):
        if _range_is_protected(match.start(), match.end(), protected):
            continue
        replacement: tuple[int, int, str] | None = None
        prefix = original[: match.end()]
        for source, target in _SAFE_SUFFIX_RULES:
            if prefix.endswith(source):
                replacement = (match.end() - len(source), match.end(), target)
                break
        if replacement is None and match.group(0) == "입니다":
            replacement = (
                match.start(),
                match.end(),
                _copula_haeyo(original, match.start()),
            )
        if replacement is not None:
            replacements.append(replacement)

    candidate = original
    for start, end, target in reversed(replacements):
        candidate = candidate[:start] + target + candidate[end:]
    return candidate


def validate_rewrite(
    original: str,
    candidate: str,
    *,
    semantic_similarity_min: float = 0.82,
    min_length_ratio: float = 0.75,
    max_length_ratio: float = 1.30,
) -> bool:
    """Validate a style-only candidate without trusting the repair model."""

    original = original or ""
    candidate = _unwrap_markdown_fence(candidate)
    if not original or not candidate or original == candidate:
        return False
    if _protected_anchors(original) != _protected_anchors(candidate):
        return False
    if _markdown_structure(original) != _markdown_structure(candidate):
        return False

    length_ratio = len(candidate) / max(1, len(original))
    if not float(min_length_ratio) <= length_ratio <= float(max_length_ratio):
        return False

    similarity = SequenceMatcher(
        None,
        _style_normalized(original),
        _style_normalized(candidate),
        autojunk=False,
    ).ratio()
    if similarity < float(semantic_similarity_min):
        return False

    original_metrics = analyze_style(original)
    candidate_metrics = analyze_style(candidate)
    if candidate_metrics.should_repair:
        return False
    if original_metrics.formal_count >= 3 and candidate_metrics.haeyo_count < 2:
        return False
    return True


def _entry_config() -> Mapping[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        plugins = config.get("plugins") or {}
        entries = plugins.get("entries") or {}
        entry = entries.get(PLUGIN_ID) or {}
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


class VoiceGuard:
    """Stateful hook handler scoped to one plugin-manager process."""

    def __init__(
        self,
        *,
        llm: Any,
        profile_name: str,
        config_loader: Callable[[], Mapping[str, Any]] = _entry_config,
    ) -> None:
        self._llm = llm
        self._profile_name = profile_name
        self._config_loader = config_loader
        self._intent_by_session: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def _config(self) -> dict[str, Any]:
        raw = dict(self._config_loader() or {})
        return {
            "enabled": _bool(raw.get("enabled"), True),
            "repair_enabled": _bool(raw.get("repair_enabled"), True),
            "min_sentences": _bounded_int(raw.get("min_sentences"), 5, 3, 50),
            "formal_share_threshold": _bounded_float(
                raw.get("formal_share_threshold"), 0.70, 0.50, 1.0
            ),
            "max_formal_run": _bounded_int(raw.get("max_formal_run"), 3, 2, 20),
            "semantic_similarity_min": _bounded_float(
                raw.get("semantic_similarity_min"), 0.82, 0.50, 1.0
            ),
            "min_length_ratio": _bounded_float(
                raw.get("min_length_ratio"), 0.75, 0.50, 1.0
            ),
            "max_length_ratio": _bounded_float(
                raw.get("max_length_ratio"), 1.30, 1.0, 2.0
            ),
            "max_tokens": _bounded_int(raw.get("max_tokens"), 8192, 512, 16384),
            "timeout_seconds": _bounded_float(
                raw.get("timeout_seconds"), 60.0, 5.0, 180.0
            ),
            "intent_ttl_seconds": _bounded_float(
                raw.get("intent_ttl_seconds"), 3600.0, 60.0, 86400.0
            ),
        }

    def _in_scope(self, platform: str, config: Mapping[str, Any]) -> bool:
        return (
            self._profile_name == _DEFAULT_PROFILE
            and (platform or "").lower() == _DISCORD_PLATFORM
            and bool(config.get("enabled", True))
        )

    def _remember_intent(self, session_id: str, formal: bool, ttl: float) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [
                key for key, (_, recorded) in self._intent_by_session.items()
                if now - recorded > ttl
            ]
            for key in stale:
                self._intent_by_session.pop(key, None)
            if len(self._intent_by_session) >= 1024:
                oldest = min(self._intent_by_session, key=lambda key: self._intent_by_session[key][1])
                self._intent_by_session.pop(oldest, None)
            self._intent_by_session[session_id or ""] = (formal, now)

    def _pop_intent(self, session_id: str) -> bool:
        with self._lock:
            value = self._intent_by_session.pop(session_id or "", None)
        return bool(value and value[0])

    def pre_llm(
        self,
        *,
        session_id: str = "",
        user_message: Any = "",
        original_user_message: Any = None,
        platform: str = "",
        **_: Any,
    ) -> str | None:
        config = self._config()
        if not self._in_scope(platform, config):
            self._pop_intent(session_id)
            return None
        source_message = (
            original_user_message
            if isinstance(original_user_message, str)
            else user_message
        )
        text = source_message if isinstance(source_message, str) else ""
        self._remember_intent(
            session_id,
            explicit_formal_intent(text),
            float(config["intent_ttl_seconds"]),
        )
        return VOICE_CONTRACT

    def transform(
        self,
        *,
        response_text: str = "",
        session_id: str = "",
        platform: str = "",
        **_: Any,
    ) -> str | None:
        config = self._config()
        formal_intent = self._pop_intent(session_id)
        if not self._in_scope(platform, config) or formal_intent:
            return None

        metrics = analyze_style(
            response_text,
            min_sentences=int(config["min_sentences"]),
            formal_share_threshold=float(config["formal_share_threshold"]),
            max_formal_run=int(config["max_formal_run"]),
        )
        if not metrics.should_repair or not bool(config["repair_enabled"]):
            return None

        deterministic = deterministic_repair(response_text)
        if deterministic != response_text and validate_rewrite(
            response_text,
            deterministic,
            semantic_similarity_min=float(config["semantic_similarity_min"]),
            min_length_ratio=float(config["min_length_ratio"]),
            max_length_ratio=float(config["max_length_ratio"]),
        ):
            logger.info(
                "voice guard applied deterministic repair sentences=%d formal=%d",
                metrics.sentence_count,
                metrics.formal_count,
            )
            return deterministic

        logger.info(
            "voice guard repairing long response sentences=%d formal=%d haeyo=%d run=%d",
            metrics.sentence_count,
            metrics.formal_count,
            metrics.haeyo_count,
            metrics.max_formal_run,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "당신은 한국어 문장 교정자예요. 아래 답변의 내용, 순서, Markdown 구조, "
                    "숫자, URL, 코드, 표, 인용문, 식별자를 그대로 유지하고 종결 어미와 "
                    "그에 꼭 필요한 조사만 따뜻한 해요체로 다듬으세요. 새로운 정보나 "
                    "설명, 머리말, 맺음말을 추가하지 마세요. 전체 답변만 출력하세요."
                ),
            },
            {"role": "user", "content": response_text},
        ]
        try:
            result = self._llm.complete(
                messages,
                temperature=0.0,
                max_tokens=int(config["max_tokens"]),
                timeout=float(config["timeout_seconds"]),
                purpose="milize_voice_style_repair",
            )
            candidate = result if isinstance(result, str) else getattr(result, "text", "")
            candidate = _unwrap_markdown_fence(candidate)
        except Exception as exc:
            logger.warning("voice guard repair call failed: %s", exc)
            return None

        if not validate_rewrite(
            response_text,
            candidate,
            semantic_similarity_min=float(config["semantic_similarity_min"]),
            min_length_ratio=float(config["min_length_ratio"]),
            max_length_ratio=float(config["max_length_ratio"]),
        ):
            logger.warning("voice guard rejected repair candidate by invariant checks")
            return None
        return candidate

    def on_session_end(self, *, session_id: str = "", **_: Any) -> None:
        self._pop_intent(session_id)


def register(ctx: Any) -> None:
    guard = VoiceGuard(llm=ctx.llm, profile_name=ctx.profile_name)
    ctx.register_hook("pre_llm_call", guard.pre_llm)
    ctx.register_hook("transform_llm_output", guard.transform)
    ctx.register_hook("on_session_end", guard.on_session_end)
