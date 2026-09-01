#!/usr/bin/env python3
"""
Tests for the subagent delegation tool.

Uses mock AIAgent instances to test the delegation logic without
requiring API keys or real LLM calls.

Run with:  python -m pytest tests/test_delegate.py -v
   or:     python tests/test_delegate.py
"""

import json
import logging
import os
import re
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_BLOCKED_TOOLS,
    DELEGATE_TASK_SCHEMA,
    DelegateEvent,
    _get_max_concurrent_children,
    _load_config,
    _LEGACY_EVENT_MAP,
    MAX_DEPTH,
    check_delegate_requirements,
    delegate_task,
    _build_child_agent,
    _build_child_progress_callback,
    _build_child_system_prompt,
    _extract_output_tail,
    _strip_blocked_tools,
    _resolve_child_credential_pool,
    _resolve_delegation_credentials,
    _inherit_parent_base_url,
)


def _make_mock_parent(depth=0):
    """Create a mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key="***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


class TestDelegateRequirements(unittest.TestCase):
    def test_always_available(self):
        self.assertTrue(check_delegate_requirements())

    def test_schema_valid(self):
        self.assertEqual(DELEGATE_TASK_SCHEMA["name"], "delegate_task")
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("goal", props)
        self.assertIn("tasks", props)
        self.assertIn("context", props)
        # toolsets is intentionally NOT exposed to the model — subagents always
        # inherit the parent's toolsets. Letting the model name toolsets was a
        # capability-selection surface the model should not control.
        self.assertNotIn("toolsets", props)
        self.assertNotIn("toolsets", props["tasks"]["items"]["properties"])
        # max_iterations is intentionally NOT exposed to the model — it's
        # config-authoritative via delegation.max_iterations so users get
        # predictable budgets.
        self.assertNotIn("max_iterations", props)
        # ACP subprocess transport is operator-controlled via config.yaml, not
        # model-controlled via delegate_task arguments.
        self.assertNotIn("acp_command", props)
        self.assertNotIn("acp_args", props)
        self.assertNotIn("acp_command", props["tasks"]["items"]["properties"])
        self.assertNotIn("acp_args", props["tasks"]["items"]["properties"])
        self.assertNotIn("maxItems", props["tasks"])  # removed — limit is now runtime-configurable

    def test_schema_description_advertises_runtime_limits(self):
        """The model must see the user's actual concurrency / spawn-depth caps,
        not the framework defaults. Without this, models that read 'default 3'
        will self-cap below the user's real limit.
        """
        from tools.delegate_tool import (
            _build_dynamic_schema_overrides,
            _get_max_concurrent_children,
            _get_max_spawn_depth,
        )

        overrides = _build_dynamic_schema_overrides()
        max_children = _get_max_concurrent_children()
        max_depth = _get_max_spawn_depth()

        desc = overrides["description"]
        tasks_desc = overrides["parameters"]["properties"]["tasks"]["description"]
        role_desc = overrides["parameters"]["properties"]["role"]["description"]

        # Top-level description names the user's concurrency limit explicitly.
        self.assertIn(f"up to {max_children}", desc)
        # Top-level description names the user's spawn-depth limit explicitly.
        self.assertIn(f"max_spawn_depth={max_depth}", desc)
        # tasks parameter description repeats the concurrency cap.
        self.assertIn(f"up to {max_children}", tasks_desc)
        # role parameter description names the spawn-depth limit.
        self.assertIn(f"max_spawn_depth={max_depth}", role_desc)
        # The misleading "default 3" / "default 2" wording is gone from
        # every dynamic surface (model-facing).
        for surface in (desc, tasks_desc, role_desc):
            self.assertNotIn("default 3", surface)
            self.assertNotIn("default 2", surface)

    def test_schema_overrides_applied_via_get_definitions(self):
        """Registry.get_definitions() must apply dynamic_schema_overrides so
        the model API call sees current values, not the static import-time text.
        """
        from tools.registry import registry
        defs = registry.get_definitions({"delegate_task"})
        self.assertEqual(len(defs), 1)
        fn = defs[0]["function"]
        # Description should mention the user's actual limits, not "default 3".
        from tools.delegate_tool import (
            _get_max_concurrent_children,
            _get_max_spawn_depth,
        )
        self.assertIn(f"up to {_get_max_concurrent_children()}", fn["description"])
        self.assertIn(f"max_spawn_depth={_get_max_spawn_depth()}", fn["description"])


class TestChildSystemPrompt(unittest.TestCase):
    def test_goal_only(self):
        prompt = _build_child_system_prompt("Fix the tests")
        self.assertIn("Fix the tests", prompt)
        self.assertIn("YOUR TASK", prompt)
        self.assertNotIn("CONTEXT", prompt)

    def test_goal_with_context(self):
        prompt = _build_child_system_prompt("Fix the tests", "Error: assertion failed in test_foo.py line 42")
        self.assertIn("Fix the tests", prompt)
        self.assertIn("CONTEXT", prompt)
        self.assertIn("assertion failed", prompt)

    def test_empty_context_ignored(self):
        prompt = _build_child_system_prompt("Do something", "  ")
        self.assertNotIn("CONTEXT", prompt)


class TestStripBlockedTools(unittest.TestCase):
    def test_removes_blocked_toolsets(self):
        result = _strip_blocked_tools(["terminal", "file", "delegation", "clarify", "memory", "code_execution"])
        self.assertEqual(sorted(result), ["file", "terminal"])

    def test_preserves_allowed_toolsets(self):
        result = _strip_blocked_tools(["terminal", "file", "web", "browser"])
        self.assertEqual(sorted(result), ["browser", "file", "terminal", "web"])

    def test_empty_input(self):
        result = _strip_blocked_tools([])
        self.assertEqual(result, [])

    def test_strips_cronjob_toolset(self):
        """Regression for issue #43466: child subagents must not inherit
        the cronjob toolset from a parent running on a gateway platform.
        Without this guard, a delegated child could schedule new cron jobs
        under the parent's identity.
        """
        result = _strip_blocked_tools(
            ["terminal", "file", "cronjob", "web"]
        )
        self.assertNotIn("cronjob", result)
        self.assertIn("terminal", result)
        self.assertIn("file", result)
        self.assertIn("web", result)

    def test_strip_set_derived_from_blocklist(self):
        """The strip set must be derived from DELEGATE_BLOCKED_TOOLS so a
        new blocked tool can't silently leak through as a toolset name
        (regression for issue #43466's 'more robust variant' suggestion).
        """
        from tools.delegate_tool import TOOLSETS, _strip_blocked_tools
        # Every toolset whose tools are ALL in the blocklist should be stripped
        for name, defn in TOOLSETS.items():
            tools = defn.get("tools", [])
            if tools and all(t in DELEGATE_BLOCKED_TOOLS for t in tools):
                self.assertNotIn(
                    name,
                    _strip_blocked_tools([name, "terminal"]),
                    f"Toolset {name!r} (tools={tools}) is fully blocked "
                    f"but was not stripped",
                )


class TestDelegateTask(unittest.TestCase):
    def test_no_parent_agent(self):
        result = json.loads(delegate_task(goal="test"))
        self.assertIn("error", result)
        self.assertIn("parent agent", result["error"])

    def test_depth_limit(self):
        parent = _make_mock_parent(depth=2)
        result = json.loads(delegate_task(goal="test", parent_agent=parent))
        self.assertIn("error", result)
        self.assertIn("depth limit", result["error"].lower())

    def test_no_goal_or_tasks(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(parent_agent=parent))
        self.assertIn("error", result)

    def test_empty_goal(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="  ", parent_agent=parent))
        self.assertIn("error", result)

    def test_task_missing_goal(self):
        parent = _make_mock_parent()
        result = json.loads(delegate_task(tasks=[{"context": "no goal here"}], parent_agent=parent))
        self.assertIn("error", result)

    @patch("tools.delegate_tool._run_single_child")
    def test_single_task_mode(self, mock_run):
        mock_run.return_value = {
            "task_index": 0, "status": "completed",
            "summary": "Done!", "api_calls": 3, "duration_seconds": 5.0
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="Fix tests", context="error log...", parent_agent=parent))
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["status"], "completed")
        self.assertEqual(result["results"][0]["summary"], "Done!")
        mock_run.assert_called_once()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode(self, mock_run):
        mock_run.side_effect = [
            {"task_index": 0, "status": "completed", "summary": "Result A", "api_calls": 2, "duration_seconds": 3.0},
            {"task_index": 1, "status": "completed", "summary": "Result B", "api_calls": 4, "duration_seconds": 6.0},
        ]
        parent = _make_mock_parent()
        tasks = [
            {"goal": "Research topic A"},
            {"goal": "Research topic B"},
        ]
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["summary"], "Result A")
        self.assertEqual(result["results"][1]["summary"], "Result B")
        self.assertIn("total_duration_seconds", result)

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode_accepts_json_string_tasks(self, mock_run):
        mock_run.side_effect = [
            {
                "task_index": 0,
                "status": "completed",
                "summary": "Result A",
                "api_calls": 2,
                "duration_seconds": 3.0,
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "Result B",
                "api_calls": 4,
                "duration_seconds": 6.0,
            },
        ]
        parent = _make_mock_parent()
        tasks = json.dumps(
            [
                {"goal": "Research topic A"},
                {"goal": "Research topic B"},
            ]
        )

        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))

        self.assertIn("results", result)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["summary"], "Result A")
        self.assertEqual(result["results"][1]["summary"], "Result B")

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode_rejects_non_object_tasks(self, mock_run):
        parent = _make_mock_parent()

        result = json.loads(
            delegate_task(tasks=["not a task object"], parent_agent=parent)
        )

        self.assertIn("error", result)
        self.assertIn("Task 0 must be an object", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_mode_rejects_malformed_json_string_tasks(self, mock_run):
        parent = _make_mock_parent()

        result = json.loads(
            delegate_task(tasks='[{"goal": "bad}', parent_agent=parent)
        )

        self.assertIn("error", result)
        self.assertIn("could not be parsed as JSON", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_capped_at_3(self, mock_run):
        mock_run.return_value = {
            "task_index": 0, "status": "completed",
            "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
        }
        parent = _make_mock_parent()
        limit = _get_max_concurrent_children()
        tasks = [{"goal": f"Task {i}"} for i in range(limit + 2)]
        result = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        # Should return an error instead of silently truncating
        self.assertIn("error", result)
        self.assertIn("Too many tasks", result["error"])
        mock_run.assert_not_called()

    @patch("tools.delegate_tool._run_single_child")
    def test_batch_ignores_toplevel_goal(self, mock_run):
        """When tasks array is provided, top-level goal/context/toolsets are ignored."""
        mock_run.return_value = {
            "task_index": 0, "status": "completed",
            "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(
            goal="This should be ignored",
            tasks=[{"goal": "Actual task"}],
            parent_agent=parent,
        ))
        # The mock was called with the tasks array item, not the top-level goal
        call_args = mock_run.call_args
        self.assertEqual(call_args.kwargs.get("goal") or call_args[1].get("goal", call_args[0][1] if len(call_args[0]) > 1 else None), "Actual task")

    @patch("tools.delegate_tool._run_single_child")
    def test_failed_child_included_in_results(self, mock_run):
        mock_run.return_value = {
            "task_index": 0, "status": "error",
            "summary": None, "error": "Something broke",
            "api_calls": 0, "duration_seconds": 0.5
        }
        parent = _make_mock_parent()
        result = json.loads(delegate_task(goal="Break things", parent_agent=parent))
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("Something broke", result["results"][0]["error"])

    def test_depth_increments(self):
        """Verify child gets parent's depth + 1."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test depth", parent_agent=parent)
            self.assertEqual(mock_child._delegate_depth, 1)

    def test_active_children_tracking(self):
        """Verify children are registered/unregistered for interrupt propagation."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test tracking", parent_agent=parent)
            self.assertEqual(len(parent._active_children), 0)

    def test_child_inherits_runtime_credentials(self):
        parent = _make_mock_parent(depth=0)
        parent.base_url = "https://chatgpt.com/backend-api/codex"
        parent.api_key="***"
        parent.provider = "openai-codex"
        parent.api_mode = "codex_responses"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "ok",
                "completed": True,
                "api_calls": 1,
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test runtime inheritance", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["base_url"], parent.base_url)
            self.assertEqual(kwargs["api_key"], parent.api_key)
            self.assertEqual(kwargs["provider"], parent.provider)
            self.assertEqual(kwargs["api_mode"], parent.api_mode)

    def test_child_inherits_parent_print_fn(self):
        parent = _make_mock_parent(depth=0)
        sink = MagicMock()
        parent._print_fn = sink

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Keep stdout clean",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertIs(mock_child._print_fn, sink)

    def test_child_uses_thinking_callback_when_progress_callback_available(self):
        parent = _make_mock_parent(depth=0)
        parent.tool_progress_callback = MagicMock()

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Avoid raw child spinners",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertTrue(callable(mock_child.thinking_callback))
        mock_child.thinking_callback("deliberating...")
        parent.tool_progress_callback.assert_not_called()


class TestToolNamePreservation(unittest.TestCase):
    """Verify _last_resolved_tool_names is restored after subagent runs."""

    def test_global_tool_names_restored_after_delegation(self):
        """The process-global _last_resolved_tool_names must be restored
        after a subagent completes so the parent's execute_code sandbox
        generates correct imports."""
        import model_tools

        parent = _make_mock_parent(depth=0)
        original_tools = ["terminal", "read_file", "web_search", "execute_code", "delegate_task"]
        model_tools._last_resolved_tool_names = list(original_tools)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1,
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test tool preservation", parent_agent=parent)

        self.assertEqual(model_tools._last_resolved_tool_names, original_tools)

    def test_global_tool_names_restored_after_child_failure(self):
        """Even when the child agent raises, the global must be restored."""
        import model_tools

        parent = _make_mock_parent(depth=0)
        original_tools = ["terminal", "read_file", "web_search"]
        model_tools._last_resolved_tool_names = list(original_tools)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.side_effect = RuntimeError("boom")
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Crash test", parent_agent=parent))
            self.assertEqual(result["results"][0]["status"], "error")

        self.assertEqual(model_tools._last_resolved_tool_names, original_tools)

    def test_build_child_agent_does_not_raise_name_error(self):
        """Regression: _build_child_agent must not reference _saved_tool_names.

        The bug introduced by the e7844e9c merge conflict: line 235 inside
        _build_child_agent read `list(_saved_tool_names)` where that variable
        is only defined later in _run_single_child.  Calling _build_child_agent
        standalone (without _run_single_child's scope) must never raise NameError.
        """
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent"):
            try:
                _build_child_agent(
                    task_index=0,
                    goal="regression check",
                    context=None,
                    toolsets=None,
                    model=None,
                    max_iterations=10,
                    parent_agent=parent,
                    task_count=1,
                )
            except NameError as exc:
                self.fail(
                    f"_build_child_agent raised NameError — "
                    f"_saved_tool_names leaked back into wrong scope: {exc}"
                )

    def test_build_child_agent_ignores_acp_command_when_binary_missing(self):
        """Stale delegation.command config must not force ACP subprocess mode."""
        parent = _make_mock_parent(depth=0)
        # The crash scenario is a TG/cron agent on a host with no ACP CLI —
        # parent itself has no acp_command, so clearing the override must NOT
        # fall through to a stray parent value.
        parent.acp_command = None
        parent.acp_args = []
        captured = {}

        with patch("run_agent.AIAgent") as MockAgent, \
             patch("shutil.which", return_value=None) as mock_which:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="search X for crypto twitter",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                override_acp_command="copilot",
                override_acp_args=["--foo"],
            )

            _, kwargs = MockAgent.call_args
            captured["provider"] = kwargs.get("provider")
            captured["acp_command"] = kwargs.get("acp_command")
            captured["acp_args"] = kwargs.get("acp_args")

        mock_which.assert_called_with("copilot")
        self.assertNotEqual(
            captured["provider"],
            "copilot-acp",
            "missing acp_command binary must NOT force copilot-acp provider",
        )
        self.assertIsNone(captured["acp_command"])
        self.assertEqual(captured["acp_args"], [])

    def test_build_child_agent_honors_acp_command_when_binary_present(self):
        """When the acp_command binary exists on PATH, behavior is unchanged:
        provider is forced to copilot-acp and command/args propagate to the
        child agent. Guards against the missing-binary check accidentally
        breaking working ACP delegation setups.
        """
        parent = _make_mock_parent(depth=0)
        captured = {}

        with patch("run_agent.AIAgent") as MockAgent, \
             patch("shutil.which", return_value="/usr/local/bin/copilot"):
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="copilot path",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                override_acp_command="copilot",
                override_acp_args=["--foo"],
            )

            _, kwargs = MockAgent.call_args
            captured["provider"] = kwargs.get("provider")
            captured["acp_command"] = kwargs.get("acp_command")

        self.assertEqual(captured["provider"], "copilot-acp")
        self.assertEqual(captured["acp_command"], "copilot")

    def test_schema_never_exposes_acp_transport_fields(self):
        """delegate_task must never make ACP transport model-facing."""
        from tools.delegate_tool import _build_dynamic_schema_overrides

        with patch("shutil.which", return_value="/usr/local/bin/copilot"):
            overrides = _build_dynamic_schema_overrides()

        props = overrides["parameters"]["properties"]
        self.assertNotIn("acp_command", props)
        self.assertNotIn("acp_args", props)

        task_item_props = props["tasks"]["items"]["properties"]
        self.assertNotIn("acp_command", task_item_props)
        self.assertNotIn("acp_args", task_item_props)

    def test_saved_tool_names_set_on_child_before_run(self):
        """_run_single_child must set _delegate_saved_tool_names on the child
        from model_tools._last_resolved_tool_names before run_conversation."""
        import model_tools

        parent = _make_mock_parent(depth=0)
        expected_tools = ["read_file", "web_search", "execute_code"]
        model_tools._last_resolved_tool_names = list(expected_tools)

        captured = {}

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()

            def capture_and_return(user_message, task_id=None, stream_callback=None):
                captured["saved"] = list(mock_child._delegate_saved_tool_names)
                return {"final_response": "ok", "completed": True, "api_calls": 1}

            mock_child.run_conversation.side_effect = capture_and_return
            MockAgent.return_value = mock_child

            delegate_task(goal="capture test", parent_agent=parent)

        self.assertEqual(captured["saved"], expected_tools)


class TestDelegateObservability(unittest.TestCase):
    """Tests for enriched metadata returned by _run_single_child."""

    def test_observability_fields_present(self):
        """Completed child should return tool_trace, tokens, model, exit_reason."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 5000
            mock_child.session_completion_tokens = 1200
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 3,
                "messages": [
                    {"role": "user", "content": "do something"},
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "web_search", "arguments": '{"query": "test"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": '{"results": [1,2,3]}'},
                    {"role": "assistant", "content": "done"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test observability", parent_agent=parent))
            entry = result["results"][0]

            # Core observability fields
            self.assertEqual(entry["model"], "claude-sonnet-4-6")
            self.assertEqual(entry["exit_reason"], "completed")
            self.assertEqual(entry["tokens"]["input"], 5000)
            self.assertEqual(entry["tokens"]["output"], 1200)

            # Tool trace
            self.assertEqual(len(entry["tool_trace"]), 1)
            self.assertEqual(entry["tool_trace"][0]["tool"], "web_search")
            self.assertIn("args_bytes", entry["tool_trace"][0])
            self.assertIn("result_bytes", entry["tool_trace"][0])
            self.assertEqual(entry["tool_trace"][0]["status"], "ok")

    def test_tool_trace_handles_list_content_blocks(self):
        """Tool-result content blocks should not crash observability metadata."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "image_generate", "arguments": '{"prompt": "x"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": [
                        {"type": "text", "text": '{"success": true}'},
                    ]},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test list content", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]
            self.assertEqual(trace[0]["tool"], "image_generate")
            self.assertEqual(trace[0]["status"], "ok")
            self.assertGreater(trace[0]["result_bytes"], 0)

    def test_output_tail_flattens_list_content_blocks(self):
        """_extract_output_tail (live overlay) must flatten content-block lists
        so error markers buried inside blocks are detected and previews are
        real text, not a "[{'type': 'text'...}]" repr blob."""
        result = {
            "messages": [
                {"role": "assistant", "tool_calls": [
                    {"id": "t1", "function": {"name": "terminal", "arguments": "{}"}}
                ]},
                {"role": "tool", "tool_call_id": "t1", "content": [
                    {"type": "text", "text": "Error: command not found"},
                ]},
                {"role": "assistant", "tool_calls": [
                    {"id": "t2", "function": {"name": "vision", "arguments": "{}"}}
                ]},
                {"role": "tool", "tool_call_id": "t2", "content": [
                    {"type": "text", "text": "all good"},
                    {"type": "image_url", "image_url": {"url": "data:x"}},
                ]},
            ]
        }
        tail = _extract_output_tail(result, max_entries=8, max_chars=600)
        by_tool = {t["tool"]: t for t in tail}

        # Block-wrapped error is correctly flagged (crude str() would miss it).
        self.assertTrue(by_tool["terminal"]["is_error"])
        self.assertEqual(by_tool["terminal"]["preview"], "Error: command not found")
        # Non-error multimodal result is not flagged, and the text is readable.
        self.assertFalse(by_tool["vision"]["is_error"])
        self.assertIn("all good", by_tool["vision"]["preview"])
        # No raw content-block repr leaked into any preview.
        for entry in tail:
            self.assertNotIn("'type'", entry["preview"])

    def test_tool_trace_detects_error(self):
        """Tool results containing 'error' should be marked as error status."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "failed",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "terminal", "arguments": '{"cmd": "ls"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Error: command not found"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test error trace", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]
            self.assertEqual(trace[0]["status"], "error")

    def test_parallel_tool_calls_paired_correctly(self):
        """Parallel tool calls should each get their own result via tool_call_id matching."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 3000
            mock_child.session_completion_tokens = 800
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_a", "function": {"name": "web_search", "arguments": '{"q": "a"}'}},
                        {"id": "tc_b", "function": {"name": "web_search", "arguments": '{"q": "b"}'}},
                        {"id": "tc_c", "function": {"name": "terminal", "arguments": '{"cmd": "ls"}'}},
                    ]},
                    {"role": "tool", "tool_call_id": "tc_a", "content": '{"ok": true}'},
                    {"role": "tool", "tool_call_id": "tc_b", "content": "Error: rate limited"},
                    {"role": "tool", "tool_call_id": "tc_c", "content": "file1.txt\nfile2.txt"},
                    {"role": "assistant", "content": "done"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test parallel", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]

            # All three tool calls should have results
            self.assertEqual(len(trace), 3)

            # First: web_search → ok
            self.assertEqual(trace[0]["tool"], "web_search")
            self.assertEqual(trace[0]["status"], "ok")
            self.assertIn("result_bytes", trace[0])

            # Second: web_search → error
            self.assertEqual(trace[1]["tool"], "web_search")
            self.assertEqual(trace[1]["status"], "error")
            self.assertIn("result_bytes", trace[1])

            # Third: terminal → ok
            self.assertEqual(trace[2]["tool"], "terminal")
            self.assertEqual(trace[2]["status"], "ok")
            self.assertIn("result_bytes", trace[2])

    def test_exit_reason_interrupted(self):
        """Interrupted child should report exit_reason='interrupted'."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "",
                "completed": False,
                "interrupted": True,
                "api_calls": 2,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test interrupt", parent_agent=parent))
            self.assertEqual(result["results"][0]["exit_reason"], "interrupted")

    def test_exit_reason_max_iterations(self):
        """Child that didn't complete and wasn't interrupted hit max_iterations."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "",
                "completed": False,
                "interrupted": False,
                "api_calls": 50,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test max iter", parent_agent=parent))
            self.assertEqual(result["results"][0]["exit_reason"], "max_iterations")

    def test_empty_sentinel_marks_status_failed(self):
        """Regression: a child that returns the literal '(empty)' sentinel
        (emitted by run_agent.py when the LLM returns empty responses after
        retries — e.g. transport misrouting) must be reported as failed, not
        silently accepted as a completed delegation. Otherwise the parent
        surfaces an empty string as if the subagent succeeded."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "(empty)",
                "completed": True,
                "interrupted": False,
                "api_calls": 4,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test empty sentinel", parent_agent=parent))
            self.assertEqual(result["results"][0]["status"], "failed")


class TestSubagentCostRollup(unittest.TestCase):
    """Port of Kilo-Org/kilocode#9448 — parent's session_estimated_cost_usd
    must include subagent spend, not just the parent's own API calls."""

    def _make_parent_with_cost_counters(self, depth=0, starting_cost=0.0):
        parent = _make_mock_parent(depth=depth)
        # The fields AIAgent exposes and the footer reads from.  Set real
        # floats/strings so the rollup can add to them rather than tripping
        # on MagicMock auto-attrs.
        parent.session_estimated_cost_usd = starting_cost
        parent.session_cost_status = "unknown"
        parent.session_cost_source = "none"
        return parent

    def test_single_child_cost_folded_into_parent(self):
        parent = self._make_parent_with_cost_counters(starting_cost=0.10)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 1000
            mock_child.session_completion_tokens = 200
            mock_child.session_estimated_cost_usd = 0.42
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 2,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="do stuff", parent_agent=parent))

        # Parent footer must reflect parent_cost + child_cost.
        self.assertAlmostEqual(parent.session_estimated_cost_usd, 0.52, places=6)
        # Rollup must strip the internal field before serialising to the model.
        self.assertNotIn("_child_cost_usd", result["results"][0])
        self.assertNotIn("_child_role", result["results"][0])

    def test_batch_children_costs_sum_into_parent(self):
        parent = self._make_parent_with_cost_counters(starting_cost=0.00)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.side_effect = [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "A",
                    "api_calls": 2,
                    "duration_seconds": 1.0,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.15,
                },
                {
                    "task_index": 1,
                    "status": "completed",
                    "summary": "B",
                    "api_calls": 2,
                    "duration_seconds": 1.0,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.27,
                },
                {
                    "task_index": 2,
                    "status": "failed",
                    "summary": "",
                    "error": "boom",
                    "api_calls": 0,
                    "duration_seconds": 0.1,
                    "_child_role": "leaf",
                    "_child_cost_usd": 0.03,
                },
            ]
            result = json.loads(
                delegate_task(
                    tasks=[{"goal": "A"}, {"goal": "B"}, {"goal": "C"}],
                    parent_agent=parent,
                )
            )

        # 0.15 + 0.27 + 0.03 even though one child failed — the API calls it
        # made before failing still cost money.
        self.assertAlmostEqual(parent.session_estimated_cost_usd, 0.45, places=6)
        # cost_source promoted from "none" since the parent had no direct spend.
        self.assertEqual(parent.session_cost_source, "subagent")
        self.assertEqual(parent.session_cost_status, "estimated")
        # All internal fields stripped from results.
        for entry in result["results"]:
            self.assertNotIn("_child_cost_usd", entry)
            self.assertNotIn("_child_role", entry)

    def test_zero_cost_children_leave_parent_source_untouched(self):
        """If every child reports 0 cost (e.g. free local model), we should
        not invent a fake 'subagent' source — the parent's 'none' stays."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.00)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                "_child_role": "leaf",
                "_child_cost_usd": 0.0,
            }
            delegate_task(goal="free local run", parent_agent=parent)

        self.assertEqual(parent.session_estimated_cost_usd, 0.0)
        self.assertEqual(parent.session_cost_source, "none")

    def test_parent_with_real_source_not_overwritten(self):
        """If the parent already has its own cost billed (cost_source != 'none'),
        adding subagent cost must not clobber the existing source label."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.20)
        parent.session_cost_status = "exact"
        parent.session_cost_source = "openrouter"

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                "_child_role": "leaf",
                "_child_cost_usd": 0.30,
            }
            delegate_task(goal="billed run", parent_agent=parent)

        self.assertAlmostEqual(parent.session_estimated_cost_usd, 0.50, places=6)
        # Real source label preserved.
        self.assertEqual(parent.session_cost_source, "openrouter")
        self.assertEqual(parent.session_cost_status, "exact")

    def test_rollup_tolerates_missing_cost_fields(self):
        """Older fixtures / fabricated error entries may not carry
        _child_cost_usd.  Rollup must degrade to zero-add silently."""
        parent = self._make_parent_with_cost_counters(starting_cost=0.10)

        with patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_run.return_value = {
                "task_index": 0,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.5,
                # no _child_role, no _child_cost_usd
            }
            result = json.loads(delegate_task(goal="legacy", parent_agent=parent))

        # Parent cost unchanged.
        self.assertEqual(parent.session_estimated_cost_usd, 0.10)
        self.assertEqual(len(result["results"]), 1)


