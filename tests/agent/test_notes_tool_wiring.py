"""Curated notes are exposed and dispatched in a normal Discord agent turn."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


NOTES_TOOLS = {"notes_write", "notes_read", "memory_propose"}


def test_notes_tools_resolve_for_discord_but_not_webhook():
    import model_tools

    def names(toolset):
        return {
            tool["function"]["name"]
            for tool in model_tools.get_tool_definitions(
                enabled_toolsets=[toolset], quiet_mode=True,
                skip_tool_search_assembly=True,
            )
        }

    assert NOTES_TOOLS <= names("hermes-discord")
    assert NOTES_TOOLS <= names("memory")
    assert NOTES_TOOLS.isdisjoint(names("hermes-webhook"))


@pytest.mark.parametrize("name", sorted(NOTES_TOOLS))
def test_both_agent_dispatch_paths_preserve_notes_agent_context(name):
    from agent.agent_runtime_helpers import invoke_tool
    from run_agent import AIAgent

    definitions = [{
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
    }]
    with (
        patch("model_tools.get_tool_definitions", return_value=definitions),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.invalid/v1",
            quiet_mode=True,
            session_id="notes-discord-session",
        )
    agent.client = MagicMock()
    args = {"action": "list"} if name == "notes_read" else {"content": "test fact"}
    call = SimpleNamespace(
        id="call_notes", type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    message = SimpleNamespace(content="", tool_calls=[call], reasoning=None)
    seen = []

    def dispatch(current_agent, tool_name, current_args):
        seen.append((current_agent, tool_name, current_args))
        return json.dumps({"success": True})

    with patch("tools.notes_tool.dispatch_notes_tool_for_agent", dispatch):
        messages = []
        agent._execute_tool_calls_sequential(message, messages, "task-1")
        result = invoke_tool(agent, name, dict(args), "task-1")

    assert seen == [(agent, name, args), (agent, name, args)]
    assert json.loads(messages[-1]["content"])["success"] is True
    assert json.loads(result)["success"] is True


def test_delegated_child_cannot_resolve_notes_from_discord_bundle():
    from tools.delegate_tool_toolsets import _resolve_child_toolsets
    from toolsets import resolve_toolset

    parent = SimpleNamespace(enabled_toolsets=["hermes-discord"], disabled_toolsets=[])
    enabled, disabled = _resolve_child_toolsets(parent, None, "leaf")
    child_names = set().union(*(resolve_toolset(name) for name in enabled))
    child_names.difference_update(
        set().union(*(resolve_toolset(name) for name in disabled))
    )
    assert NOTES_TOOLS.isdisjoint(child_names)


def test_notes_guidance_only_when_notes_write_is_exposed():
    from agent.system_prompt import _tool_guidance_block

    agent = SimpleNamespace(
        valid_tool_names=NOTES_TOOLS,
        _kanban_worker_guidance="",
    )
    assert "two-step" in _tool_guidance_block(agent)
    agent.valid_tool_names = {"session_search"}
    assert "two-step" not in _tool_guidance_block(agent)
