#!/usr/bin/env python3
"""
Tests for the subagent delegation tool.

Uses mock AIAgent instances to test the delegation logic without
requiring API keys or real LLM calls.

Run with:  python -m pytest tests/test_delegate.py -v
   or:     python tests/test_delegate.py
"""

import copy
import json
import logging
import os
import re
import sys
import threading
import time
import types
import unittest
from contextvars import ContextVar
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_BLOCKED_TOOLS,
    DELEGATE_TASK_SCHEMA,
    DelegateEvent,
    _get_max_concurrent_children,
    _load_config,
    delegate_task,
    _build_child_agent,
    _build_child_progress_callback,
    _build_child_system_prompt,
    _build_dynamic_schema_overrides,
    _strip_blocked_tools,
    _resolve_child_credential_pool,
    _resolve_delegation_credentials,
    _resolve_route_override,
)
from hermes_state import SessionDB


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

    def test_schema_valid(self):
        self.assertEqual(DELEGATE_TASK_SCHEMA["name"], "delegate_task")
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        # tasks[] is the only advertised spawn shape (single task = one-entry
        # array); legacy top-level goal/context/output_schema stay
        # handler-accepted but unadvertised.
        self.assertIn("tasks", props)
        self.assertNotIn("goal", props)
        self.assertNotIn("context", props)
        self.assertNotIn("output_schema", props)
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("goal", task_props)
        self.assertIn("context", task_props)
        self.assertIn("output_schema", task_props)
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

    def test_top_level_description_compact_and_complete(self):
        """The top-level description must stay compact while keeping every
        contract that exists nowhere else in the schema (keyword-level, not
        prose-literal, so rewording doesn't break CI)."""
        from tools.delegate_tool import _build_top_level_description

        desc = _build_top_level_description()
        # Compaction ceiling: the old description was ~4,000 chars.
        self.assertLessEqual(len(desc), 2200)
        # Contracts only the top-level text carries:
        for keyword in (
            "background",          # async semantics
            "wait or poll",        # no-poll rule
            "execute_code",        # mechanical-work routing
            "cronjob",             # durable-work routing
            "/stop",               # non-durability warning
            "context",             # pass-everything-via-context rule
            "respond in Chinese",  # language example (weak models regress without it)
            "SELF-REPORTS",        # verification contract
            "clarify",             # child blocked-tool list
            "delegation.provider", # model inheritance / pinning
        ):
            self.assertIn(keyword, desc, f"top-level description lost: {keyword!r}")
        # send_message must NOT be named: gateway-internal vocabulary most
        # sessions never see (still enforced via DELEGATE_BLOCKED_TOOLS).
        self.assertNotIn("send_message", desc)

    def test_dynamic_limits_moved_to_param_descriptions(self):
        """Concurrency reaches the model through the tasks parameter
        description; the depth ceiling lives in the top-level description's
        depth-derived recursion rule (role param is gone)."""
        from tools.delegate_tool import _build_dynamic_schema_overrides
        from tools.registry import registry

        with (
            patch("tools.delegate_tool._get_max_concurrent_children", return_value=7),
            patch("tools.delegate_tool._get_max_spawn_depth", return_value=4),
            patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True),
        ):
            overrides = _build_dynamic_schema_overrides()
            definition = registry.get_definitions({"delegate_task"})[0]["function"]

        for parameters in (overrides["parameters"], definition["parameters"]):
            self.assertIn("up to 7", parameters["properties"]["tasks"]["description"])
            self.assertNotIn("role", parameters["properties"])
        # Depth ceiling now rides the depth-derived recursion rule in the
        # top-level text (only rendered when nesting is available).
        self.assertIn("max_spawn_depth=4", overrides["description"])
        self.assertNotIn("up to 7", overrides["description"])

class TestChildSystemPrompt(unittest.TestCase):
    def test_goal_only(self):
        prompt = _build_child_system_prompt("Fix the tests")
        self.assertIn("Fix the tests", prompt)
        self.assertIn("YOUR TASK", prompt)
        self.assertNotIn("CONTEXT", prompt)

