"""Tests for ordered provider fallback chain (salvage of PR #1761).

Extends the single-fallback tests in test_fallback_model.py to cover
the new list-based ``fallback_providers`` config format and chain
advancement through multiple providers.
"""

import logging
from unittest.mock import MagicMock, patch

import httpx

from agent.error_classifier import FailoverReason
from agent.failover_domain import (
    INFRASTRUCTURE_FAILOVER_REASONS,
    endpoint_origin,
    same_failure_domain,
)
from run_agent import AIAgent, _pool_may_recover_from_rate_limit


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


def _mock_client(base_url="https://openrouter.ai/api/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


# ── Chain initialisation ──────────────────────────────────────────────────


class TestFallbackChainInit:
    def test_no_fallback(self):
        agent = _make_agent(fallback_model=None)
        assert agent._fallback_chain == []
        assert agent._fallback_index == 0
        assert agent._fallback_model is None

    def test_single_dict_backwards_compat(self):
        fb = {"provider": "openai", "model": "gpt-4o"}
        agent = _make_agent(fallback_model=fb)
        assert agent._fallback_chain == [fb]
        assert agent._fallback_model == fb

    def test_list_of_providers(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        assert len(agent._fallback_chain) == 2
        assert agent._fallback_model == fbs[0]

    def test_invalid_entries_filtered(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "", "model": "glm-4.7"},
            {"provider": "zai"},
            "not-a-dict",
        ]
        agent = _make_agent(fallback_model=fbs)
        assert len(agent._fallback_chain) == 1
        assert agent._fallback_chain[0]["provider"] == "openai"

    def test_empty_list(self):
        agent = _make_agent(fallback_model=[])
        assert agent._fallback_chain == []
        assert agent._fallback_model is None

    def test_invalid_dict_no_provider(self):
        agent = _make_agent(fallback_model={"model": "gpt-4o"})
        assert agent._fallback_chain == []


# ── Chain advancement ─────────────────────────────────────────────────────


class TestFallbackChainAdvancement:
    def test_exhausted_returns_false(self):
        agent = _make_agent(fallback_model=None)
        assert agent._try_activate_fallback() is False

    def test_advances_index(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            assert agent._try_activate_fallback() is True
            assert agent._fallback_index == 1
            assert agent.model == "gpt-4o"
            assert agent._fallback_activated is True

    def test_second_fallback_works(self):
        fbs = [
            {"provider": "openai", "model": "gpt-4o"},
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "resolved")):
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"
            assert agent._try_activate_fallback() is True
            assert agent.model == "glm-4.7"
            assert agent._fallback_index == 2

    def test_all_exhausted_returns_false(self):
        fbs = [{"provider": "openai", "model": "gpt-4o"}]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client",
                    return_value=(_mock_client(), "gpt-4o")):
            assert agent._try_activate_fallback() is True
            assert agent._try_activate_fallback() is False

    def test_skips_unconfigured_provider_to_next(self):
        """If resolve_provider_client returns None, skip to next in chain."""
        fbs = [
            {"provider": "broken", "model": "nope"},
            {"provider": "openai", "model": "gpt-4o"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client") as mock_rpc:
            mock_rpc.side_effect = [
                (None, None),                    # broken provider
                (_mock_client(), "gpt-4o"),       # fallback succeeds
            ]
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"
            assert agent._fallback_index == 2

    def test_skips_provider_that_raises_to_next(self):
        """If resolve_provider_client raises, skip to next in chain."""
        fbs = [
            {"provider": "broken", "model": "nope"},
            {"provider": "openai", "model": "gpt-4o"},
        ]
        agent = _make_agent(fallback_model=fbs)
        with patch("agent.auxiliary_client.resolve_provider_client") as mock_rpc:
            mock_rpc.side_effect = [
                RuntimeError("auth failed"),
                (_mock_client(), "gpt-4o"),
            ]
            assert agent._try_activate_fallback() is True
            assert agent.model == "gpt-4o"

    def test_resolves_key_env_for_fallback_provider(self):
        fbs = [
            {
                "provider": "custom",
                "model": "fallback-model",
                "base_url": "https://fallback.example/v1",
                "key_env": "MY_FALLBACK_KEY",
            }
        ]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch.dict("os.environ", {"MY_FALLBACK_KEY": "env-secret"}, clear=False),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _mock_client(
                        base_url="https://fallback.example/v1",
                        api_key="env-secret",
                    ),
                    "fallback-model",
                ),
            ) as mock_rpc,
        ):
            assert agent._try_activate_fallback() is True
            assert mock_rpc.call_args.kwargs["explicit_api_key"] == "env-secret"

    def test_anthropic_host_custom_provider_uses_anthropic_messages(self):
        """A custom provider on the native api.anthropic.com host (no
        "/anthropic" path suffix, name != "anthropic") must resolve to the
        anthropic_messages wire protocol — not default to chat_completions,
        which POSTs /v1/chat/completions and 404s. Mirrors the primary-path
        determine_api_mode() host check."""
        fbs = [
            {
                "provider": "cron-anthropic",
                "model": "claude-sonnet-4-6",
                "base_url": "https://api.anthropic.com",
                "key_env": "MY_FALLBACK_KEY",
            }
        ]
        agent = _make_agent(fallback_model=fbs)
        with (
            patch.dict("os.environ", {"MY_FALLBACK_KEY": "env-secret"}, clear=False),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(
                    _mock_client(base_url="https://api.anthropic.com"),
                    "claude-sonnet-4-6",
                ),
            ),
            patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        ):
            assert agent._try_activate_fallback() is True
            assert agent.api_mode == "anthropic_messages"


