"""The gateway-to-Mac envelope uses authenticated intake and operator policy."""
from types import SimpleNamespace

import pytest

from agent.task_lifecycle.intake import gateway_envelope
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.task_completion import admit_request, capture_request, completion_scope
from gateway.task_completion_policy import GatewayExecutionPolicy
from tools.approval_context import set_current_session_key, reset_current_session_key


def test_actual_gateway_envelope_uses_frozen_original_and_bounded_operator_policy(tmp_path):
    source = SessionSource(platform=Platform.DISCORD, chat_id='canary', user_id='human')
    event = MessageEvent(text='fix original required behavior', source=source, message_id='m1')
    admit_request(event, capture_request(event))
    event.text = 'plugin rewrite loses original requirement'
    ctx = SimpleNamespace(source=source, event=event, session_key='isolated:canary')
    registry = Registry(tmp_path / 'state.db')
    token = set_current_session_key(ctx.session_key)
    request = {'cli': 'codex', 'workdir': '/trusted/repo', 'timeout': 60, 'sandbox': 'workspace-write'}
    policy = GatewayExecutionPolicy(lane='codex', workdir='/trusted/repo', timeout_seconds=60,
                                    approval='not_required')
    try:
        with completion_scope(ctx, lambda: None, registry=registry) as scope:
            def envelope(**overrides):
                args = dict(source=source, session_key=ctx.session_key, request_revision='1',
                    request_text=scope.request.text, decision=policy, request=request,
                    objective='fix required behavior', checks=[{'name': 'required', 'kind': 'test', 'argv': ['/usr/bin/true']}], artifacts=[], spec_sha256='a' * 64)
                return gateway_envelope(**(args | overrides))
            result = envelope()
            assert result['request_text'] == 'fix original required behavior'
            assert result['origin'] == scope.request.origin
            assert result['owner'] == 'human'
            with pytest.raises(LifecycleError, match='authenticated original'):
                envelope(request_text=event.text)
            with pytest.raises(LifecycleError, match='execution policy'):
                envelope(request=request | {'timeout': 61})
    finally:
        reset_current_session_key(token)
        registry.close()


def test_gateway_envelope_cannot_be_minted_outside_authenticated_scope():
    source = SessionSource(platform=Platform.DISCORD, chat_id='canary', user_id='human')
    token = set_current_session_key('isolated:canary')
    try:
        with pytest.raises(LifecycleError, match='authenticated original'):
            gateway_envelope(source=source, session_key='isolated:canary', request_revision='1',
                request_text='fix behavior', decision=GatewayExecutionPolicy(
                    lane='codex', workdir='/trusted/repo', timeout_seconds=60, approval='not_required'),
                request={'cli': 'codex', 'workdir': '/trusted/repo', 'timeout': 60, 'sandbox': 'workspace-write'},
                objective='fix behavior', checks=[{'name': 'required', 'kind': 'test', 'argv': ['/usr/bin/true']}], artifacts=[], spec_sha256='a' * 64)
    finally:
        reset_current_session_key(token)


@pytest.mark.asyncio
async def test_real_inbound_admission_captures_original_before_plugin_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._session_key_for_source = lambda source: "isolated-canary:session"
    async def rewrite(event, source):
        event.text = 'plugin rewritten task'
        return event
    runner._hm_pre_gateway_dispatch_hook = rewrite
    runner._is_user_authorized_for_source = lambda source, **kw: True
    runner._admit_bot_message = lambda source: True
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated-canary', user_id='human')
    event = MessageEvent(text='implement original required behavior', source=source, message_id='native1')
    admitted, actual, internal = await runner._hm_admit_event(event)
    assert admitted is event and actual is source and internal is False
    assert event.text == 'plugin rewritten task'
    ctx = SimpleNamespace(source=actual, event=admitted, session_key='isolated-canary:session')
    registry = Registry(tmp_path / 'state.db')
    try:
        with completion_scope(ctx, lambda: None, registry=registry) as scope:
            assert scope.parent['original_request'] == 'implement original required behavior'
            assert scope.request.origin == 'discord:isolated-canary::native1'
    finally:
        registry.close()


@pytest.mark.asyncio
async def test_real_admission_appends_queued_requirement_before_existing_decision(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from agent.task_lifecycle.parent import ParentLedger
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._session_key_for_source = lambda source: 'isolated:session'
    async def passthrough(event, source):
        return event
    runner._hm_pre_gateway_dispatch_hook = passthrough
    runner._is_user_authorized_for_source = lambda source, **kw: True
    runner._admit_bot_message = lambda source: True
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    first = MessageEvent(text='implement feature X', source=source, message_id='m1')
    await runner._hm_admit_event(first)
    registry = Registry(tmp_path / 'state.db')
    ledger = ParentLedger(registry)
    identity = dict(session_key='isolated:session', profile='default', owner='human')
    try:
        before = ledger.current(**identity)
        second = MessageEvent(text='also implement feature Y', source=source, message_id='m2')
        await runner._hm_admit_event(second)
        after = ledger.current(**identity)
        assert after['parent_id'] == before['parent_id']
        assert after['contract_digest'] != before['contract_digest']
        assert [s['text'] for s in after['contract']['sources']] == [first.text, second.text]
        status = MessageEvent(text='status please', source=source, message_id='m3')
        await runner._hm_admit_event(status)
        assert ledger.current(**identity)['contract_digest'] == after['contract_digest']
        assert ledger.decision(after['parent_id'])['accepted'] is False
    finally:
        registry.close()


def test_amended_request_envelope_keeps_original_and_all_authenticated_sources(tmp_path):
    from gateway.task_completion_requests import source_context
    registry = Registry(tmp_path / 'state.db')
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    def context(text, message_id):
        event = MessageEvent(text=text, source=source, message_id=message_id)
        admit_request(event, capture_request(event))
        return SimpleNamespace(source=source, event=event, session_key='isolated:session',
            user_config={'completion_gate': {'mode': 'enforce'}})
    first = context('implement feature X', 'm1')
    second = context('also implement feature Y', 'm2')
    token = set_current_session_key(first.session_key)
    try:
        with completion_scope(first, lambda: None, registry=registry):
            pass
        with completion_scope(second, lambda: None, registry=registry) as scope:
            envelope = gateway_envelope(source=source, session_key=first.session_key,
                request_revision=str(scope.parent['contract']['revision']),
                request_text=scope.parent['original_request'], decision=GatewayExecutionPolicy(
                    lane='codex', workdir='/trusted/repo', timeout_seconds=60, approval='not_required'),
                request=dict(cli='codex', workdir='/trusted/repo', timeout=60, sandbox='workspace-write'),
                objective='implement X and Y', checks=[dict(name='required', kind='test', argv=['/usr/bin/true'])],
                artifacts=[], spec_sha256='a' * 64, context=source_context(scope.parent))
            assert envelope['request_text'] == 'implement feature X'
            assert envelope['origin'] == second.event._completion_request.origin
            assert envelope['context']['manifest']['parent_sources'] == scope.parent['contract']['sources']
            assert 'also implement feature Y' in envelope['context']['text']
    finally:
        reset_current_session_key(token)
        registry.close()
