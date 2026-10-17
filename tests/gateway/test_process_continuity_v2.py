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
        'admission': lambda: monkeypatch.setattr(runner, '_inject_watch_notification', AsyncMock(return_value=False)),
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