class TestBlockedTools(unittest.TestCase):
    def test_blocked_tools_constant(self):
        for tool in ["delegate_task", "clarify", "memory", "send_message", "execute_code"]:
            self.assertIn(tool, DELEGATE_BLOCKED_TOOLS)

    def test_constants(self):
        from tools.delegate_tool import (
            _get_max_spawn_depth, _get_orchestrator_enabled,
            _MIN_SPAWN_DEPTH,
        )
        self.assertEqual(_get_max_concurrent_children(), 3)
        self.assertEqual(MAX_DEPTH, 1)
        self.assertEqual(_get_max_spawn_depth(), 1)       # default: flat
        self.assertTrue(_get_orchestrator_enabled())      # default
        self.assertEqual(_MIN_SPAWN_DEPTH, 1)


class TestDelegationCredentialResolution(unittest.TestCase):
    """Tests for provider:model credential resolution in delegation config."""

    def test_no_provider_returns_none_credentials(self):
        """When delegation.provider is empty, all credentials are None (inherit parent)."""
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "", "provider": ""}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["provider"])
        self.assertIsNone(creds["base_url"])
        self.assertIsNone(creds["api_key"])
        self.assertIsNone(creds["api_mode"])
        self.assertIsNone(creds["model"])

    def test_model_only_no_provider(self):
        """When only model is set (no provider), model is returned but credentials are None."""
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "google/gemini-3-flash-preview", "provider": ""}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["model"], "google/gemini-3-flash-preview")
        self.assertIsNone(creds["provider"])
        self.assertIsNone(creds["base_url"])
        self.assertIsNone(creds["api_key"])



    def test_direct_endpoint_uses_configured_base_url_and_api_key(self):
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "provider": "openrouter",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["model"], "qwen2.5-coder")
        self.assertEqual(creds["provider"], "custom")
        self.assertEqual(creds["base_url"], "http://localhost:1234/v1")
        self.assertEqual(creds["api_key"], "local-key")
        self.assertEqual(creds["api_mode"], "chat_completions")

    def test_direct_endpoint_auto_detects_anthropic_messages_suffix(self):
        # Issue #10213: Azure AI Foundry exposes Anthropic-compatible models at
        # a /anthropic URL suffix. Subagents must pick anthropic_messages
        # automatically, matching the main agent's runtime resolver.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["provider"], "custom")
        self.assertEqual(creds["base_url"], "https://myfoundry.services.ai.azure.com/anthropic")
        self.assertEqual(creds["api_key"], "foundry-key")
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_honors_explicit_api_mode(self):
        # When delegation.api_mode is set explicitly, it overrides URL-based
        # detection so users can force a transport on non-standard endpoints.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://proxy.example.com/v1",
            "api_key": "proxy-key",
            "api_mode": "anthropic_messages",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_explicit_api_mode_overrides_url_detection(self):
        # Explicit api_mode in config always wins over auto-detection.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
            "api_mode": "chat_completions",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "chat_completions")

    def test_direct_endpoint_invalid_api_mode_falls_back_to_detection(self):
        # An invalid api_mode string must not break detection; fall back to URL heuristic.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "claude-opus-4-6",
            "provider": "custom",
            "base_url": "https://myfoundry.services.ai.azure.com/anthropic",
            "api_key": "foundry-key",
            "api_mode": "garbage",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["api_mode"], "anthropic_messages")

    def test_direct_endpoint_returns_none_api_key_when_not_configured(self):
        # When base_url is set without api_key, api_key should be None so
        # _build_child_agent inherits the parent's key (effective_api_key = override or parent).
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-openai-key"}, clear=False):
            creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["api_key"])
        self.assertEqual(creds["provider"], "custom")

    def test_direct_endpoint_no_raise_when_only_provider_env_key_present(self):
        # Even if OPENAI_API_KEY is absent, no ValueError — _build_child_agent uses parent key.
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
        }
        with patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "env-openrouter-key",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        ):
            creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["api_key"])
        self.assertEqual(creds["provider"], "custom")


    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_provider_resolution_failure_raises_valueerror(self, mock_resolve):
        """When provider resolution fails, ValueError is raised with helpful message."""
        mock_resolve.side_effect = RuntimeError("OPENROUTER_API_KEY not set")
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": "openrouter"}
        with self.assertRaises(ValueError) as ctx:
            _resolve_delegation_credentials(cfg, parent)
        self.assertIn("openrouter", str(ctx.exception).lower())
        self.assertIn("Cannot resolve", str(ctx.exception))

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_provider_resolves_but_no_api_key_raises(self, mock_resolve):
        """When provider resolves but has no API key, ValueError is raised."""
        mock_resolve.return_value = {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": "openrouter"}
        with self.assertRaises(ValueError) as ctx:
            _resolve_delegation_credentials(cfg, parent)
        self.assertIn("no API key", str(ctx.exception))

    def test_missing_config_keys_inherit_parent(self):
        """When config dict has no model/provider keys at all, inherits parent."""
        parent = _make_mock_parent(depth=0)
        cfg = {"max_iterations": 45}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["model"])
        self.assertIsNone(creds["provider"])

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_named_custom_provider_preserves_provider_name(self, mock_resolve):
        """Named custom provider (e.g. crof.ai) resolves to 'custom' at runtime level
        but the subagent must retain the original provider identity so that
        resolve_provider_client routes to the correct endpoint on retry/fallback.
        Regression test for #26954.
        """
        mock_resolve.return_value = {
            "provider": "custom",  # runtime marks it as "custom" type
            "model": "deepseek-v4-pro-CEER",
            "base_url": "https://api.crof.ai/v1",
            "api_key": "crof-key-abc",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "deepseek-v4-pro-CEER", "provider": "crof.ai"}
        creds = _resolve_delegation_credentials(cfg, parent)
        # The key assertion: subagent must keep "crof.ai", NOT "custom"
        self.assertEqual(creds["provider"], "crof.ai")
        self.assertEqual(creds["model"], "deepseek-v4-pro-CEER")
        self.assertEqual(creds["base_url"], "https://api.crof.ai/v1")
        self.assertEqual(creds["api_key"], "crof-key-abc")
        # Verify resolve_runtime_provider was called with the configured name
        mock_resolve.assert_called_once_with(
            requested="crof.ai", target_model="deepseek-v4-pro-CEER"
        )

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_standard_provider_not_overwritten_by_configured_name(self, mock_resolve):
        """Standard (non-custom) providers must still return runtime identity,
        not the configured name, to preserve existing behaviour for openrouter,
        nous, etc.
        """
        mock_resolve.return_value = {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "or-key-xyz",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "anthropic/claude-sonnet-4", "provider": "openrouter"}
        creds = _resolve_delegation_credentials(cfg, parent)
        # Standard provider returns its own name, not "custom"
        self.assertEqual(creds["provider"], "openrouter")

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_custom_provider_with_empty_configured_provider_falls_back_to_runtime(self, mock_resolve):
        """When configured_provider is empty/None, the early return kicks in and
        we return provider=None regardless of what runtime resolved. The runtime
        path is only reached when configured_provider is a non-empty string.
        """
        mock_resolve.return_value = {
            "provider": "custom",
            "model": "some-model",
            "base_url": "https://fallback.example.com/v1",
            "api_key": "key-fallback",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": ""}
        creds = _resolve_delegation_credentials(cfg, parent)
        # Empty provider → early return with None (child inherits parent)
        self.assertIsNone(creds["provider"])

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_runtime_missing_provider_key_returns_none(self, mock_resolve):
        """When resolve_runtime_provider returns a dict without 'provider' key,
        the result must be None regardless of configured_provider.
        This protects against malformed runtime responses.
        """
        mock_resolve.return_value = {
            # deliberately missing "provider"
            "model": "some-model",
            "base_url": "https://example.com/v1",
            "api_key": "key-123",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "some-model", "provider": "crof.ai"}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["provider"])

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_bedrock_provider_with_base_url_uses_runtime_resolver(self, mock_resolve):
        """Regression: provider=bedrock + base_url set must NOT fall through the
        direct-base_url branch (which would force provider='custom' +
        chat_completions and silently misroute OpenAI JSON to the Bedrock
        native endpoint, returning empty responses)."""
        mock_resolve.return_value = {
            "provider": "bedrock",
            "base_url": "https://bedrock-runtime.us-west-2.amazonaws.com",
            "api_key": "aws-resolved-key",
            "api_mode": "bedrock_converse",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "us.anthropic.claude-sonnet-4-6",
            "provider": "bedrock",
            "base_url": "https://bedrock-runtime.us-west-2.amazonaws.com",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        # Must use Bedrock, not 'custom'
        self.assertEqual(creds["provider"], "bedrock")
        self.assertEqual(creds["api_mode"], "bedrock_converse")
        mock_resolve.assert_called_once()
        self.assertEqual(mock_resolve.call_args.kwargs.get("requested"), "bedrock")



class TestDelegationProviderIntegration(unittest.TestCase):
    """Integration tests: delegation config → _run_single_child → AIAgent construction."""

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_config_provider_credentials_reach_child_agent(self, mock_creds, mock_cfg):
        """When delegation.provider is configured, child agent gets resolved credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-delegation-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test provider routing", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "google/gemini-3-flash-preview")
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
            self.assertEqual(kwargs["api_key"], "sk-or-delegation-key")
            self.assertEqual(kwargs["api_mode"], "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_cross_provider_delegation(self, mock_creds, mock_cfg):
        """Parent on Nous, subagent on OpenRouter — full credential switch."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        parent.provider = "nous"
        parent.base_url = "https://inference-api.nousresearch.com/v1"
        parent.api_key = "nous-key-abc"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Cross-provider test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            # Child should use OpenRouter, NOT Nous
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
            self.assertEqual(kwargs["api_key"], "sk-or-key")
            self.assertNotEqual(kwargs["base_url"], parent.base_url)
            self.assertNotEqual(kwargs["api_key"], parent.api_key)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_provider_override_clears_parent_openrouter_filters(
        self, mock_creds, mock_cfg
    ):
        """Delegated provider should not inherit parent provider-preference filters."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        parent.providers_allowed = ["anthropic/claude-3.5-sonnet"]
        parent.providers_ignored = ["openai/gpt-4o-mini"]
        parent.providers_order = ["google/gemini-2.5-pro"]
        parent.provider_sort = "price"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "api_calls": 1,
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Cross-provider test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["provider"], "openrouter")
            self.assertIsNone(kwargs["providers_allowed"])
            self.assertIsNone(kwargs["providers_ignored"])
            self.assertIsNone(kwargs["providers_order"])
            self.assertIsNone(kwargs["provider_sort"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_direct_endpoint_credentials_reach_child_agent(self, mock_creds, mock_cfg):
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "qwen2.5-coder",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
        }
        mock_creds.return_value = {
            "model": "qwen2.5-coder",
            "provider": "custom",
            "base_url": "http://localhost:1234/v1",
            "api_key": "local-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Direct endpoint test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], "qwen2.5-coder")
            self.assertEqual(kwargs["provider"], "custom")
            self.assertEqual(kwargs["base_url"], "http://localhost:1234/v1")
            self.assertEqual(kwargs["api_key"], "local-key")
            self.assertEqual(kwargs["api_mode"], "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_empty_config_inherits_parent(self, mock_creds, mock_cfg):
        """When delegation config is empty, child inherits parent credentials."""
        mock_cfg.return_value = {"max_iterations": 45, "model": "", "provider": ""}
        mock_creds.return_value = {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Test inherit", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["model"], parent.model)
            self.assertEqual(kwargs["provider"], parent.provider)
            self.assertEqual(kwargs["base_url"], parent.base_url)

    def test_inherit_parent_base_url_prefers_client_kwargs(self):
        parent = _make_mock_parent(depth=0)
        parent.base_url = "https://openrouter.ai/api/v1"
        parent._client_kwargs = {
            "api_key": "no-key-required",
            "base_url": "http://localhost:11434/v1",
        }
        self.assertEqual(
            _inherit_parent_base_url(parent, parent.base_url),
            "http://localhost:11434/v1",
        )

    def test_build_child_agent_inherits_active_client_endpoint(self):
        """Regression: stale parent.base_url must not route subagents to OpenRouter."""
        parent = _make_mock_parent(depth=0)
        parent.provider = "ollama"
        parent.base_url = "https://openrouter.ai/api/v1"
        parent.api_key = "ollama"
        parent._client_kwargs = {
            "api_key": "no-key-required",
            "base_url": "http://localhost:11434/v1",
        }

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child
            _build_child_agent(
                task_index=0,
                goal="Use local Ollama",
                context=None,
                toolsets=["terminal"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["base_url"], "http://localhost:11434/v1")
            self.assertEqual(kwargs["api_key"], "ollama")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_credential_error_returns_json_error(self, mock_creds, mock_cfg):
        """When credential resolution fails, delegate_task returns a JSON error."""
        mock_cfg.return_value = {"model": "bad-model", "provider": "nonexistent"}
        mock_creds.side_effect = ValueError(
            "Cannot resolve delegation provider 'nonexistent': Unknown provider"
        )
        parent = _make_mock_parent(depth=0)

        result = json.loads(delegate_task(goal="Should fail", parent_agent=parent))
        self.assertIn("error", result)
        self.assertIn("Cannot resolve", result["error"])
        self.assertIn("nonexistent", result["error"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_batch_mode_all_children_get_credentials(self, mock_creds, mock_cfg):
        """In batch mode, all children receive the resolved credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "meta-llama/llama-4-scout",
            "provider": "openrouter",
        }
        mock_creds.return_value = {
            "model": "meta-llama/llama-4-scout",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-batch",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)

        # Patch _build_child_agent since credentials are now passed there
        # (agents are built in the main thread before being handed to workers)
        with patch("tools.delegate_tool._build_child_agent") as mock_build, \
             patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_child = MagicMock()
            mock_build.return_value = mock_child
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
            }

            tasks = [{"goal": "Task A"}, {"goal": "Task B"}]
            delegate_task(tasks=tasks, parent_agent=parent)

            self.assertEqual(mock_build.call_count, 2)
            for call in mock_build.call_args_list:
                self.assertEqual(call.kwargs.get("model"), "meta-llama/llama-4-scout")
                self.assertEqual(call.kwargs.get("override_provider"), "openrouter")
                self.assertEqual(call.kwargs.get("override_base_url"), "https://openrouter.ai/api/v1")
                self.assertEqual(call.kwargs.get("override_api_key"), "sk-or-batch")
                self.assertEqual(call.kwargs.get("override_api_mode"), "chat_completions")

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_delegation_acp_runtime_reaches_child_agent(self, mock_creds, mock_cfg):
        """Resolved ACP runtime command/args must be forwarded to child agents."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "copilot-model",
            "provider": "copilot-acp",
        }
        mock_creds.return_value = {
            "model": "copilot-model",
            "provider": "copilot-acp",
            "base_url": "acp://copilot",
            "api_key": "copilot-acp",
            "api_mode": "chat_completions",
            "command": "custom-copilot",
            "args": ["--stdio-custom"],
        }
        parent = _make_mock_parent(depth=0)

        with patch("tools.delegate_tool._build_child_agent") as mock_build, \
             patch("tools.delegate_tool._run_single_child") as mock_run:
            mock_child = MagicMock()
            mock_build.return_value = mock_child
            mock_run.return_value = {
                "task_index": 0, "status": "completed",
                "summary": "Done", "api_calls": 1, "duration_seconds": 1.0
            }

            delegate_task(goal="ACP delegation test", parent_agent=parent)

            _, kwargs = mock_build.call_args
            self.assertEqual(kwargs.get("override_provider"), "copilot-acp")
            self.assertEqual(kwargs.get("override_base_url"), "acp://copilot")
            self.assertEqual(kwargs.get("override_api_key"), "copilot-acp")
            self.assertEqual(kwargs.get("override_api_mode"), "chat_completions")
            self.assertEqual(kwargs.get("override_acp_command"), "custom-copilot")
            self.assertEqual(kwargs.get("override_acp_args"), ["--stdio-custom"])

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_model_only_no_provider_inherits_parent_credentials(self, mock_creds, mock_cfg):
        """Setting only model (no provider) changes model but keeps parent credentials."""
        mock_cfg.return_value = {
            "max_iterations": 45,
            "model": "google/gemini-3-flash-preview",
            "provider": "",
        }
        mock_creds.return_value = {
            "model": "google/gemini-3-flash-preview",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True, "api_calls": 1
            }
            MockAgent.return_value = mock_child

            delegate_task(goal="Model only test", parent_agent=parent)

            _, kwargs = MockAgent.call_args
            # Model should be overridden
            self.assertEqual(kwargs["model"], "google/gemini-3-flash-preview")
            # But provider/base_url/api_key should inherit from parent
            self.assertEqual(kwargs["provider"], parent.provider)
            self.assertEqual(kwargs["base_url"], parent.base_url)


class TestChildCredentialPoolResolution(unittest.TestCase):
    def test_same_provider_shares_parent_pool(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        result = _resolve_child_credential_pool("openrouter", parent)
        self.assertIs(result, mock_pool)

    def test_no_provider_inherits_parent_pool(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        result = _resolve_child_credential_pool(None, parent)
        self.assertIs(result, mock_pool)

    def test_different_provider_loads_own_pool(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()
        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = True

        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIs(result, mock_pool)

    def test_different_provider_empty_pool_returns_none(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()
        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = False

        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIsNone(result)

    def test_different_provider_load_failure_returns_none(self):
        parent = _make_mock_parent()
        parent._credential_pool = MagicMock()

        with patch("agent.credential_pool.load_pool", side_effect=Exception("disk error")):
            result = _resolve_child_credential_pool("anthropic", parent)

        self.assertIsNone(result)

    # --- Custom-endpoint identity resolution (issue #7833) ---

    def test_custom_different_endpoint_does_not_inherit_parent_pool(self):
        """A child on custom endpoint B must not inherit the parent's custom
        endpoint A pool just because both normalize to provider='custom'."""
        parent = _make_mock_parent()
        parent.provider = "custom"
        parent.base_url = "https://endpoint-a.example.com/v1"
        parent._credential_pool = MagicMock(name="parent_custom_a_pool")

        child_pool = MagicMock(name="endpoint_b_pool")
        child_pool.has_credentials.return_value = True

        def fake_key(base_url, provider_name=None):
            return {
                "https://endpoint-a.example.com/v1": "custom:endpoint-a",
                "https://endpoint-b.example.com/v1": "custom:endpoint-b",
            }.get(base_url)

        with patch("agent.credential_pool.get_custom_provider_pool_key", side_effect=fake_key), \
             patch("agent.credential_pool.load_pool", return_value=child_pool) as load_mock:
            result = _resolve_child_credential_pool(
                "custom", parent, "https://endpoint-b.example.com/v1"
            )

        # Loaded the child's OWN endpoint pool, not the parent's.
        load_mock.assert_called_once_with("custom:endpoint-b")
        self.assertIs(result, child_pool)
        self.assertIsNot(result, parent._credential_pool)

    def test_custom_same_endpoint_shares_parent_pool(self):
        """A child on the SAME custom endpoint as the parent reuses the parent's
        pool so rotation/cooldown state stays synchronized."""
        parent = _make_mock_parent()
        parent.provider = "custom"
        parent.base_url = "https://endpoint-a.example.com/v1"
        parent._credential_pool = MagicMock(name="parent_custom_a_pool")

        with patch(
            "agent.credential_pool.get_custom_provider_pool_key",
            return_value="custom:endpoint-a",
        ):
            result = _resolve_child_credential_pool(
                "custom", parent, "https://endpoint-a.example.com/v1"
            )

        self.assertIs(result, parent._credential_pool)

    def test_custom_unregistered_endpoint_returns_none(self):
        """A raw delegation.base_url with no matching custom_providers entry
        must NOT inherit the parent's pool — return None so the child keeps its
        fixed delegated credential."""
        parent = _make_mock_parent()
        parent.provider = "custom"
        parent.base_url = "https://endpoint-a.example.com/v1"
        parent._credential_pool = MagicMock(name="parent_custom_a_pool")

        with patch(
            "agent.credential_pool.get_custom_provider_pool_key",
            return_value=None,
        ):
            result = _resolve_child_credential_pool(
                "custom", parent, "https://raw-unregistered.example.com/v1"
            )

        self.assertIsNone(result)

    def test_build_child_agent_assigns_parent_pool_when_shared(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test pool assignment",
                context=None,
                toolsets=["terminal"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

            self.assertEqual(mock_child._credential_pool, mock_pool)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_build_child_agent_preserves_mcp_toolsets_by_default(self, mock_cfg):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["web", "browser", "mcp-MiniMax"]

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test narrowed toolsets",
                context=None,
                toolsets=["web", "browser"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertEqual(
            MockAgent.call_args[1]["enabled_toolsets"],
            ["web", "browser", "mcp-MiniMax"],
        )

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"inherit_mcp_toolsets": False},
    )
    def test_build_child_agent_strict_intersection_when_opted_out(self, mock_cfg):
        parent = _make_mock_parent()
        parent.enabled_toolsets = ["web", "browser", "mcp-MiniMax"]

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Test narrowed toolsets",
                context=None,
                toolsets=["web", "browser"],
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        self.assertEqual(
            MockAgent.call_args[1]["enabled_toolsets"],
            ["web", "browser"],
        )


class TestChildCredentialLeasing(unittest.TestCase):
    def test_run_single_child_acquires_and_releases_lease(self):
        from tools.delegate_tool import _run_single_child

        leased_entry = MagicMock()
        leased_entry.id = "cred-b"

        child = MagicMock()
        child._credential_pool = MagicMock()
        child._credential_pool.acquire_lease.return_value = "cred-b"
        child._credential_pool.current.return_value = leased_entry
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

        result = _run_single_child(
            task_index=0,
            goal="Investigate rate limits",
            child=child,
            parent_agent=_make_mock_parent(),
        )

        self.assertEqual(result["status"], "completed")
        child._credential_pool.acquire_lease.assert_called_once_with()
        child._swap_credential.assert_called_once_with(leased_entry)
        child._credential_pool.release_lease.assert_called_once_with("cred-b")

    def test_run_single_child_releases_lease_after_failure(self):
        from tools.delegate_tool import _run_single_child

        child = MagicMock()
        child._credential_pool = MagicMock()
        child._credential_pool.acquire_lease.return_value = "cred-a"
        child._credential_pool.current.return_value = MagicMock(id="cred-a")
        child.run_conversation.side_effect = RuntimeError("boom")

        result = _run_single_child(
            task_index=1,
            goal="Trigger failure",
            child=child,
            parent_agent=_make_mock_parent(),
        )

        self.assertEqual(result["status"], "error")
        child._credential_pool.release_lease.assert_called_once_with("cred-a")


class TestDelegateHeartbeat(unittest.TestCase):
    """Heartbeat propagates child activity to parent during delegation.

    Without the heartbeat, the gateway inactivity timeout fires because the
    parent's _last_activity_ts freezes when delegate_task starts.
    """

    def test_heartbeat_touches_parent_activity_during_child_run(self):
        """Parent's _touch_activity is called while child.run_conversation blocks."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": "terminal",
            "api_call_count": 3,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: terminal",
        }

        # Make run_conversation block long enough for heartbeats to fire
        def slow_run(**kwargs):
            time.sleep(0.25)
            return {"final_response": "done", "completed": True, "api_calls": 3}

        child.run_conversation.side_effect = slow_run

        # Patch the heartbeat interval to fire quickly
        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test heartbeat",
                child=child,
                parent_agent=parent,
            )

        # Heartbeat should have fired at least once during the 0.25s sleep
        self.assertGreater(len(touch_calls), 0,
                           "Heartbeat did not propagate activity to parent")
        # Verify the description includes child's current tool detail
        self.assertTrue(
            any("terminal" in desc for desc in touch_calls),
            f"Heartbeat descriptions should include child tool info: {touch_calls}")

    def test_heartbeat_stops_after_child_completes(self):
        """Heartbeat thread is cleaned up when the child finishes."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": None,
            "api_call_count": 1,
            "max_iterations": 50,
            "last_activity_desc": "done",
        }
        child.run_conversation.return_value = {
            "final_response": "done", "completed": True, "api_calls": 1,
        }

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test cleanup",
                child=child,
                parent_agent=parent,
            )

        # Record count after completion, wait, and verify no more calls
        count_after = len(touch_calls)
        time.sleep(0.15)
        self.assertEqual(len(touch_calls), count_after,
                         "Heartbeat continued firing after child completed")

    def test_heartbeat_stops_after_child_error(self):
        """Heartbeat thread is cleaned up even when the child raises."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": "web_search",
            "api_call_count": 2,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: web_search",
        }

        def slow_fail(**kwargs):
            time.sleep(0.15)
            raise RuntimeError("network timeout")

        child.run_conversation.side_effect = slow_fail

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            result = _run_single_child(
                task_index=0,
                goal="Test error cleanup",
                child=child,
                parent_agent=parent,
            )

        self.assertEqual(result["status"], "error")

        # Verify heartbeat stopped
        count_after = len(touch_calls)
        time.sleep(0.15)
        self.assertEqual(len(touch_calls), count_after,
                         "Heartbeat continued firing after child error")

    def test_heartbeat_includes_child_activity_desc_when_no_tool(self):
        """When child has no current_tool, heartbeat uses last_activity_desc."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": None,
            "api_call_count": 5,
            "max_iterations": 90,
            "last_activity_desc": "API call #5 completed",
        }

        def slow_run(**kwargs):
            time.sleep(0.15)
            return {"final_response": "done", "completed": True, "api_calls": 5}

        child.run_conversation.side_effect = slow_run

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05):
            _run_single_child(
                task_index=0,
                goal="Test desc fallback",
                child=child,
                parent_agent=parent,
            )

        self.assertGreater(len(touch_calls), 0)
        self.assertTrue(
            any("API call #5 completed" in desc for desc in touch_calls),
            f"Heartbeat should include last_activity_desc: {touch_calls}")

    def test_heartbeat_does_not_trip_idle_stale_while_inside_tool(self):
        """A long-running tool (no iteration advance, but current_tool set)
        must not be flagged stale at the idle threshold.

        Bug #13041: when a child is legitimately busy inside a slow tool
        (terminal command, browser fetch), api_call_count does not advance.
        The previous stale check treated this as idle and stopped the
        heartbeat after 5 cycles (~150s), letting the gateway kill the
        session. The fix uses a much higher in-tool threshold and only
        applies the tight idle threshold when current_tool is None.
        """
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        parent._touch_activity = lambda desc: touch_calls.append(desc)

        child = MagicMock()
        # Child is stuck inside a single terminal call for the whole run.
        # api_call_count never advances, current_tool is always set.
        child.get_activity_summary.return_value = {
            "current_tool": "terminal",
            "api_call_count": 1,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: terminal",
        }

        def slow_run(**kwargs):
            # Long enough to exceed the OLD idle threshold (5 cycles) at
            # the patched interval, but shorter than the new in-tool
            # threshold.
            time.sleep(0.4)
            return {"final_response": "done", "completed": True, "api_calls": 1}

        child.run_conversation.side_effect = slow_run

        # Use tiny thresholds so the assertion is scheduler-robust in CI:
        # if idle rules were used for in-tool work, heartbeat would stop after
        # ~2 cycles. The in-tool branch should keep touching well past that.
        with (
            patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.05),
            patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE", 2),
            patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IN_TOOL", 40),
        ):
            _run_single_child(
                task_index=0,
                goal="Test long-running tool",
                child=child,
                parent_agent=parent,
            )

        # If idle-threshold logic applied, we'd cap around 2 touches; prove we
        # continued beyond that while inside a long-running tool.
        self.assertGreater(
            len(touch_calls), 2,
            f"Heartbeat stopped too early while child was inside a tool; "
            f"got {len(touch_calls)} touches over 0.4s at 0.05s interval",
        )



class TestDelegationReasoningEffort(unittest.TestCase):
    """Tests for delegation.reasoning_effort config override."""

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_inherits_parent_reasoning_when_no_override(self, MockAgent, mock_cfg):
        """With no delegation.reasoning_effort, child inherits parent's config."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": ""}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "xhigh"})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_override_reasoning_effort_from_config(self, MockAgent, mock_cfg):
        """delegation.reasoning_effort overrides the parent's level."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "low"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "low"})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_override_reasoning_effort_none_disables(self, MockAgent, mock_cfg):
        """delegation.reasoning_effort: 'none' disables thinking for subagents."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "none"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "high"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": False})

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_invalid_reasoning_effort_falls_back_to_parent(self, MockAgent, mock_cfg):
        """Invalid delegation.reasoning_effort falls back to parent's config."""
        mock_cfg.return_value = {"max_iterations": 50, "reasoning_effort": "banana"}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        parent.reasoning_config = {"enabled": True, "effort": "medium"}

        _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent,
            task_count=1,
        )
        call_kwargs = MockAgent.call_args[1]
        self.assertEqual(call_kwargs["reasoning_config"], {"enabled": True, "effort": "medium"})


