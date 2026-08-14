"""RED spec: bounded per-turn failure trace on ``TurnRetryState``.

Replays the sanitized 2026-08-13 retry-exhaustion incident
(``tests/fixtures/incidents/model_api_retry_exhaustion_20260813.json``): the
primary ``custom/gpt-5.6-sol`` died with ``server_error``, the chain failed
over to ``codex-lb/gpt-5.5`` — which sits in the *same* failure domain (see
``agent.failover_domain``) — and that hop then died with ``timeout``. The turn
burned 3 retries on 23 messages / ~64k tokens and the operator-visible output
kept only the *last* error, so neither the originating ``server_error`` nor the
route it travelled survived to the terminal.

What the loop needs (and does NOT have yet) is one bounded, redacted trace of
the attempt chain that ``TurnRetryState`` — already "a single named, testable
home" for per-attempt recovery bookkeeping — can accumulate in place.

Wished-for surface (none of it exists today; these tests are RED by design):

  * ``state.record_failure(*, provider, model, reason)``
        Append one hop to the chain. Keyword names match the fixture's
        ``primary`` / ``fallback`` blocks so a hop replays via ``**hop``.
  * ``state.record_turn_footprint(*, retries, messages, approx_tokens)``
        Store the turn-level counters the summary line reports.
  * ``state.failure_chain``
        Read-only sequence of recorded hops, bounded in length, always
        preserving the FIRST hop and the most recent one. Each hop exposes
        ``provider`` / ``model`` / ``reason`` (attribute or mapping — these
        tests pin the semantics, not the container).
  * ``state.format_failure_trace() -> str``
        Bounded, terminal-safe, secret-redacted rendering suitable for
        ``f"{agent.log_prefix}{trace}"`` (see ``run_agent.AIAgent.log_prefix``).

Design note for the implementer: ``tests/agent/test_turn_retry_state.py::
test_field_set_matches_contract`` pins the dataclass field set exactly, so
adding *dataclass fields* for this feature means updating that contract too.

The last test in this file closes the loop: ``TurnRetryState`` may hold the
trace, but ``agent/conversation_loop.py`` never asks it for one, so a real
``run_conversation`` turn still ships only the last error. That test drives the
actual retry loop end to end and reads the terminal ERROR line.

Nothing here touches production code. Only symbols that exist today are
imported at module scope; every missing member is reached inside a test body,
so this file collects cleanly and fails on behavior, not on import.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_retry_state import TurnRetryState
from run_agent import AIAgent


INCIDENT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "incidents"
    / "model_api_retry_exhaustion_20260813.json"
)

_WISHED_API = (
    "record_failure(*, provider, model, reason) / "
    "record_turn_footprint(*, retries, messages, approx_tokens) / "
    "failure_chain / format_failure_trace()"
)

# Control characters that would corrupt a prefixed terminal line. LF is
# allowed (the trace may wrap onto its own lines); CR, ESC/ANSI, TAB and DEL
# are not.
_UNSAFE_CTRL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

# Synthetic, non-functional token shaped like a real credential. Matches
# ``agent.redact._PREFIX_PATTERNS`` (``sk-[A-Za-z0-9_-]{10,}``) so a formatter
# that routes through the existing redaction helpers scrubs it.
_SECRET_SHAPED = "sk-live-EXAMPLEEXAMPLE0123456789"

# Field names that would carry incident identity or credentials. Matched
# exactly (case-folded), not as substrings — ``approx_tokens`` is a token
# *count*, not a token.
_BANNED_KEYS = frozenset({
    "account", "account_id", "accountid",
    "api_key", "apikey", "access_token", "auth", "authorization",
    "base_url", "body", "endpoint",
    "key", "log", "logs",
    "request", "request_body", "request_id", "requestid",
    "secret", "session", "session_id", "sessionid",
    "token", "trace_id", "traceid", "url", "user_id", "userid",
})


@pytest.fixture
def incident() -> dict:
    """The sanitized incident record — structural facts only, no secrets."""
    return json.loads(INCIDENT_FIXTURE.read_text(encoding="utf-8"))


def _wished(state: TurnRetryState, name: str):
    """Return wished-for member *name*, failing RED with a precise message.

    Keeps the missing-feature failure legible instead of surfacing an opaque
    ``AttributeError`` from inside a helper several frames down.
    """
    assert hasattr(state, name), (
        f"TurnRetryState.{name} is not implemented yet — the bounded "
        f"failure-trace feature replayed from {INCIDENT_FIXTURE.name} is "
        f"missing. Wished-for surface: {_WISHED_API}"
    )
    return getattr(state, name)


def _walk_keys(node):
    """Yield every mapping key in *node*, at any nesting depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def _hop_fields(hop) -> tuple[str, str, str]:
    """Normalize one recorded hop to ``(provider, model, reason)``.

    Accepts a mapping or an attribute-bearing record so the spec constrains
    hop *content*, leaving the representation to the implementer.
    """
    if isinstance(hop, dict):
        return (str(hop["provider"]), str(hop["model"]), str(hop["reason"]))
    return (str(hop.provider), str(hop.model), str(hop.reason))


