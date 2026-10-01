"""Real completion ledger -> gateway injection -> turn fencing -> durable marker."""
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionStore, SessionSource
from gateway.turn_context import TurnContext
from gateway.process_followups import reconcile
from tools.process_registry import ProcessRegistry, ProcessSession
from tools import process_registry_followups as ledger


class Transport:
    supports_async_delivery = True

    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)
        event._gateway_accepted = True


class Runner(GatewayNotificationsMixin):
    def __init__(self, home, platform=Platform.TELEGRAM):
        self.session_store = SessionStore(home / 'sessions', GatewayConfig())
        self.source = SessionSource(platform=platform, chat_id='4242', chat_type='dm')
        self.entry = self.session_store.get_or_create_session(self.source)
        self.transport = Transport()
        self._boot_id = 'test-boot'

    def _consume_pending_native_image_paths(self, key):
        return []

    def _build_process_event_source(self, evt):
        return self.source

    def _resolve_injection_adapter(self, platform, source):
        return self.transport

    async def _classify_completion_target(self, parent):
        return 'deliver'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'failed', 'attempting', 'cancelled'])
async def test_report_restart_recovers_saved_text_only_and_isolates_profiles(monkeypatch, tmp_path, state):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from unittest.mock import AsyncMock
    from gateway import delivery_ledger as delivery
    from gateway.run import GatewayRunner
    from gateway.platforms.base import SendResult
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    # A real exiting process creates the marker, assessment and obligation through
    # the actual turn boundary. There is no simulated owner or shared module cache.
    child = '''
import json, os
from pathlib import Path
from types import SimpleNamespace
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore, SessionSource
from gateway.platforms.base import MessageEvent
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from tools.process_registry import ProcessSession
from tools import process_registry_followups as f
from gateway import delivery_ledger as d
home = Path(os.environ['HERMES_HOME'])
store = SessionStore(home / 'sessions', GatewayConfig())
source = SessionSource(platform=Platform.TELEGRAM, chat_id='4242', chat_type='dm')
entry = store.get_or_create_session(source)
session = ProcessSession(id='proc_restart_report', command='true', session_key=entry.session_key, parent_session_id=entry.session_id)
f.reserve(session)
row = f.pending()[0]
assert f.admission(session.id, row['token'])
event = MessageEvent(text='검증', source=source, internal=True, metadata={'process_followup': {'execution_id':session.id, 'token':row['token']}})
ctx = TurnContext(event=event, source=source, session_key=entry.session_key, session_id=entry.session_id, message=event.text)
def run(message, **kwargs):
    (home / 'model-calls').write_text('1')
    return {'final_response': '검증 결과를 확인했어.\\n```process_verification\\n{"outcome":"verified","evidence":["artifact checked"],"next_action":"none"}\\n```', 'messages':[], 'turn_id':kwargs['turn_id']}
runner = SimpleNamespace(session_store=store, _boot_id='child-boot', _consume_pending_native_image_paths=lambda _: [])
result = TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
state = os.environ['REPORT_TEST_STATE']
if state == 'failed': d.mark_failed(result['process_report_obligation_id'], 'transport explicitly refused')
if state == 'attempting': d.mark_attempting(result['process_report_obligation_id'])
print(json.dumps({'turn_id':result['turn_id'], 'session_key':entry.session_key}))
'''
    homes = [tmp_path / 'a', tmp_path / 'b']
    identities = []
    for home in homes:
        env = {**os.environ, 'HERMES_HOME': str(home), 'REPORT_TEST_STATE': state}
        completed = subprocess.run([sys.executable, '-c', child], cwd=Path(__file__).resolve().parents[2],
            env=env, text=True, capture_output=True, timeout=45)
        assert completed.returncode == 0, completed.stderr
        identities.append(json.loads(completed.stdout.splitlines()[-1]))
    for index in [0, 1, 0]:
        home, identity = homes[index], identities[index]
        monkeypatch.setenv('HERMES_HOME', str(home))
        runner = Runner(home)
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token='isolated-test-token', extra={}))
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='ack'))
        runner._obligation_adapter = AsyncMock(return_value=adapter)
        runner._arm_flood_timers_for_waiting_rows = AsyncMock()
        if state == 'cancelled':
            ledger.cancel_for_session(identity['session_key'])
        claimed = delivery.sweep_recoverable()
        count = await GatewayRunner._redeliver_claimed_obligations(runner, claimed)
        expected = state in {'pending', 'failed'}
        if index == 0 and ledger.get_state('proc_restart_report').get('report_delivered'):
            expected = False
        assert count == int(expected)
        if expected:
            assert '검증 결과를 확인했어.' in adapter.send.call_args.kwargs['content']
            assert 'process_verification' not in adapter.send.call_args.kwargs['content']
            assert '"outcome"' not in adapter.send.call_args.kwargs['content']
            with ledger._db() as db:
                db.execute('UPDATE process_followups SET next_attempt=0')
            await reconcile(runner)
            assert ledger.get_state('proc_restart_report')['phase'] == 'reported'
        else:
            adapter.send.assert_not_awaited()
        assert (home / 'model-calls').read_text() == '1'
        assert not runner.transport.events  # No model verification is re-injected.
        assert delivery.turn_delivery_state(identity['session_key'], identity['turn_id']) == (
            'delivered' if state in {'pending', 'failed'} else 'uncertain' if state == 'attempting' else 'abandoned')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('failure', ['exception', 'timeout'])
