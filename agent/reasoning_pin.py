"""Exact session pins at the final request boundary; automatic requests are untouched.

Use the installed SDK's merge, including Omit, rather than inventing a deep
merge for extra_body. This proves a local wire contract, never server behavior.
Numeric budgets and transports without a declared effort contract are not
equivalent to an exact effort pin.
"""
from collections.abc import Mapping

from agent.reasoning_effort import reasoning_is_pinned


class PinnedReasoningError(ValueError):
    """An explicit pin cannot be honored by the effective request."""


def _fail(detail):
    raise PinnedReasoningError(
        "Pinned reasoning effort is unavailable or conflicts with the final request: "
        + detail + ". Release or change the pin explicitly."
    )


def effective_body(kwargs, api_mode):
    # Both installed SDKs currently do a SHALLOW merge. Calling their helper
    # also preserves their Omit semantics if the implementation changes.
    if api_mode == "anthropic_messages":
        from anthropic._base_client import _merge_mappings
        from anthropic import NotGiven, Omit
    else:
        from openai._base_client import _merge_mappings
        from openai import NotGiven, Omit
    body = {k: v for k, v in kwargs.items()
            if k not in ("extra_body", "extra_headers", "extra_query", "timeout")
            and not isinstance(v, (NotGiven, Omit))}
    extra = kwargs.get("extra_body")
    if extra is None or isinstance(extra, (NotGiven, Omit)):
        return body
    if not isinstance(extra, Mapping):
        _fail("malformed extra_body")
    return _merge_mappings(body, extra)


def _responses_efforts(model, provider, base_url):
    from agent.reasoning_effort import (ACTUAL_RELAY_EFFORTS, XAI_GROK46_EFFORTS,
        XAI_LEGACY_EFFORTS, codex_supported_efforts)
    from agent.transports.codex import _profile_declared_efforts
    from urllib.parse import urlsplit

    host = urlsplit(base_url or "").hostname or ""
    if provider == "xai" or host == "api.x.ai":
        from agent.model_metadata import is_grok_46_family, grok_supports_reasoning_effort
        if not grok_supports_reasoning_effort(model):
            return ()
        return XAI_GROK46_EFFORTS if is_grok_46_family(model) else XAI_LEGACY_EFFORTS
    if provider == "actual":
        return ACTUAL_RELAY_EFFORTS
    declared = _profile_declared_efforts(provider, model, base_url)
    if declared is not None:
        return declared
    slug = model.lower().split("/")[-1]
    if slug.startswith(("gpt-5", "gpt-6-astra", "o1", "o3", "o4")):
        return codex_supported_efforts(model)
    return ()


def _chat_efforts(model, provider, profile):
    """Reuse existing native vocabularies; do not infer support from an echo."""
    from providers import get_provider_profile
    from agent.reasoning_effort import KIMI_K2_EFFORTS, KIMI_K3_EFFORTS, GLM52_EFFORTS, kimi_supported_efforts

    profile = profile or get_provider_profile(provider or "")
    name = profile.name if profile is not None else provider
    slug = model.lower().split("/")[-1]
    if name in ("kimi-coding", "kimi-coding-cn") and kimi_supported_efforts(model) == KIMI_K3_EFFORTS:
        return KIMI_K3_EFFORTS
    if name == "opencode-go":
        if slug.startswith("kimi-k2"):
            return KIMI_K2_EFFORTS
        if slug in ("glm-5.2", "glm-5-2", "glm-5p2"):
            return GLM52_EFFORTS
    if profile is not None and profile.api_mode == "chat_completions":
        return profile.supported_reasoning_efforts(model)
    return None


def validate_pinned_request(kwargs, config, *, api_mode, provider=None,
                            base_url=None, provider_profile=None):
    """Return unchanged kwargs or fail before sending a conflicting pin."""
    if not reasoning_is_pinned(config):
        return kwargs
    effort = "none" if config.get("enabled") is False else config.get("effort")
    if not isinstance(effort, str) or not effort:
        _fail("missing requested effort")
    if api_mode not in ("codex_responses", "chat_completions", "anthropic_messages"):
        _fail("transport has no verifiable exact effort contract")
    body = effective_body(kwargs, api_mode)
    model = body.get("model")
    if not isinstance(model, str) or not model:
        _fail("missing or malformed final model")

    if api_mode == "codex_responses":
        supported = _responses_efforts(model, provider, base_url)
        reasoning = body.get("reasoning")
        if (effort not in supported or not isinstance(reasoning, dict)
                or reasoning.get("effort") != effort
                or reasoning.get("enabled") is False or "max_tokens" in reasoning
                or any(k in body for k in ("reasoning_effort", "thinking", "output_config"))):
            _fail("Responses model capability or merged reasoning does not match")
    elif api_mode == "anthropic_messages":
        from agent.anthropic_adapter import _supports_adaptive_thinking, _supports_xhigh_effort, _accepts_thinking_disable
        thinking = body.get("thinking")
        output = body.get("output_config")
        supported = {"low", "medium", "high", "max"}
        if _supports_xhigh_effort(model):
            supported.add("xhigh")
        if any(k in body for k in ("reasoning", "reasoning_effort")):
            _fail("conflicting Messages reasoning controls")
        if effort == "none":
            if not (_accepts_thinking_disable(model) and thinking == {"type": "disabled"} and not output):
                _fail("Messages reasoning disable cannot be verified")
        elif (not _supports_adaptive_thinking(model) or effort not in supported
                or not isinstance(output, dict) or output.get("effort") != effort
                or not isinstance(thinking, dict) or thinking.get("type") != "adaptive"
                or "budget_tokens" in thinking):
            _fail("Messages requires exact adaptive effort; numeric budgets are not effort pins")
    else:
        # Chat-compatible adapters often translate to toggles or budgets.
        # Only a provider-declared vocabulary establishes a local contract;
        # an arbitrary echoed string on an unknown endpoint is not proof.
        from agent.gemini_native_adapter import is_native_gemini_base_url
        if provider == "moa" or is_native_gemini_base_url(base_url):
            _fail("adapter does not expose a verifiable native effort contract")
        supported = _chat_efforts(model, provider, provider_profile)
        if not supported or effort not in supported:
            _fail("chat provider does not declare this exact effort")
        reasoning = body.get("reasoning")
        exact_scalar = body.get("reasoning_effort") == effort and "reasoning" not in body
        exact_object = (isinstance(reasoning, dict) and reasoning.get("effort") == effort
                        and reasoning.get("enabled") is not False
                        and "max_tokens" not in reasoning and "reasoning_effort" not in body)
        if not (exact_scalar or exact_object) or any(k in body for k in ("thinking", "thinking_config", "thinkingConfig", "output_config", "extra_body")):
            _fail("merged chat reasoning does not match the declared effort")
    return kwargs


def validate_agent_request(agent, kwargs):
    return validate_pinned_request(kwargs, getattr(agent, "reasoning_config", None),
        api_mode=getattr(agent, "api_mode", "chat_completions"),
        provider=getattr(agent, "provider", None), base_url=getattr(agent, "base_url", None))
