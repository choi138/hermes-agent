"""Continuity v2: approved work survives conversation, but never an explicit stop."""

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageEvent
from gateway.process_followups import reconcile
from tests.gateway.test_process_followup_continuity import (
    CompletionRunner, armed_session, busy_runner, continuation_status, home as _home_fixture,
    run_continuation, update_payload, verify,
)
from tools import process_registry_continuation as continuation
from tools import process_registry_followups as ledger

home = _home_fixture


@pytest.fixture
def enabled(home):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    runner = CompletionRunner(home)
    runner.transport.send = AsyncMock(return_value=SimpleNamespace(success=True))
    from gateway.platforms.base import BasePlatformAdapter
    runner.transport.extract_media = BasePlatformAdapter.extract_media
    runner._delivery_adapter_for = lambda source: runner.transport
    runner._thread_metadata_for_target = lambda *args, **kwargs: {'thread_id': 'topic'}
    return runner


def notices(runner):
    return [call for call in runner.transport.send.call_args_list
            if '이어 가기 멈춤:' in str(call)]


def continuations(runner):
    return [event for event in runner.transport.events if event.metadata.get('process_continuation')]


async def reconcile_without_escaping(runner):
    failure = None
    try:
        await reconcile(runner)
    except (sqlite3.Error, RuntimeError) as error:
        failure = str(error)
    assert failure is None, f'reconciliation must retain and report failed work, not escape: {failure}'


@pytest.mark.asyncio
async def test_g1_ordinary_input_during_process_and_verification_runs_after_user_turn(enabled):
    runner = enabled
    session = armed_session(runner)
    busy, source, busy_key, _agent, adapter = busy_runner('steer')
    runner.source = source
    runner.entry = runner.session_store.get_or_create_session(source)
    update_payload(session.id, session_key=busy_key)
    continuation.record_user_event(busy_key, MessageEvent(text='실행 중이야?', source=source))
    later = MessageEvent(text='추가 정보야', source=source)
    loop = asyncio.get_running_loop()
    def during_verification():
        asyncio.run_coroutine_threadsafe(
            busy._hm_handle_running_session_message(later, source, busy_key), loop).result(5)
    await asyncio.to_thread(lambda: asyncio.run(verify(runner, during_verification)))
    async def enqueue(event):
        runner.transport.events.append(event)
        busy._enqueue_fifo(busy_key, event, adapter)
    runner.transport.handle_message = enqueue
    await reconcile(runner)
    drained, text = await busy._run_agent_drain_pending({'final_response': 'verified'}, adapter, source, busy_key)
    order = []
    assert drained is later and text == later.text
    order.append('user')
    events = continuations(runner)
    assert len(events) == 1, 'ordinary user messages must not cancel approved work'
    next_event, _ = await busy._run_agent_drain_pending({'final_response': 'user answered'}, adapter, source, busy_key)
    assert next_event is events[0], 'the real FIFO must drain the user before internal continuation'
    model = MagicMock(side_effect=lambda *args, **kwargs: (
        order.append('continuation') or {'final_response': 'done', 'messages': []}))
    run_continuation(runner, events[0], model)
    run_continuation(runner, events[0], model)
    await reconcile(runner)
    assert order == ['user', 'continuation']
    assert continuation_status(session.id)['phase'] == 'done'
    assert not notices(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before_completion', 'during_verification'])
async def test_g2_explicit_stop_cancels_and_reports_once(enabled, timing):
    runner = enabled
    session = armed_session(runner)
    if timing == 'during_verification':
        await asyncio.to_thread(lambda: asyncio.run(verify(runner,
            lambda: continuation.record_stop(runner.entry.session_key))))
    else:
        await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    if timing == 'before_completion':
        await verify(runner)
    await reconcile(runner)
    await reconcile(runner)
    assert not continuations(runner)
    assert continuation_status(session.id)['phase'] == 'skipped'
    assert len(notices(runner)) == 1, 'stopped approved work needs a durable user notice'


@pytest.mark.asyncio
async def test_g2_locked_state_db_stop_survives_restart(enabled, monkeypatch, home):
    runner = enabled
    armed_session(runner)
    await verify(runner)
    lock = sqlite3.connect(home / 'state.db')
    lock.execute('BEGIN IMMEDIATE')
    try:
        await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    finally:
        lock.rollback()
        lock.close()
    for name in ('_received_fences', '_failed_fences', '_pending_fences', '_live_authorities', '_stops'):
        if hasattr(continuation, name):
            monkeypatch.setattr(continuation, name, {})
    restarted = CompletionRunner(home)
    restarted.transport = runner.transport
    restarted._delivery_adapter_for = runner._delivery_adapter_for
    restarted._thread_metadata_for_target = runner._thread_metadata_for_target
    await reconcile(restarted)
    await reconcile(restarted)
    assert not continuations(restarted), 'a failed SQLite stop fence must survive memory loss'
    assert len(notices(restarted)) == 1


@pytest.mark.asyncio
async def test_g3_default_allows_fourth_step_and_stops_at_ten(enabled):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    update_payload(session.id, continuation={**session.continuation, 'chain': 3})
    await reconcile(runner)
    assert len(continuations(runner)) == 1, 'default cap must allow step four'
    assert continuation.settings()[1] == 10


def require_reconciliation():
    with ledger._db() as database:
        database.execute("UPDATE process_followups SET phase='needs_reconciliation'")


def abandon_report(runner, execution):
    import json
    from gateway.delivery_ledger import abandon_obligation, compute_obligation_id
    with ledger._db(read_only=True) as database:
        row = database.execute('SELECT payload FROM process_followups WHERE execution_id=?', (execution,)).fetchone()
    payload = json.loads(row['payload'])
    abandon_obligation('process-report:' + compute_obligation_id(runner.entry.session_key,
        payload['followup_turn_id'], 'process_verification_report'))


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['stop', 'age', 'cap', 'reconciliation', 'cancel', 'admission', 'crash',
    'report_reconciliation', 'fence_failure', 'completion_failure', 'admission_exception', 'turn_interrupted'])