# =========================================================================
# Dispatch helper, progress events, concurrency
# =========================================================================

class TestDispatchDelegateTask(unittest.TestCase):
    """Tests for the _dispatch_delegate_task helper and full param forwarding."""

    def test_model_acp_args_not_forwarded(self):
        """The live model dispatch path strips hidden ACP transport args."""
        import run_agent

        captured = {}

        def fake_delegate_task(**kwargs):
            captured.update(kwargs)
            return "{}"

        parent = _make_mock_parent(depth=0)
        with patch("tools.delegate_tool.delegate_task", fake_delegate_task):
            run_agent.AIAgent._dispatch_delegate_task(
                parent,
                {
                    "goal": "test",
                    "acp_command": "claude",
                    "acp_args": ["--acp", "--stdio"],
                    "tasks": [
                        {
                            "goal": "nested",
                            "acp_command": "codex",
                            "acp_args": ["--acp"],
                        },
                    ],
                },
            )

        self.assertNotIn("acp_command", captured)
        self.assertNotIn("acp_args", captured)
        self.assertEqual(captured["goal"], "test")
        self.assertNotIn("acp_command", captured["tasks"][0])
        self.assertNotIn("acp_args", captured["tasks"][0])

class TestDelegateEventEnum(unittest.TestCase):
    """Tests for DelegateEvent enum and back-compat aliases."""

    def test_enum_values_are_strings(self):
        for event in DelegateEvent:
            self.assertIsInstance(event.value, str)
            self.assertTrue(event.value.startswith("delegate."))

    def test_legacy_map_covers_all_old_names(self):
        expected_legacy = {"_thinking", "reasoning.available",
                          "tool.started", "tool.completed", "subagent_progress"}
        self.assertEqual(set(_LEGACY_EVENT_MAP.keys()), expected_legacy)

    def test_legacy_map_values_are_delegate_events(self):
        for old_name, event in _LEGACY_EVENT_MAP.items():
            self.assertIsInstance(event, DelegateEvent)

    def test_progress_callback_normalises_tool_started(self):
        """_build_child_progress_callback handles tool.started via enum."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        self.assertIsNotNone(cb)

        cb("tool.started", tool_name="terminal", preview="ls")
        parent._delegate_spinner.print_above.assert_called()

    def test_progress_callback_normalises_thinking(self):
        """Both _thinking and reasoning.available route to TASK_THINKING."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)

        cb("_thinking", tool_name=None, preview="pondering...")
        assert any("💭" in str(c) for c in parent._delegate_spinner.print_above.call_args_list)

        parent._delegate_spinner.print_above.reset_mock()
        cb("reasoning.available", tool_name=None, preview="hmm")
        assert any("💭" in str(c) for c in parent._delegate_spinner.print_above.call_args_list)

    def test_progress_callback_tool_completed_is_noop(self):
        """tool.completed is normalised but produces no display output."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("tool.completed", tool_name="terminal")
        parent._delegate_spinner.print_above.assert_not_called()

    def test_progress_callback_ignores_unknown_events(self):
        """Unknown event types are silently ignored."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        # Should not raise
        cb("some.unknown.event", tool_name="x")
        parent._delegate_spinner.print_above.assert_not_called()

    def test_progress_callback_accepts_enum_value_directly(self):
        """cb(DelegateEvent.TASK_THINKING, ...) must route to the thinking
        branch.  Pre-fix the callback only handled legacy strings via
        _LEGACY_EVENT_MAP.get and silently dropped enum-typed callers."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb(DelegateEvent.TASK_THINKING, preview="pondering")
        # If the enum was accepted, the thinking emoji got printed.
        assert any(
            "💭" in str(c)
            for c in parent._delegate_spinner.print_above.call_args_list
        )

    def test_progress_callback_accepts_new_style_string(self):
        """cb('delegate.task_thinking', ...) — the string form of the
        enum value — must route to the thinking branch too, so new-style
        emitters don't have to import DelegateEvent."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("delegate.task_thinking", preview="hmm")
        assert any(
            "💭" in str(c)
            for c in parent._delegate_spinner.print_above.call_args_list
        )

    def test_progress_callback_task_progress_not_misrendered(self):
        """'subagent_progress' (legacy name for TASK_PROGRESS) carries a
        pre-batched summary in the tool_name slot.  Before the fix, this
        fell through to the TASK_TOOL_STARTED rendering path, treating
        the summary string as a tool name.  After the fix: distinct
        render (no tool-start emoji lookup) and pass-through relay
        upward (no re-batching).

        Regression path only reachable once nested orchestration is
        enabled: nested orchestrators relay subagent_progress from
        grandchildren upward through this callback.
        """
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("subagent_progress", tool_name="🔀 [1] terminal, file")

        # Spinner gets a distinct 🔀-prefixed line, NOT a tool emoji
        # followed by the summary string as if it were a tool name.
        calls = parent._delegate_spinner.print_above.call_args_list
        self.assertTrue(any("🔀 🔀 [1] terminal, file" in str(c) for c in calls))
        # Parent callback receives the relay (pass-through, no re-batching).
        parent.tool_progress_callback.assert_called_once()
        # No '⚡' tool-start emoji should appear — that's the pre-fix bug.
        self.assertFalse(any("⚡" in str(c) for c in calls))


