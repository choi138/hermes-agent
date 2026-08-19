"""M2 mood-file loading and call-time-only tone injection."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import gateway.model_router as model_router
from agent.system_prompt import compose_effective_system_prompt
from gateway.config import Platform
from gateway.mood_loader import MAX_MOOD_BYTES, load_mood_file
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner, _mood_prompt_suffix
from gateway.session import SessionSource
from hermes_cli.model_routes import MoodsConfig


def _moods(tmp_path, *, enabled=True, threshold=0.7) -> MoodsConfig:
    return MoodsConfig(
        enabled=enabled,
        dir=str(tmp_path),
        confidence_threshold=threshold,
    )


def _record(*, mood="care", confidence=0.9) -> dict:
    return {
        "mood": mood,
        "mood_confidence": confidence,
        "mood_applied": "shadow",
    }


def _gateway_cfg(tmp_path, *, enabled=True, threshold=0.7) -> dict:
    return {
        "providers": {
            "p1": {"base_url": "https://p1.example/v1"},
            "p2": {"base_url": "https://p2.example/v1"},
        },
        "model_routes": {
            "routes": {
                "dev": {
                    "description": "dev",
                    "provider": "p1",
                    "model": "model-a",
                },
                "chat": {
                    "description": "chat",
                    "provider": "p2",
                    "model": "model-b",
                },
            },
            "router": {
                "mode": "shadow",
                "chat_route": "chat",
                "label_routes": {
                    "SYSTEM_DEV": "dev",
                    "FRONTEND_DEV": "dev",
                    "DOCUMENT_WORK": "dev",
                },
            },
            "moods": {
                "enabled": enabled,
                "dir": str(tmp_path),
                "confidence_threshold": threshold,
            },
        },
    }


def _event() -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text="도와줘", message_id="m1", source=source)


def test_loader_happy_path(tmp_path):
    content = "Respond with calm warmth.\n"
    (tmp_path / "care.md").write_text(content, encoding="utf-8")

    assert load_mood_file(_moods(tmp_path), "care") == content


def test_loader_missing_and_empty_files_return_none(tmp_path):
    assert load_mood_file(_moods(tmp_path), "care") is None

    (tmp_path / "care.md").write_text(" \n\t", encoding="utf-8")
    assert load_mood_file(_moods(tmp_path), "care") is None


def test_loader_truncates_oversize_file_at_eight_kib(tmp_path):
    (tmp_path / "focused.md").write_bytes(b"x" * MAX_MOOD_BYTES + b"discarded")

    content = load_mood_file(_moods(tmp_path), "focused")

    assert content == "x" * MAX_MOOD_BYTES
    assert len(content.encode("utf-8")) == MAX_MOOD_BYTES


def test_loader_expands_tilde(monkeypatch, tmp_path):
    home = tmp_path / "operator-home"
    moods_dir = home / "profile-moods"
    moods_dir.mkdir(parents=True)
    (moods_dir / "cute.md").write_text("Keep it gentle.", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    moods = MoodsConfig(enabled=True, dir="~/profile-moods")
    assert load_mood_file(moods, "cute") == "Keep it gentle."


def test_disabled_moods_remain_shadow_without_injection(tmp_path):
    (tmp_path / "care.md").write_text("Warm tone.", encoding="utf-8")
    record = _record()

    assert _mood_prompt_suffix(record, _moods(tmp_path, enabled=False)) == ""
    assert record["mood_applied"] == "shadow"


def test_enabled_confident_mood_uses_exact_suffix(tmp_path):
    (tmp_path / "playful.md").write_text("Use light humor.", encoding="utf-8")
    record = _record(mood="playful", confidence=0.91)

    suffix = _mood_prompt_suffix(record, _moods(tmp_path))

    assert suffix == "\n\n# Current Mood: playful\nUse light humor."
    assert record["mood_applied"] == "injection"


def test_enabled_low_confidence_mood_is_not_applied(tmp_path):
    (tmp_path / "care.md").write_text("Warm tone.", encoding="utf-8")
    record = _record(confidence=0.69)

    assert _mood_prompt_suffix(record, _moods(tmp_path, threshold=0.7)) == ""
    assert record["mood_applied"] == "none"


def test_enabled_missing_file_is_not_applied(tmp_path):
    record = _record()

    assert _mood_prompt_suffix(record, _moods(tmp_path)) == ""
    assert record["mood_applied"] == "none"


def test_enabled_invalid_mood_is_not_applied(tmp_path):
    (tmp_path / "grumpy.md").write_text("This must not load.", encoding="utf-8")
    record = _record(mood="grumpy")

    assert _mood_prompt_suffix(record, _moods(tmp_path)) == ""
    assert record["mood_applied"] == "none"


def test_shadow_stage_stages_suffix_and_logs_injection_semantics(monkeypatch, tmp_path):
    (tmp_path / "care.md").write_text("Be reassuring.", encoding="utf-8")
    cfg = _gateway_cfg(tmp_path)
    runner = object.__new__(GatewayRunner)
    runner._model_router_runtime_snapshot = MagicMock(
        return_value={"model": "model-b", "provider": "p2"}
    )
    decision = SimpleNamespace(
        directive=None,
        outcome="normal_streak_1_of_3",
        label="NORMAL",
        rule=None,
        record=_record(),
    )
    runner._classify_model_router_with_budget = AsyncMock(return_value=decision)
    logged = MagicMock()
    monkeypatch.setattr(model_router, "log_decision", logged)

    result = asyncio.run(
        runner._model_router_stage(
            _event(),
            _event().source,
            "tg:c1",
            mode="shadow",
            user_config=cfg,
        )
    )

    expected = "\n\n# Current Mood: care\nBe reassuring."
    assert result is decision
    assert decision.record["mood_applied"] == "injection"
    assert runner._consume_pending_mood_prompt("tg:c1") == expected
    assert runner._consume_pending_mood_prompt("tg:c1") == ""
    assert logged.call_args.args[0]["mood_applied"] == "injection"


def test_enabled_moods_do_not_start_detached_shadow_observer(tmp_path):
    runner = object.__new__(GatewayRunner)
    assert runner._schedule_model_router_shadow(
        event=_event(),
        session_key="tg:c1",
        runtime={"model": "model-b"},
        user_config=_gateway_cfg(tmp_path),
    ) is False


def test_injected_suffix_is_wire_only_not_persisted(tmp_path):
    (tmp_path / "focused.md").write_text("Stay concise.", encoding="utf-8")
    record = _record(mood="focused")
    suffix = _mood_prompt_suffix(record, _moods(tmp_path))
    persisted_system_prompt = "PERSISTED BASE PROMPT"
    agent = SimpleNamespace(
        _cached_system_prompt=persisted_system_prompt,
        ephemeral_system_prompt="Channel context" + suffix,
        model="model-b",
        provider="p2",
        base_url="https://p2.example/v1",
        api_mode="chat_completions",
        reasoning_config=None,
    )

    wire_prompt = compose_effective_system_prompt(
        agent, agent._cached_system_prompt
    )

    assert "Channel context" + suffix in wire_prompt
    assert suffix not in agent._cached_system_prompt
    assert agent._cached_system_prompt == persisted_system_prompt
