"""User input survives read-only verification; launch-approved work resumes once."""

import time
import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.process_followups import reconcile
from gateway.run import GatewayRunner
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore, build_session_key
from gateway.turn_context import TurnContext
from tools import process_registry_followups as ledger
from tools.process_registry import ProcessSession
from tests.waiting import async_wait_until


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('gateway.run._hermes_home', tmp_path)
    return tmp_path


def busy_runner(mode, *, followup=True):
    runner = object.__new__(GatewayRunner)
    runner._busy_input_mode = mode
    runner._draining = False
    runner.config = GatewayConfig()
    source = SessionSource(platform=Platform.DISCORD, chat_id='chat', user_id='human', chat_type='dm')
    key = build_session_key(source)
    agent = MagicMock()
    agent._supports_active_turn_redirect = True
    agent.steer.return_value = True
    agent.redirect.return_value = True
    agent._active_children = []
    state = runner._session_state(key)
    state.turn.agent = agent
    state.turn.event = MessageEvent(text='verify', source=source, internal=followup,
        metadata={'process_followup': {'execution_id': 'proc_test', 'token': 'token'}} if followup else {})
    adapter = SimpleNamespace(_pending_messages={})
    adapter.get_pending_message = lambda key: adapter._pending_messages.pop(key, None)
    runner._delivery_adapter_for = lambda _: adapter
    runner._effective_busy_input_mode = lambda _: mode
    runner._effective_busy_text_mode = lambda _: 'steer'
    runner._check_slash_access = lambda *_: None
    runner._is_user_authorized_for_source = lambda _: True
    runner._admit_bot_message_for_source = lambda _: True
    return runner, source, key, agent, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['steer', 'interrupt', 'queue'])
@pytest.mark.parametrize('entry', ['inbound', 'adapter'])
async def test_user_message_queued_and_drained_after_verification(home, mode, entry):
    runner, source, key, agent, adapter = busy_runner(mode)
    event = MessageEvent(text='run the approved tests', source=source, message_id='new-user')
    if entry == 'inbound':
        await runner._hm_handle_running_session_message(event, source, key)
    else:
        await runner._handle_active_session_busy_message(event, key)
    agent.steer.assert_not_called()
    agent.redirect.assert_not_called()
    agent.interrupt.assert_not_called()
    assert adapter._pending_messages[key] is event
    drained, text = await runner._run_agent_drain_pending({'final_response': 'report'}, adapter, source, key)
    assert drained is event and text == event.text
    assert not drained.internal and not drained.metadata.get('process_followup')
    assert await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key) == (None, None)


class Transport:
    supports_async_delivery = True

    def __init__(self):
        self.events = []
        self.send = AsyncMock(return_value=SimpleNamespace(success=True))
        from gateway.platforms.base import BasePlatformAdapter
        self.extract_media = BasePlatformAdapter.extract_media

    async def handle_message(self, event):
        self.events.append(event)
        event._gateway_accepted = True


class CompletionRunner(GatewayNotificationsMixin):
    def __init__(self, home):
        self.config = GatewayConfig()
        self.session_store = SessionStore(home / 'sessions', self.config)
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id='4242', chat_type='dm', thread_id='topic')
        self.entry = self.session_store.get_or_create_session(self.source)
        self.transport = Transport()
        self._boot_id = 'continuity-test'

    def _consume_pending_native_image_paths(self, key):
        return []

    def _build_process_event_source(self, evt):
        return self.source

    def _resolve_injection_adapter(self, platform, source):
        return self.transport

    def _delivery_adapter_for(self, source):
        return self.transport

    def _thread_metadata_for_target(self, *args, **kwargs):
        return {'thread_id': self.source.thread_id}

    async def _classify_completion_target(self, parent):
        return 'deliver'


