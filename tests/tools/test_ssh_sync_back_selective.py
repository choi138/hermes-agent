"""SSH cleanup returns changed mapped files without copying an unrelated remote home."""
import tarfile
import time

import pytest

from tools.environments import file_sync, ssh


def make_env(tmp_path, monkeypatch):
    host, remote = tmp_path / 'host', tmp_path / 'remote'
    monkeypatch.setenv('HERMES_HOME', str(host))
    host.mkdir(); (remote / '.hermes' / 'skills').mkdir(parents=True)
    skill = host / 'skills' / 'existing.txt'
    skill.parent.mkdir(); skill.write_text('initial')
    unchanged = skill.with_name('unchanged.txt'); unchanged.write_text('unchanged')
    secret = host / 'skills' / 'auth.json'; secret.write_text('host secret')
    mapping = [(str(p), str(remote / '.hermes' / p.relative_to(host)))
               for p in (skill, unchanged, secret)]
    monkeypatch.setattr(ssh, 'iter_sync_files', lambda *_: mapping)
    monkeypatch.setattr(file_sync, '_credential_host_paths', lambda: {str(secret)})
    monkeypatch.setattr(ssh.SSHEnvironment, '_establish_connection', lambda self: None)
    monkeypatch.setattr(ssh.SSHEnvironment, '_detect_remote_home', lambda self: str(remote))
    monkeypatch.setattr(ssh.SSHEnvironment, '_ensure_remote_dirs', lambda self: None)
    monkeypatch.setattr(ssh.SSHEnvironment, 'init_session', lambda self: None)
    monkeypatch.setattr(ssh.SSHEnvironment, '_build_ssh_command', lambda self, **kw: ['bash', '-c'])
    env = ssh.SSHEnvironment(host='test', user='test')
    # Avoid BaseEnvironment.__del__ running a second cleanup during interpreter shutdown.
    env.cleanup = lambda: None
    return env, host, remote


def test_changed_and_new_files_return_without_unrelated_or_unchanged_bytes(tmp_path, monkeypatch):
    env, host, remote = make_env(tmp_path, monkeypatch)
    root = remote / '.hermes'
    (root / 'skills' / 'existing.txt').write_text('edited remotely')
    new = root / 'skills' / 'new name\nwith newline.txt'; new.write_text('new result')
    (root / 'skills' / 'link').symlink_to(root / 'skills' / 'auth.json')
    (root / 'logs').mkdir()
    (root / 'logs' / 'huge.bin').write_bytes(b'x' * (2 * 1024 * 1024))
    (root / 'skills' / 'auth.json').write_text('remote secret')
    monkeypatch.setattr(file_sync, '_sync_back_max_bytes', lambda: 128 * 1024)
    archives = []
    original_open = tarfile.open

    def inspect(*args, **kwargs):
        archive = original_open(*args, **kwargs)
        if args and str(args[0]).endswith('.tar') and archive.mode == 'r':
            archives.append(archive.getnames())
        return archive

    monkeypatch.setattr(tarfile, 'open', inspect)
    env._sync_manager.sync_back()
    assert (host / 'skills' / 'existing.txt').read_text() == 'edited remotely'
    assert (host / 'skills' / new.name).read_text() == 'new result'
    assert (host / 'skills' / 'auth.json').read_text() == 'host secret'
    assert not (host / 'skills' / 'link').exists()
    assert archives and set(archives[0]) == {
        str(root / 'skills' / 'existing.txt').lstrip('/'), str(new).lstrip('/')}


@pytest.mark.parametrize('failure', ['lock', 'manager_lock', 'timeout', 'oversize'])
def test_cleanup_failures_are_bounded_and_preserve_local_files(tmp_path, monkeypatch, failure, caplog):
    env, host, remote = make_env(tmp_path, monkeypatch)
    monkeypatch.setattr(file_sync, '_sync_back_timeout', lambda: 0.2, raising=False)
    monkeypatch.setattr(file_sync, '_sync_back_max_bytes', lambda: 32 * 1024)
    (remote / '.hermes' / 'skills' / 'existing.txt').write_bytes(b'x' * (128 * 1024))
    lock = None
    manager_locked = failure == 'manager_lock' or (failure == 'lock' and file_sync.fcntl is None)
    if manager_locked:
        env._sync_manager._transaction_lock.acquire()
    if failure == 'lock' and file_sync.fcntl is not None:
        lock = (host / '.sync.lock').open('w')
        file_sync.fcntl.flock(lock, file_sync.fcntl.LOCK_EX)
    elif failure == 'timeout':
        monkeypatch.setattr(env, '_build_ssh_command', lambda **kw: ['bash', '-c', 'exec sleep 5', '--'])
    start = time.monotonic()
    try:
        env._sync_manager.sync_back()
    finally:
        if manager_locked:
            env._sync_manager._transaction_lock.release()
        if lock:
            file_sync.fcntl.flock(lock, file_sync.fcntl.LOCK_UN); lock.close()
    assert time.monotonic() - start < 2
    assert (host / 'skills' / 'existing.txt').read_text() == 'initial'
    assert 'retrying' not in caplog.text
    assert 'sync_back:' in caplog.text
