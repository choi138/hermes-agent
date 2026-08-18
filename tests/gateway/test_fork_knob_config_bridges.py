"""config.yaml bridges for gateway performance knobs."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

import gateway.run as gateway_run


def _write_home(tmp_path: Path, agent_config: dict, env_text: str = "") -> Path:
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"agent": agent_config}), encoding="utf-8"
    )
    (hermes_home / ".env").write_text(env_text, encoding="utf-8")
    return hermes_home


def test_process_wait_cap_bridged_from_config(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"process_wait_cap": 2})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setenv("HERMES_PROCESS_WAIT_CAP", "9")

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["HERMES_PROCESS_WAIT_CAP"] == "2"


def test_fast_conn_fail_limit_bridged_from_config(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"fast_conn_fail_limit": 5})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.delenv("HERMES_FAST_CONN_FAIL_LIMIT", raising=False)

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["HERMES_FAST_CONN_FAIL_LIMIT"] == "5"


def test_env_survives_when_config_omits_knobs(tmp_path, monkeypatch):
    home = _write_home(tmp_path, {"max_turns": 90})
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setenv("HERMES_PROCESS_WAIT_CAP", "0")
    monkeypatch.setenv("HERMES_FAST_CONN_FAIL_LIMIT", "7")

    gateway_run._reload_runtime_env_preserving_config_authority()

    assert os.environ["HERMES_PROCESS_WAIT_CAP"] == "0"
    assert os.environ["HERMES_FAST_CONN_FAIL_LIMIT"] == "7"
