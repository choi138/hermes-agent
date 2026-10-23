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