def armed_session(runner, execution='proc_continue'):
    from tools.process_registry_followups import _owner_for_pid
    import os
    session = ProcessSession(id=execution, command='true', session_key=runner.entry.session_key,
        parent_session_id=runner.entry.session_id, started_at=time.time() - 1, notify_on_complete=True)
    session.continue_on_complete = True
    session.continuation = {'chain': 0, 'chain_id': 'approved-task', 'launched_at': session.started_at,
        'origin_received_at': session.started_at - 1, 'owner': _owner_for_pid(os.getpid())}
    ledger.reserve(session)
    return session


async def verify(runner, on_verify=None):
    await reconcile(runner)
    event = runner.transport.events[-1]
    ctx = TurnContext(event=event, source=runner.source, session_key=runner.entry.session_key,
        session_id=runner.entry.session_id, message=event.text)
    def run(message, **kwargs):
        assert ledger.verification_tool_block('terminal', {'command': 'true'})
        if on_verify is not None:
            on_verify()
        return {'final_response': 'Need the approved test run.\n```process_verification\n'
            '{"outcome":"approval_wait","evidence":["artifact inspected"],"next_action":"run tests"}\n```',
            'messages': [], 'turn_id': kwargs['turn_id']}
    return TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)


@pytest.mark.asyncio
async def test_armed_completion_resumes_once_after_report(home):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding="utf-8")
    runner = CompletionRunner(home)
    armed_session(runner)
    result = await verify(runner)
    from gateway.delivery_ledger import turn_delivery_state
    assert turn_delivery_state(runner.entry.session_key, result['turn_id']) == 'pending'
    with ledger._db() as db:
        db.execute('UPDATE process_followups SET next_attempt=0')
    await reconcile(runner)
    continuations = [e for e in runner.transport.events if e.metadata.get('process_continuation')]
    assert len(continuations) == 1, 'armed approved work must get one normal continuation after its report is recorded'
    event = continuations[0]
    assert event.internal and 'process_followup' not in event.metadata
    assert event.source.thread_id == 'topic'
    ctx = TurnContext(event=event, source=runner.source, session_key=runner.entry.session_key,
        session_id=runner.entry.session_id, message=event.text)
    def resume(message, **kwargs):
        from tools import process_registry_continuation as continuation
        from tools.process_registry import ProcessRegistry
        assert ledger.verification_tool_block('terminal', {'command': 'true'}) is None
        with continuation.launch_scope(True):
            child = ProcessRegistry._new_session('true', 'task', 'task', runner.entry.session_key, str(home))
        assert child.continue_on_complete
        assert child.continuation['chain'] == 1
        assert child.continuation['chain_id'] == 'approved-task'
        return {'final_response': 'tests passed', 'messages': []}
    TurnRunner(runner, ctx)._run_conversation_with_approval(SimpleNamespace(run_conversation=resume), [], None, None, None)
    await reconcile(runner)
    assert len([e for e in runner.transport.events if e.metadata.get('process_continuation')]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['inbound', 'adapter'])
async def test_normal_busy_turn_still_steers(home, entry):
    runner, source, key, agent, adapter = busy_runner('steer', followup=False)
    event = MessageEvent(text='new direction', source=source)
    if entry == 'inbound':
        await runner._hm_handle_running_session_message(event, source, key)
    else:
        await runner._handle_active_session_busy_message(event, key)
    agent.steer.assert_called_once()
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_stop_keeps_escape_hatch_during_verification(home):
    runner, source, key, agent, adapter = busy_runner('steer')
    runner._interrupt_and_clear_session = AsyncMock()
    await runner._hm_handle_running_session_message(MessageEvent(text='/stop', source=source), source, key)
    runner._interrupt_and_clear_session.assert_awaited_once()
    assert not adapter._pending_messages
    agent.steer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['steer', 'interrupt', 'queue'])
async def test_media_queued_after_verification(home, mode):
    from gateway.platforms.event import MessageType
    runner, source, key, agent, adapter = busy_runner(mode)
    event = MessageEvent(text='', source=source, message_type=MessageType.PHOTO, media_urls=['image.png'])
    await runner._hm_handle_running_session_message(event, source, key)
    assert adapter._pending_messages[key] is event
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()


