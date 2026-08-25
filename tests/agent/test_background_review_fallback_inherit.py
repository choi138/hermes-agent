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
        base_url="https://parent.example/v1",
        api_key="parent-test-key",
        _client_kwargs={},
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


def _runtime_agent(provider, model, primary_runtime=None, *, has_primary=True):
    agent = SimpleNamespace(
        provider=provider,
        model=model,
        _credential_pool=None,
        request_overrides={},
        max_tokens=None,
        acp_command=None,
        acp_args=[],
        _current_main_runtime=lambda: {},
    )
    if has_primary:
        agent._primary_runtime = primary_runtime
    return agent


class _EqualTokenProvider:
    def __call__(self):
        return None

    def __eq__(self, other):
        return isinstance(other, _EqualTokenProvider)


def test_resolve_review_runtime_same_primary_model_is_not_routed(background_review):
    module, _ = background_review

    runtime = module._resolve_review_runtime(
        _runtime_agent("claude-nekos", "primary-model"),
        {"provider": "claude-nekos", "model": "primary-model"},
    )

    assert runtime["routed"] is False


def test_resolve_review_runtime_fallback_uses_primary_model(background_review):
    module, _ = background_review

    runtime = module._resolve_review_runtime(
        _runtime_agent(
            "anthropic",
            "fallback-model",
            {"provider": "claude-nekos", "model": "primary-model"},
        ),
        {"provider": "claude-nekos", "model": "primary-model"},
    )

    assert runtime["routed"] is False


def test_resolve_review_runtime_different_primary_model_is_routed(
    background_review, monkeypatch
):
    module, _ = background_review
    runtime_provider_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_provider_module.resolve_runtime_provider = lambda **kwargs: {
        "provider": kwargs["requested"],
        "model": kwargs["target_model"],
    }
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", runtime_provider_module)

    runtime = module._resolve_review_runtime(
        _runtime_agent(
            "anthropic",
            "fallback-model",
            {"provider": "claude-nekos", "model": "primary-model"},
        ),
        {"provider": "other-provider", "model": "other-model"},
    )

    assert runtime["routed"] is True


def test_resolve_review_runtime_without_primary_runtime_uses_live_runtime(
    background_review,
):
    module, _ = background_review

    runtime = module._resolve_review_runtime(
        _runtime_agent(
            "claude-nekos",
            "primary-model",
            has_primary=False,
        ),
        {"provider": "claude-nekos", "model": "primary-model"},
    )

    assert runtime["routed"] is False


def _run_fork(
    background_review,
    captured,
    *,
    routed,
    fallback_chain,
    fork_model=None,
    fork_provider=None,
    fork_base_url="https://parent.example/v1",
    fork_api_key="parent-test-key",
    parent_agent=None,
):
    module = background_review
    module._resolve_review_runtime = lambda _agent, task_cfg: {
        "model": fork_model or ("routed-model" if routed else "parent-model"),
        "provider": fork_provider
        or ("routed-provider" if routed else "parent-provider"),
        "api_mode": None,
        "base_url": fork_base_url,
        "api_key": fork_api_key,
        "credential_pool": None,
        "request_overrides": {},
        "routed": routed,
    }

    assert module._run_review_in_thread(
        parent_agent or _parent_agent(fallback_chain),
        [],
        "Review the conversation.",
    )
    return captured[-1]


