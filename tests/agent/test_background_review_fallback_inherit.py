"""Self-contained regressions for background-review fork result handling.

The extracted checkout lacks the full Hermes package, so this module loads
``background_review.py`` directly and supplies only the imports needed to
exercise the fork constructor.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


_BACKGROUND_REVIEW = (
    Path(__file__).resolve().parents[2] / "agent" / "background_review.py"
)


@pytest.fixture
def background_review(monkeypatch):
    """Load the production module with the unavailable package imports stubbed."""
    agent_package = types.ModuleType("agent")
    agent_package.__path__ = []
    output_module = types.ModuleType("agent.thread_scoped_output")
    output_module.thread_scoped_silence = nullcontext
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.thread_scoped_output", output_module)

    captured = []

    class CapturingAIAgent:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.model = kwargs["model"]
            self.provider = kwargs["provider"]
            self.base_url = kwargs["base_url"]
            self.session_input_tokens = 0
            self.session_output_tokens = 0
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.session_reasoning_tokens = 0
            self.session_api_calls = 1
            self.session_estimated_cost_usd = None
            self._session_messages = []

        def run_conversation(self, **_kwargs):
            pass

        def shutdown_memory_provider(self):
            pass

        def close(self):
            pass

    run_agent_module = types.ModuleType("run_agent")
    run_agent_module.AIAgent = CapturingAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", run_agent_module)

    tools_package = types.ModuleType("tools")
    tools_package.__path__ = []
    terminal_module = types.ModuleType("tools.terminal_tool")
    terminal_module.set_approval_callback = lambda _callback: None
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", terminal_module)

    model_tools_module = types.ModuleType("model_tools")
    model_tools_module.get_tool_definitions = lambda **_kwargs: []
    monkeypatch.setitem(sys.modules, "model_tools", model_tools_module)

    hermes_cli_package = types.ModuleType("hermes_cli")
    hermes_cli_package.__path__ = []
    plugins_module = types.ModuleType("hermes_cli.plugins")
    plugins_module.set_thread_tool_whitelist = lambda *_args, **_kwargs: None
    plugins_module.clear_thread_tool_whitelist = lambda: None
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_module)

    spec = importlib.util.spec_from_file_location(
        "background_review_under_test", _BACKGROUND_REVIEW
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, captured


def _parent_agent(fallback_chain):
    return SimpleNamespace(
        model="parent-model",
        provider="parent-provider",
        platform="test",
        session_id="parent-session",
        session_start="parent-start",
        reasoning_config=None,
        ephemeral_system_prompt=None,
        prefill_messages=[],
        enabled_toolsets=None,
        disabled_toolsets=None,
        _fallback_chain=fallback_chain,
        _memory_store=None,
        _memory_enabled=False,
        _user_profile_enabled=False,
        _cached_system_prompt="cached parent prompt",
        background_review_callback=None,
        _safe_print=lambda _message: None,
        _emit_auxiliary_failure=lambda _name, error: (_ for _ in ()).throw(error),
    )


def _run_fork(background_review, captured, *, routed, fallback_chain):
    module = background_review
    module._resolve_review_runtime = lambda _agent, task_cfg: {
        "model": "routed-model" if routed else "parent-model",
        "provider": "routed-provider" if routed else "parent-provider",
        "api_mode": None,
        "base_url": None,
        "api_key": None,
        "credential_pool": None,
        "request_overrides": {},
        "routed": routed,
    }

    assert module._run_review_in_thread(
        _parent_agent(fallback_chain), [], "Review the conversation."
    )
    return captured[-1]


def test_same_model_fork_inherits_parent_fallback_chain(background_review):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]

    kwargs = _run_fork(
        module, captured, routed=False, fallback_chain=fallback_chain
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_fork_does_not_inherit_parent_fallback_chain(background_review):
    module, captured = background_review

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=[{"model": "backup-model", "provider": "backup-provider"}],
    )

    assert kwargs["fallback_model"] is None


def test_empty_parent_fallback_chain_becomes_none(background_review):
    module, captured = background_review

    kwargs = _run_fork(module, captured, routed=False, fallback_chain=[])

    assert kwargs["fallback_model"] is None


def test_classify_review_result_distinguishes_transport_failure(background_review):
    module, _ = background_review

    assert module._classify_review_result([], usage={"api_calls": 0}) == "error"
    assert module._classify_review_result([], usage={"api_calls": 3}) == "none"
    assert (
        module._classify_review_result(
            ["Skill foo saved"], usage={"api_calls": 0}
        )
        == "skill"
    )
    assert module._classify_review_result([]) == "none"