def update_payload(execution, **changes):
    with ledger._db() as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
        payload = json.loads(row['payload'])
        payload.update(changes)
        db.execute('UPDATE process_followups SET payload=? WHERE execution_id=?', (json.dumps(payload), execution))
    if 'session_key' in changes and payload.get('followup_turn_id'):
        from gateway.delivery_ledger import record_obligation
        record_obligation(obligation_id=payload['followup_turn_id'], session_key=payload['session_key'],
            platform='discord', chat_id='chat', thread_id=None, content='verified',
            turn_id=payload['followup_turn_id'])


def continuation_status(execution):
    with ledger._db(read_only=True) as db:
        row = db.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
    return json.loads(row['payload']).get('continuation_status')


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['gate', 'stop', 'stale', 'chain', 'cancelled', 'reconciliation'])
async def test_continuation_guards_keep_report(home, guard):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding="utf-8")
    runner = CompletionRunner(home)
    session = armed_session(runner)
    result = await verify(runner)
    def reconciliation():
        with ledger._db() as db:
            db.execute("UPDATE process_followups SET phase='needs_reconciliation' WHERE execution_id=?", (session.id,))
    actions = {
        'gate': lambda: (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n', encoding="utf-8"),
        'stop': lambda: continuation.record_stop(runner.entry.session_key),
        'stale': lambda: update_payload(session.id, timings={'completion_observed_at': time.time() - 3601}),
        'chain': lambda: update_payload(session.id, continuation={**session.continuation, 'chain': 10}),
        'cancelled': lambda: ledger.cancel(session.id),
        'reconciliation': reconciliation,
    }
    actions[guard]()
    await reconcile(runner)
    assert not [e for e in runner.transport.events if e.metadata.get('process_continuation')]
    assert continuation_status(session.id)['phase'] == 'skipped'
    from gateway.delivery_ledger import turn_delivery_state
    assert turn_delivery_state(runner.entry.session_key, result['turn_id']) == 'pending'


@pytest.mark.asyncio
async def test_unarmed_completion_never_continues(home):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding="utf-8")
    runner = CompletionRunner(home)
    session = armed_session(runner)
    update_payload(session.id, continue_on_complete=False)
    await verify(runner)
    await reconcile(runner)
    assert not [e for e in runner.transport.events if e.metadata.get('process_continuation')]