class TestConcurrencyDefaults(unittest.TestCase):
    """Tests for the concurrency default and no hard ceiling."""

    def test_load_config_prefers_active_persistent_config_over_cli_defaults(self):
        stale_cli = types.ModuleType("cli")
        stale_cli.CLI_CONFIG = {
            "delegation": {
                "max_iterations": 45,
                "model": "",
                "provider": "",
                "base_url": "",
                "api_key": "",
            }
        }
        active_config = {
            "delegation": {
                "max_iterations": 50,
                "max_concurrent_children": 50,
                "max_spawn_depth": 10,
            }
        }

        with patch.dict("sys.modules", {"cli": stale_cli}):
            with patch(
                "hermes_cli.config.load_config_readonly", return_value=active_config
            ):
                self.assertEqual(_load_config()["max_concurrent_children"], 50)
                self.assertEqual(_get_max_concurrent_children(), 50)

    def test_load_config_falls_back_to_cli_config_when_persistent_load_fails(self):
        fallback_cli = types.ModuleType("cli")
        fallback_cli.CLI_CONFIG = {
            "delegation": {
                "max_iterations": 45,
                "max_concurrent_children": 8,
            }
        }

        with patch.dict("sys.modules", {"cli": fallback_cli}):
            with patch(
                "hermes_cli.config.load_config_readonly",
                side_effect=RuntimeError("boom"),
            ):
                self.assertEqual(_load_config()["max_concurrent_children"], 8)

    def test_load_config_prefers_cli_config_when_user_config_ignored(self):
        # `hermes chat --ignore-user-config` sets HERMES_IGNORE_USER_CONFIG=1,
        # which only load_cli_config() honors. The delegation loader must keep
        # CLI_CONFIG authoritative under the flag so user config.yaml
        # delegation keys stay suppressed.
        ignoring_cli = types.ModuleType("cli")
        ignoring_cli.CLI_CONFIG = {
            "delegation": {
                "max_iterations": 45,
                "max_concurrent_children": 4,
            }
        }
        user_config = {"delegation": {"max_concurrent_children": 50}}

        with patch.dict("sys.modules", {"cli": ignoring_cli}):
            with patch.dict(os.environ, {"HERMES_IGNORE_USER_CONFIG": "1"}):
                with patch(
                    "hermes_cli.config.load_config_readonly",
                    return_value=user_config,
                ) as mock_loader:
                    self.assertEqual(_load_config()["max_concurrent_children"], 4)
                    mock_loader.assert_not_called()

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_default_is_three(self, mock_cfg):
        # Clear env var if set
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_max_concurrent_children(), 3)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 10})
    def test_no_upper_ceiling(self, mock_cfg):
        """Users can raise concurrency as high as they want — no hard cap."""
        self.assertEqual(_get_max_concurrent_children(), 10)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 100})
    def test_very_high_values_honored(self, mock_cfg):
        self.assertEqual(_get_max_concurrent_children(), 100)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 0})
    def test_zero_clamped_to_one(self, mock_cfg):
        """Floor of 1 is enforced; zero or negative values raise to 1."""
        self.assertEqual(_get_max_concurrent_children(), 1)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_env_var_honored_uncapped(self, mock_cfg):
        with patch.dict(os.environ, {"DELEGATION_MAX_CONCURRENT_CHILDREN": "12"}):
            self.assertEqual(_get_max_concurrent_children(), 12)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 6})
    def test_configured_value_returned(self, mock_cfg):
        self.assertEqual(_get_max_concurrent_children(), 6)


