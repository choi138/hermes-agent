"""Sweep real Discord delivery lanes at every deterministic lifecycle iteration.

Temporary on-disk SQLite connections are reused only within a serial scenario,
with durability barriers disabled; all connections close before fresh backfill.
"""

import asyncio
import contextvars
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import TurnContext
from tests.gateway.test_discord_busy_ledger_v2e import adapter, busy, inbound, steer
from tests.gateway.test_discord_busy_ledger_v2e2 import status
from tests.gateway.test_discord_busy_ledger_v2e3 import restart_scan


LANES = ('normal', 'normal-media', 'queued-send', 'queued-edit', 'queued-streamed', 'streamed')


class DeliveryClock:
    def __init__(self, executor, fault=None):
        self.executor = executor
        self.enabled = False
        self.accepted = False
        self.labels = {}
        self.visible = []
        self.fault = fault
        self.faults = []
        self.iteration = 0
        self.trace = []
        self.shutting_down = False

    async def pause(self, label):
        if not self.enabled:
            return
        task = asyncio.current_task()
        self.labels[task] = label
        try:
            await asyncio.sleep(0)
            if self.accepted and label == self.fault:
                self.faults.append(label)
                raise RuntimeError('post-commit executor/transport failure: ' + label)
        finally:
            self.labels.pop(task, None)

    async def to_thread(self, function, *args, **kwargs):
        await self.pause('worker.' + function.__name__)
        context = contextvars.copy_context()
        return self.executor.submit(context.run, function, *args, **kwargs).result(timeout=3)

    async def send(self, *, content, **kwargs):
        await self.pause('transport.send')
        if self.shutting_down and not self.accepted:
            raise asyncio.CancelledError
        self.accepted = True
        self.visible.append(('send', content))
        return SimpleNamespace(id=900, content=content)

    async def edit(self, *, content, **kwargs):
        await self.pause('transport.edit')
        if self.shutting_down and not self.accepted:
            raise asyncio.CancelledError
        self.accepted = True
        self.visible.append(('edit', content))

    async def upload(self, **kwargs):
        await self.pause('transport.upload')
        self.visible.append(('upload', Path(kwargs['file_path']).name))
        return SendResult(success=True, message_id='901')


@pytest.fixture(scope='module')
def sweep_cache():
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield SimpleNamespace(executor=executor, lanes={})