def _replay_incident(incident: dict) -> TurnRetryState:
    """Drive a fresh ``TurnRetryState`` through the incident's two hops."""
    state = TurnRetryState()
    record_failure = _wished(state, "record_failure")
    record_failure(**incident["primary"])
    record_failure(**incident["fallback"])
    _wished(state, "record_turn_footprint")(
        retries=incident["retries"],
        messages=incident["messages"],
        approx_tokens=incident["approx_tokens"],
    )
    return state


def _expected_fragments(incident: dict) -> list[str]:
    """The four operator-facing fragments, derived from the fixture.

    Literal target for this incident::

        first=custom/gpt-5.6-sol:server_error
        route=custom/gpt-5.6-sol -> codex-lb/gpt-5.5
        last=codex-lb/gpt-5.5:timeout
        retries=3 msgs=23 tokens~=64432
    """
    primary, fallback = incident["primary"], incident["fallback"]
    first = f"{primary['provider']}/{primary['model']}"
    last = f"{fallback['provider']}/{fallback['model']}"
    return [
        f"first={first}:{primary['reason']}",
        f"route={first} -> {last}",
        f"last={last}:{fallback['reason']}",
        (
            f"retries={incident['retries']} "
            f"msgs={incident['messages']} "
            f"tokens~={incident['approx_tokens']}"
        ),
    ]


# ── Fixture integrity (GREEN today — guards the sanitization contract) ──────

def test_incident_fixture_is_sanitized_structural_facts_only(incident):
    """The fixture carries shape, never identifiers or credentials."""
    assert set(incident) == {
        "version",
        "primary",
        "fallback",
        "same_failure_domain",
        "messages",
        "approx_tokens",
        "retries",
    }
    assert incident["primary"] == {
        "provider": "custom",
        "model": "gpt-5.6-sol",
        "reason": "server_error",
    }
    assert incident["fallback"] == {
        "provider": "codex-lb",
        "model": "gpt-5.5",
        "reason": "timeout",
    }
    # The fallback re-entered the pool that had just died — that is why the
    # extra hop bought nothing (agent.failover_domain.same_failure_domain).
    assert incident["same_failure_domain"] is True
    assert (incident["messages"], incident["approx_tokens"], incident["retries"]) == (
        23,
        64432,
        3,
    )

    # Both reasons must be real classifier outcomes, not free text.
    for hop in ("primary", "fallback"):
        FailoverReason(incident[hop]["reason"])

    # No secret- or identifier-shaped content anywhere in the record.
    raw = INCIDENT_FIXTURE.read_text(encoding="utf-8")
    assert "://" not in raw, "fixture must not carry a URL / endpoint"
    for banned in ("sk-", "sk_", "ghp_", "Bearer", "eyJ", "api_key", "apiKey"):
        assert banned not in raw, f"fixture must not carry {banned!r}"
    leaked = sorted({k for k in _walk_keys(incident) if k.lower() in _BANNED_KEYS})
    assert not leaked, f"fixture keys leak incident identity / credentials: {leaked}"


# ── The failure chain itself (RED — feature missing) ────────────────────────

def test_failure_chain_preserves_first_failure_and_route(incident):
    """First ``server_error`` and the route to the fallback must survive."""
    state = _replay_incident(incident)
    chain = [_hop_fields(hop) for hop in _wished(state, "failure_chain")]

    assert chain == [
        ("custom", "gpt-5.6-sol", "server_error"),
        ("codex-lb", "gpt-5.5", "timeout"),
    ], "the chain must keep the originating failure, not just the last one"


