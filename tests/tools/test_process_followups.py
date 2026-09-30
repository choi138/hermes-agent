"""Durable admission fencing, restart windows and cancellation against a real SQLite store."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from tools.process_registry import ProcessSession
from tools import process_registry_followups as followups


def test_durable_cancel_completion_preserves_direct_group_scope(monkeypatch, tmp_path):
    import json
    from tools.process_registry import ProcessRegistry
    from tools.process_registry_notifications import format_process_notification
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_scoped_cancel', command='sleep 60',
        session_key='owner', parent_session_id='owner', remote_root='/tmp/isolated',
        notify_on_complete=True, watcher_platform='telegram',
        cancel_requested=True, cancel_confirmed=True, termination_source='Hermes')
    registry = ProcessRegistry()
    registry._running[session.id] = session
    registry._finish_exited(session, -15)
    payload = json.loads(followups.pending()[0]['payload'])
    assert payload['cancellation_scope'] == 'direct_process_group'
    assert payload['execution_tree_termination_confirmed'] is False
    assert 'detached descendants may remain' in format_process_notification(payload)


def test_cancelled_reconciliation_is_terminal_even_after_owner_dies(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_dead_cancel', command='true', session_key='owner')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    monkeypatch.setattr(followups, '_owner_alive', lambda _: False)
    assert followups.pending()[0]['phase'] == 'needs_reconciliation'
    followups.cancel_for_session('owner')
    assert followups.pending() == []
    assert followups.get_state(session.id)['phase'] == 'cancelled'
    assert not followups.attention_authorized(session.id, row['token'])


def test_owner_probe_cannot_expire_a_refreshed_verification(monkeypatch, tmp_path):
    import time
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_refreshed', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    with followups._db() as db:
        db.execute('UPDATE process_followups SET updated_at=?', (time.time() - 1000,))
    def alive(owner):
        with followups._db() as db:
            db.execute('UPDATE process_followups SET updated_at=?', (time.time(),))
        return True
    monkeypatch.setattr(followups, '_owner_alive', alive)
    assert followups.pending() == []
    assert followups.get_state(session.id)['phase'] == 'running'
    assert followups.finish(session.id, row['token'], {'final_response': 'done'})
    assert followups.get_state(session.id)['phase'] == 'turn_finished'


def test_duplicate_admission_and_execution_and_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_test', command='true', session_key='owner', started_at=1)
    followups.reserve(session)
    row = followups.pending()[0]
    with ThreadPoolExecutor(4) as pool:
        admitted = list(pool.map(lambda _: followups.admission(session.id, row['token']), range(4)))
    assert admitted.count(True) == 1
    # Repeated adapter delivery, including restart after admission, cannot execute twice.
    with ThreadPoolExecutor(4) as pool:
        begun = list(pool.map(lambda _: followups.begin(session.id, row['token']), range(4)))
    assert begun.count(True) == 1
    assert followups.pending() == []  # uncertain running turn is never automatically repeated
    followups.cancel_for_session('owner')
    assert not followups.finish(session.id, row['token'], {})
    assert followups.get_state(session.id)['phase'] == 'cancelled'
    # Completion after /stop is fenced even when no row existed at cancellation time.
    late = ProcessSession(id='proc_late', command='true', session_key='owner', started_at=1)
    followups.reserve(late)
    assert followups.get_state(late.id)['phase'] == 'cancelled'


def test_profile_a_b_a_isolation(monkeypatch, tmp_path):
    from gateway.run import _profile_runtime_scope
    session = ProcessSession(id='proc_same', command='true')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'a'))
    with _profile_runtime_scope(tmp_path / 'a'):
        followups.reserve(session)
        token = followups.pending()[0]['token']
    with _profile_runtime_scope(tmp_path / 'b'):
        assert not followups.begin(session.id, token)
        followups.reserve(session)
        assert followups.pending()[0]['token'] != token
    with _profile_runtime_scope(tmp_path / 'a'):
        assert followups.pending()[0]['token'] == token


def test_owner_probe_does_not_hold_database_lock(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_probe', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    assert followups.begin(session.id, row['token'])
    def inspect_owner(owner):
        # A second writer must succeed while an OS probe is in progress.
        followups.cancel(session.id)
        return False
    monkeypatch.setattr(followups, '_owner_alive', inspect_owner)
    assert followups.pending() == []
    assert followups.get_state(session.id)['phase'] == 'cancelled'


def test_dead_owner_requires_reconciliation_without_replay(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_dead', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    assert followups.begin(session.id, row['token'])
    monkeypatch.setattr(followups, '_owner_alive', lambda _: False)
    followups.pending()
    assert followups.get_state(session.id)['phase'] == 'needs_reconciliation'
    assert not followups.admission(session.id, row['token'])
    assert not followups.begin(session.id, row['token'])


def test_cancel_after_dispatch_retains_effects_for_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_race', command='true', session_key='owner')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    assert followups.begin(session.id, row['token'])
    assert followups.authorized(session.id, row['token'])
    followups.cancel_for_session('owner')
    assessment = {'outcome': 'approval_wait', 'evidence': ['Cancellation raced the observed result'],
                  'next_action': 'Inspect external effects'}
    import json
    result = {'final_response': 'possible external effects\n```process_verification\n' +
              json.dumps(assessment) + '\n```'}
    assert followups.finish(session.id, row['token'], result, 'turn-race')
    state = followups.get_state(session.id)
    assert state['phase'] == 'cancelled'
    assert state['verification'] == {**assessment, 'repair_attempts': 0}
    with followups._db() as db:
        payload = json.loads(db.execute('SELECT payload FROM process_followups WHERE execution_id=?',
            (session.id,)).fetchone()['payload'])
    assert payload['followup_turn_id'] == 'turn-race'
    assert not followups.admission(session.id, row['token'])


def test_live_stalled_turn_requests_attention_without_replay(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_stall', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    assert followups.begin(session.id, row['token'])
    with followups._db() as db:
        db.execute("UPDATE process_followups SET updated_at=0 WHERE execution_id=?", (session.id,))
    rows = followups.pending()
    assert rows[0]['phase'] == 'needs_reconciliation'
    assert not followups.begin(session.id, row['token'])


def test_owner_identity_failure_is_durable_attention(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_owner_failure', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    def unavailable(pid):
        raise OSError('process identity unavailable')
    monkeypatch.setattr(followups, '_owner_for_pid', unavailable)
    assert not followups.begin(session.id, row['token'])
    assert followups.get_state(session.id)['phase'] == 'needs_reconciliation'
    assert not followups.admission(session.id, row['token'])


def test_return_without_verification_cannot_close_followup(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_unverified', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token'])
    assert followups.begin(session.id, row['token'])
    assert followups.finish(session.id, row['token'], {'final_response': 'Done'}, 'turn')
    followups.report_state(session.id, row['token'], delivered=True)
    assert followups.get_state(session.id)['phase'] == 'needs_reconciliation'
    assert followups.get_state(session.id)['verification']['outcome'] == 'unverified'


def test_verified_failure_and_approval_are_distinct_durable_outcomes(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for outcome, expected in [('verified', 'reported'), ('verification_failed', 'verification_failed'),
                              ('approval_wait', 'approval_wait')]:
        session = ProcessSession(id='proc_' + outcome, command='true')
        followups.reserve(session)
        row = next(r for r in followups.pending() if r['execution_id'] == session.id)
        assert followups.admission(session.id, row['token'])
        assert followups.begin(session.id, row['token'])
        assessment = dict(outcome=outcome, evidence=['checked artifact'],
                          next_action='none' if outcome == 'verified' else 'operator action', repair_attempts=0)
        result = {'final_response': '```process_verification\n' + json.dumps(assessment) + '\n```'}
        assert followups.finish(session.id, row['token'], result, 'turn')
        followups.report_state(session.id, row['token'], delivered=True)
        state = followups.get_state(session.id)
        assert state['phase'] == expected
        assert state['verification'] == assessment


def test_completed_verification_survives_late_cancellation(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_completed', command='true', session_key='owner')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    result = {'final_response': '```process_verification\n{"outcome":"verified","evidence":["artifact checked"],"next_action":"none"}\n```'}
    assert followups.finish(session.id, row['token'], result, 'done-turn')
    followups.cancel(session.id)
    followups.cancel_for_session('owner')
    followups.report_state(session.id, row['token'], delivered=True)
    assert followups.get_state(session.id)['phase'] == 'reported'


def test_active_lease_keeps_slow_verification_settleable(monkeypatch, tmp_path):
    import time
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_slow', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    assert followups.authorized(session.id, row['token'])
    with followups.verification_lease(session.id, row['token'], interval=.01):
        with followups._db() as db:
            db.execute('UPDATE process_followups SET updated_at=0')
        deadline = time.monotonic() + 2
        while followups.get_state(session.id)['updated_at'] == 0 and time.monotonic() < deadline:
            time.sleep(.01)
        assert followups.get_state(session.id)['updated_at'] > 0
        assert followups.pending() == []
        assert followups.finish(session.id, row['token'], {'final_response': 'finished'}, 'turn')
    assert followups.get_state(session.id)['phase'] == 'turn_finished'


def test_failure_and_approval_need_action_and_repair_budget_is_system_owned():
    import json
    for outcome in ['verification_failed', 'approval_wait']:
        assessment = dict(outcome=outcome, evidence=['checked output'], next_action='none')
        result = {'final_response': '```process_verification\n' + json.dumps(assessment) + '\n```'}
        assert followups.verification_outcome(result)['outcome'] == 'unverified'
        assessment['next_action'] = 'Inspect the missing artifact before retrying'
        result['final_response'] = '```process_verification\n' + json.dumps(assessment) + '\n```'
        actual = followups.verification_outcome(result)
        assert actual['outcome'] == outcome and actual['repair_attempts'] == 0


def test_durable_payload_and_result_scrub_bearer_in_all_text(monkeypatch, tmp_path):
    import json
    from tools.process_registry import ProcessSession
    from tools.process_registry_results import save_completed_result
    from tools import process_registry_followups as ledger
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    secret = 'opaquecredential1234567890'
    session = ProcessSession(id='proc_secret', command='echo Bearer ' + secret,
        output_buffer='Bearer ' + secret, verification_scope={'authorized_command': 'Bearer ' + secret})
    ledger.reserve(session)
    assert secret not in ledger.pending()[0]['payload']
    save_completed_result(session, strict=True)
    assert secret not in (tmp_path / 'logs/process-results/proc_secret.json').read_text()


def test_cancelled_dispatch_restart_and_unverified_result_never_reenter(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for boundary in ('restart', 'finished'):
        session = ProcessSession(id='proc_cancel_' + boundary, command='true', session_key='owner')
        followups.reserve(session)
        row = followups.pending()[0]
        assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
        assert followups.authorized(session.id, row['token'])
        if boundary == 'finished':
            followups.finish(session.id, row['token'], {'final_response': 'Done'}, 'turn')
        followups.cancel(session.id)
        if boundary == 'restart':
            followups.require_reconciliation(session.id, row['token'])
        else:
            followups.report_state(session.id, row['token'], delivered=True)
        assert followups.get_state(session.id)['phase'] == 'cancelled'
        assert followups.pending() == []


def test_reason_and_errno_redaction_and_accepted_instruction(monkeypatch, tmp_path):
    import re
    from tools.environments.ssh_process import safe_error
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_redaction', command='true')
    followups.reserve(session)
    row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    secret = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh'
    reason = 'https://api.telegram.org/bot' + secret + '/sendMessage'
    followups.finish(session.id, row['token'], {'failed': True, 'failure_reason': reason})
    assert secret not in followups.get_state(session.id)['reason']
    error = safe_error(OSError(13, reason))
    assert 'errno=13' in error and secret not in error
    example = re.search(r'\{.*\}', followups.VERIFICATION_INSTRUCTION).group()
    outcome = followups.verification_outcome({'final_response': '```process_verification\n' + example + '\n```'})
    assert outcome['outcome'] == 'verified'
    with followups.verification_lease(session.id, row['token']):
        assert session.id in followups.verification_tool_block('process_manage', {'action':'log','session_id':'wrong'})


@pytest.mark.parametrize('deferred', [False, True])
@pytest.mark.parametrize('delivered,receipt_reason', [(True, ''), (False, 'Verification turn ended without a durable report receipt; inspect the conversation before retrying')])
def test_failed_verification_report_retains_original_cause(monkeypatch, tmp_path, delivered, receipt_reason, deferred):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_failed_reason', command='true')
    followups.reserve(session); row = followups.pending()[0]
    assert followups.admission(session.id, row['token']) and followups.begin(session.id, row['token'])
    followups.finish(session.id, row['token'], {'failed': True,
        'failure_reason': 'model connection refused; token=sk-testSensitiveReasonKey123456789'})
    if deferred:
        followups.defer(session.id, row['token'], 'verification turn ended; existing delivery ledger owns report retry')
        assert 'model connection refused' in followups.get_state(session.id)['reason']
    followups.report_state(session.id, row['token'], delivered=delivered, reason=receipt_reason)
    state = followups.get_state(session.id)
    assert state['phase'] == 'needs_reconciliation'
    assert 'model connection refused' in state['reason']
    assert 'sk-testSensitiveReasonKey' not in state['reason']


def test_verification_assessment_must_be_terminal_and_unquoted():
    from tools.process_registry_followups import verification_outcome
    block = '```process_verification\n{"outcome":"verified","evidence":["passed"],"next_action":"none"}\n```'
    assert verification_outcome({'final_response':block})['outcome'] == 'verified'
    for text in (block+'\nActual verification failed.', '> '+block, block+'\n'+block,
                 '````text\n'+block, '~~~text\n'+block):
        assert verification_outcome({'final_response':text})['outcome'] == 'unverified'
