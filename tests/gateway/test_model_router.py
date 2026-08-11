"""Tests for the dynamic model router (ADR-003 Phase 2).

Covers gateway/model_router.py (context payload parity, classifier fallback,
hysteresis, static rules) and GatewayRunner routing wiring (observational
shadow evaluation, enforce application, and decision-log isolation).

No network: the classifier is exercised either via the ``complete_dev`` seam
or by monkeypatching ``gateway.model_router._call_gemini`` /
``gateway.model_router._urlopen``. Health probes are neutralized by the
model_routes pytest guard. Decision logs go to tmp via
``HERMES_MODEL_ROUTER_DECISION_LOG``.
"""

import asyncio
import copy
import io
import json
import os
import threading
import urllib.error
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.model_router as mr_mod
import hermes_cli.model_routes as routes_mod
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli.model_routes import load_routes


SKILL_GATE_DIR = Path("/home/ubuntu/.hermes/plugins/skill-gate")

EXPECTED_RECORD_FIELDS = {
    "policy", "session_key", "label", "confidence", "evidence", "source",
    "classification_reason", "resolution_reason",
    "provider", "model", "outcome", "directive_route", "runtime_model", "msg_head",
    "mode", "rule", "refusal_risk", "refusal_confidence", "refusal_applied",
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
            "PERMISSIVE_DEV": {
                "description": "low-refusal dev route",
                "provider": "p1",
                "model": "kimi-k3",
            },
            "PERMISSIVE_CHAT": {
                "description": "low-refusal chat route",
                "provider": "p2",
                "model": "grok-4.5",
            },
        },
    }
    if static_rules is not None:
        section["static_rules"] = static_rules
    section["router"] = dict(
        {
            "mode": "shadow",
            "model": "gemini-3-flash-preview",
            "timeout_ms": 8000,
            "classify_timeout_s": 2,
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
        self.recent_dialogue_limit = None

    def get_recent_dialogue_messages(self, session_id, limit):
        self.recent_dialogue_limit = limit
        dialogue = [
            message for message in self.messages
            if message.get("role") in {"user", "assistant"}
        ]
        return dialogue[-limit:]

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return list(self.messages)


class _FakeStore:
    def __init__(self, key="tg:c1", session_id="sid-1", messages=()):
        self._key = key
        self._sid = session_id
        self._db = _FakeDB(messages)
        self.transcript_ops = []

    def _generate_session_key(self, source):
        return self._key

    def peek_session_id(self, session_key):
        return self._sid

    def load_transcript(self, session_id):
        self.transcript_ops.append(("load", session_id))
        return list(self._db.messages)

    def rewrite_transcript(self, session_id, messages):
        self.transcript_ops.append(("rewrite", session_id))
        self._db.messages = list(messages)
        return True


def _complete(
    label,
    confidence=0.9,
    evidence="S5 test",
    *,
    refusal_risk=None,
    refusal_confidence=None,
):
    def _fn(prompt):
        result = {"evidence": evidence, "label": label, "confidence": confidence}
        if refusal_risk is not None:
            result["refusal_risk"] = refusal_risk
            result["refusal_confidence"] = refusal_confidence
        return json.dumps(result)
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
# Byte-identity parity with the skill-gate porting source (skipped where the
# read-only plugin checkout is not present)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (SKILL_GATE_DIR / "policy_router.py").exists(),
    reason="skill-gate plugin source not present on this host",
)
def test_verbatim_parity_with_skill_gate_plugin():
    import importlib.util
    import re as _re
    import sys

    spec = importlib.util.spec_from_file_location(
        "_sg_policy_router_for_parity", SKILL_GATE_DIR / "policy_router.py"
    )
    sg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sg
    try:
        spec.loader.exec_module(sg)
        old_prompt = mr_mod.DEV_SYSTEM_PROMPT.replace(
            mr_mod.DEV_REFUSAL_S0 + "\n\n", "", 1,
        ).replace("\n" + mr_mod.DEV_REFUSAL_EXAMPLE, "", 1)
        assert old_prompt == sg.DEV_SYSTEM_PROMPT
        old_schema = json.loads(json.dumps(mr_mod.DEV_RESPONSE_SCHEMA))
        old_schema["properties"].pop("refusal_risk")
        old_schema["properties"].pop("refusal_confidence")
        old_schema["required"] = ["evidence", "label", "confidence"]
        old_schema["propertyOrdering"] = ["evidence", "label", "confidence"]
        assert old_schema == sg.DEV_RESPONSE_SCHEMA
        assert mr_mod.DEV_CANDIDATE_RE.pattern == sg.DEV_CANDIDATE_RE.pattern
        assert mr_mod.DEV_CANDIDATE_RE.flags == sg.DEV_CANDIDATE_RE.flags
        assert mr_mod.FRONTEND_FALLBACK_RE.pattern == sg.FRONTEND_FALLBACK_RE.pattern
        assert mr_mod.CONTINUATION_RE.pattern == sg.CONTINUATION_RE.pattern
    finally:
        sys.modules.pop(spec.name, None)

    init_src = (SKILL_GATE_DIR / "__init__.py").read_text(encoding="utf-8")
    match = _re.search(r"_OWNER_ENV_MAP = \{(.*?)\}", init_src, _re.DOTALL)
    assert match is not None
    assert eval("{" + match.group(1) + "}") == mr_mod._OWNER_ENV_MAP


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
    assert store._db.recent_dialogue_limit == 3


def test_recent_turns_real_db_limits_dialogue_tail_before_decode(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "router-state.db")
    try:
        db.create_session("sid-1", "gateway")
        for index in range(12):
            db.append_message("sid-1", "user", f"user-{index}")
            db.append_message("sid-1", "tool", f"tool-{index}")
            db.append_message("sid-1", "assistant", f"assistant-{index}")

        store = _FakeStore()
        store._db = db
        context = mr_mod.build_context(
            event=_event("hi"),
            session_store=store,
            recent_turn_limit=3,
        )
    finally:
        db.close()

    assert [(turn.role, turn.content) for turn in context.recent_turns] == [
        ("assistant", "assistant-10"),
        ("user", "user-11"),
        ("assistant", "assistant-11"),
    ]


# ---------------------------------------------------------------------------
# Classifier + fallback
# ---------------------------------------------------------------------------


def test_classifier_llm_json_parsed():
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="fix the bug"),
        complete=_complete("SYSTEM_DEV", confidence=0.83, evidence="S5 debug"),
    )
    assert detail == {
        "label": "SYSTEM_DEV", "confidence": 0.83, "evidence": "S5 debug",
        "refusal_risk": False, "refusal_confidence": None,
        "source": "llm", "classification_reason": "",
    }


