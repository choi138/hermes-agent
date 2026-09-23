"""The applied route, not overlapping model membership, owns outage fallback."""

from types import SimpleNamespace

from agent.model_route_fallback import _build_outage_route_fallback_chain


def test_applied_route_precedes_global_fallback_without_guessing_membership(monkeypatch):
    cfg = {
        "providers": {
            "first": {"base_url": "https://first.example/v1"},
            "second": {"base_url": "https://second.example/v1"},
        },
        "model_routes": {
            "routes": {
                "dev": {
                    "provider": "first", "model": "shared",
                    "accepted": ["shared"],
                    "fallbacks": [{"provider": "second", "model": "dev-backup"}],
                },
                "chat": {
                    "provider": "first", "model": "shared",
                    "accepted": ["shared"],
                    "fallbacks": [{"provider": "second", "model": "chat-backup"}],
                },
            },
        },
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    agent = SimpleNamespace(
        provider="first", model="shared", base_url="https://first.example/v1",
        reasoning_config=None, _active_route_name="dev",
    )
    route, chain = _build_outage_route_fallback_chain(agent)
    assert route == "dev"
    assert [(item["provider"], item["model"]) for item in chain] == [
        ("second", "dev-backup"),
    ]

    agent._active_route_name = ""
    assert _build_outage_route_fallback_chain(agent) == ("", [])
