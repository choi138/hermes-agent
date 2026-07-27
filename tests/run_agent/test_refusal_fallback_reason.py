"""Tests for fallback-activation reason plumbing (refusal-fallback-reason).

When ``_try_activate_fallback()`` swaps to a fallback model, the reason the
swap happened (e.g. a safety refusal → ``content_policy_blocked``) is recorded
on ``agent._fallback_reason`` and published to plugins via the
``runtime_state`` hook.  Capability-gating plugins (skill-gate) use this to
distinguish a refusal-driven temporary fallback from any other state.

Covers:
1. The reason is stored on successful activation and stays None otherwise
2. ``_restore_primary_runtime()`` clears it; the cooldown gate does not
3. ``get_runtime_state()`` exposes fallback_activated / fallback_reason
4. The runtime_state hook fires on activation (event="fallback") and
   restoration (event="restore")
5. switch_model and the transport-recovery reset clear the reason
6. The conversation-loop refusal branches forward the reason into
   ``_try_activate_fallback()`` (the wiring skill-gate depends on)
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from agent.runtime_control import get_runtime_state
from hermes_constants import FINISH_REASON_LENGTH, PARTIAL_STREAM_STUB_ID
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    """Create a minimal AIAgent with optional fallback config."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://api.example.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


_FB = {"provider": "openai", "model": "gpt-4o"}


def _loop_response(content="Recovered on fallback.", finish_reason="stop"):
    """Minimal OpenAI-style response for driving run_conversation()."""
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(index=0, message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


# ── Reason recorded on activation ─────────────────────────────────────────


class TestFallbackReasonRecording:
    def test_defaults_to_none_at_init(self):
        agent = _make_agent(fallback_model=[_FB])
        assert agent._fallback_reason is None

    def test_activation_with_reason_stores_enum_value(self):
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        assert agent._fallback_reason == "content_policy_blocked"

    def test_activation_without_reason_stores_none(self):
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            assert agent._try_activate_fallback() is True
        assert agent._fallback_activated is True
        assert agent._fallback_reason is None

    def test_failed_activation_leaves_reason_none(self):
        """Chain exhaustion (empty chain) must not record a reason."""
        agent = _make_agent(fallback_model=None)
        assert agent._try_activate_fallback(
            reason=FailoverReason.content_policy_blocked
        ) is False
        assert agent._fallback_reason is None

    def test_reason_flows_through_chain_skip_recursion(self):
        """When the first entry is unavailable, the recursive retry must
        still record the reason at the entry that actually activates."""
        agent = _make_agent(fallback_model=[
            {"provider": "broken", "model": "nope"},
            _FB,
        ])
        with patch("agent.auxiliary_client.resolve_provider_client") as mock_rpc:
            mock_rpc.side_effect = [
                (None, None),                # broken provider skipped
                (_mock_client(), "gpt-4o"),  # second entry activates
            ]
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        assert agent._fallback_reason == "content_policy_blocked"

    def test_reason_survives_second_activation_in_same_turn(self):
        """Chain-switching mid-turn overwrites with the new reason."""
        agent = _make_agent(fallback_model=[
            _FB,
            {"provider": "zai", "model": "glm-4.7"},
        ])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "resolved")):
            agent._try_activate_fallback(reason=FailoverReason.content_policy_blocked)
            agent._try_activate_fallback(reason=FailoverReason.auth)
        assert agent._fallback_reason == "auth"


# ── restore_primary_runtime lifecycle ─────────────────────────────────────


class TestRestoreClearsReason:
    def _activated_agent(self):
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        return agent

    def test_restore_clears_reason_and_flag(self):
        agent = self._activated_agent()
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            assert agent._restore_primary_runtime() is True
        assert agent._fallback_activated is False
        assert agent._fallback_reason is None

    def test_cooldown_early_return_keeps_reason(self):
        """While the primary is cooling down the agent stays on the fallback,
        so the reason must survive for plugins to keep honoring it."""
        agent = self._activated_agent()
        agent._rate_limited_until = time.monotonic() + 60
        assert agent._restore_primary_runtime() is False
        assert agent._fallback_activated is True
        assert agent._fallback_reason == "content_policy_blocked"