@pytest.mark.asyncio
async def test_claim_allows_concurrent_delivery_writer(home, monkeypatch):
    from gateway import delivery_ledger as delivery
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding='utf-8')
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    writer_started = threading.Event()
    writer_errors = []
    connect = delivery._connect
    lookup = delivery.turn_delivery_state

    def writer():
        try:
            delivery.record_obligation(obligation_id='concurrent-report', session_key=runner.entry.session_key,
                platform='telegram', chat_id='4242', thread_id='topic', content='another report', turn_id='other-turn')
        except Exception as error:
            writer_errors.append(error)

    def traced_connect():
        connection = connect()
        connection.execute('PRAGMA busy_timeout=1000')
        connection.set_trace_callback(lambda statement: writer_started.set()
            if 'INSERT OR REPLACE INTO delivery_obligations' in statement else None)
        return connection

    with ThreadPoolExecutor(max_workers=1) as executor:
        def lookup_with_writer(key, turn_id):
            executor.submit(writer)
            assert writer_started.wait(5), 'delivery writer did not start'
            return lookup(key, turn_id)

        monkeypatch.setattr(delivery, '_connect', traced_connect)
        monkeypatch.setattr(delivery, 'turn_delivery_state', lookup_with_writer)
        claimed = continuation.claim(session.id)
    assert not writer_errors
    assert claimed is not None
    assert ledger.get_state(session.id)['phase'] != 'needs_reconciliation'


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['busy_stop', 'slash_stop', 'inbound_stop', 'inbound', 'adapter', 'cold'])
async def test_input_dispatch_has_no_continuation_database_dependency(home, monkeypatch, entry):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding='utf-8')
    completion = CompletionRunner(home)
    session = armed_session(completion)
    await verify(completion)
    runner, source, key, agent, _adapter = busy_runner('steer', followup=False)
    update_payload(session.id, session_key=key)
    runner._interrupt_and_clear_session = AsyncMock()
    runner.session_store = completion.session_store
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
        lookup_by_session_key=AsyncMock(return_value=None))
    event = MessageEvent(text='/stop' if entry.endswith('stop') else 'new direction', source=source)
    if entry == 'cold':
        runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
        runner._hm_estop_gate = lambda *_: None
        runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
        runner._hm_evict_idle_stale_agent = lambda *_: None
        runner._is_session_running = lambda *_: False
        runner._hm_dispatch_idle_commands = AsyncMock(return_value=(True, 'dispatched'))

    @contextmanager
    def broken_db(**kwargs):
        raise sqlite3.OperationalError('injected continuation fence failure')
        yield

    with monkeypatch.context() as failing:
        failing.setattr(continuation, '_db', broken_db)
        if entry == 'busy_stop':
            await runner._busy_stop_command(event, key, source)
        elif entry == 'slash_stop':
            await runner._handle_stop_command(event)
        elif entry.startswith('inbound'):
            await runner._hm_handle_running_session_message(event, source, key)
        elif entry == 'cold':
            assert await runner._handle_message(event) == 'dispatched'
        else:
            await runner._handle_active_session_busy_message(event, key)
    if entry.endswith('stop'):
        runner._interrupt_and_clear_session.assert_awaited_once()
        agent.steer.assert_not_called()
    elif entry == 'cold':
        runner._hm_dispatch_idle_commands.assert_awaited_once_with(event, source, key)
    else:
        agent.steer.assert_called_once()
    assert (continuation.claim(session.id) is None) is entry.endswith('stop')


def fence_entry(home, entry):
    runner, source, key, agent, _adapter = busy_runner('steer', followup=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
        lookup_by_session_key=AsyncMock(return_value=None))
    event = MessageEvent(text='/stop' if entry.endswith('stop') else 'new direction', source=source)
    if entry == 'cold':
        runner._hm_admit_event = AsyncMock(return_value=(event, source, False))
        runner._hm_estop_gate = lambda *_: None
        runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
        runner._hm_evict_idle_stale_agent = lambda *_: None
        runner._is_session_running = lambda *_: False
        runner._hm_dispatch_idle_commands = AsyncMock(return_value=(True, 'dispatched'))
    calls = {
        'busy_stop': lambda: runner._busy_stop_command(event, key, source),
        'slash_stop': lambda: runner._handle_stop_command(event),
        'inbound_stop': lambda: runner._hm_handle_running_session_message(event, source, key),
        'inbound': lambda: runner._hm_handle_running_session_message(event, source, key),
        'adapter': lambda: runner._handle_active_session_busy_message(event, key),
        'cold': lambda: runner._handle_message(event),
    }
    return runner, key, agent, calls[entry]


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['busy_stop', 'slash_stop', 'inbound_stop', 'inbound', 'adapter', 'cold'])
@pytest.mark.parametrize('config', ['', 'agent:\n  process_continue_on_complete: false\n',
    'agent:\n  process_continue_on_complete: false\n  process_continue_max_chain: invalid\n'])
async def test_gate_off_input_never_opens_continuation_database(home, monkeypatch, entry, config):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text(config)
    runner, _key, agent, dispatch = fence_entry(home, entry)
    database = MagicMock(side_effect=AssertionError('gate-off input must not open state.db'))
    monkeypatch.setattr(continuation, '_db', database)
    await dispatch()
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    database.assert_not_called()
    if entry.endswith('stop'):
        runner._interrupt_and_clear_session.assert_awaited_once()
    elif entry == 'cold':
        runner._hm_dispatch_idle_commands.assert_awaited_once()
    else:
        agent.steer.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['busy_stop', 'slash_stop', 'inbound_stop', 'inbound', 'adapter', 'cold'])