async def test_g4_each_stop_reason_reports_exactly_once(enabled, reason, monkeypatch):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    mutations = {
        'stop': lambda: continuation.record_stop(runner.entry.session_key),
        'age': lambda: update_payload(session.id, timings={'completion_observed_at': time.time() - 3601}),
        'cap': lambda: update_payload(session.id, continuation={**session.continuation, 'chain': 10}),
        'reconciliation': require_reconciliation,
        'cancel': lambda: ledger.cancel(session.id),
        'admission': lambda: monkeypatch.setattr(runner, '_inject_watch_notification', AsyncMock(return_value=None)),
        'admission_exception': lambda: monkeypatch.setattr(runner, '_inject_watch_notification',
            AsyncMock(side_effect=RuntimeError('adapter failed'))),
        'report_reconciliation': lambda: abandon_report(runner, session.id),
    }
    if reason in mutations:
        await asyncio.to_thread(mutations[reason])
    elif reason == 'fence_failure':
        with monkeypatch.context() as failure:
            failure.setattr(continuation, 'claim', MagicMock(side_effect=sqlite3.OperationalError('locked fence')))
            await reconcile_without_escaping(runner)
    elif reason in {'completion_failure', 'turn_interrupted'}:
        await reconcile(runner)
        event = continuations(runner)[0]
        with monkeypatch.context() as failure:
            if reason == 'completion_failure':
                failure.setattr(continuation, 'finish', MagicMock(side_effect=sqlite3.OperationalError('locked completion')))
            run_continuation(runner, event, lambda *args, **kwargs: {
                'final_response': 'interrupted', 'messages': [], 'interrupted': reason == 'turn_interrupted'})
    else:
        await reconcile(runner)
        event = continuations(runner)[0]
        with pytest.raises(RuntimeError):
            run_continuation(runner, event, MagicMock(side_effect=RuntimeError('model crashed')))
    await reconcile_without_escaping(runner)
    await reconcile_without_escaping(runner)
    assert len(notices(runner)) == 1, f'{reason} must produce exactly one notice'
    call = notices(runner)[0]
    assert call.args[0] == '4242'
    assert call.args[1].startswith(f'이어 가기 멈춤: {session.id} — ')
    assert call.args[1].endswith('이어서 하려면 아무 메시지나 보내 줘.')
    assert call.kwargs['metadata']['thread_id'] == 'topic'


@pytest.mark.asyncio
async def test_g2_stop_retires_queued_and_unfinished_siblings(enabled):
    runner = enabled
    first = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    queued = continuations(runner)[0]
    second = armed_session(runner, 'proc_sibling')
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    await reconcile(runner)
    model = MagicMock()
    run_continuation(runner, queued, model)
    model.assert_not_called()
    assert continuation_status(first.id)['phase'] == 'skipped'
    sibling_status = continuation_status(second.id)
    assert sibling_status is not None, 'stop must settle unfinished sibling authority before verification ends'
    assert sibling_status['phase'] == 'skipped'
    assert len(notices(runner)) == 2, 'stop must report every queued or unfinished authorized execution'


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['previous-process', None])
async def test_g2_restart_keeps_healthy_authority(enabled, home, owner):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    update_payload(session.id, continuation={**session.continuation, 'owner': owner})
    restarted = CompletionRunner(home)
    await reconcile(restarted)
    await reconcile(restarted)
    events = continuations(restarted)
    assert len(events) == 1, 'approved authority must survive a gateway process change'
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    run_continuation(restarted, events[0], model)
    run_continuation(restarted, events[0], model)
    model.assert_called_once()
    assert continuation_status(session.id)['phase'] == 'done'
    assert not notices(restarted)


def other_session(home):
    runner = CompletionRunner(home)
    runner.source = replace(runner.source, chat_id='other-chat')
    runner.entry = runner.session_store.get_or_create_session(runner.source)
    return runner


@pytest.mark.asyncio
async def test_g2_restart_honors_persisted_stop(enabled, monkeypatch, home):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    other = other_session(home)
    healthy = armed_session(other, 'proc_healthy')
    await verify(other)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    monkeypatch.setattr(continuation, '_stops', {})
    for process in (session, healthy):
        update_payload(process.id, continuation={**process.continuation, 'owner': 'previous-process'})
    restarted = CompletionRunner(home)
    await reconcile(restarted)
    await reconcile(restarted)
    assert continuation_status(session.id)['phase'] == 'skipped'
    assert 'session stopped' in continuation_status(session.id)['reason']
    assert len(notices(restarted)) == 1
    assert session.id in notices(restarted)[0].args[1]
    events = continuations(restarted)
    assert len(events) == 1, 'a persisted stop must not quarantine another session after restart'
    assert events[0].metadata['process_continuation']['execution_id'] == healthy.id
    model = MagicMock(return_value={'final_response': 'healthy', 'messages': []})
    run_continuation(other, events[0], model)
    model.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['returns_failure', 'raises'])
