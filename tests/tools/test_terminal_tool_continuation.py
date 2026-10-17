"""Continuation authority is captured before spawn and cannot be minted by verification."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.session_context import clear_session_vars, set_session_vars
from tools import process_registry as processes
from tools import process_registry_continuation as continuation
from tools import process_registry_followups as followups
from tools import terminal_tool
from tools.terminal_tool_background import spawn_background_process


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    tokens = set_session_vars(platform='telegram', chat_id='123', session_key='session', session_id='parent')
    yield tmp_path
    clear_session_vars(tokens)


def config(home, enabled=True):
    (home / 'config.yaml').write_text(f'agent:\n  process_continue_on_complete: {str(enabled).lower()}\n', encoding="utf-8")


@pytest.mark.parametrize('background,notify', [(False, False), (False, True), (True, False)])
def test_requires_background_and_completion_notification(home, monkeypatch, background, notify):
    execute = MagicMock()
    monkeypatch.setattr(terminal_tool, 'terminal_tool', execute)
    result = terminal_tool._handle_terminal({'command': 'true', 'background': background,
        'notify': notify, 'continue_on_complete': True})
    assert 'error' in result
    execute.assert_not_called()


def test_boolean_schema_and_legacy_notify_alias(home, monkeypatch):
    schema = terminal_tool.TERMINAL_SCHEMA['parameters']['properties']['continue_on_complete']
    assert schema['type'] == 'boolean' and schema['default'] is False
    execute = MagicMock(return_value='ok')
    monkeypatch.setattr(terminal_tool, 'terminal_tool', execute)
    assert terminal_tool._handle_terminal({'command': 'true', 'background': True,
        'notify_on_complete': True, 'continue_on_complete': True}) == 'ok'
    assert execute.call_args.kwargs['continue_on_complete'] is True
    result = terminal_tool._handle_terminal({'command': 'true', 'background': True,
        'notify': True, 'continue_on_complete': 'true'})
    assert 'boolean' in json.loads(result)['error']


@pytest.mark.parametrize('enabled', [False, True])
def test_real_fast_background_launch_persists_authority_before_exit(home, monkeypatch, enabled):
    config(home, enabled)
    registry = processes.ProcessRegistry()
    monkeypatch.setattr(processes, 'process_registry', registry)
    event = MessageEvent(text='run my approved task')
    with continuation.turn_scope(event, 'session'), followups.task_request_scope(event.text):
        result = json.loads(spawn_background_process(
            command='printf completed', env=SimpleNamespace(env={}), env_type='local',
            effective_task_id='task', task_id='task', session_key='session', workdir=None,
            cwd=str(home), effective_pty=False, notify_on_complete=True, watch_patterns=None,
            approval_note=None, pty_disabled_reason=None, continue_on_complete=True))
    session = registry.get(result['session_id'])
    assert session._completion_event.wait(10)
    assert result['continue_on_complete'] is enabled
    if enabled:
        assert session.continuation['chain'] == 0
        assert session.continuation['launched_at'] == session.started_at
        with followups._db(read_only=True) as db:
            payload = json.loads(db.execute('SELECT payload FROM process_followups WHERE execution_id=?',
                (session.id,)).fetchone()['payload'])
        assert payload['continue_on_complete'] is True
        assert payload['continuation'] == session.continuation
        assert payload['verification_scope']['task_request'] == event.text
    else:
        assert 'disabled' in result['continuation_note']
        assert not session.continue_on_complete


@pytest.mark.parametrize('kind', ['verification', 'capped', 'internal'])
def test_verification_and_capped_or_unrelated_internal_turn_cannot_arm(home, kind):
    config(home)
    event = MessageEvent(text='verify', internal=kind != 'capped', metadata=(
        {'process_followup': {'execution_id': 'proc', 'token': 'token'}} if kind == 'verification' else {}))
    authority = {'chain': 3, 'chain_id': 'chain'} if kind == 'capped' else None
    with continuation.turn_scope(event, 'session', authority), continuation.launch_scope(True):
        if kind == 'verification':
            with followups.verification_lease('proc', 'token'):
                session = processes.ProcessRegistry._new_session('true', 'task', 'task', 'session', str(home))
                assert followups.verification_tool_block('terminal', {'command': 'true'})
        else:
            session = processes.ProcessRegistry._new_session('true', 'task', 'task', 'session', str(home))
        assert not session.continue_on_complete
        assert 'not armed' in continuation.arming_reason()


def test_continuation_chain_is_inherited_and_cannot_exceed_cap(home):
    config(home)
    event = MessageEvent(text='continue approved work', internal=True)
    with continuation.turn_scope(event, 'session', {'chain': 2, 'chain_id': 'root'}), continuation.launch_scope(True):
        session = processes.ProcessRegistry._new_session('true', 'task', 'task', 'session', str(home))
    assert session.continue_on_complete
    assert session.continuation['chain'] == 2
    assert session.continuation['chain_id'] == 'root'


def test_old_watermark_schema_upgrades_without_losing_cancellation(home):
    import sqlite3
    config(home)
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE process_followup_cancellations (session_key TEXT PRIMARY KEY, cancelled_at REAL NOT NULL)')
        db.execute("INSERT INTO process_followup_cancellations VALUES ('session',123)")
    continuation.record_stop('session')
    with followups._db(read_only=True) as db:
        row = db.execute("SELECT * FROM process_followup_cancellations WHERE session_key='session'").fetchone()
    assert row['cancelled_at'] == 123
    assert row['stopped_at'] > 123


@pytest.mark.parametrize('surface', ['cli', 'tui', 'api_server'])
def test_unsupported_session_refusal_names_surface(home, surface):
    config(home)
    tokens = set_session_vars(platform=surface, source=surface)
    try:
        with continuation.launch_scope(True):
            session = processes.ProcessRegistry._new_session('true', 'task', 'task', 'session', str(home))
        result = continuation.launch_result(session, True)
        assert not result['continue_on_complete']
        assert 'gateway-only' in result['continuation_note']
        assert surface in result['continuation_note']
    finally:
        clear_session_vars(tokens)
