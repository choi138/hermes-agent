import pytest
from tools.process_registry import ProcessRegistry, ProcessSession


def test_non_ssh_backend_loss_finishes(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from types import SimpleNamespace
    from unittest.mock import Mock
    r = ProcessRegistry()
    s = ProcessSession(id='proc_gone', command='true')
    r._running[s.id] = s
    def fail(*a, **k):
        raise RuntimeError('backend deleted')
    # Fail deterministically if the poller retries instead of finishing.
    s._completion_event = SimpleNamespace(wait=lambda *a: pytest.fail('permanent backend loss retried'), set=Mock())
    monkeypatch.setattr('tools.process_registry.time.sleep', lambda _: None)
    r._env_poller_loop(s, SimpleNamespace(execute=fail), '/log', '/pid', '/exit')
    assert s.exited and s.completion_reason == 'lost'
    s._completion_event.set.assert_called()


def test_remote_outage_persists_safe_diagnostics(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = registry._new_session('true', '', '', '', None)
    session.remote_root = '/tmp/isolated'
    session.remote_connection = {'profile_home': str(tmp_path)}
    session.last_observed_at = 123.0
    registry._running[session.id] = session
    def failed(*args):
        raise ConnectionError('connection refused')
    session._observation_wake.wait = lambda delay: setattr(session, 'exited', True)
    poll_remote(registry, session, SimpleNamespace(_observe_process=failed))
    row = json.loads((tmp_path / 'processes.json').read_text())[0]
    assert row['last_observed_at'] == 123.0
    assert 'connection refused' in row['observation_error']
    assert row['observation_retry_at'] > 0 and row['observation_operation'] == 'ssh_observe'


def test_log_outage_does_not_hide_durable_reservation_failure(monkeypatch, tmp_path):
    import json, sqlite3
    from types import SimpleNamespace
    from tools import process_registry_followups as ledger
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    ledger.pending()
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_locked_reserve', command='true', remote_root='/tmp/isolated',
                             notify_on_complete=True, watcher_platform='telegram',
                             profile_home=str(tmp_path))
    registry._running[session.id] = session
    blocked = sqlite3.connect(tmp_path / 'state.db')
    blocked.execute('BEGIN IMMEDIATE')
    observation = {'state': 'exited', 'exit_code': 0, 'error': 'log_unavailable: errno=2'}
    def stop(_delay):
        blocked.rollback()
        session.exited = True  # End this isolated observer after its first failed reservation.
    session._observation_wake.wait = stop
    try:
        poll_remote(registry, session, SimpleNamespace(_observe_process=lambda *a: observation))
    finally:
        blocked.rollback(); blocked.close()
    row = json.loads((tmp_path / 'processes.json').read_text())[0]
    assert session.id in registry._running and ledger.pending() == []
    assert 'log_unavailable' in row['observation_error']
    assert 'database is locked' in row['observation_error']


def test_local_observation_capacity_is_not_remote_outage(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from tools.environments.ssh_process import ObservationCapacityBusy
    from tools.process_registry_remote import poll_remote
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_capacity', command='true', remote_root='/tmp/isolated',
                             observation_state='running', last_observed_at=123.0)
    def busy(*args):
        raise ObservationCapacityBusy()
    session._observation_wake.wait = lambda delay: setattr(session, 'exited', True)
    registry = ProcessRegistry()
    poll_remote(registry, session, SimpleNamespace(_observe_process=busy))
    assert session.observation_state == 'running' and session.last_observed_at == 123.0
    assert not session.observation_error and registry.completion_queue.empty()


@pytest.mark.parametrize('protocol', [None, 2, 3])
def test_restored_ssh_receipt_keeps_logs_and_followup(monkeypatch, tmp_path, protocol):
    from tools.process_registry_results import save_completed_result, load_completed_results
    from tools import process_registry_followups as ledger
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_SESSION_ID', 'owner')
    session = ProcessSession(id='proc_receipt', command='true', parent_session_id='owner',
        remote_root='/tmp/isolated', remote_connection={'profile_home': str(tmp_path)},
        remote_identity={} if protocol is None else {'protocol': protocol},
        exited=True, exit_code=0, notify_on_complete=True)
    ledger.reserve(session)
    save_completed_result(session, strict=True)
    restored = load_completed_results()[session.id]
    registry = ProcessRegistry()
    registry._finished[restored.id] = restored
    result = registry.read_log(restored.id)
    if protocol is None:
        assert 'remote_log_path' not in result
    else:
        expected = '/tmp/isolated/hermes_bg_' + session.id
        if protocol == 3:
            expected += '.claim/process'
        assert result['remote_log_path'] == expected + '.log'
    assert registry.poll(restored.id)['followup']['phase'] == 'pending'


def test_observation_outage_is_diagnostic_not_completion():
    from agent.notification_presentation import diagnostic_process_event
    assert diagnostic_process_event({'type': 'observation_unavailable'})
    assert not diagnostic_process_event({'type': 'completion', 'exit_code': 0})


def test_local_completion_is_published_after_receipt_write(monkeypatch, tmp_path):
    import threading
    from tools import process_registry as module
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = ProcessRegistry()
    session = ProcessSession(id='proc_local_publish', command='true', profile_home=str(tmp_path))
    session.append_output('retained output')
    registry._running[session.id] = session
    entered, release = threading.Event(), threading.Event()
    original = module.save_completed_result
    errors = []
    def blocked_save(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'test did not release receipt writer'
        original(*args, **kwargs)
    monkeypatch.setattr(module, 'save_completed_result', blocked_save)
    def finish():
        try:
            registry._finish_exited(session, 0)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=finish)
    worker.start()
    try:
        assert entered.wait(5)
        assert registry.poll(session.id)['status'] == 'running'
        assert not session._completion_event.is_set()
        assert not (tmp_path / 'logs/process-results/proc_local_publish.json').exists()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert registry.poll(session.id)['status'] == 'exited'
    assert session._completion_event.is_set()
    import json
    receipt = json.loads((tmp_path / 'logs/process-results/proc_local_publish.json').read_text())
    assert receipt['output'] == 'retained output' and receipt['exit_code'] == 0