async def test_locked_database_does_not_delay_dispatch_or_stop(home, monkeypatch, entry):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    completion = CompletionRunner(home)
    session = armed_session(completion)
    await verify(completion)
    runner, key, agent, dispatch = fence_entry(home, entry)
    update_payload(session.id, session_key=key)
    database = MagicMock(side_effect=AssertionError('ingress must not open state.db'))
    writer = sqlite3.connect(home / 'state.db')
    writer.execute('BEGIN IMMEDIATE')
    try:
        started = time.monotonic()
        with monkeypatch.context() as ingress:
            ingress.setattr(continuation, '_db', database)
            await dispatch()
        elapsed = time.monotonic() - started
        if entry.endswith('stop'):
            runner._interrupt_and_clear_session.assert_awaited_once()
        elif entry == 'cold':
            runner._hm_dispatch_idle_commands.assert_awaited_once()
        else:
            agent.steer.assert_called_once()
        assert elapsed < 2, f'dispatch stalled {elapsed:.2f}s behind a SQLite writer'
        database.assert_not_called()
    finally:
        writer.rollback()
        writer.close()
    assert (continuation.claim(session.id) is None) is entry.endswith('stop')


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['busy_stop', 'inbound'])
async def test_unreadable_config_preserves_dispatch_and_quarantines(home, monkeypatch, entry):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    completion = CompletionRunner(home)
    session = armed_session(completion)
    await verify(completion)
    runner, key, agent, dispatch = fence_entry(home, entry)
    update_payload(session.id, session_key=key)
    (home / 'config.yaml').write_text('agent: [')
    await dispatch()
    if entry.endswith('stop'):
        runner._interrupt_and_clear_session.assert_awaited_once()
    else:
        agent.steer.assert_called_once()
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    assert (continuation.claim(session.id) is None) is entry.endswith('stop')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['queue', 'steer_fallback', 'rewrite', 'steer_drain',
    'pending', 'merged', 'interrupt', 'steer_queued', 'steer_interrupt'])
