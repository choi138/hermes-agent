"""Deliver launch-authorized continuations through the ordinary internal-event queue."""

import asyncio
import logging
import uuid

from tools import process_registry_continuation as continuation


async def stop_session(key, *, runner):
    from hermes_constants import get_hermes_home
    received_at = continuation.receive_stop(key)
    if await _persist_stop(key, received_at):
        return
    _background_task(runner, _retry_stop(key, received_at, get_hermes_home()))
    try:
        for execution, payload in await asyncio.to_thread(continuation.stop_write_failures, key, received_at):
            content = (f'멈춤 기록 저장 실패: {execution} — 지금 프로세스에서는 멈춰 있지만, '
                '게이트웨이가 재시작되면 다시 이어질 수 있어. 확실히 멈추려면 /stop 을 한 번 더 보내 줘.')
            await _deliver_notice(runner, payload,
                f'process-continuation-stop-write:{execution}:{received_at}', content)
    except Exception:
        logging.getLogger(__name__).warning('Stop storage warning pending for %s', key, exc_info=True)


async def _persist_stop(key, received_at):
    try:
        return await asyncio.to_thread(continuation.record_stop, key, received_at) is True
    except Exception:
        logging.getLogger(__name__).warning('Continuation stop write failed for %s', key, exc_info=True)
        return False


async def _retry_stop(key, received_at, home):
    from gateway.run import _async_profile_runtime_scope
    async with _async_profile_runtime_scope(home):
        await asyncio.sleep(0.25)
        await _persist_stop(key, received_at)


def _background_task(runner, coroutine):
    task = asyncio.create_task(coroutine)
    background = getattr(runner, '_background_tasks', None)
    if not isinstance(background, set):
        background = runner._background_tasks = set()
    background.add(task)
    task.add_done_callback(background.discard)
    return task


def filter_restart_candidates(runner, candidates):
    """Keep continuation recovery on its execution budget, outside the user restart breaker."""
    restart_candidates = [entry for entry in candidates if not (
        isinstance(getattr(entry, 'active_turn', None), dict)
        and (entry.active_turn.get('status') == 'retry_wait' or entry.active_turn.get('process_continuation')))]
    if restart_candidates:
        try:
            from gateway import restart_loop_guard
            max_restarts, window, max_gap = runner._restart_loop_guard_config()
            if restart_loop_guard.check_and_record(max_restarts, window, max_gap_seconds=max_gap):
                return [entry for entry in candidates if (entry.active_turn or {}).get('process_continuation')] or None
        except Exception as exc:
            logging.getLogger('gateway.run').debug('Restart-loop guard check skipped: %s', exc, exc_info=True)
    return candidates


def recover_orphaned_turn(runner, entry, record):
    """Return 1 if scheduled, 0 if retired, or None for ordinary user-turn recovery."""
    if not record.get('process_continuation'):
        return 0 if retire_orphaned_turn(runner, entry, record) else None
    source = runner._restored_source(entry)
    home = runner._resolve_profile_home_for_source(source)
    _background_task(runner, _recover_orphaned_turn(runner, entry.session_key, record, home))
    return 1


async def _recover_orphaned_turn(runner, key, record, home):
    from gateway.run import _async_profile_runtime_scope
    async with _async_profile_runtime_scope(home):
        if not (await asyncio.to_thread(continuation.settings))[0]:
            return
        try:
            retired = await asyncio.to_thread(runner.session_store.finish_active_turn,
                key, record['turn_id'], expected_resume_count=record.get('resume_count'))
            if retired:
                await asyncio.to_thread(runner.session_store.clear_resume_pending,
                    key, expected_turn_id=record['turn_id'])
            await reconcile(runner)
        except Exception:
            logging.getLogger(__name__).warning('Continuation startup recovery pending for %s', key, exc_info=True)


def retire_orphaned_turn(runner, entry, record):
    """Retire answered turns and route interrupted verification to reconciliation."""
    try:
        from gateway.delivery_ledger import has_turn_obligation
        if has_turn_obligation(entry.session_key, record.get('turn_id')):
            if runner.session_store.finish_active_turn(entry.session_key, record.get('turn_id'),
                    expected_resume_count=record.get('resume_count')):
                runner.session_store.clear_resume_pending(entry.session_key)
            return True
    except Exception:
        logging.getLogger('gateway.run').warning('Skipping auto-resume for %s: delivery ledger unavailable',
            entry.session_key, exc_info=True)
        return True
    marker = record.get('process_followup')
    if not marker:
        return False
    from tools.process_registry_followups import require_reconciliation
    require_reconciliation(marker['execution_id'], marker['token'])
    runner.session_store.finish_active_turn(entry.session_key, record.get('turn_id'), force=True)
    runner.session_store.clear_resume_pending(entry.session_key)
    return True


