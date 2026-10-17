"""Absorbed Discord inputs retain their own durable recovery identity."""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_discord_missed_message_backfill import (
    DiscordAdapter, FakeChannel, adapter, make_message,
)
from tests.gateway.test_process_followup_continuity import busy_runner
from tests.gateway.restart_test_helpers import RestartTestAdapter
from tests.gateway.test_queued_followup_processing_hooks import (
    _TwoTurnAgent, _install_fake_agent, _make_runner,
)
from tests.waiting import async_wait_until
from gateway.config import PlatformConfig
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import build_session_key


@pytest.fixture
def busy(adapter, monkeypatch):
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED', 'false')
    runner, source, _, agent, _ = busy_runner('steer', followup=False)
    source.chat_id = '123'
    source.user_id = '42'
    key = build_session_key(source)
    channel = FakeChannel()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=900, content='done'))
    adapter._client.get_channel = lambda _: channel
    adapter._handle_message = AsyncMock(return_value=True)
    runner._delivery_adapter_for = lambda _: adapter
    runner._intake_adapter_for = lambda _: adapter
    runner._effective_busy_input_mode = lambda _: runner._busy_input_mode
    adapter.gateway_runner = None
    host = MessageEvent(text='opening', source=source, message_id='100',
                        raw_message=make_message(message_id=100, channel=channel))
    state = runner._session_state(key)
    state.turn.agent = agent
    state.turn.event = host
    return SimpleNamespace(runner=runner, source=source, key=key, agent=agent,
                           adapter=adapter, host=host, channel=channel)


def inbound(busy, message_id=101, text='follow-up', kind=MessageType.TEXT):
    return MessageEvent(text=text, source=busy.source, message_id=str(message_id),
                        message_type=kind,
                        raw_message=make_message(message_id=message_id, content=text, channel=busy.channel))


def row(busy, event):
    return busy.adapter._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT status, replied, updated_at FROM discord_messages WHERE message_id=?',
        (event.message_id,),
    ).fetchone())


async def steer(busy, event):
    assert await busy.runner._handle_active_session_busy_message(event, busy.key)
    busy.agent.steer.assert_called()


async def finish(busy, outcome=ProcessingOutcome.SUCCESS):
    await busy.adapter._run_processing_hook('on_processing_complete', busy.host, outcome)


@pytest.mark.asyncio
async def test_e1_steered_message_complete_after_host_success(busy):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    await finish(busy)
    assert row(busy, event)[:2] == ('responded', 1)
    assert not await busy.adapter._should_backfill_discord_message(event.raw_message)


@pytest.mark.asyncio
async def test_e2_steered_message_claimed_while_host_runs(busy, monkeypatch):
    await busy.adapter.on_processing_start(busy.host)
    before = busy.adapter._utc_now_iso()
    event = inbound(busy)
    scheduled, release = asyncio.Event(), asyncio.Event()
    original_to_thread = asyncio.to_thread
    loop_thread = threading.get_ident()
    writer_threads = []

    async def delayed_to_thread(function, *args, **kwargs):
        if args and args[0] is event:
            scheduled.set()
            await release.wait()

        def invoke():
            writer_threads.append(threading.get_ident())
            return function(*args, **kwargs)

        return await original_to_thread(invoke)

    monkeypatch.setattr(asyncio, 'to_thread', delayed_to_thread)
    await steer(busy, event)
    try:
        await asyncio.wait_for(scheduled.wait(), timeout=2)
        assert row(busy, event) is None
    finally:
        release.set()
    await async_wait_until(lambda: row(busy, event) is not None, timeout=2)
    claimed = row(busy, event)
    assert claimed[0] == 'processing' and claimed[2] >= before
    assert not await busy.adapter._should_backfill_discord_message(event.raw_message)
    await finish(busy)
    assert writer_threads and loop_thread not in writer_threads


@pytest.mark.asyncio
async def test_e2_absorption_ledger_error_does_not_fail_busy_handler(busy, caplog):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    with sqlite3.connect(busy.adapter._discord_recovery_store.path()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        await steer(busy, event)
        await async_wait_until(lambda: 'Discord recovery ledger unavailable' in caplog.text, timeout=2)
        await finish(busy)
        connection.rollback()
    assert await busy.adapter._should_backfill_discord_message(event.raw_message)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,status', [(ProcessingOutcome.FAILURE, 'failed'),
                                           (ProcessingOutcome.CANCELLED, 'cancelled')])