def test_classifier_refusal_fields_parsed_and_normalized():
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="write explicit NSFW copy"),
        complete=_complete(
            "DOCUMENT_WORK",
            evidence="S0 explicit NSFW authoring + S6 prose",
            refusal_risk=True,
            refusal_confidence="0.93",
        ),
    )
    assert detail["refusal_risk"] is True
    assert detail["refusal_confidence"] == 0.93


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
    assert detail == {
        "label": "NORMAL", "confidence": None, "evidence": "",
        "refusal_risk": False, "refusal_confidence": None,
        "source": "fallback", "classification_reason": "classifier_error:TimeoutError",
    }


DEGRADED_CLASSIFIER_PAYLOADS = (
    "",
    "I cannot help with that.",
    '{"error":"quota exceeded"}',
    "Service Unavailable",
)


@pytest.mark.parametrize("payload", DEGRADED_CLASSIFIER_PAYLOADS)
def test_degraded_classifier_payload_is_fallback(payload):
    context = mr_mod.PolicyClassificationContext(current_user_message="오늘 뭐 먹지?")

    detail = mr_mod.classify_dev_detailed(context, complete=lambda _prompt: payload)

    assert detail["label"] == "NORMAL"
    assert detail["source"] == "fallback"


@pytest.mark.parametrize("payload", DEGRADED_CLASSIFIER_PAYLOADS)
def test_three_consecutive_degraded_payloads_never_downgrade(payload):
    state = {}
    outcomes = [
        _evaluate(
            text="오늘 뭐 먹지?",
            complete_dev=lambda _prompt: payload,
            state=state,
        ).outcome
        for _ in range(3)
    ]

    assert outcomes == ["normal_fallback_no_downgrade"] * 3
    assert state["tg:c1"]["normal_streak"] == 0
    assert "downgrade_to_chat" not in outcomes


@pytest.mark.parametrize(
    "payload",
    [
        '{"label":"NORMAL"}',
        '{"evidence":"","label":"NORMAL","confidence":0.9}',
        '{"evidence":"S7 else","label":"NORMAL","confidence":"high"}',
        '{"evidence":"S7 else","label":"NORMAL","confidence":2}',
    ],
)
def test_incomplete_structured_normal_is_not_authoritative(payload):
    context = mr_mod.PolicyClassificationContext(current_user_message="오늘 뭐 먹지?")

    detail = mr_mod.classify_dev_detailed(context, complete=lambda _prompt: payload)

    assert detail["label"] == "NORMAL"
    assert detail["source"] == "fallback"


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


def test_classifier_key_read_uses_cached_scope_aware_env_resolver(monkeypatch):
    import hermes_cli.config as config_mod

    read = MagicMock(return_value=" scoped-key ")
    monkeypatch.setattr(config_mod, "get_env_value", read)

    assert mr_mod._read_env_key("GEMINI_API_KEY") == "scoped-key"
    read.assert_called_once_with("GEMINI_API_KEY")


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


def test_non_gemini_classifier_uses_strict_safe_schema_and_parses(monkeypatch):
    captured = {}
    shared_before = copy.deepcopy(mr_mod.DEV_RESPONSE_SCHEMA)

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        content = json.dumps({
            "evidence": "S5 configured provider",
            "label": "SYSTEM_DEV",
            "confidence": 0.96,
        })
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="fix the gateway"),
        provider="openai-api",
        model="gpt-test",
    )

    assert detail["label"] == "SYSTEM_DEV"
    assert detail["source"] == "llm"
    response_format = captured["extra_body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    strict_schema = response_format["json_schema"]["schema"]
    assert strict_schema is not mr_mod.DEV_RESPONSE_SCHEMA
    assert strict_schema["additionalProperties"] is False
    assert strict_schema["required"] == list(strict_schema["properties"])
    forbidden = {"propertyOrdering", "maxLength", "minimum", "maximum"}

    def assert_strict_safe(value):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                assert_strict_safe(child)
        elif isinstance(value, list):
            for child in value:
                assert_strict_safe(child)

    assert_strict_safe(strict_schema)
    assert mr_mod.DEV_RESPONSE_SCHEMA == shared_before


def test_overlong_evidence_is_truncated_not_discarded():
    """The wire schema drops maxLength for OpenAI-strict compat, so models
    may legally exceed 120 chars. The decision must survive with truncated
    evidence instead of collapsing into invalid_classifier_response."""
    payload = json.dumps(
        {"evidence": "S2 " + "e" * 140, "label": "NORMAL", "confidence": 0.92}
    )
    parsed = mr_mod._parse_dev_json(payload)
    assert parsed is not None
    assert parsed["label"] == "NORMAL"
    assert parsed["confidence"] == 0.92
    assert len(parsed["evidence"]) == 120
    assert parsed["evidence"].startswith("S2 ")

    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="x"),
        complete=lambda prompt: payload,
    )
    assert detail["source"] == "llm"
    assert detail["label"] == "NORMAL"
    # Truncated evidence stays within the authority boundary, so an
    # over-long-but-valid LLM NORMAL still advances hysteresis normally.
    assert mr_mod._is_authoritative_llm_decision(detail)


def test_whitespace_only_evidence_still_rejected():
    payload = json.dumps({"evidence": "   ", "label": "NORMAL", "confidence": 0.9})
    assert mr_mod._parse_dev_json(payload) is None


def test_plain_label_response_is_not_authoritative():
    assert mr_mod._parse_dev_json("FRONTEND_DEV") is None
    detail = mr_mod.classify_dev_detailed(
        mr_mod.PolicyClassificationContext(current_user_message="x"),
        complete=lambda prompt: "FRONTEND_DEV",
    )
    assert detail == {
        "label": "NORMAL", "confidence": None, "evidence": "",
        "refusal_risk": False, "refusal_confidence": None,
        "source": "fallback", "classification_reason": "invalid_classifier_response",
    }


def test_dev_schema_refusal_fields_are_required_and_ordered():
    schema = mr_mod.DEV_RESPONSE_SCHEMA
    assert schema["required"] == [
        "evidence", "label", "confidence", "refusal_risk", "refusal_confidence",
    ]
    assert schema["propertyOrdering"] == schema["required"]
    assert schema["properties"]["refusal_risk"] == {"type": "boolean"}
    assert schema["properties"]["refusal_confidence"] == {
        "type": "number", "minimum": 0, "maximum": 1,
    }