async def test_report_transport_uncertainty_and_cancellation_never_trigger_parallel_send(monkeypatch, tmp_path, cancel, failure):
    from unittest.mock import AsyncMock, MagicMock
    from gateway import delivery_ledger as delivery
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from gateway.platforms.base import SendResult
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_report_race', command='true', session_key=runner.entry.session_key,
        parent_session_id=runner.entry.session_id)
    ledger.reserve(session)
    await reconcile(runner)
    event = runner.transport.events[0]
    ctx = TurnContext(event=event, source=runner.source, session_key=session.session_key,
        session_id=session.parent_session_id, message=event.text)
    def run(message, **kwargs):
        return {'final_response': '확인했어.\n```process_verification\n{"outcome":"verified","evidence":["artifact checked"],"next_action":"none"}\n```',
            'messages': [], 'turn_id': kwargs['turn_id']}
    result = TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='isolated-test-token', extra={}))
    adapter.gateway_runner = MagicMock()
    adapter._send_with_retry = (AsyncMock(side_effect=OSError('ack lost')) if failure == 'exception'
        else AsyncMock(return_value=SendResult(success=False, error='ReadTimeout: request timed out')))
    if cancel:
        ledger.cancel_for_session(session.session_key)
    elif failure == 'exception':
        with pytest.raises(OSError):
            await adapter.send_final_ledgered(event, session.session_key, result['final_response'], {}, reply_to=None)
    else:
        failed, _ = await adapter.send_final_ledgered(event, session.session_key, result['final_response'], {}, reply_to=None)
        assert not failed.success
    again, _ = await adapter.send_final_ledgered(event, session.session_key, result['final_response'], {}, reply_to=None)
    assert not again.success
    assert adapter._send_with_retry.await_count == (0 if cancel else 1)
    assert delivery.turn_delivery_state(session.session_key, result['turn_id']) == ('abandoned' if cancel else 'uncertain')


@pytest.mark.asyncio
async def test_busy_completion_is_queued_once_and_restored_only_after_owner_exit(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_busy_report', command='true',
        session_key=runner.entry.session_key, parent_session_id=runner.entry.session_id)
    ledger.reserve(session)
    await reconcile(runner)
    for _ in range(3):
        with ledger._db() as db:
            db.execute('UPDATE process_followups SET next_attempt=0')
        await reconcile(runner)
    assert len(runner.transport.events) == 1
    assert ledger.get_state(session.id)['phase'] == 'queued'
    # The in-memory queue disappeared with its owner. Restore this undispatched
    # event once, without restoring a running verification or a completed command.
    original_alive = ledger._owner_alive
    monkeypatch.setattr(ledger, '_owner_alive', lambda _: False)
    restored = Runner(tmp_path)
    await reconcile(restored)
    monkeypatch.setattr(ledger, '_owner_alive', original_alive)
    await reconcile(restored)
    assert len(restored.transport.events) == 1
    assert restored.transport.events[0].metadata['process_followup'] == runner.transport.events[0].metadata['process_followup']


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['verified', 'verification_failed', 'approval_wait'])
@pytest.mark.parametrize('platform', [Platform.TELEGRAM, Platform.DISCORD])
async def test_private_verification_reaches_queued_delivery_with_its_exact_turn(monkeypatch, tmp_path, outcome, platform):
    import json
    from unittest.mock import AsyncMock, MagicMock
    from gateway.delivery_ledger import turn_delivery_state
    from gateway.platforms.base import SendResult
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig
    from gateway.run import _normalize_empty_agent_response
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path, platform)
    session = ProcessSession(id='proc_private_report', command='true',
        session_key=runner.entry.session_key, parent_session_id=runner.entry.session_id)
    ledger.reserve(session)
    await reconcile(runner)
    event = runner.transport.events[0]
    prose = '검증 결과와 남은 작업을 확인했어.'
    assessment = dict(outcome=outcome, evidence=['observed artifact'],
        next_action='none' if outcome == 'verified' else 'Inspect remaining work')
    raw = prose + '\n```process_verification\n' + json.dumps(assessment) + '\n```'
    calls = []
    def run(message, **kwargs):
        calls.append(message)
        return dict(final_response=raw, messages=[], turn_id=kwargs['turn_id'])
    ctx = TurnContext(event=event, source=runner.source, session_key=session.session_key,
        session_id=session.parent_session_id, message=event.text)
    ctx.resolve_display_setting = lambda *_: True
    ctx.interim_assistant_messages_enabled = True
    turn = TurnRunner(runner, ctx)
    assert turn._setup_stream_consumer(platform.value) == (None, None, None, False)
    result = turn._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert result['final_response'] == prose
    assert ledger.get_state(session.id)['verification'] == {**assessment, 'repair_attempts': 0}
    assert ledger.get_state(session.id)['report_text'] == prose
    duplicate = turn._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert duplicate['process_followup_disposition'] == 'skipped'
    assert _normalize_empty_agent_response(duplicate, duplicate['final_response']) == 'NO_REPLY'
    assert len(calls) == 1
    adapter_type = TelegramAdapter if platform == Platform.TELEGRAM else DiscordAdapter
    adapter = adapter_type(PlatformConfig(enabled=True, token='isolated-test-token', extra={}))
    adapter.gateway_runner = MagicMock()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='report-ack'))
    assert await runner._deliver_queued_first_response(result['final_response'], runner.source, adapter,
        session_key=session.session_key, inbound_message_id=event.message_id, deliver_media=False,
        turn_id=result['turn_id'], response_kind=result['response_kind'])
    assert turn_delivery_state(session.session_key, result['turn_id']) == 'delivered'
    adapter.send.assert_awaited_once()
    assert adapter.send.call_args.kwargs['content'] == prose
    with ledger._db() as db:
        db.execute('UPDATE process_followups SET next_attempt=0')
    await reconcile(runner)
    assert ledger.get_state(session.id)['phase'] == ('reported' if outcome == 'verified' else outcome)
    assert len(runner.transport.events) == 1


