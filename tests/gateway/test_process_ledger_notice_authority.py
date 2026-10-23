"""A verification follow-up or approved continuation notice is admitted by the session authority and
its ledger fence survives the durable round trip into the executed turn.

Regression: the authority refused every such notice (``invalid_params``: ``process_followup`` /
``process_continuation`` were not admissible automation metadata), the drain requeued it every 30
seconds, and neither fork feature ever ran.
"""
import json

import pytest

from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_automation_notification_category import _native_owner
from tests.gateway.test_session_end_delegations import _admissions, _local

_MARKERS = {
    'process_followup': {'execution_id': 'proc_verify', 'token': 'a' * 32},
    'process_continuation': {'execution_id': 'proc_verify', 'token': 'b' * 32},
}


def _evt(route, parent, kind, **extra):
    return {'type': 'completion', 'session_id': 'proc_verify', 'started_at': 1700000000.0, 'command': 'make',
            'exit_code': 0, 'output': 'RESULT_verify', 'session_key': route, 'parent_session_id': parent,
            kind: dict(_MARKERS[kind]), **extra}


def _begun(monkeypatch):
    """Record what ``admit_turn`` begins: the ledger rows themselves are not under test."""
    from tools import process_registry_continuation as continuation
    from tools import process_registry_followups as followups
    begun = []
    monkeypatch.setattr(followups, 'begin', lambda execution, token: begun.append(
        ('process_followup', execution, token)) or True)
    monkeypatch.setattr(continuation, 'begin', lambda execution, token, key: begun.append(
        ('process_continuation', execution, token)) or {'chain': 1})
    return begun


def _turns(runner):
    from gateway.process_continuations import admit_turn
    seen = []

    async def handle(event):
        followup, authority, admitted = admit_turn(event, event.metadata['gateway_session_key'])
        seen.append((event.text, {k: event.metadata.get(k) for k in _MARKERS}, followup, authority, admitted))
        return 'ok'
    runner._handle_message = handle
    return seen


def _expected_admit(kind):
    marker = _MARKERS[kind]
    return (marker, None, True) if kind == 'process_followup' else (None, {'chain': 1}, True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(_MARKERS))
async def test_native_ledger_notice_is_admitted_and_its_fence_reaches_the_turn(tmp_path, monkeypatch, kind):
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'human')
    try:
        begun = _begun(monkeypatch)
        route, parent = metadata['gateway_session_key'], metadata['gateway_session_id']
        evt = _evt(route, parent, kind, platform='discord', chat_id='42', chat_type='dm')
        assert await runner._inject_watch_notification('[verify] RESULT_verify', evt, raise_not_accepted=True) is True
        assert await runner._inject_watch_notification('[verify] RESULT_verify', evt, raise_not_accepted=True) is True
        owner = authority.logical_owner(parent)
        rows = [r for r in list_session_admissions(authority.db, session_id=owner, pending_only=False)
                if r['principal_id'].startswith('automation:')]
        assert len(rows) == 1, 'a retry of the same attempt replays its admission'
        descriptor = rows[0]['payload']['native_text_v1']['automation']
        assert descriptor[kind] == _MARKERS[kind]
        assert json.loads(rows[0]['request_id'])[3:] == [kind, 'proc_verify', _MARKERS[kind]['token']]

        seen = _turns(runner)
        await authority._drain(_ref(authority, owner))
        notice = [turn for turn in seen if 'RESULT_verify' in str(turn[0])]
        assert len(notice) == 1
        _text, markers, *admitted = notice[0]
        assert markers == {k: (_MARKERS[k] if k == kind else None) for k in _MARKERS}
        assert tuple(admitted) == _expected_admit(kind)
        assert begun == [(kind, 'proc_verify', _MARKERS[kind]['token'])]
    finally:
        authority.db.close()


def _ref(authority, session_id):
    from gateway.session_contract import SessionRef
    return SessionRef(authority.profile_id, session_id)