# ---------------------------------------------------------------------------
# Refusal-risk routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "refusal_route"),
    [
        ("SYSTEM_DEV", "PERMISSIVE_DEV"),
        ("FRONTEND_DEV", "PERMISSIVE_DEV"),
        ("DOCUMENT_WORK", "PERMISSIVE_CHAT"),
        ("NORMAL", "PERMISSIVE_CHAT"),
    ],
)
@pytest.mark.parametrize("refusal_risk", [False, True])
@pytest.mark.parametrize("refusal_confidence", [0.84, 0.9])
@pytest.mark.parametrize("enabled", [False, True])
def test_refusal_routing_matrix(
    label, refusal_route, refusal_risk, refusal_confidence, enabled,
):
    cfg = _cfg(router={
        "refusal": {
            "enabled": enabled,
            "min_confidence": 0.85,
            "dev_route": "PERMISSIVE_DEV",
            "chat_route": "PERMISSIVE_CHAT",
            "document_route": "",
        },
    })
    decision = _evaluate(
        complete_dev=_complete(
            label,
            refusal_risk=refusal_risk,
            refusal_confidence=refusal_confidence,
        ),
        runtime={"model": "model-z", "provider": "p1"},
        cfg=cfg,
    )
    should_route = enabled and refusal_risk and refusal_confidence >= 0.85
    if should_route:
        assert decision.outcome == "refusal_switch"
        assert decision.directive["route"] == refusal_route
        assert decision.record["refusal_applied"] is True
    else:
        expected_route = None if label == "NORMAL" else "dev"
        assert (decision.directive or {}).get("route") == expected_route
        assert decision.record["refusal_applied"] is False
    assert decision.record["refusal_risk"] is refusal_risk
    assert decision.record["refusal_confidence"] == refusal_confidence
    assert decision.record.get("refusal_below_threshold") is (
        True if enabled and refusal_risk and refusal_confidence < 0.85 else None
    )


def test_refusal_document_route_override():
    cfg = _cfg(router={
        "refusal": {
            "enabled": True,
            "document_route": "PERMISSIVE_DEV",
        },
    })
    decision = _evaluate(
        complete_dev=_complete(
            "DOCUMENT_WORK", refusal_risk=True, refusal_confidence=0.91,
        ),
        runtime={"model": "model-z", "provider": "p1"},
        cfg=cfg,
    )
    assert decision.outcome == "refusal_switch"
    assert decision.directive["route"] == "PERMISSIVE_DEV"


def test_refusal_fallback_source_never_routes(monkeypatch):
    monkeypatch.setattr(mr_mod, "classify_dev_detailed", lambda *a, **k: {
        "label": "SYSTEM_DEV",
        "confidence": 0.99,
        "evidence": "S0 hard cue + S5 code",
        "refusal_risk": True,
        "refusal_confidence": 0.99,
        "source": "fallback",
    })
    decision = _evaluate(
        cfg=_cfg(router={"refusal": {"enabled": True}}),
        runtime={"model": "model-z", "provider": "p1"},
    )
    assert decision.outcome == "switch"
    assert decision.directive["route"] == "dev"
    assert decision.record["refusal_applied"] is False


def test_refusal_evaluation_exception_keeps_normal_routing(monkeypatch):
    original = mr_mod._resolve_route_directive

    def _resolve(route_name, cfg, catalog):
        if route_name == "PERMISSIVE_DEV":
            raise RuntimeError("refusal route lookup failed")
        return original(route_name, cfg, catalog)

    monkeypatch.setattr(mr_mod, "_resolve_route_directive", _resolve)
    decision = _evaluate(
        complete_dev=_complete(
            "SYSTEM_DEV", refusal_risk=True, refusal_confidence=0.99,
        ),
        cfg=_cfg(router={"refusal": {"enabled": True}}),
        runtime={"model": "model-z", "provider": "p1"},
    )
    assert decision.outcome == "switch"
    assert decision.directive["route"] == "dev"
    assert decision.record["refusal_applied"] is False


def test_refusal_membership_exception_keeps_normal_routing(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.model_routes.runtime_satisfies_route",
        MagicMock(side_effect=RuntimeError("membership lookup failed")),
    )
    decision = _evaluate(
        complete_dev=_complete(
            "SYSTEM_DEV", refusal_risk=True, refusal_confidence=0.99,
        ),
        cfg=_cfg(router={"refusal": {"enabled": True}}),
        runtime={"model": "model-z", "provider": "p1"},
    )
    assert decision.outcome == "switch"
    assert decision.directive["route"] == "dev"
    assert decision.record["refusal_applied"] is False


def test_refusal_route_membership_is_absorbing_and_repromotes():
    cfg = _cfg(router={"refusal": {"enabled": True}})
    cfg["model_routes"]["routes"]["PERMISSIVE_DEV"]["accepted"] = [
        "kimi-k3", "permissive-member",
    ]
    state = {}
    outcomes = []
    for _ in range(3):
        decision = _evaluate(
            complete_dev=_complete(
                "SYSTEM_DEV", refusal_risk=True, refusal_confidence=0.99,
            ),
            cfg=cfg,
            state=state,
            runtime={"model": "permissive-member", "provider": "p1"},
        )
        outcomes.append(decision.outcome)
        assert decision.record["refusal_applied"] is True
    assert outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_to_primary",
    ]
    assert decision.directive["route"] == "PERMISSIVE_DEV"
    assert decision.directive["model"] == "kimi-k3"


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


@pytest.mark.parametrize(
    "detail",
    [
        {"label": "NORMAL", "confidence": None, "evidence": ""},
        {"label": "NORMAL", "confidence": 0.9, "evidence": "x" * 121},
        {"label": "UNKNOWN", "confidence": 0.9, "evidence": "S7 else"},
    ],
)
def test_hysteresis_revalidates_llm_authority_at_consumer(monkeypatch, detail):
    """A future parser cannot advance the streak by setting source alone."""
    detail = dict(detail, source="llm", classification_reason="")
    monkeypatch.setattr(
        mr_mod,
        "classify_dev_detailed",
        lambda *args, **kwargs: detail,
    )
    state = {}

    decision = _evaluate(text="오늘 뭐 먹지?", state=state)

    assert decision.outcome == "normal_fallback_no_downgrade"
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
# Re-promotion hysteresis (member → route primary)
# ---------------------------------------------------------------------------


def _member_cfg(*, router=None, static_rules=None):
    """Return routes with non-primary accepted members on dev and chat."""
    cfg = _cfg(router=router, static_rules=static_rules)
    cfg["model_routes"]["routes"]["dev"]["accepted"] = ["model-a", "model-alt"]
    cfg["model_routes"]["routes"]["chat"]["accepted"] = ["model-b", "grok-x"]
    return cfg