@pytest.mark.asyncio
async def test_invalidated_late_transport_error_is_consumed(monkeypatch, tmp_path):
    import asyncio, gc
    from agent import deadline
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import attention_inflight, _attention_sends
    from gateway.run import GatewayRunner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    session = ProcessSession(id='proc_late_error', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    release = asyncio.Event()
    async def send(*args, **kwargs):
        await release.wait()
        raise ConnectionError('isolated late transport failure')
    runner.transport.send = send
    monkeypatch.setattr(deadline, 'resolve_timeout', lambda *a, **kw: .05)
    turn = 'process-attention:' + session.id + ':' + row['token']
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        await asyncio.wait_for(reconcile(runner), 2)
        assert attention_inflight(turn)
        ledger.cancel(session.id)
        release.set()
        for _ in range(200):
            if not attention_inflight(turn): break
            await asyncio.sleep(.01)
        assert not attention_inflight(turn)
        assert delivery.turn_delivery_state(session.session_key, turn) == 'abandoned'
        gc.collect()
        await asyncio.sleep(.05)
        assert not errors
    finally:
        release.set()
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_completed_attention_settles_after_caller_cancel(monkeypatch, tmp_path):
    import asyncio, threading
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import _send_attention, _attention_sends, attention_inflight
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_done_cancel', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    turn = 'process-attention:' + session.id + ':' + row['token']
    delivery.record_obligation(obligation_id=turn, session_key=session.session_key,
        platform='telegram', chat_id='4242', thread_id=None, content='attention', turn_id=turn)
    assert delivery.claim_pending_obligation(turn)
    real_authorized = ledger.attention_authorized
    entered, release = threading.Event(), threading.Event()
    probes = 0
    def authorized(*args):
        nonlocal probes
        probes += 1
        if probes == 2:
            entered.set()
            assert release.wait(5)
        return real_authorized(*args)
    monkeypatch.setattr(ledger, 'attention_authorized', authorized)
    sends = []
    async def send(*args, **kwargs):
        sends.append(args)
        return SimpleNamespace(success=True)
    runner.transport.send = send
    caller = asyncio.create_task(_send_attention(runner.transport, runner.source, 'attention', {},
        session.id, row['token'], turn))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert len(sends) == 1
        # Transport callback has run before cancellation interrupts the post-send authority check.
        assert not attention_inflight(turn)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        limit = asyncio.get_running_loop().time() + 3
        while delivery.turn_delivery_state(session.session_key, turn) != 'delivered' and asyncio.get_running_loop().time() < limit:
            await asyncio.sleep(.01)
        # Delivery and follow-up acknowledgment are separate awaited writes.
        await asyncio.wait_for(asyncio.gather(*list(_attention_sends), return_exceptions=True), 10)
        assert delivery.turn_delivery_state(session.session_key, turn) == 'delivered'
        assert ledger.get_state(session.id)['phase'] == 'needs_reconciliation_reported'
        assert not attention_inflight(turn)
        assert len(sends) == 1
    finally:
        release.set()
        if not caller.done(): caller.cancel()
        await asyncio.gather(caller, *list(_attention_sends), return_exceptions=True)


@pytest.mark.asyncio
async def test_delayed_timeout_write_preserves_known_delivery(monkeypatch, tmp_path):
    import asyncio, threading
    from agent import deadline
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import _attention_sends
    from gateway.run import GatewayRunner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    session = ProcessSession(id='proc_timeout_race', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    entered, permit_write = threading.Event(), threading.Event()
    transport_release = asyncio.Event()
    real_uncertain = delivery.mark_uncertain
    def delayed(*args):
        entered.set()
        assert permit_write.wait(5)
        real_uncertain(*args)
    monkeypatch.setattr(delivery, 'mark_uncertain', delayed)
    async def send(*args, **kwargs):
        await transport_release.wait()
        return SimpleNamespace(success=True)
    runner.transport.send = send
    monkeypatch.setattr(deadline, 'resolve_timeout', lambda *a, **kw: .05)
    turn = 'process-attention:' + session.id + ':' + row['token']
    pending = asyncio.create_task(reconcile(runner))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        transport_release.set()
        for _ in range(200):
            if delivery.turn_delivery_state(session.session_key, turn) == 'delivered': break
            await asyncio.sleep(.01)
        assert delivery.turn_delivery_state(session.session_key, turn) == 'delivered'
        permit_write.set()
        await asyncio.wait_for(pending, 5)
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)
        assert delivery.turn_delivery_state(session.session_key, turn) == 'delivered'
        assert ledger.get_state(session.id)['phase'] == 'needs_reconciliation_reported'
    finally:
        transport_release.set(); permit_write.set()
        await asyncio.gather(pending, *list(_attention_sends), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_delivery_execution_and_duplicate_fencing(monkeypatch, tmp_path, cancelled):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_gateway', command='true', remote_root='/tmp/isolated',
                             session_key=runner.entry.session_key, parent_session_id=runner.entry.session_id,
                             notify_on_complete=True, watcher_platform='telegram', watcher_chat_id='4242')
    registry = ProcessRegistry()
    registry._running[session.id] = session
    registry._finish_exited(session, 0)
    await reconcile(runner)
    assert len(runner.transport.events) == 1
    event = runner.transport.events[0]
    assert event.metadata['process_followup']['execution_id'] == session.id
    if cancelled:
        ledger.cancel_for_session(session.session_key)
    calls = []
    def run(message, **kwargs):
        calls.append(message)
        assert 'process_verification' in message
        marker = runner.session_store._entries[session.session_key].active_turn
        assert marker['process_followup'] == event.metadata['process_followup']
        return {'final_response': 'verified', 'messages': []}
    agent = SimpleNamespace(run_conversation=run)
    ctx = TurnContext(event=event, source=runner.source, session_key=session.session_key,
                      session_id=session.parent_session_id, message=event.text)
    turn = TurnRunner(runner, ctx)
    for _ in range(2):
        turn._run_conversation_with_approval(agent, [], None, None, None)
    assert len(calls) == (0 if cancelled else 1)
    assert ledger.get_state(session.id)['phase'] == ('cancelled' if cancelled else 'turn_finished')
    await reconcile(runner)
    assert len(runner.transport.events) == 1


@pytest.mark.asyncio
async def test_report_requires_real_delivery_receipt(monkeypatch, tmp_path):
    from gateway.delivery_ledger import record_obligation, mark_delivered
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_report', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token'])
    assert ledger.begin(session.id, row['token'])
    result = {'final_response': '```process_verification\n{"outcome":"verified","evidence":["checked artifact"],"next_action":"none","repair_attempts":0}\n```'}
    assert ledger.finish(session.id, row['token'], result, 'verified-turn')
    record_obligation(obligation_id='test-report', session_key=session.session_key,
                      platform='telegram', chat_id='4242', thread_id=None, content='verified',
                      turn_id='verified-turn')
    def due():
        with ledger._db() as db:
            db.execute('UPDATE process_followups SET next_attempt=0')
    due()
    await reconcile(runner)
    assert ledger.get_state(session.id)['phase'] == 'turn_finished'
    import tools.process_registry as module
    registry = ProcessRegistry()
    registry._finished[session.id] = session
    session.exited = True
    session.append_output('verified artifact')
    registry.read_log(session.id)
    monkeypatch.setattr(module, 'process_registry', registry)
    assert registry.is_completion_consumed(session.id)
    due()
    await reconcile(runner)
    state = ledger.get_state(session.id)
    assert state['phase'] == 'turn_finished'
    assert state.get('report_delivered', False) is False
    mark_delivered('test-report')
    due()
    await reconcile(runner)
    assert ledger.get_state(session.id)['phase'] == 'reported'
    assert runner.transport.events == []


