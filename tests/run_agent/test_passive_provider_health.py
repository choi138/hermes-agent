"""Tests for passive provider health wiring (passive-provider-health patch).

Real completion traffic feeds the model-routes health cache:

1. ``try_activate_fallback`` files an unhealthy verdict for the runtime being
   abandoned — but only for outage-shaped FailoverReasons, and only when the
   runtime maps to a ``providers:`` config key
2. ``_note_provider_success`` clears an unhealthy verdict after a live
   completion succeeds, gated on ``has_unhealthy_verdicts`` so the steady
   state stays cheap
3. Recording is best-effort: recorder failures never break the fallback walk
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import (
    _PASSIVE_UNHEALTHY_REASONS,
    _note_provider_success,
    _record_passive_provider_outcome,
)
from agent.error_classifier import FailoverReason
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="http://10.0.0.114:2455",
            provider="claude-lb",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://api.example.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


_FB = {"provider": "openai", "model": "gpt-4o"}


# ── Reason classification ──────────────────────────────────────────────────


class TestUnhealthyReasonSet:
    def test_outage_shaped_reasons_included(self):
        assert {
            FailoverReason.billing,
            FailoverReason.rate_limit,
            FailoverReason.overloaded,
            FailoverReason.server_error,
            FailoverReason.timeout,
        } == set(_PASSIVE_UNHEALTHY_REASONS)

    def test_request_scoped_reasons_excluded(self):
        for reason in (
            FailoverReason.auth,
            FailoverReason.content_policy_blocked,
            FailoverReason.context_overflow,
            FailoverReason.format_error,
            FailoverReason.upstream_rate_limit,
            None,
        ):
            assert reason not in _PASSIVE_UNHEALTHY_REASONS


# ── Failure hook on fallback activation ────────────────────────────────────


class TestFallbackActivationRecordsUnhealthy:
    def test_outage_reason_records_current_runtime(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.model_routes.provider_key_for_runtime",
                  return_value="claude-lb") as key_mock,
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.server_error) is True
        assert key_mock.call_args.kwargs["provider"] == "claude-lb"
        assert key_mock.call_args.kwargs["base_url"] == "http://10.0.0.114:2455"
        rec_mock.assert_called_once_with("claude-lb", False, "server_error")

    def test_refusal_reason_does_not_record(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        rec_mock.assert_not_called()

    def test_reasonless_activation_does_not_record(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            assert agent._try_activate_fallback() is True
        rec_mock.assert_not_called()

    def test_chain_exhausted_still_records(self):
        agent = _make_agent(fallback_model=None)  # empty chain
        with patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock:
            with patch("hermes_cli.model_routes.provider_key_for_runtime",
                       return_value="claude-lb"):
                assert agent._try_activate_fallback(reason=FailoverReason.overloaded) is False
        rec_mock.assert_called_once_with("claude-lb", False, "overloaded")

    def test_unmapped_runtime_skips_recording(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.model_routes.provider_key_for_runtime", return_value=""),
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True
        rec_mock.assert_not_called()

    def test_recorder_failure_never_breaks_activation(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.model_routes.provider_key_for_runtime",
                  side_effect=RuntimeError("config exploded")),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.server_error) is True


# ── Success hook ───────────────────────────────────────────────────────────


class TestSuccessHook:
    def test_gated_off_when_no_unhealthy_verdicts(self):
        agent = SimpleNamespace(provider="claude-lb", base_url="http://10.0.0.114:2455")
        with (
            patch("hermes_cli.model_routes.has_unhealthy_verdicts", return_value=False),
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            _note_provider_success(agent)
        rec_mock.assert_not_called()

    def test_records_recovery_when_unhealthy_verdicts_exist(self):
        agent = SimpleNamespace(provider="claude-lb", base_url="http://10.0.0.114:2455")
        with (
            patch("hermes_cli.model_routes.has_unhealthy_verdicts", return_value=True),
            patch("hermes_cli.model_routes.provider_key_for_runtime",
                  return_value="claude-lb"),
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            _note_provider_success(agent)
        rec_mock.assert_called_once()
        args = rec_mock.call_args.args
        assert args[0] == "claude-lb"
        assert args[1] is True

    def test_gate_failure_is_swallowed(self):
        agent = SimpleNamespace(provider="claude-lb", base_url="")
        with patch("hermes_cli.model_routes.has_unhealthy_verdicts",
                   side_effect=RuntimeError("boom")):
            _note_provider_success(agent)  # must not raise


# ── Helper robustness ──────────────────────────────────────────────────────


class TestRecordHelper:
    def test_missing_agent_attrs_default_to_empty(self):
        with (
            patch("hermes_cli.model_routes.provider_key_for_runtime",
                  return_value="") as key_mock,
            patch("hermes_cli.model_routes.record_provider_outcome") as rec_mock,
        ):
            _record_passive_provider_outcome(SimpleNamespace(), False, "timeout")
        assert key_mock.call_args.kwargs == {"provider": "", "base_url": ""}
        rec_mock.assert_not_called()


def test_rate_limit_marks_claude_nekos_unhealthy_and_route_skips_primary(
    monkeypatch, tmp_path,
):
    import hermes_cli.model_routes as model_routes

    cache_path = tmp_path / "model-route-health.json"
    cfg = {
        "providers": {
            "claude-nekos": {"base_url": "http://10.0.0.114:2455"},
        },
        "model_routes": {
            "health": {"cache_path": str(cache_path)},
            "routes": {
                "dev": {
                    "description": "development",
                    "provider": "claude-nekos",
                    "model": "claude-opus-4-6",
                    "fallbacks": [
                        {"provider": "openai-api", "model": "gpt-5.2-codex"},
                    ],
                },
            },
        },
    }
    monkeypatch.setenv("HERMES_MODEL_ROUTES_HEALTH_TEST", "1")
    monkeypatch.setattr(model_routes, "load_config", lambda: cfg)
    monkeypatch.setattr(model_routes, "load_config_readonly", lambda: cfg)
    model_routes._last_passive_unhealthy_write.clear()

    agent = _make_agent(fallback_model=[_FB])
    agent.provider = "claude-nekos"
    agent.base_url = "http://10.0.0.114:2455"
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(_mock_client(), "gpt-4o"),
    ):
        assert agent._try_activate_fallback(reason=FailoverReason.rate_limit) is True

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["claude-nekos"]["healthy"] is False
    assert cache["claude-nekos"]["reason"] == "passive: rate_limit"

    directive = model_routes.resolve_route("dev", cfg)
    assert directive["provider"] == "openai-api"
    assert directive["model"] == "gpt-5.2-codex"
    assert directive["source"] == "fallback:1"
