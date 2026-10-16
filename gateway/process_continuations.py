"""Deliver launch-authorized continuations through the ordinary internal-event queue."""

import asyncio
import logging
import uuid

from tools import process_registry_continuation as continuation


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
            authority = None
        return followup, authority, authority is not None
    if followup:
        from tools.process_registry_followups import begin
        return followup, None, begin(followup['execution_id'], followup['token'])
    return None, None, True


def complete_turn(event, authority):
    if authority is not None:
        marker = event.metadata['process_continuation']
        try:
            continuation.finish(marker['execution_id'], marker['token'])
        except Exception:
            logging.getLogger(__name__).warning('Could not finish process continuation %s; not replaying',
                marker['execution_id'], exc_info=True)


def record_active_turn(runner, ctx, kwargs, followup):
    """Persist the shared normal/verification turn marker before model dispatch."""
    if ctx.turn_resume_marker or not ctx.session_key:
        return
    turn_id = f'{ctx.session_id}:{ctx.session_id}:{uuid.uuid4().hex[:8]}'
    begin = getattr(getattr(runner, 'session_store', None), 'begin_active_turn', None)
    if not callable(begin):
        if followup:
            raise RuntimeError('Follow-up requires durable active turn storage')
        return
    try:
        extra = {'process_followup': followup} if followup else {}
        owner = (ctx.persist_user_display_metadata or {}).get('gateway_input_owner')
        recorded = begin(ctx.session_key, turn_id, getattr(runner, '_boot_id', 'unknown-boot'),
            origin_session_id=ctx.session_id, origin_owner=owner, **extra)
        if followup and not recorded:
            raise RuntimeError('Follow-up turn marker was not persisted')
        if recorded:
            kwargs['turn_id'] = turn_id
            if ctx.event is not None:
                ctx.event._gateway_active_turn_id = turn_id
    except Exception:
        logging.getLogger('gateway.run_turn_runner').warning(
            'Could not record agent turn for %s', ctx.session_key, exc_info=True)
        if followup:
            raise


async def reconcile(runner):
    for row in await asyncio.to_thread(continuation.candidates):
        evt = await asyncio.to_thread(continuation.claim, row['execution_id'])
        if evt is None:
            continue
        marker = evt['process_continuation']
        text = (f"[System note: Background process {row['execution_id']} that you launched with "
                f"continue_on_complete has finished (verification outcome: {evt['verification']['outcome']}). "
                "Continue the task the user already approved from where you left off. Do not expand scope; "
                "ask before any new external publication, merge, or destructive action that the original "
                "approval did not cover.]")
        try:
            accepted = await runner._inject_watch_notification(text, evt)
        except Exception:
            await asyncio.to_thread(continuation.admission_failed, row['execution_id'], marker['token'], uncertain=True)
            raise
        if accepted is not True:
            await asyncio.to_thread(continuation.admission_failed, row['execution_id'], marker['token'])