async def test_replayed_input_keeps_origin_before_newer_input(home, monkeypatch, path):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    runner, source, key, agent, adapter = busy_runner('steer', followup=False)
    clock = SimpleNamespace(time=MagicMock(return_value=100.0))
    monkeypatch.setattr(continuation, 'time', clock)
    text = '/queue job' if path == 'queue' else '/steer job' if path.startswith('steer') else 'job'
    original = MessageEvent(text=text, source=source)
    continuation.record_user_event(key, original)
    if path == 'queue':
        await runner._busy_queue_command(original, key, source)
    elif path == 'steer_fallback':
        runner._session_state(key).turn.agent = None
        await runner._busy_steer_command(original, key, source)
    elif path == 'rewrite':
        monkeypatch.setattr('hermes_cli.lifecycle.ainvoke_hook',
            AsyncMock(return_value=[{'action': 'rewrite', 'text': 'job'}]))
        replay = await runner._hm_pre_gateway_dispatch_hook(original, source)
    elif path in {'pending', 'merged'}:
        from gateway.platforms.base import merge_pending_message_event
        merge_pending_message_event(adapter._pending_messages, key, original)
        if path == 'merged':
            merge_pending_message_event(adapter._pending_messages, key,
                MessageEvent(text='same request', source=source), merge_text=True)
    elif path == 'interrupt':
        agent._supports_active_turn_redirect = False
        await runner._hm_busy_interrupt(original, source, agent, key)
    else:
        await runner._busy_steer_command(original, key, source)
        if path == 'steer_queued':
            runner._enqueue_fifo(key, MessageEvent(text='notification', source=source, internal=True), adapter)
        elif path == 'steer_interrupt':
            agent._supports_active_turn_redirect = False
            await runner._hm_busy_interrupt(original, source, agent, key)
    clock.time.return_value = 101.0
    continuation.record_user_event(key, MessageEvent(text='newer request', source=source))
    clock.time.return_value = 102.0
    if path != 'rewrite':
        replay, text = await runner._run_agent_drain_pending(
            {'final_response': 'done', 'pending_steer': 'job' if path in {
                'steer_drain', 'steer_queued', 'steer_interrupt'} else '',
             'interrupted': path in {'interrupt', 'steer_interrupt'},
             'interrupt_message': 'job'},
            adapter, source, key)
        if path == 'steer_interrupt':
            assert replay._process_user_received_at == 100.0
            replay, text = await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key)
    continuation.record_user_event(key, replay)
    tokens = set_session_vars(platform='discord', session_key=key, session_id='origin-session')
    try:
        with continuation.turn_scope(replay, key), continuation.launch_scope(True):
            session = ProcessSession(id='replayed-process', command='true', session_key=key,
                parent_session_id='origin-session', started_at=102.0, notify_on_complete=True)
            continuation.arm_session(session)
        assert session.continue_on_complete
        assert session.continuation['origin_received_at'] == 100.0
        assert replay._process_user_recorded
        ledger.reserve(session)
        update_payload(session.id, verification={'outcome': 'verified'},
            timings={'completion_observed_at': 102.0})
        with ledger._db() as connection:
            connection.execute("UPDATE process_followups SET phase='reported' WHERE execution_id=?", (session.id,))
        from gateway.delivery_ledger import record_obligation
        update_payload(session.id, followup_turn_id='replayed-report')
        record_obligation(obligation_id='replayed-report', session_key=key, platform='discord',
            chat_id='chat', thread_id=None, content='verified', turn_id='replayed-report')
        assert continuation.claim(session.id) is not None
    finally:
        clear_session_vars(tokens)


def test_gate_off_stop_never_writes_database_with_live_authority(home, monkeypatch):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    runner = CompletionRunner(home)
    armed_session(runner)
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
    database = MagicMock(side_effect=AssertionError('disabled stop must not open state.db'))
    monkeypatch.setattr(continuation, '_db', database)
    continuation.record_stop(runner.entry.session_key)
    database.assert_not_called()
    assert not list(home.glob('continuation-stop-*'))


@pytest.mark.asyncio
async def test_pending_fence_quarantines_before_worker_persists(home, monkeypatch):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    started, release = threading.Event(), threading.Event()
    stop_path = continuation._stop_path
    loop_thread = threading.get_ident()

    def delayed_fence(key):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(5)
        return stop_path(key)

    with monkeypatch.context() as pending:
        pending.setattr(continuation, '_stop_path', delayed_fence)
        worker = asyncio.create_task(asyncio.to_thread(continuation.record_stop, runner.entry.session_key))
        try:
            await async_wait_until(started.is_set)
            assert continuation.claim(session.id) is None
            assert continuation_status(session.id)['phase'] == 'skipped'
        finally:
            release.set()
            await worker


@pytest.mark.asyncio
async def test_fence_worker_keeps_receiving_profile(home, monkeypatch):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    other_home = home / 'other-profile'
    other_home.mkdir()
    started, release = threading.Event(), threading.Event()
    load = continuation._agent_config

    def delayed_config():
        started.set()
        assert release.wait(5)
        return load()

    with monkeypatch.context() as delayed:
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        delayed.setattr(continuation, '_agent_config', delayed_config)
        token = set_hermes_home_override(home)
        try:
            worker = asyncio.create_task(asyncio.to_thread(continuation.record_stop, 'profile-session'))
        finally:
            reset_hermes_home_override(token)
        try:
            await async_wait_until(started.is_set)
            monkeypatch.setenv('HERMES_HOME', str(other_home))
        finally:
            release.set()
            await worker
    assert len(list(home.glob('continuation-stop-*'))) == 1
    assert not list(other_home.glob('continuation-stop-*'))
    assert not (other_home / 'state.db').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('followup', [False, True])
