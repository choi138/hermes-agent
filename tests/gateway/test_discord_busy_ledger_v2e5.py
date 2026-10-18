"""Final-payload ownership, revocable inherited contexts and inline cancellation."""

import asyncio
import contextvars
import inspect
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.platforms import base
from agent.i18n import t
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from gateway.turn_context import TurnContext
from tests.gateway.test_discord_busy_ledger_v2e import inbound, steer
from tests.gateway.test_discord_busy_ledger_v2e2 import status
from tests.gateway.test_discord_busy_ledger_v2e3 import restart_scan
from tests.gateway.test_discord_busy_ledger_v2e4 import DeliveryClock
from tests.gateway.test_discord_busy_ledger_v2e5_matrix import (
    CELLS, MatrixTransport, drain, expected_states, matrix_context, matrix_scenario,
)


def armed():
    binding = base._coverage_delivery.get()
    return binding is not None and getattr(binding, 'active', True)


class PayloadRecorder:
    ALLOWED_CALLLESS = {
        ('gateway.run_turn', '_run_agent_deliver_first_response'): {18, 51},
        ('gateway.run_turn', '_run_agent_mark_streamed_delivery'): {45},
        ('gateway.run_notifications', '_deliver_queued_first_response'): {24},
        ('gateway.stream_consumer', '_mark_final_delivered'): {14},
        ('gateway.stream_consumer_fallback', '_fallback_when_nothing_unseen'): {35, 38},
        ('gateway.platforms.base', '_record_delivery'): {8},
    }

    def __init__(self, cell):
        self.cell = cell
        self.inflight = contextvars.ContextVar('matrix_platform_calls', default=())
        self.commit_site = contextvars.ContextVar('matrix_commit_site', default=None)
        self.calls = []
        self.selections = []
        self.violations = []
        self.patched = set()
        self.final_texts = set()

    def final_payload(self, text):
        return not self.cell.startswith('p') and text in self.final_texts

    def observe(self, patch, current, transport, text, *, replacement=None):
        from gateway.run import _strip_response_attachments_for_direct_send

        final_text = _strip_response_attachments_for_direct_send(text, current.adapter)
        self.final_texts.update((text, final_text))
        self.final_texts.add(current.adapter.warning_text(
            t('gateway.notify.plain_fallback_prefix', content=final_text[:3500]), final_text[:3500],
            chat_id=current.source.chat_id, metadata={}))
        for module_name in ('gateway.platforms.base', 'gateway.run_turn', 'gateway.run_notifications',
                            'gateway.stream_consumer', 'gateway.stream_consumer_transport',
                            'gateway.stream_consumer_fallback', 'plugins.platforms.discord.adapter'):
            module = sys.modules.get(module_name)
            if module is None or module_name in self.patched:
                continue
            original = getattr(module, 'commit_processing_coverage', None)
            if original is None:
                continue

            def commit(original=original):
                frame = inspect.currentframe().f_back
                site = (frame.f_globals['__name__'], frame.f_code.co_name, frame.f_lineno,
                        frame.f_lineno - frame.f_code.co_firstlineno)
                token = self.commit_site.set(site)
                try:
                    return original()
                finally:
                    self.commit_site.reset(token)

            patch.setattr(module, 'commit_processing_coverage', commit)
            self.patched.add(module_name)
        target = replacement or current.adapter
        if id(target) in self.patched:
            return
        self.patched.add(id(target))
        original_select = target.select_processing_coverage

        def select(event, outcome):
            if armed():
                calls = self.inflight.get()
                site = self.commit_site.get()
                self.selections.append((calls, site, outcome.value))
                if calls:
                    if not calls[-1][1]:
                        self.violations.append(('non-final selection', calls[-1]))
                elif site is None or site[3] not in self.ALLOWED_CALLLESS.get(site[:2], set()):
                    self.violations.append(('unapproved call-less selection', site))
            return original_select(event, outcome)

        patch.setattr(target, 'select_processing_coverage', select)
        for name in ('send', 'edit_message', 'send_document', 'send_image', 'send_multiple_images',
                     'send_video', 'send_voice', 'send_stream_frame', 'delete_message'):
            original = getattr(target, name, None)
            if original is None:
                continue

            async def call(*args, original=original, name=name, **kwargs):
                content = kwargs.get('content', args[0] if name == 'send_stream_frame' and args
                                     else args[1] if len(args) > 1 else '')
                media = name in {'send_document', 'send_image', 'send_multiple_images', 'send_video', 'send_voice'}
                final = (self.cell in {'q6', 'q7a', 'q7b'} if media else
                         name != 'delete_message' and self.final_payload(content))
                record = (name, final, armed(), (content or '')[:80])
                self.calls.append(record)
                if record[2] and not final:
                    self.violations.append(('non-final armed call', record))
                token = self.inflight.set((*self.inflight.get(), (name, final)))
                try:
                    return await original(*args, **kwargs)
                finally:
                    self.inflight.reset(token)

            patch.setattr(target, name, call)
        for owner, name in ((target, '_finalize_delivery_obligation'),
                            (current.runner, '_redeliver_failed_obligations_for_platform'),
                            (current.runner, '_redeliver_claimed_obligations'),
                            (current.runner, '_arm_flood_timers_for_waiting_rows')):
            original = getattr(owner, name)

            async def outside(*args, original=original, name=name, **kwargs):
                if armed():
                    self.violations.append(('armed ledger operation', name))
                return await original(*args, **kwargs)

            patch.setattr(owner, name, outside)
        original_schedule = current.runner._schedule_flood_redelivery

        def schedule(*args, **kwargs):
            if armed():
                self.violations.append(('armed timer creation',))
            return original_schedule(*args, **kwargs)

        patch.setattr(current.runner, '_schedule_flood_redelivery', schedule)


