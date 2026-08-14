"""
Tests for subagent progress relay (issue #169).

Verifies that:
- KawaiiSpinner.print_above() works with and without active spinner
- _build_child_progress_callback handles CLI/gateway/no-display paths
- Thinking events are relayed correctly
- Parallel callbacks don't share state
"""

import io
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

from agent.agent_runtime_helpers import emit_reasoning_progress, extract_reasoning
from agent.display import KawaiiSpinner
from tools.delegate_tool import _build_child_progress_callback


# =========================================================================
# KawaiiSpinner.print_above tests
# =========================================================================

class TestPrintAbove:
    """Tests for KawaiiSpinner.print_above method."""

    def test_print_above_without_spinner_running(self):
        """print_above should write to stdout even when spinner is not running."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf  # Redirect to buffer
        
        spinner.print_above("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_print_above_with_spinner_running(self):
        """print_above should clear spinner line and print text."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True  # Pretend spinner is running (don't start thread)
        
        spinner.print_above("tool line")
        output = buf.getvalue()
        assert "tool line" in output
        assert "\r" in output  # Should start with carriage return to clear spinner line

    def test_print_above_uses_captured_stdout(self):
        """print_above should use self._out, not sys.stdout.
        This ensures it works inside redirect_stdout(devnull)."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        
        # Simulate redirect_stdout(devnull)
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            spinner.print_above("should go to buf")
        finally:
            sys.stdout = old_stdout
        
        assert "should go to buf" in buf.getvalue()


# =========================================================================
# _build_child_progress_callback tests
# =========================================================================

class TestBuildChildProgressCallback:
    """Tests for child progress callback builder."""




    def test_gateway_batched_progress(self):
        """Gateway path: each tool.started relays a subagent.tool event, and a
        subagent.progress summary fires once BATCH_SIZE tools accumulate."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Each tool.started relays a subagent.tool event immediately (per-tool relay).
        for i in range(4):
            cb("tool.started", f"tool_{i}", f"arg_{i}", {})
        # 4 per-tool relays so far, no batch summary yet (BATCH_SIZE=5)
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 4

        # 5th call triggers another per-tool relay PLUS the batch-size summary
        cb("tool.started", "tool_4", "arg_4", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 5 + ["subagent.progress"]
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "tool_0" in summary_text
        assert "tool_4" in summary_text


    def test_parallel_callbacks_independent(self):
        """Each child's callback batches tool names independently."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb0 = _build_child_progress_callback(0, "goal a", parent)
        cb1 = _build_child_progress_callback(1, "goal b", parent)

        # 3 tool.started per child = 6 per-tool relays; neither should hit
        # the batch-size summary (batch size = 5, counted per-child).
        for i in range(3):
            cb0("tool.started", f"tool_{i}", f"a_{i}", {})
            cb1("tool.started", f"other_{i}", f"b_{i}", {})

        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events.count("subagent.tool") == 6
        assert "subagent.progress" not in events

    def test_task_index_prefix_in_batch_mode(self):
        """Batch mode (task_count > 1) should show 1-indexed prefix for all tasks."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        # task_index=0 in a batch of 3 → prefix "[1]"
        cb0 = _build_child_progress_callback(0, "test goal", parent, task_count=3)
        cb0("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[1]" in output

        # task_index=2 in a batch of 3 → prefix "[3]"
        buf.truncate(0)
        buf.seek(0)
        cb2 = _build_child_progress_callback(2, "test goal", parent, task_count=3)
        cb2("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[3]" in output



# =========================================================================
# Integration: thinking callback in run_agent.py
# =========================================================================

class TestThinkingCallback:
    """Behavior tests for the production reasoning-progress classifier."""

    @staticmethod
    def _agent(callback, *, delegate_depth=0):
        return SimpleNamespace(
            tool_progress_callback=callback,
            _delegate_depth=delegate_depth,
            _reasoning_progress_seen=set(),
            _extract_reasoning=lambda message: extract_reasoning(None, message),
        )

    def test_final_assistant_content_is_not_thinking(self):
        calls = []
        agent = self._agent(lambda *args: calls.append(args))

        emitted = emit_reasoning_progress(
            agent,
            SimpleNamespace(content="The final answer belongs to final delivery."),
        )

        assert emitted is False
        assert calls == []

    def test_structured_reasoning_emits_structured_event(self):
        calls = []
        agent = self._agent(lambda *args: calls.append(args))

        emitted = emit_reasoning_progress(
            agent,
            SimpleNamespace(
                content="The final answer.",
                reasoning="Checking the relevant delivery invariant.",
            ),
        )

        assert emitted is True
        assert calls == [
            (
                "reasoning.available",
                "_thinking",
                "Checking the relevant delivery invariant.",
                None,
            )
        ]

    def test_explicit_reasoning_tags_emit_only_scratch(self):
        calls = []
        agent = self._agent(lambda *args: calls.append(args))

        emit_reasoning_progress(
            agent,
            SimpleNamespace(
                content="<think>Inspect the queue ordering.</think>\nThe final answer.",
            ),
        )

        assert calls[0][2] == "Inspect the queue ordering."
        assert "final answer" not in calls[0][2].lower()

    def test_reasoning_progress_is_turn_deduped_and_bounded(self):
        calls = []
        agent = self._agent(lambda *args: calls.append(args))
        message = SimpleNamespace(content="done", reasoning="A" * 700)

        assert emit_reasoning_progress(agent, message) is True
        assert emit_reasoning_progress(agent, message) is False

        assert len(calls) == 1
        assert len(calls[0][2]) == 500

    def test_subagent_final_summary_is_not_thinking(self):
        calls = []
        agent = self._agent(
            lambda *args: calls.append(args),
            delegate_depth=1,
        )

        emit_reasoning_progress(
            agent,
            SimpleNamespace(content="Completed the delegated task successfully."),
        )

        assert calls == []

    def test_subagent_real_reasoning_keeps_legacy_relay_shape(self):
        calls = []
        agent = self._agent(
            lambda *args: calls.append(args),
            delegate_depth=1,
        )

        emit_reasoning_progress(
            agent,
            SimpleNamespace(content="done", reasoning="R" * 120 + "\nnext line"),
        )

        assert calls == [("_thinking", "R" * 80)]

    def test_opaque_reasoning_replay_payload_is_not_emitted(self):
        calls = []
        agent = self._agent(lambda *args: calls.append(args))

        emitted = emit_reasoning_progress(
            agent,
            SimpleNamespace(
                content="The final answer.",
                reasoning_details=[
                    {
                        "type": "reasoning",
                        "id": "rs_opaque",
                        "encrypted_content": "opaque-provider-blob",
                        "signature": "opaque-signature",
                    }
                ],
            ),
        )

        assert emitted is False
        assert calls == []

    def test_reasoning_progress_is_force_redacted(self, monkeypatch):
        calls = []
        redaction = {}

        def _redact(text, *, force=False, **kwargs):
            redaction["force"] = force
            return text.replace("raw-secret", "[REDACTED]")

        monkeypatch.setattr("agent.redact.redact_sensitive_text", _redact)
        agent = self._agent(lambda *args: calls.append(args))

        emit_reasoning_progress(
            agent,
            SimpleNamespace(content="done", reasoning="Inspect raw-secret safely."),
        )

        assert redaction["force"] is True
        assert calls[0][2] == "Inspect [REDACTED] safely."






# =========================================================================
# Gateway batch flush tests
# =========================================================================

class TestBatchFlush:
    """Tests for gateway batch flush on subagent completion."""

    def test_flush_sends_remaining_batch(self):
        """_flush should send a final subagent.progress summary of any unsent
        tool names in the batch (less than BATCH_SIZE)."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Send 3 tools (below batch size of 5) — each relays subagent.tool
        cb("tool.started", "web_search", "query1", {})
        cb("tool.started", "read_file", "file.txt", {})
        cb("tool.started", "write_file", "out.txt", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool"] * 3  # per-tool relays so far
        assert "subagent.progress" not in events  # no batch-size summary yet

        # Flush should send the remaining 3 as a summary
        cb._flush()
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events[-1] == "subagent.progress"
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "web_search" in summary_text
        assert "write_file" in summary_text

    def test_flush_noop_when_batch_empty(self):
        """_flush should not send anything when batch is empty."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb._flush()
        parent_cb.assert_not_called()

    def test_flush_noop_when_no_parent_callback(self):
        """_flush should not crash when there's no parent callback."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True

        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("tool.started", "web_search", "test", {})
        cb._flush()  # Should not crash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