@pytest.mark.parametrize('mode', ['steer', 'interrupt'])
@pytest.mark.parametrize('entry', ['inbound', 'adapter'])
async def test_recursive_turn_uses_live_event(home, followup, mode, entry):
    runner, source, key, agent, adapter = busy_runner(mode, followup=not followup)
    agent._supports_active_turn_redirect = False
    live_event = MessageEvent(text='next turn', source=source, internal=followup,
        metadata={'process_followup': {'execution_id': 'next', 'token': 'token'}} if followup else {})
    runner._profile_scope_for_source = lambda _: nullcontext()
    runner._interrupt_running_turn = MagicMock()
    runner._send_busy_interrupt_notice = AsyncMock()

    async def run_live(message, context_prompt, history, source, session_id, **kwargs):
        ctx = TurnContext(event=kwargs['event'], source=source, session_key=key,
            session_id=session_id, agent_holder=[agent])
        await runner._run_agent_track_agent(ctx)
        event = MessageEvent(text='later user input', source=source)
        if entry == 'inbound':
            await runner._hm_handle_running_session_message(event, source, key)
        else:
            await runner._handle_active_session_busy_message(event, key)
        if followup:
            assert adapter._pending_messages[key] is event
            agent.steer.assert_not_called()
            agent.interrupt.assert_not_called()
            runner._interrupt_running_turn.assert_not_called()
        elif mode == 'steer':
            agent.steer.assert_called_once()
            assert not adapter._pending_messages
        else:
            assert runner._interrupt_running_turn.called or agent.interrupt.called
        return {'final_response': 'done', 'messages': []}

    runner._run_agent_inner = run_live
    await runner._run_agent('next', '', [], source, 'session', session_key=key, event=live_event)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', [False, True])
async def test_input_queued_before_launch_invalidates_origin_turn(home, monkeypatch, stop):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding='utf-8')
    runner = CompletionRunner(home)
    key = runner.entry.session_key
    clock = SimpleNamespace(time=MagicMock(return_value=time.time() - 10))
    monkeypatch.setattr(continuation, 'time', clock)
    origin = MessageEvent(text='approved task', source=runner.source)
    continuation.record_user_event(key, origin)
    tokens = set_session_vars(platform='telegram', session_key=key, session_id=runner.entry.session_id)
    try:
        with continuation.turn_scope(origin, key):
            clock.time.return_value += 1
            queued = MessageEvent(text='/stop' if stop else 'new task', source=runner.source)
            continuation.record_user_event(key, queued)
            if stop:
                await asyncio.to_thread(continuation.record_stop, key)
            with continuation.launch_scope(True):
                session = ProcessSession(id='proc_continue', command='true', session_key=key,
                    parent_session_id=runner.entry.session_id, started_at=time.time() - 1,
                    notify_on_complete=True)
                continuation.arm_session(session)
            assert session.continue_on_complete
            ledger.reserve(session)
        continuation.record_user_event(key, queued)
    finally:
        clear_session_vars(tokens)
    clock.time.return_value = time.time()
    await verify(runner)
    await reconcile(runner)
    assert bool([event for event in runner.transport.events if event.metadata.get('process_continuation')]) is not stop
    assert continuation_status(session.id)['phase'] == ('skipped' if stop else 'queued')


def run_continuation(runner, event, run):
    ctx = TurnContext(event=event, source=runner.source, session_key=runner.entry.session_key,
        session_id=runner.entry.session_id, message=event.text)
    return TurnRunner(runner, ctx)._run_conversation_with_approval(
        SimpleNamespace(run_conversation=run), [], None, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['begin', 'finish'])
