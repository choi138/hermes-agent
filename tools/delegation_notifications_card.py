"""Presentation and transport for a run's automatic progress card."""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


def coordinator_active(runner, session):
    peek = getattr(runner, '_peek_session_state', None)
    state = peek(session.session_key) if callable(peek) else None
    return state is not None and (state.turn.agent is not None or
        (state.last_turn_ended_at > 0 and time.time() - state.last_turn_ended_at < 300))


def suppress_progress(oid):
    from gateway import delivery_ledger as ledger
    from tools.process_registry_followups import _db
    pid, started = ledger._owner_stamp()
    with _db() as db:
        return db.execute("""UPDATE delivery_obligations SET state='abandoned',updated_at=?,
            last_error='suppressed_coordinator_active' WHERE obligation_id=? AND state='attempting'
            AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), oid, pid, started)).rowcount == 1


async def suppress_if_active(runner, session, message, oid):
    from tools import delegation_notifications as notices
    if message.get('event') != 'periodic' or not coordinator_active(runner, session):
        return False
    if await asyncio.to_thread(suppress_progress, oid):
        await asyncio.to_thread(notices.remote_observe, session, ack=message['id'])
    return True


def label_content(session, message, body):
    event = message.get('event')
    label = '-# 자동 진행 알림'
    if event == 'started':
        label += ' · 실행 시작'
    elif event == 'periodic':
        minutes = max(1, int((time.time() - session.started_at) // 60))
        label += f' · 실행 {minutes}분째'
    if body.startswith('-# 자동 진행 알림'):
        body = body.partition('\n')[2]
    return label + '\n' + body[:1200 - len(label) - 1]


def card_message_id(session, message):
    from tools.process_registry_followups import _db
    run_id = message.get('run_id') or message['id'].rsplit(':', 1)[0]
    prefix = f'delegation-progress:{session.id}:{run_id}:'
    with _db(read_only=True) as db:
        row = db.execute("""SELECT r.message_id FROM delivery_obligation_receipts r
            JOIN delivery_obligations o ON o.obligation_id=r.obligation_id
            WHERE substr(o.obligation_id,1,?)=? AND o.state='delivered'
            AND o.content LIKE '-# 자동 진행 알림 · 실행%'
            ORDER BY r.observed_at DESC, o.rowid DESC LIMIT 1""", (len(prefix), prefix)).fetchone()
    return row[0] if row else None


async def send_card(adapter, source, content, session, message, fallback_allowed):
    from gateway.platforms.base import SendResult
    card_id = card_message_id(session, message) if message.get('event') == 'periodic' else None
    if card_id is not None:
        try:
            result = await adapter.edit_message(source.chat_id, card_id, content)
        except (asyncio.CancelledError, TimeoutError):
            raise
        except Exception:
            logger.warning('Progress card edit failed', exc_info=True)
            result = None
        if getattr(result, 'success', False):
            return SendResult(success=True, message_id=card_id)
        if not fallback_allowed.is_set():
            return SendResult(success=False, error='Interrupted progress edit; receipt unknown')
    return await adapter.send(source.chat_id, content,
        metadata={'thread_id': source.thread_id, 'allowed_mentions': {'parse': [], 'replied_user': False},
                  'non_conversational': True})


async def deliver_card(adapter, source, content, session, message, oid):
    from tools import delegation_notifications as notices
    from tools.process_registry_followups import scrub_payload
    fallback_allowed = asyncio.Event()
    fallback_allowed.set()
    send = asyncio.create_task(send_card(adapter, source, content, session, message, fallback_allowed))
    try:
        result = await asyncio.wait_for(asyncio.shield(send), timeout=20)
    except (asyncio.TimeoutError, asyncio.CancelledError) as interruption:
        fallback_allowed.clear()
        async def settle():
            try:
                receipt = await send
                if getattr(receipt, 'success', False):
                    await asyncio.to_thread(notices._mark_progress_delivered, oid,
                                          message_id=getattr(receipt, 'message_id', None))
                    await asyncio.to_thread(notices.remote_observe, session, ack=message['id'])
                else:
                    await asyncio.to_thread(notices._mark_progress_uncertain, oid,
                        'progress_late_receipt_unknown:' + scrub_payload(str(getattr(receipt, 'error', '') or 'unconfirmed'))[:350])
            except BaseException:
                logger.warning('Late progress transport interrupted', exc_info=True)
                await asyncio.to_thread(notices._mark_progress_uncertain, oid, 'Late progress send interrupted')
        task = asyncio.create_task(settle())
        notices._sends.add(task)
        task.add_done_callback(notices._sends.discard)
        if isinstance(interruption, asyncio.CancelledError):
            raise
        return
    if not getattr(result, 'success', False):
        await asyncio.to_thread(notices._mark_progress_uncertain, oid,
            'progress_receipt_unknown:' + scrub_payload(str(getattr(result, 'error', '') or 'unconfirmed'))[:350])
        return
    await asyncio.to_thread(notices._mark_progress_delivered, oid, message_id=getattr(result, 'message_id', None))
    await asyncio.to_thread(notices.remote_observe, session, ack=message['id'])