def test_formatted_trace_reports_first_route_last_and_counters(incident):
    """The rendered trace carries all four operator-facing fragments."""
    state = _replay_incident(incident)
    trace = _wished(state, "format_failure_trace")()

    assert isinstance(trace, str) and trace, "format_failure_trace() must return text"
    for fragment in _expected_fragments(incident):
        assert fragment in trace, f"missing {fragment!r} in:\n{trace}"


def test_formatted_trace_is_safe_behind_the_terminal_log_prefix(incident):
    """Output must compose with ``f"{agent.log_prefix}{trace}"`` unharmed."""
    state = _replay_incident(incident)
    trace = _wished(state, "format_failure_trace")()

    assert trace == trace.strip(), "trace must not carry outer whitespace"
    assert not _UNSAFE_CTRL_RE.search(trace), (
        "trace must contain no CR / ANSI escape / TAB / DEL — it is printed "
        "straight after the agent log prefix"
    )
    for line in trace.splitlines():
        assert line == line.rstrip(), "no trailing whitespace on any line"
        assert line, "no blank lines inside the trace"
    assert len(trace) <= 400, f"two-hop trace should stay compact, got {len(trace)}"


def test_failure_chain_and_trace_stay_bounded_across_many_hops(incident):
    """A long retry storm must not grow the chain or the trace without limit."""
    state = TurnRetryState()
    record_failure = _wished(state, "record_failure")
    record_failure(**incident["primary"])
    for i in range(200):
        record_failure(provider="codex-lb", model=f"gpt-5.5-{i}", reason="timeout")
    record_failure(**incident["fallback"])
    _wished(state, "record_turn_footprint")(
        retries=incident["retries"],
        messages=incident["messages"],
        approx_tokens=incident["approx_tokens"],
    )

    chain = [_hop_fields(hop) for hop in _wished(state, "failure_chain")]
    # Exact window size is the implementer's call — it just has to be small.
    assert len(chain) <= 12, f"failure chain is unbounded ({len(chain)} hops kept)"
    assert chain[0] == ("custom", "gpt-5.6-sol", "server_error"), (
        "the originating failure must never be evicted"
    )
    assert chain[-1] == ("codex-lb", "gpt-5.5", "timeout"), (
        "the most recent failure must never be evicted"
    )

    trace = _wished(state, "format_failure_trace")()
    assert len(trace) <= 600, f"trace grows with hop count ({len(trace)} chars)"
    assert "first=custom/gpt-5.6-sol:server_error" in trace
    assert "last=codex-lb/gpt-5.5:timeout" in trace


def test_formatted_trace_redacts_secret_shaped_values(incident):
    """A credential leaking in via an upstream error string must be scrubbed."""
    state = TurnRetryState()
    record_failure = _wished(state, "record_failure")
    record_failure(**incident["primary"])
    record_failure(
        provider="codex-lb",
        model="gpt-5.5",
        reason=f"timeout {_SECRET_SHAPED}",
    )
    _wished(state, "record_turn_footprint")(
        retries=incident["retries"],
        messages=incident["messages"],
        approx_tokens=incident["approx_tokens"],
    )

    trace = _wished(state, "format_failure_trace")()
    assert _SECRET_SHAPED not in trace, "secret-shaped value survived into the trace"
    assert "EXAMPLEEXAMPLE0123456789" not in trace, "secret body leaked unmasked"
    # Redaction must not cost the diagnostic signal.
    assert "timeout" in trace and "server_error" in trace


# ── The loop integration (RED — TurnRetryState is never asked) ──────────────
#
# Everything above pins ``TurnRetryState`` in isolation, and it holds up. What
# does not exist is the *wiring*: the terminal line at
# agent/conversation_loop.py:4083 is built from ``_provider`` / ``_model`` /
# ``_final_summary`` alone and never consults ``_retry`` — the per-attempt
# ``TurnRetryState`` the loop already owns (agent/conversation_loop.py:1122).
# So a real turn that fails over still reports only the last error, which is
# exactly how the incident lost its originating ``server_error``.
#
# The test below replays the incident through the real ``run_conversation``
# retry loop with ``api_max_retries=2``:
#
#   attempt 1  custom/gpt-5.6-sol  HTTP 500     → server_error, retry
#   attempt 2  custom/gpt-5.6-sol  HTTP 500     → server_error, budget spent
#              ├─ _try_recover_primary_transport → False (no transport rebuild,
#              │    so the intended fallback branch is the one that runs)
#              └─ _try_activate_fallback         → True → codex-lb/gpt-5.5
#   attempt 3  codex-lb/gpt-5.5    read timeout → timeout, retry
#   attempt 4  codex-lb/gpt-5.5    read timeout → timeout, budget spent
#              └─ _try_activate_fallback         → False (chain exhausted)
#   terminal   logger.error("…API call failed after 2 retries. …")
#
# Patch surface follows the established full-loop pattern in
# tests/run_agent/test_32646_fallback_429_after_timeout.py: the API call,
# session/trajectory persistence, task cleanup, sleeps and every client
# factory are mocked, so nothing here opens a socket or reads a credential.

