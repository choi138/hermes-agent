"""Post-tool compression shares the configured per-turn attempt budget."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_response(index: int):
    call = SimpleNamespace(
        id=f"call_{index}",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query":"x"}'),
    )
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[call],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=None,
    )


def _stop_response():
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _make_agent():
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 10_000
    compressor.context_length = 200_000
    compressor.last_prompt_tokens = 150_000
    compressor.should_compress.return_value = True
    compressor.should_defer_preflight_to_real_usage.return_value = True
    compressor.get_active_compression_failure_cooldown.return_value = None
    agent.context_compressor = compressor
    return agent


def test_post_tool_compression_honors_shared_attempt_cap():
    agent = _make_agent()
    agent.max_compression_attempts = 2
    agent.client.chat.completions.create.side_effect = [
        *[_tool_response(i) for i in range(5)],
        _stop_response(),
    ]
    compress_calls = []

    def _compress(messages, _system_message, **_kwargs):
        compress_calls.append(len(messages))
        return messages, "compressed prompt"

    with (
        patch.object(agent, "_compress_context", side_effect=_compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("use the tool repeatedly")

    assert result["completed"] is True
    assert len(compress_calls) == 2


def test_preflight_and_post_tool_compression_share_one_attempt_cap():
    agent = _make_agent()
    agent.max_compression_attempts = 2
    agent.context_compressor.should_defer_preflight_to_real_usage.return_value = False
    agent.client.chat.completions.create.side_effect = [
        *[_tool_response(i) for i in range(3)],
        _stop_response(),
    ]
    history = []
    for index in range(13):
        history.extend(
            [
                {"role": "user", "content": f"prior user {index}"},
                {"role": "assistant", "content": f"prior answer {index}"},
            ]
        )
    compress_calls = []

    def _compress(messages, _system_message, **_kwargs):
        compress_calls.append(len(messages))
        return messages, "compressed prompt"

    with (
        patch.object(agent, "_compress_context", side_effect=_compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation(
            "continue with tools", conversation_history=history
        )

    assert result["completed"] is True
    assert len(compress_calls) == 2


def test_proactive_prune_commits_when_full_compression_stands_down():
    agent = _make_agent()
    agent.context_compressor.should_compress.return_value = False
    prune_calls = []

    def _prune(messages, current_tokens=None):
        prune_calls.append(current_tokens)
        pruned = [dict(message) for message in messages]
        changed = 0
        for message in pruned:
            if message.get("role") == "tool" and message.get("content") != "[pruned]":
                message["content"] = "[pruned]"
                changed += 1
        return (pruned, changed) if changed else (messages, 0)

    agent.context_compressor.prune_tool_results_only = _prune
    agent.client.chat.completions.create.side_effect = [
        _tool_response(0),
        _tool_response(1),
        _stop_response(),
    ]
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("prune stale tool history")

    assert result["completed"] is True
    assert prune_calls == [150_000, 150_000]
    tool_rows = [
        message for message in result["messages"] if message.get("role") == "tool"
    ]
    assert tool_rows
    assert all(message["content"] == "[pruned]" for message in tool_rows)


def test_full_compression_and_proactive_prune_are_mutually_exclusive():
    agent = _make_agent()
    prune = MagicMock(side_effect=lambda messages, current_tokens=None: (messages, 0))
    agent.context_compressor.prune_tool_results_only = prune
    agent.client.chat.completions.create.side_effect = [
        _tool_response(0),
        _stop_response(),
    ]
    with (
        patch.object(
            agent,
            "_compress_context",
            side_effect=lambda messages, _system, **_kwargs: (
                messages,
                "compressed prompt",
            ),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("compress instead of pruning")

    assert result["completed"] is True
    prune.assert_not_called()
