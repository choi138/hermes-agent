"""Delegated contexts keep durable audit authority beyond their parent's turn."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.task_completion import (
    admit_request, capture_request, completion_scope, transform_output,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.registry import ToolRegistry


def _context():
    source = SessionSource(platform=Platform.DISCORD, chat_id='lifetime',
        user_id='original-owner', profile='original-profile')
    event = MessageEvent(text='implement the requested fix', source=source, message_id='original')
    admit_request(event, capture_request(event))
    return SimpleNamespace(event=event, source=source, session_key='lifetime:original',
        user_config={'completion_gate': {'mode': 'enforce'}})


def test_delegated_tool_after_parent_close_audits_original_profile_before_effect(tmp_path):
    home = tmp_path / 'original'
    home.mkdir()
    other_home = tmp_path / 'other'
    other_home.mkdir()
    owner_token = set_hermes_home_override(home)
    release = threading.Event()
    tools = ToolRegistry()
    effects = []
    parent_id = None

    def write(args):
        with sqlite3.connect(home / 'state.db') as conn:
            rows = conn.execute('SELECT parent_id,session_key,profile,owner,kind,attribution '
                'FROM lifecycle_parent_executions').fetchall()
        assert len(rows) == 1
        assert rows[0][:5] == (parent_id, 'lifetime:original', 'original-profile',
            'original-owner', 'write_file')
        assert json.loads(rows[0][5])['source_request']['origin'].endswith(':original')
        effects.append('saved')
        return 'saved'

    def delegated():
        assert release.wait(10), 'parent did not finish'
        token = set_hermes_home_override(other_home)
        try:
            result = tools.dispatch('write_file', {})
            return result, transform_output('child says everything is complete')
        finally:
            reset_hermes_home_override(token)

    tools.register(name='write_file', toolset='file', schema={'parameters': {}}, handler=write)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                with completion_scope(_context(), lambda: None) as scope:
                    parent_id = scope.parent['parent_id']
                    future = pool.submit(copy_context().run, delegated)
                # The parent connection really is gone, not held open for children.
                with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
                    scope.registry._conn.execute('SELECT 1')
            finally:
                release.set()
            result, output = future.result(timeout=15)
        assert result == 'saved'
        assert effects == ['saved']
        assert 'completion withheld' in output
        assert 'child says everything is complete' not in output
        assert not (other_home / 'state.db').exists()
    finally:
        release.set()
        reset_hermes_home_override(owner_token)


def test_retired_scope_cannot_execute_when_original_ledger_is_unavailable(tmp_path):
    home = tmp_path / 'original'
    home.mkdir()
    token = set_hermes_home_override(home)
    tools = ToolRegistry()
    effects = []
    tools.register(name='write_file', toolset='file', schema={'parameters': {}},
        handler=lambda args: effects.append('saved') or 'saved')
    try:
        with completion_scope(_context(), lambda: None):
            inherited = copy_context()
        (home / 'state.db').unlink()
        result = inherited.run(tools.dispatch, 'write_file', {})
        assert effects == []
        assert 'error' in result
        output = inherited.run(transform_output, 'complete')
        assert 'unavailable' in output
        assert 'completion withheld' in output
        assert not (home / 'state.db').exists()
    finally:
        reset_hermes_home_override(token)
