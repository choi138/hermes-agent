"""Contract tests for structured failover rendering in ``gateway.agent_health``.

The production module does not exist in this working tree yet, so every test
imports it *dynamically inside the test body*.  That keeps collection green and
turns the missing implementation into a precise, readable failure instead of a
collection error that hides the rest of the contract.

What is pinned here:

* the already-approved allowlist surface — ``classify_log_record`` promotes a
  plain ``agent.conversation_loop`` record carrying the legacy
  ``API call failed after N retries.`` prefix as category ``B`` /
  ``B.api_retries_exhausted`` / ``mention=False``, and promotes nothing else;
* the new structured-failover contract — a record that carries the same legacy
  prefix *plus* bounded ``first=/route=/last=/retries=/msgs=/tokens~=`` fields
  must render an operator-readable original cause, final cause, route and
  counters, rather than pasting one opaque blob back into the alert;
* the safety envelope — Discord's length budget, credential redaction, the
  deliberately silent category-B mention policy, and fail-safe degradation to
  the legacy rendering when the structured fields are malformed or partial.

Only ``TestSyntheticCredentialFixture`` is expected to pass today: it exercises
``agent.redact`` alone and proves the synthetic credential used by the
redaction contract really is maskable, so that contract is testing the health
module rather than an unredactable fixture.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


MODULE_NAME = "gateway.agent_health"

RETRY_LOGGER = "agent.conversation_loop"

# Exactly the shape emitted today by agent/conversation_loop.py:
#   "%sAPI call failed after %s retries. %s | provider=%s model=%s
#    msgs=%s tokens=~%s"
LEGACY_RECORD = (
    "[hermes-canary] API call failed after 3 retries. Read timed out. "
    "| provider=custom model=gpt-5.6-sol msgs=23 tokens=~64,432"
)

# The structured record keeps the legacy prefix — so the existing allowlist
# still fires unchanged — and appends bounded failover fields.  The two model
# names are the real load-balancer alias pair from agent/failover_domain.py.
STRUCTURED_FIELDS = (
    "first=custom/gpt-5.6-sol:server_error "
    "route=custom/gpt-5.6-sol -> codex-lb/gpt-5.5 "
    "last=codex-lb/gpt-5.5:timeout "
    "retries=3 msgs=23 tokens~=64432"
)
STRUCTURED_RECORD = (
    "[hermes-canary] API call failed after 3 retries. upstream chain "
    f"exhausted | {STRUCTURED_FIELDS}"
)

FIRST_MODEL = "custom/gpt-5.6-sol"
FIRST_CAUSE = "server_error"
LAST_MODEL = "codex-lb/gpt-5.5"
LAST_CAUSE = "timeout"

# The Korean rendering is pinned semantically, not by punctuation: an
# implementation may pick any of these labels and any arrow glyph.  What it may
# NOT do is collapse the original cause, the final cause and the route into one
# undifferentiated line — operators triage by "where did it start" vs "where
# did it end", and a single fused line cannot answer either question.
FIRST_CAUSE_LABELS = ("최초", "처음", "1차", "first")
FINAL_CAUSE_LABELS = ("최종", "마지막", "last")
ROUTE_LABELS = ("경로", "라우트", "폴백", "전환", "route", "failover")
ARROWS = ("->", "→", "=>", "➜")

RETRY_LABELS = ("재시도", "retries", "retry")
MESSAGE_COUNT_LABELS = ("메시지", "메세지", "msgs", "messages")
TOKEN_LABELS = ("토큰", "token")

# A clearly nonfunctional synthetic credential.  It matches the ``sk-`` vendor
# prefix shape that agent.redact masks, but the body spells out that it is not
# a key, so an accidental leak into a log is self-describing.
SYNTHETIC_TOKEN = "sk-hermes-synthetic-not-a-real-key-000000000000"
SYNTHETIC_TOKEN_BODY = "synthetic-not-a-real-key"
SYNTHETIC_CREDENTIAL_URL = (
    f"http://hermes-canary:{SYNTHETIC_TOKEN}@127.0.0.1:2455/v1"
)

# Placeholder role mention — all zeroes is not a routable Discord snowflake.
MENTION_TEXT = "<@&000000000000000000>"

# Discord's hard message limit is 2000; the alert formatter reserves headroom
# for platform-side decoration.
MAX_ALERT_CHARS = 1950


def _load_module() -> ModuleType:
    """Import the production module, RED-ing precisely when it is absent."""
    try:
        return importlib.import_module(MODULE_NAME)
    except Exception as exc:
        pytest.fail(
            f"{MODULE_NAME} must exist and be importable, but it raised "
            f"{type(exc).__name__}: {exc}. The structured-failover health "
            "contract asserted by this file is unimplemented."
        )


def _classify(message: str, logger_name: str = RETRY_LOGGER):
    return _load_module().classify_log_record(logger_name, message)


def _require_event(message: str, logger_name: str = RETRY_LOGGER):
    event = _classify(message, logger_name)
    assert event is not None, (
        f"record from {logger_name} must stay on the health allowlist: "
        f"{message!r}"
    )
    return event


def _render(event, *, mention_text: str = "") -> str:
    return _load_module().format_health_alert(event, mention_text=mention_text)


def _informative_lines(rendered: str) -> list[str]:
    """Rendered lines with any verbatim ``first=…`` field dump removed.

    Dropping the raw dump is what makes "renders the causes" a real
    requirement.  An implementation that only pastes the original log line back
    into the alert has nothing left to satisfy the assertions below, which is
    the point: an opaque blob is not a rendering.
    """
    return [
        line
        for line in rendered.splitlines()
        if line.strip() and "first=" not in line
    ]


def _index_of_line_with(lines, labels, *required) -> int:
    """Index of the first line carrying one of *labels* and every *required*."""
    for index, line in enumerate(lines):
        if any(label in line for label in labels) and all(
            token in line for token in required
        ):
            return index
    return -1


def _index_of_route_line(lines) -> int:
    """Index of a route line: a route label, an arrow, and both endpoints."""
    for index, line in enumerate(lines):
        if (
            any(label in line for label in ROUTE_LABELS)
            and any(arrow in line for arrow in ARROWS)
            and FIRST_MODEL in line
            and LAST_MODEL in line
        ):
            return index
    return -1


class TestLegacyAllowlistIsPreserved:
    """The pre-existing simple record must classify exactly as it does today."""

    def test_simple_retry_record_still_classifies_as_category_b(self):
        event = _require_event(LEGACY_RECORD)

        assert event.category == "B"
        assert event.rule == "B.api_retries_exhausted"
        assert event.mention is False
        assert "3" in event.reason


class TestPositiveAllowlist:
    """Promotion stays a positive allowlist, not broad WARNING/ERROR forwarding."""

    UNRELATED_RECORDS = [
        (RETRY_LOGGER, "Tool call failed: read_file raised OSError"),
        (RETRY_LOGGER, "WARNING: context window is nearly full, compacting"),
        ("agent.chat_completion_helpers", "ERROR: unexpected payload shape"),
        ("tools.file_tools", "ERROR: cwd no longer exists, falling back"),
        ("gateway.run", "WARNING: platform bridge reconnect scheduled"),
        # Right message, wrong logger — the allowlist is keyed on both.
        ("root", LEGACY_RECORD),
    ]

    @pytest.mark.parametrize("logger_name,message", UNRELATED_RECORDS)
    def test_unrelated_records_stay_unclassified(self, logger_name, message):
        assert _classify(message, logger_name) is None, (
            f"{logger_name} / {message!r} must not be promoted into the "
            "health channel"
        )


class TestStructuredFailoverRendering:
    """Structured fields must be parsed and rendered, not dumped."""

    def test_structured_record_stays_on_the_allowlist(self):
        event = _require_event(STRUCTURED_RECORD)

        assert event.category == "B"
        assert event.rule == "B.api_retries_exhausted"
        assert event.mention is False

    def test_structured_record_renders_first_last_and_route_separately(self):
        lines = _informative_lines(_render(_require_event(STRUCTURED_RECORD)))

        first_index = _index_of_line_with(
            lines, FIRST_CAUSE_LABELS, FIRST_MODEL, FIRST_CAUSE
        )
        last_index = _index_of_line_with(
            lines, FINAL_CAUSE_LABELS, LAST_MODEL, LAST_CAUSE
        )
        route_index = _index_of_route_line(lines)

        assert first_index >= 0, (
            "alert must carry a labelled original-cause line naming "
            f"{FIRST_MODEL} and {FIRST_CAUSE}; got lines: {lines!r}"
        )
        assert last_index >= 0, (
            "alert must carry a labelled final-cause line naming "
            f"{LAST_MODEL} and {LAST_CAUSE}; got lines: {lines!r}"
        )
        assert route_index >= 0, (
            "alert must carry a labelled route line naming both endpoints "
            f"with an arrow; got lines: {lines!r}"
        )
        assert len({first_index, last_index, route_index}) == 3, (
            "original cause, final cause and route must be separately "
            f"labelled elements; got lines: {lines!r}"
        )

    def test_structured_record_renders_counters(self):
        lines = _informative_lines(_render(_require_event(STRUCTURED_RECORD)))

        assert _index_of_line_with(lines, RETRY_LABELS, "3") >= 0, (
            f"retry count must be labelled; got lines: {lines!r}"
        )
        assert _index_of_line_with(lines, MESSAGE_COUNT_LABELS, "23") >= 0, (
            f"message count must be labelled; got lines: {lines!r}"
        )
        token_index = max(
            _index_of_line_with(lines, TOKEN_LABELS, "64432"),
            _index_of_line_with(lines, TOKEN_LABELS, "64,432"),
        )
        assert token_index >= 0, (
            f"token count must be labelled; got lines: {lines!r}"
        )


class TestOutputBudget:
    """The formatter must fit Discord's limit regardless of input size."""

    def test_oversized_detail_is_truncated(self):
        module = _load_module()
        event = module.HealthEvent(
            rule="B.api_retries_exhausted",
            category="B",
            title="에이전트 API 재시도 소진",
            reason="모델 API 호출이 3회 재시도 후 종결되었습니다.",
            details=("X" * 20000,),
        )

        rendered = module.format_health_alert(event)

        assert 0 < len(rendered) <= MAX_ALERT_CHARS

    def test_oversized_structured_record_is_truncated(self):
        record = f"{STRUCTURED_RECORD} trailing={'Y' * 20000}"

        rendered = _render(_require_event(record))

        assert 0 < len(rendered) <= MAX_ALERT_CHARS


