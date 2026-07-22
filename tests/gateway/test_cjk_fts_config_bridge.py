"""config.yaml sessions.cjk_fts bridge (config-authoritative).

Salvaged from PR #65544 (adapted: agent.fts_v2_read → sessions.cjk_fts).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

import gateway.run as gateway_run


def _write_home(tmp_path: Path, sessions_cfg: dict, env_text: str = "") -> Path:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"sessions": sessions_cfg}), encoding="utf-8"
    )
    (hermes_home / ".env").write_text(env_text, encoding="utf-8")
    return hermes_home


def test_cjk_fts_bridged_from_config(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"cjk_fts": False})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setenv("HERMES_CJK_FTS", "1")
    gateway_run._reload_runtime_env_preserving_config_authority()
    assert os.environ["HERMES_CJK_FTS"] == "False"


def test_env_survives_when_config_omits_cjk_setting(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"auto_prune": False})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setenv("HERMES_CJK_FTS", "0")
    gateway_run._reload_runtime_env_preserving_config_authority()
    assert os.environ["HERMES_CJK_FTS"] == "0"


def test_cjk_fts_has_documented_default():
    """The advertised config surface defaults the CJK index to ON."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["sessions"]["cjk_fts"] is True


def test_config_false_disables_cjk_semantics(tmp_path, monkeypatch):
    """The bridged 'False' string must parse as OFF in hermes_state."""
    from hermes_state import _cjk_fts_config_enabled

    monkeypatch.setenv("HERMES_CJK_FTS", "False")
    assert not _cjk_fts_config_enabled()
    monkeypatch.setenv("HERMES_CJK_FTS", "True")
    assert _cjk_fts_config_enabled()
    monkeypatch.delenv("HERMES_CJK_FTS", raising=False)
    assert _cjk_fts_config_enabled()  # default on
