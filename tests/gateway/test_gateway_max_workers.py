"""gateway.max_workers resolution for the shared agent-turn thread pool."""

from __future__ import annotations

from unittest.mock import patch

from gateway.run import _gateway_max_workers


class TestGatewayMaxWorkers:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_MAX_WORKERS", raising=False)
        with patch("gateway.run._load_gateway_config", return_value={}):
            assert _gateway_max_workers() == 12

    def test_config_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_MAX_WORKERS", "8")
        with patch(
            "gateway.run._load_gateway_config",
            return_value={"gateway": {"max_workers": 20}},
        ):
            assert _gateway_max_workers() == 20

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_MAX_WORKERS", "16")
        with patch("gateway.run._load_gateway_config", return_value={}):
            assert _gateway_max_workers() == 16

    def test_invalid_values_fall_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_MAX_WORKERS", "lots")
        with patch("gateway.run._load_gateway_config", return_value={}):
            assert _gateway_max_workers() == 12

    def test_clamped_to_minimum_two(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_MAX_WORKERS", raising=False)
        with patch(
            "gateway.run._load_gateway_config",
            return_value={"gateway": {"max_workers": 0}},
        ):
            assert _gateway_max_workers() == 2

    def test_config_read_failure_falls_back(self, monkeypatch):
        monkeypatch.delenv("HERMES_GATEWAY_MAX_WORKERS", raising=False)
        with patch("gateway.run._load_gateway_config", side_effect=RuntimeError):
            assert _gateway_max_workers() == 12
