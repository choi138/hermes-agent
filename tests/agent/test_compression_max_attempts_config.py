"""Behavioral coverage for the configured compression-attempt budget."""

from __future__ import annotations

import contextlib
import io

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _config(max_attempts=...):
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    if max_attempts is not ...:
        compression["max_attempts"] = max_attempts
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path, max_attempts=...):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config(max_attempts))
    with contextlib.redirect_stdout(io.StringIO()):
        return AIAgent(
            base_url="https://openrouter.ai/api/v1",
            api_key="test-key",
            model="test/model",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=SessionDB(db_path=tmp_path / "state.db"),
            session_id="compression-attempt-config",
        )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (..., 3),
        (1, 1),
        (6, 6),
        (10, 10),
        (25, 10),
        (0, 3),
        (-2, 3),
        (True, 3),
        (4.7, 3),
        ("invalid", 3),
        ("6", 6),
    ],
)
def test_compression_max_attempts_is_validated(
    monkeypatch, tmp_path, configured, expected
):
    agent = _make_agent(monkeypatch, tmp_path, configured)
    assert agent.max_compression_attempts == expected