@pytest.mark.asyncio
@pytest.mark.parametrize('lane,cell', [('normal', 'n3'), ('queued', 'q4'), ('inline', 'i2'),
                                      ('stream-to-normal', 's3'), ('process-report', 'p2')],
                         ids=['normal', 'queued', 'inline', 'stream-to-normal', 'process-report'])
async def test_r5_1_notice_never_settles(tmp_path, lane, cell):
    recorder = PayloadRecorder(cell)
    run = await matrix_scenario(tmp_path, cell, recorder.observe)
    assert any('could not' in (content or '').lower() for _, content in run.transport.accepted), lane
    assert all(state != ('responded', 1) for state in run.states), (lane, run.states)
    assert {101, 102} <= set(run.replay), (lane, run.replay)


@pytest.mark.asyncio
@pytest.mark.parametrize('cell', CELLS)
async def test_r5_3_selection_only_during_final_payload(tmp_path, cell):
    recorder = PayloadRecorder(cell)
    run = await matrix_scenario(tmp_path, cell, recorder.observe)
    assert not recorder.violations, (cell, recorder.violations)
    assert run.states == expected_states(cell), (cell, run.states)
    print('RECORDER ' + json.dumps({'cell': cell, 'calls': recorder.calls,
                                  'selections': recorder.selections}), flush=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', ['normal', 'queued'])
async def test_r5_media_only_cancel_after_acceptance(tmp_path, lane):
    resources, patch, current, _ = matrix_context(tmp_path)
    with resources:
        covered = inbound(current)
        current.host.absorbed_events.append(covered)
        await current.adapter.on_processing_covered(covered)
        transport = MatrixTransport('q7a')
        patch.setattr(current.channel, 'send', transport.send)
        patch.setattr(current.adapter, 'send_typing', AsyncMock())
        patch.setattr(current.adapter, 'stop_typing', AsyncMock())
        media = tmp_path / 'answer.txt'
        media.write_text('answer', encoding='utf-8')
        response = 'MEDIA:' + str(media)
        reached = asyncio.Event()
        original = current.adapter.send_document

        async def upload(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result.success
            reached.set()
            await asyncio.Event().wait()

        patch.setattr(current.adapter, 'send_document', upload)

        async def handler(event):
            if lane == 'queued':
                context = TurnContext(source=current.source, event=current.host, session_key=current.key,
                                      event_message_id='100', inbound_message_id='100')
                result = {'final_response': response, 'messages': []}
                await current.runner._run_agent_deliver_first_response(
                    context, current.adapter, result, result, None)
                return None
            return response

        current.adapter._message_handler = handler
        task = asyncio.create_task(current.adapter._process_message_background(current.host, current.key))
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await drain(current.adapter)
            assert status(current, covered.message_id) == ('responded', 1)
            assert transport.accepted == [('upload', None)]
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('lane', ['t1', 't2'])
async def test_r5_4_inherited_binding_cannot_select(tmp_path, lane):
    resources, patch, current, _ = matrix_context(tmp_path)
    with resources, ThreadPoolExecutor(max_workers=1) as executor:
        covered = inbound(current, 101)
        await steer(current, covered)
        await drain(current.adapter)
        transport = MatrixTransport('n1')
        patch.setattr(current.channel, 'send', transport.send)
        patch.setattr(current.adapter, 'send_typing', AsyncMock())
        release, thread_release = asyncio.Event(), threading.Event()
        tasks, futures, selections = [], [], []
        original_select = current.adapter.select_processing_coverage

        def select(event, outcome):
            selections.append((event, outcome))
            return original_select(event, outcome)

        patch.setattr(current.adapter, 'select_processing_coverage', select)

        async def late():
            await release.wait()
            await current.adapter.send('123', 'late nonfinal')
            child_event = MessageEvent(text='', source=current.source, internal=True)
            await current.adapter.send_final_ledgered(child_event, current.key, 'child final', {}, reply_to=None)

        def thread_send():
            assert thread_release.wait(5)
            base.commit_processing_coverage()

        async def spawn(content):
            if tasks:
                return
            tasks.append(asyncio.create_task(late()))
            context = contextvars.copy_context()
            futures.append(executor.submit(context.run, thread_send))

        transport.before_send = spawn
        try:
            if lane == 't1':
                await current.adapter.send_final_ledgered(current.host, current.key, 'done', {}, reply_to='100')
            else:
                ctx = TurnContext(source=current.source, event=current.host, session_key=current.key,
                                  event_message_id='100', inbound_message_id='100')
                result = {'final_response': 'done'}
                await current.runner._run_agent_deliver_first_response(ctx, current.adapter, result, result, None)
            prior = len([event for event, _ in selections if event is current.host])
            release.set()
            thread_release.set()
            await asyncio.gather(*tasks)
            for future in futures:
                future.result(timeout=5)
            await drain(current.adapter)
            assert len([event for event, _ in selections if event is current.host]) == prior, selections
            assert status(current, 101) == ('responded', 1)
        finally:
            release.set()
            thread_release.set()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_d5_gather_inside_region_selects():
    selected = []

    async def commit():
        base.commit_processing_coverage()

    with base.coverage_delivery_scope(lambda: selected.append(True)):
        await asyncio.gather(commit())
        await asyncio.shield(commit())
        await asyncio.wait_for(commit(), timeout=5)
        await asyncio.to_thread(base.commit_processing_coverage)
    assert selected == [True] * 4


async def inline_scenario(tmp_path, *, cancel_at=None, mode='plain'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    resources, patch, current, close_connections = matrix_context(tmp_path)
    with resources:
        covered = [inbound(current, 101, 'argument'), inbound(current, 102, ' ')]
        current.host.absorbed_events.extend(covered)
        for event in covered:
            await current.adapter.on_processing_covered(event)
        if mode == 'command':
            current.host.text = '/help'
        if mode == 'cached':
            current.host._gateway_inline_coverage_event = current.host
        if mode == 'deferred':
            current.host._gateway_inline_deferred = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            clock = DeliveryClock(executor)
            patch.setattr(asyncio, 'to_thread', clock.to_thread)
            patch.setattr(current.channel, 'send', clock.send)
            current.adapter._message_handler = AsyncMock(return_value='done')
            clock.enabled = True
            lifecycle = asyncio.create_task(current.adapter._dispatch_inline_reply(current.host))
            current.adapter._background_tasks.add(lifecycle)
            lifecycle.add_done_callback(current.adapter._background_tasks.discard)
            requested = accepted = False
            try:
                for iteration in range(200):
                    if lifecycle.done():
                        break
                    if iteration == cancel_at:
                        requested, accepted = True, clock.accepted
                        clock.shutting_down = True
                        await current.adapter.cancel_background_tasks()
                        break
                    await asyncio.sleep(0)
                else:
                    raise AssertionError('inline lifecycle exceeded 200 deterministic iterations')
                await asyncio.gather(lifecycle, return_exceptions=True)
                await current.adapter.cancel_background_tasks()
                clock.enabled = False
                states = [status(current, event.message_id) for event in covered]
                current.adapter._with_discord_recovery_db(lambda conn: conn.execute(
                    "UPDATE discord_messages SET updated_at='2000-01-01T00:00:00+00:00' WHERE status='processing'"))
                close_connections()
                fresh, _ = await restart_scan(current, patch, [current.host, *covered], expect_empty=False)
                replay = [call.args[0].id for call in fresh._handle_message.await_args_list]
                return SimpleNamespace(requested=requested, accepted=accepted if requested else clock.accepted,
                                       states=states, replay=replay, iteration=iteration)
            finally:
                clock.enabled = False
                if not lifecycle.done():
                    lifecycle.cancel()
                await asyncio.gather(lifecycle, return_exceptions=True)


@pytest.mark.asyncio
async def test_r5_5_inline_cancel_sweep(tmp_path):
    for mode in ('plain', 'command', 'cached'):
        accepted = []
        for iteration in range(200):
            run = await inline_scenario(tmp_path / f'{mode}-{iteration}', cancel_at=iteration, mode=mode)
            if run.accepted:
                expected = [('cancelled', 0), ('responded', 1)] if mode == 'command' else [('responded', 1)] * 2
                accepted.append((iteration, run.states))
                assert run.states == expected, (mode, iteration, run.states)
                assert 102 not in run.replay
            else:
                assert all(state != ('responded', 1) for state in run.states)
                assert {101, 102} <= set(run.replay)
            if not run.requested:
                break
        else:
            raise AssertionError('inline sweep never reached uncancelled completion')
        assert len(accepted) > 1, (mode, accepted)
        print('INLINE ' + json.dumps({'mode': mode, 'accepted': accepted}), flush=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['plain', 'command', 'cached', 'deferred'])
async def test_d5_inline_cancel_before_commit_recoverable(tmp_path, mode):
    run = await inline_scenario(tmp_path, cancel_at=0, mode=mode)
    assert not run.accepted
    assert all(state != ('responded', 1) for state in run.states)
    assert {101, 102} <= set(run.replay)


@pytest.mark.asyncio
async def test_d5_inline_deferred_reply_does_not_settle(tmp_path):
    run = await inline_scenario(tmp_path, mode='deferred')
    assert run.accepted
    assert run.states == [('processing', 0)] * 2
    assert {101, 102} <= set(run.replay)
