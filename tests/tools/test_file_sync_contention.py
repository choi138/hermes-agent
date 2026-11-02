"""Read-only sync checks must not queue behind another session's transfers."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from tools.environments import file_sync


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(file_sync, "_credential_host_paths", set)
    local = tmp_path / "skill.md"
    local.write_text("instructions")
    uploads = []
    manager = file_sync.FileSyncManager(
        lambda: [(str(local), "/remote/skill.md")],
        lambda host, remote: uploads.append(Path(host).read_text()),
        lambda _: None,
        selective_download_fn=lambda *args: None,
        fail_closed=True,
    )
    manager.sync(force=True)
    return manager, local, uploads


@pytest.mark.parametrize("force", [False, True])
def test_unchanged_sync_does_not_acquire_transfer_lock(tmp_path, monkeypatch, force):
    manager, _, uploads = _manager(tmp_path, monkeypatch)

    @contextmanager
    def contended_lock(_):
        raise AssertionError("unchanged files queued behind another session")
        yield

    monkeypatch.setattr(manager, "_sync_file_lock", contended_lock)
    manager.sync(force=force)
    assert uploads == ["instructions"]


def test_pending_recovery_bypasses_interval_and_unchanged_fast_path(tmp_path, monkeypatch):
    manager, _, _ = _manager(tmp_path, monkeypatch)
    manager._pending_sync_back().mkdir()
    recovered = []

    def recover():
        recovered.append(True)
        manager._pending_sync_back().rmdir()

    monkeypatch.setattr(manager, "_recover_pending_sync_back", recover)
    manager.sync()
    assert recovered == [True]


def test_changed_sync_still_serializes_transfer(tmp_path, monkeypatch):
    manager, local, uploads = _manager(tmp_path, monkeypatch)
    local.write_text("updated and longer instructions")
    locked = False

    @contextmanager
    def transfer_lock(_):
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    def upload(host, _):
        assert locked, "remote mutation must remain serialized"
        uploads.append(Path(host).read_text())

    monkeypatch.setattr(manager, "_sync_file_lock", transfer_lock)
    monkeypatch.setattr(manager, "_upload_fn", upload)
    manager.sync(force=True)
    assert uploads == ["instructions", "updated and longer instructions"]