async def test_continuation_write_failure_preserves_turn_cleanup(home, monkeypatch, caplog, operation):
    from tools.approval_context import get_current_session_key
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding='utf-8')
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    event = runner.transport.events[-1]
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    with monkeypatch.context() as failing:
        failing.setattr(continuation, operation,
            MagicMock(side_effect=sqlite3.OperationalError('injected continuation write failure')))
        result = run_continuation(runner, event, model)
    assert get_current_session_key() != runner.entry.session_key
    assert 'warning' in caplog.text.lower()
    if operation == 'begin':
        assert result['final_response'] == 'NO_REPLY'
        model.assert_not_called()
    else:
        assert result['final_response'] == 'done'
        model.assert_called_once()
        assert continuation_status(session.id)['phase'] == 'running'
        assert continuation.claim(session.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['user', 'stop', 'gate', 'stale'])
async def test_dispatch_rechecks_queued_authorization(home, guard):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding="utf-8")
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    event = runner.transport.events[-1]
    if guard == 'user':
        continuation.record_user_event(runner.entry.session_key, MessageEvent(text='new task', source=runner.source))
    elif guard == 'stop':
        continuation.record_stop(runner.entry.session_key)
    elif guard == 'gate':
        (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n', encoding="utf-8")
    else:
        update_payload(session.id, timings={'completion_observed_at': time.time() - 3601})
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    assert run_continuation(runner, event, model)['final_response'] == ('done' if guard in {'user', 'stale'} else 'NO_REPLY')
    assert model.call_count == (1 if guard in {'user', 'stale'} else 0)


@pytest.mark.asyncio
async def test_restart_during_admission_reclaims_queue_and_fences_old_token(home):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n', encoding="utf-8")
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    event = runner.transport.events[-1]
    assert event.metadata.get('process_continuation')
    status = continuation_status(session.id)
    update_payload(session.id, continuation_status={**status, 'owner': json.dumps([999999999, 0])})
    restarted = CompletionRunner(home)
    await reconcile(restarted)
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    run_continuation(restarted, event, model)
    model.assert_not_called()
    assert not ledger.finish(session.id, 'old-token', {'final_response': 'duplicate'})
    await reconcile(restarted)
    assert len(restarted.transport.events) == 1
    run_continuation(restarted, restarted.transport.events[0], model)
    run_continuation(restarted, restarted.transport.events[0], model)
    model.assert_called_once()
    assert continuation_status(session.id)['phase'] == 'done'
    assert restarted.transport.send.await_count == 0


@pytest.mark.asyncio
async def test_chain_cap_counts_sibling_continuations(home):
    from tools import process_registry_continuation as continuation
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n  process_continue_max_chain: 1\n', encoding="utf-8")
    runner = CompletionRunner(home)
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    event = runner.transport.events[-1]
    def capped(message, **kwargs):
        from tools.process_registry import ProcessRegistry
        with continuation.launch_scope(True):
            child = ProcessRegistry._new_session('true', 'task', 'task', runner.entry.session_key, str(home))
        assert not child.continue_on_complete
        return {'final_response': 'done', 'messages': []}
    run_continuation(runner, event, capped)
    # Another completed sibling from the original launch must not get its own fresh budget.
    with ledger._db() as db:
        row = db.execute('SELECT * FROM process_followups WHERE execution_id=?', (session.id,)).fetchone()
        payload = json.loads(row['payload'])
        payload.pop('continuation_status')
        db.execute('''INSERT INTO process_followups (execution_id,parent_session_id,payload,phase,token,updated_at)
            VALUES ('sibling',?,?,?,?,?)''', (row['parent_session_id'], json.dumps(payload), 'turn_finished', 'sibling-token', time.time()))
    await reconcile(runner)
    assert len([e for e in runner.transport.events if e.metadata.get('process_continuation')]) == 1
    assert continuation_status('sibling')['reason'] == 'chain cap reached'