_MEMBER_RUNTIME = {"model": "model-alt", "provider": "p1"}


def test_repromote_streak_advances_and_emits_at_threshold():
    state = {}
    cfg = _member_cfg()
    outcomes = []
    for _ in range(3):
        decision = _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
        outcomes.append(decision.outcome)
    assert outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_to_primary",
    ]
    assert decision.directive["route"] == "dev"
    assert decision.directive["model"] == "model-a"
    assert decision.directive["reason"] == (
        "repromote to route primary after 3 accepted-member turns "
        "(model-alt -> model-a)"
    )
    assert set(decision.record) == EXPECTED_RECORD_FIELDS
    # Emission resets even in shadow, where the directive is not applied.
    assert state["tg:c1"]["repromote_streak"] == 0
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
    )
    assert decision.outcome == "noop_satisfied_repromote_1_of_3"


def test_repromote_fallback_label_never_advances():
    state = {}

    def _boom(prompt):
        raise TimeoutError("classifier down")

    cfg = _member_cfg()
    for _ in range(5):
        decision = _evaluate(
            text="gateway 고장났어 디버깅해줘",
            complete_dev=_boom,
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
        assert decision.outcome == "noop_satisfied"
        assert decision.record["source"] == "fallback"
    assert state["tg:c1"].get("repromote_streak", 0) == 0


def test_repromote_static_noop_always_advances(monkeypatch):
    rules = [
        {"name": "pin", "route": "dev", "when": {"text_matches_any": ["codex-lb"]}}
    ]
    cfg = _member_cfg(static_rules=rules)
    sentinel = MagicMock(side_effect=AssertionError("classifier must not run"))
    monkeypatch.setattr(mr_mod, "_call_gemini", sentinel)
    state = {}
    outcomes = [
        _evaluate(
            text="codex-lb 확인해줘",
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
            state=state,
        ).outcome
        for _ in range(3)
    ]
    sentinel.assert_not_called()
    assert outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_to_primary",
    ]


def test_repromote_streak_shared_across_static_and_classifier_paths():
    rules = [
        {"name": "pin", "route": "dev", "when": {"text_matches_any": ["codex-lb"]}}
    ]
    cfg = _member_cfg(static_rules=rules)
    state = {}
    for _ in range(2):
        _evaluate(
            text="codex-lb 확인해줘",
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
            state=state,
        )
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
        state=state,
    )
    assert decision.outcome == "repromote_to_primary"


def test_repromote_resets_on_primary_runtime():
    cfg = _member_cfg()
    state = {}
    for _ in range(2):
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
    assert state["tg:c1"]["repromote_streak"] == 2
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime={"model": "model-a", "provider": "p1"},
    )
    assert decision.outcome == "noop_satisfied"
    assert state["tg:c1"]["repromote_streak"] == 0
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
    )
    assert decision.outcome == "noop_satisfied_repromote_1_of_3"


def test_repromote_route_change_resets_then_advances():
    cfg = _member_cfg()
    cfg["model_routes"]["routes"]["doc"] = {
        "description": "doc route",
        "provider": "p1",
        "model": "model-d",
        "accepted": ["model-alt", "model-d"],
    }
    cfg["model_routes"]["router"]["label_routes"] = {
        "SYSTEM_DEV": "dev",
        "FRONTEND_DEV": "dev",
        "DOCUMENT_WORK": "doc",
    }
    state = {}
    for _ in range(2):
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
    assert state["tg:c1"] == {
        "normal_streak": 0,
        "repromote_streak": 2,
        "repromote_route": "dev",
    }
    decision = _evaluate(
        complete_dev=_complete("DOCUMENT_WORK"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
    )
    assert decision.outcome == "noop_satisfied_repromote_1_of_3"
    assert state["tg:c1"]["repromote_route"] == "doc"
    assert state["tg:c1"]["repromote_streak"] == 1


def test_repromote_resets_on_any_emission():
    cfg = _member_cfg()
    state = {}
    for _ in range(2):
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime={"model": "model-z", "provider": "p1"},
    )
    assert decision.outcome == "switch"
    assert state["tg:c1"]["repromote_streak"] == 0
    assert state["tg:c1"]["repromote_route"] == ""

    state = {}
    for _ in range(2):
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
    for expected in (
        "normal_streak_1_of_3",
        "normal_streak_2_of_3",
        "downgrade_to_chat",
    ):
        decision = _evaluate(
            complete_dev=_complete("NORMAL"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
        assert decision.outcome == expected
    assert state["tg:c1"]["repromote_streak"] == 0


def test_repromote_held_when_primary_unhealthy(monkeypatch):
    cfg = _member_cfg()
    state = {}
    unhealthy = {
        "route": "dev",
        "provider": "p2",
        "model": "model-b",
        "reasoning_effort": "",
        "source": "fallback:1",
        "reason": "failover — p1 unhealthy (HTTP 500)",
    }
    monkeypatch.setattr(
        mr_mod,
        "_resolve_route_directive",
        lambda *args, **kwargs: dict(unhealthy),
    )
    outcomes = [
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        ).outcome
        for _ in range(4)
    ]
    assert outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_held",
        "repromote_held",
    ]
    assert state["tg:c1"]["repromote_streak"] == 3

    healthy = {
        "route": "dev",
        "provider": "p1",
        "model": "model-a",
        "reasoning_effort": "xhigh",
        "source": "default",
        "reason": "dev",
    }
    monkeypatch.setattr(
        mr_mod,
        "_resolve_route_directive",
        lambda *args, **kwargs: dict(healthy),
    )
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
    )
    assert decision.outcome == "repromote_to_primary"
    assert decision.directive["model"] == "model-a"


def test_repromote_held_when_resolution_matches_runtime(monkeypatch):
    cfg = _member_cfg()
    state = {
        "tg:c1": {
            "normal_streak": 0,
            "repromote_streak": 2,
            "repromote_route": "dev",
        }
    }
    same = {
        "route": "dev",
        "provider": "p1",
        "model": "model-alt",
        "reasoning_effort": "",
        "source": "default",
        "reason": "dev",
    }
    monkeypatch.setattr(
        mr_mod,
        "_resolve_route_directive",
        lambda *args, **kwargs: dict(same),
    )
    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
    )
    assert decision.outcome == "repromote_held"
    assert decision.directive is None


