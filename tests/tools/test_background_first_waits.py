"""Repeated foreground waits promote a process to notify-on-completion."""

from __future__ import annotations

import pytest

from tools.process_registry import ProcessRegistry


@pytest.fixture()
def registry():
    return ProcessRegistry()


def test_second_consecutive_timeout_arms_notify(registry, monkeypatch):
    monkeypatch.delenv("HERMES_PROCESS_WAIT_CAP", raising=False)
    session = registry.spawn_local("sleep 30", cwd=None)
    try:
        first = registry.wait(session.id, timeout=1)
        assert first["status"] == "timeout"
        assert "notify_on_complete" not in first
        assert session.notify_on_complete is False

        second = registry.wait(session.id, timeout=1)
        assert second["status"] == "timeout"
        assert second["notify_on_complete"] is True
        assert "end your turn" in second["timeout_note"].lower()
        assert session.notify_on_complete is True
    finally:
        registry.kill_process(session.id)


def test_streak_resets_on_exit(registry, monkeypatch):
    monkeypatch.delenv("HERMES_PROCESS_WAIT_CAP", raising=False)
    session = registry.spawn_local("sleep 0.2", cwd=None)

    result = registry.wait(session.id, timeout=10)

    assert result["status"] == "exited"
    assert registry._wait_timeout_streaks.get(session.id, 0) == 0


def test_cap_zero_disables_escalation(registry, monkeypatch):
    monkeypatch.setenv("HERMES_PROCESS_WAIT_CAP", "0")
    session = registry.spawn_local("sleep 30", cwd=None)
    try:
        registry.wait(session.id, timeout=1)
        second = registry.wait(session.id, timeout=1)
        assert second["status"] == "timeout"
        assert "notify_on_complete" not in second
        assert session.notify_on_complete is False
    finally:
        registry.kill_process(session.id)


def test_cap_two_allows_two_quiet_waits(registry, monkeypatch):
    monkeypatch.setenv("HERMES_PROCESS_WAIT_CAP", "2")
    session = registry.spawn_local("sleep 30", cwd=None)
    try:
        registry.wait(session.id, timeout=1)
        second = registry.wait(session.id, timeout=1)
        assert "notify_on_complete" not in second
        third = registry.wait(session.id, timeout=1)
        assert third["notify_on_complete"] is True
    finally:
        registry.kill_process(session.id)
