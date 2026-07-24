"""Proactive prune configuration reaches the built-in compressor safely."""

import contextlib
import io

from hermes_state import SessionDB
from run_agent import AIAgent


def _make_agent(monkeypatch, tmp_path, compression):
    from hermes_cli import config as config_mod

    config = {
        "compression": {
            "enabled": True,
            "threshold": 0.50,
            "target_ratio": 0.20,
            "protect_first_n": 3,
            "protect_last_n": 20,
            **compression,
        },
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: config)
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
            session_id="proactive-prune-config",
        )


def test_proactive_prune_defaults_off(monkeypatch, tmp_path):
    compressor = _make_agent(monkeypatch, tmp_path, {}).context_compressor
    assert compressor.proactive_prune_tokens == 0
    assert compressor.proactive_prune_min_result_chars == 8_000
    assert compressor.proactive_prune_min_reclaim_tokens == 4_096


def test_proactive_prune_candidate_values_reach_compressor(monkeypatch, tmp_path):
    compressor = _make_agent(
        monkeypatch,
        tmp_path,
        {
            "proactive_prune_tokens": 48_000,
            "proactive_prune_min_result_chars": 8_000,
            "proactive_prune_min_reclaim_tokens": 4_096,
        },
    ).context_compressor
    assert compressor.proactive_prune_tokens == 48_000
    assert compressor.proactive_prune_min_result_chars == 8_000
    assert compressor.proactive_prune_min_reclaim_tokens == 4_096