async def test_e3_steered_message_mirrors_host_failure_and_cancel(busy, outcome, status):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    await finish(busy, outcome)
    assert row(busy, event)[:2] == (status, 0)
    assert row(busy, busy.host)[0] == status
    assert await busy.adapter._should_backfill_discord_message(event.raw_message)


@pytest.mark.asyncio
@pytest.mark.parametrize('replay_outcome', [None, ProcessingOutcome.FAILURE, ProcessingOutcome.SUCCESS])
async def test_e4_undrained_steer_not_completed_by_host(busy, replay_outcome):
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    await steer(busy, event)
    pending = busy.runner._session_state(busy.key).turn.process_steer_events[-1][0]
    replay, _ = await busy.runner._run_agent_drain_pending(
        {'pending_steer': pending}, busy.adapter, busy.source, busy.key)
    assert row(busy, event) is not None and row(busy, event)[0] == 'cancelled'
    if replay_outcome is not None:
        await busy.adapter.on_processing_start(replay)
        await busy.adapter.on_processing_complete(replay, replay_outcome)
    await finish(busy)
    expected = {None: 'cancelled', ProcessingOutcome.FAILURE: 'failed',
                ProcessingOutcome.SUCCESS: 'responded'}[replay_outcome]
    assert row(busy, event)[0] == expected
    assert row(busy, busy.host)[0] == 'responded'


@pytest.mark.asyncio
async def test_e4_undrained_batch_and_late_steer_keep_own_outcomes(busy):
    await busy.adapter.on_processing_start(busy.host)
    delivered, first, second, late = [inbound(busy, 201 + index, text) for index, text in enumerate(
        ('already consumed', 'pending first', 'pending second', 'after drain'))]
    for event in (delivered, first, second, late):
        await steer(busy, event)
    accepted = busy.runner._session_state(busy.key).turn.process_steer_events
    pending_text = '\n'.join(text for text, event in accepted if event is not delivered)
    replay, _ = await busy.runner._run_agent_drain_pending(
        {'pending_steer': pending_text}, busy.adapter, busy.source, busy.key,
    )
    assert [row(busy, event)[0] if row(busy, event) else None for event in (first, second, late)] == ['cancelled'] * 3
    await busy.adapter.on_processing_start(replay)
    await busy.adapter.on_processing_complete(replay, ProcessingOutcome.FAILURE)
    await finish(busy)
    assert row(busy, delivered)[:2] == ('responded', 1)
    assert [row(busy, event)[:2] for event in (first, second, late)] == [('failed', 0)] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,status', [(ProcessingOutcome.SUCCESS, 'responded'),
                                           (ProcessingOutcome.FAILURE, 'failed'),
                                           (ProcessingOutcome.CANCELLED, 'cancelled')])
async def test_e5_redirect_records_both_messages(busy, outcome, status):
    busy.runner._busy_input_mode = 'interrupt'
    await busy.adapter.on_processing_start(busy.host)
    event = inbound(busy)
    assert await busy.runner._handle_active_session_busy_message(event, busy.key)
    busy.agent.redirect.assert_called()
    await async_wait_until(lambda: row(busy, event) is not None and row(busy, event)[0] == 'processing', timeout=2)
    await finish(busy, outcome)
    expected = (status, int(outcome == ProcessingOutcome.SUCCESS))
    assert row(busy, event)[:2] == expected
    assert row(busy, busy.host)[:2] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'photo', 'caption', 'nested'])
@pytest.mark.parametrize('outcome,status', [(ProcessingOutcome.SUCCESS, 'responded'),
                                           (ProcessingOutcome.FAILURE, 'failed'),
                                           (ProcessingOutcome.CANCELLED, 'cancelled')])