class TestStripBlockedTools(unittest.TestCase):
    def test_removes_blocked_toolsets(self):
        result = _strip_blocked_tools(["terminal", "file", "delegation", "clarify", "memory", "code_execution"])
        self.assertEqual(sorted(result), ["code_execution", "file", "terminal"])

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

    def test_mixed_composite_is_subtracted_at_child_assembly(self):
        """A mixed platform bundle must not re-expose blocked leaf tools.

        ``hermes-cli`` contains both allowed tools and every sensitive
        delegate tool, so it cannot be dropped wholesale. Child construction
        must instead pass exact one-tool deny toolsets to AIAgent, where
        model_tools applies them after resolving the composite.
        """
        import model_tools

        parent = _make_mock_parent()
        parent.enabled_toolsets = ["hermes-cli"]
        parent.disabled_toolsets = ["browser"]

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="Inspect safely",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="leaf",
            )

        _, kwargs = MockAgent.call_args
        disabled = kwargs["disabled_toolsets"]
        self.assertIn("browser", disabled)
        for toolset_name in (
            "clarify",
            "cronjob",
            "delegation",
            "memory",
        ):
            self.assertIn(toolset_name, disabled)
        # code_execution is deliberately NOT denied — children keep
        # execute_code for programmatic tool calling (Teknium, Jul 2026).
        self.assertNotIn("code_execution", disabled)

        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=kwargs["enabled_toolsets"],
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = {item["function"]["name"] for item in definitions}
        self.assertTrue(names & {"terminal", "read_file", "web_search"})
        self.assertTrue(DELEGATE_BLOCKED_TOOLS.isdisjoint(names))

    def test_orchestrator_composite_regains_only_delegate_task(self):
        import model_tools

        parent = _make_mock_parent()
        parent.enabled_toolsets = ["hermes-cli"]
        parent.disabled_toolsets = ["delegation", "browser"]

        with (
            patch("run_agent.AIAgent") as MockAgent,
            patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True),
            patch("tools.delegate_tool._get_max_spawn_depth", return_value=2),
        ):
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="Coordinate safely",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                role="orchestrator",
            )

        _, kwargs = MockAgent.call_args
        disabled = kwargs["disabled_toolsets"]
        self.assertNotIn("delegation", disabled)
        definitions = model_tools.get_tool_definitions(
            enabled_toolsets=kwargs["enabled_toolsets"],
            disabled_toolsets=disabled,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        names = {item["function"]["name"] for item in definitions}
        self.assertIn("delegate_task", names)
        self.assertTrue(
            (DELEGATE_BLOCKED_TOOLS - {"delegate_task"}).isdisjoint(names)
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

    def test_child_gets_dedicated_session_db_not_parents_handle(self):
        """#81267: children must not share the parent's SessionDB object.

        cron run_job closes its per-job SessionDB in its finally block while
        a fire-and-forget background delegation subagent is still flushing on
        a daemon thread. A SHARED handle then has ``_conn=None`` and every
        child flush raises ``'NoneType' object has no attribute 'execute'`` —
        the failure is downgraded to a WARNING and the child's transcript is
        silently dropped. Each child must own a dedicated connection that no
        parent teardown can close, released by the child's own close().
        """
        parent = _make_mock_parent(depth=0)
        parent_db = SessionDB()
        parent._session_db = parent_db
        try:
            with patch("run_agent.AIAgent") as MockAgent:
                mock_child = MagicMock()
                MockAgent.return_value = mock_child

                _build_child_agent(
                    task_index=0,
                    goal="test",
                    context=None,
                    toolsets=None,
                    model="test-model",
                    max_iterations=5,
                    parent_agent=parent,
                    task_count=1,
                )

                _, kwargs = MockAgent.call_args
                self.assertEqual(mock_child._owns_session_db, True)

            child_db = kwargs["session_db"]
            self.assertIsInstance(child_db, SessionDB)
            self.assertIsNot(child_db, parent_db)

            # Parent teardown (cron run_job finally, gateway session end)
            # must not break the child's handle — the #81267 crash mechanism.
            parent_db.close()
            self.assertIsNotNone(child_db._conn)
            child_db.create_session(
                session_id="child-session-81267",
                source="subagent",
                model="test-model",
            )
        finally:
            parent_db.close()

    def test_child_without_parent_db_still_degrades_to_none(self):
        """Parent without a SessionDB -> child gets None (pre-fix behaviour).

        The dedicated-handle path must not change the degradation contract:
        a parent that never opened a session store (headless/oneshot runs,
        test doubles) still yields ``session_db=None`` children.
        """
        parent = _make_mock_parent(depth=0)
        parent._session_db = None
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="test",
                context=None,
                toolsets=None,
                model="test-model",
                max_iterations=5,
                parent_agent=parent,
                task_count=1,
            )

            _, kwargs = MockAgent.call_args
            self.assertIsNone(kwargs["session_db"])

    def test_child_dedicated_db_follows_parents_db_path(self):
        """Per-profile parents: the child's dedicated handle must target the
        parent's database FILE, not the launch profile's default state.db.

        tui_gateway hands agents dedicated per-profile handles
        (``SessionDB(db_path=<profile_home>/state.db)`` via
        ``_transfer_db_to_agent``). A bare ``SessionDB()`` in
        ``_build_child_agent`` would write the child's transcript into the
        launch profile's db — cross-profile leakage that breaks
        ``parent_session_id`` lineage and ``session_search``.
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            profile_db_path = Path(tmp) / "profile-work" / "state.db"
            profile_db_path.parent.mkdir(parents=True)
            parent = _make_mock_parent(depth=0)
            parent_db = SessionDB(db_path=profile_db_path)
            parent._session_db = parent_db
            child_db = None
            try:
                with patch("run_agent.AIAgent") as MockAgent:
                    MockAgent.return_value = MagicMock()

                    _build_child_agent(
                        task_index=0,
                        goal="test",
                        context=None,
                        toolsets=None,
                        model="test-model",
                        max_iterations=5,
                        parent_agent=parent,
                        task_count=1,
                    )

                    _, kwargs = MockAgent.call_args

                child_db = kwargs["session_db"]
                self.assertIsInstance(child_db, SessionDB)
                self.assertIsNot(child_db, parent_db)
                self.assertEqual(
                    str(child_db.db_path), str(parent_db.db_path)
                )
            finally:
                if child_db is not None:
                    child_db.close()
                parent_db.close()

    def test_nous_child_rederives_api_mode_from_model(self):
        """Portal is dual-wire — same provider + different model prefix must
        not inherit the parent's Messages/chat_completions mode verbatim."""
        parent = _make_mock_parent(depth=0)
        parent.base_url = "https://inference-api.nousresearch.com/v1"
        parent.api_key = "portal-jwt"
        parent.provider = "nous"
        parent.api_mode = "anthropic_messages"
        parent.model = "anthropic/claude-opus-4.8"

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child

            _build_child_agent(
                task_index=0,
                goal="Stay on chat completions",
                context=None,
                toolsets=None,
                model="hermes-4-405b",
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["provider"], "nous")
            self.assertEqual(kwargs["model"], "hermes-4-405b")
            self.assertEqual(kwargs["api_mode"], "chat_completions")

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            MockAgent.return_value = mock_child
            parent.api_mode = "chat_completions"
            parent.model = "hermes-4-405b"

            _build_child_agent(
                task_index=0,
                goal="Move onto Messages",
                context=None,
                toolsets=None,
                model="anthropic/claude-opus-4.8",
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
            )

            _, kwargs = MockAgent.call_args
            self.assertEqual(kwargs["api_mode"], "anthropic_messages")

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
            self.assertEqual(
                entry["tool_trace"][0]["input_summary"],
                {"argument_keys": ["query"], "targets": {}},
            )
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

    def test_failed_child_with_explanation_is_not_marked_completed(self):
        """A failure explanation is useful output, but it is not success."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "gpt-5.6-sol"
            mock_child.session_prompt_tokens = 100
            mock_child.session_completion_tokens = 20
            mock_child.run_conversation.return_value = {
                "final_response": "Provider retries were exhausted.",
                "completed": False,
                "failed": True,
                "interrupted": False,
                "turn_exit_reason": "all_retries_exhausted_no_response",
                "api_calls": 4,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test failed explanation", parent_agent=parent)
            )

        entry = result["results"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(
            entry["exit_reason"], "all_retries_exhausted_no_response"
        )
        self.assertEqual(entry["error"], "Provider retries were exhausted.")

    def test_max_iterations_summary_remains_usable_completion(self):
        """A normal budget stop with useful output keeps legacy semantics."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "gpt-5.6-sol"
            mock_child.session_prompt_tokens = 100
            mock_child.session_completion_tokens = 20
            mock_child.run_conversation.return_value = {
                "final_response": "Useful partial findings.",
                "completed": False,
                "failed": False,
                "interrupted": False,
                "turn_exit_reason": "max_iterations_reached(4/4)",
                "api_calls": 4,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test normal budget stop", parent_agent=parent)
            )

        entry = result["results"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["exit_reason"], "max_iterations")

    def test_failed_child_with_error_summary_marks_status_failed(self):
        """Regression: a child whose loop gave up on a structured failure
        (``failed=True``, ``completed=False``, e.g. "API call failed after 3
        retries: HTTP 524") returns that error message as final_response.
        Status was derived from summary alone, so the non-empty error text
        made the batch report show the task as ✓ status=completed. The
        ``failed`` flag must win over a non-empty summary."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": (
                    "API call failed after 3 retries: HTTP 524 — origin timeout"
                ),
                "completed": False,
                "failed": True,
                "error": "HTTP 524 — origin timeout",
                "failure_reason": "server_error",
                "interrupted": False,
                "api_calls": 3,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test failed child", parent_agent=parent)
            )
            entry = result["results"][0]
            self.assertEqual(entry["status"], "failed")
            # The classified reason must survive into the batch entry so the
            # parent can tell a quota wall from a real task error.
            self.assertEqual(entry["failure_reason"], "server_error")
            self.assertEqual(entry["error"], "HTTP 524 — origin timeout")
            # A structured failure is not budget truncation.
            self.assertEqual(entry["exit_reason"], "error")
            self.assertFalse(entry["truncated"])

    def test_successful_child_still_completed(self):
        """Control for the failed-flag check: a child that succeeds
        (``completed=True``, no ``failed`` flag) must keep reporting
        status=completed — the fix must not change success behavior."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "All done.",
                "completed": True,
                "interrupted": False,
                "api_calls": 2,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test success control", parent_agent=parent)
            )
            entry = result["results"][0]
            self.assertEqual(entry["status"], "completed")
            self.assertEqual(entry["exit_reason"], "completed")
            self.assertNotIn("failure_reason", entry)