class TestAsyncCapUnified(unittest.TestCase):
    """max_async_children is deprecated: the async cap IS max_concurrent_children."""

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 15})
    def test_async_cap_follows_concurrent_children(self, mock_cfg):
        from tools.delegate_tool import _get_max_async_children
        self.assertEqual(_get_max_async_children(), 15)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 15, "max_async_children": 3})
    def test_stale_max_async_children_ignored(self, mock_cfg):
        """A leftover max_async_children in config must not shrink the cap."""
        from tools.delegate_tool import _get_max_async_children
        self.assertEqual(_get_max_async_children(), 15)

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_default_matches_concurrent_children_default(self, mock_cfg):
        from tools.delegate_tool import _get_max_async_children
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_get_max_async_children(), _get_max_concurrent_children())


# =========================================================================
# max_spawn_depth clamping
# =========================================================================

class TestMaxSpawnDepth(unittest.TestCase):
    """Tests for _get_max_spawn_depth clamping and fallback behavior."""

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_max_spawn_depth_defaults_to_1(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 0})
    def test_max_spawn_depth_clamped_below_one(self, mock_cfg):
        import logging
        from tools.delegate_tool import _get_max_spawn_depth
        with self.assertLogs("tools.delegate_tool", level=logging.WARNING) as cm:
            result = _get_max_spawn_depth()
        self.assertEqual(result, 1)
        self.assertTrue(any("below floor 1" in m for m in cm.output))

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 99})
    def test_max_spawn_depth_no_upper_ceiling(self, mock_cfg):
        """No upper ceiling — high values pass through unchanged (cost is the limiter)."""
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 99)

    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": "not-a-number"})
    def test_max_spawn_depth_invalid_falls_back_to_default(self, mock_cfg):
        from tools.delegate_tool import _get_max_spawn_depth
        self.assertEqual(_get_max_spawn_depth(), 1)


# =========================================================================
# role param plumbing
# =========================================================================
#
# These tests cover the schema + signature + stash plumbing of the role
# param.  The full role-honoring behavior (toolset re-add, role-aware
# prompt) lives in TestOrchestratorRoleBehavior below; these tests only
# assert on _delegate_role stashing and on the schema shape.


