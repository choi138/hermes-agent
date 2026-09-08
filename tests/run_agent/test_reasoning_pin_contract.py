"""Pinned requests through production builders and installed SDKs, offline only."""
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import OpenAI, omit

from agent.runtime_control import model_status
from run_agent import AIAgent

PIN = {"enabled": True, "effort": "max", "selection": "pinned"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.create_connection", deny)
    monkeypatch.setattr("socket.getaddrinfo", deny)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 128000)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})


def make_agent(**kwargs):
    params = dict(model="gpt-6-astra", provider="custom", api_key="offline-only",
        base_url="https://local.invalid/v1", api_mode="codex_responses", quiet_mode=True,
        skip_context_files=True, skip_memory=True, reasoning_config=dict(PIN))
    params.update(kwargs)
    with patch("run_agent.get_tool_definitions", return_value=[]), patch("run_agent.check_toolset_requirements", return_value={}), patch("run_agent.OpenAI"):
        return AIAgent(**params)


def sdk_wire(payload, mode="codex_responses"):
    sent = []
    def capture(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline", "object": "response", "output": [], "status": "completed"})
    with OpenAI(api_key="offline-only", base_url="https://local.invalid/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(capture), trust_env=False)) as client:
        method = client.responses.create if mode == "codex_responses" else client.chat.completions.create
        response = method(**payload)
        if hasattr(response, "close"):
            response.close()
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize("override", [
    {"extra_body": {"reasoning": {"effort": "high"}}},
    {"extra_body": {"reasoning": {"summary": "auto"}}},
    {"extra_body": {"reasoning": None}},
    {"extra_body": {"reasoning": []}},
    {"extra_body": {"reasoning": "max"}},
    {"extra_body": {"reasoning": omit}},
    {"extra_body": {"reasoning": {"effort": None}}},
    {"extra_body": {"model": "gpt-5.5", "reasoning": {"effort": "max"}}},
    {"model": "gpt-5.5", "reasoning": {"effort": "max"}},
])
def test_responses_effective_override_conflict(override):
    agent = make_agent(request_overrides=override)
    with pytest.raises(ValueError, match="[Pp]inned"):
        # If the guard misses, the installed SDK actually sends the bad body.
        sdk_wire(agent._build_api_kwargs([], []))
    assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"


@pytest.mark.parametrize("override", [
    {}, {"extra_body": {"reasoning": {"effort": "max", "summary": "auto"}}},
    {"extra_body": {"metadata": {"local": "yes"}}},
    {"extra_body": {"model": "gpt-5.6-sol"}},
    {"model": "gpt-5.5", "extra_body": {"model": "gpt-6-astra", "reasoning": {"effort": "max"}}},
    {"reasoning": {"effort": "high"}, "extra_body": {"reasoning": {"effort": "max"}}},
])
def test_responses_effective_override_positive(override):
    agent = make_agent(request_overrides=override)
    wire = sdk_wire(agent._build_api_kwargs([], []))
    assert wire["reasoning"]["effort"] == "max"
    if "model" in override.get("extra_body", {}):
        assert wire["model"] == override["extra_body"]["model"]


def test_unpinned_sdk_nested_override_remains_shallow():
    agent = make_agent(reasoning_config={"effort": "max"},
        request_overrides={"extra_body": {"reasoning": {"summary": "auto"}}})
    wire = sdk_wire(agent._build_api_kwargs([], []))
    assert wire["reasoning"] == {"summary": "auto"}


@pytest.mark.parametrize("pinned", [True, False])
def test_real_opencode_fallback_pin_cannot_clamp(pinned):
    destination = dict(provider="opencode-go", model="kimi-k2.5", base_url="https://opencode.ai/zen/go/v1",
                       api_key="offline-only", api_mode="chat_completions")
    agent = make_agent(fallback_model=[destination], reasoning_config=dict(PIN) if pinned else {"effort": "max"})
    client = MagicMock(base_url=destination["base_url"], api_key="offline-only")
    with patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None), \
         patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, destination["model"])), \
         patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m), \
         patch("hermes_cli.config.load_config", return_value={"agent": {"reasoning_effort": "high"}}):
        assert agent._try_activate_fallback()
    assert agent.api_mode == "chat_completions"
    if pinned:
        assert agent.reasoning_config == PIN
        with pytest.raises(ValueError, match="[Pp]inned"):
            sdk_wire(agent._build_api_kwargs([], []), "chat_completions")
        assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"
    else:
        assert sdk_wire(agent._build_api_kwargs([], []), "chat_completions")["reasoning_effort"] == "high"