@pytest.mark.asyncio
async def test_new_attempt_of_the_same_process_is_a_new_admission(tmp_path, monkeypatch):
    """A continuation (or a re-verification under a fresh token) after the follow-up is not an
    ``admission_conflict`` with the follow-up's admission."""
    runner, authority, _adapter, _source, metadata = await _native_owner(tmp_path, monkeypatch, 'human')
    try:
        route, parent = metadata['gateway_session_key'], metadata['gateway_session_id']
        for kind in _MARKERS:
            evt = _evt(route, parent, kind, platform='discord', chat_id='42', chat_type='dm')
            assert await runner._inject_watch_notification('[verify]', evt, raise_not_accepted=True) is True
        revised = _evt(route, parent, 'process_followup', platform='discord', chat_id='42', chat_type='dm')
        revised['process_followup']['token'] = 'c' * 32
        assert await runner._inject_watch_notification('[verify]', revised, raise_not_accepted=True) is True
        owner = authority.logical_owner(parent)
        rows = [r for r in list_session_admissions(authority.db, session_id=owner, pending_only=False)
                if r['principal_id'].startswith('automation:')]
        assert len(rows) == 3
    finally:
        authority.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(_MARKERS))
async def test_local_ledger_notice_runs_as_a_turn_with_its_fence(tmp_path, monkeypatch, kind):
    """No viewer attached: a plain local completion becomes a note, but a fenced attempt must run (a
    note would leave its ledger row queued forever)."""
    from gateway.platforms.event import MessageEvent
    from gateway.run_notifications import _watch_notification_metadata
    from gateway.session_automation import _process_completed, admit_process_notice
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        begun = _begun(monkeypatch)
        live = authority.sessions[ref.session_id]
        target = runner.session_store.lookup_by_session_key(live.route).session_id
        evt = _evt(live.route, target, kind)
        event = MessageEvent(text='[verify] RESULT_verify', source=live.source, internal=True,
                             metadata=_watch_notification_metadata(evt, live.source))
        await admit_process_notice(runner, authority, runner._adapter_for_source(live.source), event, evt)
        [row] = _admissions(authority, ref)
        descriptor = row['payload']['local_automation_v1']
        assert descriptor[kind] == _MARKERS[kind]
        assert _process_completed(runner, authority, ref.session_id,
                                  {'session_id': 'proc_verify', 'started_at': 1700000000.0}), \
            'a late tick of the process sees its completion admitted'

        seen = _turns(runner)
        await authority._drain(ref)
        assert len(seen) == 1
        _text, markers, *admitted = seen[0]
        assert markers == {k: (_MARKERS[k] if k == kind else None) for k in _MARKERS}
        assert tuple(admitted) == _expected_admit(kind)
        assert begun == [(kind, 'proc_verify', _MARKERS[kind]['token'])]
        assert [r['outcome'] for r in _admissions(authority, ref)] == ['completed']
    finally:
        authority.db.close()


@pytest.mark.parametrize('marker', [
    {}, [], 'x', {'execution_id': 'proc'}, {'execution_id': 'proc', 'token': ''},
    {'execution_id': 'proc', 'token': 7}, {'execution_id': 'proc', 'token': 't', 'extra': 1},
    {'execution_id': 'p' * 257, 'token': 't'},
])
@pytest.mark.parametrize('kind', list(_MARKERS))
def test_malformed_fence_refuses(kind, marker):
    from gateway.session_automation import automation_process_metadata
    with pytest.raises(RuntimeStoreError, match='invalid_params'):
        automation_process_metadata({kind: marker})


def test_well_formed_fence_is_copied():
    from gateway.session_automation import automation_process_metadata
    metadata = {key: dict(value) for key, value in _MARKERS.items()}
    validated = automation_process_metadata(metadata)
    assert validated == _MARKERS
    assert all(validated[key] is not metadata[key] for key in _MARKERS)
    assert automation_process_metadata({'notification_category': 'result'}) == {}


