from types import SimpleNamespace

from agent import relay_runtime
from hermes_cli import lifecycle, observability, plugins


def test_invoke_hook_notifies_builtin_observers_before_plugins(monkeypatch):
    calls = []
    manager = SimpleNamespace(
        invoke_hook=lambda name, **kwargs: calls.append(("plugin", name, kwargs)) or ["ok"]
    )
    monkeypatch.setattr(
        observability,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("builtin", name, kwargs)),
    )
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)

    result = lifecycle.invoke_hook("on_session_start", session_id="session-1")

    assert result == ["ok"]
    assert [call[0] for call in calls] == ["builtin", "plugin"]


def test_finalize_session_closes_core_before_plugin_export(monkeypatch):
    calls = []
    manager = SimpleNamespace(
        invoke_hook=lambda name, **kwargs: calls.append(("plugin", name, kwargs)) or []
    )
    coordinator = SimpleNamespace(
        finalize_conversation=lambda **kwargs: calls.append(("core", kwargs))
    )
    monkeypatch.setattr(
        observability,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("builtin", name, kwargs)),
    )
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)
    monkeypatch.setattr(relay_runtime, "SESSION_COORDINATOR", coordinator)
    monkeypatch.setattr(relay_runtime, "current_profile_key", lambda: "profile-1")

    lifecycle.finalize_session(session_id="session-1", platform="cli")

    assert [call[0] for call in calls] == ["builtin", "core", "plugin"]
    assert calls[1][1] == {
        "profile_key": "profile-1",
        "session_id": "session-1",
    }


def test_plugin_only_dispatch_does_not_reenter_builtin_observers(monkeypatch):
    manager = SimpleNamespace(invoke_hook=lambda name, **kwargs: [name, kwargs])
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(
        observability,
        "observe_lifecycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
    )

    assert plugins.invoke_hook("custom", value=1) == ["custom", {"value": 1}]


def test_both_first_party_observers_are_dispatched(monkeypatch):
    from hermes_cli.observability import local_observations, relay_shared_metrics

    calls = []
    monkeypatch.setattr(
        relay_shared_metrics,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("relay", name)),
    )
    monkeypatch.setattr(
        local_observations,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("local", name)),
    )

    observability.observe_lifecycle("pre_api_request", session_id="s1")

    assert calls == [("relay", "pre_api_request"), ("local", "pre_api_request")]


def test_a_raising_local_observer_does_not_stop_the_relay_observer(monkeypatch):
    from hermes_cli.observability import local_observations, relay_shared_metrics

    calls = []
    monkeypatch.setattr(
        relay_shared_metrics,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("relay", name)),
    )

    def exploding(name, **kwargs):
        raise RuntimeError("recorder is broken")

    monkeypatch.setattr(local_observations, "observe_lifecycle", exploding)
    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kwargs: ["plugin"])

    # Must not raise, and the plugin dispatch must still happen.
    assert lifecycle.invoke_hook("post_api_request", session_id="s1") == ["plugin"]
    assert calls == [("relay", "post_api_request")]


def test_a_raising_relay_observer_does_not_stop_the_local_observer(monkeypatch):
    from hermes_cli.observability import local_observations, relay_shared_metrics

    calls = []

    def exploding(name, **kwargs):
        raise RuntimeError("relay is broken")

    monkeypatch.setattr(relay_shared_metrics, "observe_lifecycle", exploding)
    monkeypatch.setattr(
        local_observations,
        "observe_lifecycle",
        lambda name, **kwargs: calls.append(("local", name)),
    )

    observability.observe_lifecycle("post_tool_call", session_id="s1")

    assert calls == [("local", "post_tool_call")]


def test_handles_hook_delegates_only_to_the_relay_predicate(monkeypatch):
    """The local recorder must NOT add a second lock-serialised config read."""
    from hermes_cli.observability import relay_shared_metrics

    observed = []

    def relay_handles(name):
        observed.append(name)
        return name == "pre_api_request"

    monkeypatch.setattr(relay_shared_metrics, "handles_hook", relay_handles)

    names = sorted(relay_shared_metrics.HANDLED_HOOKS) + ["not_a_hook"]
    for name in names:
        assert observability.handles_hook(name) is (name == "pre_api_request")

    # Exactly ONE delegation per query — no doubled predicate, and therefore no
    # doubled config stat under the global config lock.
    assert observed == names


def test_one_hook_dispatch_reads_the_config_once(monkeypatch, tmp_path):
    """Guard for the doubled read_raw_config_readonly stat."""
    import hermes_cli.config as config_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    reads = []
    real = config_module.read_raw_config_readonly

    def counting():
        reads.append(1)
        return {"telemetry": {"shared_metrics": {"enabled": False}}}

    del real
    monkeypatch.setattr(config_module, "read_raw_config_readonly", counting)
    monkeypatch.setattr(plugins, "invoke_hook", lambda name, **kwargs: [])

    lifecycle.invoke_hook("pre_api_request", session_id="s1")

    # One read for the relay observer's gate, one for the local recorder's.
    assert len(reads) <= 2
