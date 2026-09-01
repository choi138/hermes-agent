"""Per-attempt recovery bookkeeping for the conversation turn loop.

The inner retry loop in ``run_conversation`` (``while retry_count <
max_retries``) makes several distinct recovery attempts on a single model API
call: a credential-pool 429 retry, a per-provider OAuth refresh (codex,
anthropic, nous, copilot), a long-context compression restart, a length-
continuation restart, and a handful of format-recovery branches (thinking-
signature stripping, multimodal-tool-content stripping, llama.cpp grammar
fallback, image shrink, invalid-encrypted-content, 1M-beta header).

Each of those branches is guarded by a one-shot boolean so it fires at most
once per attempt. They used to be ~16 bare ``*_attempted`` / ``has_retried_*``
/ ``restart_with_*`` locals declared inline before the loop and threaded
through its 2,400-line body. ``TurnRetryState`` collapses them into one object
the loop mutates in place (``state.codex_auth_retry_attempted = True``), giving
the recovery bookkeeping a single named, testable home.

Loop-control variables (``retry_count``, ``max_retries``,
``max_compression_attempts``) intentionally stay as plain locals — they are the
``while`` mechanics, not recovery bookkeeping, and putting them on the object
would add indirection without clarifying anything.

On top of the guards, the state accumulates a bounded **failure trace** for the
turn: each failed API call is appended with ``record_failure``, the turn's size
with ``record_turn_footprint``, and ``format_failure_trace`` renders one compact
line. The point is to keep the *originating* failure and the route it travelled,
not just the last error — the 2026-08-13 retry-exhaustion incident lost a primary
``server_error`` because the chain failed over into the same failure domain (see
``agent.failover_domain``), timed out there, and only that timeout reached the
operator.

The trace is printed straight after ``AIAgent.log_prefix`` and written to logs,
so every component is collapsed to one line, stripped of ANSI/control bytes, run
through the canonical secret redactor and length-capped; the chain keeps at most
``_MAX_FAILURE_HOPS`` hops, always including the first and the most recent. Full
URLs, request bodies and credentials are never stored — only ``provider`` /
``model`` / ``reason`` labels and three counters.

That trace state lives *outside* the dataclass field set (see ``__post_init__``):
the fields below are exactly the loop's one-shot booleans, and both ``__iter__``
and the field-set contract test in tests/agent/test_turn_retry_state.py depend on
that staying true.

The only dependency is ``agent.redact``, itself stdlib-only, so this module
still unit-tests in isolation and imports into the turn loop without a cycle.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, fields
from typing import Any

from agent.redact import redact_sensitive_text

# ── Failure-trace bounds ─────────────────────────────────────────────────────
# Deliberately small: the trace is a single log line, and an operator reads the
# originating failure, the route and the final failure — not 200 timeouts. The
# per-component caps also bound the rendered line, since only the first and last
# hop are ever printed.
_MAX_FAILURE_HOPS = 8
_MAX_PROVIDER_CHARS = 32
_MAX_MODEL_CHARS = 48
_MAX_REASON_CHARS = 64
_MAX_TRACE_CHARS = 600

_TRUNCATED = "…"
_UNKNOWN = "?"

# ANSI / VT escape sequences: CSI, the string sequences (OSC/DCS/PM/APC/SOS) and
# the two-character Fe forms. Matched whole so their parameter bytes don't
# survive as literal junk ("[0m") once the ESC byte itself is stripped.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]"
    r"|[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[@-Z\\-_])"
)
# Anything left in the C0/C1 control ranges, DEL included. CR would let a hop
# label overwrite the log prefix, LF would split the line, TAB would misalign it.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _strip_terminal_noise(text: str) -> str:
    """Collapse *text* into one control-free, single-spaced line."""
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub(" ", text)
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


def _stringify(value: Any) -> str:
    """Best-effort text for a non-string hop component.

    Enum members get their ``.value``: the classifier hands out
    ``FailoverReason.timeout``, whose ``str()`` is the far less useful
    ``"FailoverReason.timeout"``. A ``__str__`` that raises degrades to empty
    rather than masking the API failure we are trying to trace.
    """
    if isinstance(value, enum.Enum) and isinstance(value.value, str):
        return value.value
    try:
        return str(value)
    except Exception:
        return ""


def _normalize_component(value: Any, limit: int) -> str:
    """Sanitize one hop component: single-line, redacted, length-capped."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else _stringify(value)
    # Terminal noise goes first: an escape sequence spliced into a credential
    # would otherwise hide it from the redactor's prefix patterns.
    text = _strip_terminal_noise(text)
    if not text:
        return ""
    # force=True — a failure trace must never emit raw credentials, whatever the
    # operator's global ``security.redact_secrets`` preference is.
    text = redact_sensitive_text(text, force=True)
    # Cap last, so truncation can never shorten a credential below the redactor's
    # minimum match length and let the remnant through.
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + _TRUNCATED
    return text