class TestCredentialRedaction:
    """Structured details cross an external boundary and must be masked."""

    def test_structured_credentials_never_reach_the_alert(self):
        record = (
            f"{STRUCTURED_RECORD} auth=Bearer {SYNTHETIC_TOKEN} "
            f"endpoint={SYNTHETIC_CREDENTIAL_URL}"
        )

        rendered = _render(_require_event(record))

        assert SYNTHETIC_TOKEN not in rendered
        assert SYNTHETIC_TOKEN_BODY not in rendered
        assert SYNTHETIC_CREDENTIAL_URL not in rendered
        assert f"hermes-canary:{SYNTHETIC_TOKEN}" not in rendered


class TestMentionPolicy:
    """Category B stays silent; the mention bit itself must still work."""

    def test_category_b_never_prepends_the_mention(self):
        event = _require_event(STRUCTURED_RECORD)
        assert event.mention is False

        rendered = _render(event, mention_text=MENTION_TEXT)

        assert MENTION_TEXT not in rendered
        assert not rendered.splitlines()[0].startswith(MENTION_TEXT)

    def test_mention_bit_is_still_honoured_when_set(self):
        module = _load_module()
        event = module.HealthEvent(
            rule="C3.stream_stale",
            category="C",
            title="업스트림 스트림 정체",
            reason="테스트용 이벤트입니다.",
            mention=True,
        )

        rendered = module.format_health_alert(event, mention_text=MENTION_TEXT)

        assert rendered.startswith(MENTION_TEXT)


