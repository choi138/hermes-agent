"""Local serialization contracts, never live provider acceptance evidence."""
import json

import pytest

from agent.reasoning_effort import codex_supported_efforts, clamp_effort
from agent.transports.codex import ResponsesApiTransport


@pytest.mark.parametrize("model", ["gpt-6-astra", "openai/gpt-6-astra", "gpt-6-astra-2026-09-01"])
def test_astra_producer_preserves_max_and_has_no_none(model):
    levels = codex_supported_efforts(model)
    assert clamp_effort("max", levels) == "max"
    assert "none" not in levels
    assert all(level in levels for level in ("low", "medium", "high", "xhigh"))


@pytest.mark.parametrize("model", ["gpt-6", "gpt-7-astra", "gpt-6-astraish", "gpt-6-astra-preview", "gpt-5.5"])
def test_unknown_families_keep_legacy_ceiling(model):
    assert clamp_effort("max", codex_supported_efforts(model)) == "xhigh"


def test_sol_preserves_existing_max_and_none():
    levels = codex_supported_efforts("gpt-5.6-sol")
    assert clamp_effort("max", levels) == "max"
    assert "none" in levels


@pytest.mark.parametrize("declared,expected", [(None, "max"), (("low", "high"), "high"), ((), None)])
def test_final_serialized_astra_payload(monkeypatch, declared, expected):
    monkeypatch.setattr("agent.transports.codex._profile_declared_efforts", lambda *a: declared)
    payload = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", messages=[{"role": "user", "content": "contract"}], tools=[],
        reasoning_config={"enabled": True, "effort": "max"},
        request_overrides={"metadata": {"test": "local"}},
    )
    wire = json.loads(json.dumps(payload))
    assert wire.get("reasoning", {}).get("effort") == expected
    assert wire["metadata"] == {"test": "local"}


def test_explicit_request_override_retains_existing_precedence():
    payload = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", messages=[], tools=[], reasoning_config={"effort": "max"},
        request_overrides={"reasoning": {"effort": "low"}},
    )
    assert json.loads(json.dumps(payload))["reasoning"]["effort"] == "low"


def test_sdk_serializes_final_astra_max_request_without_network():
    import httpx
    from openai import OpenAI

    sent = []
    def capture(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "local-contract", "object": "response", "output": [], "status": "completed"})
    payload = ResponsesApiTransport().build_kwargs(
        model="gpt-6-astra", messages=[{"role": "user", "content": "local contract"}], tools=[],
        reasoning_config={"enabled": True, "effort": "max"},
    )
    with OpenAI(api_key="test-only", base_url="https://local.invalid/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(capture))) as client:
        client.responses.create(**payload)
    assert sent[0]["model"] == "gpt-6-astra"
    assert sent[0]["reasoning"]["effort"] == "max"
