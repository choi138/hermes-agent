"""Delivery boundaries, late admission, and batching retain Discord recovery coverage."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome
from gateway.platforms.base import SendResult
from tests.gateway.test_discord_busy_ledger_v2e import (
    adapter, busy, finish, inbound, steer,
)
from tests.gateway.test_discord_missed_message_backfill import DiscordAdapter, make_message
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent, _make_runner
from tests.gateway.restart_test_helpers import RestartTestAdapter
from tests.waiting import async_wait_until


def status(busy, message_id):
    return busy.adapter._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT status, replied FROM discord_messages WHERE message_id=?', (str(message_id),),
    ).fetchone())


async def live_batch(adapter, events, monkeypatch):
    blocked = asyncio.Event()
    monkeypatch.setattr(adapter, '_flush_text_batch', lambda *_: blocked.wait())
    captured = []

    async def dispatch(event):
        captured.append(event)

    monkeypatch.setattr(adapter, '_dispatch_text_batch', dispatch)
    for event in events:
        adapter._enqueue_text_event(event)
    await adapter._flush_text_batch_now(adapter._text_batch_key(events[0]))
    tasks = list(adapter._pending_text_batch_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return captured[0]


async def exercise_turns(busy, monkeypatch, tmp_path, moment, path='steer', *,
                         delivered=False, fail_followup=False, restart=False, batched=False):
    loop = asyncio.get_running_loop()
    runner = _make_runner(busy.adapter)
    runner._busy_input_mode = 'interrupt' if path == 'redirect' else 'steer'
    runner._effective_busy_input_mode = lambda _: runner._busy_input_mode
    runner._effective_busy_text_mode = lambda _: 'steer'
    runner._check_slash_access = lambda *_: None
    runner._is_user_authorized_for_source = lambda _: True
    runner._admit_bot_message_for_source = lambda _: True
    late = inbound(busy, text='/steer late-payload' if path == 'command' else 'late-payload')
    batch = [late]
    if batched:
        batch.extend((inbound(busy, 103, 'second-fragment'), inbound(busy, 104, 'third-fragment')))
        late = await live_batch(busy.adapter, batch, monkeypatch)
    following = inbound(busy, 102, 'earlier-pending')
    trace = SimpleNamespace(inputs=[], delivered=[], agents=[], sends=[], before_followup=[],
                            order=[], coverage=[], recoverable=[], batch=batch, batch_before_followup=[],
                            late=late, runner=runner, injected=False)

    async def inject(event=late):
        if event is late:
            trace.injected = True
        if path == 'thread-steer' and event is late:
            agent = runner._session_state(busy.key).turn.agent
            text = runner._steer_text_with_origin(event.text, event)
            accepted = await asyncio.to_thread(runner._try_agent_verb,
                agent, 'steer', event.text, busy.key, event=event)
            if accepted:
                runner._fold_into_running_turn(agent, busy.key, event, steer_text=text)
            else:
                runner._queue_or_replace_pending_event(busy.key, event)
        elif path == 'command' and event is late:
            assert await runner._busy_steer_command(event, busy.key, busy.source)
        elif path == 'priority' and event is late:
            runner._hm_busy_steer(event, runner._session_state(busy.key).turn.agent, busy.key)
        elif path == 'priority-redirect' and event is late:
            await runner._hm_busy_interrupt(event, busy.source,
                runner._session_state(busy.key).turn.agent, busy.key)
        else:
            assert await runner._handle_active_session_busy_message(event, busy.key)

    class Agent:
        _supports_active_turn_redirect = True

        def __init__(self, **kwargs):
            self.tools = []
            self.pending = []
            self.lock = threading.Lock()
            trace.agents.append(self)

        def steer(self, text):
            with self.lock:
                self.pending.append(text)
            return True

        def redirect(self, text):
            return self.steer(text)

        def interrupt(self, text=None):
            return None

        def _drain_pending_steer(self):
            with self.lock:
                text = '\n'.join(self.pending)
                self.pending.clear()
                return text

        def run_conversation(self, message, **kwargs):
            number = len(trace.inputs) + 1
            trace.inputs.append(message)
            trace.order.append(('model', number))
            if number == 1:
                async def ready():
                    await async_wait_until(
                        lambda: runner._session_state(busy.key).turn.agent is self, timeout=3)
                asyncio.run_coroutine_threadsafe(ready(), loop).result(timeout=5)
                if delivered:
                    asyncio.run_coroutine_threadsafe(inject(), loop).result(timeout=5)
                    trace.delivered.append(self._drain_pending_steer())
                if restart:
                    asyncio.run_coroutine_threadsafe(inject(following), loop).result(timeout=5)
                pending = self._drain_pending_steer()
                if moment == 'before-return' and not delivered:
                    asyncio.run_coroutine_threadsafe(inject(), loop).result(timeout=5)
            else:
                trace.before_followup.append(status(busy, 101))
                trace.batch_before_followup.append([status(busy, event.raw_message.id) for event in batch])
                pending = self._drain_pending_steer()
            return {'final_response': f'done-{number}', 'messages': [], 'api_calls': 1,
                    'pending_steer': pending}

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    original_tts = runner._run_agent_finalize_streaming_tts

    async def finalize_tts(ctx, adapter):
        if moment == 'tts' and not trace.injected:
            class Speech:
                done = True
                suppress_whole_file = False

                def finish(self):
                    return None

                async def wait_complete(self, **kwargs):
                    await inject()

            ctx.streaming_tts_consumer_holder[0] = Speech()
        await original_tts(ctx, adapter)

    monkeypatch.setattr(runner, '_run_agent_finalize_streaming_tts', finalize_tts)
    original_cleanup = runner._run_agent_cleanup_turn_tasks

    async def cleanup(ctx, **kwargs):
        if moment == 'cleanup' and not trace.injected:
            await inject()
        await original_cleanup(ctx, **kwargs)

    monkeypatch.setattr(runner, '_run_agent_cleanup_turn_tasks', cleanup)
    original_send = busy.adapter.send

    async def send(chat_id, content, **kwargs):
        if content.startswith('done-'):
            trace.sends.append(content)
            trace.order.append(('reply', int(content.removeprefix('done-'))))
            if content == 'done-1' and moment == 'delivery' and not trace.injected:
                await inject()
            if content != 'done-1' and fail_followup:
                return SendResult(success=False, error='final delivery refused', retryable=False)
        return await original_send(chat_id, content, **kwargs)

    monkeypatch.setattr(busy.adapter, 'send', send)
    original_start = busy.adapter.on_processing_start

    async def start(event):
        if event.raw_message.id == 101:
            trace.recoverable.append(await busy.adapter._should_backfill_discord_message(late.raw_message))
        await original_start(event)

    monkeypatch.setattr(busy.adapter, 'on_processing_start', start)
    original_coverage = getattr(busy.adapter, 'on_processing_coverage_complete', None)

    async def coverage(event, outcome):
        await original_coverage(event, outcome)
        if event.raw_message.id == 100 and not trace.before_followup:
            trace.coverage.append(status(busy, 101))

    if original_coverage is not None:
        monkeypatch.setattr(busy.adapter, 'on_processing_coverage_complete', coverage)
    if moment == 'delivery' or delivered:
        busy.adapter._pending_messages[busy.key] = following

    async def handler(event):
        result = await runner._run_agent(message=event.text, context_prompt='', history=[],
            source=event.source, session_id='v2e2-turn', session_key=busy.key,
            event=event, event_message_id=event.message_id)
        return None if result.get('already_sent') else result['final_response']

    busy.adapter._message_handler = handler
    try:
        await busy.adapter._process_message_background(busy.host, busy.key)
        await async_wait_until(lambda: not busy.adapter._background_tasks, timeout=5)
    finally:
        tasks = list(busy.adapter._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return trace


@pytest.mark.asyncio
@pytest.mark.parametrize('moment', ['before-return', 'tts', 'delivery', 'cleanup'])
async def test_r2_1_late_steer_answered_by_own_turn(busy, monkeypatch, tmp_path, moment):
    trace = await exercise_turns(busy, monkeypatch, tmp_path, moment)
    assert sum('late-payload' in text for text in trace.inputs) == 1
    assert 'late-payload' not in trace.inputs[0]
    assert all(not agent.pending for agent in trace.agents)
    assert trace.before_followup and all(value != ('responded', 1) for value in trace.before_followup)
    assert trace.recoverable and all(trace.recoverable)
    assert trace.order.index(('reply', 1)) < trace.order.index(('model', 2))
    assert all(value != ('responded', 1) for value in trace.coverage)
    assert trace.sends[0] == 'done-1'
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['command', 'redirect', 'priority', 'priority-redirect', 'thread-steer'])
@pytest.mark.parametrize('moment', ['before-return', 'tts'])
async def test_r2_1_late_steer_paths(busy, monkeypatch, tmp_path, path, moment):
    trace = await exercise_turns(busy, monkeypatch, tmp_path, moment, path)
    assert sum('late-payload' in text for text in trace.inputs) == 1
    assert all(not agent.pending for agent in trace.agents)
    assert trace.order.index(('reply', 1)) < trace.order.index(('model', 2))
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
async def test_r2_2_repeated_completion_keeps_absorbed_success(busy, monkeypatch):
    removing = asyncio.Event()
    blocker = asyncio.Event()

    async def remove(*args):
        if not removing.is_set():
            removing.set()
            await blocker.wait()

    monkeypatch.setattr(busy.adapter, '_remove_reaction', remove)
    busy.host.raw_message.add_reaction = AsyncMock()
    event = inbound(busy)

    async def handler(host):
        await steer(busy, event)
        return 'done'

    busy.adapter._message_handler = handler
    task = asyncio.create_task(busy.adapter._process_message_background(busy.host, busy.key))
    try:
        await asyncio.wait_for(removing.wait(), timeout=3)
        assert status(busy, 101) == ('responded', 1)
        busy.adapter._expected_cancelled_tasks.add(task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        blocker.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [ProcessingOutcome.FAILURE, ProcessingOutcome.CANCELLED])
async def test_r2_2_first_terminal_outcome_is_immutable(busy, outcome):
    await busy.adapter.on_processing_start(busy.host)
    await steer(busy, inbound(busy))
    await finish(busy, outcome)
    await finish(busy)
    assert status(busy, 101) == ('failed' if outcome == ProcessingOutcome.FAILURE else 'cancelled', 0)


@pytest.mark.asyncio
async def test_r2_3_absorbed_settled_before_followup(busy, monkeypatch, tmp_path):
    trace = await exercise_turns(busy, monkeypatch, tmp_path, 'delivered',
                                 delivered=True, fail_followup=True)
    assert trace.before_followup == [('responded', 1)]
    assert status(busy, 101) == ('responded', 1)
    assert sum('late-payload' in text for text in trace.delivered) == 1
    assert trace.sends == ['done-1', 'done-2']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['idle-success', 'idle-failure', 'steer', 'debounce'])
async def test_r2_4_batched_messages_all_recorded(busy, monkeypatch, mode):
    events = [inbound(busy, 201 + index, text) for index, text in enumerate(('one', 'two', 'three'))]
    if mode == 'debounce':
        blocked = asyncio.Event()
        monkeypatch.setattr(busy.adapter, '_flush_text_debounce', lambda *_: blocked.wait())
        for event in events:
            await busy.adapter._queue_text_debounce(busy.key, event)
        timer = busy.adapter._text_debounce_store()[busy.key].task
        await busy.adapter._flush_text_debounce_now(busy.key)
        await asyncio.gather(timer, return_exceptions=True)
        merged = busy.adapter.get_pending_message(busy.key)
    else:
        merged = await live_batch(busy.adapter, events, monkeypatch)
    if mode == 'steer':
        await busy.adapter.on_processing_start(busy.host)
        await steer(busy, merged)
        await finish(busy)
    else:
        async def handler(event):
            assert all(status(busy, message_id) == ('processing', 0) for message_id in (201, 202, 203))
            if mode == 'idle-failure':
                raise RuntimeError('test turn failed')
            return 'done'

        busy.adapter._message_handler = handler
        await busy.adapter._process_message_background(merged, busy.key)
    expected = ('failed', 0) if mode == 'idle-failure' else ('responded', 1)
    assert [status(busy, message_id) for message_id in (201, 202, 203)] == [expected] * 3
    if mode == 'idle-failure':
        for event in events:
            assert await busy.adapter._should_backfill_discord_message(event.raw_message)


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [False, True])
async def test_r2_4_batch_leftover_and_early_settlement(busy, monkeypatch, tmp_path, delivered):
    trace = await exercise_turns(busy, monkeypatch, tmp_path, 'before-return',
        delivered=delivered, batched=True, fail_followup=delivered)
    expected = ('responded', 1)
    assert [status(busy, event.raw_message.id) for event in trace.batch] == [expected] * 3
    if delivered:
        assert trace.batch_before_followup == [[expected] * 3]
    else:
        assert trace.batch_before_followup == [[('processing', 0)] * 3]
    for fragment in ('late-payload', 'second-fragment', 'third-fragment'):
        assert sum(fragment in text for text in trace.inputs + trace.delivered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['late', 'batch'])
async def test_r2_5_restart_contract(busy, monkeypatch, tmp_path, scenario):
    if scenario == 'late':
        trace = await exercise_turns(busy, monkeypatch, tmp_path, 'before-return', restart=True)
        events = [busy.host, trace.late]
    else:
        events = [inbound(busy, 201 + index, text) for index, text in enumerate(('one', 'two', 'three'))]
        merged = await live_batch(busy.adapter, events, monkeypatch)
        busy.adapter._message_handler = AsyncMock(return_value='done')
        await busy.adapter._process_message_background(merged, busy.key)
    reply = make_message(message_id=900, content='done', channel=busy.channel)
    reply.author = busy.adapter._client.user
    reply.reference = SimpleNamespace(message_id=int(events[0].raw_message.id))
    busy.channel._history_messages = [event.raw_message for event in events] + [reply]
    fresh = DiscordAdapter(PlatformConfig(enabled=True, token='fake-token'))
    fresh._client = SimpleNamespace(user=busy.adapter._client.user, get_channel=lambda _: busy.channel)
    fresh._ready_event.set()
    fresh._handle_message = AsyncMock(return_value=True)
    monkeypatch.setenv('DISCORD_REQUIRE_MENTION', 'false')
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS', '123')
    await fresh._run_missed_message_backfill()
    fresh._handle_message.assert_not_awaited()
    scan = fresh._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT missed, dispatched FROM discord_recovery_scans ORDER BY started_at DESC LIMIT 1',
    ).fetchone())
    assert scan == (0, 0)
    assert all(status(busy, event.raw_message.id) == ('responded', 1) for event in events)


@pytest.mark.asyncio
async def test_d_delivered_steer_still_completes_with_host(busy):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    await finish(busy)
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
async def test_d_finished_model_request_redirect_cannot_fold(busy):
    from agent.interrupt_control import InterruptControlMixin

    agent = InterruptControlMixin()
    agent._model_request_active = threading.Event()
    agent._pending_redirect_lock = threading.Lock()
    agent._pending_redirect = None
    busy.runner._session_state(busy.key).turn.agent = agent
    event = inbound(busy)
    assert busy.runner._redirect_active_turn(agent, event.text, busy.key, event) is False
    assert busy.host.absorbed_events == []
    assert agent._pending_redirect is None


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [ProcessingOutcome.FAILURE, ProcessingOutcome.CANCELLED])
async def test_d_premature_ack_failure_still_recoverable(busy, outcome):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    await busy.adapter._run_processing_hook('on_processing_covered', event, ProcessingOutcome.SUCCESS)
    await finish(busy, outcome)
    assert status(busy, 101) == ('failed' if outcome == ProcessingOutcome.FAILURE else 'cancelled', 0)
    assert await busy.adapter._should_backfill_discord_message(event.raw_message)


@pytest.mark.asyncio
async def test_d_replay_outcome_recorded_after_cancelled_claim(busy):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    text = busy.runner._session_state(busy.key).turn.process_steer_events[0][0]
    replay, _ = await busy.runner._run_agent_drain_pending(
        {'pending_steer': text}, busy.adapter, busy.source, busy.key)
    assert status(busy, 101) == ('cancelled', 0)
    await busy.adapter.on_processing_start(replay)
    await busy.adapter.on_processing_complete(replay, ProcessingOutcome.SUCCESS)
    await finish(busy, ProcessingOutcome.FAILURE)
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
async def test_d_new_lifecycle_can_complete_previously_failed_coverage(busy):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    await finish(busy, ProcessingOutcome.FAILURE)
    assert status(busy, 101) == ('failed', 0)
    await busy.adapter.on_processing_start(busy.host)
    await finish(busy)
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('debounce', [False, True])
async def test_d_batch_anchor_text_reactions_unchanged(busy, monkeypatch, debounce):
    first, second = inbound(busy, 201, 'first'), inbound(busy, 202, 'second')
    for event in (first, second):
        event.raw_message.add_reaction = AsyncMock()
        event.raw_message.remove_reaction = AsyncMock()
    delay = busy.adapter._text_batch_delay_seconds
    if debounce:
        blocked = asyncio.Event()
        monkeypatch.setattr(busy.adapter, '_flush_text_debounce', lambda *_: blocked.wait())
        await busy.adapter._queue_text_debounce(busy.key, first)
        await busy.adapter._queue_text_debounce(busy.key, second)
        timer = busy.adapter._text_debounce_store()[busy.key].task
        await busy.adapter._flush_text_debounce_now(busy.key)
        await asyncio.gather(timer, return_exceptions=True)
        merged = busy.adapter.get_pending_message(busy.key)
    else:
        merged = await live_batch(busy.adapter, [first, second], monkeypatch)
    assert merged is first and merged.raw_message.id == 201
    assert merged.message_id == ('202' if debounce else '201')
    assert merged.reply_to_message_id == ('202' if debounce else None)
    assert merged.text == 'first\nsecond'
    assert busy.adapter._text_batch_delay_seconds == delay == 0.6
    await busy.adapter.on_processing_start(merged)
    await busy.adapter.on_processing_complete(merged, ProcessingOutcome.SUCCESS)
    assert [call.args[0] for call in first.raw_message.add_reaction.await_args_list] == ['👀', '✅']
    second.raw_message.add_reaction.assert_not_awaited()
    second.raw_message.remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_d_other_platform_batching_unchanged(busy, monkeypatch):
    other = RestartTestAdapter()
    events = [inbound(busy, 201, 'first'), inbound(busy, 202, 'second')]
    merged = await live_batch(other, events, monkeypatch)
    await other.on_processing_start(merged)
    await other.on_processing_complete(merged, ProcessingOutcome.SUCCESS)
    assert merged.text == 'first\nsecond' and merged.message_id == '201'
    assert other.sent == []
    assert not busy.adapter._discord_recovery_store.path().exists()