async def delivery_scenario(tmp_path, executor, lane, *, cancel_at=None, fault=None,
                            failed=False, cancel_label=None, pending_steer=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as patch, ExitStack() as resources:
        original_sleep = asyncio.sleep

        async def deterministic_sleep(delay, result=None):
            await original_sleep(0)
            return result

        patch.setattr(asyncio, 'sleep', deterministic_sleep)
        connect = sqlite3.connect
        connections = {}

        class ScenarioConnection(sqlite3.Connection):
            def close(self):
                return None

        def close_connections():
            for connection in connections.values():
                sqlite3.Connection.close(connection)
            connections.clear()

        resources.callback(close_connections)

        def fast_connect(*args, **kwargs):
            database = str(args[0])
            if database not in connections:
                kwargs.update(factory=ScenarioConnection, check_same_thread=False)
                connection = connect(*args, **kwargs)
                connection.execute('PRAGMA synchronous=OFF')
                connections[database] = connection
            return connections[database]

        patch.setattr(sqlite3, 'connect', fast_connect)
        patch.setattr('hermes_state_wal._enforce_macos_synchronous_full',
                      lambda connection: connection.execute('PRAGMA synchronous=OFF'))
        current_adapter = adapter.__wrapped__(patch, tmp_path)
        current = busy.__wrapped__(current_adapter, patch)
        clock = DeliveryClock(executor, fault)
        patch.setattr(asyncio, 'to_thread', clock.to_thread)
        patch.setattr(current.channel, 'send', clock.send)
        partial = SimpleNamespace(id=800, edit=clock.edit)
        patch.setattr(current.channel, 'get_partial_message', lambda _: partial, raising=False)
        patch.setattr(current_adapter, 'send_document', clock.upload)
        patch.setattr(current_adapter, 'send_typing', AsyncMock())
        patch.setattr(current_adapter, 'stop_typing', AsyncMock())
        original_marker = current_adapter._release_turn_marker

        async def marker(event):
            await clock.pause('marker.release')
            await original_marker(event)

        async def reaction(*args, **kwargs):
            await clock.pause('reaction')

        patch.setattr(current_adapter, '_release_turn_marker', marker)
        for event in [current.host]:
            event.raw_message.add_reaction = reaction
            event.raw_message.remove_reaction = reaction
        covered = [inbound(current, 101, 'covered-one'), inbound(current, 102, 'covered-two')]
        for event in covered:
            await steer(current, event)
        claims = list(current_adapter._background_tasks)
        if claims:
            await asyncio.gather(*claims)
        media = tmp_path / 'answer.txt'
        media.write_text('attachment', encoding='utf-8')
        has_media = lane == 'normal-media' or lane.startswith('queued-')
        response_text = 'done' + ('\nMEDIA:' + str(media) if has_media else '')
        result = {'final_response': response_text, 'messages': [], 'failed': failed}
        if pending_steer:
            result['pending_steer'] = covered[-1].text
        current.agent.run_conversation = lambda *_args, **_kwargs: result
        runner = current.runner
        ctx = TurnContext(source=current.source, event=current.host, session_key=current.key,
                          event_message_id='100', inbound_message_id='100')
        stream_tasks = []

        async def handler(event):
            response = current.agent.run_conversation(event.text)
            stream_task = None
            if lane in {'streamed', 'queued-streamed'}:
                consumer = GatewayStreamConsumer(
                    current_adapter, '123', StreamConsumerConfig(buffer_only=True, cursor=''),
                    initial_reply_to_id='100')
                ctx.stream_consumer_holder[0] = consumer
                worker = object.__new__(TurnRunner)
                worker._ctx, worker._runner = ctx, runner
                consumer.on_delta('done')
                worker._finish_stream_consumer(response, [], consumer)

                async def consume():
                    await consumer.run()
                    if clock.accepted:
                        await clock.pause('stream.flush')

                stream_task = asyncio.create_task(consume())
                stream_tasks.append(stream_task)
            elif lane == 'queued-edit':
                ctx.stream_consumer_holder[0] = SimpleNamespace(
                    message_id='800', final_response_sent=False)
            if lane.startswith('queued-'):
                await runner._run_agent_deliver_first_response(
                    ctx, current_adapter, response, result, stream_task)
                return None
            if stream_task is not None:
                await runner._await_stream_task(stream_task)
                await clock.pause('stream.postwait')
                await runner._run_agent_mark_streamed_delivery(response, ctx)
                return None if response.get('already_sent') else response_text
            return response_text

        current_adapter._message_handler = handler
        clock.enabled = True
        lifecycle = asyncio.create_task(current_adapter._process_message_background(current.host, current.key))
        current_adapter._background_tasks.add(lifecycle)
        lifecycle.add_done_callback(current_adapter._background_tasks.discard)
        cancelled_when_accepted = None
        cancellation_labels = ()
        try:
            for iteration in range(500):
                clock.iteration = iteration
                labels = tuple(sorted(set(clock.labels.values())))
                clock.trace.append((iteration, clock.accepted, labels))
                if lifecycle.done():
                    break
                if iteration == cancel_at or (cancel_label in labels if cancel_label else False):
                    cancelled_when_accepted = clock.accepted
                    cancellation_labels = labels
                    clock.shutting_down = True
                    await current_adapter.cancel_background_tasks()
                    break
                await asyncio.sleep(0)
            else:
                raise AssertionError('lifecycle did not finish within 500 deterministic iterations')
            await asyncio.gather(lifecycle, return_exceptions=True)
            for task in stream_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
            await current_adapter.cancel_background_tasks()
            clock.enabled = False
            states = [status(current, event.message_id) for event in covered]
            own = status(current, '100')
            current_adapter._with_discord_recovery_db(lambda conn: conn.execute(
                "UPDATE discord_messages SET updated_at='2000-01-01T00:00:00+00:00' "
                "WHERE status='processing'"))
            close_connections()
            fresh, scan = await restart_scan(current, patch, [current.host, *covered], expect_empty=False)
            dispatched = [call.args[0].id for call in fresh._handle_message.await_args_list]
            return SimpleNamespace(
                accepted=clock.accepted if cancelled_when_accepted is None else cancelled_when_accepted,
                cancelled=lifecycle.cancelled(), states=states, own=own, visible=clock.visible,
                cancel_requested=cancelled_when_accepted is not None,
                dispatched=dispatched, scan=scan, iteration=clock.iteration,
                labels=cancellation_labels, trace=clock.trace, faults=clock.faults)
        finally:
            clock.enabled = False
            if not lifecycle.done():
                lifecycle.cancel()
            await asyncio.gather(lifecycle, return_exceptions=True)
            for task in stream_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)
            await current_adapter.cancel_background_tasks()