# ── Pool-rotation vs fallback gating (#11314) ────────────────────────────


def _pool(n_entries: int, has_available: bool = True):
    """Make a minimal credential-pool stand-in for rotation-room checks."""
    pool = MagicMock()
    pool.entries.return_value = [MagicMock() for _ in range(n_entries)]
    pool.has_available.return_value = has_available
    return pool


class TestPoolRotationRoom:
    def test_none_pool_returns_false(self):
        assert _pool_may_recover_from_rate_limit(None) is False

    def test_single_credential_returns_false(self):
        """With one credential that just 429'd, rotation has nowhere to go.

        The pool may still report has_available() True once cooldown expires,
        but retrying against the same entry will hit the same daily-quota
        429 and burn the retry budget.  Must fall back.
        """
        assert _pool_may_recover_from_rate_limit(_pool(1)) is False

    def test_single_credential_in_cooldown_returns_false(self):
        assert _pool_may_recover_from_rate_limit(_pool(1, has_available=False)) is False

    def test_two_credentials_available_returns_true(self):
        """With >1 credentials and at least one available, rotate instead of fallback."""
        assert _pool_may_recover_from_rate_limit(_pool(2)) is True

    def test_multiple_credentials_all_in_cooldown_returns_false(self):
        """All credentials cooling down — fall back rather than wait."""
        assert _pool_may_recover_from_rate_limit(_pool(3, has_available=False)) is False

    def test_many_credentials_available_returns_true(self):
        assert _pool_may_recover_from_rate_limit(_pool(10)) is True


# ── Skip-self dedup (#22548) ───────────────────────────────────────────────


