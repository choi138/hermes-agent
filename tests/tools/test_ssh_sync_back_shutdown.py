"""Process shutdown and concurrent sync must finish without starting reader threads."""
import os
import subprocess
import sys
import textwrap
import time

import pytest


def _check_shutdown_syncs_edits_and_unblocks_another_session(tmp_path, monkeypatch):
    script = textwrap.dedent('''        import atexit, hashlib, os, sys, pathlib, threading, time
        from tools.environments import file_sync, ssh_sync_back
        from tools.environments.base import BaseEnvironment
        root = pathlib.Path(sys.argv[1])
        host, remote = root/'host', root/'remote'
        host.mkdir(); remote.mkdir()
        os.environ['HERMES_HOME'] = str(host)
        local, other = host/'result.txt', remote/'result.txt'
        local.write_text('old'); other.write_text('completed remotely')
        file_sync._credential_host_paths = lambda: set()
        file_sync._sync_back_timeout = lambda: 2
        file_sync._sync_back_max_bytes = lambda: 65536
        def download(dest, request, timeout):
            # FileSyncManager holds the real flock here. Give the second process
            # a deterministic chance to contend while this process is exiting.
            (root/'locked').write_text('yes')
            time.sleep(0.4)
            ssh_sync_back.download_changes(['bash', '-c'], dest, request, timeout)
        manager = file_sync.FileSyncManager(
            lambda: [(str(local), str(other))], lambda *a: None, lambda *a: None,
            selective_download_fn=download)
        manager._pushed_hashes = {str(other): hashlib.sha256(b'old').hexdigest()}
        class Finalizer:
            __del__ = BaseEnvironment.__del__
            def cleanup(self):
                manager.sync_back()
        def exit_cleanup():
            def no_threads(*a, **kw):
                raise RuntimeError('cannot start a thread during interpreter shutdown')
            threading.Thread.start = no_threads
            finalizer = Finalizer()
            del finalizer
            (root/'finished').write_text('yes')
        atexit.register(exit_cleanup)
    ''')
    from tools.environments import file_sync
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'host'))
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: set())
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 3)
    with (tmp_path/'stderr').open('w') as errors:
        proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                                stdout=subprocess.DEVNULL, stderr=errors)
        try:
            deadline = time.monotonic() + 6
            while not (tmp_path/'locked').exists():
                assert proc.poll() is None, (tmp_path/'stderr').read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            second = file_sync.FileSyncManager(lambda: [], lambda *a: None, lambda *a: None,
                                               selective_download_fn=lambda *a: None)
            second.sync(force=True)
            # A second session can run a command and read the returned result.
            result = subprocess.run([sys.executable, '-c',
                'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())',
                str(tmp_path/'host/result.txt')], capture_output=True, text=True, timeout=2)
            assert result.returncode == 0
            assert result.stdout.strip() == 'completed remotely', (tmp_path/'stderr').read_text()
            proc.wait(timeout=3)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
    assert proc.returncode == 0
    assert (tmp_path/'finished').read_text() == 'yes'
    assert not (tmp_path/'host/.sync-back-pending').exists()


def _check_transport_limits_kill_and_reap_without_threads(tmp_path, monkeypatch, behavior):
    import threading
    import time
    from tools.environments import ssh_sync_back
    from tools.environments.file_sync import SyncBackRefused
    commands = {
        'silent': 'exec sleep 10',
        'oversize': "exec python3 -c 'import sys; sys.stdout.buffer.write(bytes(131072)); sys.stdout.flush()'",
        'eof_then_hang': 'exec 1>&-; exec sleep 10',
    }
    def forbid_thread(*args, **kwargs):
        raise AssertionError('transport must not create threads')
    monkeypatch.setattr(threading.Thread, 'start', forbid_thread)
    children = []
    popen = subprocess.Popen
    def capture(*args, **kwargs):
        child = popen(*args, **kwargs); children.append(child); return child
    monkeypatch.setattr(subprocess, 'Popen', capture)
    dest = tmp_path/'download.tar'
    start = time.monotonic()
    with pytest.raises(SyncBackRefused if behavior == 'oversize' else TimeoutError):
        ssh_sync_back.download_changes(['bash', '-c', commands[behavior], '--'], dest,
            {'roots': [], 'excluded': [], 'hashes': {}, 'max_bytes': 1024}, 0.3)
    assert time.monotonic() - start < 2
    assert all(child.poll() is not None for child in children)
    assert dest.stat().st_size <= 1024


def _check_transport_during_actual_interpreter_finalization(tmp_path):
    script = textwrap.dedent('''
        import pathlib, sys, traceback
        from tools.environments.ssh_sync_back import download_changes
        root = pathlib.Path(sys.argv[1])
        remote = root/'remote'; remote.mkdir()
        (remote/'result').write_text('shutdown result')
        request = {'roots': [str(remote)], 'hashes': {}, 'excluded': [], 'max_bytes': 65536}
        class Finalizer:
            def __del__(self):
                (root/'finalizing').write_text(str(sys.is_finalizing()))
                try:
                    download_changes(['/bin/bash', '-c'], root/'result.tar', request, 1)
                except BaseException:
                    (root/'failure').write_text(traceback.format_exc())
        finalizer = Finalizer()
    ''')
    proc = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
    assert proc.returncode == 0
    assert (tmp_path/'finalizing').read_text() == 'True'
    assert not (tmp_path/'failure').exists(), (tmp_path/'failure').read_text()
    import tarfile
    with tarfile.open(tmp_path/'result.tar') as archive:
        assert archive.extractfile(str(tmp_path/'remote/result').lstrip('/')).read() == b'shutdown result'


@pytest.mark.linux_only
def test_shutdown_syncs_edits_and_unblocks_another_session_linux(tmp_path, monkeypatch):
    _check_shutdown_syncs_edits_and_unblocks_another_session(tmp_path, monkeypatch)


@pytest.mark.linux_only
@pytest.mark.parametrize('behavior', ['silent', 'oversize', 'eof_then_hang'])
def test_transport_limits_kill_and_reap_without_threads_linux(tmp_path, monkeypatch, behavior):
    _check_transport_limits_kill_and_reap_without_threads(tmp_path, monkeypatch, behavior)


@pytest.mark.linux_only
def test_transport_during_actual_interpreter_finalization_linux(tmp_path):
    _check_transport_during_actual_interpreter_finalization(tmp_path)


@pytest.mark.macos_only
def test_shutdown_syncs_edits_and_unblocks_another_session_macos(tmp_path, monkeypatch):
    _check_shutdown_syncs_edits_and_unblocks_another_session(tmp_path, monkeypatch)


@pytest.mark.macos_only
@pytest.mark.parametrize('behavior', ['silent', 'oversize', 'eof_then_hang'])
def test_transport_limits_kill_and_reap_without_threads_macos(tmp_path, monkeypatch, behavior):
    _check_transport_limits_kill_and_reap_without_threads(tmp_path, monkeypatch, behavior)


@pytest.mark.macos_only
def test_transport_during_actual_interpreter_finalization_macos(tmp_path):
    _check_transport_during_actual_interpreter_finalization(tmp_path)
