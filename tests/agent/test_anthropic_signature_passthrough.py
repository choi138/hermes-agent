"""Only an explicitly trusted Anthropic proxy may replay signed thinking."""

from agent.anthropic_message_convert import convert_messages_to_anthropic


def _replayed_thinking(base_url: str):
    messages = [
        {"role": "user", "content": "Inspect target.py"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {"type": "thinking", "thinking": "Inspect first", "signature": "sig-nekos"},
            ],
            "tool_calls": [{
                "id": "toolu_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"target.py"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
    ]
    _system, converted = convert_messages_to_anthropic(
        messages, base_url=base_url, model="claude-sonnet-4-6",
    )
    assistant = next(message for message in converted if message["role"] == "assistant")
    return [block for block in assistant["content"] if block.get("type") == "thinking"]


def test_signature_passthrough_requires_exact_opted_in_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  claude-nekos:\n"
        "    api: HTTPS://CLAUDE.NEKOS.ME/\n"
        "    transport: anthropic_messages\n"
        "    anthropic_signature_passthrough: true\n"
        "  other:\n"
        "    api: https://other.example/anthropic\n",
        encoding="utf-8",
    )

    assert _replayed_thinking("https://claude.nekos.me") == [
        {"type": "thinking", "thinking": "Inspect first", "signature": "sig-nekos"},
    ]
    assert _replayed_thinking("https://claude.nekos.me/v1") == []
    assert _replayed_thinking("https://other.example/anthropic") == []


def test_signature_passthrough_is_scoped_to_active_profile(monkeypatch, tmp_path):
    trusted = tmp_path / "trusted"
    untrusted = tmp_path / "untrusted"
    trusted.mkdir()
    untrusted.mkdir()
    (trusted / "config.yaml").write_text(
        "providers:\n  proxy:\n    api: https://proxy.example/anthropic\n"
        "    anthropic_signature_passthrough: true\n",
        encoding="utf-8",
    )
    (untrusted / "config.yaml").write_text(
        "providers:\n  proxy:\n    api: https://proxy.example/anthropic\n"
        '    anthropic_signature_passthrough: "true"\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(trusted))
    assert _replayed_thinking("https://proxy.example/anthropic")
    monkeypatch.setenv("HERMES_HOME", str(untrusted))
    assert _replayed_thinking("https://proxy.example/anthropic") == []
    monkeypatch.setenv("HERMES_HOME", str(trusted))
    assert _replayed_thinking("https://proxy.example/anthropic")
