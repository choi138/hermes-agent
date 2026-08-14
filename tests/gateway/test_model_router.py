"""Tests for the dynamic model router (ADR-003 Phase 2).

Covers gateway/model_router.py (context payload parity, classifier fallback,
hysteresis, static rules) and the current GatewayRunner shadow-only wiring
(shadow evaluation, off/enforce no-op behavior, decision-log isolation).

No network: the classifier is exercised either via the ``complete_dev`` seam
or by monkeypatching ``gateway.model_router._call_gemini`` /
``gateway.model_router._urlopen``. Health probes are neutralized by the
model_routes pytest guard. Decision logs go to tmp via
``HERMES_MODEL_ROUTER_DECISION_LOG``.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gateway.model_router as mr_mod
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.model_routes import load_routes


EXPECTED_RECORD_FIELDS = {
    "policy", "session_key", "label", "confidence", "evidence", "source",
    "provider", "model", "outcome", "directive_route", "runtime_model", "msg_head",
    "mode", "rule",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _providers():
    return {
        "p1": {"base_url": "https://p1.example/v1"},
        "p2": {"base_url": "https://p2.example/v1"},
    }


def _cfg(*, router=None, static_rules=None):
    section = {
        "routes": {
            "dev": {
                "description": "dev route",
                "provider": "p1",
                "model": "model-a",
                "reasoning_effort": "xhigh",
            },
            "chat": {"description": "chat route", "provider": "p2", "model": "model-b"},
        },
    }
    if static_rules is not None:
        section["static_rules"] = static_rules
    section["router"] = dict(
        {
            "mode": "shadow",
            "model": "gemini-3-flash-preview",
            "timeout_ms": 8000,
            "recent_turns": 5,
            "normal_downgrade_streak": 3,
            "chat_route": "chat",
            "label_routes": {"SYSTEM_DEV": "dev", "FRONTEND_DEV": "dev", "DOCUMENT_WORK": "dev"},
        },
        **(router or {}),
    )
    return {"providers": _providers(), "model_routes": section}


def _catalog(cfg):
    catalog = load_routes(cfg)
    assert [i for i in catalog.issues if i.severity == "error"] == []
    return catalog


def _source(**kwargs) -> SessionSource:
    defaults = dict(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )
    defaults.update(kwargs)
    return SessionSource(**defaults)


def _event(text="hermes gateway 고장났어 디버깅해줘", **kwargs) -> MessageEvent:
    source = kwargs.pop("source", None) or _source()
    return MessageEvent(text=text, message_id="m1", source=source, **kwargs)


class _FakeDB:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return list(self.messages)


class _FakeStore:
    def __init__(self, key="tg:c1", session_id="sid-1", messages=()):
        self._key = key
        self._sid = session_id
        self._db = _FakeDB(messages)

    def _generate_session_key(self, source):
        return self._key

    def peek_session_id(self, session_key):
        return self._sid


def _complete(label, confidence=0.9, evidence="S5 test"):
    def _fn(prompt):
        return json.dumps({"evidence": evidence, "label": label, "confidence": confidence})
    return _fn


def _evaluate(
    *,
    text="status?",
    complete_dev=None,
    runtime=None,
    state=None,
    cfg=None,
    mode="shadow",
    store=None,
    source=None,
    event=None,
):
    cfg = cfg if cfg is not None else _cfg()
    catalog = _catalog(cfg)
    return mr_mod.evaluate_event(
        event=event or _event(text, source=source),
        session_store=store or _FakeStore(),
        # Default runtime is a full member of the "dev" route: the route
        # declares reasoning_effort xhigh, and legacy membership matches
        # effort-declaring specs against the runtime effort (B3 semantics).
        runtime=(
            {"model": "model-a", "provider": "p1", "reasoning_effort": "xhigh"}
            if runtime is None else runtime
        ),
        cfg=cfg,
        catalog=catalog,
        router=catalog.router,
        mode=mode,
        state={} if state is None else state,
        complete_dev=complete_dev,
    )


# ---------------------------------------------------------------------------
# Context payload shape + truncation budget
# ---------------------------------------------------------------------------


def test_payload_field_order_and_truncation():
    store = _FakeStore(messages=[
        {"role": "user", "content": "질문" * 700},          # > 1200 chars, truncated
        {"role": "tool", "content": "ignored role"},
        {"role": "assistant", "content": "답변"},
        {"role": "user", "content": ""},                    # empty, dropped
    ])
    event = _event(
        "x" * 3000,
        reply_to_text="r" * 2000,
        channel_context="c" * 4000,
    )
    context = mr_mod.build_context(
        event=event, session_store=store, runtime={"model": "m"}, recent_turn_limit=5,
    )
    payload = context.as_prompt_payload()
    assert list(payload) == [
        "current_user_message", "recent_turns", "reply_to_text", "channel_context",
        "source", "session_key", "session_id", "runtime", "loaded_skills",
    ]
    # _truncate keeps limit-20 chars + the 12-char "…[truncated]" marker.
    assert len(payload["current_user_message"]) == 2000 - 8
    assert payload["current_user_message"].endswith("…[truncated]")
    assert len(payload["reply_to_text"]) == 1000 - 8
    assert len(payload["channel_context"]) == 1800 - 8
    assert [t["role"] for t in payload["recent_turns"]] == ["user", "assistant"]
    assert len(payload["recent_turns"][0]["content"]) == 1200 - 8
    assert payload["session_key"] == "tg:c1"
    assert payload["session_id"] == "sid-1"
    assert payload["runtime"] == {"model": "m"}
    assert payload["loaded_skills"] == []  # always present, [] at this base


def test_payload_budget_drops_oldest_turns_and_stays_valid_json():
    turns = [
        {"role": "user", "content": f"turn-{i} " + "가" * 1100} for i in range(12)
    ]
    store = _FakeStore(messages=turns)
    context = mr_mod.build_context(
        event=_event("최근 메시지"), session_store=store, recent_turn_limit=12,
    )
    text = mr_mod._payload_json(context)
    assert len(text) <= mr_mod.MAX_CONTEXT_CHARS
    payload = json.loads(text)  # stays valid JSON (no char-slice tail)
    remaining = [t["content"].split()[0] for t in payload["recent_turns"]]
    assert remaining  # something survived
    # Oldest turns were dropped: the survivors are a suffix of the originals.
    assert remaining == [f"turn-{i}" for i in range(12 - len(remaining), 12)]


def test_recent_turns_limit_applied():
    store = _FakeStore(messages=[
        {"role": "user", "content": f"m{i}"} for i in range(10)
    ])
    context = mr_mod.build_context(event=_event("hi"), session_store=store, recent_turn_limit=3)
    assert [t.content for t in context.recent_turns] == ["m7", "m8", "m9"]


# ---------------------------------------------------------------------------
# Classifier + fallback
# ---------------------------------------------------------------------------


def test_classifier_llm_json_parsed():
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="fix the bug"),
        complete=_complete("SYSTEM_DEV", confidence=0.83, evidence="S5 debug"),
    )
    assert detail == {
        "label": "SYSTEM_DEV", "confidence": 0.83, "evidence": "S5 debug", "source": "llm",
    }


def test_classifier_failure_falls_back_to_regex():
    def _boom(prompt):
        raise TimeoutError("classifier down")

    context = mr_mod.PolicyClassificationContext(
        current_user_message="gateway 고장났어 디버깅 좀"
    )
    detail = mr_mod.classify_dev_detailed(context, complete=_boom)
    assert detail["source"] == "fallback"
    assert detail["label"] == "SYSTEM_DEV"

    frontend = mr_mod.PolicyClassificationContext(current_user_message="React 컴포넌트 수정해줘")
    assert mr_mod.classify_dev_detailed(frontend, complete=_boom)["label"] == "FRONTEND_DEV"

    normal = mr_mod.PolicyClassificationContext(current_user_message="오늘 날씨 어때?")
    detail = mr_mod.classify_dev_detailed(normal, complete=_boom)
    assert detail == {"label": "NORMAL", "confidence": None, "evidence": "", "source": "fallback"}


def test_missing_api_key_takes_fallback_path(monkeypatch, tmp_path):
    # No complete seam → real _call_gemini path; no key anywhere → fallback.
    for name in ("HERMES_GRAPHITI_EMBEDDER_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    called = []
    monkeypatch.setattr(mr_mod, "_urlopen", lambda *a, **k: called.append(1))
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="pytest 돌려서 fix 해줘")
    )
    assert called == []  # never reached the network seam
    assert detail["source"] == "fallback"


def test_call_gemini_request_shape(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "candidates": [{"content": {"parts": [{"text": "NORMAL"}]}}]
            }).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(mr_mod, "_urlopen", fake_urlopen)
    raw = mr_mod._call_gemini(
        "Context JSON:\n{}",
        model="gemini-3-flash-preview",
        timeout=8.0,
        max_tokens=256,
        system_instruction=mr_mod.DEV_SYSTEM_PROMPT,
        response_schema=mr_mod.DEV_RESPONSE_SCHEMA,
    )
    assert raw == "NORMAL"
    assert "v1beta/models/gemini-3-flash-preview:generateContent" in captured["url"]
    assert captured["timeout"] == 8.0
    gen = captured["body"]["generationConfig"]
    assert gen["temperature"] == 0
    assert gen["thinkingConfig"] == {"thinkingBudget": 0}
    assert gen["maxOutputTokens"] == 256
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseSchema"] == mr_mod.DEV_RESPONSE_SCHEMA
    assert captured["body"]["systemInstruction"] == {
        "parts": [{"text": mr_mod.DEV_SYSTEM_PROMPT}]
    }


def test_parse_dev_json_plain_token_fallback():
    assert mr_mod._parse_dev_json("FRONTEND_DEV") is None
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="x"),
        complete=lambda prompt: "FRONTEND_DEV",
    )
    assert detail == {"label": "FRONTEND_DEV", "confidence": None, "evidence": "", "source": "llm"}


# ---------------------------------------------------------------------------
# Hysteresis ladder
# ---------------------------------------------------------------------------


def test_normal_streak_downgrades_after_threshold():
    state = {}
    outcomes = []
    for _ in range(3):
        decision = _evaluate(complete_dev=_complete("NORMAL"), state=state)
        outcomes.append(decision.outcome)
    assert outcomes == ["normal_streak_1_of_3", "normal_streak_2_of_3", "downgrade_to_chat"]
    final = _evaluate(complete_dev=_complete("NORMAL"), state=state)
    assert final.outcome == "downgrade_to_chat"
    assert final.directive["route"] == "chat"
    assert final.directive["model"] == "model-b"
    assert final.directive["reason"].startswith("chat handoff after 4 consecutive NORMAL turns")


def test_fallback_normal_never_advances_streak():
    state = {}

    def _boom(prompt):
        raise TimeoutError("down")

    for _ in range(5):
        decision = _evaluate(text="오늘 뭐 먹지?", complete_dev=_boom, state=state)
        assert decision.outcome == "normal_fallback_no_downgrade"
        assert decision.record["source"] == "fallback"
    assert state["tg:c1"]["normal_streak"] == 0


def test_dev_label_resets_streak():
    state = {}
    _evaluate(complete_dev=_complete("NORMAL"), state=state)
    _evaluate(complete_dev=_complete("NORMAL"), state=state)
    assert state["tg:c1"]["normal_streak"] == 2
    decision = _evaluate(complete_dev=_complete("SYSTEM_DEV"), state=state)
    assert decision.outcome == "noop_satisfied"  # runtime model-a is already dev
    assert state["tg:c1"]["normal_streak"] == 0
    # The next NORMAL starts a fresh streak.
    decision = _evaluate(complete_dev=_complete("NORMAL"), state=state)
    assert decision.outcome == "normal_streak_1_of_3"


def test_normal_outcomes_no_chat_route_and_unknown_runtime():
    cfg = _cfg(router={"chat_route": ""})
    decision = _evaluate(complete_dev=_complete("NORMAL"), cfg=cfg)
    assert decision.outcome == "normal_no_chat_route"
    assert decision.directive is None

    decision = _evaluate(complete_dev=_complete("NORMAL"), runtime={})
    assert decision.outcome == "normal_unknown_runtime"

    decision = _evaluate(
        complete_dev=_complete("NORMAL"), runtime={"model": "model-b", "provider": "p2"},
    )
    assert decision.outcome == "noop_already_chat"


def test_dev_switch_and_noop_and_unmapped_label():
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        runtime={"model": "model-b", "provider": "p2"},
    )
    assert decision.outcome == "switch"
    assert decision.directive["route"] == "dev"
    assert decision.directive["provider"] == "p1"
    assert decision.directive["model"] == "model-a"
    assert decision.directive["reasoning_effort"] == "xhigh"

    decision = _evaluate(complete_dev=_complete("SYSTEM_DEV"))  # runtime already dev
    assert decision.outcome == "noop_satisfied"
    assert decision.directive is None

    cfg = _cfg(router={"label_routes": {"SYSTEM_DEV": "dev"}})  # DOCUMENT_WORK unmapped
    decision = _evaluate(
        complete_dev=_complete("DOCUMENT_WORK"),
        runtime={"model": "model-b", "provider": "p2"},
        cfg=cfg,
    )
    assert decision.outcome == "none"
    assert decision.directive is None


def test_slash_command_and_empty_text_early_return():
    sentinel = MagicMock(side_effect=AssertionError("classifier must not run"))
    assert _evaluate(text="/model sonnet", complete_dev=sentinel) is None
    assert _evaluate(text="   ", complete_dev=sentinel) is None
    assert _evaluate(text="", complete_dev=sentinel) is None
    sentinel.assert_not_called()


def test_decision_record_schema():
    decision = _evaluate(complete_dev=_complete("SYSTEM_DEV"), mode="shadow")
    assert set(decision.record) == EXPECTED_RECORD_FIELDS
    assert decision.record["policy"] == "dev_routing"
    assert decision.record["mode"] == "shadow"
    assert decision.record["rule"] is None
    assert decision.record["model"] == "gemini-3-flash-preview"
    assert decision.record["runtime_model"] == "model-a"
    assert decision.record["msg_head"] == "status?"


# ---------------------------------------------------------------------------
# Static rules
# ---------------------------------------------------------------------------


def test_static_rule_first_match_wins_and_short_circuits(monkeypatch):
    rules = [
        {"name": "second", "route": "chat", "when": {"text_matches_any": ["never-matches"]}},
        {"name": "pr-rule", "route": "dev", "when": {"text_matches_any": [r"codex-lb\s+#?\d+"]}},
        {"name": "shadowed", "route": "chat", "when": {"text_matches_any": [r"codex-lb"]}},
    ]
    sentinel = MagicMock(side_effect=AssertionError("classifier must not run"))
    monkeypatch.setattr(mr_mod, "_call_gemini", sentinel)
    decision = _evaluate(
        text="codex-lb #123 리뷰해줘",
        cfg=_cfg(static_rules=rules),
        runtime={"model": "model-b", "provider": "p2"},
        complete_dev=None,
    )
    sentinel.assert_not_called()
    assert decision.rule == "pr-rule"
    assert decision.outcome == "switch"
    assert decision.directive["route"] == "dev"
    record = decision.record
    assert set(record) == EXPECTED_RECORD_FIELDS
    assert record["policy"] == "static_rule"
    assert record["source"] == "static"
    assert record["rule"] == "pr-rule"
    assert record["label"] == "dev"


def test_static_rule_noop_when_runtime_already_member():
    rules = [{"name": "pr-rule", "route": "dev", "when": {"text_matches_any": ["codex-lb"]}}]
    decision = _evaluate(
        text="codex-lb 확인해줘",
        cfg=_cfg(static_rules=rules),
        runtime={"model": "model-a", "provider": "p1", "reasoning_effort": "xhigh"},
        complete_dev=_complete("NORMAL"),
    )
    assert decision.rule == "pr-rule"
    assert decision.outcome == "noop_satisfied"
    assert decision.directive is None


def test_static_rule_is_owner_env_semantics(monkeypatch):
    rules = [{"name": "guard", "route": "chat", "when": {"is_owner": {"eq": False}}}]
    cfg = _cfg(static_rules=rules)
    runtime = {"model": "model-a", "provider": "p1"}

    # Allowlist set, sender not on it → not owner → rule matches.
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999,888")
    decision = _evaluate(text="안녕", cfg=cfg, runtime=runtime, complete_dev=_complete("NORMAL"))
    assert decision.rule == "guard" and decision.outcome == "switch"

    # Sender on the allowlist → owner → falls through to the classifier.
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "u1,999")
    decision = _evaluate(text="안녕", cfg=cfg, runtime=runtime, complete_dev=_complete("NORMAL"))
    assert decision.rule is None

    # Missing/empty allowlist → everyone is owner (fail-open).
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    decision = _evaluate(text="안녕", cfg=cfg, runtime=runtime, complete_dev=_complete("NORMAL"))
    assert decision.rule is None

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "   ,  ")
    decision = _evaluate(text="안녕", cfg=cfg, runtime=runtime, complete_dev=_complete("NORMAL"))
    assert decision.rule is None


def test_static_rule_source_field_conditions():
    rules = [{
        "name": "scoped",
        "route": "dev",
        "when": {
            "platform": {"eq": "telegram"},
            "chat_id": {"in": ["c1", "c2"]},
            "user_id": {"not_in": ["banned"]},
        },
    }]
    cfg = _cfg(static_rules=rules)
    runtime = {"model": "model-b", "provider": "p2"}
    decision = _evaluate(text="아무 텍스트", cfg=cfg, runtime=runtime, complete_dev=_complete("NORMAL"))
    assert decision.rule == "scoped" and decision.outcome == "switch"

    # AND semantics: one failing condition → no match.
    decision = _evaluate(
        text="아무 텍스트", cfg=cfg, runtime=runtime,
        source=_source(chat_id="c3"), complete_dev=_complete("NORMAL"),
    )
    assert decision.rule is None


def test_static_rule_unknown_condition_never_matches():
    assert mr_mod.match_static_rule(
        [{"route": "dev", "when": {"channel": "codex-lb-pr"}}],
        text="anything", source_context={"platform": "telegram"},
    ) is None


def test_static_rule_text_matches_ignorecase():
    # Plugin parity: skill-gate compiles every scan pattern with IGNORECASE;
    # the live codex-lb rule fails without it.
    rules = [{"name": "pr", "route": "dev", "when": {"text_matches_any": ["codex-lb"]}}]
    assert mr_mod.match_static_rule(
        rules, text="CODEX-LB #123 봐줘", source_context={},
    ) is not None
    assert mr_mod.match_static_rule(
        rules, text="Codex-Lb 상태 어때", source_context={},
    ) is not None


def test_static_rule_matches_raw_unstripped_text():
    # Plugin parity: matching runs on the RAW event text, so anchors can see
    # leading whitespace that .strip() would have removed.
    rules = [{"name": "ws", "route": "dev", "when": {"text_matches_any": [r"^\s+urgent"]}}]
    assert mr_mod.match_static_rule(rules, text="   urgent fix", source_context={}) is not None
    assert mr_mod.match_static_rule(rules, text="urgent fix", source_context={}) is None

    # evaluate_event feeds the raw text through to matching.
    decision = _evaluate(
        text="   urgent fix",
        cfg=_cfg(static_rules=[{"name": "ws", "route": "dev", "when": {"text_matches_any": [r"^\s+urgent"]}}]),
        runtime={"model": "model-b", "provider": "p2"},
        complete_dev=_complete("NORMAL"),
    )
    assert decision.rule == "ws" and decision.outcome == "switch"


def test_static_rule_is_owner_non_bool_operand_never_matches(monkeypatch):
    # YAML string "false" is truthy — bool-coercing it would invert the
    # author's intent, so a non-bool operand must never match (B2).
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "999")  # sender u1 is NOT owner
    rules = [{"name": "guard", "route": "chat", "when": {"is_owner": {"eq": "false"}}}]
    decision = _evaluate(
        text="안녕", cfg=_cfg(static_rules=rules),
        runtime={"model": "model-a", "provider": "p1"},
        complete_dev=_complete("NORMAL"),
    )
    assert decision.rule is None  # fell through to the classifier


def test_evaluate_event_static_rule_applies_to_slash_commands():
    # Plugin parity: static runtime_overrides apply even for "/status" from a
    # non-owner — only the CLASSIFIER is skipped for slash commands.
    rules = [{"name": "slash-pin", "route": "dev", "when": {"text_matches_any": [r"^/status"]}}]
    decision = _evaluate(
        text="/status",
        cfg=_cfg(static_rules=rules),
        runtime={"model": "model-b", "provider": "p2"},
        complete_dev=MagicMock(side_effect=AssertionError("classifier must not run")),
    )
    assert decision is not None
    assert decision.rule == "slash-pin"
    assert decision.outcome == "switch"
    assert decision.record["policy"] == "static_rule"


def test_switch_directive_reason_never_blank(monkeypatch):
    # B4: a healthy default resolution has an empty resolve_route reason —
    # the directive gets the route name so log/notify text is never blank.
    cfg = _cfg()
    directive = mr_mod._resolve_route_directive("dev", cfg, _catalog(cfg))
    assert directive["reason"] == "dev"

    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        runtime={"model": "model-b", "provider": "p2"},
    )
    assert decision.outcome == "switch"
    assert decision.directive["reason"] == "dev"

    # Failover reasons from resolve_route are kept as-is.
    cfg2 = _cfg()
    cfg2["model_routes"]["routes"]["dev"]["fallbacks"] = [
        {"provider": "p2", "model": "model-c"},
    ]
    monkeypatch.setattr(
        "hermes_cli.model_routes.provider_health",
        lambda provider, model="", **kw: (
            (provider != "p1"), "HTTP 500" if provider == "p1" else "HTTP 200",
        ),
    )
    directive = mr_mod._resolve_route_directive("dev", cfg2, _catalog(cfg2))
    assert directive["model"] == "model-c"
    assert directive["reason"].startswith("failover")


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------


def test_log_decision_env_isolation_and_schema(monkeypatch, tmp_path):
    log_path = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(log_path))
    decision = _evaluate(complete_dev=_complete("SYSTEM_DEV"), mode="shadow")
    mr_mod.log_decision(decision.record, decision_log="/nonexistent/ignored.jsonl")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == EXPECTED_RECORD_FIELDS | {"ts"}
    assert isinstance(record["ts"], float)
    assert record["mode"] == "shadow"


def test_log_decision_default_path_under_hermes_home(monkeypatch):
    from hermes_constants import get_hermes_home

    monkeypatch.delenv("HERMES_MODEL_ROUTER_DECISION_LOG", raising=False)
    mr_mod.log_decision({"policy": "dev_routing"})
    default = get_hermes_home() / "logs" / "model_router_decisions.jsonl"
    assert default.exists()
    assert json.loads(default.read_text().splitlines()[0])["policy"] == "dev_routing"


def test_log_decision_swallows_write_errors(monkeypatch, tmp_path):
    target = tmp_path / "not-a-dir"
    target.write_text("file blocks parent mkdir")
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(target / "x.jsonl"))
    mr_mod.log_decision({"policy": "dev_routing"})  # must not raise


def test_configured_classifier_provider_and_model_are_forwarded(monkeypatch):
    calls = []

    def fake_classifier(user_prompt, **kwargs):
        calls.append((user_prompt, kwargs))
        return json.dumps({
            "evidence": "S5 configured classifier",
            "label": "SYSTEM_DEV",
            "confidence": 0.97,
        })

    monkeypatch.setattr(mr_mod, "_call_configured_classifier", fake_classifier)
    cfg = _cfg(router={"provider": "p1", "model": "router-v1"})
    decision = _evaluate(
        cfg=cfg,
        runtime={"model": "model-b", "provider": "p2"},
    )

    assert decision.record["source"] == "llm"
    assert decision.record["provider"] == "p1"
    assert decision.record["model"] == "router-v1"
    assert len(calls) == 1
    assert calls[0][1]["provider"] == "p1"
    assert calls[0][1]["model"] == "router-v1"


def test_configured_classifier_failure_falls_back_without_crashing(monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError("classifier credentials unavailable")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", unavailable)
    cfg = _cfg(router={"provider": "p1", "model": "router-v1"})
    decision = _evaluate(
        text="gateway 고장났어 디버깅해줘",
        cfg=cfg,
        runtime={"model": "model-b", "provider": "p2"},
    )

    assert decision.record["source"] == "fallback"
    assert decision.label == "SYSTEM_DEV"
    assert decision.outcome == "switch"


def test_decision_log_redacts_sensitive_fields_and_message_head(monkeypatch, tmp_path):
    log_path = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(log_path))
    secret = "sk-proj-this-must-not-be-logged"
    mr_mod.log_decision({
        "policy": "dev_routing",
        "api_key": secret,
        "nested": {"authorization": f"Bearer {secret}"},
        "evidence": f"S5 pasted credential {secret}",
        "notes": [f"https://example.test/callback?api_key={secret}"],
        "msg_head": f"OPENAI_API_KEY={secret}",
    })

    raw = log_path.read_text(encoding="utf-8")
    record = json.loads(raw)
    assert secret not in raw
    assert record["api_key"] == "[REDACTED]"
    assert record["nested"]["authorization"] == "[REDACTED]"
    assert secret not in record["evidence"]
    assert secret not in record["notes"][0]


def test_gateway_shadow_wiring_logs_without_runtime_mutation(monkeypatch):
    from gateway.run import GatewayRunner

    cfg = _cfg(router={"mode": "shadow"})
    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_state = {}
    runtime = {"model": "model-b", "provider": "p2", "api_mode": "chat_completions"}
    runtime_before = dict(runtime)
    fake_decision = SimpleNamespace(record={"policy": "dev_routing", "mode": "shadow"})
    evaluate = MagicMock(return_value=fake_decision)
    logged = MagicMock()
    monkeypatch.setattr(mr_mod, "evaluate_event", evaluate)
    monkeypatch.setattr(mr_mod, "log_decision", logged)

    result = runner._evaluate_model_router_shadow(
        event=_event(),
        session_key="canonical:session",
        runtime=runtime,
        user_config=cfg,
    )

    assert result is fake_decision
    assert runtime == runtime_before
    assert "_session_model_overrides" not in runner.__dict__
    assert evaluate.call_args.kwargs["mode"] == "shadow"
    assert evaluate.call_args.kwargs["session_key_override"] == "canonical:session"
    logged.assert_called_once_with(
        fake_decision.record,
        decision_log=evaluate.call_args.kwargs["catalog"].router.decision_log,
    )


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_gateway_non_shadow_modes_are_not_wired(monkeypatch, mode):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    evaluate = MagicMock(side_effect=AssertionError("router must not evaluate"))
    monkeypatch.setattr(mr_mod, "evaluate_event", evaluate)

    assert runner._evaluate_model_router_shadow(
        event=_event(),
        session_key="canonical:session",
        runtime={"model": "model-b"},
        user_config=_cfg(router={"mode": mode}),
    ) is None
    evaluate.assert_not_called()
