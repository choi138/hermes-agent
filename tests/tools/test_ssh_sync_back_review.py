"""Review reproduction: deadline must not leave remote edits vulnerable to next upload."""
import hashlib
import shutil
import tarfile
from pathlib import Path

import pytest

from tools.environments import file_sync


def test_deadline_during_application_preserves_pending_edits(tmp_path, monkeypatch):
    host, remote = tmp_path / 'host', tmp_path / 'remote'
    host.mkdir()
    remote.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(host))
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: set())
    mapping = [(str(host / name), str(remote / name)) for name in ['a.txt', 'b.txt']]
    for local, other in mapping:
        Path(local).write_text('old')
        Path(other).write_text('new')
    clock = [0.0]
    monkeypatch.setattr(file_sync, '_monotonic', lambda: clock[0])
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 1)

    def download(dest, request, timeout):
        with tarfile.open(dest, 'w') as archive:
            for _, other in mapping:
                archive.add(other, arcname=other.lstrip('/'))

    manager = file_sync.FileSyncManager(
        get_files_fn=lambda: mapping, upload_fn=shutil.copy2, delete_fn=lambda _: None,
        selective_download_fn=download,
    )
    manager._pushed_hashes = {other: hashlib.sha256(b'old').hexdigest() for _, other in mapping}
    apply = manager._apply_staged_file

    def apply_then_expire(*args):
        result = apply(*args)
        clock[0] = 2.0
        return result

    monkeypatch.setattr(manager, '_apply_staged_file', apply_then_expire)
    manager.sync_back()
    before_reconnect = [Path(p).read_text() for p, _ in mapping]
    assert sorted(before_reconnect) == ['new', 'old']
    assert (host / '.sync-back-pending').is_dir()
    # A newly created SSH environment always does sync(force=True) before executing.
    fresh = file_sync.FileSyncManager(
        get_files_fn=lambda: mapping, upload_fn=shutil.copy2, delete_fn=lambda _: None,
    )
    restored = fresh._apply_staged_file
    def apply_only_unfinished(staged, remote_path, *args):
        host_path = dict((remote, host) for host, remote in mapping)[remote_path]
        assert Path(host_path).read_text() == 'old', 'completed file was replayed'
        return restored(staged, remote_path, *args)
    monkeypatch.setattr(fresh, '_apply_staged_file', apply_only_unfinished)
    fresh.sync(force=True)
    assert not (host / '.sync-back-pending').exists()
    assert [Path(p).read_text() for p, _ in mapping] == ['new', 'new']
    remote_after = [Path(p).read_text() for _, p in mapping]
    print({'host_after_timeout': before_reconnect, 'remote_after_reconnect': remote_after})
    assert remote_after == ['new', 'new'], 'pending remote edit was overwritten on reconnect'


def test_large_mapping_inference_does_not_resolve_unrelated_paths(tmp_path, monkeypatch):
    mapping = [(str(tmp_path / str(i) / 'existing'), f'/remote/{i}/existing')
               for i in range(2707)]
    manager = file_sync.FileSyncManager(lambda: mapping, lambda *a: None, lambda *a: None)
    resolve = file_sync._resolve_host_path_str
    count = 0
    def bounded_resolve(path):
        nonlocal count
        count += 1
        assert count <= 512, 'unrelated paths repeatedly canonicalized'
        return resolve(path)
    monkeypatch.setattr(file_sync, '_resolve_host_path_str', bounded_resolve)
    credentials = {str(tmp_path / 'credential')}
    for i in range(2451, 2707):
        assert manager._infer_host_path(f'/remote/{i}/new', mapping,
            upload_only_host_paths=credentials) == str(tmp_path / str(i) / 'new')


def test_failed_recovery_keeps_host_and_blocks_upload(tmp_path, monkeypatch):
    host, remote = tmp_path / 'host', tmp_path / 'remote'
    host.mkdir()
    remote.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(host))
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: set())
    local, other = host / 'state.txt', remote / 'state.txt'
    local.write_text('old')
    other.write_text('new')
    mapping = [(str(local), str(other))]

    def download(dest, request, timeout):
        with tarfile.open(dest, 'w') as archive:
            archive.add(other, arcname=str(other).lstrip('/'))

    manager = file_sync.FileSyncManager(
        get_files_fn=lambda: mapping, upload_fn=shutil.copy2, delete_fn=lambda _: None,
        selective_download_fn=download,
    )
    manager._pushed_hashes = {str(other): hashlib.sha256(b'old').hexdigest()}
    copy = shutil.copy2

    def interrupted_copy(source, destination, *args, **kwargs):
        if '.sync-back-pending' in Path(source).parts:
            Path(destination).write_text('partial')
            raise OSError('injected disk write failure')
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copy2', interrupted_copy)
    monkeypatch.setattr(file_sync, '_sleep', lambda _: None)
    manager.sync_back()
    assert local.read_text() == 'old'
    assert (host / '.sync-back-pending').is_dir()
    uploads = []
    fresh = file_sync.FileSyncManager(
        get_files_fn=lambda: mapping, upload_fn=lambda *args: uploads.append(args),
        delete_fn=lambda _: None, selective_download_fn=download,
    )
    with pytest.raises(OSError, match='injected disk write failure'):
        fresh.sync(force=True)
    assert uploads == []
    assert local.read_text() == 'old'
    assert other.read_text() == 'new'
    assert (host / '.sync-back-pending').is_dir()
    monkeypatch.setattr(shutil, 'copy2', copy)
    fresh.sync(force=True)
    assert local.read_text() == 'new'
    assert uploads
    assert not (host / '.sync-back-pending').exists()


def test_shared_state_lock_honors_shutdown_deadline(tmp_path):
    import threading
    import time
    from unittest.mock import Mock

    state = file_sync.FileSyncState()
    entered, release = threading.Event(), threading.Event()
    def hold_lock():
        with state.lock:
            entered.set()
            release.wait(5)
    worker = threading.Thread(target=hold_lock)
    worker.start()
    download = Mock()
    manager = file_sync.FileSyncManager(
        lambda: [], lambda *a: None, lambda *a: None,
        shared_state=state, selective_download_fn=download)
    try:
        assert entered.wait(2)
        start = time.monotonic()
        assert manager.sync_back(deadline=start + 0.1) is False
        assert time.monotonic() - start < 2
        download.assert_not_called()
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_selective_shutdown_preserves_bool_success_and_deadline(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: set())
    deadlines = []
    def download(dest, request, remaining):
        deadlines.append(remaining)
        with tarfile.open(dest, 'w'):
            pass
    def forbidden_bulk(*args):
        raise AssertionError('selective cleanup used legacy bulk download')
    manager = file_sync.FileSyncManager(
        lambda: [], lambda *a: None, lambda *a: None,
        selective_download_fn=download)
    manager._pushed_hashes = {'/remote/old': 'baseline'}
    assert manager.sync_back(deadline=time.monotonic() + 2,
                             bulk_download_fn=forbidden_bulk, max_attempts=1) is True
    assert 0 < deadlines[0] <= 2
    assert not (tmp_path / '.sync-back-intent.json').exists()
    monkeypatch.setattr(manager, '_selective_download_fn',
                        lambda *a: (_ for _ in ()).throw(TimeoutError('offline')))
    assert manager.sync_back(max_attempts=1) is False
    assert (tmp_path / '.sync-back-intent.json').exists()
