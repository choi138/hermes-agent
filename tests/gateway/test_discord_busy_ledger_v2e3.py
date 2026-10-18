"""Shutdown, admission and inline exits retain every Discord input identity."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome
from tests.gateway.test_discord_busy_ledger_v2e import adapter, busy, inbound, steer
from tests.gateway.test_discord_busy_ledger_v2e2 import live_batch, status
from tests.gateway.test_discord_missed_message_backfill import DiscordAdapter, make_message
from tests.gateway.test_queued_followup_processing_hooks import _install_fake_agent, _make_runner
from tests.waiting import async_wait_until


async def restart_scan(busy, monkeypatch, events, *, expect_empty=True):
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
    scan = fresh._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT missed, dispatched FROM discord_recovery_scans ORDER BY started_at DESC LIMIT 1',
    ).fetchone())
    if expect_empty:
        fresh._handle_message.assert_not_awaited()
        assert scan == (0, 0)
    return fresh, scan


async def cancel_at_delivery_boundary(busy, monkeypatch, window):
    reached, release = asyncio.Event(), asyncio.Event()
    late = inbound(busy)
    original_thread = asyncio.to_thread
    original_marker = busy.adapter._release_turn_marker
    original_covered = busy.adapter.on_processing_covered

    async def to_thread(function, *args, **kwargs):
        name = function.__name__
        block = (
            window == 'host-write' and name == '_record_discord_processing_complete'
            and args[1] == ProcessingOutcome.SUCCESS
        ) or (
            window == 'coverage-write' and name == '_record_discord_covered_complete'
            and args[1] == ProcessingOutcome.SUCCESS
        ) or (
            window == 'claim-wait' and name == '_record_discord_processing_start'
            and args[0] is late
        )
        if block and not release.is_set():
            if window != 'claim-wait':
                reached.set()
            await release.wait()
        return await original_thread(function, *args, **kwargs)

    async def covered(event, outcome=None, **kwargs):
        if window == 'claim-wait' and event is late and outcome == ProcessingOutcome.SUCCESS:
            reached.set()
        await original_covered(event, outcome, **kwargs)

    async def marker(event):
        if window == 'marker' and busy.channel.send.await_count and not release.is_set():
            reached.set()
            await release.wait()
        await original_marker(event)

    async def finalize(*args):
        reached.set()
        await release.wait()

    async def handler(event):
        await steer(busy, late)
        return 'done'

    monkeypatch.setattr(asyncio, 'to_thread', to_thread)
    monkeypatch.setattr(busy.adapter, 'on_processing_covered', covered)
    monkeypatch.setattr(busy.adapter, '_release_turn_marker', marker)
    if window == 'obligation-finalize':
        monkeypatch.setattr(busy.adapter, '_record_delivery_obligation', AsyncMock(return_value='fake-obligation'))
        monkeypatch.setattr(busy.adapter, '_finalize_delivery_obligation', finalize)
    busy.adapter._message_handler = handler
    task = asyncio.create_task(busy.adapter._process_message_background(busy.host, busy.key))
    busy.adapter._background_tasks.add(task)
    task.add_done_callback(busy.adapter._background_tasks.discard)
    shutdown = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        shutdown = asyncio.create_task(busy.adapter.cancel_background_tasks())
        await async_wait_until(
            lambda: task in busy.adapter._expected_cancelled_tasks or shutdown.done(), timeout=3,
        )
        release.set()
        await asyncio.wait_for(shutdown, 5)
    finally:
        release.set()
        if shutdown is not None and not shutdown.done():
            await asyncio.wait_for(shutdown, 5)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return late


@pytest.mark.asyncio
@pytest.mark.parametrize('window', ['marker', 'host-write', 'claim-wait', 'coverage-write', 'obligation-finalize'])
async def test_r3_1_shutdown_cancel_keeps_answered_absorbed(busy, monkeypatch, window):
    late = await cancel_at_delivery_boundary(busy, monkeypatch, window)
    assert status(busy, 101) == ('responded', 1)
    await restart_scan(busy, monkeypatch, [busy.host, late])


async def runner_exits(busy, monkeypatch, tmp_path, *, ticket=False, fault=None):
    loop = asyncio.get_running_loop()
    runner = _make_runner(busy.adapter)
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._busy_input_mode = 'steer'
    runner._effective_busy_input_mode = lambda _: 'steer'
    runner._effective_busy_text_mode = lambda _: 'steer'
    runner._check_slash_access = lambda *_: None
    late = inbound(busy, 101, 'late-payload')
    late._process_user_received_at = 200.0
    successor = inbound(busy, 102, 'queued-successor')
    successor._process_user_received_at = 300.0
    resume = inbound(busy, 103, 'resume-ticket')
    marker = {'turn_id': 'owned-turn', 'origin_owner': 'owner'}
    resume._hermes_turn_resume = marker
    trace = SimpleNamespace(inputs=[], agents=[], admission=[], before=[], replies=[], late=late,
                            runner=runner, resume=resume, origins=[])
    cancellation_ready, worker_release = asyncio.Event(), threading.Event()
    workers = []
    original_worker = runner._run_agent_start_turn_worker

    def start_worker(*args):
        worker = original_worker(*args)
        workers.append(worker.executor_task)
        return worker

    monkeypatch.setattr(runner, '_run_agent_start_turn_worker', start_worker)
    if ticket:
        store = SimpleNamespace(_entries={busy.key: SimpleNamespace(active_turn=marker)},
                                resume_owner_matches=lambda *args, **kwargs: True,
                                get_reasoning_override=lambda *args: None)
        runner.session_store = store
        busy.adapter._session_store = store
        runner._resume_owner_authorized = lambda *_: True

    async def inject(agent):
        await async_wait_until(lambda: runner._session_state(busy.key).turn.agent is agent, timeout=3)
        assert await runner._handle_active_session_busy_message(late, busy.key)
        if ticket:
            resume._gateway_pending_followups = [successor]
            busy.adapter._pending_messages[busy.key] = resume
        cancellation_ready.set()

    class Agent:
        def __init__(self, **kwargs):
            self.tools = []
            self.pending = []
            self.lock = threading.Lock()
            trace.agents.append(self)

        def steer(self, text):
            with self.lock:
                self.pending.append(text)
            return True

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
            trace.origins.append((kwargs.get('persist_user_platform_id'), kwargs.get('persist_user_timestamp')))
            if number == 1:
                self._drain_pending_steer()
                asyncio.run_coroutine_threadsafe(inject(self), loop).result(timeout=5)
                if fault == 'worker-cancel':
                    assert worker_release.wait(timeout=5)
                elif fault:
                    if fault == 'interrupt':
                        raise asyncio.CancelledError()
                    raise RuntimeError('fake agent failure')
            else:
                trace.before.append(status(busy, 101))
                trace.inputs.extend(self.pending)
            return {'final_response': f'done-{number}', 'messages': [], 'api_calls': 1}

    _install_fake_agent(monkeypatch, tmp_path, Agent)
    original_start = busy.adapter.on_processing_start

    async def start(event):
        trace.admission.append(event.message_id)
        await original_start(event)

    monkeypatch.setattr(busy.adapter, 'on_processing_start', start)
    original_send = busy.adapter.send

    async def send(chat_id, content, **kwargs):
        trace.replies.append((content, status(busy, 101)))
        return await original_send(chat_id, content, **kwargs)

    monkeypatch.setattr(busy.adapter, 'send', send)

    async def handler(event):
        history = [{'role': 'user', 'content': 'opening',
                    'display_metadata': {'gateway_input_owner': 'owner'}}] if event is resume else []
        try:
            result = await runner._run_agent(message=event.text, context_prompt='', history=history,
                source=event.source, session_id='v2e3-turn', session_key=busy.key,
                event=event, event_message_id=event.message_id, inbound_message_id=event.message_id,
                persist_user_timestamp=event._process_user_received_at)
        except RuntimeError as error:
            if fault != 'error-reply':
                raise
            return await runner._hmwa_agent_error_reply(error, event, event.source, None, busy.key,
                SimpleNamespace(history=[], message_text=None))
        return None if result.get('already_sent') else result['final_response']

    busy.adapter._message_handler = handler
    lifecycle = None
    try:
        if fault == 'worker-cancel':
            lifecycle = asyncio.create_task(busy.adapter._process_message_background(busy.host, busy.key))
            await asyncio.wait_for(cancellation_ready.wait(), 5)
            busy.adapter._expected_cancelled_tasks.add(lifecycle)
            lifecycle.cancel()
            await lifecycle
        else:
            await busy.adapter._process_message_background(busy.host, busy.key)
    except asyncio.CancelledError:
        assert fault in {'interrupt', 'worker-cancel'}
    finally:
        worker_release.set()
        if lifecycle is not None:
            if not lifecycle.done():
                lifecycle.cancel()
            await asyncio.gather(lifecycle, return_exceptions=True)
        await asyncio.gather(*workers, return_exceptions=True)
    await async_wait_until(lambda: not busy.adapter._background_tasks, timeout=5)
    pending = busy.adapter.get_pending_message(busy.key) if fault else None
    if pending is not None:
        await busy.adapter._process_message_background(pending, busy.key)
        await async_wait_until(lambda: not busy.adapter._background_tasks, timeout=5)
    return trace


@pytest.mark.asyncio
async def test_r3_2_resume_ticket_keeps_late_steer(busy, monkeypatch, tmp_path):
    trace = await runner_exits(busy, monkeypatch, tmp_path, ticket=True)
    assert sum('late-payload' in message for message in trace.inputs) == 1
    assert trace.admission[:2] == ['100', '103']
    assert trace.inputs[1] == ''
    assert 'late-payload' in trace.inputs[2]
    assert trace.inputs[3] == 'queued-successor'
    assert trace.origins[2] == ('101', 200.0)
    assert trace.replies[0][1] != ('responded', 1)
    assert status(busy, 101) == ('responded', 1)
    assert all(not agent.pending for agent in trace.agents)


async def inline_batch(busy, monkeypatch, path, *, single=False):
    first = inbound(busy, 201, '/commands' if path == 'command' else 'first-fragment')
    if path == 'approval':
        first.text = 'yes'
    fragments = [first]
    if not single:
        fragments.append(inbound(busy, 202, ' ' if path == 'approval' else '2' if path == 'command' else 'second-fragment'))
    event = await live_batch(busy.adapter, fragments, monkeypatch)
    if path == 'approval':
        monkeypatch.setattr('tools.approval.has_blocking_approval', lambda _: True)
        busy.runner._handle_approve_command = AsyncMock(return_value='Approved')

        async def approval(incoming):
            assert await busy.runner._handle_active_session_busy_message(incoming, busy.key)

        busy.adapter._message_handler = approval
        await busy.adapter._dispatch_inline_reply(event)
    elif path == 'command':
        from hermes_cli.commands import resolve_command
        runner = _make_runner(busy.adapter)
        busy.adapter._message_handler = lambda incoming: runner._dispatch_busy_slash_command(
            incoming, resolve_command('commands'), busy.key, busy.source)
        await busy.adapter._handle_message_while_active(event, busy.key)
    else:
        from tools import clarify_gateway as clarify
        clarify.register('v2e3-clarify', busy.key, 'Explain', [])
        busy.adapter._message_handler = lambda incoming: busy.runner._hm_clarify_reply(
            incoming, busy.source, busy.key)
        try:
            await busy.adapter._handle_message_while_active(event, busy.key)
            assert clarify.get_pending_for_session(busy.key, include_choice_prompts=True).response == event.text
        finally:
            clarify.clear_session(busy.key)
    return fragments


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['clarify', 'command', 'approval'])
async def test_r3_3_inline_paths_settle_folded_ids(busy, monkeypatch, path):
    events = await inline_batch(busy, monkeypatch, path)
    assert status(busy, 202) == ('responded', 1)
    assert status(busy, 201) == ('responded', 1)
    await restart_scan(busy, monkeypatch, events)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['error-reply', 'failure', 'interrupt', 'worker-cancel'])
async def test_r3_4_agent_exception_late_steer(busy, monkeypatch, tmp_path, fault):
    trace = await runner_exits(busy, monkeypatch, tmp_path, fault=fault)
    assert all(not agent.pending for agent in trace.agents)
    assert len(trace.agents) == (2 if fault == 'worker-cancel' else 1)
    assert sum('late-payload' in message for message in trace.inputs) == 1
    assert trace.origins[-1] == ('101', 200.0)
    assert all(before != ('responded', 1) for before in trace.before)
    assert status(busy, 101) == ('responded', 1)


@pytest.mark.asyncio
async def test_r3_5_restart_contract(busy, monkeypatch):
    late = await cancel_at_delivery_boundary(busy, monkeypatch, 'host-write')
    events = await inline_batch(busy, monkeypatch, 'clarify')
    await restart_scan(busy, monkeypatch, [busy.host, late, *events])


@pytest.mark.asyncio
async def test_d3_cancel_before_delivery_still_recoverable(busy):
    entered, release = asyncio.Event(), asyncio.Event()
    late = inbound(busy)

    async def handler(event):
        await steer(busy, late)
        entered.set()
        await release.wait()

    busy.adapter._message_handler = handler
    task = asyncio.create_task(busy.adapter._process_message_background(busy.host, busy.key))
    await asyncio.wait_for(entered.wait(), 3)
    busy.adapter._expected_cancelled_tasks.add(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert status(busy, 101) == ('cancelled', 0)
    assert await busy.adapter._should_backfill_discord_message(late.raw_message)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', list(ProcessingOutcome))
async def test_d3_host_row_values_unchanged(busy, outcome):
    await busy.adapter.on_processing_start(busy.host)
    await busy.adapter.on_processing_complete(busy.host, outcome)
    assert status(busy, 100) == ({ProcessingOutcome.SUCCESS: 'responded',
                                ProcessingOutcome.FAILURE: 'failed',
                                ProcessingOutcome.CANCELLED: 'cancelled'}[outcome],
                                int(outcome == ProcessingOutcome.SUCCESS))


@pytest.mark.asyncio
@pytest.mark.parametrize('window', ['marker', 'host-write', 'claim-wait', 'coverage-write', 'obligation-finalize'])
async def test_d3_delivered_host_row_survives_shutdown(busy, monkeypatch, window):
    await cancel_at_delivery_boundary(busy, monkeypatch, window)
    assert status(busy, 100) == ('responded', 1)


@pytest.mark.asyncio
async def test_d3_resume_ticket_still_enters_through_admission(busy):
    ticket = inbound(busy, 103, 'resume-ticket')
    ticket._hermes_turn_resume = {'turn_id': 'owned-turn'}
    busy.adapter._pending_messages[busy.key] = ticket
    busy.runner.session_store = SimpleNamespace(resume_owner_matches=lambda *args, **kwargs: True)
    busy.adapter._session_store = busy.runner.session_store
    result = await busy.runner._run_agent_drain_pending(
        {'pending_steer': ''}, busy.adapter, busy.source, busy.key)
    assert result == (None, None)
    assert busy.adapter._pending_messages[busy.key] is ticket
    entered = []
    busy.adapter._message_handler = AsyncMock(return_value='done')
    original_start = busy.adapter.on_processing_start

    async def start(event):
        entered.append(event)
        await original_start(event)

    busy.adapter.on_processing_start = start
    await busy.adapter._process_message_background(busy.adapter.get_pending_message(busy.key), busy.key)
    assert entered == [ticket]


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['clarify', 'command', 'approval'])
async def test_d3_inline_single_event_rows_unchanged(busy, monkeypatch, path):
    await inline_batch(busy, monkeypatch, path, single=True)
    assert status(busy, 201) == ('responded', 1)
    assert status(busy, 202) is None


@pytest.mark.asyncio
async def test_d3_shutdown_still_cancels_other_tasks(busy):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def background():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(background())
    busy.adapter._background_tasks.add(task)
    await entered.wait()
    await busy.adapter.cancel_background_tasks()
    assert cancelled.is_set()
    assert task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/help', '/kanban help'])
async def test_d3_command_discarded_sibling_stays_recoverable(busy, monkeypatch, command):
    from hermes_cli.commands import resolve_command

    events = [inbound(busy, 201, command), inbound(busy, 202, 'not-command-arguments')]
    event = await live_batch(busy.adapter, events, monkeypatch)
    runner = _make_runner(busy.adapter)
    busy.adapter._message_handler = lambda incoming: runner._dispatch_busy_slash_command(
        incoming, resolve_command(command.split()[0].lstrip('/')), busy.key, busy.source)
    await busy.adapter._dispatch_inline_reply(event)
    assert status(busy, 201) == ('responded', 1)
    assert await busy.adapter._should_backfill_discord_message(events[1].raw_message)