class TestFallbackChainDedup:
    """A fallback chain entry that resolves to the current provider/model
    (or the same custom-provider base_url) must be skipped, not retried.
    Otherwise a misconfigured chain or two custom_providers entries pointing
    at the same shim loop the same failure. See issue #22548."""

    def test_skips_entry_matching_current_provider_and_model(self):
        """Chain has [same-as-current, real-fallback]; activate must skip
        the first and use the second."""
        fbs = [
            # First entry == current state. Should be skipped.
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
            # Second entry: real fallback.
            {"provider": "zai", "model": "glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "z-ai/glm-4.7"
        agent.base_url = "https://openrouter.ai/api/v1"

        # Stub out resolve_provider_client so we can assert which entry was
        # actually used — return a MagicMock client tagged with the provider.
        called = []
        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            return _mock_client(), model
        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve):
            with patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m):
                ok = agent._try_activate_fallback()

        assert ok is True
        # The first entry was skipped — only the second reached resolve.
        assert called == [("zai", "glm-4.7")], (
            f"expected fallback to skip same-state entry, got call order: {called}"
        )

    def test_skips_entry_matching_current_base_url_and_model(self):
        """Two custom_providers entries pointing at the same shim URL
        with the same model should dedup even if their provider names differ."""
        fbs = [
            # Different provider name but same shim URL + model — same backend.
            {"provider": "claude-cli-alt", "model": "claude-opus-4.7",
             "base_url": "http://127.0.0.1:7891/v1"},
            # Real different fallback.
            {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "claude-cli"
        agent.model = "claude-opus-4.7"
        agent.base_url = "http://127.0.0.1:7891/v1"

        called = []
        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            return _mock_client(), model
        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve):
            with patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m):
                ok = agent._try_activate_fallback()

        assert ok is True
        # Same shim/base_url+model entry skipped, second one used.
        assert called == [("openrouter", "anthropic/claude-opus-4.7")], (
            f"expected base_url-aware dedup, got call order: {called}"
        )

    def test_returns_false_when_only_self_matching_entries(self):
        """A chain with only self-matching entries exhausts to False."""
        fbs = [
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ]
        agent = _make_agent(fallback_model=fbs)
        agent.provider = "openrouter"
        agent.model = "z-ai/glm-4.7"
        agent.base_url = "https://openrouter.ai/api/v1"

        with patch("agent.auxiliary_client.resolve_provider_client") as mock_resolve:
            ok = agent._try_activate_fallback()

        assert ok is False
        mock_resolve.assert_not_called()


# ── Same-failure-domain skip on infrastructure failures ───────────────────