async def test_g2_stop_persist_failure_notifies_and_retries(enabled, monkeypatch, home, failure):
    from gateway.run import GatewayRunner
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    other = other_session(home)
    healthy = armed_session(other, 'proc_healthy')
    await verify(other)
    runner._interrupt_and_clear_session = AsyncMock()
    monkeypatch.setenv('HERMES_HEALTH_MENTION', '<@12345>')
    original = continuation.record_stop
    retry_release, retried = threading.Event(), threading.Event()
    attempts = []
    def write_stop(key, received_at=None):
        attempts.append((key, received_at))
        if len(attempts) == 1:
            if failure == 'raises':
                raise OSError('read-only stop journal')
            return False
        assert retry_release.wait(20)
        result = original(key, received_at)
        retried.set()
        return result
    monkeypatch.setattr(continuation, 'record_stop', write_stop)
    try:
        escaped = None
        try:
            await GatewayRunner._busy_stop_command(runner,
                MessageEvent(text='/stop', source=runner.source), runner.entry.session_key, runner.source)
        except OSError as error:
            escaped = error
        assert escaped is None, 'stop persistence errors must notify and retry, not escape /stop'
        from tests.waiting import async_wait_until
        assert await async_wait_until(lambda: runner.transport.send.await_count == 1, timeout=5)
        warnings = [call for call in runner.transport.send.call_args_list if '멈춤 기록 저장 실패:' in str(call)]
        assert len(warnings) == 1, 'a failed stop write must immediately warn the original chat'
        assert warnings[0].args[0] == '4242'
        assert warnings[0].args[1] == (f'멈춤 기록 저장 실패: {session.id} — 지금 프로세스에서는 멈춰 있지만, '
            '게이트웨이가 재시작되면 다시 이어질 수 있어. 확실히 멈추려면 /stop 을 한 번 더 보내 줘.\n<@12345>')
        assert warnings[0].kwargs['metadata'] == {'thread_id': 'topic', '_interim_send': True, 'notify': True}
        runner._interrupt_and_clear_session.assert_awaited_once()
        await reconcile(runner)
        assert continuation_status(session.id)['phase'] == 'skipped'
        assert [event.metadata['process_continuation']['execution_id'] for event in continuations(runner)] == [healthy.id]
        run_continuation(other, continuations(runner)[0], lambda *args, **kwargs: {'final_response': 'healthy'})
        assert continuation_status(healthy.id)['phase'] == 'done'
        retry_release.set()
        assert await asyncio.to_thread(retried.wait, 5), 'the stop journal must be retried once in the background'
        await asyncio.gather(*getattr(runner, '_background_tasks', set()))
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        monkeypatch.setattr(continuation, '_stops', {})
        assert continuation.stopped_after(runner.entry.session_key, session.continuation['origin_received_at'])
        update_payload(session.id, continuation_status={})
        restarted = CompletionRunner(home)
        await reconcile(restarted)
        assert continuation_status(session.id)['phase'] == 'skipped'
        assert not continuations(restarted), 'the retried receipt must fence execution after memory loss'
        assert len([call for call in runner.transport.send.call_args_list if '멈춤 기록 저장 실패:' in str(call)]) == 1
    finally:
        retry_release.set()
        await asyncio.gather(*getattr(runner, '_background_tasks', set()))


@pytest.mark.asyncio
async def test_g3_tenth_runs_eleventh_reports_cap(enabled):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    update_payload(session.id, continuation={**session.continuation, 'chain': 9})
    await reconcile(runner)
    assert len(continuations(runner)) == 1, 'the tenth continuation must be allowed'
    run_continuation(runner, continuations(runner)[0], lambda *args, **kwargs: {'final_response': 'done'})
    sibling = armed_session(runner, 'proc_eleventh')
    update_payload(sibling.id, continuation={**sibling.continuation, 'chain': 10})
    await verify(runner)
    await reconcile(runner)
    assert len(continuations(runner)) == 1
    assert len(notices(runner)) == 1


@pytest.mark.asyncio
async def test_g4_successful_notice_is_not_repeated_after_restart(enabled, home):
    runner = enabled
    armed_session(runner)
    await verify(runner)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    await reconcile(runner)
    restarted = CompletionRunner(home)
    restarted.transport = runner.transport
    await reconcile(restarted)
    await reconcile(restarted)
    assert len(notices(restarted)) == 1, 'successful notices must have a durable per-execution delivery fence'


