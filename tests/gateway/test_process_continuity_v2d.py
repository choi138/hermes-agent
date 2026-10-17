"""Production-profile notices and late-steer provenance (review F10/F21/F23)."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.process_continuations import report_stops
from gateway.run import _profile_runtime_scope
from gateway.session import build_session_key
from tests.gateway.test_process_continuity_v2 import enabled, home
from tests.gateway.test_process_followup_continuity import armed_session, busy_runner, update_payload
from tools import process_registry_continuation as continuation
from tools import process_registry_followups as followups


@pytest.fixture
def discord(enabled, monkeypatch):
    monkeypatch.delenv('HERMES_HEALTH_MENTION', raising=False)
    enabled.source = replace(enabled.source, platform=Platform.DISCORD, user_id='123456789')
    enabled.entry = enabled.session_store.get_or_create_session(enabled.source)
    return enabled


def stopped_job(runner, execution='proc_notice'):
    session = armed_session(runner, execution)
    update_payload(execution, continuation={**session.continuation, 'approving_user_id': '123456789'},
        continuation_status={'phase': 'skipped', 'reason': 'explicit stop'})
    return session


def stored_payload(home, execution):
    with sqlite3.connect(home / 'state.db') as connection:
        return json.loads(connection.execute('SELECT payload FROM process_followups WHERE execution_id=?',
            (execution,)).fetchone()[0])


def notice_row(execution):
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as connection:
        return connection.execute('SELECT state FROM delivery_obligations WHERE obligation_id=?',
            ('process-continuation-stop:' + execution,)).fetchone()[0]


@pytest.fixture
def secondary(discord, home, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: home.parent)
    profile_home = home / 'profiles' / 'research'
    profile_home.mkdir(parents=True)
    (profile_home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    monkeypatch.setattr('hermes_cli.profiles.get_profile_dir', lambda name: profile_home)
    with followups._db():
        pass
    discord.config.multiplex_profiles = True
    discord.source = replace(discord.source, profile='research')
    discord.entry = discord.session_store.get_or_create_session(discord.source)
    assert discord.entry.session_key.startswith('agent:research:')
    return discord, profile_home


@pytest.mark.asyncio
async def test_f21_secondary_profile_stop_notice_is_delivered(secondary, home):
    runner, profile_home = secondary
    with _profile_runtime_scope(profile_home, hydrate_secrets=False):
        session = stopped_job(runner)
        await report_stops(runner)
    runner.transport.send.assert_awaited_once()
    assert notice_row(session.id) == 'delivered', 'secondary notice must not be abandoned'
    assert stored_payload(profile_home, session.id)['continuation_status']['notice_delivered'] == 1
    with sqlite3.connect(home / 'state.db') as connection:
        assert connection.execute('SELECT COUNT(*) FROM process_followups').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_f21_secondary_gate_uses_owning_profile(secondary, home):
    runner, profile_home = secondary
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
    with _profile_runtime_scope(profile_home, hydrate_secrets=False):
        session = stopped_job(runner)
    obligation = 'process-continuation-stop:' + session.id
    delivery_ledger.record_obligation(obligation_id=obligation, session_key=runner.entry.session_key,
        platform='discord', chat_id='4242', thread_id='topic', content='notice', turn_id=obligation)
    assert delivery_ledger.process_attention_authorized(obligation) is True


@pytest.mark.asyncio
async def test_f21_secondary_acknowledges_own_store(secondary):
    runner, profile_home = secondary
    with _profile_runtime_scope(profile_home, hydrate_secrets=False):
        session = stopped_job(runner)
    obligation = 'process-continuation-stop:' + session.id
    delivery_ledger.record_obligation(obligation_id=obligation, session_key=runner.entry.session_key,
        platform='discord', chat_id='4242', thread_id='topic', content='notice', turn_id=obligation)
    delivery_ledger.mark_delivered(obligation, message_id='sent')
    assert stored_payload(profile_home, session.id)['continuation_status'].get('notice_delivered') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['explicit stop', 'verification failed', 'chain cap reached', 'restart interrupted'])
async def test_f10_stop_notice_mentions_owner_without_health_mention(discord, reason):
    session = stopped_job(discord)
    update_payload(session.id, continuation_status={'phase': 'skipped', 'reason': reason})
    await report_stops(discord)
    discord.transport.send.assert_awaited_once()
    assert discord.transport.send.call_args.args[1].endswith('\n<@123456789>')


@pytest.mark.asyncio
async def test_d_single_profile_notice_unchanged(discord):
    session = stopped_job(discord)
    await report_stops(discord)
    await report_stops(discord)
    discord.transport.send.assert_awaited_once()
    assert notice_row(session.id) == 'delivered'
    assert '이어 가기 멈춤: proc_notice — explicit stop.' in discord.transport.send.call_args.args[1]


@pytest.mark.asyncio
async def test_d_gate_off_no_state_writes(home, monkeypatch, discord):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
    database = MagicMock(side_effect=AssertionError('gate-off path touched state DB'))
    monkeypatch.setattr(continuation, '_db', database)
    monkeypatch.setattr(delivery_ledger, '_connect', database)
    before = (home / 'state.db').read_bytes()
    await report_stops(discord)
    continuation.record_user_event(discord.entry.session_key, MessageEvent(text='hello', source=discord.source))
    assert continuation.record_stop(discord.entry.session_key)
    database.assert_not_called()
    discord.transport.send.assert_not_awaited()
    assert (home / 'state.db').read_bytes() == before


@pytest.mark.asyncio
async def test_d_health_mention_keeps_priority(discord, monkeypatch):
    monkeypatch.setenv('HERMES_HEALTH_MENTION', '<@987654321>')
    stopped_job(discord)
    await report_stops(discord)
    assert discord.transport.send.call_args.args[1] == (
        '이어 가기 멈춤: proc_notice — explicit stop. 이어서 하려면 아무 메시지나 보내 줘.\n<@987654321>')
    assert discord.transport.send.call_args.kwargs['metadata'] == {
        'thread_id': 'topic', '_interim_send': True, 'notify': True}


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['ordinary', 'slash', 'adapter'])
@pytest.mark.parametrize('pending', ['alone', 'queued', 'eventless'])
async def test_f23_replayed_steer_keeps_own_receipt_and_identity(home, entry, pending):
    runner, source, key, agent, adapter = busy_runner('steer', followup=False)
    prefix = '/steer ' if entry == 'slash' else ''
    first = MessageEvent(text=prefix + 'consumed', source=source, message_id='first',
        _process_user_received_at=100.0, _process_user_recorded=True)
    last = MessageEvent(text=prefix + 'remaining', source=source, message_id='last',
        _process_user_received_at=200.0, _process_user_recorded=True)
    for event in (first, last):
        if entry == 'slash':
            await runner._busy_steer_command(event, key, source)
        elif entry == 'adapter':
            outcome = await runner._resolve_busy_steer_or_redirect(event, key, 'steer', agent)
            assert outcome.steered
        else:
            await runner._hm_handle_running_session_message(event, source, key)
    leftover = agent.steer.call_args.args[0]
    if pending == 'queued':
        runner._enqueue_fifo(key, MessageEvent(text='notification', source=source, internal=True), adapter)
    result = {'final_response': 'done', 'pending_steer': leftover,
        'interrupted': pending == 'eventless', 'interrupt_message': 'other input'}
    replay, text = await runner._run_agent_drain_pending(result, adapter, source, key)
    if pending == 'eventless':
        replay, text = await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key)
    assert text == leftover
    assert replay.message_id == 'last', 'consumed steer must not donate its identity'
    assert replay._process_user_received_at == 200.0
    assert replay.timestamp == last.timestamp
    assert replay._process_user_recorded and not replay.internal


@pytest.mark.asyncio
@pytest.mark.parametrize('grouped', [False, True])
async def test_f23_pending_suffix_uses_first_unconsumed_receipt(home, grouped):
    runner, source, key, agent, adapter = busy_runner('steer', followup=False)
    submitted = []
    for identity, receipt in [('consumed', 100.0), ('remaining', 200.0), ('latest', 300.0)]:
        event = MessageEvent(text='identical', source=source, message_id=identity,
            _process_user_received_at=receipt, _process_user_recorded=True)
        await runner._hm_handle_running_session_message(event, source, key)
        submitted.append(agent.steer.call_args.args[0])
    leftover = '\n'.join(submitted[-2:]) if grouped else submitted[-1]
    replay, text = await runner._run_agent_drain_pending(
        {'final_response': 'done', 'pending_steer': leftover}, adapter, source, key)
    assert text == leftover
    assert replay.message_id == ('remaining' if grouped else 'latest')
    assert replay._process_user_received_at == (200.0 if grouped else 300.0)


@pytest.mark.asyncio
async def test_d_single_leftover_steer_unchanged(home):
    runner, source, key, agent, adapter = busy_runner('steer', followup=False)
    event = MessageEvent(text='/steer unchanged', source=source, message_id='only',
        _process_user_received_at=123.0, _process_user_recorded=True)
    await runner._busy_steer_command(event, key, source)
    replay, text = await runner._run_agent_drain_pending(
        {'final_response': 'done', 'pending_steer': 'unchanged'}, adapter, source, key)
    assert text == replay.text == 'unchanged'
    assert replay.message_id == event.message_id
    assert replay._process_user_received_at == 123.0
    assert replay.source is event.source and not replay.internal
    assert await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [False, True])
async def test_f10_stop_write_warning_mentions_owner(discord, monkeypatch, direct):
    from gateway.process_continuations import _warn_stop_failure
    session = stopped_job(discord)
    payload = stored_payload(Path(continuation.get_hermes_home()), session.id)
    if direct:
        monkeypatch.setattr(continuation, 'stop_write_failures', MagicMock(side_effect=OSError('unavailable')))
    else:
        monkeypatch.setattr(continuation, 'stop_write_failures', lambda *args: [(session.id, payload)])
    await _warn_stop_failure(discord, discord.entry.session_key, 100.0, discord.source)
    discord.transport.send.assert_awaited_once()
    assert discord.transport.send.call_args.args[1].endswith('\n<@123456789>')


def test_f10_authority_records_and_inherits_approver(discord):
    event = MessageEvent(text='approved', source=discord.source, _process_user_received_at=100.0)
    with continuation.turn_scope(event, discord.entry.session_key):
        authority = dict(continuation._turn.get())
    assert authority.get('approving_user_id') == '123456789'
    event = replace(event, source=replace(discord.source, user_id='other'), internal=True)
    with continuation.turn_scope(event, discord.entry.session_key, authority):
        assert continuation._turn.get()['approving_user_id'] == '123456789'


@pytest.mark.asyncio
async def test_f10_followup_reconciliation_mentions_approver(discord, home):
    from gateway.process_followups import _report_attention
    session = stopped_job(discord)
    with followups._db() as connection:
        connection.execute("UPDATE process_followups SET phase='needs_reconciliation' WHERE execution_id=?", (session.id,))
        row = dict(connection.execute('SELECT * FROM process_followups WHERE execution_id=?', (session.id,)).fetchone())
    await _report_attention(discord, discord.transport, discord.source, row, stored_payload(home, session.id))
    discord.transport.send.assert_awaited_once()
    assert discord.transport.send.call_args.args[1].endswith('\n<@123456789>')


@pytest.mark.asyncio
@pytest.mark.parametrize('late', [False, True])
async def test_f21_secondary_attention_retry_uses_own_store(secondary, monkeypatch, late):
    from gateway.run import GatewayRunner
    from tests.waiting import async_wait_until
    runner, profile_home = secondary
    with _profile_runtime_scope(profile_home, hydrate_secrets=False):
        session = stopped_job(runner)
        with followups._db() as connection:
            connection.execute("UPDATE process_followups SET phase='needs_reconciliation' WHERE execution_id=?", (session.id,))
            token = connection.execute('SELECT token FROM process_followups WHERE execution_id=?', (session.id,)).fetchone()[0]
    obligation = 'process-attention:' + session.id + ':' + token
    delivery_ledger.record_obligation(obligation_id=obligation, session_key=runner.entry.session_key,
        platform='discord', chat_id='4242', thread_id=None, content='attention', turn_id=obligation)
    assert delivery_ledger.claim_pending_obligation(obligation)
    release = asyncio.Event()
    async def send(*args, **kwargs):
        if late:
            await release.wait()
        return SimpleNamespace(success=True)
    runner.transport.send = AsyncMock(side_effect=send)
    runner._obligation_adapter = AsyncMock(return_value=runner.transport)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    monkeypatch.setattr('agent.deadline.resolve_timeout', lambda *args, **kwargs: .001)
    try:
        await GatewayRunner._redeliver_claimed_obligations(runner, [dict(obligation_id=obligation,
            session_key=runner.entry.session_key, platform='discord', chat_id='4242', thread_id=None,
            content='attention', attempts=1)])
        runner.transport.send.assert_awaited_once()
    finally:
        release.set()
    if late:
        from gateway.process_followups import attention_inflight
        with _profile_runtime_scope(profile_home, hydrate_secrets=False):
            assert await async_wait_until(lambda: not attention_inflight(obligation), timeout=5)
    assert delivery_ledger.turn_delivery_state(runner.entry.session_key, obligation) == 'delivered'
    with _profile_runtime_scope(profile_home, hydrate_secrets=False):
        with followups._db(read_only=True) as connection:
            phase = connection.execute('SELECT phase FROM process_followups WHERE execution_id=?', (session.id,)).fetchone()[0]
    assert phase == 'needs_reconciliation_reported'