_PRIMARY = ("custom", "gpt-5.6-sol")
_FALLBACK = ("codex-lb", "gpt-5.5")

# The legacy prefix operators and gateway/agent_health.py both key on
# (``API call failed after (?P<retries>\d+) retries\.``). The wished-for trace
# is additive — this prefix must survive verbatim.
_LEGACY_TERMINAL_PREFIX = "API call failed after 2 retries."

# Loopback with the IANA discard port: the agent needs *a* base_url, and this
# one cannot resolve to anything real even if a mock were to leak. It is never
# part of the asserted trace — the trace carries provider/model labels only.
_UNROUTABLE_BASE_URL = "http://127.0.0.1:9/v1"

# Non-functional placeholder. Asserted absent from the terminal line, so a
# future formatter cannot start echoing credentials into it.
_TEST_API_KEY = "test-key-not-a-credential"

# Runaway guard for the fake API call. The real loop can only reach 4 attempts
# here (2 retries x 2 routes); anything beyond means the branch order changed.
_MAX_FAKE_API_CALLS = 8


class InternalServerError(Exception):
    """Deterministic HTTP 500 from the primary route.

    ``status_code`` is what both the loop (agent/conversation_loop.py:2608) and
    ``error_classifier._extract_status_code`` read, and 500 lands on the
    ``FailoverReason.server_error`` branch of ``_classify_by_status``. The
    message deliberately avoids ``_REQUEST_VALIDATION_PATTERNS`` and
    ``_CONTEXT_OVERFLOW_PATTERNS``, either of which would divert a 5xx into
    ``format_error`` / ``context_overflow`` and never reach the fallback branch.
    """

    status_code = 500

    def __init__(self) -> None:
        super().__init__("Error code: 500 - internal server error")
        self.response = SimpleNamespace(headers={}, status_code=500)
        self.body = {"error": {"message": "internal server error"}}


class ReadTimeout(Exception):
    """Deterministic read timeout from the fallback route.

    The class *name* is load-bearing: ``_TRANSPORT_ERROR_TYPES`` matches on
    ``type(error).__name__``, and "read timed out" independently matches
    ``_TIMEOUT_MESSAGE_PATTERNS`` — both routes classify as
    ``FailoverReason.timeout``. Same shape as the one in
    tests/run_agent/test_32646_fallback_429_after_timeout.py.
    """

    def __init__(self) -> None:
        super().__init__("read timed out")


