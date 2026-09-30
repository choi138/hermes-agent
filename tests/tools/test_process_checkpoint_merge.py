import json
import os
import subprocess
import sys
import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.parametrize('payload', [{}, '', 0, [None]])
def test_corrupt_primary_checkpoint_is_rejected_before_ssh_sidecar_merge(tmp_path, payload):
    from tools.process_registry_checkpoint import _read_checkpoint, _write_checkpoint_files
    path = tmp_path / 'processes.json'
    sidecar = path.with_name(path.name + '.ssh-v2')
    path.write_text(json.dumps(payload))
    sidecar.write_text(json.dumps([{'session_id': 'proc_kept', 'remote_root': '/tmp/isolated'}]))
    before = (path.read_bytes(), sidecar.read_bytes())
    with pytest.raises(ValueError, match='Invalid process checkpoint'):
        _read_checkpoint(path)
    with pytest.raises(ValueError, match='Invalid process checkpoint'):
        _write_checkpoint_files(path, [])
    assert (path.read_bytes(), sidecar.read_bytes()) == before


def test_other_process_checkpoint_survives_local_completion(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    first = registry._new_session('first', '', '', '', None)
    registry._running[first.id] = first
    registry._write_checkpoint(strict=True)
    subprocess.run([sys.executable, '-c', '''
from tools.process_registry import ProcessRegistry
r=ProcessRegistry()
s=r._new_session('second','','','',None)
r._running[s.id]=s
r._write_checkpoint(strict=True)
'''], check=True, env=os.environ.copy())
    rows = json.loads((tmp_path / 'processes.json').read_text())
    assert {row['command'] for row in rows} == {'first', 'second'}
    registry._finish_exited(first, 0)
    assert [row['command'] for row in json.loads((tmp_path / 'processes.json').read_text())] == ['second']


def test_checkpoint_profile_a_b_a(monkeypatch, tmp_path):
    from gateway.run import _profile_runtime_scope
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'a'))
    registry = ProcessRegistry()
    for profile in ('a', 'b', 'a'):
        home = tmp_path / profile
        with _profile_runtime_scope(home):
            session = registry._new_session(profile, '', '', '', None)
            session.pid = os.getpid()
            session.host_start_time = registry._safe_host_start_time(os.getpid())
            registry._running[session.id] = session
            registry._write_checkpoint(strict=True)
            assert {row['command'] for row in json.loads((home / 'processes.json').read_text())} == {profile}

    recovered = ProcessRegistry()
    expected = set()
    for profile in ('a', 'b', 'a'):
        with _profile_runtime_scope(tmp_path / profile):
            recovered.recover_from_checkpoint()
            expected.update(s.id for s in registry._running.values() if s.command == profile)
            assert set(recovered._running) == expected
            recovered._write_checkpoint(strict=True)
            assert {row['command'] for row in json.loads((tmp_path / profile / 'processes.json').read_text())} == {profile}


def test_legacy_unowned_row_never_adopted_by_first_profile(monkeypatch, tmp_path):
    registry = ProcessRegistry()
    row = dict(session_id='proc_legacy', command='legacy', pid=os.getpid(),
               host_start_time=registry._safe_host_start_time(os.getpid()), pid_scope='host')
    for profile in ('a', 'b'):
        home = tmp_path / profile
        home.mkdir()
        (home / 'processes.json').write_text(json.dumps([row]))
    for profile in ('a', 'b', 'a'):
        home = tmp_path / profile
        monkeypatch.setenv('HERMES_HOME', str(home))
        assert registry.recover_from_checkpoint() == 0
        registry._write_checkpoint(strict=True)
        assert not registry._running
        retained = json.loads((home / 'processes.json').read_text())
        assert retained == [row]


def test_head_reader_rollback_preserves_versioned_remote_rows(monkeypatch, tmp_path):
    import types
    from tools.process_registry_checkpoint import _read_checkpoint
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = registry._new_session('remote', '', '', '', None)
    session.pid_scope, session.pid = 'sandbox', 12345
    session.remote_root = '/tmp/isolated'
    session.remote_connection = {'profile_home': str(tmp_path)}
    registry._running[session.id] = session
    registry._write_checkpoint(strict=True)
    import importlib.util
    from pathlib import Path
    fixture = Path(__file__).parents[1] / 'fixtures/process_checkpoint_legacy.py'
    spec = importlib.util.spec_from_file_location('rollback_checkpoint', fixture)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    legacy = ProcessRegistry()
    legacy._write_checkpoint = types.MethodType(old.ProcessCheckpointMixin._write_checkpoint, legacy)
    old.ProcessCheckpointMixin.recover_from_checkpoint(legacy)
    assert json.loads((tmp_path / 'processes.json').read_text()) == []
    assert _read_checkpoint(tmp_path / 'processes.json')[0]['session_id'] == session.id
    registry._running.clear()
    registry._write_checkpoint(strict=True)
    assert _read_checkpoint(tmp_path / 'processes.json') == []


