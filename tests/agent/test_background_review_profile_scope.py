"""Background review must run under the profile HERMES_HOME of the turn that queued it.

The coordinator invokes ``target_factory`` on its own worker thread, so any context captured
inside the factory is the worker's (empty) context, not the foreground turn's profile scope.
Pending memory writes then land in the process-default ``<home>/pending`` where the profile's
``/memory pending`` never looks.
"""

import threading
import types

import pytest


def test_background_review_inherits_profile_home_override(monkeypatch, tmp_path):
    import run_agent
    import agent.background_review as bg
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    profile_home = tmp_path / "profiles" / "koharu"
    profile_home.mkdir(parents=True)

    seen = {}
    done = threading.Event()

    def fake_prepare(agent):
        return types.SimpleNamespace(cancel_requested=threading.Event())

    def fake_spawn(agent, messages_snapshot, **kwargs):
        def _target():
            seen["home"] = str(get_hermes_home())
            done.set()
        return _target, "prompt"

    monkeypatch.setattr(bg, "prepare_background_review_run", fake_prepare)
    monkeypatch.setattr(bg, "spawn_background_review_thread", fake_spawn)
    monkeypatch.setattr(bg, "finish_background_review_run", lambda agent, run: None)

    fake_agent = types.SimpleNamespace(_maybe_requeue_preempted_review=lambda run, kw: None)

    token = set_hermes_home_override(str(profile_home))
    try:
        accepted = run_agent.AIAgent._spawn_background_review_now(
            fake_agent,
            messages_snapshot=[{"role": "user", "content": f"profile-scope-{tmp_path.name}"}],
            review_memory=True,
            task_cfg={"idle_grace_seconds": 0.0, "dedupe_ttl_seconds": 0.0, "queue_limit": 64},
        )
    finally:
        reset_hermes_home_override(token)

    assert accepted is True
    assert done.wait(10), "background review target never ran"
    assert seen["home"] == str(profile_home)
