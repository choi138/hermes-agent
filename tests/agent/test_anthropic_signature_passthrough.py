"""Thinking-signature replay for explicitly trusted Anthropic proxies."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# The passthrough helpers moved with _manage_thinking_signatures into
# agent.anthropic_message_convert during upstream's adapter split (v2026.8.31);
# patch the module that actually owns the cache global.
import agent.anthropic_message_convert as adapter
from agent.anthropic_adapter import convert_messages_to_anthropic
from agent.transports import get_transport


SIGNATURE = "sig-nekos"
PASSTHROUGH_URL = "https://claude.nekos.me"
OTHER_PROXY_URL = "https://proxy.example/anthropic"


@pytest.fixture(autouse=True)
def _reset_passthrough_cache(monkeypatch):
    monkeypatch.setattr(adapter, "_signature_passthrough_urls_cache", None)


def _write_config(tmp_path, text: str):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _point_config_at(monkeypatch, config_path) -> None:
    monkeypatch.setattr(
        "hermes_constants.get_config_path",
        lambda: config_path,
    )


def _tool_loop_messages():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="thinking",
                thinking="inspect the target first",
                signature=SIGNATURE,
            ),
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="read_file",
                input={"path": "target.py"},
            ),
        ],
        stop_reason="tool_use",
        usage=None,
    )
    normalized = get_transport("anthropic_messages").normalize_response(response)
    provider_data = normalized.provider_data or {}
    stored = {
        "role": "assistant",
        "content": normalized.content or "",
        "reasoning_details": provider_data.get("reasoning_details"),
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
            }
            for tool_call in (normalized.tool_calls or [])
        ],
    }
    if provider_data.get("anthropic_content_blocks"):
        stored["anthropic_content_blocks"] = provider_data[
            "anthropic_content_blocks"
        ]
    return [
        {"role": "user", "content": "inspect target.py"},
        stored,
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
    ]


def _latest_assistant_thinking(base_url: str):
    _system, converted = convert_messages_to_anthropic(
        _tool_loop_messages(),
        base_url=base_url,
        model="claude-sonnet-4-6",
    )
    latest = [m for m in converted if m.get("role") == "assistant"][-1]
    return [
        block
        for block in latest["content"]
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]


def test_flagged_proxy_preserves_signed_thinking_with_normalized_url(
    tmp_path,
    monkeypatch,
):
    config_path = _write_config(
        tmp_path,
        """
providers:
  claude-nekos:
    base_url: HTTPS://CLAUDE.NEKOS.ME/
    anthropic_signature_passthrough: true
""",
    )
    _point_config_at(monkeypatch, config_path)

    thinking = _latest_assistant_thinking(PASSTHROUGH_URL)

    assert thinking[0]["signature"] == SIGNATURE
    assert adapter._get_signature_passthrough_urls() == frozenset(
        {PASSTHROUGH_URL}
    )


def test_unflagged_third_party_proxy_still_strips_thinking(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        f"""
providers:
  other:
    base_url: {OTHER_PROXY_URL}
""",
    )
    _point_config_at(monkeypatch, config_path)

    assert _latest_assistant_thinking(OTHER_PROXY_URL) == []


def test_flag_trusts_only_the_exact_normalized_base_url(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        f"""
providers:
  claude-nekos:
    base_url: {PASSTHROUGH_URL}
    anthropic_signature_passthrough: true
""",
    )
    _point_config_at(monkeypatch, config_path)

    assert _latest_assistant_thinking(f"{PASSTHROUGH_URL}/v1") == []


def test_unreadable_config_fails_closed(monkeypatch):
    unreadable = MagicMock()
    unreadable.exists.return_value = True
    unreadable.read_text.side_effect = OSError("permission denied")
    _point_config_at(monkeypatch, unreadable)

    assert adapter._get_signature_passthrough_urls() == frozenset()
    assert _latest_assistant_thinking(PASSTHROUGH_URL) == []


def test_invalid_yaml_fails_closed(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, "providers: [")
    _point_config_at(monkeypatch, config_path)

    assert adapter._get_signature_passthrough_urls() == frozenset()
    assert _latest_assistant_thinking(PASSTHROUGH_URL) == []


def test_passthrough_urls_are_cached_for_process_lifetime(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        f"""
providers:
  claude-nekos:
    base_url: {PASSTHROUGH_URL}
    anthropic_signature_passthrough: true
""",
    )
    _point_config_at(monkeypatch, config_path)

    first = adapter._get_signature_passthrough_urls()
    config_path.write_text("providers: {}\n", encoding="utf-8")

    assert adapter._get_signature_passthrough_urls() is first
    assert adapter._is_signature_passthrough_endpoint(PASSTHROUGH_URL)
