from __future__ import annotations

from hermes_cli.config_providers import get_provider_config_entry


def _coerce_timeout(raw: object) -> float | None:
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _configured_timeout(
    provider_id: str, model: str | None, model_key: str, provider_key: str,
    requested_provider: str | None = None,
) -> float | None:
    """Per-model ``providers.<id>.models.<model>.<model_key>`` wins over ``providers.<id>.<provider_key>``."""
    if not provider_id:
        return None
    try:
        resolved = get_provider_config_entry(
            provider_id, requested_provider=requested_provider,
        )
    except Exception:
        return None
    if resolved is None:
        return None
    _resolved_id, provider_config = resolved
    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get(model_key))
        if timeout is not None:
            return timeout
    return _coerce_timeout(provider_config.get(provider_key))


def get_provider_request_timeout(
    provider_id: str, model: str | None = None, *, requested_provider: str | None = None,
) -> float | None:
    """Return a configured provider request timeout in seconds, if any."""
    return _configured_timeout(
        provider_id, model, "timeout_seconds", "request_timeout_seconds", requested_provider,
    )


def get_provider_stale_timeout(
    provider_id: str, model: str | None = None, *, requested_provider: str | None = None,
) -> float | None:
    """Return a configured non-stream stale timeout in seconds, if any."""
    return _configured_timeout(
        provider_id, model, "stale_timeout_seconds", "stale_timeout_seconds", requested_provider,
    )


def _get_model_config(provider_config: dict[str, object], model: str | None) -> dict[str, object] | None:
    if not model:
        return None
    models = provider_config.get("models", {})
    model_config = models.get(model, {}) if isinstance(models, dict) else {}
    return model_config if isinstance(model_config, dict) else None