class TestDelegateFailedChildStatus(unittest.TestCase):
    """Honest status / exit_reason for failed subagents (issue #97655).

    A child that fails on its first API call (e.g. an HTTP 400 "not a valid
    model ID") returns completed=False with failed=True + an error string as
    its terminal final_response. It must be reported as status=failed with an
    honest exit_reason — never status=completed + exit_reason=max_iterations
    (which mislabels provider rejections as iteration-budget exhaustion and
    would render the false "TRUNCATED" banner).
    """

    def _delegate_single(self, child_result):
        """Dispatch a single task whose mock child returns `child_result`,
        returning the parsed child result entry dict."""
        parent = _make_mock_parent(depth=0)
        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = child_result
            MockAgent.return_value = mock_child
            result = json.loads(
                delegate_task(goal="Test child status", parent_agent=parent)
            )
            return result["results"][0]

    def test_failed_flag_marks_status_failed(self):
        """Regression (issue #97655): a provider-rejected child (HTTP 400 on its
        first call) returns completed=False with failed=True + an error string.
        It must be status=failed, exit_reason=error, and NOT truncated."""
        entry = self._delegate_single(
            {
                "final_response": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
                "completed": False,
                "interrupted": False,
                "failed": True,
                "error": "HTTP 400: upstage/solar-pro-4 is not a valid model ID",
                "api_calls": 1,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertFalse(entry["truncated"])

    def test_error_with_summary_still_failed(self):
        """A child that returns BOTH an error field and a summary must still be
        failed — the summary-presence heuristic must not override the
        structured failure."""
        entry = self._delegate_single(
            {
                "final_response": "partial work before crashing",
                "completed": False,
                "interrupted": False,
                "failed": True,
                "error": "provider boom",
                "api_calls": 3,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertFalse(entry["truncated"])

    def test_error_without_failed_flag_marks_failed(self):
        """A child result that carries a non-empty error string but OMITS the
        ``failed`` key entirely (not ``failed=False`` — the key is absent, as in
        legacy/partial result dicts) must still be status=failed + exit_reason=error.
        The status branch checks ``result.get('failed') or result.get('error')``,
        so the error field alone has to win — otherwise a dropped ``failed`` key
        would silently mislabel a provider rejection as budget exhaustion."""
        entry = self._delegate_single(
            {
                "final_response": "connection reset while streaming",
                "completed": False,
                "interrupted": False,
                "error": "connection reset",
                "api_calls": 2,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["exit_reason"], "error")
        self.assertFalse(entry["truncated"])

    def test_empty_error_with_summary_is_completed(self):
        """REGRESSION PIN: an empty-string ``error`` field must NOT be treated as
        a failure. ``result.get('error')`` returns ``''`` which is falsy, so the
        failure branch correctly falls through to the summary-presence heuristic.
        Empty error + a real summary => status=completed, exit_reason=completed
        (or max_iterations if completed=False), never 'error'."""
        entry = self._delegate_single(
            {
                "final_response": "work produced",
                "completed": True,
                "interrupted": False,
                "error": "",
                "api_calls": 2,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["exit_reason"], "completed")
        self.assertFalse(entry["truncated"])

    def test_genuine_truncation_stays_completed_max_iterations(self):
        """REGRESSION GUARD: a child that genuinely exhausts its iteration
        budget (completed=False, no failed flag, no error) but still returns a
        summary must keep status=completed, exit_reason=max_iterations, and
        truncated=True. This is the legitimate truncation path we must not
        break while making failure labels honest."""
        entry = self._delegate_single(
            {
                "final_response": "made partial progress before the budget ran out",
                "completed": False,
                "interrupted": False,
                "api_calls": 10,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["exit_reason"], "max_iterations")
        self.assertTrue(entry["truncated"])

    def test_interrupted_unchanged(self):
        """Interrupted children keep status=interrupted + exit_reason=interrupted
        and are not marked truncated."""
        entry = self._delegate_single(
            {
                "final_response": "some partial output",
                "completed": False,
                "interrupted": True,
                "api_calls": 2,
                "messages": [],
            }
        )
        self.assertEqual(entry["status"], "interrupted")
        self.assertEqual(entry["exit_reason"], "interrupted")
        self.assertFalse(entry["truncated"])



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
                    tasks=[
                        {"goal": "Investigate module A"},
                        {"goal": "Investigate module B"},
                        {"goal": "Investigate module C"},
                    ],
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

class TestBlockedTools(unittest.TestCase):

    def test_execute_code_not_blocked(self):
        """Children retain execute_code (programmatic tool calling) so they
        can batch mechanical work instead of burning reasoning iterations
        (Teknium, Jul 2026)."""
        self.assertNotIn("execute_code", DELEGATE_BLOCKED_TOOLS)

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

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_explicit_model_provider_override_config_runtime(self, mock_resolve):
        mock_resolve.return_value = {
            "model": "explicit-model",
            "provider": "explicit-provider",
            "base_url": "https://explicit.example/v1",
            "api_key": "explicit-key",
            "api_mode": "chat_completions",
        }
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "configured-model",
            "provider": "configured-provider",
            "base_url": "https://configured.example/v1",
            "api_key": "configured-key",
        }

        creds = _resolve_delegation_credentials(
            cfg,
            parent,
            model_override="explicit-model",
            provider_override="explicit-provider",
        )

        mock_resolve.assert_called_once_with(
            requested="explicit-provider", target_model="explicit-model"
        )
        self.assertEqual(creds["model"], "explicit-model")
        self.assertEqual(creds["provider"], "explicit-provider")
        self.assertEqual(creds["base_url"], "https://explicit.example/v1")
        self.assertEqual(creds["api_key"], "explicit-key")

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


    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_base_url_with_provider_carries_runtime_request_overrides(self, mock_resolve):
        """#65035: the base_url short-circuit must not drop the configured
        provider's request_overrides / max_output_tokens."""
        mock_resolve.return_value = {
            "provider": "custom",
            "base_url": "https://provider-default.example/v1",
            "api_key": "provider-key",
            "api_mode": "chat_completions",
            "request_overrides": {"extra_body": {"thinking": {"type": "disabled"}}},
            "max_output_tokens": 8192,
        }
        parent = _make_mock_parent(depth=0)
        cfg = {
            "model": "mimo-v2.5-pro",
            "provider": "mimo",
            "base_url": "https://api.xiaomimimo.com/v1",
            "api_key": "cfg-key",
        }
        creds = _resolve_delegation_credentials(cfg, parent)
        # Explicitly configured endpoint + key still win over the runtime's.
        self.assertEqual(creds["base_url"], "https://api.xiaomimimo.com/v1")
        self.assertEqual(creds["api_key"], "cfg-key")
        # The provider's request personality survives the short-circuit.
        self.assertEqual(
            creds["request_overrides"],
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(creds["max_output_tokens"], 8192)

    def test_bare_base_url_returns_none_overrides(self):
        """No provider alongside base_url → no overrides source; keys are
        present but None (shape parity with the inherit-everything path)."""
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "m", "provider": "", "base_url": "http://localhost:1234/v1", "api_key": "k"}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertIsNone(creds["request_overrides"])
        self.assertIsNone(creds["max_output_tokens"])

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_base_url_survives_runtime_resolution_failure(self, mock_resolve):
        """Best-effort: the explicit endpoint worked before this change even
        when the provider can't resolve — a resolution failure must not
        break it, only skip the overrides."""
        mock_resolve.side_effect = RuntimeError("MIMO_API_KEY not set")
        parent = _make_mock_parent(depth=0)
        cfg = {"model": "m", "provider": "mimo", "base_url": "https://api.xiaomimimo.com/v1", "api_key": "k"}
        creds = _resolve_delegation_credentials(cfg, parent)
        self.assertEqual(creds["base_url"], "https://api.xiaomimimo.com/v1")
        self.assertIsNone(creds["request_overrides"])
        self.assertIsNone(creds["max_output_tokens"])

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

class TestChildCredentialPoolResolution(unittest.TestCase):
    def test_same_provider_shares_parent_pool(self):
        parent = _make_mock_parent()
        mock_pool = MagicMock()
        parent._credential_pool = mock_pool

        result = _resolve_child_credential_pool("openrouter", parent)
        self.assertIs(result, mock_pool)

    # --- Custom-endpoint identity resolution (issue #7833) ---


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
    def test_child_conversation_worker_inherits_contextvars(self):
        from tools.delegate_tool import _run_single_child

        marker = ContextVar("delegate_child_marker", default="missing")
        token = marker.set("profile-a")
        child = MagicMock()
        child._credential_pool = None
        child._delegate_saved_tool_names = []
        child.tool_progress_callback = None
        child.get_activity_summary.return_value = {
            "api_call_count": 0,
            "max_iterations": 1,
        }
        child.run_conversation.side_effect = lambda **kwargs: {
            "final_response": marker.get(),
            "completed": True,
            "interrupted": False,
            "api_calls": 0,
            "messages": [],
        }

        try:
            result = _run_single_child(
                task_index=0,
                goal="Keep profile context",
                child=child,
                parent_agent=_make_mock_parent(),
            )
        finally:
            marker.reset(token)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "profile-a")

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
        first_touch = threading.Event()

        def record(desc):
            touch_calls.append(desc)
            first_touch.set()

        parent._touch_activity = record

        child = MagicMock()
        child.get_activity_summary.return_value = {
            "current_tool": "terminal",
            "api_call_count": 3,
            "max_iterations": 50,
            "last_activity_desc": "executing tool: terminal",
        }

        # Block the child only until the first heartbeat lands (bounded), so
        # the test is event-driven rather than sleep-timed.
        def slow_run(**kwargs):
            first_touch.wait(5)
            return {"final_response": "done", "completed": True, "api_calls": 3}

        child.run_conversation.side_effect = slow_run

        # Patch the heartbeat interval to fire quickly
        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01):
            _run_single_child(
                task_index=0,
                goal="Test heartbeat",
                child=child,
                parent_agent=parent,
            )

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

        with patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01):
            _run_single_child(
                task_index=0,
                goal="Test cleanup",
                child=child,
                parent_agent=parent,
            )

        # Record count after completion, wait several heartbeat intervals, and
        # verify no more calls landed.
        count_after = len(touch_calls)
        time.sleep(0.05)
        self.assertEqual(len(touch_calls), count_after,
                         "Heartbeat continued firing after child completed")

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
        kept_going = threading.Event()

        def record(desc):
            touch_calls.append(desc)
            if len(touch_calls) > 2:
                kept_going.set()

        parent._touch_activity = record

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
            # Return as soon as the heartbeat has proven it kept firing past
            # the idle threshold. If the idle rules wrongly applied, the event
            # never sets and the bounded wait expires, failing the assertion
            # below instead of hanging.
            kept_going.wait(5)
            return {"final_response": "done", "completed": True, "api_calls": 1}

        child.run_conversation.side_effect = slow_run

        # Use tiny thresholds so the assertion is scheduler-robust in CI:
        # if idle rules were used for in-tool work, heartbeat would stop after
        # ~2 cycles. The in-tool branch should keep touching well past that.
        with (
            patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01),
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
            f"got {len(touch_calls)} touches",
        )

    def test_heartbeat_does_not_trip_idle_stale_while_waiting_on_model(self):
        """A slow in-flight model wait (api_call_count frozen, no tool) must
        stay alive when last_activity_ts keeps advancing.

        Top-level delegate_task runs in the background; the async stall
        monitor already treats ticking last_activity_ts as progress. The sync
        heartbeat path must use the same signal so slow local / long-prefill
        completions are not mistaken for a wedged idle child.
        """
        from tools.delegate_tool import _run_single_child

        parent = _make_mock_parent()
        touch_calls = []
        kept_going = threading.Event()

        def record(desc):
            touch_calls.append(desc)
            if len(touch_calls) > 2:
                kept_going.set()

        parent._touch_activity = record

        child = MagicMock()
        activity = {"ts": 1000.0}

        def _summary():
            # Frozen iteration / no tool — only the activity clock moves,
            # matching direct_api_call's mid-wait heartbeats.
            activity["ts"] += 1.0
            return {
                "current_tool": None,
                "api_call_count": 1,
                "max_iterations": 50,
                "last_activity_desc": "waiting for non-streaming API response",
                "last_activity_ts": activity["ts"],
            }

        child.get_activity_summary.side_effect = _summary

        def slow_run(**kwargs):
            kept_going.wait(5)
            return {"final_response": "done", "completed": True, "api_calls": 1}

        child.run_conversation.side_effect = slow_run

        with (
            patch("tools.delegate_tool._HEARTBEAT_INTERVAL", 0.01),
            patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IDLE", 2),
            patch("tools.delegate_tool._HEARTBEAT_STALE_CYCLES_IN_TOOL", 40),
        ):
            _run_single_child(
                task_index=0,
                goal="Test slow model wait",
                child=child,
                parent_agent=parent,
            )

        self.assertGreater(
            len(touch_calls), 2,
            f"Heartbeat stopped too early while child was waiting on the model; "
            f"got {len(touch_calls)} touches",
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


# =========================================================================
# Model-route delegation (ADR-003 Phase 3a)
# =========================================================================


def _route_credentials(route_name):
    return {
        "model": f"model-{route_name}",
        "provider": f"provider-{route_name}",
        "base_url": f"https://{route_name}.example/v1",
        "api_key": f"key-{route_name}",
        "api_mode": "chat_completions",
        "request_overrides": {},
        "max_output_tokens": None,
        "command": None,
        "args": [],
    }


class TestDelegateRouteSchema(unittest.TestCase):
    """Route names are config-derived and dormant without the catalog."""

    @patch("tools.delegate_tool._route_catalog_pairs", return_value=[])
    def test_schema_hides_route_when_catalog_is_dormant(self, _mock_pairs):
        static_before = copy.deepcopy(DELEGATE_TASK_SCHEMA)

        overrides = _build_dynamic_schema_overrides()
        props = overrides["parameters"]["properties"]
        static_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]

        self.assertNotIn("route", props)
        self.assertNotIn("route", props["tasks"]["items"]["properties"])
        self.assertEqual(set(props), set(static_props))
        self.assertIs(props["tasks"]["items"], static_props["tasks"]["items"])
        self.assertEqual(DELEGATE_TASK_SCHEMA, static_before)

    @patch(
        "tools.delegate_tool._route_catalog_pairs",
        return_value=[
            ("dev", "Deep coding and debugging"),
            ("chat", "Quick conversation"),
        ],
    )
    def test_schema_injects_route_enum_without_raw_model_surface(self, _mock_pairs):
        static_before = copy.deepcopy(DELEGATE_TASK_SCHEMA)

        overrides = _build_dynamic_schema_overrides()
        props = overrides["parameters"]["properties"]

        self.assertEqual(props["route"]["enum"], ["dev", "chat"])
        self.assertIn("dev: Deep coding and debugging", props["route"]["description"])
        self.assertIn("chat: Quick conversation", props["route"]["description"])
        self.assertEqual(
            props["tasks"]["items"]["properties"]["route"]["enum"],
            ["dev", "chat"],
        )
        self.assertNotIn("model", props)
        self.assertNotIn("provider", props)
        self.assertNotIn("route", DELEGATE_TASK_SCHEMA["parameters"]["properties"])
        self.assertEqual(DELEGATE_TASK_SCHEMA, static_before)

    def test_schema_enum_tracks_catalog_changes(self):
        with patch(
            "tools.delegate_tool._route_catalog_pairs", return_value=[("dev", "d")]
        ):
            first = _build_dynamic_schema_overrides()
        with patch(
            "tools.delegate_tool._route_catalog_pairs",
            return_value=[("dev", "d"), ("docs", "documents")],
        ):
            second = _build_dynamic_schema_overrides()

        self.assertEqual(
            first["parameters"]["properties"]["route"]["enum"], ["dev"]
        )
        self.assertEqual(
            second["parameters"]["properties"]["route"]["enum"],
            ["dev", "docs"],
        )


class TestDelegateRouteResolution(unittest.TestCase):
    """Catalog lookup errors and resolved runtime plumbing."""

    @staticmethod
    def _fake_routes_module(resolve_result):
        module = types.ModuleType("hermes_cli.model_routes")
        catalog = types.SimpleNamespace(
            routes={
                "dev": types.SimpleNamespace(name="dev"),
                "chat": types.SimpleNamespace(name="chat"),
            }
        )
        module.load_routes = MagicMock(return_value=catalog)
        module.resolve_route = MagicMock(return_value=resolve_result)
        return module

    @patch("tools.delegate_tool._load_config", return_value={"max_iterations": 5})
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("run_agent.AIAgent")
    def test_requested_route_gets_teaching_error_without_catalog(
        self, MockAgent, mock_credentials, _mock_cfg
    ):
        parent = _make_mock_parent()
        with patch.dict(sys.modules, {"hermes_cli.model_routes": None}):
            result = delegate_task(
                goal="dormant route seam",
                route="dev",
                background=False,
                parent_agent=parent,
            )

        self.assertIn("Route-based delegation is unavailable", result)
        self.assertIn("omit 'route'", result.lower())
        mock_credentials.assert_not_called()
        MockAgent.assert_not_called()

    def test_resolver_is_case_insensitive_and_passes_route_runtime_as_overrides(self):
        resolved = {
            "route": "dev",
            "provider": "p1",
            "model": "model-a",
            "reasoning_effort": "xhigh",
        }
        module = self._fake_routes_module(resolved)
        expected_creds = _route_credentials("dev")
        parent = _make_mock_parent()
        full_cfg = {"model_routes": {"routes": {"dev": {}}}}

        with patch.dict(sys.modules, {"hermes_cli.model_routes": module}), patch(
            "tools.delegate_tool._load_full_config", return_value=full_cfg
        ), patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=expected_creds,
        ) as mock_credentials:
            creds, effort = _resolve_route_override("  DEV  ", {}, parent)

        self.assertIs(creds, expected_creds)
        self.assertEqual(effort, "xhigh")
        module.resolve_route.assert_called_once_with(
            "dev", full_cfg, catalog=module.load_routes.return_value
        )
        mock_credentials.assert_called_once_with(
            {},
            parent,
            model_override="model-a",
            provider_override="p1",
        )

    def test_unknown_and_unhealthy_routes_name_declared_catalog(self):
        module = self._fake_routes_module(None)
        parent = _make_mock_parent()

        with patch.dict(sys.modules, {"hermes_cli.model_routes": module}), patch(
            "tools.delegate_tool._load_full_config", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "Declared routes: dev, chat"):
                _resolve_route_override("docs", {}, parent)
            with self.assertRaisesRegex(ValueError, "no healthy runtime"):
                _resolve_route_override("dev", {}, parent)

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._resolve_route_override")
    @patch("run_agent.AIAgent")
    def test_route_runtime_and_reasoning_reach_child(
        self, MockAgent, mock_route, mock_default_credentials, mock_cfg
    ):
        cfg = {
            "max_iterations": 5,
            "provider": "broken-config-provider",
            "reasoning_effort": "low",
        }
        mock_cfg.return_value = cfg
        mock_route.return_value = (_route_credentials("dev"), "xhigh")
        child = MagicMock()
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "api_calls": 1,
        }
        MockAgent.return_value = child
        parent = _make_mock_parent()

        delegate_task(
            goal="route runtime reaches child",
            route="dev",
            background=False,
            parent_agent=parent,
        )

        mock_route.assert_called_once_with("dev", cfg, parent)
        mock_default_credentials.assert_not_called()
        kwargs = MockAgent.call_args.kwargs
        self.assertEqual(kwargs["model"], "model-dev")
        self.assertEqual(kwargs["provider"], "provider-dev")
        self.assertEqual(kwargs["base_url"], "https://dev.example/v1")
        self.assertEqual(kwargs["api_key"], "key-dev")
        self.assertEqual(
            kwargs["reasoning_config"], {"enabled": True, "effort": "xhigh"}
        )

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._resolve_route_override")
    @patch("run_agent.AIAgent")
    def test_delegation_default_route_applies_without_call_selector(
        self, MockAgent, mock_route, mock_default_credentials, mock_cfg
    ):
        cfg = {"max_iterations": 5, "default_route": "docs"}
        mock_cfg.return_value = cfg
        mock_route.return_value = (_route_credentials("docs"), "")
        child = MagicMock()
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "api_calls": 1,
        }
        MockAgent.return_value = child
        parent = _make_mock_parent()

        delegate_task(
            goal="default route child",
            background=False,
            parent_agent=parent,
        )

        mock_route.assert_called_once_with("docs", cfg, parent)
        mock_default_credentials.assert_not_called()
        self.assertEqual(MockAgent.call_args.kwargs["model"], "model-docs")

    @patch("tools.delegate_tool._load_config", return_value={"max_iterations": 5})
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._resolve_route_override")
    @patch("run_agent.AIAgent")
    def test_mixed_batch_resolves_config_credentials_only_for_unrouted_task(
        self, MockAgent, mock_route, mock_default_credentials, _mock_cfg
    ):
        mock_route.return_value = (_route_credentials("dev"), "")
        mock_default_credentials.return_value = _route_credentials("configured")
        children = []
        for label in ("dev", "configured"):
            child = MagicMock(name=label)
            child.run_conversation.return_value = {
                "final_response": label,
                "completed": True,
                "api_calls": 1,
            }
            children.append(child)
        MockAgent.side_effect = children
        parent = _make_mock_parent()

        delegate_task(
            tasks=[
                {"goal": "routed child", "route": "dev"},
                {"goal": "configured child"},
            ],
            background=False,
            parent_agent=parent,
        )

        mock_route.assert_called_once()
        mock_default_credentials.assert_called_once_with(
            {"max_iterations": 5}, parent
        )
        self.assertEqual(
            [call.kwargs["model"] for call in MockAgent.call_args_list],
            ["model-dev", "model-configured"],
        )

    @patch("tools.delegate_tool._load_config", return_value={"max_iterations": 5})
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._resolve_route_override")
    @patch("run_agent.AIAgent")
    def test_per_task_route_wins_and_each_distinct_route_resolves_once(
        self, MockAgent, mock_route, mock_default_credentials, _mock_cfg
    ):
        def resolve(name, _cfg, _parent):
            canonical = name.casefold()
            return _route_credentials(canonical), ""

        mock_route.side_effect = resolve
        children = []
        for label in ("chat", "dev-a", "dev-b"):
            child = MagicMock(name=label)
            child.run_conversation.return_value = {
                "final_response": label,
                "completed": True,
                "api_calls": 1,
            }
            children.append(child)
        MockAgent.side_effect = children
        parent = _make_mock_parent()

        delegate_task(
            tasks=[
                {"goal": "top route child"},
                {"goal": "per-task dev child", "route": "dev"},
                {"goal": "same route casefold child", "route": "DEV"},
            ],
            route="chat",
            background=False,
            parent_agent=parent,
        )

        self.assertEqual(
            [call.args[0] for call in mock_route.call_args_list], ["chat", "dev"]
        )
        self.assertEqual(
            [call.kwargs["model"] for call in MockAgent.call_args_list],
            ["model-chat", "model-dev", "model-dev"],
        )
        mock_default_credentials.assert_not_called()

    @patch("tools.delegate_tool._load_config")
    @patch("tools.delegate_tool._resolve_delegation_credentials")
    @patch("tools.delegate_tool._resolve_route_override")
    @patch("run_agent.AIAgent")
    def test_explicit_model_provider_remain_compatible_and_beat_default_route(
        self, MockAgent, mock_route, mock_credentials, mock_cfg
    ):
        cfg = {
            "max_iterations": 5,
            "default_route": "dev",
            "model": "configured-model",
            "provider": "configured-provider",
        }
        mock_cfg.return_value = cfg
        mock_credentials.return_value = _route_credentials("explicit")
        child = MagicMock()
        child.run_conversation.return_value = {
            "final_response": "done",
            "completed": True,
            "api_calls": 1,
        }
        MockAgent.return_value = child
        parent = _make_mock_parent()

        delegate_task(
            goal="explicit override compatibility",
            model="explicit-model",
            provider="explicit-provider",
            background=False,
            parent_agent=parent,
        )

        mock_route.assert_not_called()
        mock_credentials.assert_called_once_with(
            cfg,
            parent,
            model_override="explicit-model",
            provider_override="explicit-provider",
        )
        self.assertEqual(MockAgent.call_args.kwargs["model"], "model-explicit")


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

    def test_route_and_explicit_compatibility_fields_are_forwarded(self):
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
                    "goal": "route a child",
                    "route": "dev",
                    "model": "legacy-model",
                    "provider": "legacy-provider",
                },
            )

        self.assertEqual(captured["route"], "dev")
        self.assertEqual(captured["model"], "legacy-model")
        self.assertEqual(captured["provider"], "legacy-provider")
        self.assertIs(captured["parent_agent"], parent)