def test_repromote_chat_member_via_noop_already_chat():
    cfg = _member_cfg()
    state = {}
    runtime = {"model": "grok-x", "provider": "p2"}
    outcomes = []
    for _ in range(3):
        decision = _evaluate(
            complete_dev=_complete("NORMAL"),
            state=state,
            cfg=cfg,
            runtime=runtime,
        )
        outcomes.append(decision.outcome)
    assert outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_to_primary",
    ]
    assert decision.directive["route"] == "chat"
    assert decision.directive["model"] == "model-b"
    assert decision.directive["reason"] == (
        "repromote to route primary after 3 accepted-member turns (grok-x -> model-b)"
    )
    assert state["tg:c1"]["normal_streak"] == 3
    assert state["tg:c1"]["repromote_streak"] == 0


def test_repromote_chat_plain_outcomes_untouched():
    cfg = _member_cfg()
    state = {}
    decision = _evaluate(
        complete_dev=_complete("NORMAL"),
        cfg=cfg,
        state=state,
        runtime={"model": "model-b", "provider": "p2"},
    )
    assert decision.outcome == "noop_already_chat"
    assert state["tg:c1"].get("repromote_streak", 0) == 0

    def _boom(prompt):
        raise TimeoutError("down")

    decision = _evaluate(
        text="오늘 뭐 먹지?",
        complete_dev=_boom,
        cfg=cfg,
        state=state,
        runtime={"model": "grok-x", "provider": "p2"},
    )
    assert decision.outcome == "noop_already_chat"
    assert state["tg:c1"].get("repromote_streak", 0) == 0


def test_repromote_disabled_by_zero_threshold():
    cfg = _member_cfg()
    cfg["model_routes"]["routes"]["dev"]["repromote_after_turns"] = 0
    state = {}
    for _ in range(4):
        decision = _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
        assert decision.outcome == "noop_satisfied"
    assert state["tg:c1"].get("repromote_streak", 0) == 0

    cfg = _member_cfg(router={"repromote_after_turns": 0})
    state = {}
    for _ in range(4):
        decision = _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
        )
        assert decision.outcome == "noop_satisfied"


def test_repromote_route_override_wins_over_router_value():
    def _outcomes(cfg):
        state = {}
        return [
            _evaluate(
                complete_dev=_complete("SYSTEM_DEV"),
                state=state,
                cfg=cfg,
                runtime=_MEMBER_RUNTIME,
            ).outcome
            for _ in range(2)
        ]

    cfg = _member_cfg(router={"repromote_after_turns": 5})
    cfg["model_routes"]["routes"]["dev"]["repromote_after_turns"] = 2
    assert _outcomes(cfg) == [
        "noop_satisfied_repromote_1_of_2",
        "repromote_to_primary",
    ]

    cfg = _member_cfg(router={"repromote_after_turns": 0})
    cfg["model_routes"]["routes"]["dev"]["repromote_after_turns"] = 2
    assert _outcomes(cfg) == [
        "noop_satisfied_repromote_1_of_2",
        "repromote_to_primary",
    ]


def test_repromote_recovery_probe_is_enforce_only(monkeypatch, tmp_path):
    """Shadow holds on cached health; enforce may probe a stale primary."""
    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH_TEST", "1")
    monkeypatch.setattr(routes_mod, "_last_passive_unhealthy_write", {})
    monkeypatch.setattr(routes_mod, "_unhealthy_memo", {"mtime": None, "value": False})
    health_path = tmp_path / "health.json"
    cfg = _member_cfg()
    cfg["model_routes"]["health"] = {
        "cache_path": str(health_path),
        "fail_ttl_seconds": 1,
    }
    catalog = _catalog(cfg)
    clock = {"now": 1000.0}
    monkeypatch.setattr(routes_mod, "_now", lambda: clock["now"])
    routes_mod.record_provider_outcome(
        "p1",
        False,
        "server_error",
        health=catalog.health,
    )
    before = health_path.read_bytes()
    clock["now"] += catalog.health.fail_ttl_seconds + 1

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "base_url": "https://p1.example/v1",
            "api_key": "test-key",
            "api_mode": "chat_completions",
        },
    )
    auth_error = urllib.error.HTTPError(
        "https://p1.example/v1/models",
        401,
        "Unauthorized",
        None,
        io.BytesIO(b'{"error":"unauthorized"}'),
    )
    urlopen = MagicMock(side_effect=auth_error)
    monkeypatch.setattr(routes_mod, "_urlopen", urlopen)

    state = {}
    shadow_outcomes = [
        _evaluate(
            complete_dev=_complete("SYSTEM_DEV"),
            state=state,
            cfg=cfg,
            runtime=_MEMBER_RUNTIME,
            mode="shadow",
        ).outcome
        for _ in range(3)
    ]
    assert shadow_outcomes == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_held",
    ]
    urlopen.assert_not_called()
    assert health_path.read_bytes() == before

    decision = _evaluate(
        complete_dev=_complete("SYSTEM_DEV"),
        state=state,
        cfg=cfg,
        runtime=_MEMBER_RUNTIME,
        mode="enforce",
    )
    assert decision.outcome == "repromote_to_primary"
    assert decision.directive["source"] == "default"
    urlopen.assert_called_once()
    assert json.loads(health_path.read_text(encoding="utf-8"))["p1"]["healthy"] is True


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_log_decision_creates_private_file_and_parent(monkeypatch, tmp_path):
    log_path = tmp_path / "private-logs" / "decisions.jsonl"
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(log_path))

    mr_mod.log_decision({"sequence": 1})

    assert (log_path.parent.stat().st_mode & 0o777) == 0o700
    assert (log_path.stat().st_mode & 0o777) == 0o600
    lock_path = log_path.with_name(f"{log_path.name}.lock")
    assert (lock_path.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_log_decision_migrates_existing_0644_file(monkeypatch, tmp_path):
    log_path = tmp_path / "existing" / "decisions.jsonl"
    log_path.parent.mkdir()
    log_path.write_text('{"sequence":0}\n', encoding="utf-8")
    log_path.chmod(0o644)
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(log_path))

    mr_mod.log_decision({"sequence": 1})

    assert (log_path.stat().st_mode & 0o777) == 0o600
    assert [json.loads(line)["sequence"] for line in log_path.read_text().splitlines()] == [0, 1]


