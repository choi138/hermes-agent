"""Gateway adapter queue, process restart, and stale parent publication proof."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError
from gateway import delivery_ledger as delivery
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from tests.agent.task_lifecycle.test_publication import parent_before_worker
from tests.agent.task_lifecycle.test_review_regressions import Channel, discord_adapter, gateway_runner
from tests.agent.task_lifecycle.test_workflow import case as workflow_case

pytestmark = pytest.mark.platforms('macos')
case = workflow_case

_FAILED_SENDER = '''
import asyncio,json,sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from gateway import delivery_ledger as d
from gateway.config import Platform,PlatformConfig
from gateway.platforms.base import MessageEvent,SendResult
from gateway.session import SessionSource
from plugins.platforms.discord.adapter import DiscordAdapter
p=json.load(sys.stdin)
d.ledger_enabled=lambda config=None: True
source=SessionSource(platform=Platform.DISCORD,chat_id='123',thread_id='123',profile='default',user_id='operator')
event=MessageEvent(text='fix request',source=source,message_id='restart-proof')
event._completion_decision=p['decision']
adapter=DiscordAdapter(PlatformConfig())
adapter.send=AsyncMock(return_value=SendResult(success=False,error='send_path_degraded'))
async def main():
    sent,_=await adapter.send_final_ledgered(event,p['session_key'],p['text'],{},reply_to=None)
    assert not sent.success
    print(json.dumps({'obligation_id':event._gateway_delivery_obligation_id}))
asyncio.run(main())
'''


def authority(case, monkeypatch):
    monkeypatch.setattr(delivery, '_db_path', lambda: case['home'] / 'state.db')
    monkeypatch.setattr(delivery, 'ledger_enabled', lambda config=None: True)
    registry = Registry()
    try:
        ledger, parent, _ = parent_before_worker(case, registry)
        decision = ledger.decision(parent['parent_id'])
        assert decision['accepted'], decision
        return parent, decision, '[Hermes task: verified; decision ' + decision['decision_id'] + ']'
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [False, True])
async def test_adapter_failed_send_recovers_after_real_sender_exit(case, monkeypatch, stale):
    parent, decision, text = authority(case, monkeypatch)
    result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', _FAILED_SENDER],
        input=json.dumps(dict(decision=decision, session_key=parent['session_key'], text=text)),
        env={**os.environ, 'HERMES_HOME': str(case['home'])},
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30,
        check=False)
    assert result.returncode == 0, result.stderr
    oid = json.loads(result.stdout.splitlines()[-1])['obligation_id']
    if stale:
        (case['repo'] / 'answer.json').write_text('{"status":"changed after queue"}')
    channel = Channel()
    adapter = discord_adapter(channel)
    runner = gateway_runner(adapter)
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    claimed = delivery.sweep_recoverable()
    assert [row['obligation_id'] for row in claimed] == [oid]
    assert await runner._redeliver_claimed_obligations(claimed) == int(not stale)
    assert len(channel.messages) == int(not stale)
    if not stale:
        assert decision['decision_id'] in channel.messages[0].content
        assert delivery.sweep_recoverable() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [False, True])
async def test_queued_clone_uses_atomic_authority_before_transport(case, monkeypatch, stale):
    parent, decision, text = authority(case, monkeypatch)
    channel = Channel()
    adapter = discord_adapter(channel)
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id='existing'))
    runner = gateway_runner(adapter)
    source = SessionSource(platform=Platform.DISCORD, chat_id='123', thread_id='123',
                           profile='default', user_id='operator')
    if stale:
        (case['repo'] / 'answer.json').write_text('{"status":"changed before edit"}')
    kwargs = dict(source=source, adapter=adapter, session_key=parent['session_key'],
        inbound_message_id='queued-proof', deliver_media=False,
        completion_decision=decision, stream_consumer=SimpleNamespace(message_id='existing'))
    if stale:
        with pytest.raises(LifecycleError, match='no longer authoritative'):
            await runner._deliver_queued_first_response(text, **kwargs)
        adapter.edit_message.assert_not_awaited()
    else:
        assert await runner._deliver_queued_first_response(text, **kwargs)
        adapter.edit_message.assert_not_awaited()
        with delivery._transaction() as conn:
            row = conn.execute('SELECT state FROM delivery_obligations WHERE content=?', (text,)).fetchone()
        assert row == ('delivered',)
    assert len(channel.messages) == int(not stale)


@pytest.mark.asyncio
async def test_automatic_publication_uses_current_authority_and_sends_once(case, monkeypatch):
    from gateway.task_completion_recovery import publish_ready
    parent, decision, _ = authority(case, monkeypatch)
    channel = Channel()
    adapter = discord_adapter(channel)
    runner = gateway_runner(adapter)
    source = SessionSource(platform=Platform.DISCORD, chat_id='123', thread_id='123',
                           profile='default', user_id='operator')
    runner.session_store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None,
        _entries={parent['session_key']: SimpleNamespace(origin=source)})
    runner._restored_source = lambda entry: entry.origin
    runner._delivery_adapter_for = lambda value: adapter
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    assert await publish_ready(runner, parent, decision)
    assert not await publish_ready(runner, parent, decision)
    assert len(channel.messages) == 1
    hook = runner.hooks.emit.call_args.args[1]
    assert hook['completion_notification'] is True
    assert hook['task_completion']['accepted'] is True
    assert hook['task_completion']['decision_id'] == decision['decision_id']


@pytest.mark.asyncio
async def test_normal_and_automatic_send_share_one_atomic_decision(case, monkeypatch):
    from gateway.platforms.event import MessageEvent
    from gateway.task_completion_recovery import publish_ready
    parent, decision, _ = authority(case, monkeypatch)
    channel = Channel()
    adapter = discord_adapter(channel)
    runner = gateway_runner(adapter)
    source = SessionSource(platform=Platform.DISCORD, chat_id='123', thread_id='123',
                           profile='default', user_id='operator')
    runner.session_store = SimpleNamespace(_lock=threading.RLock(), _ensure_loaded_locked=lambda: None,
        _entries={parent['session_key']: SimpleNamespace(origin=source)})
    runner._restored_source = lambda entry: entry.origin
    runner._delivery_adapter_for = lambda value: adapter
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    text = ('[Hermes task: verified; parent ' + parent['parent_id']
            + '; decision ' + decision['decision_id'] + ']')
    event = MessageEvent(text='status', source=source, message_id='normal-final')
    event._completion_decision = decision
    await asyncio.gather(publish_ready(runner, parent, decision),
        adapter.send_final_ledgered(event, parent['session_key'], text, {}, reply_to=None))
    assert len(channel.messages) == 1
    with delivery._transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM delivery_obligations WHERE content=?', (text,)).fetchone() == (1,)