@pytest.mark.asyncio
async def test_g4_notice_tombstone_outlives_delivery_ledger_retention(enabled, home):
    from gateway import delivery_ledger as delivery
    runner = enabled
    session = armed_session(runner)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    await reconcile(runner)
    with delivery._connect() as database:
        database.execute("UPDATE delivery_obligations SET updated_at=0 WHERE state='delivered'")
        delivery._prune_unlocked(database, time.time())
    restarted = CompletionRunner(home)
    restarted.transport = runner.transport
    await reconcile(restarted)
    await reconcile(restarted)
    assert len(notices(restarted)) == 1, 'ledger retention must not resurrect a delivered stop notice'
    assert continuation_status(session.id)['notice_delivered'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('interruption', ['pending', 'rejected', 'exception', 'before_send'])
async def test_g4_notice_recovery_uses_existing_delivery_ledger(enabled, monkeypatch, interruption):
    from gateway import delivery_ledger as delivery
    from gateway.run import GatewayRunner
    runner = enabled
    session = armed_session(runner)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    with monkeypatch.context() as interrupted:
        if interruption == 'pending':
            interrupted.setattr(delivery, 'claim_pending_obligation', lambda *args: False)
        elif interruption == 'before_send':
            interrupted.setattr('gateway.process_continuations._send_stop_notice', AsyncMock(), raising=False)
        elif interruption == 'exception':
            runner.transport.send.side_effect = RuntimeError('connection lost before send')
        else:
            runner.transport.send.return_value = SimpleNamespace(success=False, error='503 unavailable')
        await reconcile(runner)
    runner.transport.send.reset_mock(side_effect=True)
    runner.transport.send.return_value = SimpleNamespace(success=True)
    with delivery._connect() as database:
        database.execute("UPDATE delivery_obligations SET owner_pid=999999999,owner_started_at=1")
    restarted = object.__new__(GatewayRunner)
    restarted._obligation_adapter = AsyncMock(return_value=runner.transport)
    restarted._arm_flood_timers_for_waiting_rows = AsyncMock()
    claimed = await asyncio.to_thread(delivery.sweep_recoverable)
    assert len(claimed) == 1, 'a notice interrupted before delivery must survive restart'
    assert await restarted._redeliver_claimed_obligations(claimed) == 1
    await reconcile(runner)
    assert runner.transport.send.await_count == 1
    sent = runner.transport.send.call_args.kwargs
    assert f'이어 가기 멈춤: {session.id} — ' in sent['content']
    assert sent['chat_id'] == '4242' and sent['metadata']['thread_id'] == 'topic'
    assert sent['metadata']['notify'] is True


@pytest.mark.asyncio
async def test_g6_gate_off_suppresses_previously_recorded_notice_recovery(enabled, monkeypatch, home):
    from gateway import delivery_ledger as delivery
    runner = enabled
    armed_session(runner)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    with monkeypatch.context() as pending:
        pending.setattr(delivery, 'claim_pending_obligation', lambda *args: False)
        await reconcile(runner)
    with delivery._connect() as database:
        database.execute("UPDATE delivery_obligations SET owner_pid=999999999,owner_started_at=1")
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
    assert await asyncio.to_thread(delivery.sweep_recoverable) == [], 'gate-off recovery must not send old notices'
    assert not notices(runner)
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
    assert len(await asyncio.to_thread(delivery.sweep_recoverable)) == 1


@pytest.mark.asyncio
async def test_g4_routing_failure_does_not_silence_siblings(enabled, monkeypatch):
    runner = enabled
    first = armed_session(runner)
    second = armed_session(runner, 'proc_sibling')
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    with monkeypatch.context() as failure:
        routes = iter([RuntimeError('route unavailable')])
        def route(payload):
            result = next(routes, runner.source)
            if isinstance(result, Exception):
                raise result
            return result
        failure.setattr(runner, '_build_process_event_source', route)
        await reconcile_without_escaping(runner)
    await reconcile(runner)
    await reconcile(runner)
    assert len(notices(runner)) == 2, 'one broken route must not suppress another execution notice'
    assert sum(first.id in call.args[1] for call in notices(runner)) == 1
    assert sum(second.id in call.args[1] for call in notices(runner)) == 1


@pytest.mark.asyncio
async def test_g4_refused_cap_arming_reports_and_mentions_owner(enabled, monkeypatch, home):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.process_registry import ProcessRegistry
    runner = enabled
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n  process_continue_max_chain: 0\n')
    monkeypatch.setenv('HERMES_HEALTH_MENTION', '<@12345>')
    event = MessageEvent(text='approved work', source=runner.source)
    tokens = set_session_vars(platform='telegram', session_key=runner.entry.session_key,
                              session_id=runner.entry.session_id)
    try:
        with continuation.turn_scope(event, runner.entry.session_key), continuation.launch_scope(True):
            session = ProcessRegistry._new_session('true', 'task', 'task', runner.entry.session_key, str(home))
        assert not session.continue_on_complete
        await reconcile(runner)
        await reconcile(runner)
        assert len(notices(runner)) == 1, 'refusing arming at the cap must notify without waiting for completion'
        assert '<@12345>' in notices(runner)[0].args[1]
    finally:
        clear_session_vars(tokens)


@asynccontextmanager
async def interrupted_turn(runner, event, execution):
    entered, release = threading.Event(), threading.Event()
    def model(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        return {'final_response': 'late answer', 'messages': []}
    worker = asyncio.create_task(asyncio.to_thread(run_continuation, runner, event, model))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        status = continuation_status(execution)
        assert status['phase'] == 'running'
        update_payload(execution, continuation_status={**status, 'owner': json.dumps([999999999, 0])})
        yield
    finally:
        release.set()
        await worker


def restart_runner(home, boot):
    from tests.gateway.restart_test_helpers import make_restart_runner
    restarted, _adapter = make_restart_runner()
    delivery = CompletionRunner(home)
    restarted._boot_id = boot
    restarted.session_store = delivery.session_store
    restarted.entry, restarted.source, restarted.transport = delivery.entry, delivery.source, delivery.transport
    for name in ('_inject_watch_notification', '_build_process_event_source', '_delivery_adapter_for',
                 '_thread_metadata_for_target', '_classify_completion_target'):
        setattr(restarted, name, getattr(delivery, name))
    return restarted


async def startup_continuation(runner):
    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.gather(*runner._background_tasks)
    return scheduled


@pytest.mark.asyncio
async def test_g5_running_restart_resumes_once(enabled, home):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    original = continuations(runner)[0]
    async with interrupted_turn(runner, original, session.id):
        restarted = restart_runner(home, 'second-boot')
        assert restarted.entry.active_turn is not None
        await startup_continuation(restarted)
        await reconcile(restarted)
        events = continuations(restarted)
        assert len(events) == 1, 'startup must resume the interrupted continuation exactly once'
        assert events[0].metadata['process_continuation']['execution_id'] == session.id
        assert events[0].metadata['process_continuation']['token'] != original.metadata['process_continuation']['token']
        assert events[0].internal
        model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
        run_continuation(restarted, original, model)
        model.assert_not_called()
        run_continuation(restarted, events[0], model)
        run_continuation(restarted, events[0], model)
        model.assert_called_once()
        message = model.call_args.args[0]
        assert 'previous continuation turn was interrupted by a gateway restart' in message
        assert 'inspect current state' in message and 'Do not blindly redo side effects' in message
        assert continuation_status(session.id)['phase'] == 'done'
        assert not notices(restarted)
    assert continuation_status(session.id)['phase'] == 'done', 'late completion must not change the resumed token'


@pytest.mark.asyncio
async def test_g5_repeated_restart_stops_with_notice(enabled, home):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    async with interrupted_turn(runner, continuations(runner)[0], session.id):
        restarted = restart_runner(home, 'second-boot')
        await startup_continuation(restarted)
        events = continuations(restarted)
        assert len(events) == 1, 'the first restart must resume before testing a repeated restart'
        async with interrupted_turn(restarted, events[0], session.id):
            repeated = restart_runner(home, 'third-boot')
            await startup_continuation(repeated)
            await reconcile(repeated)
            await reconcile(repeated)
            assert not continuations(repeated), 'a second interrupted execution must not produce a third run'
            assert len(notices(repeated)) == 1
            assert '게이트웨이 재시작이 반복돼 자동 재개를 멈췄어' in notices(repeated)[0].args[1]
            assert continuation_status(session.id)['phase'] == 'uncertain'
            assert repeated.entry.active_turn is None
    assert continuation_status(session.id)['phase'] == 'uncertain'


@pytest.mark.asyncio
async def test_g5_queued_resume_reclaims_without_spending_another_chain_step(enabled, home):
    runner = enabled
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n  process_continue_max_chain: 1\n')
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    async with interrupted_turn(runner, continuations(runner)[0], session.id):
        restarted = restart_runner(home, 'second-boot')
        await startup_continuation(restarted)
        queued = continuations(restarted)
        assert len(queued) == 1, 'resuming the first step must not exhaust a cap of one'
        status = continuation_status(session.id)
        assert status['phase'] == 'queued' and status['resume_count'] == 1
        sibling = armed_session(runner, 'proc_sibling')
        await verify(runner)
        update_payload(session.id, continuation_status={**status, 'owner': json.dumps([999999999, 0])})
        repeated = restart_runner(home, 'third-boot')
        await reconcile(repeated)
        events = continuations(repeated)
        assert len(events) == 1, 'a resume lost before model dispatch must still be reclaimable'
        assert continuation_status(sibling.id)['phase'] == 'skipped', 'the queued resume still reserves its chain step'
        assert 'chain cap' in continuation_status(sibling.id)['reason']
        model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
        for event in (continuations(runner)[0], queued[0], events[0], events[0]):
            run_continuation(repeated, event, model)
        model.assert_called_once()
        assert continuation_status(session.id)['phase'] == 'done'
        assert continuation_status(session.id)['resume_count'] == 1
        assert len(notices(repeated)) == 1 and sibling.id in notices(repeated)[0].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_reason', ['interrupted_by_system(gateway_shutdown)',
    'interrupted_during_api_call(gateway_shutdown)'])