async def lane_sweep(tmp_path, sweep_cache, lane):
    if lane not in sweep_cache.lanes:
        runs = []
        for iteration in range(500):
            run = await delivery_scenario(tmp_path / str(iteration), sweep_cache.executor, lane,
                                          cancel_at=iteration)
            runs.append(run)
            if not run.cancel_requested:
                break
        else:
            raise AssertionError('sweep never reached the uncancelled lifecycle end')
        sweep_cache.lanes[lane] = runs
        print('SWEEP ' + json.dumps({
            'lane': lane, 'end': len(runs) - 1,
            'accepted': {str(index): run.labels for index, run in enumerate(runs) if run.accepted},
            'defects': {str(index): run.labels for index, run in enumerate(runs)
                        if run.accepted and run.states != [('responded', 1)] * 2}}), flush=True)
    return sweep_cache.lanes[lane]


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', LANES)
async def test_r4_1_cancel_sweep_after_commit_keeps_answered(tmp_path, sweep_cache, lane):
    runs = await lane_sweep(tmp_path, sweep_cache, lane)
    defects = [(index, run.labels, run.states, run.dispatched) for index, run in enumerate(runs)
               if run.accepted and (run.states != [('responded', 1)] * 2 or run.dispatched)]
    assert not defects, defects


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', LANES)
async def test_d4_cancel_before_commit_recoverable(tmp_path, sweep_cache, lane):
    runs = await lane_sweep(tmp_path, sweep_cache, lane)
    for run in runs:
        if not run.accepted:
            assert all(state is None or state[1] == 0 for state in run.states)
            assert sorted(identifier for identifier in run.dispatched if identifier != 100) == [101, 102]


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', LANES)
async def test_d4_sweep_reaches_end(tmp_path, sweep_cache, lane):
    runs = await lane_sweep(tmp_path, sweep_cache, lane)
    assert any(run.accepted and run.cancel_requested for run in runs)
    assert not runs[-1].cancel_requested
    assert runs[-1].accepted


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', LANES)
async def test_r4_2_post_commit_exception_keeps_answered(tmp_path, sweep_cache, lane):
    baseline = await delivery_scenario(tmp_path / 'baseline', sweep_cache.executor, lane)
    labels = sorted({label for _, accepted, active in baseline.trace if accepted for label in active
                     if label not in {'worker._record_discord_covered_complete',
                                      'worker._record_discord_processing_start'}})
    defects = []
    for index, label in enumerate(labels):
        run = await delivery_scenario(tmp_path / str(index), sweep_cache.executor, lane, fault=label)
        if run.faults and (run.states != [('responded', 1)] * 2 or run.dispatched):
            defects.append((label, run.states, run.dispatched))
    assert labels
    assert not defects, defects


@pytest.mark.asyncio
async def test_r4_3_restart_contract(tmp_path, sweep_cache):
    for lane, label in [('queued-send', 'transport.upload'),
                        ('normal', 'worker._record_discord_response'),
                        ('streamed', 'stream.postwait')]:
        run = await delivery_scenario(tmp_path / lane, sweep_cache.executor, lane, cancel_label=label)
        assert run.accepted and run.cancelled, (lane, run.trace)
        assert run.states == [('responded', 1)] * 2, (lane, run.states)
        assert not run.dispatched, (lane, run.dispatched)
        assert run.scan == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', LANES)
async def test_d4_uncancelled_outcomes_match_head(tmp_path, sweep_cache, lane):
    for failed in (False, True):
        run = await delivery_scenario(tmp_path / str(failed), sweep_cache.executor, lane, failed=failed)
        expected_coverage = ('failed', 0) if failed and lane.startswith('queued-') else ('responded', 1)
        assert run.states == [expected_coverage] * 2, (lane, failed, run.states)
        assert run.own == ('responded', 1), (lane, failed, run.own)
        expected_text = [('edit' if lane == 'queued-edit' else 'send', 'done')]
        expected_upload = [('upload', 'answer.txt')] if (
            lane == 'normal-media' or lane.startswith('queued-') and not failed) else []
        if lane == 'streamed' and failed:
            expected_text *= 2
        assert run.visible == expected_text + expected_upload, (lane, failed, run.visible)


@pytest.mark.asyncio
async def test_d4_cancelled_task_still_cancelled(tmp_path, sweep_cache):
    for lane in LANES:
        label = {'streamed': 'stream.postwait', 'queued-streamed': 'transport.upload'}.get(
            lane, 'worker._record_discord_response')
        run = await delivery_scenario(tmp_path / lane, sweep_cache.executor, lane,
                                      cancel_label=label)
        assert run.accepted and run.cancelled, (lane, run.trace)


@pytest.mark.asyncio
async def test_r4_4_stream_commit_excludes_undrained_steer(tmp_path, sweep_cache):
    run = await delivery_scenario(tmp_path, sweep_cache.executor, 'streamed',
                                  pending_steer=True, cancel_label='stream.postwait')
    assert run.accepted and run.cancelled
    assert run.states[0] == ('responded', 1)
    assert run.states[1][1] == 0
    assert run.dispatched == [102]