class TestFallbackSameEndpointDomain:
    """The provider/model names differed and no fallback ``base_url`` was
    configured, so the config-level dedup above could not see that the
    fallback RESOLVED to the very endpoint that just failed
    (``http://127.0.0.1:2455/v1``). Activating it re-entered the same dead
    capacity pool. For infrastructure failures the resolved endpoint origin
    (scheme + host + effective port) must be compared AFTER
    resolve_provider_client returns and BEFORE any agent state is mutated."""

    def _incident_agent(self, chain):
        agent = _make_agent(fallback_model=chain)
        agent.provider = "custom"
        agent.model = "gpt-5.6-sol"
        agent.base_url = "http://127.0.0.1:2455/v1"
        return agent

    def test_skips_same_endpoint_candidate_on_server_error(self):
        """custom/gpt-5.6-sol → codex-lb/gpt-5.5 resolves to the SAME
        origin, so the chain must advance to the independent provider."""
        fbs = [
            # Different provider AND model, no explicit base_url — only the
            # resolver knows it lands on 127.0.0.1:2455.
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ]
        agent = self._incident_agent(fbs)

        called = []

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            called.append((provider, model))
            if provider == "codex-lb":
                return _mock_client(base_url="http://127.0.0.1:2455/v1"), model
            return _mock_client(base_url="https://openrouter.ai/api/v1"), model

        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
            patch("hermes_cli.model_normalize.normalize_model_for_provider",
                  side_effect=lambda m, p: m),
        ):
            ok = agent._try_activate_fallback(reason=FailoverReason.server_error)

        assert ok is True
        # Both entries were resolved (the first has to be built before its
        # real endpoint is knowable), but only the second was activated.
        assert called == [("codex-lb", "gpt-5.5"), ("openrouter", "z-ai/glm-4.7")]
        assert agent.provider == "openrouter"
        assert agent.model == "z-ai/glm-4.7"
        assert agent.base_url == "https://openrouter.ai/api/v1"

    def _activate(self, agent, reason, candidates):
        """Resolve each chain entry to ``candidates[provider]`` and activate."""
        clients = {}

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            clients[provider] = _mock_client(base_url=candidates[provider])
            return clients[provider], model

        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
            patch("hermes_cli.model_normalize.normalize_model_for_provider",
                  side_effect=lambda m, p: m),
        ):
            ok = agent._try_activate_fallback(reason=reason)
        return ok, clients

    def test_skips_same_endpoint_on_timeout(self):
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        ok, _ = self._activate(agent, FailoverReason.timeout, {
            "codex-lb": "http://127.0.0.1:2455/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")

    def test_skips_same_endpoint_on_overloaded(self):
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        ok, _ = self._activate(agent, FailoverReason.overloaded, {
            "codex-lb": "http://127.0.0.1:2455/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")

    def test_normalization_matches_across_case_default_port_and_path(self):
        """Same pool written differently: uppercase host, implicit :443,
        different path, query string, fragment, trailing slash."""
        agent = self._incident_agent([
            {"provider": "gw-alias", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        # This test is about origin spelling, not cross-model capacity. Keep
        # the model identical so the public-host guard must reject the replay.
        agent.model = "gpt-5.5"
        agent.base_url = "https://Gateway.Example.COM/v1/"
        ok, _ = self._activate(agent, FailoverReason.server_error, {
            "gw-alias": "https://gateway.example.com:443/v2/chat?key=secret#frag",
            "openrouter": "https://openrouter.ai/api/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")

    def test_different_port_is_a_different_domain(self):
        """A neighbouring port is a separate pool — must NOT be skipped."""
        agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
        ok, clients = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2456/v1",
        })
        assert ok is True
        assert agent.base_url == "http://127.0.0.1:2456/v1"
        clients["codex-lb"].close.assert_not_called()

    def test_same_endpoint_allowed_for_model_not_found(self):
        """A model-specific failure is recoverable by a different model on
        the same endpoint — the domain guard must not fire."""
        agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
        ok, clients = self._activate(agent, FailoverReason.model_not_found, {
            "codex-lb": "http://127.0.0.1:2455/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("codex-lb", "gpt-5.5")
        assert agent.base_url == "http://127.0.0.1:2455/v1"
        clients["codex-lb"].close.assert_not_called()

    def test_same_endpoint_allowed_for_content_policy_blocked(self):
        agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
        ok, _ = self._activate(agent, FailoverReason.content_policy_blocked, {
            "codex-lb": "http://127.0.0.1:2455/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("codex-lb", "gpt-5.5")

    def test_public_same_origin_different_model_allowed_for_infrastructure_failure(self):
        """A public provider origin can host independent per-model capacity.

        The incident guard must still reject aliases of the same private shim,
        but it cannot turn a timeout/overload on one public model into a ban on
        every other model served by that origin.
        """
        for reason in (
            FailoverReason.timeout,
            FailoverReason.overloaded,
            FailoverReason.server_error,
        ):
            agent = _make_agent(
                fallback_model=[{"provider": "openai", "model": "gpt-5-mini"}]
            )
            agent.provider = "openai"
            agent.model = "gpt-5.5"
            agent.base_url = "https://api.openai.com/v1"

            ok, clients = self._activate(agent, reason, {
                "openai": "https://api.openai.com/v1",
            })

            assert ok is True, f"different public model blocked for {reason.value}"
            assert (agent.provider, agent.model) == ("openai", "gpt-5-mini")
            clients["openai"].close.assert_not_called()

    def test_public_same_origin_same_model_still_skipped(self):
        """A provider alias must not replay the identical public model."""
        agent = _make_agent(
            fallback_model=[{"provider": "openai-alias", "model": "gpt-5.5"}]
        )
        agent.provider = "openai"
        agent.model = "gpt-5.5"
        agent.base_url = "https://api.openai.com/v1"

        ok, clients = self._activate(agent, FailoverReason.timeout, {
            "openai-alias": "https://api.openai.com/v1",
        })

        assert ok is False
        assert (agent.provider, agent.model) == ("openai", "gpt-5.5")
        assert clients["openai-alias"].close.call_count == 1

    def test_skipped_candidate_client_closed_exactly_once(self):
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        ok, clients = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2455/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        })
        assert ok is True
        assert clients["codex-lb"].close.call_count == 1
        # The activated client stays open — it is now the live client.
        clients["openrouter"].close.assert_not_called()

    def test_same_live_client_object_is_never_closed_by_domain_skip(self):
        """Resolver aliasing must not let rejection close the active client."""
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
        ])
        live_client = agent.client
        live_client.base_url = "http://127.0.0.1:2455/v1"

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(live_client, "gpt-5.5"),
        ):
            ok = agent._try_activate_fallback(reason=FailoverReason.timeout)

        assert ok is False
        live_client.close.assert_not_called()
        assert agent.client is live_client

    def test_all_same_domain_exhausts_cleanly_without_mutating_state(self):
        """Every candidate is the failing endpoint under another name:
        return False, leave the runtime untouched, no recursion blow-up."""
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "codex-lb-2", "model": "gpt-5.4"},
        ])
        live_client = agent.client
        ok, clients = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2455/v1",
            "codex-lb-2": "http://127.0.0.1:2455/v1/",
        })
        assert ok is False
        assert agent.provider == "custom"
        assert agent.model == "gpt-5.6-sol"
        assert agent.base_url == "http://127.0.0.1:2455/v1"
        assert agent.client is live_client
        assert getattr(agent, "_fallback_activated", False) is False
        assert all(c.close.call_count == 1 for c in clients.values())

    def test_domain_skip_does_not_mark_entry_globally_unavailable(self):
        """The entry is unusable for THIS reason only — it must stay a valid
        target later (e.g. after the primary moves off that endpoint)."""
        fbs = [{"provider": "codex-lb", "model": "gpt-5.5"}]
        agent = self._incident_agent(fbs)
        ok, _ = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2455/v1",
        })
        assert ok is False
        assert not getattr(agent, "_unavailable_fallback_keys", set())

        # Replay the chain from a primary on a different endpoint: the same
        # entry now activates normally.
        agent._fallback_index = 0
        agent.base_url = "https://api.other.example/v1"
        ok, clients = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2455/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("codex-lb", "gpt-5.5")
        clients["codex-lb"].close.assert_not_called()

    def test_unknown_current_origin_fails_open(self):
        """With no usable current base_url the guard cannot compare origins;
        it must not block an otherwise valid fallback."""
        agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
        agent.base_url = ""
        ok, _ = self._activate(agent, FailoverReason.server_error, {
            "codex-lb": "http://127.0.0.1:2455/v1",
        })
        assert ok is True
        assert (agent.provider, agent.model) == ("codex-lb", "gpt-5.5")

    def test_httpx_url_candidate_base_url_skipped_on_timeout(self):
        """A real OpenAI/httpx client exposes ``base_url`` as an
        ``httpx.URL``, not a ``str``.  The guard must canonicalize the object
        itself — if it only handled strings, the same-domain candidate would
        activate and re-enter the pool that just timed out."""
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        clients = {}

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            base_url = (
                httpx.URL("http://127.0.0.1:2455/v1")
                if provider == "codex-lb"
                else httpx.URL("https://openrouter.ai/api/v1")
            )
            assert isinstance(base_url, httpx.URL)
            clients[provider] = _mock_client(base_url=base_url)
            return clients[provider], model

        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
            patch("hermes_cli.model_normalize.normalize_model_for_provider",
                  side_effect=lambda m, p: m),
        ):
            ok = agent._try_activate_fallback(reason=FailoverReason.timeout)

        assert ok is True
        # Same-domain candidate skipped and released; the different origin
        # activated and stays open as the live client.
        assert clients["codex-lb"].close.call_count == 1
        clients["openrouter"].close.assert_not_called()
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")
        assert agent.base_url == "https://openrouter.ai/api/v1"

    def test_non_enum_and_missing_reason_fail_open(self):
        """The guard keys off ``FailoverReason`` membership.  Production
        callers pass an enum member or ``None``; a bare string is deliberately
        NOT coerced into an enum, so both ``None`` and ``"timeout"`` fail open
        and the same-origin candidate activates."""
        assert "timeout" not in INFRASTRUCTURE_FAILOVER_REASONS
        assert None not in INFRASTRUCTURE_FAILOVER_REASONS

        for reason in (None, "timeout"):
            agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
            ok, clients = self._activate(agent, reason, {
                "codex-lb": "http://127.0.0.1:2455/v1",
            })
            assert ok is True, f"reason={reason!r} must fail open"
            assert (agent.provider, agent.model) == ("codex-lb", "gpt-5.5")
            assert agent.base_url == "http://127.0.0.1:2455/v1"
            clients["codex-lb"].close.assert_not_called()

    def test_candidate_close_failure_does_not_abort_the_walk(self):
        """``_close_unused_fallback_client`` swallows a failing ``close()``:
        releasing the rejected candidate is best-effort, and must never stop
        the chain from advancing to the next entry."""
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        clients = {}

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            client = _mock_client(base_url=(
                "http://127.0.0.1:2455/v1" if provider == "codex-lb"
                else "https://openrouter.ai/api/v1"
            ))
            if provider == "codex-lb":
                client.close.side_effect = RuntimeError("transport already gone")
            clients[provider] = client
            return client, model

        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
            patch("hermes_cli.model_normalize.normalize_model_for_provider",
                  side_effect=lambda m, p: m),
        ):
            ok = agent._try_activate_fallback(reason=FailoverReason.server_error)

        assert ok is True
        assert clients["codex-lb"].close.call_count == 1
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")
        assert agent.base_url == "https://openrouter.ai/api/v1"

    def test_non_closeable_candidate_is_skipped(self):
        """The router returns whatever the provider needs (SDK client, adapter
        shim) and not every shim exposes ``close()``.  A candidate without one
        must still be skipped rather than raising AttributeError."""
        agent = self._incident_agent([
            {"provider": "codex-lb", "model": "gpt-5.5"},
            {"provider": "openrouter", "model": "z-ai/glm-4.7"},
        ])
        clients = {}

        def _resolve(provider, model=None, raw_codex=False, **kwargs):
            if provider == "codex-lb":
                # Spec-limited: no ``close`` attribute at all.
                shim = MagicMock(spec=["base_url"])
                shim.base_url = "http://127.0.0.1:2455/v1"
                assert not hasattr(shim, "close")
                clients[provider] = shim
                return shim, model
            clients[provider] = _mock_client(base_url="https://openrouter.ai/api/v1")
            return clients[provider], model

        with (
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve),
            patch("hermes_cli.model_normalize.normalize_model_for_provider",
                  side_effect=lambda m, p: m),
        ):
            ok = agent._try_activate_fallback(reason=FailoverReason.overloaded)

        assert ok is True
        assert (agent.provider, agent.model) == ("openrouter", "z-ai/glm-4.7")
        assert agent.base_url == "https://openrouter.ai/api/v1"
        clients["openrouter"].close.assert_not_called()

    def test_skip_log_carries_both_canonical_origins_and_no_secrets(self, caplog):
        """The skip log names BOTH sides as canonical origins so an operator
        can see which pool collided — and nothing else.  Userinfo, query-string
        tokens and paths from either URL must never reach the log."""
        agent = self._incident_agent([{"provider": "codex-lb", "model": "gpt-5.5"}])
        agent.base_url = "http://alice:current-s3cret@127.0.0.1:2455/v1?token=cur-tok"

        with caplog.at_level(logging.WARNING):
            ok, _ = self._activate(agent, FailoverReason.timeout, {
                "codex-lb": "http://bob:cand-s3cret@127.0.0.1:2455/v2/chat?token=cand-tok",
            })

        assert ok is False
        messages = [
            r.getMessage() for r in caplog.records
            if "same failure domain" in r.getMessage()
        ]
        assert len(messages) == 1
        message = messages[0]
        # Both canonical origins are present (current= and candidate=).
        assert message.count("http://127.0.0.1:2455") == 2
        assert "current=http://127.0.0.1:2455" in message
        assert "candidate=http://127.0.0.1:2455" in message
        for secret in (
            "current-s3cret", "cand-s3cret", "cur-tok", "cand-tok",
            "alice", "bob", "token=", "/v1", "/v2", "?",
        ):
            assert secret not in message, f"{secret!r} leaked into skip log"


