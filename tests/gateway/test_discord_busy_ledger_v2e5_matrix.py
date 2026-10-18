"""Uncancelled delivery contracts, runnable unchanged against round three."""

import asyncio
import contextvars
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.process_followups import deliver_foreground_report
from gateway.run_turn_runner import TurnRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from gateway.turn_context import TurnContext
from tests.gateway.test_discord_busy_ledger_v2e import adapter, busy, inbound, steer
from tests.gateway.test_discord_busy_ledger_v2e2 import status
from tests.gateway.test_discord_busy_ledger_v2e3 import restart_scan
from tests.gateway.test_discord_missed_message_backfill import discord
from tools import process_registry_followups
from tools.process_registry import ProcessSession


CELLS = (
    'n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'n7', 'n8', 'n9a', 'n9b', 'n10', 'n11', 'n12',
    'q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7a', 'q7b', 'q8', 'q9', 'q10a', 'q10b', 'q11',
    's1', 's2', 's3', 's4a', 's4b', 's5', 's6', 's7', 's8',
    'i1', 'i2', 'i3', 'p1', 'p2',
)
FAILED = {'n3', 'n4', 'n5', 'n7', 'n8', 'n9b', 'n12', 'q4', 'q8', 'q10a', 'q10b', 'q11', 's3', 'i2'}


def expected_states(cell):
    if cell.startswith('p'):
        return [('processing', 0)] * 2
    if cell == 'i3':
        return [('cancelled', 0), ('responded', 1)]
    if cell in FAILED:
        return [('failed', 0)] * 2
    return [('responded', 1)] * 2


class MatrixTransport:
    def __init__(self, cell):
        self.cell = cell
        self.calls = []
        self.accepted = []
        self.attempts = 0
        self.before_send = None
        self.after_accept = None
        self.media_raises = cell in {'q6', 'q7b'}
        self.redelivery = False

    async def send(self, *, content=None, file=None, files=None, reference=None, **kwargs):
        if self.before_send is not None:
            await self.before_send(content)
        upload = file is not None or bool(files)
        self.calls.append(('send', content, upload, reference))
        if upload:
            if self.media_raises:
                raise RuntimeError('media upload refused')
        elif self.redelivery:
            if self.cell == 'q10a' and 'other-session' not in content:
                raise ConnectionError('replacement refused host')
        elif content and 'could not' in content.lower():
            if self.cell == 'n4':
                raise ConnectionError('notice refused')
        else:
            self.attempts += 1
            if self.cell in {'n3', 'n4', 'n9b', 'q4', 's3', 'i2', 'p2', 'q10a', 'q10b'}:
                if self.cell != 'n9b' or content.startswith('tail'):
                    raise ConnectionError('final transport unavailable')
            if self.cell in {'n5', 'q11'}:
                raise RuntimeError('rate limit retry_after=120')
            if self.cell in {'n6', 'q5'} and self.attempts == 1:
                raise RuntimeError('invalid formatting')
            if self.cell in {'n7', 'n12'}:
                raise RuntimeError('invalid formatting')
            if self.cell == 'n8':
                raise RuntimeError('ReadTimeout: timed out')
            if self.cell == 'n2' and self.attempts == 1:
                raise ConnectionError('transient connection reset')
            if self.cell == 'n9a' and content.startswith('tail') and self.attempts == 2:
                raise ConnectionError('chunk two reset')
            if self.cell == 'n10' and reference is not None:
                raise RuntimeError('error code: 10008')
            if self.cell in {'q9', 's5'}:
                raise ConnectionError('overflow continuation refused')
            if self.cell == 's4b' and self.attempts == 1:
                raise RuntimeError('fresh final refused')
        self.accepted.append(('upload' if upload else 'send', content))
        if self.after_accept is not None:
            await self.after_accept('send')
        return SimpleNamespace(id=900 + len(self.accepted), content=content,
                               attachments=[SimpleNamespace(id=1)] if upload else [])

    async def edit(self, *, content, **kwargs):
        self.calls.append(('edit', content))
        if self.cell in {'q3', 'q4', 's2', 's3'}:
            raise RuntimeError('edit unavailable')
        if self.cell == 's7':
            raise RuntimeError('flood_control:120')
        self.accepted.append(('edit', content))
        if self.after_accept is not None:
            await self.after_accept('edit')

    async def forum(self, *, content, **kwargs):
        self.calls.append(('forum', content))
        self.accepted.append(('forum', content))
        return SimpleNamespace(thread=SimpleNamespace(id=700), message=SimpleNamespace(id=900))