def test_overlapping_restart_retries_after_previous_owner_exits(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    owner = subprocess.Popen([sys.executable, '-c', """
import os,sys
from tools.process_registry import ProcessRegistry
r=ProcessRegistry()
s=r._new_session('still running','','','',None)
s.pid=int(sys.argv[1]); s.host_start_time=r._safe_host_start_time(s.pid)
r._running[s.id]=s
r._write_checkpoint(strict=True)
print(s.id,flush=True)
sys.stdin.readline()
""", str(os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        execution = owner.stdout.readline().strip()
        assert execution.startswith('proc_')
        recovered = ProcessRegistry()
        assert recovered.recover_from_checkpoint() == 0
        assert execution not in recovered._running
        owner.communicate('exit\n', timeout=5)
        assert recovered.retry_checkpoint_recovery() == 1
        assert execution in recovered._running
        assert recovered.retry_checkpoint_recovery() == 0
    finally:
        if owner.poll() is None:
            owner.communicate('exit\n', timeout=5)



def test_failed_main_checkpoint_restores_authoritative_sidecar(monkeypatch, tmp_path):
    import pytest, utils
    from tools.process_registry_checkpoint import _read_checkpoint
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry=ProcessRegistry()
    session=registry._new_session('not dispatched','','','',None)
    session.remote_root='/tmp/isolated'; session.pid_scope='sandbox'
    registry._running[session.id]=session
    original=utils.atomic_json_write
    def failed_main(path,*args,**kwargs):
        if path.name == 'processes.json': raise OSError('main unavailable')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(utils,'atomic_json_write',failed_main)
    with pytest.raises(OSError): registry._write_checkpoint(strict=True)
    assert _read_checkpoint(tmp_path/'processes.json') == []


def test_initial_recovery_read_failure_retries(monkeypatch, tmp_path):
    from tools import process_registry_checkpoint as checkpoint
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    producer=ProcessRegistry()
    session=producer._new_session('adopt','','','',None)
    session.pid=os.getpid(); session.host_start_time=producer._safe_host_start_time(session.pid)
    producer._running[session.id]=session; producer._write_checkpoint(strict=True)
    recovered=ProcessRegistry()
    original=checkpoint._read_checkpoint
    def unavailable(path): raise OSError('transient read outage')
    monkeypatch.setattr(checkpoint,'_read_checkpoint',unavailable)
    assert recovered.recover_from_checkpoint() == 0
    monkeypatch.setattr(checkpoint,'_read_checkpoint',original)
    assert recovered.retry_checkpoint_recovery() == 1
    assert session.id in recovered._running



def test_legacy_local_pid_ownership_migrates_a_b_a(monkeypatch, tmp_path):
    from pathlib import Path
    a,b=tmp_path/'a',tmp_path/'b'
    a.mkdir(); b.mkdir()
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],env={**os.environ,'HERMES_HOME':str(a)})
    try:
        registry=ProcessRegistry()
        row=dict(session_id='proc_legacy_owned',command='legacy',pid=child.pid,
            host_start_time=registry._safe_host_start_time(child.pid),pid_scope='host')
        for home in [a,b]: (home/'processes.json').write_text(json.dumps([row]))
        monkeypatch.setenv('HERMES_HOME',str(b))
        assert registry.recover_from_checkpoint()==0
        monkeypatch.setenv('HERMES_HOME',str(a))
        assert registry.recover_from_checkpoint()==1
        assert registry._running[row['session_id']].profile_home==str(a)
        monkeypatch.setenv('HERMES_HOME',str(b))
        registry._write_checkpoint(strict=True)
        assert json.loads((b/'processes.json').read_text())==[row]
        monkeypatch.setenv('HERMES_HOME',str(a))
        assert registry.recover_from_checkpoint()==0
        assert len(registry._running)==1
    finally:
        child.terminate();child.wait(timeout=5)


def test_changed_ssh_config_remains_queryable_and_recovery_can_resume(monkeypatch, tmp_path):
    from tools.environments import ssh
    from tools.environments.ssh import SSHEnvironment
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    config = ['hostname original\n']
    monkeypatch.setattr(ssh, 'run_capture', lambda *a, **k: subprocess.CompletedProcess([], 0, config[0], ''))
    env = SSHEnvironment('alias', 'tester', _status_only=True)
    original = ProcessRegistry()
    session = original._new_session('non-idempotent command', '', '', '', None)
    session.pid_scope = 'sandbox'
    session.remote_root = '/tmp/isolated'
    session.remote_connection = dict(host='alias', user='tester', port=22, key_path='',
        profile_home=str(tmp_path), identity=env._connection_identity)
    original._running[session.id] = session
    original._write_checkpoint(strict=True)
    config[0] = 'hostname different\n'
    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 0
    status = recovered.poll(session.id)
    assert status['status'] == 'unknown'
    assert status['observation_state'] == 'identity_mismatch'
    assert 'do not rerun' in status['next_action']
    assert recovered.read_log(session.id)['status'] == 'unknown'
    adopted = []
    def track(s, *args):
        adopted.append(s)
        recovered._running[s.id] = s
    monkeypatch.setattr(recovered, '_track_started', track)
    config[0] = 'hostname original\n'
    assert recovered.retry_checkpoint_recovery() == 1
    assert len(adopted) == 1 and adopted[0].env_ref is not None
    assert recovered.recover_from_checkpoint() == 0


def test_incomplete_owned_remote_checkpoint_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    row = dict(session_id='proc_incomplete', command='never replay', pid_scope='sandbox',
               remote_root='/tmp/isolated', profile_home=str(tmp_path))
    path = tmp_path/'processes.json'
    path.write_text(json.dumps([row]))
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint() == 0
    for checkpoint in (path, path.with_name('processes.json.ssh-v2')):
        assert json.loads(checkpoint.read_text())[0]['session_id'] == row['session_id']
    registry._write_checkpoint(strict=True)
    assert json.loads(path.read_text())[0]['remote_root'] == row['remote_root']