async def test_e6_queue_merged_messages_all_recorded(busy, kind, outcome, status):
    message_type = MessageType.PHOTO if kind in ('photo', 'caption') else MessageType.TEXT
    events = [inbound(busy, message_id, kind=message_type) for message_id in (201, 202, 203)]
    if message_type == MessageType.PHOTO:
        for event in events:
            event.media_urls = [f'https://invalid/{event.message_id}.png']
            event.media_types = ['image/png']
    if kind == 'caption':
        events[-1].message_type = MessageType.TEXT
        events[-1].media_urls = []
        events[-1].media_types = []
    if kind == 'nested':
        nested = {}
        for event in events[1:]:
            merge_pending_message_event(nested, busy.key, event, merge_text=True)
        to_merge = [events[0], nested[busy.key]]
    else:
        to_merge = events
    pending = {}
    for event in to_merge:
        merge_pending_message_event(pending, busy.key, event, merge_text=True)
    host = pending[busy.key]
    await busy.adapter.on_processing_start(host)
    assert [row(busy, event)[0] if row(busy, event) else None for event in events] == ['processing'] * 3
    await busy.adapter.on_processing_complete(host, outcome)
    assert [row(busy, event)[:2] for event in events] == [(status, int(outcome == ProcessingOutcome.SUCCESS))] * 3


@pytest.mark.asyncio
async def test_e7_restart_backfill_skips_steered_message_end_to_end(busy, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event):
        entered.set()
        await release.wait()
        return 'done'

    busy.adapter._message_handler = handler
    task = asyncio.create_task(busy.adapter._process_message_background(busy.host, busy.key))
    await entered.wait()
    event = inbound(busy)
    try:
        await steer(busy, event)
    finally:
        release.set()
        await task
    reply = make_message(message_id=900, content='done', channel=busy.channel)
    reply.author = busy.adapter._client.user
    reply.reference = SimpleNamespace(message_id=100)
    busy.channel._history_messages = [busy.host.raw_message, event.raw_message, reply]
    monkeypatch.setenv('DISCORD_REQUIRE_MENTION', 'false')
    fresh = DiscordAdapter(PlatformConfig(enabled=True, token='fake-token'))
    fresh._client = SimpleNamespace(user=busy.adapter._client.user, get_channel=lambda _: busy.channel)
    fresh._ready_event.set()
    fresh._handle_message = AsyncMock(return_value=True)
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS', '123')
    await fresh._run_missed_message_backfill()
    fresh._handle_message.assert_not_awaited()
    scan = fresh._with_discord_recovery_db(lambda conn: conn.execute(
        'SELECT missed, dispatched FROM discord_recovery_scans ORDER BY started_at DESC LIMIT 1',
    ).fetchone())
    assert scan == (0, 0)
    assert row(busy, event)[:2] == ('responded', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['inline', 'active_command'])
async def test_e8_bypass_without_reply_is_complete(busy, path):
    event = inbound(busy, text='/stop')
    busy.adapter._message_handler = AsyncMock(return_value='')
    if path == 'active_command':
        await busy.adapter._dispatch_active_session_command(event, busy.key, 'stop')
    else:
        await busy.adapter._dispatch_inline_reply(event)
    assert row(busy, event) is not None and row(busy, event)[:2] == ('responded', 1)
    busy.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_e8_clarify_text_intercept_is_complete(busy):
    from tools import clarify_gateway as clarify
    event = inbound(busy, text='1')
    clarify.register('v2e-clarify', busy.key, 'Pick one', ['A', 'B'])

    async def handler(incoming):
        return await busy.runner._hm_clarify_reply(incoming, busy.source, busy.key)

    busy.adapter._message_handler = handler
    try:
        await busy.adapter._handle_message_while_active(event, busy.key)
        assert clarify.get_pending_for_session(busy.key, include_choice_prompts=True).response == 'A'
        assert row(busy, event) is not None and row(busy, event)[:2] == ('responded', 1)
        busy.channel.send.assert_not_awaited()
    finally:
        clarify.clear_session(busy.key)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['yes', 'no'])