async def test_g5_cooperative_gateway_shutdown_keeps_running_claim(enabled, home, exit_reason):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    event = continuations(runner)[0]
    def shutdown(*args, **kwargs):
        runner.session_store.mark_active_turn_interrupted(runner.entry.session_key, 'restart_timeout')
        return {'final_response': '', 'messages': [], 'interrupted': True, 'turn_exit_reason': exit_reason}
    run_continuation(runner, event, shutdown)
    status = continuation_status(session.id)
    assert status['phase'] == 'running', 'a deploy shutdown must leave the execution available for recovery'
    update_payload(session.id, continuation_status={**status, 'owner': json.dumps([999999999, 0])})
    restarted = restart_runner(home, 'second-boot')
    await startup_continuation(restarted)
    events = continuations(restarted)
    assert len(events) == 1
    model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
    run_continuation(restarted, events[0], model)
    run_continuation(restarted, events[0], model)
    model.assert_called_once()
    assert 'inspect current state' in model.call_args.args[0]
    assert continuation_status(session.id)['phase'] == 'done'
    assert not notices(restarted)


@pytest.mark.parametrize('enabled_profile', ['research', 'main'])
def test_g2_stop_write_redelivery_reads_owning_profile_gate(home, monkeypatch, enabled_profile):
    from pathlib import Path
    from gateway import delivery_ledger as delivery
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setattr(Path, 'home', lambda: home)
    research = get_profile_dir('research')
    research.mkdir(parents=True)
    for name, directory in [('main', home), ('research', research)]:
        gate = str(name == enabled_profile).lower()
        (directory / 'config.yaml').write_text(f'agent:\n  process_continue_on_complete: {gate}\n')
        delivery.record_obligation(obligation_id=f'process-continuation-stop-write:proc_{name}:1',
            session_key=f'agent:{name}:telegram:dm:4242', platform='telegram', chat_id='4242',
            thread_id='topic', content='멈춤 기록 저장 실패')
    token = set_hermes_home_override(research if enabled_profile == 'main' else home)
    try:
        for name in ('main', 'research'):
            allowed = delivery.process_attention_authorized(f'process-continuation-stop-write:proc_{name}:1')
            assert allowed is (True if name == enabled_profile else None)
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_g5_continuation_uses_own_limit_when_user_restart_guard_trips(enabled, home, monkeypatch):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    async with interrupted_turn(runner, continuations(runner)[0], session.id):
        user = other_session(home)
        assert user.session_store.begin_active_turn(user.entry.session_key, 'user-turn', 'old-user-boot')
        restarted = restart_runner(home, 'second-boot')
        guard = MagicMock(return_value=True)
        monkeypatch.setattr('gateway.restart_loop_guard.check_and_record', guard)
        assert await startup_continuation(restarted) == 1
        guard.assert_called_once()
        assert len(continuations(restarted)) == 1
        model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
        run_continuation(restarted, continuations(restarted)[0], model)
        model.assert_called_once()
        assert not notices(restarted)


