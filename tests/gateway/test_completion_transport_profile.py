"""A routed worker's result belongs to the bot that admitted its request."""
from types import SimpleNamespace

import pytest

from agent.task_lifecycle.intake import gateway_envelope
from agent.task_lifecycle.registry import Registry
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity
from gateway.task_completion import admit_request, capture_request, completion_scope
from gateway.task_completion_policy import GatewayExecutionPolicy
from tools.approval_context import set_current_session_key, reset_current_session_key


@pytest.mark.parametrize('transport,runtime', [('default', 'satellite'), ('shared-bot', 'default')])
def test_gateway_grant_retains_receiving_bot_with_routed_runtime(tmp_path, transport, runtime):
    source = SessionSource(platform=Platform.DISCORD, chat_id='canary', user_id='human', profile=runtime)
    source._identity = RoutingIdentity(transport_profile=transport, runtime_profile=runtime,
        authorization_home=tmp_path / transport, runtime_home=tmp_path / runtime)
    event = MessageEvent(text='implement required behavior', source=source, message_id='routed')
    admit_request(event, capture_request(event))
    ctx = SimpleNamespace(source=source, event=event, session_key='isolated:routed')
    registry = Registry(tmp_path / 'state.db')
    token = set_current_session_key(ctx.session_key)
    try:
        with completion_scope(ctx, lambda: None, registry=registry):
            envelope = gateway_envelope(source=source, session_key=ctx.session_key,
                request_revision='1', request_text=event.text,
                decision=GatewayExecutionPolicy(lane='codex', workdir='/trusted/repo',
                    timeout_seconds=60, approval='not_required'),
                request=dict(cli='codex', workdir='/trusted/repo', timeout=60, sandbox='workspace-write'),
                objective='implement required behavior', checks=[dict(name='required', kind='test',
                    argv=['/usr/bin/true'])], artifacts=[], spec_sha256='a' * 64)
            assert envelope['profile'] == runtime
            assert envelope['destination']['adapter_profile'] == transport
    finally:
        reset_current_session_key(token)
        registry.close()