def event_metadata(evt):
    return {key: evt[key] for key in ('process_followup', 'process_continuation') if evt.get(key)}


def admit_turn(event, key):
    metadata = getattr(event, 'metadata', None) or {}
    followup = metadata.get('process_followup')
    marker = metadata.get('process_continuation')
    if marker:
        try:
            authority = continuation.begin(marker['execution_id'], marker['token'], key) if event.internal and not followup else None
        except Exception:
            logging.getLogger(__name__).warning('Could not admit process continuation for %s', key, exc_info=True)
            continuation.record_failure(marker['execution_id'], 'continuation admission persistence failed')
            authority = None
        return followup, authority, authority is not None
    if followup:
        from tools.process_registry_followups import begin
        return followup, None, begin(followup['execution_id'], followup['token'])
    return None, None, True


def complete_turn(event, authority, result=None):
    if authority is not None:
        if result and result.get('interrupted') and result.get('turn_exit_reason') in (
                'interrupted_by_system(gateway_shutdown)',
                'interrupted_during_api_call(gateway_shutdown)'):
            return
        marker = event.metadata['process_continuation']
        try:
            reason = 'continuation turn failed or interrupted' if result is None or (
                result.get('failed') or result.get('interrupted') or result.get('completed') is False) else ''
            continuation.finish(marker['execution_id'], marker['token'], reason=reason)
        except Exception:
            continuation.record_failure(marker['execution_id'], 'continuation completion persistence failed')
            logging.getLogger(__name__).warning('Could not finish process continuation %s; not replaying',
                marker['execution_id'], exc_info=True)


def record_active_turn(runner, ctx, kwargs, followup):
    """Persist the shared normal/verification turn marker before model dispatch."""
    if ctx.turn_resume_marker or not ctx.session_key:
        return
    turn_id = f'{ctx.session_id}:{ctx.session_id}:{uuid.uuid4().hex[:8]}'
    marker = (getattr(ctx.event, 'metadata', None) or {}).get('process_continuation')
    begin = getattr(getattr(runner, 'session_store', None), 'begin_active_turn', None)
    if not callable(begin):
        if followup or marker:
            raise RuntimeError('Follow-up requires durable active turn storage')
        return
    try:
        extra = {'process_followup': followup} if followup else ({'process_continuation': marker} if marker else {})
        owner = (ctx.persist_user_display_metadata or {}).get('gateway_input_owner')
        recorded = begin(ctx.session_key, turn_id, getattr(runner, '_boot_id', 'unknown-boot'),
            origin_session_id=ctx.session_id, origin_owner=owner, **extra)
        if (followup or marker) and not recorded:
            raise RuntimeError('Follow-up turn marker was not persisted')
        if recorded:
            kwargs['turn_id'] = turn_id
            if ctx.event is not None:
                ctx.event._gateway_active_turn_id = turn_id
    except Exception:
        logging.getLogger('gateway.run_turn_runner').warning(
            'Could not record agent turn for %s', ctx.session_key, exc_info=True)
        if followup or marker:
            raise


async def reconcile(runner):
    for row in await asyncio.to_thread(continuation.candidates):
        try:
            evt = await asyncio.to_thread(continuation.claim, row['execution_id'])
        except Exception:
            await asyncio.to_thread(continuation.record_failure, row['execution_id'],
                                    'continuation reconciliation persistence failed')
            logging.getLogger(__name__).warning('Could not reconcile continuation %s', row['execution_id'], exc_info=True)
            continue
        if evt is None:
            continue
        marker = evt['process_continuation']
        text = (f"[System note: Background process {row['execution_id']} that you launched with "
                f"continue_on_complete has finished (verification outcome: {evt['verification']['outcome']}). "
                "Continue the task the user already approved from where you left off. Do not expand scope; "
                "ask before any new external publication, merge, or destructive action that the original "
                "approval did not cover.]")
        if evt['continuation_status'].get('resume_count'):
            text += ('\n[Internal recovery note: The previous continuation turn was interrupted by a gateway restart. '
                'Before continuing, inspect current state and the conversation to determine which actions already '
                'completed. Do not blindly redo side effects. Resume only remaining work within the same approval.]')
        try:
            accepted = await runner._inject_watch_notification(text, evt)
        except Exception:
            await _reject_admission(row['execution_id'], marker['token'], uncertain=True)
            logging.getLogger(__name__).warning('Continuation admission failed for %s', row['execution_id'], exc_info=True)
            continue
        if accepted is not True:
            await _reject_admission(row['execution_id'], marker['token'])
    await report_stops(runner)