@pytest.mark.asyncio
async def test_g6_disabled_startup_retains_interrupted_continuation_without_state_access(enabled, home, monkeypatch):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    await reconcile(runner)
    async with interrupted_turn(runner, continuations(runner)[0], session.id):
        (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
        restarted = restart_runner(home, 'second-boot')
        with monkeypatch.context() as disabled:
            database = MagicMock(side_effect=AssertionError('disabled startup must not open continuation state'))
            disabled.setattr(continuation, '_db', database)
            await startup_continuation(restarted)
            database.assert_not_called()
        assert restarted.entry.active_turn is not None
        assert not continuations(restarted) and not notices(restarted)
        (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n')
        await startup_continuation(restarted)
        assert len(continuations(restarted)) == 1


@pytest.mark.asyncio
async def test_g6_disabled_gate_never_touches_state_even_with_prior_authority(enabled, home, monkeypatch):
    runner = enabled
    armed_session(runner)
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: false\n')
    database = MagicMock(side_effect=AssertionError('gate-off ingress must not open state.db'))
    monkeypatch.setattr(continuation, '_db', database)
    event = MessageEvent(text='hello', source=runner.source)
    await asyncio.to_thread(continuation.record_user_event, runner.entry.session_key, event)
    await asyncio.to_thread(continuation.record_stop, runner.entry.session_key)
    database.assert_not_called()
    assert event._process_user_received_at > 0
    assert not notices(runner)


@pytest.mark.asyncio
async def test_g7_receipt_replay_does_not_revoke_authority(enabled):
    runner = enabled
    armed_session(runner)
    event = MessageEvent(text='/queue more info', source=runner.source)
    continuation.record_user_event(runner.entry.session_key, event)
    received = event._process_user_received_at
    continuation.record_user_event(runner.entry.session_key, event)
    assert event._process_user_received_at == received
    await verify(runner)
    await reconcile(runner)
    assert len(continuations(runner)) == 1, 'receipt preservation must not restore a user-message fence'


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_count', [0, 1])
async def test_c1_adapter_unavailable_retries_until_connected(enabled, monkeypatch, caplog, resume_count):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    update_payload(session.id, continuation_status={'resume_count': resume_count, 'interrupted_at': time.time()})
    caplog.set_level('INFO', logger='gateway.run')
    resolve = runner._resolve_injection_adapter
    clock = [0]
    monkeypatch.setattr('gateway.process_continuations.time', SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    monkeypatch.setattr(runner, '_resolve_injection_adapter', lambda *args: None)
    for _attempt in range(3):
        await reconcile(runner)
    assert continuation_status(session.id)['phase'] == 'ready', 'adapter outage must release, not retire, the claim'
    assert continuation_status(session.id)['resume_count'] == resume_count
    assert not continuations(runner) and not notices(runner)
    warnings = [record for record in caplog.records if record.levelname == 'WARNING' and session.id in record.message]
    assert len(warnings) <= 1, 'the two-second retry loop must not spam warnings'
    clock[0] = 299
    await reconcile(runner)
    assert len([record for record in caplog.records if record.levelname == 'WARNING' and session.id in record.message]) == 1
    clock[0] = 300
    await reconcile(runner)
    assert len([record for record in caplog.records if record.levelname == 'WARNING' and session.id in record.message]) == 2
    monkeypatch.setattr(runner, '_resolve_injection_adapter', resolve)
    with monkeypatch.context() as reconnecting:
        reconnecting.setattr('gateway.wake.admit_internal_event', AsyncMock(side_effect=RuntimeError('adapter reconnecting')))
        for _attempt in range(3):
            await reconcile(runner)
    with monkeypatch.context() as reconnecting:
        reconnecting.setattr('gateway.wake.adapter_supports_push', lambda adapter: False)
        reconnecting.setattr(runner, '_served_api_server_wake_profile', MagicMock(return_value=None))
        reconnecting.setattr('gateway.wake.deliver_wake', AsyncMock(side_effect=RuntimeError('API reconnecting')))
        for _attempt in range(3):
            await reconcile(runner)
        reconnecting.setattr(runner, '_served_api_server_wake_profile', MagicMock(side_effect=LookupError('route reconnecting')))
        for _attempt in range(3):
            await reconcile(runner)
    assert continuation_status(session.id)['phase'] == 'ready'
    assert not any(record.name == 'gateway.run' and ('Watch pattern notification' in record.message
        or 'Watch notification' in record.message) for record in caplog.records)
    assert len([record for record in caplog.records if record.levelname == 'WARNING' and session.id in record.message]) == 2
    await reconcile(runner)
    await reconcile(runner)
    events = continuations(runner)
    assert len(events) == 1
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    run_continuation(runner, events[0], model)
    run_continuation(runner, events[0], model)
    model.assert_called_once()
    assert continuation_status(session.id)['phase'] == 'done'
    assert continuation_status(session.id)['resume_count'] == resume_count
    assert not notices(runner)


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_count', [0, 1])
async def test_c1_adapter_unavailable_past_bound_notifies_once(enabled, monkeypatch, resume_count):
    runner = enabled
    session = armed_session(runner)
    await verify(runner)
    now = [time.time()]
    update_payload(session.id, timings={'completion_observed_at': now[0]},
        continuation_status={'resume_count': resume_count, 'interrupted_at': now[0]})
    monkeypatch.setattr(continuation, 'time', SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr('gateway.run._auto_continue_freshness_window', lambda: 60)
    monkeypatch.setattr(runner, '_resolve_injection_adapter', lambda *args: None)
    await reconcile(runner)
    now[0] += 61 if resume_count else 3601
    await reconcile(runner)
    await reconcile(runner)
    assert not continuations(runner)
    assert len(notices(runner)) == 1
    assert '게이트웨이가 채팅에 연결되지 않아 이어 가기를 시작하지 못했어' in notices(runner)[0].args[1]


@pytest.mark.asyncio
async def test_c1_no_route_still_skips(enabled, monkeypatch):
    session = armed_session(enabled)
    await verify(enabled)
    admission = AsyncMock(return_value=False)
    monkeypatch.setattr(enabled, '_inject_watch_notification', admission)
    await reconcile(enabled)
    assert continuation_status(session.id)['phase'] == 'ready'
    admission.return_value = None
    await reconcile(enabled)
    await reconcile(enabled)
    assert continuation_status(session.id)['phase'] == 'skipped'
    assert len(notices(enabled)) == 1 and not continuations(enabled)


@pytest.mark.asyncio
async def test_c1_retry_release_cannot_reset_newer_or_running_claim(enabled, monkeypatch):
    session = armed_session(enabled)
    await verify(enabled)
    admission = AsyncMock(return_value=False)
    monkeypatch.setattr(enabled, '_inject_watch_notification', admission)
    await reconcile(enabled)
    old_token = admission.call_args.args[1]['process_continuation']['token']
    assert continuation_status(session.id)['phase'] == 'ready'
    event = continuation.claim(session.id)
    token = event['process_continuation']['token']
    assert token != old_token
    continuation.admission_failed(session.id, old_token, retryable=True)
    assert continuation_status(session.id)['token'] == token
    assert continuation_status(session.id)['phase'] == 'queued'
    assert continuation.begin(session.id, token, enabled.entry.session_key) is not None
    continuation.admission_failed(session.id, token, retryable=True)
    assert continuation_status(session.id)['phase'] == 'running'


@pytest.mark.asyncio
async def test_c1_queued_behind_long_user_turn_still_runs(enabled, monkeypatch):
    session = armed_session(enabled)
    await verify(enabled)
    await reconcile(enabled)
    event = continuations(enabled)[0]
    stopped = armed_session(enabled, 'proc_stopped_while_queued')
    await verify(enabled)
    await reconcile(enabled)
    stopped_event = continuations(enabled)[1]
    clock = time.time() + 3601
    monkeypatch.setattr(continuation, 'time', SimpleNamespace(time=lambda: clock))
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    run_continuation(enabled, event, model)
    model.assert_called_once()
    await asyncio.to_thread(continuation.record_stop, enabled.entry.session_key)
    run_continuation(enabled, stopped_event, model)
    model.assert_called_once()
    await reconcile(enabled)
    assert continuation_status(session.id)['phase'] == 'done', 'queue waiting must not expire admission'
    assert continuation_status(stopped.id)['phase'] == 'skipped', 'explicit stop must still cancel queued work'
    assert len(notices(enabled)) == 1 and stopped.id in notices(enabled)[0].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('resume_count', [0, 1])
async def test_c1_missing_verification_ledger_waits_for_evidence(enabled, resume_count):
    from gateway.delivery_ledger import record_obligation
    session = armed_session(enabled)
    result = await verify(enabled)
    update_payload(session.id, continuation_status={'resume_count': resume_count, 'interrupted_at': time.time()})
    with ledger._db() as database:
        database.execute('DELETE FROM delivery_obligations WHERE turn_id=?', (result['turn_id'],))
    await reconcile(enabled)
    await reconcile(enabled)
    assert not notices(enabled), 'missing report rows can be a transient publication gap'
    assert not continuations(enabled)
    record_obligation(obligation_id='late-report', session_key=enabled.entry.session_key,
        platform='telegram', chat_id='4242', thread_id='topic', content='verified', turn_id=result['turn_id'])
    await reconcile(enabled)
    assert len(continuations(enabled)) == 1
    model = MagicMock(return_value={'final_response': 'done', 'messages': []})
    run_continuation(enabled, continuations(enabled)[0], model)
    model.assert_called_once()
    assert continuation_status(session.id)['phase'] == 'done'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_site', ['listing', 'state', 'ledger', 'claim'])
async def test_c2_stop_persist_failure_warns_without_state_db(enabled, monkeypatch, failure_site):
    from gateway.run import GatewayRunner
    session = armed_session(enabled)
    await verify(enabled)
    enabled._interrupt_and_clear_session = AsyncMock()
    source = replace(enabled.source, chat_id='stop-chat', thread_id='stop-thread')
    enabled._thread_metadata_for_target = lambda platform, chat, thread, **kwargs: {'thread_id': thread}
    monkeypatch.setattr(continuation, 'record_stop', lambda *args: False)
    monkeypatch.setenv('HERMES_HEALTH_MENTION', '<@12345>')
    failing = MagicMock(side_effect=sqlite3.OperationalError('read-only state database'))
    if failure_site == 'listing':
        monkeypatch.setattr(continuation, '_db', failing)
    else:
        method = {'state': 'turn_delivery_state', 'ledger': 'record_obligation', 'claim': 'claim_pending_obligation'}[failure_site]
        monkeypatch.setattr('gateway.delivery_ledger.' + method, failing)
    await GatewayRunner._busy_stop_command(enabled, MessageEvent(text='/stop', source=source),
        enabled.entry.session_key, source)
    await asyncio.gather(*getattr(enabled, '_background_tasks', set()))
    warnings = [call for call in enabled.transport.send.call_args_list if '멈춤 기록 저장 실패' in str(call)]
    assert len(warnings) == 1, 'a broken state DB must not silence the stop persistence warning'
    assert warnings[0].args[0] == source.chat_id
    assert warnings[0].kwargs['metadata']['thread_id'] == source.thread_id
    assert '재시작되면 다시 이어질 수 있어' in warnings[0].args[1]
    assert warnings[0].args[1].endswith('\n<@12345>')
    if failure_site != 'listing':
        assert session.id in warnings[0].args[1]
    enabled._interrupt_and_clear_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_c2_stop_persist_failure_db_ok_no_duplicate(enabled, monkeypatch):
    from gateway.run import GatewayRunner
    session = armed_session(enabled)
    await verify(enabled)
    enabled._interrupt_and_clear_session = AsyncMock()
    monkeypatch.setattr(continuation, 'record_stop', lambda *args: False)
    release = asyncio.Event()
    async def send(*args, **kwargs):
        await release.wait()
        return SimpleNamespace(success=True)
    enabled.transport.send = AsyncMock(side_effect=send)
    command = asyncio.create_task(GatewayRunner._busy_stop_command(enabled,
        MessageEvent(text='/stop', source=enabled.source), enabled.entry.session_key, enabled.source))
    try:
        done, _pending = await asyncio.wait({command}, timeout=2)
        assert command in done, 'healthy ledger warning delivery must also stay off the interrupt path'
    finally:
        release.set()
        await command
        while getattr(enabled, '_background_tasks', set()):
            await asyncio.gather(*enabled._background_tasks)
    warnings = [call for call in enabled.transport.send.call_args_list if '멈춤 기록 저장 실패:' in str(call)]
    assert len(warnings) == 1
    from gateway.delivery_ledger import turn_delivery_state
    with ledger._db(read_only=True) as database:
        turn_id = database.execute("SELECT turn_id FROM delivery_obligations WHERE turn_id LIKE 'process-continuation-stop-write:%'").fetchone()[0]
    assert turn_delivery_state(enabled.entry.session_key, turn_id) == 'delivered'
    assert session.id in warnings[0].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('handler', ['busy', 'idle'])
async def test_c3_stop_interrupt_not_blocked_by_warning(enabled, monkeypatch, handler):
    from gateway.run import GatewayRunner
    armed_session(enabled)
    await verify(enabled)
    release, started = asyncio.Event(), asyncio.Event()
    async def send(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(success=True)
    enabled.transport.send = AsyncMock(side_effect=send)
    enabled._interrupt_and_clear_session = AsyncMock()
    monkeypatch.setattr(continuation, 'record_stop', lambda *args: False)
    event = MessageEvent(text='/stop', source=enabled.source)
    if handler == 'idle':
        enabled._session_key_for_source = lambda source: enabled.entry.session_key
        enabled.async_session_store = SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=enabled.entry))
        enabled._running_agents = {enabled.entry.session_key: object()}
        command = asyncio.create_task(GatewayRunner._handle_stop_command(enabled, event))
    else:
        command = asyncio.create_task(GatewayRunner._busy_stop_command(enabled, event,
            enabled.entry.session_key, enabled.source))
    try:
        done, _pending = await asyncio.wait({command}, timeout=2)
        assert command in done, 'hard interrupt must not wait for the hanging warning send'
        await command
        enabled._interrupt_and_clear_session.assert_awaited_once()
        await asyncio.wait_for(started.wait(), 5)
        assert not release.is_set(), 'warning delivery remains in the background after interrupt'
    finally:
        release.set()
        await command
        await asyncio.gather(*getattr(enabled, '_background_tasks', set()))


@pytest.mark.asyncio
async def test_c3_slow_stop_write_does_not_block_interrupt(enabled, monkeypatch):
    from gateway.run import GatewayRunner
    armed_session(enabled)
    await verify(enabled)
    release, entered = threading.Event(), threading.Event()
    attempts = []
    def write(key, received_at):
        attempts.append(received_at)
        entered.set()
        assert release.wait(20)
        return False
    monkeypatch.setattr(continuation, 'record_stop', write)
    enabled._interrupt_and_clear_session = AsyncMock()
    command = asyncio.create_task(GatewayRunner._busy_stop_command(enabled,
        MessageEvent(text='/stop', source=enabled.source), enabled.entry.session_key, enabled.source))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert continuation.stopped_after(enabled.entry.session_key, 1)
        done, _pending = await asyncio.wait({command}, timeout=4)
        assert command in done, 'stop fsync must stop delaying the interrupt after about two seconds'
        enabled._interrupt_and_clear_session.assert_awaited_once()
    finally:
        release.set()
        await command
        while getattr(enabled, '_background_tasks', set()):
            await asyncio.gather(*enabled._background_tasks)
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len([call for call in enabled.transport.send.call_args_list if '멈춤 기록 저장 실패:' in str(call)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery', ['startup', 'reconcile'])
async def test_c4_long_running_turn_resumes_after_restart(enabled, home, recovery):
    session = armed_session(enabled)
    await verify(enabled)
    await reconcile(enabled)
    async with interrupted_turn(enabled, continuations(enabled)[0], session.id):
        update_payload(session.id, timings={'completion_observed_at': time.time() - 7200})
        restarted = restart_runner(home, 'second-boot')
        if recovery == 'startup':
            await startup_continuation(restarted)
        else:
            await reconcile(restarted)
        events = continuations(restarted)
        assert len(events) == 1, 'fresh interruption, not old process completion, bounds restart resume'
        model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
        run_continuation(restarted, events[0], model)
        run_continuation(restarted, events[0], model)
        model.assert_called_once()
        assert continuation_status(session.id)['resume_count'] == 1
        assert not notices(restarted)


@pytest.mark.asyncio
@pytest.mark.parametrize('stamp_field', ['failed_at', 'interrupted_at', 'started_at'])
async def test_c4_stale_interruption_skips_with_notice(enabled, home, monkeypatch, stamp_field):
    session = armed_session(enabled)
    await verify(enabled)
    await reconcile(enabled)
    async with interrupted_turn(enabled, continuations(enabled)[0], session.id):
        restarted = restart_runner(home, 'second-boot')
        record = restarted.entry.active_turn
        record[stamp_field] = time.time() - 61
        if stamp_field == 'failed_at':
            record['interrupted_at'] = time.time()
        monkeypatch.setattr('gateway.run._auto_continue_freshness_window', lambda: 60)
        await startup_continuation(restarted)
        await reconcile(restarted)
        assert not continuations(restarted), 'a stale gateway interruption must not dispatch a continuation'
        assert len(notices(restarted)) == 1
        assert '게이트웨이가 너무 오래 멈춰 있어서 자동 재개를 멈췄어' in notices(restarted)[0].args[1]


@pytest.mark.asyncio
async def test_c4_restart_retry_retains_freshness_and_chain_reservation(enabled, home, monkeypatch):
    (home / 'config.yaml').write_text('agent:\n  process_continue_on_complete: true\n  process_continue_max_chain: 1\n')
    session = armed_session(enabled)
    await verify(enabled)
    await reconcile(enabled)
    async with interrupted_turn(enabled, continuations(enabled)[0], session.id):
        update_payload(session.id, timings={'completion_observed_at': time.time() - 7200})
        restarted = restart_runner(home, 'second-boot')
        inject = restarted._inject_watch_notification
        async def admission(text, event):
            return False if event.get('process_continuation') else await inject(text, event)
        monkeypatch.setattr(restarted, '_inject_watch_notification', admission)
        await startup_continuation(restarted)
        status = continuation_status(session.id)
        assert status['phase'] == 'ready' and status['resume_count'] == 1
        assert status['interrupted_at']
        sibling = armed_session(restarted, 'proc_sibling')
        await verify(restarted)
        await reconcile(restarted)
        assert continuation_status(sibling.id)['phase'] == 'skipped'
        assert 'chain cap' in continuation_status(sibling.id)['reason']
        monkeypatch.setattr(restarted, '_inject_watch_notification', inject)
        await reconcile(restarted)
        events = continuations(restarted)
        assert len(events) == 1
        assert continuation_status(session.id)['interrupted_at'] == status['interrupted_at']
        model = MagicMock(return_value={'final_response': 'resumed', 'messages': []})
        run_continuation(restarted, events[0], model)
        model.assert_called_once()


@pytest.mark.asyncio
async def test_c5_stop_warning_ignores_finished_executions(enabled):
    for phase in ('done', 'skipped', 'uncertain', 'ready', 'running'):
        session = armed_session(enabled, 'proc_' + phase)
        update_payload(session.id, continuation_status={'phase': phase})
    failures = continuation.stop_write_failures(enabled.entry.session_key, time.time())
    assert {execution for execution, _payload in failures} == {'proc_ready', 'proc_running'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_site', ['source', 'profile'])
async def test_c6_one_bad_session_does_not_block_startup_resume(enabled, home, monkeypatch, failure_site):
    session = armed_session(enabled)
    await verify(enabled)
    await reconcile(enabled)
    async with interrupted_turn(enabled, continuations(enabled)[0], session.id):
        restarted = restart_runner(home, 'second-boot')
        bad = restarted.entry
        good = replace(bad, session_key=bad.session_key + ':healthy', active_turn=None, resume_pending=True)
        monkeypatch.setattr(restarted, '_resume_pending_candidates', lambda platform: [bad, good])
        restored = restarted._restored_source
        seen = []
        def restore(entry):
            if entry is bad and failure_site == 'source':
                raise RuntimeError('invalid stored route')
            if entry is good:
                seen.append(entry.session_key)
            return restored(entry)
        monkeypatch.setattr(restarted, '_restored_source', restore)
        if failure_site == 'profile':
            monkeypatch.setattr(restarted, '_resolve_profile_home_for_source', MagicMock(side_effect=RuntimeError('unserved profile')))
        monkeypatch.setattr(restarted, '_is_session_running', lambda key: key == good.session_key and bool(seen))
        monkeypatch.setattr(restarted, '_resume_owner_authorized', lambda *args: False)
        failure = None
        try:
            restarted._schedule_resume_pending_sessions()
        except RuntimeError as error:
            failure = error
        assert failure is None, f'one bad continuation route must not abort startup: {failure}'
        assert seen == [good.session_key], 'later ordinary user sessions must still reach resume scheduling'
        assert bad.active_turn is not None, 'failed entry must retain its recovery marker'