def test_log_decision_rotates_by_size_and_keeps_three(monkeypatch, tmp_path):
    log_path = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("HERMES_MODEL_ROUTER_DECISION_LOG", str(log_path))
    monkeypatch.setattr(mr_mod, "_DECISION_LOG_MAX_BYTES", 1)

    for sequence in range(5):
        mr_mod.log_decision({"sequence": sequence})

    def sequence(path):
        return json.loads(path.read_text(encoding="utf-8"))["sequence"]

    assert sequence(log_path) == 4
    assert sequence(log_path.with_name(f"{log_path.name}.1")) == 3
    assert sequence(log_path.with_name(f"{log_path.name}.2")) == 2
    assert sequence(log_path.with_name(f"{log_path.name}.3")) == 1
    assert not log_path.with_name(f"{log_path.name}.4").exists()


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
    assert decision.record["classification_reason"] == "classifier_error:RuntimeError"
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
    assert "_sessions" not in runner.__dict__
    assert evaluate.call_args.kwargs["mode"] == "shadow"
    assert evaluate.call_args.kwargs["session_key_override"] == "canonical:session"
    logged.assert_called_once_with(
        fake_decision.record,
        decision_log=evaluate.call_args.kwargs["catalog"].router.decision_log,
    )


def test_gateway_route_catalog_cache_invalidates_on_relevant_config_reload(
    monkeypatch,
):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    cfg = _cfg(router={"mode": "shadow"})
    load = MagicMock(wraps=routes_mod.load_routes)
    monkeypatch.setattr(routes_mod, "load_routes", load)

    first = runner._model_route_catalog(cfg)
    same_content = runner._model_route_catalog(copy.deepcopy(cfg))
    irrelevant = copy.deepcopy(cfg)
    irrelevant["display"] = {"compact": True}
    same_routes = runner._model_route_catalog(irrelevant)

    changed = copy.deepcopy(cfg)
    changed["model_routes"]["router"]["recent_turns"] = 9
    reloaded = runner._model_route_catalog(changed)

    monkeypatch.setenv("HERMES_MODEL_ROUTER_MODE", "off")
    env_reloaded = runner._model_route_catalog(changed)

    assert same_content is first
    assert same_routes is first
    assert reloaded is not first
    assert reloaded.router.recent_turns == 9
    assert env_reloaded is not reloaded
    assert env_reloaded.router.mode == "off"
    assert load.call_count == 3


def test_gateway_shadow_turn_does_not_wait_for_hung_classifier(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    returned = threading.Event()
    scheduled = {}

    def hang(**_kwargs):
        started.set()
        release.wait(5)
        finished.set()
        return SimpleNamespace(record={"mode": "shadow"})

    monkeypatch.setattr(runner, "_evaluate_model_router_shadow", hang)

    def schedule_from_turn():
        scheduled["value"] = runner._schedule_model_router_shadow(
            event=_event(),
            session_key="tg:c1",
            runtime={"model": "model-b"},
            user_config=_cfg(router={"mode": "shadow"}),
        )
        returned.set()

    caller = threading.Thread(target=schedule_from_turn, daemon=True)
    caller.start()
    try:
        assert started.wait(2)
        assert returned.wait(2), "the user turn waited for shadow classification"
    finally:
        release.set()
    caller.join(2)
    assert finished.wait(2)
    assert scheduled["value"] is True


def test_gateway_shadow_worker_inherits_turn_context(monkeypatch):
    from gateway.run import GatewayRunner

    marker = ContextVar("router_profile_marker", default="default")
    token = marker.set("profile-a")
    runner = object.__new__(GatewayRunner)
    observed = {}
    finished = threading.Event()

    def evaluate(**_kwargs):
        observed["marker"] = marker.get()
        finished.set()
        return None

    monkeypatch.setattr(runner, "_evaluate_model_router_shadow", evaluate)
    try:
        assert runner._schedule_model_router_shadow(
            event=_event(),
            session_key="tg:c1",
            runtime={"model": "model-b"},
            user_config=_cfg(router={"mode": "shadow"}),
        ) is True
        assert finished.wait(2)
    finally:
        marker.reset(token)

    assert observed["marker"] == "profile-a"


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [
        ("shadow", "off", "off"),
        ("off", "shadow", "shadow"),
        ("shadow", "enforce", "enforce"),
        ("enforce", "invalid", "off"),
    ],
)
def test_gateway_router_mode_env_bridge_takes_precedence(
    monkeypatch, configured, override, expected,
):
    from gateway.run import _model_router_mode

    monkeypatch.setenv("HERMES_MODEL_ROUTER_MODE", override)
    assert _model_router_mode(_cfg(router={"mode": configured})) == expected