async def _reject_admission(execution, token, *, uncertain=False):
    try:
        await asyncio.to_thread(continuation.admission_failed, execution, token, uncertain=uncertain)
    except Exception:
        await asyncio.to_thread(continuation.record_failure, execution, 'continuation admission persistence failed')
        logging.getLogger(__name__).warning('Continuation admission settlement pending for %s', execution, exc_info=True)


async def report_stops(runner):
    if not (await asyncio.to_thread(continuation.settings))[0]:
        return
    for execution, payload in await asyncio.to_thread(continuation.stopped_executions):
        try:
            await _report_stop(runner, execution, payload)
        except Exception:
            logging.getLogger(__name__).warning('Continuation notice pending for %s', execution, exc_info=True)


async def _report_stop(runner, execution, payload):
    turn_id = 'process-continuation-stop:' + execution
    reason = payload['continuation_status'].get('reason') or 'continuation needs reconciliation'
    content = f'이어 가기 멈춤: {execution} — {reason}. 이어서 하려면 아무 메시지나 보내 줘.'
    await _deliver_notice(runner, payload, turn_id, content)


async def _deliver_notice(runner, payload, turn_id, content):
    from gateway.delivery_ledger import turn_delivery_state, record_obligation, claim_pending_obligation
    state = await asyncio.to_thread(turn_delivery_state, payload['session_key'], turn_id)
    if state == 'delivered':
        return
    source = await asyncio.to_thread(runner._build_process_event_source, payload)
    if source is None:
        return
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return
    profile = getattr(adapter, '_owner_profile', None)
    if state in {None, 'pending'}:
        from gateway.agent_health import _strip_control
        from gateway.platforms._shared import get_scoped_secret
        mention = _strip_control(str(get_scoped_secret('HERMES_HEALTH_MENTION', '') or '')).strip()
        if mention:
            content += '\n' + mention
        metadata = runner._thread_metadata_for_target(source.platform, source.chat_id, source.thread_id,
            chat_type=source.chat_type, adapter=adapter)
        metadata = {**(metadata or {}), '_interim_send': True, 'notify': True}
        prime = getattr(adapter, 'prime_routing_cache', None)
        if callable(prime):
            from gateway.platforms.event import MessageEvent
            prime(MessageEvent(text='', source=source, internal=True))
        await asyncio.to_thread(record_obligation, obligation_id=turn_id, session_key=payload['session_key'],
            platform=getattr(source.platform, 'value', source.platform), chat_id=source.chat_id,
            thread_id=source.thread_id, content=content, adapter_profile=profile,
            turn_id=turn_id, preserve_existing=True)
        if not await asyncio.to_thread(claim_pending_obligation, turn_id):
            return
        send = _background_task(runner, _send_stop_notice(adapter, source, content, metadata, turn_id))
        await asyncio.wait({send}, timeout=15)
    schedule = getattr(runner, '_schedule_flood_redelivery', None)
    if callable(schedule):
        schedule(source.platform, profile=profile)


async def _send_stop_notice(adapter, source, content, metadata, turn_id):
    from gateway.delivery_ledger import mark_delivered, mark_failed
    try:
        try:
            result = await adapter.send(source.chat_id, content, metadata=metadata)
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).warning('Continuation notice transport outcome unknown: %s',
                turn_id, exc_info=True)
            await asyncio.to_thread(mark_failed, turn_id, 'continuation stop notice transport outcome unknown')
        else:
            if getattr(result, 'success', False) is True:
                await asyncio.to_thread(mark_delivered, turn_id, message_id=getattr(result, 'message_id', None))
            else:
                await asyncio.to_thread(mark_failed, turn_id, str(getattr(result, 'error', '') or 'send rejected'))
    except Exception:
        logging.getLogger(__name__).warning('Continuation notice settlement pending for %s', turn_id, exc_info=True)
