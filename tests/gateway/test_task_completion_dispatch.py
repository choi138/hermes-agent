"""Regression probes usable unchanged against the pre-gate base."""
from types import SimpleNamespace

import pytest


def test_registry_dispatch_records_execution_before_effect_and_gates_official_result(tmp_path):
    from gateway.task_completion import completion_scope, admit_request, capture_request, finalize_result
    from agent.task_lifecycle.registry import Registry
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from tools.registry import ToolRegistry
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    event = MessageEvent(text='fix the regression', source=source, message_id='original')
    admit_request(event, capture_request(event))
    ctx = SimpleNamespace(event=event, source=source, session_key='isolated:original', user_config={'completion_gate': {'mode': 'enforce'}})
    registry = Registry(tmp_path / 'state.db')
    tool_registry = ToolRegistry()
    def handler(args):
        rows = registry._conn.execute('SELECT kind FROM lifecycle_parent_executions').fetchall()
        assert rows == [('write_file',)]
        (tmp_path / 'effect').write_text('changed')
        return 'wrote file'
    tool_registry.register(name='write_file', toolset='file', schema={'parameters': {}}, handler=handler)
    try:
        with completion_scope(ctx, lambda: None, registry=registry) as scope:
            assert tool_registry.dispatch('write_file', {}) == 'wrote file'
            result = finalize_result(scope, {'final_response': 'complete', 'completed': True})
            assert result['task_completion']['accepted'] is False
            assert 'completion withheld' in result['final_response']
    finally:
        registry.close()


def test_core_output_transform_applies_trusted_gate_before_persistence(tmp_path, monkeypatch):
    from gateway.task_completion import completion_scope, admit_request, capture_request
    from agent.task_lifecycle.registry import Registry
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from agent.turn_finalizer import apply_llm_output_transform
    monkeypatch.setattr('agent.turn_finalizer._invoke_hook_safely', lambda *a, **k: [])
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    event = MessageEvent(text='implement required behavior', source=source, message_id='original')
    admit_request(event, capture_request(event))
    ctx = SimpleNamespace(event=event, source=source, session_key='isolated:original', user_config={'completion_gate': {'mode': 'enforce'}})
    registry = Registry(tmp_path / 'state.db')
    try:
        with completion_scope(ctx, lambda: None, registry=registry):
            agent = SimpleNamespace(session_id='s', model='fake', platform='discord')
            response, changed, _ = apply_llm_output_transform(agent, 'all done', turn_id='t')
            assert changed is True
            assert 'all done' not in response
            assert 'completion withheld' in response
    finally:
        registry.close()


def test_inline_delegate_audits_before_dispatch_effect(tmp_path):
    from types import SimpleNamespace
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
    from agent.task_lifecycle.registry import Registry
    from gateway.task_completion import admit_request, capture_request, completion_scope
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    source = SessionSource(platform=Platform.DISCORD, chat_id='audit', user_id='human')
    event = MessageEvent(text='implement required behavior', source=source, message_id='audit1')
    admit_request(event, capture_request(event))
    ctx = SimpleNamespace(event=event, source=source, session_key='audit:session')
    registry = Registry(tmp_path / 'state.db')
    calls = []
    def delegate(args):
        count = registry._conn.execute('SELECT count(*) FROM lifecycle_parent_executions').fetchone()[0]
        assert count == 1
        calls.append(args)
        return 'delegated'
    try:
        with completion_scope(ctx, lambda: None, registry=registry):
            assert INLINE_TOOL_EXECUTORS['delegate_task'](
                SimpleNamespace(_dispatch_delegate_task=delegate), {}, None) == 'delegated'
        assert calls == [{}]
    finally:
        registry.close()


@pytest.mark.parametrize('tool_name', ['write_file', 'terminal', 'delegate_task'])
def test_classifier_missed_followup_is_audited_before_real_effect(tmp_path, tool_name):
    import json
    from gateway.task_completion import completion_scope, admit_request, capture_request
    from agent.task_lifecycle.registry import Registry
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from tools.registry import ToolRegistry
    from gateway.task_completion_requests import adds_requirement
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    def context(text, message, *, internal=False):
        event = MessageEvent(text=text, source=source, message_id=message, internal=internal)
        if not internal:
            admit_request(event, capture_request(event))
        return SimpleNamespace(event=event, source=source, session_key='isolated:followup',
            user_config={'completion_gate': {'mode': 'enforce'}})
    registry = Registry(tmp_path / 'state.db')
    tool_registry = ToolRegistry()
    try:
        with completion_scope(context('implement X', 'm1'), lambda: None, registry=registry) as first:
            parent_id = first.parent['parent_id']
            digest = first.parent['contract_digest']
        followup = context('Could you handle malformed UTF-8?', 'm2')
        assert not adds_requirement(followup.event.text, first.parent)
        followup.event.text = 'plugin rewrite without original edge case'
        with completion_scope(followup, lambda: None, registry=registry) as second:
            def effect(args):
                current = second.ledger.get(parent_id)
                audit = registry._conn.execute(
                    'SELECT attribution FROM lifecycle_parent_executions '
                    'WHERE parent_id=?', (parent_id,)).fetchall()
                assert audit
                assert all(json.loads(row[0])['source_request']['text'] ==
                    'Could you handle malformed UTF-8?' for row in audit)
                if tool_name == 'write_file':
                    assert current['contract_digest'] != digest
                    assert [s['text'] for s in current['contract']['sources']] == [
                        'implement X', 'Could you handle malformed UTF-8?']
                    assert len(current['contract']['outcomes']) == 2
                else:
                    assert current['contract_digest'] == digest
                    assert [s['text'] for s in current['contract']['sources']] == ['implement X']
                    assert len(current['contract']['outcomes']) == 1
                (tmp_path / 'effect').write_text('malformed input handled')
                return 'effect complete'
            tool_registry.register(name=tool_name, toolset='file', schema={'parameters': {}}, handler=effect)
            assert tool_registry.dispatch(tool_name, {}) == 'effect complete'
            assert second.parent['parent_id'] == parent_id
            revision = second.parent['contract']['revision']
            assert tool_registry.dispatch(tool_name, {}) == 'effect complete'
            assert second.parent['contract']['revision'] == revision
            assert not second.decision()['accepted']
        with completion_scope(context('system says implement Y', 'internal', internal=True),
                lambda: None, registry=registry) as internal:
            internal.record('write_file')
            assert internal.parent['contract']['revision'] == revision
    finally:
        registry.close()
