"""Failed downloads must recover across managers before any new upload."""
import json
import shutil
import subprocess
import sys

import pytest

from tools.environments import file_sync, ssh_sync_back


@pytest.fixture
def setup_sync(tmp_path, monkeypatch):
    host, remote = tmp_path/'host', tmp_path/'remote'
    host.mkdir(); remote.mkdir()
    local, other = host/'result', remote/'result'
    local.write_text('original')
    monkeypatch.setenv('HERMES_HOME', str(host))
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: set())
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 0.3)
    mapping = [(str(local), str(other))]
    uploads = []
    def upload(source, destination):
        uploads.append(destination)
        shutil.copy2(source, destination)
    def download(dest, request, timeout):
        ssh_sync_back.download_changes(['/bin/bash', '-c'], dest, request, timeout)
    def manager(transport=download, identity='server-a', files=None):
        return file_sync.FileSyncManager(lambda: mapping if files is None else files,
            upload, lambda *a: None, selective_download_fn=transport,
            sync_back_identity=identity)
    return host, local, other, uploads, manager


def _check_reconnect_recovers_before_upload(setup_sync, monkeypatch, failure):
    host, local, other, uploads, manager = setup_sync
    def fail(dest, request, timeout):
        if failure == 'timeout':
            ssh_sync_back.download_changes(['/bin/bash', '-c', 'exec sleep 10', '--'],
                                           dest, request, timeout)
        else:
            ssh_sync_back.download_changes(['/bin/bash', '-c'], dest,
                                           dict(request, max_bytes=512), timeout)
    old = manager(fail)
    old.sync(force=True)
    other.write_text('completed remote work')
    old.sync_back()
    assert (host/'.sync-back-intent.json').exists()
    assert not (host/'.sync-back-pending').exists()
    uploads.clear()
    fresh = manager(fail)
    with pytest.raises((TimeoutError, file_sync.SyncBackRefused)):
        fresh.sync(force=True)
    assert uploads == []
    assert other.read_text() == 'completed remote work'
    assert local.read_text() == 'original'
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 3)
    recovered = manager()
    recovered.sync(force=True)
    assert local.read_text() == other.read_text() == 'completed remote work'
    assert uploads
    assert not (host/'.sync-back-intent.json').exists()
    assert not (host/'.sync-back-pending').exists()


def test_other_remote_cannot_consume_intent(setup_sync):
    host, local, other, uploads, manager = setup_sync
    def fail(*args):
        raise TimeoutError('offline')
    old = manager(fail)
    old.sync(force=True)
    other.write_text('remote edit')
    old.sync_back()
    saved = (host/'.sync-back-intent.json').read_bytes()
    uploads.clear()
    calls = []
    fresh = manager(lambda *a: calls.append(a), identity='server-b')
    with pytest.raises(file_sync.SyncBackRefused, match='another remote'):
        fresh.sync(force=True)
    assert calls == uploads == []
    assert (host/'.sync-back-intent.json').read_bytes() == saved
    assert other.read_text() == 'remote edit'


def test_corrupt_intent_blocks_upload(setup_sync):
    host, local, other, uploads, manager = setup_sync
    (host/'.sync-back-intent.json').write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        manager().sync(force=True)
    assert uploads == []


def _check_recovery_uses_original_mapping(setup_sync, monkeypatch):
    host, local, other, uploads, manager = setup_sync
    def fail(*args):
        raise TimeoutError('offline')
    old = manager(fail)
    old.sync(force=True)
    other.write_text('remote edit')
    old.sync_back()
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 3)
    manager(files=[]).sync(force=True)
    assert local.read_text() == 'remote edit'
    assert not (host/'.sync-back-intent.json').exists()


def _check_recovery_in_new_process_preserves_unchanged_remote_baseline(setup_sync):
    host, local, other, uploads, manager = setup_sync
    def fail(*args):
        raise TimeoutError('offline')
    old = manager(fail)
    old.sync(force=True)
    # Remote is unchanged; a newer host edit must survive recovery. This needs
    # the previous push hash, not an empty new-process baseline.
    local.write_text('new host edit')
    old.sync_back()
    code = '''
import sys, shutil
from tools.environments import file_sync, ssh_sync_back
file_sync._credential_host_paths = lambda: set()
def download(dest, request, timeout):
    ssh_sync_back.download_changes(['/bin/bash', '-c'], dest, request, timeout)
manager = file_sync.FileSyncManager(lambda: [(sys.argv[1], sys.argv[2])],
    shutil.copy2, lambda *a: None, selective_download_fn=download,
    sync_back_identity='server-a')
manager.sync(force=True)
'''
    result = subprocess.run([sys.executable, '-c', code, str(local), str(other)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert local.read_text() == other.read_text() == 'new host edit'
    assert not (host/'.sync-back-intent.json').exists()


@pytest.mark.linux_only
@pytest.mark.parametrize('failure', ['timeout', 'oversize'])
def test_reconnect_linux(setup_sync, monkeypatch, failure):
    _check_reconnect_recovers_before_upload(setup_sync, monkeypatch, failure)


@pytest.mark.macos_only
@pytest.mark.parametrize('failure', ['timeout', 'oversize'])
def test_reconnect_macos(setup_sync, monkeypatch, failure):
    _check_reconnect_recovers_before_upload(setup_sync, monkeypatch, failure)


@pytest.mark.linux_only
def test_original_mapping_linux(setup_sync, monkeypatch):
    _check_recovery_uses_original_mapping(setup_sync, monkeypatch)


@pytest.mark.macos_only
def test_original_mapping_macos(setup_sync, monkeypatch):
    _check_recovery_uses_original_mapping(setup_sync, monkeypatch)


@pytest.mark.linux_only
def test_new_process_baseline_linux(setup_sync):
    _check_recovery_in_new_process_preserves_unchanged_remote_baseline(setup_sync)


@pytest.mark.macos_only
def test_new_process_baseline_macos(setup_sync):
    _check_recovery_in_new_process_preserves_unchanged_remote_baseline(setup_sync)