def matrix_context(tmp_path):
    resources = ExitStack()
    patch = resources.enter_context(pytest.MonkeyPatch.context())
    executor = resources.enter_context(ThreadPoolExecutor(max_workers=1))
    original_sleep = asyncio.sleep
    original_connect = sqlite3.connect
    connections = {}

    class ScenarioConnection(sqlite3.Connection):
        def close(self):
            self.execute('PRAGMA query_only=OFF')

    def close_connections():
        for connection in connections.values():
            sqlite3.Connection.close(connection)
        connections.clear()

    resources.callback(close_connections)

    def connect(database, *args, **kwargs):
        key = str(database)
        if key not in connections:
            kwargs.update(factory=ScenarioConnection, check_same_thread=False)
            connection = original_connect(database, *args, **kwargs)
            connection.execute('PRAGMA synchronous=OFF')
            connections[key] = connection
        return connections[key]

    async def sleep(delay, result=None):
        await original_sleep(0)
        return result

    async def to_thread(function, *args, **kwargs):
        await original_sleep(0)
        context = contextvars.copy_context()
        return executor.submit(context.run, function, *args, **kwargs).result(timeout=5)

    patch.setattr(sqlite3, 'connect', connect)
    patch.setattr('hermes_state_wal._enforce_macos_synchronous_full',
                  lambda connection: connection.execute('PRAGMA synchronous=OFF'))
    patch.setattr(asyncio, 'sleep', sleep)
    patch.setattr(asyncio, 'to_thread', to_thread)
    patch.setattr(delivery_ledger, 'ledger_enabled', lambda config=None: True)
    current_adapter = adapter.__wrapped__(patch, tmp_path)
    current = busy.__wrapped__(current_adapter, patch)
    return resources, patch, current, close_connections


async def drain(adapter):
    for _ in range(20):
        pending = [task for task in adapter._background_tasks if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending)
    raise AssertionError('coverage settlement failed to drain')