class TestOrchestratorRoleSchema(unittest.TestCase):
    """Tests that the role param reaches the child via dispatch."""

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def _run_with_mock_child(self, role_arg, mock_cfg, mock_creds):
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.run_conversation.return_value = {
                "final_response": "done", "completed": True,
                "api_calls": 1, "messages": [],
            }
            mock_child._delegate_saved_tool_names = []
            mock_child._credential_pool = None
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.model = "test"
            MockAgent.return_value = mock_child
            kwargs = {"goal": "test", "parent_agent": parent}
            if role_arg is not _SENTINEL:
                kwargs["role"] = role_arg
            delegate_task(**kwargs)
            return mock_child

    def test_default_role_is_leaf(self):
        child = self._run_with_mock_child(_SENTINEL)
        self.assertEqual(child._delegate_role, "leaf")

    def test_explicit_orchestrator_role_stashed(self):
        """role='orchestrator' reaches _build_child_agent and is stashed.
        Full behavior (toolset re-add) lands in commit 3; commit 2 only
        verifies the plumbing."""
        child = self._run_with_mock_child("orchestrator")
        self.assertEqual(child._delegate_role, "orchestrator")

    def test_unknown_role_coerces_to_leaf(self):
        """role='nonsense' → _normalize_role warns and returns 'leaf'."""
        import logging
        with self.assertLogs("tools.delegate_tool", level=logging.WARNING) as cm:
            child = self._run_with_mock_child("nonsense")
        self.assertEqual(child._delegate_role, "leaf")
        self.assertTrue(any("coercing" in m.lower() for m in cm.output))

    def test_schema_has_role_top_level_and_per_task(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("role", props)
        self.assertEqual(props["role"]["enum"], ["leaf", "orchestrator"])
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("role", task_props)
        self.assertEqual(task_props["role"]["enum"], ["leaf", "orchestrator"])

    def test_schema_omits_acp_transport_fields(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]

        task_props = props["tasks"]["items"]["properties"]
        self.assertNotIn("acp_command", props)
        self.assertNotIn("acp_args", props)
        self.assertNotIn("acp_command", task_props)
        self.assertNotIn("acp_args", task_props)


# Sentinel used to distinguish "role kwarg omitted" from "role=None".
_SENTINEL = object()


# =========================================================================
# role-honoring behavior
# =========================================================================


def _make_role_mock_child():
    """Helper: mock child with minimal fields for delegate_task to process."""
    mock_child = MagicMock()
    mock_child.run_conversation.return_value = {
        "final_response": "done", "completed": True,
        "api_calls": 1, "messages": [],
    }
    mock_child._delegate_saved_tool_names = []
    mock_child._credential_pool = None
    mock_child.session_prompt_tokens = 0
    mock_child.session_completion_tokens = 0
    mock_child.model = "test"
    return mock_child


class TestOrchestratorRoleBehavior(unittest.TestCase):
    """Tests that role='orchestrator' actually changes toolset + prompt."""

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_orchestrator_role_keeps_delegation_at_depth_1(
        self, mock_cfg, mock_creds
    ):
        """role='orchestrator' + depth-0 parent with max_spawn_depth=2 →
        child at depth 1 gets 'delegation' in enabled_toolsets (can
        further delegate).  Requires max_spawn_depth>=2 since the new
        default is 1 (flat)."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "orchestrator")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_orchestrator_blocked_at_max_spawn_depth(
        self, mock_cfg, mock_creds
    ):
        """Parent at depth 1 with max_spawn_depth=2 spawns child
        at depth 2 (the floor); role='orchestrator' degrades to leaf."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=1)
        parent.enabled_toolsets = ["terminal", "delegation"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertNotIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "leaf")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config", return_value={})
    def test_orchestrator_blocked_at_default_flat_depth(
        self, mock_cfg, mock_creds
    ):
        """With default max_spawn_depth=1 (flat), role='orchestrator'
        on a depth-0 parent produces a depth-1 child that is already at
        the floor — the role degrades to 'leaf' and the delegation
        toolset is stripped.  This is the new default posture."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator", parent_agent=parent)
            kwargs = MockAgent.call_args[1]
            self.assertNotIn("delegation", kwargs["enabled_toolsets"])
            self.assertEqual(mock_child._delegate_role, "leaf")

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    def test_orchestrator_enabled_false_forces_leaf(self, mock_creds):
        """Kill switch delegation.orchestrator_enabled=false overrides
        role='orchestrator'."""
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "delegation"]
        with patch("tools.delegate_tool._load_config",
                   return_value={"orchestrator_enabled": False}):
            with patch("run_agent.AIAgent") as MockAgent:
                mock_child = _make_role_mock_child()
                MockAgent.return_value = mock_child
                delegate_task(goal="test", role="orchestrator",
                              parent_agent=parent)
                kwargs = MockAgent.call_args[1]
                self.assertNotIn("delegation", kwargs["enabled_toolsets"])
                self.assertEqual(mock_child._delegate_role, "leaf")

    # ── Role-aware system prompt ────────────────────────────────────────

    def test_leaf_prompt_does_not_mention_delegation(self):
        prompt = _build_child_system_prompt(
            "Fix tests", role="leaf",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertNotIn("delegate_task", prompt)
        self.assertNotIn("Orchestrator Role", prompt)

    def test_orchestrator_prompt_mentions_delegation_capability(self):
        prompt = _build_child_system_prompt(
            "Survey approaches", role="orchestrator",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertIn("delegate_task", prompt)
        self.assertIn("Orchestrator Role", prompt)
        # Depth/max-depth note present and literal:
        self.assertIn("depth 1", prompt)
        self.assertIn("max_spawn_depth=2", prompt)

    def test_orchestrator_prompt_at_depth_floor_says_children_are_leaves(self):
        """With max_spawn_depth=2 and child_depth=1, the orchestrator's
        own children would be at depth 2 (the floor) → must be leaves."""
        prompt = _build_child_system_prompt(
            "Survey", role="orchestrator",
            max_spawn_depth=2, child_depth=1,
        )
        self.assertIn("MUST be leaves", prompt)

    def test_orchestrator_prompt_below_floor_allows_more_nesting(self):
        """With max_spawn_depth=3 and child_depth=1, the orchestrator's
        own children can themselves be orchestrators (depth 2 < 3)."""
        prompt = _build_child_system_prompt(
            "Deep work", role="orchestrator",
            max_spawn_depth=3, child_depth=1,
        )
        self.assertIn("can themselves be orchestrators", prompt)

    # ── Batch mode and intersection ─────────────────────────────────────

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_batch_mode_per_task_role_override(self, mock_cfg, mock_creds):
        """Per-task role beats top-level; no top-level role → "leaf".

        tasks=[{role:'orchestrator'},{role:'leaf'},{}] → first gets
        delegation, second and third don't.  Requires max_spawn_depth>=2
        (raised explicitly here) since the new default is 1 (flat).
        """
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]
        built_toolsets = []

        def _factory(*a, **kw):
            m = _make_role_mock_child()
            built_toolsets.append(kw.get("enabled_toolsets"))
            return m

        with patch("run_agent.AIAgent", side_effect=_factory):
            delegate_task(
                tasks=[
                    {"goal": "A", "role": "orchestrator"},
                    {"goal": "B", "role": "leaf"},
                    {"goal": "C"},  # no role → falls back to top_role (leaf)
                ],
                parent_agent=parent,
            )
        self.assertIn("delegation", built_toolsets[0])
        self.assertNotIn("delegation", built_toolsets[1])
        self.assertNotIn("delegation", built_toolsets[2])

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_intersection_preserves_delegation_bound(
        self, mock_cfg, mock_creds
    ):
        """Design decision: orchestrator capability is granted by role,
        NOT inherited from the parent's toolset. A parent without
        'delegation' in its enabled_toolsets can still spawn an
        orchestrator child — the re-add in _build_child_agent runs
        unconditionally for orchestrators (when max_spawn_depth allows).

        If you want to change to "parent must have delegation too",
        update _build_child_agent to check parent_toolsets before the
        re-add and update this test to match.
        """
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file"]  # no delegation
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = _make_role_mock_child()
            MockAgent.return_value = mock_child
            delegate_task(goal="test", role="orchestrator",
                          parent_agent=parent)
            self.assertIn("delegation", MockAgent.call_args[1]["enabled_toolsets"])


class TestOrchestratorEndToEnd(unittest.TestCase):
    """End-to-end: parent -> orchestrator -> two-leaf nested orchestration.

    Covers the acceptance gate: parent delegates to an orchestrator
    child; the orchestrator delegates to two leaf grandchildren; the
    role/toolset/depth chain all resolve correctly.

    Mock strategy: a single AIAgent patch with a side_effect factory
    that keys on the child's ephemeral_system_prompt — orchestrator
    prompts contain the string "Orchestrator Role" (see
    _build_child_system_prompt), leaves don't.  The orchestrator
    mock's run_conversation recursively calls delegate_task with
    tasks=[{goal:...},{goal:...}] to spawn two leaves.  This keeps
    the test in one patch context and avoids depth-indexed nesting.
    """

    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._load_config",
           return_value={"max_spawn_depth": 2})
    def test_end_to_end_nested_orchestration(self, mock_cfg, mock_creds):
        mock_creds.return_value = {
            "provider": None, "base_url": None,
            "api_key": None, "api_mode": None, "model": None,
        }
        parent = _make_mock_parent(depth=0)
        parent.enabled_toolsets = ["terminal", "file", "delegation"]

        # (enabled_toolsets, _delegate_role) for each agent built
        built_agents: list = []
        # Keep the orchestrator mock around so the re-entrant delegate_task
        # can reach it via closure.
        orch_mock = {}

        def _factory(*a, **kw):
            prompt = kw.get("ephemeral_system_prompt", "") or ""
            is_orchestrator = "Orchestrator Role" in prompt
            m = _make_role_mock_child()
            built_agents.append({
                "enabled_toolsets": list(kw.get("enabled_toolsets") or []),
                "is_orchestrator_prompt": is_orchestrator,
            })

            if is_orchestrator:
                # Prepare the orchestrator mock as a parent-capable object
                # so the nested delegate_task call succeeds.
                m._delegate_depth = 1
                m._delegate_role = "orchestrator"
                m._active_children = []
                m._active_children_lock = threading.Lock()
                m._session_db = None
                m.platform = "cli"
                m.enabled_toolsets = ["terminal", "file", "delegation"]
                m.api_key = "***"
                m.base_url = ""
                m.provider = None
                m.api_mode = None
                m.providers_allowed = None
                m.providers_ignored = None
                m.providers_order = None
                m.provider_sort = None
                m._print_fn = None
                m.tool_progress_callback = None
                m.thinking_callback = None
                orch_mock["agent"] = m

                def _orchestrator_run(user_message=None, task_id=None, stream_callback=None):
                    # Re-entrant: orchestrator spawns two leaves
                    delegate_task(
                        tasks=[{"goal": "leaf-A"}, {"goal": "leaf-B"}],
                        parent_agent=m,
                    )
                    return {
                        "final_response": "orchestrated 2 workers",
                        "completed": True, "api_calls": 1,
                        "messages": [],
                    }
                m.run_conversation.side_effect = _orchestrator_run

            return m

        with patch("run_agent.AIAgent", side_effect=_factory) as MockAgent:
            delegate_task(
                goal="top-level orchestration",
                role="orchestrator",
                parent_agent=parent,
            )

        # 1 orchestrator + 2 leaf grandchildren = 3 agents
        self.assertEqual(MockAgent.call_count, 3)
        # First built = the orchestrator (parent's direct child)
        self.assertIn("delegation", built_agents[0]["enabled_toolsets"])
        self.assertTrue(built_agents[0]["is_orchestrator_prompt"])
        # Next two = leaves (grandchildren)
        self.assertNotIn("delegation", built_agents[1]["enabled_toolsets"])
        self.assertFalse(built_agents[1]["is_orchestrator_prompt"])
        self.assertNotIn("delegation", built_agents[2]["enabled_toolsets"])
        self.assertFalse(built_agents[2]["is_orchestrator_prompt"])


class TestSubagentApprovalCallback(unittest.TestCase):
    """Subagent worker threads must have a non-interactive approval callback
    installed so dangerous-command prompts don't fall back to input() and
    deadlock the parent's prompt_toolkit TUI.

    Governed by delegation.subagent_auto_approve:
      false (default) → _subagent_auto_deny
      true            → _subagent_auto_approve
    """

    def test_auto_deny_returns_deny(self):
        from tools.delegate_tool import _subagent_auto_deny
        self.assertEqual(
            _subagent_auto_deny("rm -rf /tmp/x", "dangerous"),
            "deny",
        )

    def test_auto_approve_returns_once(self):
        from tools.delegate_tool import _subagent_auto_approve
        self.assertEqual(
            _subagent_auto_approve("rm -rf /tmp/x", "dangerous"),
            "once",
        )

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_getter_defaults_to_deny(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_deny,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_deny)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": False},
    )
    def test_getter_explicit_false_is_deny(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_deny,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_deny)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": True},
    )
    def test_getter_true_is_approve(self, _mock_cfg):
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_approve,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_approve)

    @patch(
        "tools.delegate_tool._load_config",
        return_value={"subagent_auto_approve": "yes"},
    )
    def test_getter_truthy_string_is_approve(self, _mock_cfg):
        """is_truthy_value accepts 'yes'/'1'/'true' as truthy."""
        from tools.delegate_tool import (
            _get_subagent_approval_callback,
            _subagent_auto_approve,
        )
        self.assertIs(_get_subagent_approval_callback(), _subagent_auto_approve)

    def test_executor_initializer_installs_callback_in_worker(self):
        """The initializer sets the callback on the worker thread's TLS,
        not the parent's — verifies the fix actually scopes to workers.
        """
        from concurrent.futures import ThreadPoolExecutor
        from tools.terminal_tool import (
            set_approval_callback as _set_cb,
            _get_approval_callback,
        )
        from tools.delegate_tool import _subagent_auto_deny

        # Parent thread has no callback.
        _set_cb(None)
        self.assertIsNone(_get_approval_callback())

        seen = []

        def worker():
            seen.append(_get_approval_callback())

        with ThreadPoolExecutor(
            max_workers=1,
            initializer=_set_cb,
            initargs=(_subagent_auto_deny,),
        ) as executor:
            executor.submit(worker).result()

        self.assertEqual(seen, [_subagent_auto_deny])
        # Parent's callback slot is still empty (TLS isolates threads).
        self.assertIsNone(_get_approval_callback())


class TestFallbackModelInheritance(unittest.TestCase):
    """Subagents must inherit the parent's fallback provider chain."""

    def test_child_inherits_fallback_chain(self):
        """_build_child_agent passes parent._fallback_chain as fallback_model."""
        parent = _make_mock_parent(depth=0)
        fallback_entry = {"provider": "openrouter", "model": "gpt-4o-mini", "api_key": "sk-or-x"}
        parent._fallback_chain = [fallback_entry]

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="test fallback inheritance",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        _, kwargs = MockAgent.call_args
        self.assertEqual(kwargs["fallback_model"], [fallback_entry])

    def test_child_gets_no_fallback_when_parent_chain_empty(self):
        """When parent._fallback_chain is empty, fallback_model is None."""
        parent = _make_mock_parent(depth=0)
        parent._fallback_chain = []

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="test no fallback",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

        _, kwargs = MockAgent.call_args
        self.assertIsNone(kwargs["fallback_model"])


# ---------------------------------------------------------------------------
# Active-delegation fingerprint dedup
# ---------------------------------------------------------------------------

import tools.delegate_tool as _delegate_mod

_DEDUP_CREDS = {
    "provider": None,
    "base_url": None,
    "api_key": None,
    "api_mode": None,
    "model": None,
}


def _dedup_parent(
    session_id="sess-dedup",
    turn_id="turn-dedup",
    workspace="/tmp/hermes-dedup-ws",
):
    """Parent whose fingerprint inputs are real strings, not MagicMock stubs."""
    parent = _make_mock_parent(depth=0)
    parent.session_id = session_id
    parent._current_turn_id = turn_id
    parent.terminal_cwd = workspace
    parent._memory_manager = None
    return parent


class TestActiveDelegationDedup(unittest.TestCase):
    """A byte-identical delegation must not run twice concurrently.

    The incident: the same expensive review was delegated again while an
    equivalent child was still in flight, doubling model/API queue contention.
    Only a *truly* identical concurrent delegation is suppressed — sequential
    reruns and intentionally different comparison/QA work still spawn.
    """

    def tearDown(self):
        """No test may leave a fingerprint claimed.

        A stranded reservation would suppress that exact delegation for the
        rest of the process, so every test here doubles as a leak detector.
        """
        with _delegate_mod._active_delegation_lock:
            leaked = dict(_delegate_mod._active_delegations)
            _delegate_mod._active_delegations.clear()
        self.assertEqual(leaked, {}, "delegation reservation was never released")

    def _concurrent_delegate(self, first_kwargs, second_kwargs, second_creds=None):
        """Start `first_kwargs`, hold its child inside ``_run_single_child``,
        then issue `second_kwargs` from this thread while the first is live.

        `second_creds` overrides the resolved provider/model for the second
        delegation only — those are not model-facing kwargs, so varying them
        is the only way to exercise their slot in the fingerprint.

        Returns ``(built, run_goals, second_result)``.  Only the first child to
        reach ``_run_single_child`` blocks, so a second delegation that is
        *allowed* to run returns immediately instead of stalling the test.
        """
        main_ident = threading.get_ident()

        def _creds(*args, **kwargs):
            # The second delegation is the one issued from this thread; the
            # first runs on the worker thread below.
            if second_creds is not None and threading.get_ident() == main_ident:
                return dict(second_creds)
            return dict(_DEDUP_CREDS)

        built = []
        run_goals = []
        state_lock = threading.Lock()
        first_started = threading.Event()
        release_first = threading.Event()

        def _build(*args, **kwargs):
            child = MagicMock()
            with state_lock:
                child._subagent_id = f"sa-{len(built)}-dedup"
                built.append(kwargs)
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            with state_lock:
                run_goals.append(goal)
                is_first = len(run_goals) == 1
            first_started.set()
            if is_first:
                release_first.wait(timeout=5)
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        holder = {}

        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            side_effect=_creds,
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ):

            def _first():
                holder["first"] = delegate_task(**first_kwargs)

            thread = threading.Thread(target=_first, daemon=True)
            thread.start()
            try:
                self.assertTrue(
                    first_started.wait(timeout=5), "first child never started"
                )
                holder["second"] = delegate_task(**second_kwargs)
            finally:
                release_first.set()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "first delegation never finished")
        return built, run_goals, json.loads(holder["second"])

    def test_concurrent_identical_delegation_suppressed(self):
        parent = _dedup_parent()
        call = {
            "goal": "Review the payment module for concurrency bugs",
            "context": "focus on the refund path",
            "parent_agent": parent,
        }

        built, run_goals, second = self._concurrent_delegate(dict(call), dict(call))

        self.assertEqual(len(built), 1, "duplicate delegation built a second child")
        self.assertEqual(len(run_goals), 1, "duplicate delegation ran a second child")

        entry = second["results"][0]
        self.assertEqual(entry["status"], "duplicate")
        self.assertEqual(entry["api_calls"], 0)
        self.assertTrue(entry.get("fingerprint"), "duplicate entry lacks fingerprint")
        self.assertTrue(
            entry.get("existing_subagent_id"),
            "duplicate entry lacks the in-flight owner id",
        )

    def _sequential_delegate(self, calls, build_side_effect=None):
        """Run each kwargs dict in `calls` to completion, in order.

        Nothing blocks, so every reservation is released before the next call —
        this is the control for "only *concurrent* duplicates are suppressed".
        Returns ``(built, run_goals, [parsed results])``.
        """
        built = []
        run_goals = []

        def _build(*args, **kwargs):
            if build_side_effect is not None:
                build_side_effect(len(built))
            child = MagicMock()
            child._subagent_id = f"sa-{len(built)}-dedup"
            built.append(kwargs)
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            run_goals.append(goal)
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        results = []
        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=dict(_DEDUP_CREDS),
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ):
            for call in calls:
                results.append(json.loads(delegate_task(**call)))

        return built, run_goals, results

    def test_sequential_rerun_after_completion_is_not_suppressed(self):
        """Once the first child finishes, the identical task must run again."""
        parent = _dedup_parent()
        call = {
            "goal": "Review the payment module for concurrency bugs",
            "context": "focus on the refund path",
            "parent_agent": parent,
        }

        built, run_goals, results = self._sequential_delegate(
            [dict(call), dict(call)]
        )

        self.assertEqual(len(built), 2, "sequential rerun was wrongly suppressed")
        self.assertEqual(len(run_goals), 2, "sequential rerun never ran")
        for i, result in enumerate(results):
            self.assertEqual(
                result["results"][0]["status"],
                "completed",
                f"run {i} did not complete",
            )

    def test_child_build_exception_releases_reservation(self):
        """A crash between reserve and run must not strand the fingerprint."""
        parent = _dedup_parent()
        call = {
            "goal": "Review the payment module for concurrency bugs",
            "context": "focus on the refund path",
            "parent_agent": parent,
        }

        def _explode_on_first(build_count):
            if build_count == 0:
                raise RuntimeError("child build blew up")

        with self.assertRaises(RuntimeError):
            self._sequential_delegate(
                [dict(call)], build_side_effect=_explode_on_first
            )

        with _delegate_mod._active_delegation_lock:
            self.assertEqual(
                dict(_delegate_mod._active_delegations),
                {},
                "failed build left the fingerprint claimed forever",
            )

        # ...and the very same task is delegable again.
        built, run_goals, _results = self._sequential_delegate([dict(call)])
        self.assertEqual(len(built), 1)
        self.assertEqual(len(run_goals), 1)

    def test_release_requires_owner_match(self):
        """One caller must never drop another caller's reservation."""
        reserve = _delegate_mod._reserve_active_delegation
        release = _delegate_mod._release_active_delegation
        fingerprint = "f" * 64

        self.assertIsNone(reserve(fingerprint, owner_id="owner-a", task_index=0))

        # A stale/foreign owner id is refused, and the claim survives it.
        self.assertFalse(release(fingerprint, "owner-b"))
        self.assertIsNotNone(
            reserve(fingerprint, owner_id="owner-c", task_index=0),
            "foreign release freed owner-a's reservation",
        )

        # The real owner releases it exactly once.
        self.assertTrue(release(fingerprint, "owner-a"))
        self.assertFalse(release(fingerprint, "owner-a"), "double release accepted")

        # Now free for the next caller.
        self.assertIsNone(reserve(fingerprint, owner_id="owner-d", task_index=0))
        self.assertTrue(release(fingerprint, "owner-d"))

    def test_exact_duplicate_within_one_batch_runs_as_explicit_n_sample(self):
        """Explicit batch entries are distinct requests, even with equal text.

        Dedup suppresses an equivalent request from another concurrent
        ``delegate_task`` call.  It must not silently reduce a caller-requested
        N-sample batch before any child has started.
        """
        parent = _dedup_parent()
        task = {
            "goal": "Audit the retry policy",
            "context": "focus on exponential backoff",
        }

        built, run_goals, results = self._sequential_delegate(
            [{"tasks": [dict(task), dict(task)], "parent_agent": parent}]
        )

        self.assertEqual(len(built), 2, "explicit N-sample batch was collapsed")
        self.assertEqual(len(run_goals), 2, "one explicit batch entry never ran")

        entries = results[0]["results"]
        self.assertEqual(len(entries), 2, "a batch result entry went missing")
        self.assertEqual(
            [e["task_index"] for e in entries],
            [0, 1],
            "batch result ordering was not preserved",
        )
        self.assertEqual([e["status"] for e in entries], ["completed", "completed"])

    def test_finished_child_stops_suppressing_while_sibling_still_runs(self):
        """A reservation belongs to one child, not to the batch around it.

        Two children run in one batch: A completes, B stays blocked.  While B
        is genuinely still in flight, an identical delegation to A must RUN
        (its request is over, so nothing is being duplicated) while an
        identical delegation to B must still be SUPPRESSED.  Releasing only
        when the whole batch drained would falsely suppress A here.

        Fully event-driven — no sleeps.  ``_release_active_delegation`` is
        spied on so the probes are issued only once A's claim has actually been
        dropped; B's blocking run guarantees the batch has not finished.

        This covers the background path too: ``background=true`` hands this
        exact ``_execute_and_aggregate`` closure to the async scheduler as its
        runner (see ``_batch_runner``), so the per-child release lives on the
        one code path both modes share.
        """
        parent = _dedup_parent()
        goal_a = "Audit the retry policy"
        goal_b = "Audit the refund ledger"
        shared_context = "focus on exponential backoff"

        built = []
        run_goals = []
        state_lock = threading.Lock()
        b_started = threading.Event()
        release_b = threading.Event()
        a_claim_dropped = threading.Event()

        real_release = _delegate_mod._release_active_delegation

        def _spy_release(fingerprint, owner_id):
            dropped = real_release(fingerprint, owner_id)
            # B is blocked and every probe below is issued after this fires, so
            # the first successful release in this test is A's and only A's.
            if dropped:
                a_claim_dropped.set()
            return dropped

        def _build(*args, **kwargs):
            child = MagicMock()
            with state_lock:
                child._subagent_id = f"sa-{len(built)}-dedup"
                built.append(kwargs)
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            with state_lock:
                run_goals.append(goal)
            if goal == goal_b:
                b_started.set()
                release_b.wait(timeout=5)
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        holder = {}

        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._get_max_concurrent_children", return_value=2
        ), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=dict(_DEDUP_CREDS),
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ), patch(
            "tools.delegate_tool._release_active_delegation",
            side_effect=_spy_release,
        ):

            def _batch():
                holder["batch"] = delegate_task(
                    tasks=[
                        {"goal": goal_a, "context": shared_context},
                        {"goal": goal_b, "context": shared_context},
                    ],
                    parent_agent=parent,
                )

            thread = threading.Thread(target=_batch, daemon=True)
            thread.start()
            try:
                self.assertTrue(b_started.wait(timeout=5), "child B never started")
                self.assertTrue(
                    a_claim_dropped.wait(timeout=5),
                    "child A finished but its reservation was never released",
                )
                # B is provably mid-run (it is parked on release_b), so the
                # batch cannot have reached its batch-wide sweep yet.
                self.assertFalse(release_b.is_set())
                holder["probe_a"] = delegate_task(
                    goal=goal_a, context=shared_context, parent_agent=parent
                )
                holder["probe_b"] = delegate_task(
                    goal=goal_b, context=shared_context, parent_agent=parent
                )
            finally:
                release_b.set()
                thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "batch never finished")

        probe_a = json.loads(holder["probe_a"])["results"][0]
        probe_b = json.loads(holder["probe_b"])["results"][0]

        self.assertEqual(
            probe_a["status"],
            "completed",
            "a finished child kept suppressing its own task while a sibling ran",
        )
        self.assertEqual(
            probe_b["status"],
            "duplicate",
            "the still-running child stopped suppressing its duplicate",
        )
        self.assertEqual(probe_b["api_calls"], 0)
        self.assertTrue(probe_b.get("existing_subagent_id"))

        # A ran twice (batch + probe), B exactly once — the probe never
        # double-ran the child that was actually in flight.
        self.assertEqual(run_goals.count(goal_a), 2)
        self.assertEqual(run_goals.count(goal_b), 1)
        self.assertEqual(len(built), 3, "probe B built a child it should not have")

        batch = json.loads(holder["batch"])["results"]
        self.assertEqual([e["status"] for e in batch], ["completed", "completed"])

    def test_distinct_delegations_are_never_suppressed(self):
        """Each identity component alone must keep two delegations independent."""
        base_parent = _dedup_parent()
        base = {
            "goal": "Review the payment module for concurrency bugs",
            "context": "focus on the refund path",
            "parent_agent": base_parent,
        }

        # (label, second-call kwargs, second-call resolved creds)
        variants = [
            ("goal", {**base, "goal": "Review the payout module instead"}, None),
            ("context", {**base, "context": "focus on the capture path"}, None),
            ("role", {**base, "role": "orchestrator"}, None),
            ("model", dict(base), {**_DEDUP_CREDS, "model": "some-other-model"}),
            ("provider", dict(base), {**_DEDUP_CREDS, "provider": "other-provider"}),
            (
                "base_url",
                dict(base),
                {**_DEDUP_CREDS, "base_url": "https://other-endpoint.invalid/v1"},
            ),
            (
                "api_key",
                dict(base),
                {**_DEDUP_CREDS, "api_key": "synthetic-second-credential"},
            ),
            (
                "turn_id",
                {**base, "parent_agent": _dedup_parent(turn_id="turn-OTHER")},
                None,
            ),
            (
                "session_id",
                {**base, "parent_agent": _dedup_parent(session_id="sess-OTHER")},
                None,
            ),
            (
                "workspace",
                {
                    **base,
                    "parent_agent": _dedup_parent(workspace="/tmp/hermes-other-ws"),
                },
                None,
            ),
        ]

        for label, variant, creds in variants:
            with self.subTest(differs_by=label):
                built, run_goals, second = self._concurrent_delegate(
                    dict(base), dict(variant), second_creds=creds
                )
                self.assertEqual(
                    len(built), 2, f"differing {label} was wrongly deduped"
                )
                self.assertEqual(
                    len(run_goals), 2, f"differing {label} never ran its child"
                )
                self.assertEqual(second["results"][0]["status"], "completed")

    def _background_delegate(
        self, dispatch_result, run_exc=None, after=None, **call_kwargs
    ):
        """Dispatch a background batch with the async scheduler stubbed out.

        `dispatch_result` is what the fake ``dispatch_async_delegation_batch``
        returns, or an exception instance to raise instead.  The real runner /
        interrupt closures are captured and handed to ``after(captured,
        run_goals)``, which runs while the patches are still active so the test
        can drive the batch's terminal paths itself — no daemon threads, no
        timing.

        Returns ``(built, run_goals, captured, result)`` where `captured` holds
        the ``runner`` and ``interrupt_fn`` handed to the scheduler.
        """
        built = []
        run_goals = []
        captured = {}

        def _build(*args, **kwargs):
            child = MagicMock()
            child._subagent_id = f"sa-{len(built)}-dedup"
            built.append(kwargs)
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            run_goals.append(goal)
            if run_exc is not None:
                raise run_exc
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        def _dispatch(**kwargs):
            captured.update(kwargs)
            if isinstance(dispatch_result, BaseException):
                raise dispatch_result
            return dict(dispatch_result)

        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=dict(_DEDUP_CREDS),
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ), patch(
            "tools.async_delegation.dispatch_async_delegation_batch",
            side_effect=_dispatch,
        ), patch(
            "gateway.session_context.async_delivery_supported", return_value=True
        ):
            raw = delegate_task(background=True, **call_kwargs)
            if after is not None:
                after(captured, run_goals)

        return built, run_goals, captured, json.loads(raw)

    def _held_fingerprints(self):
        with _delegate_mod._active_delegation_lock:
            return dict(_delegate_mod._active_delegations)

    def test_background_reservation_is_held_until_the_runner_finishes(self):
        """Hand-off keeps the claim; the runner's completion drops it."""
        parent = _dedup_parent()
        goal = "Review the payment module for concurrency bugs"
        seen = {}

        def _after(captured, run_goals):
            # Dispatch has returned but the daemon executor has not started the
            # batch yet: the claim must still stand, so a second identical
            # delegation issued right now is still suppressed.
            seen["queued_goals"] = list(run_goals)
            seen["queued_holds"] = self._held_fingerprints()
            seen["batch"] = captured["runner"]()

        built, run_goals, _captured, result = self._background_delegate(
            {"status": "dispatched", "delegation_id": "deleg-1"},
            after=_after,
            goal=goal,
            context="focus on the refund path",
            parent_agent=parent,
        )

        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(len(built), 1)
        self.assertEqual(
            seen["queued_goals"], [], "dispatch ran the child instead of queueing it"
        )
        self.assertEqual(
            len(seen["queued_holds"]),
            1,
            "dispatch dropped the reservation while the batch was still queued",
        )

        # The runner has now completed, as the daemon executor would have.
        self.assertEqual(run_goals, [goal])
        self.assertEqual(seen["batch"]["results"][0]["status"], "completed")
        self.assertEqual(
            self._held_fingerprints(),
            {},
            "background runner completion never released the reservation",
        )

    def test_background_runner_exception_releases_reservation(self):
        """Runner error and the post-cancel unwind share this release path."""
        parent = _dedup_parent()

        def _after(captured, run_goals):
            # A cancelled batch interrupts its children and then unwinds
            # through the very same runner frame, so this covers both the
            # error and the cancellation terminal paths.
            captured["interrupt_fn"]()
            with self.assertRaises(RuntimeError):
                captured["runner"]()

        _built, _run_goals, _captured, result = self._background_delegate(
            {"status": "dispatched", "delegation_id": "deleg-2"},
            run_exc=RuntimeError("batch runner blew up"),
            after=_after,
            goal="Review the payment module for concurrency bugs",
            parent_agent=parent,
        )

        self.assertEqual(result["status"], "dispatched")
        self.assertEqual(
            self._held_fingerprints(),
            {},
            "a failed background runner stranded the reservation",
        )

    def test_background_dispatch_rejection_releases_reservation(self):
        """Pool at capacity runs inline, and inline completion releases."""
        parent = _dedup_parent()
        _built, run_goals, _captured, result = self._background_delegate(
            {"status": "rejected", "error": "async pool at capacity"},
            goal="Review the payment module for concurrency bugs",
            parent_agent=parent,
        )

        self.assertEqual(len(run_goals), 1, "rejected dispatch never ran inline")
        self.assertEqual(result["results"][0]["status"], "completed")
        self.assertIn("capacity", result.get("note", ""))
        self.assertEqual(
            self._held_fingerprints(),
            {},
            "pool-at-capacity fallback stranded the reservation",
        )

    def test_background_dispatch_exception_releases_reservation(self):
        """A scheduler that raises must not leave the fingerprint claimed."""
        parent = _dedup_parent()
        with self.assertRaises(RuntimeError):
            self._background_delegate(
                RuntimeError("scheduler exploded"),
                goal="Review the payment module for concurrency bugs",
                parent_agent=parent,
            )

        self.assertEqual(
            self._held_fingerprints(),
            {},
            "a raising scheduler stranded the reservation",
        )

    def test_background_post_dispatch_failure_keeps_runner_owned_reservation(self):
        """Once accepted, only the background runner may release its claim."""
        parent = _dedup_parent()
        captured = {}

        def _build(*args, **kwargs):
            child = MagicMock()
            child._subagent_id = "sa-owned-by-runner"
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        def _dispatch(**kwargs):
            captured.update(kwargs)
            # Accepted by the scheduler, but deliberately not JSON serialisable:
            # the parent-frame response construction fails after ownership moved.
            return {"status": "dispatched", "delegation_id": object()}

        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=dict(_DEDUP_CREDS),
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ), patch(
            "tools.async_delegation.dispatch_async_delegation_batch",
            side_effect=_dispatch,
        ), patch(
            "gateway.session_context.async_delivery_supported", return_value=True
        ):
            with self.assertRaises(TypeError):
                delegate_task(
                    background=True,
                    goal="Review ownership transfer",
                    parent_agent=parent,
                )

            self.assertEqual(
                len(self._held_fingerprints()),
                1,
                "parent frame released a reservation already owned by runner",
            )
            captured["runner"]()

        self.assertEqual(
            self._held_fingerprints(),
            {},
            "background runner completion did not release its reservation",
        )

    def test_malformed_result_index_sorts_after_valid_entries(self):
        """Internal malformed results cannot jump ahead of caller task order."""
        parent = _dedup_parent()

        def _build(*args, **kwargs):
            child = MagicMock()
            child._subagent_id = f"sa-sort-{kwargs['task_index']}"
            return child

        def _run(task_index, goal, child=None, parent_agent=None, **kwargs):
            return {
                "task_index": "malformed" if task_index == 0 else task_index,
                "status": "completed",
                "summary": "done",
                "api_calls": 1,
                "duration_seconds": 0.1,
            }

        with patch("tools.delegate_tool._load_config", return_value={}), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=dict(_DEDUP_CREDS),
        ), patch(
            "tools.delegate_tool._build_child_agent", side_effect=_build
        ), patch(
            "tools.delegate_tool._run_single_child", side_effect=_run
        ):
            result = json.loads(
                delegate_task(
                    tasks=[{"goal": "bad index"}, {"goal": "valid index"}],
                    parent_agent=parent,
                )
            )

        self.assertEqual(
            [entry["task_index"] for entry in result["results"]],
            [1, "malformed"],
        )

    def test_background_explicit_n_sample_batch_dispatches_every_entry(self):
        """Equal entries in one explicit background batch are separate requests."""
        parent = _dedup_parent()
        task = {"goal": "Audit the retry policy", "context": "backoff only"}
        _built, run_goals, captured, result = self._background_delegate(
            {"status": "dispatched", "delegation_id": "deleg-3"},
            after=lambda captured, run_goals: captured["runner"](),
            tasks=[dict(task), dict(task)],
            parent_agent=parent,
        )

        self.assertEqual(
            captured["goals"],
            ["Audit the retry policy", "Audit the retry policy"],
        )
        self.assertEqual(result["count"], 2)
        self.assertNotIn("duplicates", result)
        self.assertEqual(len(run_goals), 2)
        self.assertEqual(self._held_fingerprints(), {})