# ── get_runtime_state exposure ────────────────────────────────────────────


class TestRuntimeStateKeys:
    def test_state_before_activation(self):
        agent = _make_agent(fallback_model=[_FB])
        state = get_runtime_state(agent)
        assert state["fallback_activated"] is False
        assert state["fallback_reason"] is None

    def test_state_after_activation(self):
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            agent._try_activate_fallback(reason=FailoverReason.content_policy_blocked)
        state = get_runtime_state(agent)
        assert state["fallback_activated"] is True
        assert state["fallback_reason"] == "content_policy_blocked"

    def test_state_defaults_on_bare_agent(self):
        """Agents predating the attribute (restored sessions) degrade safely."""
        class Bare:
            pass
        state = get_runtime_state(Bare())
        assert state["fallback_activated"] is False
        assert state["fallback_reason"] is None


# ── runtime_state hook publication ────────────────────────────────────────


def _hook_calls_for_event(mock_hook, event):
    return [
        call for call in mock_hook.call_args_list
        if call.args and call.args[0] == "runtime_state"
        and call.kwargs.get("event") == event
    ]


class TestRuntimeStateHook:
    def test_hook_fires_on_activation(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.plugins.invoke_hook") as mock_hook,
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        calls = _hook_calls_for_event(mock_hook, "fallback")
        assert len(calls) == 1
        state = calls[0].kwargs["state"]
        assert state["fallback_activated"] is True
        assert state["fallback_reason"] == "content_policy_blocked"
        assert state["model"] == "gpt-4o"

    def test_hook_failure_does_not_break_activation(self):
        agent = _make_agent(fallback_model=[_FB])
        with (
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("hermes_cli.plugins.invoke_hook",
                  side_effect=RuntimeError("plugin exploded")),
        ):
            assert agent._try_activate_fallback(
                reason=FailoverReason.content_policy_blocked
            ) is True
        assert agent._fallback_activated is True

    def test_hook_fires_on_restore(self):
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            agent._try_activate_fallback(reason=FailoverReason.content_policy_blocked)
        with (
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("hermes_cli.plugins.invoke_hook") as mock_hook,
        ):
            assert agent._restore_primary_runtime() is True
        calls = _hook_calls_for_event(mock_hook, "restore")
        assert len(calls) == 1
        state = calls[0].kwargs["state"]
        assert state["fallback_activated"] is False
        assert state["fallback_reason"] is None

    def test_no_fallback_event_when_activation_fails(self):
        agent = _make_agent(fallback_model=None)
        with patch("hermes_cli.plugins.invoke_hook") as mock_hook:
            assert agent._try_activate_fallback() is False
        assert _hook_calls_for_event(mock_hook, "fallback") == []


# ── Other reset paths ─────────────────────────────────────────────────────


class TestOtherResetPaths:
    def test_switch_model_clears_reason(self):
        """A deliberate primary switch resets all fallback state."""
        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            agent._try_activate_fallback(reason=FailoverReason.content_policy_blocked)
        assert agent._fallback_reason == "content_policy_blocked"
        with (
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
        ):
            agent.switch_model(
                new_model="glm-4.7",
                new_provider="zai",
                api_key="zai-key",
                base_url="https://api.z.ai/v1",
                api_mode="chat_completions",
            )
        assert agent._fallback_activated is False
        assert agent._fallback_reason is None

    def test_transport_recovery_reset_clears_reason(self):
        """The transport-recovery reset lives inline in run_conversation's
        retry loop; drive retry exhaustion into a (mocked) successful
        primary-transport recovery and assert the reset clears
        _fallback_reason alongside _fallback_activated.  The cleared reason
        is stale pre-recovery bookkeeping — _try_recover_primary_transport
        refuses while a fallback is actually live — so seed the stale shape:
        a burned chain index with a recorded reason but no activation."""

        class ReadTimeout(Exception):
            pass

        agent = _make_agent(fallback_model=[_FB])
        agent._api_max_retries = 2
        agent._fallback_index = len(agent._fallback_chain)
        agent._fallback_reason = "content_policy_blocked"

        responses = [
            ReadTimeout("read timed out"),
            ReadTimeout("read timed out"),
            _loop_response("Recovered on rebuilt primary."),
        ]
        with (
            patch.object(agent, "_interruptible_api_call", side_effect=responses),
            patch.object(agent, "_try_recover_primary_transport", return_value=True),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        assert result["final_response"] == "Recovered on rebuilt primary."
        assert agent._fallback_activated is False
        assert agent._fallback_reason is None

    def test_runtime_snapshot_roundtrip_carries_reason(self):
        """snapshot_runtime/restore_runtime must copy the reason with the flag."""
        from agent.runtime_control import restore_runtime, snapshot_runtime

        agent = _make_agent(fallback_model=[_FB])
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            agent._try_activate_fallback(reason=FailoverReason.content_policy_blocked)
        snapshot = snapshot_runtime(agent)
        assert snapshot["fallback_reason"] == "content_policy_blocked"

        agent._fallback_reason = None
        agent._fallback_activated = False
        with patch("run_agent.OpenAI", return_value=MagicMock()):
            restore_runtime(agent, snapshot)
        assert agent._fallback_activated is True
        assert agent._fallback_reason == "content_policy_blocked"


# ── Conversation-loop call sites forward the reason ───────────────────────


class TestLoopCallSitesForwardReason:
    """Drive run_conversation() through each refusal branch and assert the
    call site forwards the reason into ``_try_activate_fallback()``.  This
    is the linchpin wiring for skill-gate's refusal-fallback exemption: if
    a rebase or refactor drops the kwarg, the reason stays None and the
    exemption goes permanently dead with no other test failing."""

    def _run(self, agent, responses):
        """Run one turn with ``_interruptible_api_call`` staged and a spy
        wrapping the real ``_try_activate_fallback`` (activation succeeds
        via the patched provider resolver)."""
        real_activate = agent._try_activate_fallback
        with (
            patch.object(agent, "_interruptible_api_call", side_effect=responses),
            patch.object(agent, "_try_activate_fallback",
                         side_effect=real_activate) as spy,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch("agent.auxiliary_client.resolve_provider_client",
                  return_value=(_mock_client(), "gpt-4o")),
            patch("agent.model_metadata.get_model_context_length",
                  return_value=200000),
        ):
            result = agent.run_conversation("hello")
        return result, spy

    def test_http200_content_filter_branch_forwards_reason(self):
        """HTTP-200 refusal (finish_reason="content_filter") must activate
        the fallback with reason=content_policy_blocked."""
        agent = _make_agent(fallback_model=[_FB])
        refusal = _loop_response(content="", finish_reason="content_filter")
        result, spy = self._run(agent, [refusal, _loop_response()])
        assert result["final_response"] == "Recovered on fallback."
        assert spy.call_count == 1
        assert spy.call_args.kwargs["reason"] is FailoverReason.content_policy_blocked
        assert agent._fallback_reason == "content_policy_blocked"

    def test_midstream_content_filter_stub_forwards_reason(self):
        """A content-filter-terminated partial-stream stub (#32421) must
        activate the fallback with reason=content_policy_blocked."""
        agent = _make_agent(fallback_model=[_FB])
        stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=SimpleNamespace(content="partial ", tool_calls=None),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
            _content_filter_terminated=True,
        )
        result, spy = self._run(agent, [stub, _loop_response()])
        assert result["final_response"] == "Recovered on fallback."
        assert spy.call_count == 1
        assert spy.call_args.kwargs["reason"] is FailoverReason.content_policy_blocked
        assert agent._fallback_reason == "content_policy_blocked"

    def test_nonretryable_refusal_exception_forwards_classified_reason(self):
        """A status-less safety refusal raised as an exception (#18028
        shape) must reach the is_client_error branch and forward
        classified.reason into the activation."""
        agent = _make_agent(fallback_model=[_FB])
        refusal_exc = Exception(
            "This content was flagged for possible cybersecurity risk."
        )
        result, spy = self._run(agent, [refusal_exc, _loop_response()])
        assert result["final_response"] == "Recovered on fallback."
        assert spy.call_count == 1
        assert spy.call_args.kwargs["reason"] is FailoverReason.content_policy_blocked
        assert agent._fallback_reason == "content_policy_blocked"
