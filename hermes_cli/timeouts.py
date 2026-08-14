from __future__ import annotations

from typing import Optional

from hermes_cli.provider_config import get_provider_config_entry


def _coerce_timeout(raw: object) -> float | None:
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return timeout


def get_provider_request_timeout(
    provider_id: str,
    model: str | None = None,
    *,
    requested_provider: Optional[str] = None,
) -> float | None:
    """Return a configured provider request timeout in seconds, if any."""
    resolved = get_provider_config_entry(
        provider_id,
        requested_provider=requested_provider,
    )
    if resolved is None:
        return None
    _resolved_id, provider_config = resolved

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("request_timeout_seconds"))


def get_provider_stale_timeout(
    provider_id: str,
    model: str | None = None,
    *,
    requested_provider: Optional[str] = None,
) -> float | None:
    """Return a configured non-stream stale timeout in seconds, if any."""
    resolved = get_provider_config_entry(
        provider_id,
        requested_provider=requested_provider,
    )
    if resolved is None:
        return None
    _resolved_id, provider_config = resolved

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("stale_timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("stale_timeout_seconds"))


def _get_model_config(
    provider_config: dict[str, object], model: str | None
) -> dict[str, object] | None:
    if not model:
        return None

    models = provider_config.get("models", {})
    model_config = models.get(model, {}) if isinstance(models, dict) else {}
    if isinstance(model_config, dict):
        return model_config
    return None