def _make_incident_agent() -> AIAgent:
    """A quiet agent on the incident's primary route with one fallback hop."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key=_TEST_API_KEY,
            base_url=_UNROUTABLE_BASE_URL,
            provider=_PRIMARY[0],
            model=_PRIMARY[1],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": _FALLBACK[0], "model": _FALLBACK[1]}],
        )
    agent.client = MagicMock()
    # ``api_max_retries`` is config-driven (agent/agent_init.py), so the
    # incident's budget is set on the built agent.
    agent._api_max_retries = 2
    return agent


def test_run_conversation_loop_terminal_log_keeps_first_route_and_last(caplog):
    """The loop's terminal ERROR line must carry the whole attempt chain.

    Replays the incident against the real retry loop. Today the line reports
    ``provider=codex-lb model=gpt-5.5`` — the *last* hop — and the originating
    ``custom/gpt-5.6-sol:server_error`` plus the route it travelled are gone,
    which is the bug this test pins.
    """
    # ── Preconditions: the two exception shapes classify as intended, so a
    # failure below is never a mis-shaped fixture.
    assert classify_api_error(
        InternalServerError(), provider=_PRIMARY[0], model=_PRIMARY[1]
    ).reason is FailoverReason.server_error
    assert classify_api_error(
        ReadTimeout(), provider=_FALLBACK[0], model=_FALLBACK[1]
    ).reason is FailoverReason.timeout

    agent = _make_incident_agent()
    assert (agent.provider, agent.model) == _PRIMARY
    assert len(agent._fallback_chain) == 1

    calls: list[tuple[str, str]] = []
    # (route at the moment of the call, primary attempts already burned)
    activations: list[tuple[tuple[str, str], int]] = []

    def fake_api_call(api_kwargs):
        """Fail deterministically, keyed on the route the loop is using."""
        if len(calls) >= _MAX_FAKE_API_CALLS:
            # pytest.fail raises BaseException, so the loop's ``except
            # Exception`` cannot swallow a runaway and hang the suite.
            pytest.fail(f"runaway retry loop: {calls}")
        route = (agent.provider, agent.model)
        calls.append(route)
        if route == _PRIMARY:
            raise InternalServerError()
        raise ReadTimeout()

    def fake_activate_fallback(*args, **kwargs):
        """Stand in for the real chain walk: activate once, then refuse."""
        route = (agent.provider, agent.model)
        activations.append((route, calls.count(_PRIMARY)))
        if agent._fallback_activated:
            return False  # chain spent — the loop must go terminal
        # Fallback is only legitimate once the primary's budget is gone.
        assert calls.count(_PRIMARY) == agent._api_max_retries, (
            "fallback activated before the primary exhausted its retries"
        )
        agent.provider, agent.model = _FALLBACK
        agent._fallback_index = len(agent._fallback_chain)  # exhaust the chain
        agent._fallback_activated = True
        return True

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(
            agent, "_try_recover_primary_transport", return_value=False,
        ) as recover,
        patch.object(
            agent, "_try_activate_fallback", side_effect=fake_activate_fallback,
        ) as activate,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_dump_api_request_debug"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(MagicMock(), _FALLBACK[1]),
        ) as resolve,
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
        caplog.at_level(logging.ERROR, logger="agent.conversation_loop"),
    ):
        result = agent.run_conversation("replay the incident turn")

    # ── The turn really failed, on both routes ──────────────────────────────
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == FailoverReason.timeout.value
    assert calls == [_PRIMARY, _PRIMARY, _FALLBACK, _FALLBACK], (
        "the loop must burn the primary's budget, fail over, then burn the "
        f"fallback's — got {calls}"
    )
    assert (agent.provider, agent.model) == _FALLBACK
    # Fallback was attempted after primary exhaustion, then again once spent.
    assert [route for route, _ in activations] == [_PRIMARY, _FALLBACK]
    assert [burned for _, burned in activations] == [2, 2]
    assert activate.call_count == 2
    assert recover.call_count >= 1, (
        "the transport-recovery probe must run before the fallback branch"
    )
    resolve.assert_not_called()  # no client was really built

    # ── The terminal line ───────────────────────────────────────────────────
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    terminal = [m for m in messages if _LEGACY_TERMINAL_PREFIX in m]
    assert terminal, (
        f"no terminal ERROR record carrying {_LEGACY_TERMINAL_PREFIX!r}; "
        f"ERROR records seen: {messages}"
    )
    line = terminal[0]

    # Legacy contract: gateway/agent_health.py parses this prefix.
    assert _LEGACY_TERMINAL_PREFIX in line
    assert _TEST_API_KEY not in line, "terminal line must never echo a credential"
    assert _UNROUTABLE_BASE_URL not in line, "terminal line must not carry an endpoint"

    # Wished-for: the same line carries the chain, not just the last hop.
    for fragment in (
        f"first={_PRIMARY[0]}/{_PRIMARY[1]}:{FailoverReason.server_error.value}",
        f"route={_PRIMARY[0]}/{_PRIMARY[1]} -> {_FALLBACK[0]}/{_FALLBACK[1]}",
        f"last={_FALLBACK[0]}/{_FALLBACK[1]}:{FailoverReason.timeout.value}",
        f"retries={agent._api_max_retries}",
    ):
        assert fragment in line, (
            f"terminal log line is missing {fragment!r} — the loop never asks "
            f"TurnRetryState for its failure trace ({_WISHED_API}).\n"
            f"got: {line}"
        )
    # Turn footprint stays labelled. Tolerant of both spellings so this pins
    # the fields, not the punctuation the implementer settles on.
    assert re.search(r"\bmsgs=\d+", line), f"no labelled msgs field in: {line}"
    assert re.search(r"\btokens~?=~?[\d,]+", line), f"no labelled tokens field in: {line}"
