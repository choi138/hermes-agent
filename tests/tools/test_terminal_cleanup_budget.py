"""Shutdown-specific terminal environment cleanup budgeting."""

from tools import terminal_tool


def test_cleanup_all_environments_shares_one_shutdown_deadline(monkeypatch):
    clock = [100.0]
    calls: list[tuple[str, float | None]] = []

    def fake_cleanup_vm(task_id, *, shutdown_timeout_seconds=None, **_kwargs):
        calls.append((task_id, shutdown_timeout_seconds))
        clock[0] += 4.0

    monkeypatch.setattr(terminal_tool, "_monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"first": object(), "second": object()},
    )
    monkeypatch.setattr(terminal_tool, "cleanup_vm", fake_cleanup_vm)

    cleaned = terminal_tool.cleanup_all_environments(
        shutdown_budget_seconds=10.0
    )

    assert cleaned == 2
    assert calls == [("first", 10.0), ("second", 6.0)]


def test_cleanup_vm_forwards_shutdown_timeout_to_supporting_backend(monkeypatch):
    observed: list[float | None] = []

    class ShutdownAwareEnvironment:
        def cleanup(self, *, shutdown_timeout_seconds=None):
            observed.append(shutdown_timeout_seconds)

    monkeypatch.setattr(
        terminal_tool,
        "_active_environments",
        {"task": ShutdownAwareEnvironment()},
    )
    monkeypatch.setattr(terminal_tool, "_last_activity", {"task": 1.0})

    terminal_tool.cleanup_vm("task", shutdown_timeout_seconds=4.5)

    assert observed == [4.5]