@pytest.mark.asyncio
async def test_interrupted_verification_reports_waiting_without_model_replay(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    notices = []
    async def send(chat, text, **kwargs):
        notices.append(text)
        return SimpleNamespace(success=True)
    runner.transport.send = send
    session = ProcessSession(id='proc_crash', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token'])
    assert ledger.begin(session.id, row['token'])
    from gateway.run_startup import GatewayStartupMixin
    assert runner.session_store.begin_active_turn(runner.entry.session_key, 'crashed-turn', 'old-boot',
        process_followup={'execution_id': session.id, 'token': row['token']})
    runner._resume_pending_candidates = lambda platform: [runner.entry]
    assert GatewayStartupMixin._schedule_resume_pending_sessions(runner) == 0
    assert runner.entry.active_turn is None
    with ledger._db() as db:
        db.execute('UPDATE process_followups SET next_attempt=0')
    await reconcile(runner)
    assert len(notices) == 1 and 'needs reconciliation' in notices[0]
    assert ledger.get_state(session.id)['phase'] == 'needs_reconciliation_reported'
    assert runner.transport.events == []


def test_abandoned_report_is_terminal_delivery_failure(monkeypatch, tmp_path):
    from gateway.delivery_ledger import record_obligation, turn_delivery_state, _update_state
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    record_obligation(obligation_id='abandoned-test', session_key='owner', platform='telegram',
                      chat_id='4242', thread_id=None, content='result', turn_id='turn')
    _update_state('abandoned-test', 'abandoned')
    assert turn_delivery_state('owner', 'turn') == 'abandoned'


@pytest.mark.parametrize('after_dispatch', [False, True])
def test_cancel_at_real_turn_dispatch_boundary(monkeypatch, tmp_path, after_dispatch):
    from gateway.platforms.base import MessageEvent
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_boundary', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token'])
    event = MessageEvent(text='check', source=runner.source,
                         metadata={'process_followup': {'execution_id': session.id, 'token': row['token']}})
    original = runner.session_store.begin_active_turn
    def marker(*args, **kwargs):
        result = original(*args, **kwargs)
        if not after_dispatch:
            ledger.cancel_for_session(session.session_key)
        return result
    monkeypatch.setattr(runner.session_store, 'begin_active_turn', marker)
    calls = []
    def run(message, **kwargs):
        calls.append(message)
        ledger.cancel_for_session(session.session_key)
        return {'final_response': 'effect happened', 'messages': []}
    ctx = TurnContext(event=event, source=runner.source, session_key=session.session_key,
                      session_id=runner.entry.session_id, message='check')
    TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert len(calls) == int(after_dispatch)
    assert ledger.get_state(session.id)['phase'] == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('crash_point', ['after_send', 'after_receipt'])