class TestMalformedStructuredFields:
    """Partial or broken fields degrade to the legacy alert, never to a crash."""

    MALFORMED_RECORDS = [
        "[hermes-canary] API call failed after 3 retries. boom "
        "| first= route= last= retries= msgs= tokens~=",
        "[hermes-canary] API call failed after 3 retries. boom "
        "| first=custom/gpt-5.6-sol route=custom/gpt-5.6-sol -> "
        "last=codex-lb/gpt-5.5: retries=x msgs=NaN tokens~=",
        "[hermes-canary] API call failed after 3 retries. boom "
        "| first=:server_error last=:timeout",
    ]

    @pytest.mark.parametrize("record", MALFORMED_RECORDS)
    def test_partial_fields_fall_back_to_legacy_classification(self, record):
        event = _require_event(record)

        assert event.category == "B"
        assert event.rule == "B.api_retries_exhausted"
        assert event.mention is False

        rendered = _render(event)

        assert 0 < len(rendered) <= MAX_ALERT_CHARS
        assert "None" not in rendered, (
            f"missing structured fields must not leak a None into the alert: "
            f"{rendered!r}"
        )


class TestAlertBudgetIdentity:
    """A retry storm collapses on rule+session and stays mention-free."""

    def test_repeated_structured_alerts_collapse_under_cooldown(self):
        module = _load_module()
        budget = module.AlertBudget(cooldown_seconds=900, hourly_cap=12)
        event = _require_event(STRUCTURED_RECORD)

        assert budget.admit(event, now=1_000.0) is not None
        assert budget.admit(event, now=1_060.0) is None

        resumed = budget.admit(event, now=2_000.0)

        assert resumed is not None
        assert resumed.suppressed_count >= 1
        assert resumed.mention is False
        assert MENTION_TEXT not in module.format_health_alert(
            resumed, mention_text=MENTION_TEXT
        )


class TestSyntheticCredentialFixture:
    """Pure input sanitization — grounds the redaction contract above.

    This is the one test expected to pass before the health module exists: it
    proves the synthetic credential really is maskable, so a redaction failure
    upstream is the module's fault and not an unredactable fixture.
    """

    def test_synthetic_credential_fixture_is_redactable(self):
        from agent.redact import redact_sensitive_text

        raw = (
            f"auth=Bearer {SYNTHETIC_TOKEN} "
            f"endpoint={SYNTHETIC_CREDENTIAL_URL}"
        )

        masked = redact_sensitive_text(raw, force=True)

        assert SYNTHETIC_TOKEN not in masked
        assert SYNTHETIC_TOKEN_BODY not in masked
        assert SYNTHETIC_CREDENTIAL_URL not in masked
