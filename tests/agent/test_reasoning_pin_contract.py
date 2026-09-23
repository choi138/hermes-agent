"""Explicit session pins must match the installed SDK wire and survive fallback."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import OpenAI

from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
from agent.transports.anthropic import AnthropicTransport
from agent.transports.chat_completions import ChatCompletionsTransport
from agent.transports.codex import ResponsesApiTransport


PIN = {"enabled": True, "effort": "max", "selection": "pinned"}


def _payload(mode, *, override=None, pinned=True):
    reasoning = dict(PIN) if pinned else {"enabled": True, "effort": "max"}
    if mode == "codex_responses":
        return ResponsesApiTransport().build_kwargs(
            model="gpt-5.6-sol", messages=[{"role": "user", "content": "offline"}], tools=[],
            reasoning_config=reasoning, provider="custom", base_url="https://local.invalid/v1",
            request_overrides=override,
        )
    if mode == "chat_completions":
        from providers import get_provider_profile

        return ChatCompletionsTransport().build_kwargs(
            model="glm-5.2", messages=[{"role": "user", "content": "offline"}], tools=[],
            reasoning_config=reasoning, provider="opencode-go",
            provider_profile=get_provider_profile("opencode-go"),
            base_url="https://local.invalid/v1", request_overrides=override,
        )
    return AnthropicTransport().build_kwargs(
        model="claude-opus-4-7", messages=[{"role": "user", "content": "offline"}], tools=[],
        reasoning_config=reasoning, base_url="https://local.invalid",
    )


@pytest.mark.parametrize("mode,field", [
    ("codex_responses", "reasoning"),
    ("chat_completions", "reasoning_effort"),
    ("anthropic_messages", "output_config"),
])
def test_pin_matches_sdk_wire_and_late_conflicts_never_send(mode, field):
    sent = []

    def capture(request):
        sent.append(json.loads(request.content))
        if mode == "anthropic_messages":
            return httpx.Response(200, json={
                "id": "offline", "type": "message", "role": "assistant", "model": "claude-opus-4-7",
                "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        if mode == "chat_completions":
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0, "model": "glm-5.2",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            })
        return httpx.Response(200, json={
            "id": "offline", "object": "response", "output": [], "status": "completed",
        })

    payload = _payload(mode)
    with httpx.Client(transport=httpx.MockTransport(capture), trust_env=False) as http_client:
        if mode == "anthropic_messages":
            from anthropic import Anthropic

            with Anthropic(api_key="offline", base_url="https://local.invalid", max_retries=0,
                           http_client=http_client) as client:
                client.messages.create(**payload)
        else:
            with OpenAI(api_key="offline", base_url="https://local.invalid/v1", max_retries=0,
                        http_client=http_client) as client:
                method = client.responses.create if mode == "codex_responses" else client.chat.completions.create
                response = method(**payload)
                if hasattr(response, "close"):
                    response.close()
    assert len(sent) == 1
    control = sent[0][field]
    assert (control["effort"] if isinstance(control, dict) else control) == "max"

    late = dict(payload)
    late["extra_body"] = {field: {"effort": "high"} if field != "reasoning_effort" else "high"}
    agent = SimpleNamespace(
        api_mode=mode, provider="opencode-go" if mode == "chat_completions" else "custom",
        base_url="https://local.invalid/v1", reasoning_config=dict(PIN),
    )
    make_client = MagicMock()
    with pytest.raises(ValueError, match="Pinned reasoning"):
        _dispatch_nonstreaming_api_request(agent, late, make_client=make_client)
    make_client.assert_not_called()


def test_real_fallback_keeps_pin_and_rejects_unsupported_destination():
    from run_agent import AIAgent

    destination = {
        "provider": "custom", "model": "gpt-5.5", "base_url": "https://api.openai.com/v1",
        "api_key": "offline", "api_mode": "codex_responses",
    }
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("model_tools.check_toolset_requirements", return_value={}), \
         patch("agent.process_bootstrap.OpenAI"), \
         patch("agent.model_metadata.get_model_context_length", return_value=128000):
        agent = AIAgent(
            model="gpt-5.6-sol", provider="custom", api_key="offline",
            base_url="https://primary.invalid/v1", api_mode="codex_responses",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            reasoning_config=dict(PIN), fallback_model=[destination],
        )
    client = MagicMock(base_url=destination["base_url"], api_key="offline")
    with patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None), \
         patch("agent.auxiliary_client.resolve_provider_client", return_value=(client, destination["model"])), \
         patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, provider: model), \
         patch("hermes_cli.config.load_config", return_value={"agent": {"reasoning_effort": "high"}}):
        assert agent._try_activate_fallback()
    assert agent.model == "gpt-5.5"
    assert agent.reasoning_config == PIN
    with pytest.raises(ValueError, match="Pinned reasoning"):
        agent._build_api_kwargs([], [])