def _normalize_counter(value: Any) -> int:
    """Coerce a turn-level counter to a non-negative int (0 for garbage)."""
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class ApiFailureHop:
    """One failed API call: who was called, and why it failed.

    Frozen because a recorded hop is diagnostic evidence — nothing downstream
    should be able to edit it. Values arrive already sanitized by
    ``TurnRetryState.record_failure``.
    """

    provider: str
    model: str
    reason: str

    @property
    def route(self) -> str:
        """``provider/model`` — the hop's endpoint identity."""
        return f"{self.provider}/{self.model}"

    def describe(self) -> str:
        """``provider/model:reason``, dropping ``:reason`` when unknown."""
        return f"{self.route}:{self.reason}" if self.reason else self.route


@dataclass
class TurnRetryState:
    """One-shot recovery guards + restart signals for a single API-call attempt.

    A fresh instance is created for each iteration of the outer turn loop
    (once per ``api_call_count``). Each guard fires its recovery branch at most
    once; the ``restart_with_*`` signals are read by the loop after the attempt
    to decide whether to rebuild the request and retry.
    """

    # ── Per-provider OAuth / credential refresh guards ───────────────────
    codex_auth_retry_attempted: bool = False
    anthropic_auth_retry_attempted: bool = False
    nous_auth_retry_attempted: bool = False
    nous_paid_entitlement_refresh_attempted: bool = False
    copilot_auth_retry_attempted: bool = False
    # Copilot surfaces a stale/degraded credential as a 400
    # ``model_not_available_for_integrator`` / ``model_not_supported`` instead
    # of a clean 401 (e.g. a raw OAuth token seeded when the token exchange
    # degraded at startup, routing the request to the restricted
    # ``copilot-language-server`` integrator). Guard a single-shot forced
    # re-exchange + client rebuild for that case, separate from the 401 guard
    # so both can fire within one attempt if needed.
    copilot_stale_cred_retry_attempted: bool = False
    vertex_auth_retry_attempted: bool = False

    # ── Format / payload recovery guards ─────────────────────────────────
    thinking_sig_retry_attempted: bool = False
    invalid_encrypted_content_retry_attempted: bool = False
    image_shrink_retry_attempted: bool = False
    multimodal_tool_content_retry_attempted: bool = False
    oauth_1m_beta_retry_attempted: bool = False
    llama_cpp_grammar_retry_attempted: bool = False

    # ── Transport / rate-limit recovery ──────────────────────────────────
    primary_recovery_attempted: bool = False
    has_retried_429: bool = False

    # ── Auth-failure provider failover ───────────────────────────────────
    # Set once we've escalated a persistent 401/403 (after the per-provider
    # credential-refresh attempt above failed) to the fallback chain, so we
    # don't loop on the same auth failover within one attempt.
    auth_failover_attempted: bool = False

    # ── Restart signals (read by the outer loop after the attempt) ───────
    restart_with_compressed_messages: bool = False
    restart_with_length_continuation: bool = False
    # Set when a content-filter stream stall (e.g. MiniMax "new_sensitive")
    # has been escalated to the fallback chain: the partial-stream content
    # was rolled back off ``messages`` and the loop should re-issue the API
    # call against the newly-activated provider (#32421).
    restart_with_rebuilt_messages: bool = False
    # A user correction cancelled the in-flight provider request. The outer
    # loop must append a role-safe checkpoint + user message, rebuild the API
    # payload, and retry the same logical iteration.
    restart_with_redirected_messages: bool = False

    def __iter__(self):
        # Convenience for debugging / tests: iterate (name, value) pairs.
        for f in fields(self):
            yield f.name, getattr(self, f.name)

    # ── Bounded per-turn failure trace ───────────────────────────────────
    # All trace state below is deliberately kept off the dataclass field set:
    # the fields above are exactly the loop's one-shot booleans, and both
    # ``__iter__`` and the field-set contract test in
    # tests/agent/test_turn_retry_state.py depend on that staying true. The
    # private attributes are created in ``__post_init__``, but every reader
    # goes through a lazy accessor / ``getattr`` default so an instance
    # restored from an older pickle (no private state at all) still records and
    # formats instead of raising mid-incident.

    def __post_init__(self) -> None:
        # Annotated on ``self``, never in the class body — an attribute
        # annotation does not reach ``__annotations__``, so ``fields()`` and
        # ``__iter__`` stay limited to the guards above.
        self._failure_hops: list[ApiFailureHop] = []
        self._footprint_recorded: bool = False
        self._retry_count: int = 0
        self._message_count: int = 0
        self._approx_token_count: int = 0

    def _hops(self) -> list[ApiFailureHop]:
        """The live hop list, materialized on first use."""
        hops = getattr(self, "_failure_hops", None)
        if not isinstance(hops, list):
            hops = []
            self._failure_hops = hops
        return hops

    def record_failure(self, *, provider: Any, model: Any, reason: Any) -> None:
        """Append one failed API call to this turn's failure chain.

        ``provider`` / ``model`` degrade to ``_UNKNOWN`` when they sanitize to
        nothing: a hop with no identity is still evidence that a call failed,
        and dropping it would break the first → last route the operator reads.
        An empty ``reason`` is kept as-is — ``ApiFailureHop.describe`` then
        renders the bare route instead of a dangling ``":"``.
        """
        hop = ApiFailureHop(
            provider=_normalize_component(provider, _MAX_PROVIDER_CHARS) or _UNKNOWN,
            model=_normalize_component(model, _MAX_MODEL_CHARS) or _UNKNOWN,
            reason=_normalize_component(reason, _MAX_REASON_CHARS),
        )
        hops = self._hops()
        hops.append(hop)
        # Evict from the middle. The originating failure (index 0) and the most
        # recent one are the two hops that carry information; a retry storm
        # against one endpoint loses only the indistinguishable hops between
        # them. This is what the 2026-08-13 incident needed and did not have.
        overflow = len(hops) - _MAX_FAILURE_HOPS
        if overflow > 0:
            start = 1 if _MAX_FAILURE_HOPS > 1 else 0
            del hops[start : start + overflow]

    def record_turn_footprint(
        self, *, retries: Any, messages: Any, approx_tokens: Any
    ) -> None:
        """Store what the turn cost, so the trace can report it alongside why."""
        self._retry_count = _normalize_counter(retries)
        self._message_count = _normalize_counter(messages)
        self._approx_token_count = _normalize_counter(approx_tokens)
        self._footprint_recorded = True

    @property
    def failure_chain(self) -> tuple[ApiFailureHop, ...]:
        """Recorded hops, oldest first — a snapshot, not the live list."""
        hops = getattr(self, "_failure_hops", None)
        return tuple(hops) if isinstance(hops, list) else ()

    def _footprint_fragment(self) -> str:
        """``retries=N msgs=N tokens~=N`` from the recorded counters."""
        retries = _normalize_counter(getattr(self, "_retry_count", 0))
        messages = _normalize_counter(getattr(self, "_message_count", 0))
        tokens = _normalize_counter(getattr(self, "_approx_token_count", 0))
        return f"retries={retries} msgs={messages} tokens~={tokens}"

    def format_failure_trace(self) -> str:
        """Render the chain as one bounded, terminal-safe, redacted line.

        Empty when nothing was recorded, so callers can guard with ``if
        trace:`` and never print a lone prefix. Fragment order is fixed —
        ``first`` / ``route`` / ``last`` / counters — because operators grep it.
        """
        hops = self.failure_chain
        recorded = bool(getattr(self, "_footprint_recorded", False))
        if not hops:
            # A turn that recorded its size but no failure still says so; the
            # counters are the only signal left.
            return self._footprint_fragment() if recorded else ""

        first, last = hops[0], hops[-1]
        head = f"first={first.describe()}"
        tail = f"last={last.describe()}"
        # Only when the chain actually moved endpoints. A same-route storm
        # renders first/last — whose reasons may still differ — without an
        # arrow pointing at itself.
        route = (
            f"route={first.route} -> {last.route}"
            if first.route != last.route
            else ""
        )
        counters = self._footprint_fragment() if recorded else ""

        # Shed optional fragments before ever cutting into the first/last
        # diagnostics. Defensive: the per-component caps already keep the
        # natural line well inside _MAX_TRACE_CHARS.
        line = ""
        for parts in ((head, route, tail, counters), (head, route, tail), (head, tail)):
            line = _strip_terminal_noise(" ".join(part for part in parts if part))
            if len(line) <= _MAX_TRACE_CHARS:
                return line
        # Unreachable with sane caps. Every component is already control-free,
        # so the cut cannot land inside an escape sequence; re-strip anyway and
        # keep the ellipsis so the line reads as truncated.
        return _strip_terminal_noise(line[: _MAX_TRACE_CHARS - 1].rstrip() + _TRUNCATED)