async def test_e8_plaintext_approval_is_complete(busy, monkeypatch, text):
    event = inbound(busy, text=text)
    monkeypatch.setattr('tools.approval.has_blocking_approval', lambda _: True)
    busy.runner._handle_approve_command = AsyncMock(return_value='Approved')
    busy.runner._handle_deny_command = AsyncMock(return_value='Denied')
    assert await busy.runner._handle_active_session_busy_message(event, busy.key)
    assert row(busy, event)[:2] == ('responded', 1)
    assert not await busy.adapter._should_backfill_discord_message(event.raw_message)
    busy.agent.steer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['inline', 'active_command'])
async def test_d_bypass_with_reply_already_complete(busy, path):
    event = inbound(busy, text='/stop')
    busy.adapter._message_handler = AsyncMock(return_value='Stopped')
    if path == 'active_command':
        await busy.adapter._dispatch_active_session_command(event, busy.key, 'stop')
    else:
        await busy.adapter._dispatch_inline_reply(event)
    assert row(busy, event)[:2] == ('responded', 1)
    busy.channel.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,status', [(ProcessingOutcome.SUCCESS, 'responded'),
                                           (ProcessingOutcome.FAILURE, 'failed'),
                                           (ProcessingOutcome.CANCELLED, 'cancelled')])
async def test_d_ordinary_message_lifecycle_unchanged(busy, outcome, status):
    await busy.adapter.on_processing_start(busy.host)
    assert row(busy, busy.host)[0] == 'processing'
    await finish(busy, outcome)
    assert row(busy, busy.host)[:2] == (status, int(outcome == ProcessingOutcome.SUCCESS))


@pytest.mark.asyncio
async def test_d_backfill_disabled_no_ledger_writes(busy, monkeypatch):
    monkeypatch.setenv('DISCORD_MISSED_MESSAGE_BACKFILL', 'false')
    await busy.adapter.on_processing_start(busy.host)
    await steer(busy, inbound(busy))
    await finish(busy)
    assert not busy.adapter._discord_recovery_store.path().exists()


@pytest.mark.asyncio
async def test_d_non_discord_adapter_unaffected(busy):
    other = RestartTestAdapter()
    busy.runner._delivery_adapter_for = lambda _: other
    busy.runner._intake_adapter_for = lambda _: other
    await other.on_processing_start(busy.host)
    await steer(busy, inbound(busy))
    await other.on_processing_complete(busy.host, ProcessingOutcome.SUCCESS)
    assert other.sent == []
    assert not busy.adapter._discord_recovery_store.path().exists()


@pytest.mark.asyncio
async def test_d_no_reactions_for_absorbed_messages(busy):
    events = [busy.host, inbound(busy)]
    for event in events:
        event.raw_message.add_reaction = AsyncMock()
        event.raw_message.remove_reaction = AsyncMock()
    await busy.adapter.on_processing_start(busy.host)
    await steer(busy, events[1])
    await finish(busy)
    assert [call.args[0] for call in busy.host.raw_message.add_reaction.await_args_list] == ['👀', '✅']
    events[1].raw_message.add_reaction.assert_not_awaited()
    events[1].raw_message.remove_reaction.assert_not_awaited()
    busy.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_d_pending_followups_keep_own_lifecycle(busy, monkeypatch, tmp_path):
    head, following = inbound(busy, 201, 'head'), inbound(busy, 202, 'following')
    third = inbound(busy, 203, 'third')
    head._gateway_pending_followups = [third]
    pending = {busy.key: head}
    merge_pending_message_event(pending, busy.key, following, merge_text=True)
    assert head.text == 'head'
    events = {event.text: event for event in (head, third, following)}

    class CheckedAgent(_TwoTurnAgent):
        calls = []

        def run_conversation(self, message, **kwargs):
            if message in events:
                assert row(busy, events[message])[0] == 'processing'
            return super().run_conversation(message, **kwargs)

    _install_fake_agent(monkeypatch, tmp_path, CheckedAgent)
    runner = _make_runner(busy.adapter)
    busy.adapter._pending_messages = pending
    result = await runner._run_agent(
        message='opening', context_prompt='', history=[], source=busy.source,
        session_id='separate-hooks', session_key=busy.key,
    )
    assert result['final_response'] == 'done-4'
    assert CheckedAgent.calls == ['opening', 'head', 'third', 'following']
    for event in events.values():
        assert row(busy, event)[:2] == ('responded', 1)