@pytest.mark.asyncio
async def test_follow_up_admits_on_a_renamed_auto_thread(tmp_path, monkeypatch):
    """The route's origin keeps the auto-thread creation stamp and first name; the committed human
    turns carry the renamed thread and no stamp. Display labels must not refuse the wake
    (``admission_conflict`` every 30 s on koharu's renamed threads)."""
    from gateway.session_identity import replace_source
    runner, authority, _adapter, source, metadata = await _native_owner(tmp_path, monkeypatch, 'human')
    try:
        route, parent = metadata['gateway_session_key'], metadata['gateway_session_id']
        runner.session_store._entries[route].origin = replace_source(
            source, chat_name='first question', auto_thread_created=True, auto_thread_initial_name='first question')
        evt = _evt(route, parent, 'process_followup', platform='discord', chat_id='42', chat_type='dm')
        assert await runner._inject_watch_notification('[verify]', evt, raise_not_accepted=True) is True
        runner.session_store._entries[route].origin = replace_source(source, user_name='someone else')
        evt = _evt(route, parent, 'process_continuation', platform='discord', chat_id='42', chat_type='dm')
        with pytest.raises(Exception, match='admission_conflict'):
            await runner._inject_watch_notification('[verify]', evt, raise_not_accepted=True)
    finally:
        authority.db.close()


async def _refusing_ledger(tmp_path, monkeypatch, permanent):
    from tests.gateway.test_durable_process_followups import Runner
    from tools import process_registry_followups as ledger
    from tools.process_registry import ProcessSession
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    refusals = []

    async def refuse(text, evt, **kwargs):
        refusals.append(evt['process_followup']['token'])
        evt['_refusal_permanent'], evt['_refusal_reason'] = permanent, 'not_found' if permanent else 'queue_full'
        return False
    runner._inject_watch_notification = refuse
    session = ProcessSession(id='proc_refused', command='true', session_key=runner.entry.session_key,
                             parent_session_id=runner.entry.session_id)
    ledger.reserve(session)
    return runner, ledger, session, refusals


async def _reconcile_rounds(runner, ledger, rounds):
    from gateway.process_followups import reconcile
    for _ in range(rounds):
        with ledger._db() as db:
            db.execute('UPDATE process_followups SET next_attempt=0')
        await reconcile(runner)


@pytest.mark.asyncio
async def test_permanently_refused_follow_up_stops_retrying(tmp_path, monkeypatch):
    from gateway.run_notifications import _MAX_COMPLETION_INJECTION_ATTEMPTS
    runner, ledger, session, refusals = await _refusing_ledger(tmp_path, monkeypatch, permanent=True)
    await _reconcile_rounds(runner, ledger, _MAX_COMPLETION_INJECTION_ATTEMPTS - 1)
    assert ledger.get_state(session.id)['phase'] == 'pending'
    await _reconcile_rounds(runner, ledger, 1)
    state = ledger.get_state(session.id)
    assert state['phase'] == 'needs_reconciliation'
    assert 'not_found' in state['reason']
    await _reconcile_rounds(runner, ledger, 3)
    assert len(refusals) == _MAX_COMPLETION_INJECTION_ATTEMPTS, 'no admission is retried after giving up'


@pytest.mark.asyncio
async def test_transiently_refused_follow_up_keeps_retrying(tmp_path, monkeypatch):
    from gateway.run_notifications import _MAX_COMPLETION_INJECTION_ATTEMPTS
    runner, ledger, session, refusals = await _refusing_ledger(tmp_path, monkeypatch, permanent=False)
    await _reconcile_rounds(runner, ledger, _MAX_COMPLETION_INJECTION_ATTEMPTS + 2)
    assert ledger.get_state(session.id)['phase'] == 'pending'
    assert len(refusals) == _MAX_COMPLETION_INJECTION_ATTEMPTS + 2


def test_refusal_count_resets_on_admission(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from gateway.process_continuations import admission_settled, refused_for_good
    from gateway.run_notifications import _MAX_COMPLETION_INJECTION_ATTEMPTS
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner, refused = SimpleNamespace(), {'_refusal_permanent': True}
    for _ in range(_MAX_COMPLETION_INJECTION_ATTEMPTS - 1):
        assert not refused_for_good(runner, 'proc', refused)
    admission_settled(runner, 'proc')
    assert not refused_for_good(runner, 'proc', refused)
    assert not refused_for_good(runner, 'proc', {'_refusal_permanent': False})