async def matrix_scenario(tmp_path, cell, observe=None):
    with ExitStack() as cleanup:
        resources, patch, current, close_connections = matrix_context(tmp_path)
        cleanup.enter_context(resources)
        transport = MatrixTransport(cell)
        patch.setattr(current.channel, 'send', transport.send)
        patch.setattr(current.channel, 'get_partial_message',
                      lambda _: SimpleNamespace(id=800, edit=transport.edit), raising=False)
        patch.setattr(current.adapter, 'send_typing', AsyncMock())
        patch.setattr(current.adapter, 'stop_typing', AsyncMock())
        patch.setattr(current.adapter, 'delete_message', AsyncMock())
        covered = [inbound(current, 101, 'argument'), inbound(current, 102, ' ' if cell == 'i3' else 'consumed')]
        for event in covered:
            if cell == 'i3' and event.message_id == '102':
                current.host.absorbed_events.append(event)
                await current.adapter.on_processing_covered(event)
            else:
                await steer(current, event)
        await drain(current.adapter)
        media = tmp_path / 'answer.txt'
        media.write_text('attachment', encoding='utf-8')
        text = 'done'
        if cell in {'n9a', 'n9b', 'q9', 's5'}:
            text = 'head ' + 'x' * 1980 + '\ntail ' + 'y' * 500
        if cell in {'q6', 'q7a', 'q7b'}:
            text = ('' if cell.startswith('q7') else text + '\n') + 'MEDIA:' + str(media)
        if cell == 'n12':
            text = ''
        if cell == 'n11':
            forum = discord.ForumChannel.__new__(discord.ForumChannel)
            patch.setattr(type(forum), 'id', 123, raising=False)
            patch.setattr(type(forum), 'create_thread', transport.forum, raising=False)
            current.adapter._client.get_channel = lambda _: forum
        ctx = TurnContext(source=current.source, event=current.host, session_key=current.key,
                          event_message_id='100', inbound_message_id='100')
        result = {'final_response': text, 'messages': [], 'failed': cell == 'q8'}
        timers = []
        if observe is not None:
            observe(patch, current, transport, text)

        async def handler(event):
            if cell.startswith('q'):
                if cell in {'q2', 'q3', 'q4', 'q9'}:
                    ctx.stream_consumer_holder[0] = SimpleNamespace(message_id='800', final_response_sent=False)
                if cell == 'q1':
                    consumer = GatewayStreamConsumer(current.adapter, '123', StreamConsumerConfig(buffer_only=True, cursor=''))
                    consumer.on_delta(text)
                    worker = object.__new__(TurnRunner)
                    worker._ctx, worker._runner = ctx, current.runner
                    ctx.stream_consumer_holder[0] = consumer
                    worker._finish_stream_consumer(result, [], consumer)
                    await consumer.run()
                await current.runner._run_agent_deliver_first_response(ctx, current.adapter, result, result, None)
                return None
            if cell.startswith('s'):
                consumer = GatewayStreamConsumer(current.adapter, '123', StreamConsumerConfig(buffer_only=True, cursor=''),
                                                 initial_reply_to_id='100')
                consumer._message_id = '800'
                consumer._last_sent_text = 'preview'
                consumer._already_sent = True
                ctx.stream_consumer_holder[0] = consumer
                worker = object.__new__(TurnRunner)
                worker._ctx, worker._runner = ctx, current.runner
                if cell == 's6':
                    consumer._accumulated = 'partial preview'
                    await consumer._on_cancelled()
                    return text
                consumer.on_delta(text)
                worker._finish_stream_consumer(result, [], consumer)
                if cell in {'s4a', 's4b'}:
                    patch.setattr(current.adapter, 'prefers_fresh_final_streaming', lambda *args, **kwargs: True,
                                  raising=False)
                if cell == 's5':
                    patch.setattr(consumer, '_resolve_length_budget', lambda: (len, 4000))
                if cell == 's8':
                    result['response_transformed'] = True
                    await current.runner._run_agent_mark_streamed_delivery(result, ctx)
                else:
                    await consumer.run()
                    await current.runner._run_agent_mark_streamed_delivery(result, ctx)
                return None if result.get('already_sent') else text
            return text

        if cell in {'q10a', 'q10b', 'q11'}:
            current.adapter.gateway_runner = current.runner
            current.runner.adapters = {current.source.platform: current.adapter}
            current.runner._running = cell == 'q11'
            original_finalize = current.adapter._finalize_delivery_obligation
            replacement = adapter.__wrapped__(patch, tmp_path)
            replacement._client = current.adapter._client

            async def finalize(obligation_id, result, event, delivery_adapter):
                if cell != 'q11':
                    current.runner._delivery_adapter_for = lambda _: replacement
                    delivery_ledger.record_obligation(
                        obligation_id='other-session', session_key='other-session', platform='discord',
                        chat_id='123', thread_id=None, content='other-session reply')
                    delivery_ledger.mark_failed('other-session', 'send_path_degraded')
                    transport.redelivery = True
                await original_finalize(obligation_id, result, event, delivery_adapter)

            async def redeliver(platform, *, profile=None):
                transport.redelivery = True
                claimed = delivery_ledger.sweep_failed_for_runtime('discord', now=time.time() + 1000, profile=profile)
                await current.runner._redeliver_claimed_obligations(claimed)
                current.runner._running = False

            async def obligation_adapter(row):
                return replacement

            original_schedule = current.runner._schedule_flood_redelivery

            def schedule(platform, *, profile=None):
                original_schedule(platform, profile=profile)
                timers.extend(current.runner._flood_redelivery_tasks.values())

            patch.setattr(current.adapter, '_finalize_delivery_obligation', finalize)
            patch.setattr(current.runner, '_redeliver_failed_obligations_for_platform', redeliver)
            patch.setattr(current.runner, '_obligation_adapter', obligation_adapter)
            patch.setattr(current.runner, '_schedule_flood_redelivery', schedule)
            patch.setattr(current.runner, '_arm_flood_timers_for_waiting_rows', AsyncMock())
            if cell == 'q11':
                patch.setattr(delivery_ledger, 'flood_retry_delay', lambda delay: 0)
            if observe is not None:
                observe(patch, current, transport, text, replacement=replacement)

        if cell.startswith('p'):
            session = ProcessSession(id='matrix-report', command='true', session_key=current.key)
            process_registry_followups.reserve(session)
            assert process_registry_followups.claim_foreground_report(
                session.id, current.key, 'report-turn', {'outcome': 'verified'}, text)
            current.runner._build_process_event_source = lambda _: current.source
            await deliver_foreground_report(current.runner, {
                'session_key': current.key, 'followup_turn_id': 'report-turn', 'report_text': text})
        elif cell.startswith('i'):
            if cell == 'i3':
                current.host.text = '/help'
            current.adapter._message_handler = AsyncMock(return_value=text)
            await current.adapter._dispatch_inline_reply(current.host)
        elif cell == 'n12':
            await current.adapter.on_processing_start(current.host)
            sent, _ = await current.adapter.send_final_ledgered(current.host, current.key, '', {}, reply_to='100')
            assert not sent.success
            await current.adapter.on_processing_complete(current.host, ProcessingOutcome.FAILURE)
        else:
            current.adapter._message_handler = handler
            await current.adapter._process_message_background(current.host, current.key)
        if timers:
            await asyncio.gather(*timers)
        if cell in {'q10a', 'q10b', 'q11'}:
            assert transport.redelivery
            assert any('Recovered reply' in (content or '') for _, content in transport.accepted)
        if cell == 'q11':
            assert timers and all(task.done() for task in timers)
        await drain(current.adapter)
        states = [status(current, event.message_id) for event in covered]
        current.adapter._with_discord_recovery_db(lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at='2000-01-01T00:00:00+00:00' WHERE status='processing'"))
        close_connections()
        fresh, scan = await restart_scan(current, patch, [current.host, *covered], expect_empty=False)
        replay = [call.args[0].id for call in fresh._handle_message.await_args_list]
        return SimpleNamespace(states=states, replay=replay, scan=scan, transport=transport, timers=timers)


@pytest.mark.asyncio
@pytest.mark.parametrize('cell', CELLS)
async def test_r5_2_failure_matrix_matches_round3(tmp_path, cell):
    run = await matrix_scenario(tmp_path, cell)
    if cell in {'q6', 'q7a', 'q7b'}:
        assert any(call[0] == 'send' and call[2] for call in run.transport.calls)
        assert any(kind == 'upload' for kind, _ in run.transport.accepted) == (cell == 'q7a')
    assert run.states == expected_states(cell), (cell, run.states, run.transport.calls)
    recoverable = [101 + index for index, state in enumerate(run.states) if state[0] != 'responded']
    assert set(recoverable) <= set(run.replay), (cell, run.states, run.replay)
    assert not {101 + index for index, state in enumerate(run.states) if state[0] == 'responded'} & set(run.replay)
    print('MATRIX ' + json.dumps({'cell': cell, 'states': run.states, 'replay': run.replay}), flush=True)