class TestDelegateEventEnum(unittest.TestCase):
    """Tests for DelegateEvent enum and back-compat aliases."""

    def test_progress_callback_normalises_tool_started(self):
        """_build_child_progress_callback handles tool.started via enum."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()
        parent.tool_progress_callback = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        self.assertIsNotNone(cb)

        cb("tool.started", tool_name="terminal", preview="ls")
        parent._delegate_spinner.print_above.assert_called()


    def test_progress_callback_ignores_unknown_events(self):
        """Unknown event types are silently ignored."""
        parent = _make_mock_parent()
        parent._delegate_spinner = MagicMock()

        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        # Should not raise
        cb("some.unknown.event", tool_name="x")
        parent._delegate_spinner.print_above.assert_not_called()

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


    @patch("tools.delegate_tool._load_config",
           return_value={"max_concurrent_children": 0})
    def test_zero_clamped_to_one(self, mock_cfg):
        """Floor of 1 is enforced; zero or negative values raise to 1."""
        self.assertEqual(_get_max_concurrent_children(), 1)

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

    def test_role_is_depth_derived_not_caller_declared(self):
        """With max_spawn_depth=2 (mocked), a depth-1 child has depth budget
        left, so it becomes an orchestrator automatically — no role arg
        needed, and a passed legacy role arg is ignored either way."""
        child = self._run_with_mock_child(_SENTINEL)
        self.assertEqual(child._delegate_role, "orchestrator")
        # Legacy explicit role='leaf' does not override the depth derivation.
        child = self._run_with_mock_child("leaf")
        self.assertEqual(child._delegate_role, "orchestrator")

    def test_schema_no_longer_advertises_role(self):
        """`role` left the advertised schema (capability is depth-derived);
        the handler still accepts it for wire compat."""
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertNotIn("role", props)
        self.assertNotIn("role", props["tasks"]["items"]["properties"])

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


    # ── Role-aware system prompt ────────────────────────────────────────

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
                        tasks=[
                            {"goal": "Do leaf work stream A"},
                            {"goal": "Do leaf work stream B"},
                        ],
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

    @patch("tools.delegate_tool._load_config", return_value={})
    def test_getter_defaults_to_deny(self, _mock_cfg):
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

    def test_pinned_provider_disables_parent_fallback_chain(self):
        """An explicit delegation.provider pin must NOT inherit the parent
        fallback chain — a mid-run failure on the pin would otherwise silently
        reroute the quiet-mode child onto parent fallback models (#80450)."""
        parent = _make_mock_parent(depth=0)
        parent._fallback_chain = [
            {"provider": "openrouter", "model": "gpt-4o-mini", "api_key": "sk-or-x"}
        ]

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="test pinned provider",
                context=None,
                toolsets=None,
                model="minimax/m2",
                max_iterations=10,
                parent_agent=parent,
                task_count=1,
                override_provider="minimax",
                override_base_url="https://api.minimax.example/v1",
                override_api_key="sk-mm-x",
            )

        _, kwargs = MockAgent.call_args
        self.assertIsNone(kwargs["fallback_model"])

    def test_pinned_acp_command_missing_raises(self):
        """A pinned delegation command absent from PATH must refuse the spawn
        loudly instead of silently falling back to the default transport
        (#80450)."""
        parent = _make_mock_parent(depth=0)
        parent._fallback_chain = None

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = MagicMock()
            with patch("shutil.which", return_value=None):
                with self.assertRaises(ValueError) as ctx:
                    _build_child_agent(
                        task_index=0,
                        goal="test pinned acp command",
                        context=None,
                        toolsets=None,
                        model=None,
                        max_iterations=10,
                        parent_agent=parent,
                        task_count=1,
                        override_acp_command="definitely-not-a-real-binary",
                    )
        self.assertIn("definitely-not-a-real-binary", str(ctx.exception))
        self.assertIn("not", str(ctx.exception).lower())

    def test_resolve_credentials_rejects_missing_pinned_command(self):
        """_resolve_delegation_credentials refuses a provider whose pinned
        command is not installed (#80450)."""
        cfg = {"provider": "acp-provider", "model": "some-model"}
        parent = _make_mock_parent(depth=0)
        runtime = {
            "api_key": "sk-x",
            "base_url": "https://api.example/v1",
            "api_mode": "chat_completions",
            "provider": "acp-provider",
            "command": "missing-acp-binary",
            "args": [],
        }
        with patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=runtime,
        ):
            with patch("shutil.which", return_value=None):
                with self.assertRaises(ValueError) as ctx:
                    _resolve_delegation_credentials(cfg, parent)
        self.assertIn("missing-acp-binary", str(ctx.exception))


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