@pytest.mark.parametrize("args", [
    {"operation": "pin_reasoning", "reasoning_effort": "max"},
    {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": False},
    {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": "true"},
    {"operation": "pin_reasoning", "reasoning_effort": "max", "user_requested": True, "route": "dev"},
    {"operation": "pin_reasoning", "user_requested": True},
    {"operation": "pin_reasoning", "reasoning_effort": "bad", "user_requested": True},
    {"operation": "release_reasoning", "user_requested": True, "reasoning_effort": "max"},
    {"operation": "release_reasoning", "reason": "user asked to release"},
    {"operation": "release_reasoning", "user_requested": True, "model": "gpt-5.5"},
    {"operation": "bad", "route": "dev"},
    {"route": "dev", "user_requested": True},
])
def test_pin_operations_require_explicit_unambiguous_user_intent(args):
    from agent.runtime_control import dispatch_model_switch
    agent = make_agent()
    assert json.loads(dispatch_model_switch(agent, args))["success"] is False
    assert agent.reasoning_config == PIN


@pytest.mark.parametrize("operation", [None, "route"])
def test_old_route_call_and_user_reason_never_self_pin(operation):
    from agent.runtime_control import dispatch_model_switch
    cfg = {"providers": {"test": {"base_url": "https://local.invalid/v1"}},
           "model_routes": {"routes": {"dev": {"provider": "test", "model": "gpt-6-astra", "reasoning_effort": "max"}}}}
    agent = make_agent(provider="test", reasoning_config={"effort": "max"})
    args = {"route": "dev", "reason": "사용자가 max 고정을 요청했습니다"}
    if operation:
        args["operation"] = operation
    with patch("hermes_cli.config.load_config", return_value=cfg):
        result = json.loads(dispatch_model_switch(agent, args))
    assert result["success"] and result["noop"]
    assert agent.reasoning_config.get("selection") != "pinned"


def anthropic_wire(payload):
    from anthropic import Anthropic
    sent = []
    def capture(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline", "type": "message", "role": "assistant", "model": "claude-opus-4-7", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
    with Anthropic(api_key="offline-only", base_url="https://local.invalid", max_retries=0, timeout=60,
        http_client=httpx.Client(transport=httpx.MockTransport(capture), trust_env=False)) as client:
        client.messages.create(**payload)
    return sent[0]


@pytest.mark.parametrize("model,effort,accepted", [
    ("claude-opus-4-7", "max", True), ("claude-opus-4-7", "xhigh", True),
    ("claude-sonnet-4-6", "max", True), ("claude-sonnet-4-6", "xhigh", False),
    ("claude-sonnet-4-5", "max", False), ("MiniMax-M2.5", "max", False),
    ("claude-haiku-4-5", "high", False), ("claude-opus-4-7", "minimal", False),
])
def test_native_messages_exact_effort_or_visible_error(model, effort, accepted):
    from agent.transports.anthropic import AnthropicTransport
    def build(pin):
        return AnthropicTransport().build_kwargs(model=model, messages=[{"role": "user", "content": "offline"}],
            tools=[], reasoning_config={"effort": effort, **({"selection": "pinned"} if pin else {})})
    if accepted:
        wire = anthropic_wire(build(True))
        assert wire["output_config"]["effort"] == effort
        assert wire["thinking"]["type"] == "adaptive"
    else:
        with pytest.raises(ValueError, match="[Pp]inned"):
            anthropic_wire(build(True))
        # Automatic numeric budget / alias modes still build and serialize.
        anthropic_wire(build(False))


@pytest.mark.parametrize("mode,override", [
    ("codex_responses", {"extra_body": {"reasoning": {"effort": "high"}}}),
    ("anthropic_messages", {"extra_body": {"output_config": {"effort": "high"}}}),
    ("anthropic_messages", {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 32000}}}),
    ("anthropic_messages", {"extra_body": {"model": "claude-sonnet-4-5"}}),
    ("anthropic_messages", {"extra_body": {"output_config": None}}),
])
def test_actual_send_boundary_rejects_late_provider_extras(mode, override):
    from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
    from agent.transports.anthropic import AnthropicTransport
    from types import SimpleNamespace
    agent = SimpleNamespace(api_mode=mode, model="gpt-6-astra", provider="custom", base_url="https://local.invalid/v1", reasoning_config=dict(PIN))
    if mode == "anthropic_messages":
        payload = AnthropicTransport().build_kwargs(model="claude-opus-4-7", messages=[], tools=[], reasoning_config=dict(PIN))
    else:
        payload = make_agent()._build_api_kwargs([], [])
    payload.update(override)
    client_factory = MagicMock()
    with pytest.raises(ValueError, match="[Pp]inned"):
        _dispatch_nonstreaming_api_request(agent, payload, make_client=client_factory)
    client_factory.assert_not_called()


@pytest.mark.parametrize("mode", ["bedrock_converse", "codex_app_server", "unknown"])
def test_unsupported_transports_cannot_claim_pin(mode):
    from agent.reasoning_pin import validate_pinned_request
    with pytest.raises(ValueError, match="[Pp]inned"):
        validate_pinned_request({"reasoning_effort": "max"}, PIN, api_mode=mode)


def test_unknown_chat_echo_is_not_a_verified_effort_contract():
    agent = make_agent(api_mode="chat_completions", request_overrides={"extra_body": {"reasoning_effort": "max"}})
    with pytest.raises(ValueError, match="[Pp]inned"):
        sdk_wire(agent._build_api_kwargs([], []), "chat_completions")


@pytest.mark.parametrize("mode,provider,model,url", [
    ("bedrock_converse", "bedrock", "anthropic.claude-opus-4-6-v1", "https://local.invalid"),
    ("chat_completions", "custom", "gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta"),
    ("chat_completions", "custom", "gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("chat_completions", "moa", "mixture", "https://local.invalid/v1"),
])
def test_unsupported_family_real_builder_preserves_automatic_mode(mode, provider, model, url):
    agent = make_agent()
    # Select the production transport without creating any provider client.
    agent.api_mode, agent.provider, agent.model, agent.base_url = mode, provider, model, url
    with pytest.raises(ValueError, match="[Pp]inned"):
        agent._build_api_kwargs([], [])
    assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"
    agent.reasoning_config = {"effort": "max"}
    assert isinstance(agent._build_api_kwargs([], []), dict)


@pytest.mark.parametrize("provider,model,effort,url", [
    ("kimi-coding", "k3", "max", "https://api.kimi.com/coding/v1"),
    ("opencode-go", "kimi-k2.5", "high", "https://opencode.ai/zen/go/v1"),
])
def test_native_kimi_exact_positive_sdk_controls(provider, model, effort, url):
    agent = make_agent(provider=provider, model=model, api_mode="chat_completions", base_url=url,
        reasoning_config={"effort": effort, "selection": "pinned"})
    wire = sdk_wire(agent._build_api_kwargs([], []), "chat_completions")
    assert wire["reasoning_effort"] == effort


@pytest.mark.parametrize("override,accepted", [
    ({}, True),
    ({"extra_body": {"reasoning_effort": "max"}}, True),
    ({"extra_body": {"metadata": {"test": "offline"}}}, True),
    ({"extra_body": {"reasoning_effort": "high"}}, False),
    ({"extra_body": {"reasoning_effort": None}}, False),
    ({"extra_body": {"reasoning_effort": omit}}, False),
    ({"extra_body": {"model": "kimi-k2.5", "reasoning_effort": "max"}}, False),
    ({"model": "kimi-k2.5", "reasoning_effort": "max"}, False),
])
def test_real_native_chat_profile_final_sdk_body(override, accepted):
    agent = make_agent(provider="opencode-go", model="glm-5.2", api_mode="chat_completions",
        base_url="https://opencode.ai/zen/go/v1", request_overrides=override)
    if accepted:
        wire = sdk_wire(agent._build_api_kwargs([], []), "chat_completions")
        assert wire["reasoning_effort"] == "max"
        assert json.loads(model_status(agent))["reasoning"]["availability"] == "locally_supported"
    else:
        with pytest.raises(ValueError, match="[Pp]inned"):
            sdk_wire(agent._build_api_kwargs([], []), "chat_completions")
        assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"


@pytest.mark.parametrize("override", [None, {"output_config": {"effort": "max"}, "metadata": {"user_id": "offline"}},
    {"output_config": {"effort": "high"}}, {"output_config": {}}, {"thinking": {"type": "disabled"}},
    {"model": "claude-opus-4-5"}])
def test_real_anthropic_builder_after_provider_extras(monkeypatch, override):
    def provider_extras(agent, kwargs):
        if override is not None:
            kwargs["extra_body"] = override
        return kwargs
    monkeypatch.setattr("agent.chat_completion_helpers._merge_nous_portal_messages_extra_body", provider_extras)
    agent = make_agent(provider="anthropic", model="claude-opus-4-7", api_mode="anthropic_messages")
    accepted = override is None or override.get("output_config", {}).get("effort") == "max"
    if accepted:
        wire = anthropic_wire(agent._build_api_kwargs([], []))
        assert wire["output_config"]["effort"] == "max"
    else:
        with pytest.raises(ValueError, match="[Pp]inned"):
            anthropic_wire(agent._build_api_kwargs([], []))
        assert json.loads(model_status(agent))["reasoning"]["availability"] == "unavailable"