# =========================================================================
# Request-size instrumentation on normal (non-timeout) child runs
# =========================================================================

# One numeric key=value pair per field, nothing else. Anchored on both ends so
# a stray path, URL, repr or prompt fragment cannot slip in unnoticed.
_SIZE_LINE_RE = re.compile(r"^subagent_request_size(?: [a-z_]+=\d+)+$")

_SIZE_FIELDS = (
    "task_index",
    "goal_chars",
    "goal_tokens",
    "context_chars",
    "context_tokens",
    "system_chars",
    "system_tokens",
    "tool_schema_bytes",
    "message_count",
    "approx_tokens",
)

# Values that must never reach the log line. Distinctive enough that a
# substring search cannot false-negative.
_SIZE_SENTINELS = (
    "GOALSENTINELalpha",
    "CONTEXTSENTINELbravo",
    "SYSTEMSENTINELcharlie",
    "TOOLSENTINELdelta",
    "sk-CREDSENTINELecho",
    "https://sentinel.invalid/v1",
    "/tmp/sentinel-workspace",
)


def _size_child(
    *,
    system=None,
    tools=None,
    context_chars=None,
    session_messages=None,
    prefill_messages=None,
):
    """Child double carrying real (non-Mock) size-bearing attributes.

    ``_subagent_id`` is left None so ``_run_single_child`` skips the live
    subagent registry — these tests are about the log line, not the TUI.
    """
    child = MagicMock()
    child._subagent_id = None
    child._credential_pool = None
    child.tool_progress_callback = None
    child.ephemeral_system_prompt = system
    child.tools = tools
    child._session_messages = [] if session_messages is None else session_messages
    child.prefill_messages = [] if prefill_messages is None else prefill_messages
    if context_chars is not None:
        child._delegate_context_chars = context_chars
    child.get_activity_summary.return_value = {
        "current_tool": None,
        "api_call_count": 0,
        "max_iterations": 50,
        "last_activity_desc": "",
    }
    child.run_conversation.return_value = {
        "final_response": "ok",
        "completed": True,
        "api_calls": 1,
    }
    return child