class TestEndpointOriginNormalization:
    """Canonical failure-domain identity: scheme + lowercase host + effective
    port. Path, query, fragment, userinfo and trailing slash are ignored."""

    def test_default_ports_normalize(self):
        assert endpoint_origin("http://example.com/v1") == "http://example.com:80"
        assert endpoint_origin("https://example.com/v1") == "https://example.com:443"
        assert endpoint_origin("http://example.com:80/v1") == "http://example.com:80"
        assert endpoint_origin("https://example.com:443/v1") == "https://example.com:443"

    def test_host_case_and_trailing_dot_normalize(self):
        assert endpoint_origin("HTTPS://API.Example.COM./v1") == "https://api.example.com:443"

    def test_httpx_url_object_normalizes_and_is_idempotent(self):
        """Clients hand the guard an ``httpx.URL``, not a ``str`` — mixed-case
        host, explicit default port, path and query-string token all collapse
        to the canonical origin.  And feeding an origin back in is a no-op, so
        comparing two already-canonical values is safe."""
        url = httpx.URL("https://API.Example.com:443/v1?token=secret")
        assert endpoint_origin(url) == "https://api.example.com:443"
        assert endpoint_origin(endpoint_origin(url)) == endpoint_origin(url)

    def test_path_query_fragment_and_userinfo_ignored(self):
        assert (
            endpoint_origin("http://user:pass@127.0.0.1:2455/v1/chat?key=secret#frag")
            == "http://127.0.0.1:2455"
        )

    def test_trailing_slash_ignored(self):
        assert endpoint_origin("http://127.0.0.1:2455/v1/") == endpoint_origin(
            "http://127.0.0.1:2455/v1"
        )

    def test_ipv6_literal(self):
        assert endpoint_origin("http://[::1]:2455/v1") == "http://[::1]:2455"

    def test_unusable_values_return_empty(self):
        assert endpoint_origin("") == ""
        assert endpoint_origin(None) == ""
        assert endpoint_origin("127.0.0.1:2455/v1") == ""      # no scheme
        assert endpoint_origin("http://127.0.0.1:99999/v1") == ""  # invalid port
        assert endpoint_origin(MagicMock()) == ""

    def test_scheme_and_port_differences_are_distinct_domains(self):
        assert endpoint_origin("http://example.com/v1") != endpoint_origin(
            "https://example.com/v1"
        )
        assert endpoint_origin("http://127.0.0.1:2455") != endpoint_origin(
            "http://127.0.0.1:2456"
        )

    def test_same_failure_domain_requires_known_origins(self):
        assert same_failure_domain(
            "https://Gateway.Example.com/v1/", "https://gateway.example.com:443/v2?k=1"
        ) is True
        assert same_failure_domain("", "https://gateway.example.com/v1") is False
        assert same_failure_domain("https://gateway.example.com/v1", "") is False

    def test_infrastructure_reason_set(self):
        assert INFRASTRUCTURE_FAILOVER_REASONS == frozenset({
            FailoverReason.timeout,
            FailoverReason.server_error,
            FailoverReason.overloaded,
        })