def test_gateway_enforce_apply_records_and_rebinds_exact_route_intent(monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_cli.model_switch import ModelSwitchResult

    runner = object.__new__(GatewayRunner)
    runner._evict_cached_agent = MagicMock()
    switch = MagicMock(return_value=ModelSwitchResult(
        success=True,
        new_model="model-a",
        target_provider="p1",
        api_key="test-key",
        base_url="https://p1.example/v1",
        api_mode="chat_completions",
    ))
    monkeypatch.setattr("hermes_cli.model_switch.switch_model", switch)
    directive = {
        "route": "dev",
        "provider": "p1",
        "model": "model-a",
        "reasoning_effort": "xhigh",
        "reason": "dev",
    }

    assert asyncio.run(
        runner._apply_model_router_directive(
            "tg:c1",
            directive,
            _cfg(router={"mode": "enforce"}),
            source=_source(),
        )
    ) == (True, True)

    conversation = runner._session_state("tg:c1").conversation
    assert conversation.active_route_name == "dev"
    assert conversation.model_override["model"] == "model-a"
    rebuilt_agent = SimpleNamespace()
    runner._bind_active_model_route(rebuilt_agent, "tg:c1")
    assert rebuilt_agent._active_route_name == "dev"
    runner._evict_cached_agent.assert_called_once_with("tg:c1")


def test_gateway_enforce_authoritative_noop_records_route_intent(monkeypatch):
    from gateway.run import GatewayRunner

    cfg = _cfg(router={"mode": "enforce"})
    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_runtime_snapshot = MagicMock(
        return_value={"model": "model-a", "provider": "p1"},
    )
    decision = SimpleNamespace(
        directive=None,
        outcome="noop_satisfied",
        label="SYSTEM_DEV",
        rule=None,
        record={"source": "llm"},
    )
    runner._classify_model_router_with_budget = AsyncMock(return_value=decision)
    monkeypatch.setattr(mr_mod, "log_decision", MagicMock())

    result = asyncio.run(
        runner._model_router_stage(
            _event(),
            _source(),
            "tg:c1",
            mode="enforce",
            user_config=cfg,
        )
    )

    assert result is decision
    assert runner._session_state("tg:c1").conversation.active_route_name == "dev"


def _refusal_stage_runner(
    monkeypatch, tmp_path, *, notify=True, clean_fork=True, messages=(),
):
    from gateway.run import GatewayRunner

    cfg = _cfg(router={
        "mode": "enforce",
        "refusal": {
            "enabled": True,
            "notify": notify,
            "clean_fork": clean_fork,
            "keep_user_turns": 2,
        },
    })
    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_runtime_snapshot = MagicMock(
        return_value={"model": "model-z", "provider": "p1"},
    )
    runner.session_store._db.messages = list(messages)
    evidence = "S0 hard refusal cue + S5 code " + "x" * 100
    monkeypatch.setattr(
        mr_mod,
        "_call_configured_classifier",
        lambda *a, **k: json.dumps({
            "evidence": evidence,
            "label": "SYSTEM_DEV",
            "confidence": 0.97,
            "refusal_risk": True,
            "refusal_confidence": 0.93,
        }),
    )
    runner._apply_model_router_directive = AsyncMock(return_value=(True, False))
    runner._deliver_platform_notice = AsyncMock()
    monkeypatch.setenv(
        "HERMES_MODEL_ROUTER_DECISION_LOG",
        str(tmp_path / "refusal-decisions.jsonl"),
    )
    return runner, cfg, evidence


def test_refusal_notify_sent_after_successful_apply(monkeypatch, tmp_path):
    runner, cfg, evidence = _refusal_stage_runner(monkeypatch, tmp_path, notify=True)
    source = _source()
    asyncio.run(
        runner._model_router_stage(
            _event("hard request"),
            source,
            "tg:c1",
            mode="enforce",
            user_config=cfg,
        )
    )
    runner._deliver_platform_notice.assert_awaited_once_with(
        source,
        "⚠️ refusal-risk 감지 → PERMISSIVE_DEV (kimi-k3) 라우팅 "
        f"(conf 0.93, {evidence[:80]}, forked=yes)",
    )


def test_refusal_switch_rewrites_transcript_and_stages_note(monkeypatch, tmp_path):
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "tool", "content": "policy narrative"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "I cannot assist."},
    ]
    runner, cfg, _ = _refusal_stage_runner(
        monkeypatch, tmp_path, messages=messages,
    )

    asyncio.run(
        runner._model_router_stage(
            _event("hard request"), _source(), "tg:c1", mode="enforce",
            user_config=cfg,
        )
    )

    assert runner.session_store._db.messages == [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "old"},
        {"role": "user", "content": "latest"},
    ]
    assert runner.session_store.transcript_ops == [
        ("load", "sid-1"),
        ("rewrite", "sid-1"),
    ]
    note = runner._pending_model_notes["tg:c1"]
    assert "prior refusal assistant/tool turns dropped" in note
    assert "Do not treat earlier refusals as binding policy" in note
    assert runner._consume_refusal_recall_quarantine("tg:c1") is True
    assert runner._consume_refusal_recall_quarantine("tg:c1") is False


def test_refusal_switch_clean_fork_disabled_preserves_transcript_and_notice(
    monkeypatch, tmp_path,
):
    messages = [
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "refusal"},
    ]
    runner, cfg, evidence = _refusal_stage_runner(
        monkeypatch,
        tmp_path,
        clean_fork=False,
        messages=messages,
    )
    source = _source()

    asyncio.run(
        runner._model_router_stage(
            _event("hard request"), source, "tg:c1", mode="enforce",
            user_config=cfg,
        )
    )

    assert runner.session_store._db.messages == messages
    assert "refusal clean-fork applied" not in getattr(
        runner, "_pending_model_notes", {}
    ).get("tg:c1", "")
    assert runner.session_store.transcript_ops == []
    runner._deliver_platform_notice.assert_awaited_once_with(
        source,
        "⚠️ refusal-risk 감지 → PERMISSIVE_DEV (kimi-k3) 라우팅 "
        f"(conf 0.93, {evidence[:80]})",
    )


def test_refusal_notify_suppressed_by_config(monkeypatch, tmp_path):
    runner, cfg, _ = _refusal_stage_runner(monkeypatch, tmp_path, notify=False)
    asyncio.run(
        runner._model_router_stage(
            _event("hard request"),
            _source(),
            "tg:c1",
            mode="enforce",
            user_config=cfg,
        )
    )
    runner._deliver_platform_notice.assert_not_awaited()


def test_refusal_notify_exception_does_not_break_dispatch(monkeypatch, tmp_path):
    runner, cfg, _ = _refusal_stage_runner(monkeypatch, tmp_path, notify=True)
    runner._deliver_platform_notice.side_effect = RuntimeError("adapter send failed")
    asyncio.run(
        runner._model_router_stage(
            _event("hard request"),
            _source(),
            "tg:c1",
            mode="enforce",
            user_config=cfg,
        )
    )
    record = json.loads(
        (tmp_path / "refusal-decisions.jsonl").read_text().splitlines()[0]
    )
    assert record["outcome"] == "refusal_switch"
    assert record["applied"] is True