async def test_attention_crash_keeps_delivery_obligation_without_blind_resend(monkeypatch, tmp_path, crash_point):
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    session = ProcessSession(id='proc_attention', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db:
        db.execute('UPDATE process_followups SET next_attempt=0')
    calls = []
    async def send(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(success=True)
    runner.transport.send = send
    def crash(*args):
        raise SystemExit('simulated process death')
    original = delivery.mark_delivered if crash_point == 'after_send' else ledger.acknowledge_attention
    module = delivery if crash_point == 'after_send' else ledger
    name = 'mark_delivered' if crash_point == 'after_send' else 'acknowledge_attention'
    monkeypatch.setattr(module, name, crash)
    with pytest.raises(SystemExit):
        await reconcile(runner)
    monkeypatch.setattr(module, name, original)
    turn_id = 'process-attention:' + session.id + ':' + row['token']
    if crash_point == 'after_send':
        assert delivery.turn_delivery_state(session.session_key, turn_id) == 'pending'
    else:
        assert delivery.turn_delivery_state(session.session_key, turn_id) == 'delivered'
    # A fresh interpreter has no in-memory deduplication or outstanding send tasks.
    import subprocess, sys, os
    from pathlib import Path
    restarted_effect = tmp_path / 'restart-send'
    consumer = r"""
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path.cwd() / 'tests/gateway'))
from test_durable_process_followups import Runner
from gateway.run import GatewayRunner
from gateway.process_followups import reconcile
runner=Runner(Path(os.environ['HERMES_HOME']))
runner._thread_metadata_for_target=GatewayRunner._thread_metadata_for_target.__get__(runner)
async def send(*args,**kwargs):
    Path(sys.argv[1]).write_text('repeated')
    return SimpleNamespace(success=True)
runner.transport.send=send
asyncio.run(reconcile(runner))
"""
    subprocess.run([sys.executable,'-c',consumer,str(restarted_effect)],env=os.environ.copy(),check=True,timeout=20)
    assert not restarted_effect.exists()
    assert len(calls) == 1
    if crash_point == 'after_receipt':
        assert ledger.get_state(session.id)['phase'] == 'needs_reconciliation_reported'


@pytest.mark.asyncio
async def test_confirmed_remote_cancel_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_cancel_report', command='sleep 60', remote_root='/tmp/isolated',
        session_key=runner.entry.session_key, parent_session_id=runner.entry.session_id,
        notify_on_complete=True, watcher_platform='telegram', watcher_chat_id='4242', cancel_confirmed=True)
    registry = ProcessRegistry()
    registry._running[session.id] = session
    registry._finish_exited(session, -15)
    await reconcile(runner)
    assert len(runner.transport.events) == 1
    assert ledger.get_state(session.id)['phase'] == 'queued'

    assert 'detached descendants may remain' in runner.transport.events[0].text
    assert 'completed normally' not in runner.transport.events[0].text


@pytest.mark.asyncio
async def test_invalid_parent_classification_never_dispatches(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._classify_completion_target = AsyncMock(return_value='alive')
    ledger.reserve(ProcessSession(id='proc_invalid', command='true', parent_session_id='parent'))
    await reconcile(runner)
    assert not runner.transport.events
    assert ledger.get_state('proc_invalid')['phase'] == 'pending'


@pytest.mark.asyncio
async def test_reset_cancels_waiting_attention_send_and_retry(monkeypatch, tmp_path):
    import asyncio
    from gateway.run import GatewayRunner
    from gateway.delivery_ledger import turn_delivery_state
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    entered, stopped = asyncio.Event(), asyncio.Event()
    sent = []
    async def send(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
            sent.append(args)
        finally:
            stopped.set()
    runner.transport.send = send
    session = ProcessSession(id='proc_reset_notice', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db:
        db.execute('UPDATE process_followups SET next_attempt=0')
    task = asyncio.create_task(reconcile(runner))
    await asyncio.wait_for(entered.wait(), 3)
    from gateway.platforms.event import MessageEvent
    reset = object.__new__(GatewayRunner)
    from unittest.mock import AsyncMock, MagicMock
    reset.__dict__.update(runner.__dict__)
    reset.config = GatewayConfig()
    reset._session_key_for_source = lambda source: session.session_key
    reset._invalidate_session_run_generation = MagicMock()
    reset._release_running_agent_state = MagicMock()
    reset._cleanup_old_agent_for_reset = AsyncMock()
    reset._evict_cached_agent = MagicMock()
    reset._clear_conversation_scope = MagicMock()
    reset._fire_session_reset_hooks = AsyncMock()
    reset._reset_notice_session_info = lambda source: ''
    reset.adapters = {}
    reset._pending_messages = {}
    await reset._handle_reset_command(MessageEvent(text='/reset', source=runner.source))
    await asyncio.wait_for(task, 3)
    assert stopped.is_set() and not sent
    turn = 'process-attention:' + session.id + ':' + row['token']
    assert turn_delivery_state(session.session_key, turn) == 'abandoned'


@pytest.mark.asyncio
async def test_attention_deadline_survives_transport_suppressing_cancel(monkeypatch, tmp_path):
    import asyncio
    from gateway.process_followups import _send_attention, _attention_sends
    from agent import deadline
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_stuck_send', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    release = asyncio.Event()
    async def stubborn(*args, **kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return SimpleNamespace(success=False)
    runner.transport.send = stubborn
    monkeypatch.setattr(deadline, 'resolve_timeout', lambda *a, **k: .05)
    try:
        with pytest.raises(TimeoutError, match='deadline'):
            await asyncio.wait_for(_send_attention(runner.transport, runner.source, 'notice', {},
                session.id, row['token'], 'process-attention:' + session.id + ':' + row['token']), 1)
    finally:
        release.set()
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)


@pytest.mark.parametrize('sweep', ['runtime', 'restart'])
def test_cancelled_attention_retry_fenced_when_cleanup_failed(monkeypatch, tmp_path, sweep):
    import time
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_retry_cancel', command='true', session_key='owner')
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    turn = 'process-attention:' + session.id + ':' + row['token']
    delivery.record_obligation(obligation_id=turn, session_key='owner', platform='telegram', chat_id='4242',
                               thread_id=None, content='notice', turn_id=turn)
    delivery.mark_failed(turn, 'send failed')
    def fail_cleanup(*args):
        raise OSError(5, 'ledger unavailable')
    monkeypatch.setattr(delivery, 'abandon_obligation', fail_cleanup)
    with pytest.raises(OSError):
        ledger.cancel_for_session('owner')
    assert ledger.get_state(session.id)['phase'] == 'cancelled'
    if sweep == 'restart':
        with delivery._transaction() as db:
            db.execute('UPDATE delivery_obligations SET owner_pid=999999999,owner_started_at=0')
        claimed = delivery.sweep_recoverable(now=time.time() + 1000)
    else:
        claimed = delivery.sweep_failed_for_runtime('telegram', now=time.time() + 1000)
    assert claimed == []
    assert delivery.turn_delivery_state('owner', turn) == 'abandoned'


@pytest.mark.asyncio
async def test_watcher_drains_events_while_reconciliation_is_stalled(monkeypatch, tmp_path):
    import asyncio
    from gateway import process_followups
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._running = True
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def stalled(_runner):
        entered.set()
        await release.wait()
    async def drain(queue):
        drained.set()
        runner._running = False
    monkeypatch.setattr(process_followups, 'reconcile_all', stalled)
    runner._drain_watch_notifications = drain
    try:
        await asyncio.wait_for(runner._async_delegation_watcher(interval=.01), 5)
        assert drained.is_set() and entered.is_set()
    finally:
        release.set()
        task = getattr(runner, '_process_reconcile_task', None)
        if task:
            await task


def test_normal_turn_captures_task_request_without_leaking_context(monkeypatch, tmp_path):
    from gateway.platforms.base import MessageEvent
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    request = 'Create report, verify total against source, and tell me the result.'
    event = MessageEvent(text=request, source=runner.source)
    ctx = TurnContext(event=event, source=runner.source, session_key=runner.entry.session_key,
                      session_id=runner.entry.session_id, message=request)
    seen = []
    def run(message, **kwargs):
        seen.append(ledger.task_request())
        return {'final_response': 'done', 'messages': []}
    TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert seen == [request]
    assert ledger.task_request() == ''


def test_attention_authority_outage_preserves_retry_and_other_messages(monkeypatch, tmp_path, caplog):
    import sqlite3,time
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    for obligation in ('process-attention:proc_outage:token', 'ordinary'):
        delivery.record_obligation(obligation_id=obligation, session_key='owner', platform='telegram',
                                   chat_id='4242', thread_id=None, content='message', turn_id=obligation)
        delivery.mark_failed(obligation, 'send failed')
    def unavailable(*args):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(ledger, 'attention_authorized', unavailable)
    claimed = delivery.sweep_failed_for_runtime('telegram', now=time.time() + 1000)
    assert [row['obligation_id'] for row in claimed] == ['ordinary']
    assert delivery.turn_delivery_state('owner', 'process-attention:proc_outage:token') == 'pending'
    assert 'proc_outage' in caplog.text
    assert 'proc_outage:token' not in caplog.text



@pytest.mark.asyncio
async def test_attention_pending_claim_failure_retried(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    runner=Runner(tmp_path)
    runner._thread_metadata_for_target=GatewayRunner._thread_metadata_for_target.__get__(runner)
    session=ProcessSession(id='proc_claimretry',command='true',session_key=runner.entry.session_key)
    ledger.reserve(session); row=ledger.pending()[0]
    assert ledger.admission(session.id,row['token']) and ledger.begin(session.id,row['token'])
    ledger.require_reconciliation(session.id,row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    calls=[]
    async def send(*args,**kwargs):
        calls.append(args); return SimpleNamespace(success=True)
    runner.transport.send=send
    original=delivery.claim_pending_obligation
    def failed(*args): raise OSError('claim temporarily unavailable')
    monkeypatch.setattr(delivery,'claim_pending_obligation',failed)
    await reconcile(runner)
    assert calls == []
    assert delivery.turn_delivery_state(session.session_key,
        'process-attention:' + session.id + ':' + row['token']) == 'pending'
    assert ledger.get_state(session.id)['phase'] == 'needs_reconciliation'
    monkeypatch.setattr(delivery,'claim_pending_obligation',original)
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    await reconcile(runner)
    assert len(calls)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('preparation', ['metadata', 'routing'])
async def test_attention_preparation_failure_retains_unsent_retry(monkeypatch, tmp_path, preparation, caplog):
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    metadata = GatewayRunner._thread_metadata_for_target.__get__(runner)
    runner._thread_metadata_for_target = metadata
    session = ProcessSession(id='proc_prepareretry', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    calls = []
    async def send(*args, **kwargs):
        calls.append(args); return SimpleNamespace(success=True)
    runner.transport.send = send
    def failed(*args, **kwargs):
        raise RuntimeError('temporary message preparation failure https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi/sendMessage')
    if preparation == 'metadata':
        runner._thread_metadata_for_target = failed
    else:
        runner.transport.prime_routing_cache = failed
    await reconcile(runner)
    assert calls == []
    assert 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi' not in caplog.text
    assert 'Process reconciliation delivery pending' in caplog.text
    runner._thread_metadata_for_target = metadata
    runner.transport.prime_routing_cache = lambda *args: None
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    await reconcile(runner)
    assert len(calls) == 1
    assert delivery.turn_delivery_state(session.session_key,
        'process-attention:' + session.id + ':' + row['token']) == 'delivered'


@pytest.mark.asyncio
async def test_busy_followup_db_does_not_block_event_loop(monkeypatch, tmp_path):
    import asyncio, sqlite3, time
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    runner=Runner(tmp_path)
    session=ProcessSession(id='proc_lockresponsive',command='true',session_key=runner.entry.session_key)
    ledger.reserve(session)
    from tools.process_registry_followups import admission
    from gateway.process_followups import reconcile
    conn=sqlite3.connect(tmp_path/'state.db',check_same_thread=False)
    conn.execute('BEGIN IMMEDIATE')
    ticks=[]
    async def heartbeat():
        await asyncio.sleep(.05); ticks.append(time.monotonic()); conn.rollback()
    start=time.monotonic()
    try:
        await asyncio.gather(reconcile(runner),heartbeat())
    finally: conn.close()
    assert ticks[0]-start < .5



@pytest.mark.asyncio
async def test_late_attention_success_has_no_concurrent_retry(monkeypatch, tmp_path):
    import asyncio, time
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import attention_inflight
    from agent import deadline
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    runner=Runner(tmp_path)
    runner._thread_metadata_for_target=GatewayRunner._thread_metadata_for_target.__get__(runner)
    session=ProcessSession(id='proc_latesuccess',command='true',session_key=runner.entry.session_key)
    ledger.reserve(session); row=ledger.pending()[0]
    assert ledger.admission(session.id,row['token']) and ledger.begin(session.id,row['token'])
    ledger.require_reconciliation(session.id,row['token'])
    with ledger._db() as db:db.execute('UPDATE process_followups SET next_attempt=0')
    release=asyncio.Event(); calls=[]
    async def stubborn(*args,**kwargs):
        calls.append(args)
        try:await release.wait()
        except asyncio.CancelledError:await release.wait()
        return SimpleNamespace(success=True)
    runner.transport.send=stubborn
    monkeypatch.setattr(deadline,'resolve_timeout',lambda *a,**k:.05)
    turn='process-attention:'+session.id+':'+row['token']
    try:
        await asyncio.wait_for(reconcile(runner),1)
        assert attention_inflight(turn)
        assert delivery.sweep_failed_for_runtime('telegram',now=time.time()+1000)==[]
        with ledger._db() as db:db.execute('UPDATE process_followups SET next_attempt=0')
        await reconcile(runner)
        assert len(calls)==1
        release.set()
        for _ in range(100):
            if not attention_inflight(turn):break
            await asyncio.sleep(.01)
        assert delivery.turn_delivery_state(session.session_key,turn)=='delivered'
        assert ledger.get_state(session.id)['phase']=='needs_reconciliation_reported'
        assert len(calls)==1
    finally:
        release.set()
        from gateway.process_followups import _attention_sends
        await asyncio.gather(*list(_attention_sends),return_exceptions=True)


@pytest.mark.asyncio
async def test_attention_timeout_never_cancels_accepted_transport(monkeypatch, tmp_path):
    import asyncio, time
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import attention_inflight, _attention_sends
    from agent import deadline
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    session = ProcessSession(id='proc_accepted', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id,row['token']) and ledger.begin(session.id,row['token'])
    ledger.require_reconciliation(session.id,row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    release = asyncio.Event(); accepted = []; cancelled = []
    async def send(*args, **kwargs):
        accepted.append(args)
        try: await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return SimpleNamespace(success=True)
    runner.transport.send = send
    monkeypatch.setattr(deadline, 'resolve_timeout', lambda *a, **k: .05)
    turn = 'process-attention:'+session.id+':'+row['token']
    try:
        await asyncio.wait_for(reconcile(runner), 1)
        await asyncio.sleep(.05)
        assert not cancelled
        assert attention_inflight(turn)
        assert delivery.sweep_failed_for_runtime('telegram', now=time.time()+1000) == []
        release.set()
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)
        for _ in range(100):
            if not attention_inflight(turn): break
            await asyncio.sleep(.01)
        assert delivery.turn_delivery_state(session.session_key, turn) == 'delivered'
        assert len(accepted) == 1
    finally:
        release.set()
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)


def test_dead_owner_attention_send_requires_manual_reconciliation(monkeypatch, tmp_path):
    import json, os, subprocess, sys, time
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_restartattention', command='true', session_key='telegram:dm:4242')
    ledger.reserve(session)
    row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    turn = 'process-attention:'+session.id+':'+row['token']
    worker = '''
import json,sys
from gateway.delivery_ledger import record_obligation,mark_attempting
turn=json.loads(sys.stdin.read())
record_obligation(obligation_id=turn,session_key='telegram:dm:4242',platform='telegram',chat_id='4242',thread_id=None,content='attention',turn_id=turn)
mark_attempting(turn)
'''
    subprocess.run([sys.executable, '-c', worker], input=json.dumps(turn), text=True,
                   env=os.environ.copy(), check=True)
    assert delivery.sweep_recoverable(now=time.time()+30) == []
    assert delivery.turn_delivery_state(session.session_key, turn) == 'uncertain'
    assert delivery.sweep_recoverable(now=time.time()+60) == []


@pytest.mark.asyncio
async def test_late_rejected_attention_arms_existing_retry(monkeypatch, tmp_path):
    import asyncio, time
    from agent import deadline
    from gateway import delivery_ledger as delivery
    from gateway.process_followups import _attention_sends
    from gateway.run import GatewayRunner
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner._thread_metadata_for_target = GatewayRunner._thread_metadata_for_target.__get__(runner)
    session = ProcessSession(id='proc_late_rejected', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    with ledger._db() as db: db.execute('UPDATE process_followups SET next_attempt=0')
    release = asyncio.Event()
    sends, retries = [], []
    async def send(*args, **kwargs):
        sends.append(args)
        if len(sends) == 1:
            await release.wait()
            return SimpleNamespace(success=False, error='send_path_degraded')
        return SimpleNamespace(success=True)
    runner.transport.send = send
    runner._schedule_flood_redelivery = lambda platform, **kw: retries.append((platform, kw))
    monkeypatch.setattr(deadline, 'resolve_timeout', lambda *a, **kw: .05)
    turn = 'process-attention:' + session.id + ':' + row['token']
    try:
        await reconcile(runner)
        assert len(sends) == 1 and not retries
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(_attention_sends), return_exceptions=True), 10)
        # A send's done callback can create its tracked settlement after gather captured the send.
        await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.gather(*list(_attention_sends), return_exceptions=True), 10)
        assert retries == [(runner.source.platform, {'profile': None})]
        claimed = delivery.sweep_failed_for_runtime('telegram', now=time.time() + 100)
        assert len(claimed) == 1
        async def adapter(_row): return runner.transport
        async def arm(): pass
        runner._obligation_adapter = adapter
        runner._arm_flood_timers_for_waiting_rows = arm
        assert await GatewayRunner._redeliver_claimed_obligations(runner, claimed) == 1
        assert delivery.turn_delivery_state(session.session_key, turn) == 'delivered'
        assert len(sends) == 2 and not runner.transport.events
    finally:
        release.set()
        await asyncio.gather(*list(_attention_sends), return_exceptions=True)


@pytest.mark.parametrize('caller', ['runtime', 'startup'])
def test_attention_storage_failure_cannot_enable_resend_after_owner_exit(monkeypatch, tmp_path, caller):
    import json, os, subprocess, sys, time
    from gateway import delivery_ledger as delivery
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    session = ProcessSession(id='proc_storage_restart', command='true', session_key='telegram:dm:4242')
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token']) and ledger.begin(session.id, row['token'])
    ledger.require_reconciliation(session.id, row['token'])
    turn = 'process-attention:' + session.id + ':' + row['token']
    worker = '''
import asyncio,json,os,sys
from types import SimpleNamespace
from gateway import delivery_ledger as delivery
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.process_followups import _report_attention
from gateway.run import GatewayRunner
from agent import deadline
packet=json.loads(sys.stdin.read()); row=packet['row']; turn=packet['turn']; sends=[]
async def send(*args, **kwargs):
 sends.append(True)
 await asyncio.Event().wait()
adapter=SimpleNamespace(send=send)
source=SessionSource(platform=Platform.TELEGRAM,chat_id='4242',chat_type='dm')
async def get_adapter(row):return adapter
async def arm():pass
runner=SimpleNamespace(_thread_metadata_for_target=lambda *a,**k:{},_obligation_adapter=get_adapter,_arm_flood_timers_for_waiting_rows=arm)
original=delivery.mark_uncertain; writes=[]
def fault(*args):
 writes.append(True)
 if len(writes)<=2:raise OSError(28,'isolated uncertain-state write failure')
 return original(*args)
delivery.mark_uncertain=fault
deadline.resolve_timeout=lambda *a,**k:.05
async def main():
 if packet['caller']=='runtime':
  await _report_attention(runner,adapter,source,row,{'session_key':'telegram:dm:4242'})
 else:
  delivery.record_obligation(obligation_id=turn,session_key='telegram:dm:4242',platform='telegram',chat_id='4242',thread_id=None,content='attention',turn_id=turn)
  assert delivery.claim_pending_obligation(turn)
  await GatewayRunner._redeliver_claimed_obligations(runner,[dict(obligation_id=turn,platform='telegram',chat_id='4242',thread_id=None,content='attention',attempts=1)])
 print(json.dumps(dict(state=delivery.turn_delivery_state('telegram:dm:4242',turn),sends=len(sends),writes=len(writes))),flush=True)
 # Actual owner death before its accepted transport can settle.
 os._exit(0)
asyncio.run(main())
'''
    producer = subprocess.run([sys.executable, '-c', worker], input=json.dumps(dict(row=row, turn=turn, caller=caller)),
        text=True, capture_output=True, env=os.environ.copy(), timeout=15, check=True)
    report = json.loads(producer.stdout.strip().splitlines()[-1])
    assert report['sends'] == 1 and report['writes'] >= 2
    assert report['state'] in {'attempting', 'uncertain'}, report
    assert delivery.sweep_recoverable(now=time.time()+30) == []
    assert delivery.turn_delivery_state(session.session_key, turn) == 'uncertain'


def test_live_turn_storage_failure_preserves_actual_cause(monkeypatch, tmp_path):
    from gateway.platforms.base import MessageEvent
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    session = ProcessSession(id='proc_marker_error', command='true', session_key=runner.entry.session_key)
    ledger.reserve(session); row = ledger.pending()[0]
    assert ledger.admission(session.id, row['token'])
    def storage_failure(*args, **kwargs):
        raise OSError(28, 'turn marker disk full; token=sk-testSensitiveReasonKey123456789')
    monkeypatch.setattr(runner.session_store, 'begin_active_turn', storage_failure)
    calls = []
    def run(message, **kwargs): calls.append(message); return {}
    event = MessageEvent(text='check', source=runner.source,
        metadata={'process_followup': {'execution_id': session.id, 'token': row['token']}})
    ctx = TurnContext(event=event, source=runner.source, session_key=session.session_key,
        session_id=runner.entry.session_id, message='check')
    with pytest.raises(OSError):
        TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    state = ledger.get_state(session.id)
    assert state['phase'] == 'needs_reconciliation' and not calls
    assert 'Live verification turn failed' in state['reason'] and 'errno=28' in state['reason']
    assert 'Gateway restarted' not in state['reason'] and 'sk-testSensitiveReasonKey' not in state['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['admission', 'profile'])
async def test_followup_error_logs_redact_exception_credentials(monkeypatch, tmp_path, caplog, path):
    from gateway.process_followups import reconcile_all
    from tools.process_registry import process_registry
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = Runner(tmp_path)
    runner.config = GatewayConfig()
    sentinel = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
    def failed(*args, **kwargs):
        raise RuntimeError('https://api.telegram.org/bot123456789:' + sentinel + '/sendMessage')
    if path == 'admission':
        session = ProcessSession(id='proc_redactadmit', command='true', session_key=runner.entry.session_key)
        ledger.reserve(session)
        async def inject(*args, **kwargs):
            failed()
        runner._inject_watch_notification = inject
        await reconcile(runner)
        assert 'Process follow-up admission failed' in caplog.text
    else:
        monkeypatch.setattr(process_registry, 'retry_checkpoint_recovery', lambda: None)
        monkeypatch.setattr(ledger, 'pending', failed)
        await reconcile_all(runner)
        assert 'Process follow-up reconciliation failed' in caplog.text
    assert sentinel not in caplog.text
    assert not any(record.exc_info for record in caplog.records)