class TestSubagentRequestSizeInstrumentation(unittest.TestCase):
    """Every normal child run emits one numeric request-size line.

    Request-size evidence used to exist only in the timeout-only diagnostic
    dump (#14726) and the summary-trim log, so a subagent that was merely
    slow — never timing out — left no record of how large its first request
    was. This line closes that gap, and it is numbers only: no goal,
    context, system-prompt or tool-schema text, no credential, no URL, no
    path, no repr.
    """

    def _run_and_capture(self, child, goal="do the thing", task_index=3):
        """Run a real ``_run_single_child`` and return (result, size_lines)."""
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        parent._touch_activity = lambda desc: None

        with self.assertLogs("tools.delegate_tool", level=logging.DEBUG) as cm:
            result = _run_single_child(
                task_index=task_index,
                goal=goal,
                child=child,
                parent_agent=parent,
            )
        size_lines = [
            r.getMessage()
            for r in cm.records
            if r.getMessage().startswith("subagent_request_size")
        ]
        return result, size_lines

    def _parse(self, line):
        """``subagent_request_size a=1 b=2`` -> {'a': 1, 'b': 2}."""
        self.assertRegex(line, _SIZE_LINE_RE)
        pairs = line.split(" ")[1:]
        return {k: int(v) for k, v in (p.split("=", 1) for p in pairs)}

    def test_normal_run_emits_one_request_size_line(self):
        """A healthy child logs the line exactly once, with every field."""
        child = _size_child(system="s" * 100, tools=[{"name": "t"}], context_chars=20)

        result, size_lines = self._run_and_capture(child)

        self.assertEqual(
            len(size_lines),
            1,
            f"expected exactly one subagent_request_size line, got {size_lines}",
        )
        fields = self._parse(size_lines[0])
        self.assertEqual(
            tuple(fields), _SIZE_FIELDS,
            "field set/order drifted from the documented contract",
        )
        self.assertEqual(fields["task_index"], 3)
        # Instrumentation must not disturb the run itself.
        self.assertEqual(result["status"], "completed")
        child.run_conversation.assert_called_once()

    def test_fields_are_exact_for_known_inputs(self):
        """Pin the numbers, not just the shape.

        goal 40 chars, context 20 chars, system 100 chars,
        json.dumps([{"name": "t"}]) == 15 bytes, no staged messages.
        """
        from tools.delegate_tool import _subagent_request_size_fields

        child = _size_child(system="s" * 100, tools=[{"name": "t"}], context_chars=20)

        fields = _subagent_request_size_fields(
            child=child, task_index=7, goal="g" * 40
        )

        self.assertEqual(
            fields,
            {
                "task_index": 7,
                "goal_chars": 40,
                "goal_tokens": 10,
                "context_chars": 20,
                "context_tokens": 5,
                "system_chars": 100,
                "system_tokens": 25,
                "tool_schema_bytes": 15,
                "message_count": 0,
                # 10 + 5 + 25 + ceil(15/4)=4
                "approx_tokens": 44,
            },
        )

    def test_token_estimate_is_ceiling_of_chars_over_four(self):
        """The documented formula: tokens = ceil(chars / 4), no tokenizer."""
        from tools.delegate_tool import _approx_tokens_from_chars

        for chars, expected in ((0, 0), (1, 1), (3, 1), (4, 1), (5, 2), (400, 100)):
            with self.subTest(chars=chars):
                self.assertEqual(_approx_tokens_from_chars(chars), expected)
        # Garbage in -> 0, never a crash and never a repr.
        for bad in (None, "40", object(), True):
            with self.subTest(bad=bad):
                self.assertEqual(_approx_tokens_from_chars(bad), 0)

    def test_line_carries_numbers_only_no_content_or_secrets(self):
        """No prompt text, tool text, credential, URL or path in the line."""
        child = _size_child(
            system="SYSTEMSENTINELcharlie " * 10,
            tools=[{"name": "TOOLSENTINELdelta", "description": "x"}],
            context_chars=len("CONTEXTSENTINELbravo"),
        )
        child.api_key = "sk-CREDSENTINELecho"
        child.base_url = "https://sentinel.invalid/v1"
        child.workspace_path = "/tmp/sentinel-workspace"

        _result, size_lines = self._run_and_capture(
            child, goal="GOALSENTINELalpha " * 3
        )

        self.assertEqual(len(size_lines), 1)
        line = size_lines[0]
        # Structural proof: the whole line is `key=<int>` pairs and nothing else.
        self.assertRegex(line, _SIZE_LINE_RE)
        for sentinel in _SIZE_SENTINELS:
            self.assertNotIn(sentinel, line)
        for banned in ("http", "sk-", "/", "'", '"', "<", ">", "Mock"):
            self.assertNotIn(banned, line, f"{banned!r} leaked into {line!r}")

    def test_malformed_child_attributes_degrade_to_zero(self):
        """Mock/garbage attrs yield zeros — never a repr, never a crash."""
        child = MagicMock()  # every size attribute is a Mock, not str/list/int
        child._subagent_id = None
        child._credential_pool = None
        child.tool_progress_callback = None
        child.get_activity_summary.return_value = {
            "current_tool": None,
            "api_call_count": 0,
            "max_iterations": 50,
            "last_activity_desc": "",
        }
        child.run_conversation.return_value = {
            "final_response": "ok",
            "completed": True,
            "api_calls": 1,
        }

        result, size_lines = self._run_and_capture(child, goal="hi", task_index=0)

        self.assertEqual(len(size_lines), 1)
        fields = self._parse(size_lines[0])
        self.assertEqual(fields["context_chars"], 0)
        self.assertEqual(fields["system_chars"], 0)
        self.assertEqual(fields["tool_schema_bytes"], 0)
        self.assertEqual(fields["message_count"], 0)
        # The real goal string is still measurable.
        self.assertEqual(fields["goal_chars"], 2)
        self.assertEqual(fields["approx_tokens"], 1)
        self.assertEqual(result["status"], "completed")

    def test_message_count_reflects_staged_messages(self):
        """Staged prefill/session turns are counted; 0 only when truly empty."""
        from tools.delegate_tool import _subagent_request_size_fields

        child = _size_child(
            session_messages=[{"role": "user"}],
            prefill_messages=[{"role": "system"}, {"role": "assistant"}],
        )

        fields = _subagent_request_size_fields(child=child, task_index=0, goal="x")

        self.assertEqual(fields["message_count"], 3)

    def test_child_runs_even_when_metric_calculation_raises(self):
        """Instrumentation is best-effort: a broken metric must not block work."""
        from tools.delegate_tool import _run_single_child

        child = _size_child(system="s" * 8, tools=[{"name": "t"}], context_chars=4)
        parent = _make_mock_parent()
        parent._touch_activity = lambda desc: None

        with patch(
            "tools.delegate_tool._subagent_request_size_fields",
            side_effect=RuntimeError("metric boom"),
        ):
            result = _run_single_child(
                task_index=1, goal="still must run", child=child, parent_agent=parent
            )

        child.run_conversation.assert_called_once()
        self.assertEqual(result["status"], "completed")

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_build_child_agent_stashes_only_numeric_context_size(
        self, MockAgent, mock_cfg
    ):
        """The child carries the context *length*, never the context text."""
        mock_cfg.return_value = {"max_iterations": 50}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()
        context = "CONTEXTSENTINELbravo details"

        child = _build_child_agent(
            task_index=0, goal="test", context=context, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent, task_count=1,
        )

        self.assertIsInstance(child._delegate_context_chars, int)
        self.assertEqual(child._delegate_context_chars, len(context))
        self.assertNotIsInstance(child._delegate_context_chars, str)

    @patch("tools.delegate_tool._load_config")
    @patch("run_agent.AIAgent")
    def test_build_child_agent_records_zero_context_when_absent(
        self, MockAgent, mock_cfg
    ):
        """No context -> 0, so the field is always present and numeric."""
        mock_cfg.return_value = {"max_iterations": 50}
        MockAgent.return_value = MagicMock()
        parent = _make_mock_parent()

        child = _build_child_agent(
            task_index=0, goal="test", context=None, toolsets=None,
            model=None, max_iterations=50, parent_agent=parent, task_count=1,
        )

        self.assertEqual(child._delegate_context_chars, 0)


if __name__ == "__main__":
    unittest.main()