def test_non_routed_fork_inherits_parent_fallback_chain_regardless_of_route(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]

    kwargs = _run_fork(
        module,
        captured,
        routed=False,
        fallback_chain=fallback_chain,
        fork_provider="isolated-provider",
        fork_base_url="https://isolated.example/v1",
        fork_api_key="isolated-test-key",
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_model_only_fork_inherits_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.model = "claude-sonnet-4"
    parent.provider = "claude-nekos"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_model="claude-sonnet-5",
        fork_provider="claude-nekos",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_namespaced_primary_provider_matches_resolved_family(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.provider = "custom:minimax"
    parent._primary_runtime = {
        "provider": "custom:minimax",
        "model": "parent-model",
        "base_url": "https://custom.example/v1",
        "api_key": "custom-test-key",
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="custom",
        fork_base_url="https://custom.example/v1",
        fork_api_key="custom-test-key",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_same_provider_different_base_url_withholds_fallback_chain(
    background_review,
):
    module, captured = background_review

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=[{"model": "backup-model", "provider": "backup-provider"}],
        fork_provider="parent-provider",
        fork_base_url="https://isolated.example/v1",
    )

    assert kwargs["fallback_model"] is None


def test_routed_azure_query_split_inherits_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://foo.azure.example/openai"
    parent._primary_runtime = {
        "provider": "parent-provider",
        "model": "parent-model",
        "base_url": "https://foo.azure.example/openai",
        "api_key": "parent-test-key",
        "client_kwargs": {
            "default_query": {"api-version": "2024-06-01"},
        },
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url=(
            "https://foo.azure.example/openai?api-version=2024-06-01"
        ),
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_azure_query_mismatch_withholds_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://foo.azure.example/openai"
    parent._primary_runtime = {
        "provider": "parent-provider",
        "model": "parent-model",
        "base_url": "https://foo.azure.example/openai",
        "api_key": "parent-test-key",
        "client_kwargs": {
            "default_query": {"api-version": "2024-06-01"},
        },
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url=(
            "https://foo.azure.example/openai?api-version=2024-10-21"
        ),
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_default_query_list_does_not_match_bare_repeated_fork_query(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent._client_kwargs = {
        "default_query": {"scope": ["a", "b"]},
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1?scope=a&scope=b",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_default_query_list_matches_bracketed_repeated_fork_query(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent._client_kwargs = {
        "default_query": {"scope": ["a", "b"]},
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1?scope[]=a&scope[]=b",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_repeated_query_value_sets_match(background_review):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://parent.example/v1?scope=a&scope=b"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1?scope=b&scope=a",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_repeated_query_value_set_mismatch_withholds_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://parent.example/v1?scope=a&scope=b"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1?scope=a&scope=c",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_different_path_params_withholds_fallback_chain(background_review):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://parent.example/v1;tenant=A"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1;tenant=B",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_same_path_params_inherits_parent_fallback_chain(background_review):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://parent.example/v1;tenant=A"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1;tenant=A",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_path_param_slash_difference_withholds_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.base_url = "https://parent.example/v1;tenant=A"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1/;tenant=A",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_trailing_slash_difference_inherits_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_base_url="https://parent.example/v1/",
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_same_provider_different_api_key_withholds_fallback_chain(
    background_review,
):
    module, captured = background_review

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=[{"model": "backup-model", "provider": "backup-provider"}],
        fork_provider="parent-provider",
        fork_api_key="isolated-test-key",
    )

    assert kwargs["fallback_model"] is None


def test_routed_same_callable_api_key_inherits_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    token_provider = _EqualTokenProvider()
    parent = _parent_agent(fallback_chain)
    parent.api_key = token_provider

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_api_key=token_provider,
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


def test_routed_different_callable_api_keys_withhold_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.api_key = _EqualTokenProvider()

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-provider",
        fork_api_key=_EqualTokenProvider(),
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_routed_different_provider_family_withholds_parent_fallback_chain(
    background_review,
):
    module, captured = background_review
    parent = _parent_agent(
        [{"model": "backup-model", "provider": "backup-provider"}]
    )
    parent.provider = "claude-nekos"

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=parent._fallback_chain,
        fork_provider="anthropic",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is None


def test_empty_parent_fallback_chain_becomes_none(background_review):
    module, captured = background_review

    kwargs = _run_fork(module, captured, routed=False, fallback_chain=[])

    assert kwargs["fallback_model"] is None


@pytest.mark.parametrize("fork_provider", ["parent-provider", "routed-provider"])
def test_empty_parent_fallback_chain_becomes_none_when_routed(
    background_review, fork_provider
):
    module, captured = background_review

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=[],
        fork_provider=fork_provider,
    )

    assert kwargs["fallback_model"] is None


def test_routed_fork_compares_against_parent_primary_provider(background_review):
    module, captured = background_review
    fallback_chain = [{"model": "backup-model", "provider": "backup-provider"}]
    parent = _parent_agent(fallback_chain)
    parent.provider = "live-fallback-provider"
    parent._primary_runtime = {
        "provider": "parent-primary-provider",
        "model": "parent-model",
    }

    kwargs = _run_fork(
        module,
        captured,
        routed=True,
        fallback_chain=fallback_chain,
        fork_provider="parent-primary-provider",
        parent_agent=parent,
    )

    assert kwargs["fallback_model"] is fallback_chain


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