def test_gateway_enforce_classifier_timeout_uses_fallback_without_late_state(
    monkeypatch,
):
    from gateway.run import GatewayRunner

    cfg = _cfg(router={"mode": "enforce", "classify_timeout_s": 0.05})
    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_runtime_snapshot = MagicMock(
        return_value={"model": "model-b", "provider": "p2"},
    )
    runner._apply_model_router_directive = AsyncMock(return_value=(True, False))
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def hanging_classifier(_context, **_kwargs):
        started.set()
        release.wait(5)
        finished.set()
        return {
            "label": "NORMAL",
            "confidence": 0.99,
            "evidence": "S1 ordinary chat",
            "source": "llm",
            "classification_reason": "",
        }

    monkeypatch.setattr(mr_mod, "classify_dev_detailed", hanging_classifier)
    monkeypatch.setattr(mr_mod, "log_decision", MagicMock())

    async def scenario():
        task = asyncio.create_task(
            runner._model_router_stage(
                _event("gateway 버그 고쳐줘"),
                _source(),
                "tg:c1",
                mode="enforce",
                user_config=cfg,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            decision = await asyncio.wait_for(task, timeout=2)
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.sleep(0)
        return decision

    decision = asyncio.run(scenario())
    assert decision.record["source"] == "fallback"
    assert decision.record["classification_reason"] == "classifier_timeout"
    assert decision.label == "SYSTEM_DEV"
    assert runner._model_router_state["tg:c1"]["normal_streak"] == 0


def test_enforce_state_transaction_rejects_stale_shadow_commit():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    key, shared, shadow_identity, shadow_local = (
        runner._begin_model_router_state("tg:c1", invalidate=False)
    )
    shadow_local[key]["normal_streak"] = 9

    enforce_key, enforce_shared, _enforce_identity, _enforce_local = (
        runner._begin_model_router_state("tg:c1", invalidate=True)
    )

    assert enforce_key == key
    assert enforce_shared is shared
    assert runner._commit_model_router_state(
        key, shared, shadow_identity, shadow_local
    ) is False
    assert shared[key]["normal_streak"] == 0


def test_gateway_enforce_applies_repromote_once_at_threshold(monkeypatch):
    from gateway.run import GatewayRunner

    cfg = _member_cfg(router={"mode": "enforce"})
    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_runtime_snapshot = MagicMock(return_value=_MEMBER_RUNTIME)
    runner._apply_model_router_directive = AsyncMock(return_value=(True, False))
    logged = MagicMock()
    monkeypatch.setattr(mr_mod, "log_decision", logged)
    monkeypatch.setattr(
        mr_mod,
        "_call_configured_classifier",
        lambda *args, **kwargs: json.dumps({
            "evidence": "S5 routed dev work",
            "label": "SYSTEM_DEV",
            "confidence": 0.9,
        }),
    )

    decisions = [
        asyncio.run(
            runner._model_router_stage(
                _event("gateway 버그 고쳐줘"),
                _source(),
                "tg:c1",
                mode="enforce",
                user_config=cfg,
            )
        )
        for _ in range(3)
    ]

    assert [decision.outcome for decision in decisions] == [
        "noop_satisfied_repromote_1_of_3",
        "noop_satisfied_repromote_2_of_3",
        "repromote_to_primary",
    ]
    runner._apply_model_router_directive.assert_awaited_once()
    applied_directive = runner._apply_model_router_directive.await_args.args[1]
    assert applied_directive["route"] == "dev"
    assert applied_directive["model"] == "model-a"
    records = [call.args[0] for call in logged.call_args_list]
    assert [record["applied"] for record in records] == [False, False, True]
    assert records[-1]["reasoning_applied"] is False
    assert runner._session_state("tg:c1").conversation.active_route_name == "dev"


@pytest.mark.parametrize(
    "outcome",
    ["noop_satisfied_repromote_1_of_3", "repromote_held"],
)
def test_gateway_chat_repromote_noops_preserve_chat_route_intent(outcome):
    from gateway.run import GatewayRunner

    router = _catalog(_member_cfg()).router
    decision = SimpleNamespace(
        directive=None,
        outcome=outcome,
        label="NORMAL",
        rule=None,
    )

    assert GatewayRunner._selected_model_route(decision, router) == "chat"


def test_gateway_refusal_noop_preserves_permissive_route_intent():
    from gateway.run import GatewayRunner

    router = _catalog(_cfg(router={"refusal": {"enabled": True}})).router
    decision = SimpleNamespace(
        directive=None,
        outcome="noop_satisfied",
        label="SYSTEM_DEV",
        rule=None,
        record={"refusal_applied": True},
    )

    assert GatewayRunner._selected_model_route(decision, router) == "PERMISSIVE_DEV"


def test_gateway_shadow_resolution_never_probes_or_rewrites_live_health(
    monkeypatch, tmp_path,
):
    """A stale live verdict remains authoritative during shadow observation."""
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH_TEST", "1")
    monkeypatch.setattr(routes_mod, "_last_passive_unhealthy_write", {})
    monkeypatch.setattr(routes_mod, "_unhealthy_memo", {"mtime": None, "value": False})
    health_path = tmp_path / "model_route_health.json"
    decision_log = tmp_path / "router-decisions.jsonl"
    cfg = _cfg(router={"mode": "shadow", "decision_log": str(decision_log)})
    cfg["model_routes"]["health"] = {
        "cache_path": str(health_path),
        "fail_ttl_seconds": 1,
    }
    catalog = _catalog(cfg)
    clock = {"now": 1000.0}
    monkeypatch.setattr(routes_mod, "_now", lambda: clock["now"])
    routes_mod.record_provider_outcome(
        "p1", False, "server_error", health=catalog.health,
    )
    before = health_path.read_bytes()
    assert json.loads(before)["p1"]["healthy"] is False
    clock["now"] += catalog.health.fail_ttl_seconds + 1

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "base_url": "https://p1.example/v1",
            "api_key": "test-key",
            "api_mode": "chat_completions",
        },
    )
    auth_error = urllib.error.HTTPError(
        "https://p1.example/v1/models",
        401,
        "Unauthorized",
        None,
        io.BytesIO(b'{"error":"unauthorized"}'),
    )
    urlopen = MagicMock(side_effect=auth_error)
    monkeypatch.setattr(routes_mod, "_urlopen", urlopen)
    monkeypatch.setattr(
        mr_mod,
        "_call_configured_classifier",
        lambda *args, **kwargs: json.dumps({
            "evidence": "S5 debug request",
            "label": "SYSTEM_DEV",
            "confidence": 0.99,
        }),
    )

    runner = object.__new__(GatewayRunner)
    runner.session_store = _FakeStore()
    runner._model_router_state = {}
    decision = runner._evaluate_model_router_shadow(
        event=_event(),
        session_key="canonical:session",
        runtime={"model": "model-b", "provider": "p2"},
        user_config=cfg,
    )

    urlopen.assert_not_called()
    assert health_path.read_bytes() == before
    verdict = json.loads(health_path.read_text(encoding="utf-8"))["p1"]
    assert verdict["healthy"] is False
    assert verdict["reason"] == "passive: server_error"
    assert "recovery probe suppressed" in decision.record["resolution_reason"]
    assert json.loads(decision_log.read_text(encoding="utf-8"))["mode"] == "shadow"


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


def test_strip_code_fences_normalizes_markdown_wrapped_classifier_reply():
    from gateway.model_router import _strip_code_fences

    fenced = "```json\n{\"label\": \"NORMAL\"}\n```"
    assert _strip_code_fences(fenced) == "{\"label\": \"NORMAL\"}"
    # No fences: untouched apart from whitespace.
    assert _strip_code_fences("  {\"label\": \"SYSTEM_DEV\"}\n") == "{\"label\": \"SYSTEM_DEV\"}"
    # Unterminated fence: keep the body rather than returning empty.
    assert _strip_code_fences("```json\n{\"label\": \"NORMAL\"}") == "{\"label\": \"NORMAL\"}"
    # Fence with no body degrades to the original stripped text.
    assert _strip_code_fences("```\n```") == "```\n```"
