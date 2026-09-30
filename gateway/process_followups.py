"""Reconcile durable process completions through the existing gateway wake path."""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)
_attention_sends = set()
_attention_inflight = {}


class AttentionSendPending(TimeoutError):
    """The transport still owns an unresolved send; another attempt must wait."""


def attention_inflight(turn_id):
    from hermes_constants import get_hermes_home
    return (str(get_hermes_home()), turn_id) in _attention_inflight



async def reconcile(runner):
    from tools.process_registry_followups import pending, admission, defer, cancel
    from tools.process_registry_notifications import format_process_notification
    from gateway.wake import adapter_supports_push
    for row in await asyncio.to_thread(pending):
        execution, token = row['execution_id'], row['token']
        from tools.process_registry_followups import scrub_payload
        evt = scrub_payload(json.loads(row['payload']))
        if row['phase'] in {'turn_finished', 'failed'}:
            from gateway.delivery_ledger import turn_delivery_state
            from tools.process_registry_followups import report_state
            delivery = await asyncio.to_thread(turn_delivery_state, evt.get('session_key'), evt.get('followup_turn_id'))
            if delivery == 'delivered':
                await asyncio.to_thread(report_state, execution, token, delivered=True)
            elif delivery == 'pending':
                await asyncio.to_thread(defer, execution, token, 'verification turn ended; existing delivery ledger owns report retry')
            else:
                await asyncio.to_thread(report_state, execution, token, reason='Verification turn ended without a durable report receipt; inspect the conversation before retrying')
            continue
        parent = row['parent_session_id']
        if parent:
            verdict = await runner._classify_completion_target(parent)
            if verdict == 'terminal':
                await asyncio.to_thread(cancel, execution)
                continue
            if verdict != 'deliver':
                await asyncio.to_thread(defer, execution, token, 'parent state unavailable; retry scheduled')
                continue
        source = runner._build_process_event_source(evt)
        if source is None:
            await asyncio.to_thread(defer, execution, token, 'parent routing unavailable; retry scheduled')
            logger.warning('Process follow-up waiting for route: %s', execution)
            continue
        platform = getattr(source.platform, 'value', source.platform)
        adapter = runner._resolve_injection_adapter(platform, source)
        if adapter is None or not adapter_supports_push(adapter):
            # Stateless HTTP wake has no execution-token handoff; never bypass fencing.
            await asyncio.to_thread(defer, execution, token, 'push adapter unavailable; retry scheduled')
            logger.warning('Process follow-up waiting for adapter: %s', execution)
            continue
        if row['phase'] == 'needs_reconciliation':
            await _report_attention(runner, adapter, source, row, evt)
            continue
        if not await asyncio.to_thread(admission, execution, token):
            continue
        evt['process_followup'] = {'execution_id': execution, 'token': token}
        text = format_process_notification(evt)
        if evt.get('verification_scope'):
            scope = evt['verification_scope']
            text += '\nExecution working directory: ' + str(scope.get('cwd', ''))
            if scope.get('task_request'):
                text += '\nDispatching message (resolve the complete task and completion criteria from the conversation):\n' + scope['task_request']
            else:
                text += '\nOriginal task request was not recorded. Resolve it from the source message/conversation before claiming verification; report approval_wait if it is unavailable.'
        if evt.get('observation_error'):
            text += '\nRemote output is incomplete: ' + evt['observation_error'] + '. Inspect artifacts before verifying.'
        text += ('\nContinue the previously authorized task: verify this result and report the evidence. '
                 'Do not repeat the completed command. If verification fails, report the failure and '
                 'remaining work. If approval is required, explicitly report what is waiting. '
                 'Do not claim verification merely because the process exited successfully.')
        try:
            accepted = await runner._inject_watch_notification(text, evt)
            if accepted is not True:
                await asyncio.to_thread(defer, execution, token, 'gateway admission unavailable; retry scheduled')
        except Exception as exc:
            await asyncio.to_thread(defer, execution, token, f'admission failed: {type(exc).__name__}')
            from tools.environments.ssh_process import safe_error
            logger.warning('Process follow-up admission failed: %s: %s', execution, safe_error(exc))


async def reconcile_all(runner):
    from gateway.run import _multiplex_profile_homes, _async_profile_runtime_scope
    from hermes_constants import get_hermes_home
    homes = [get_hermes_home()]
    if getattr(runner.config, 'multiplex_profiles', False):
        homes.extend(home for _name, home in _multiplex_profile_homes(runner.config))
    for home in dict.fromkeys(map(str, homes)):
        try:
            from pathlib import Path
            async with _async_profile_runtime_scope(Path(home)):
                from tools.process_registry import process_registry
                await asyncio.to_thread(process_registry.retry_checkpoint_recovery)
                await reconcile(runner)
        except Exception as exc:
            from tools.environments.ssh_process import safe_error
            logger.warning('Process follow-up reconciliation failed for profile %s: %s', home, safe_error(exc))


async def _send_attention(adapter, source, content, metadata, execution, token, turn_id, *, runner=None):
    from tools.process_registry_followups import attention_authorized
    from gateway.delivery_ledger import abandon_obligation, mark_uncertain
    if attention_inflight(turn_id):
        raise AttentionSendPending('previous attention send still unresolved')
    if not await asyncio.to_thread(attention_authorized, execution, token):
        await asyncio.to_thread(abandon_obligation, turn_id)
        return None
    from hermes_constants import get_hermes_home
    key = (str(get_hermes_home()), turn_id)
    # Authorization awaited a thread; another sender may have acquired ownership meanwhile.
    if attention_inflight(turn_id):
        raise AttentionSendPending('previous attention send still unresolved')
    detached = False
    settlement_scheduled = False
    send = asyncio.create_task(adapter.send(source.chat_id, content, metadata=metadata))
    _attention_sends.add(send)
    _attention_inflight[key] = send

    async def settle_late(task):
        from gateway.delivery_ledger import mark_delivered, mark_failed
        from tools.process_registry_followups import acknowledge_attention
        from tools.environments.ssh_process import safe_error
        try:
            if not await asyncio.to_thread(attention_authorized, execution, token):
                await asyncio.to_thread(abandon_obligation, turn_id)
                return
            try:
                result = task.result()
            except asyncio.CancelledError:
                await asyncio.to_thread(mark_uncertain, turn_id, 'attention transport cancelled; inspect conversation before manual resend')
            except Exception as exc:
                await asyncio.to_thread(mark_uncertain, turn_id, safe_error(exc))
            else:
                if getattr(result, 'success', False) is True:
                    await asyncio.to_thread(mark_delivered, turn_id)
                    await asyncio.to_thread(acknowledge_attention, execution, token)
                else:
                    await asyncio.to_thread(mark_failed, turn_id, safe_error(RuntimeError(str(getattr(result, 'error', '') or 'send rejected'))))
                    schedule = getattr(runner, '_schedule_flood_redelivery', None)
                    if callable(schedule):
                        schedule(source.platform, profile=getattr(adapter, '_owner_profile', None))
        except Exception as exc:
            logger.warning('Late attention settlement failed: %s: %s', execution, safe_error(exc))
        finally:
            _attention_inflight.pop(key, None)

    def schedule_settlement(task):
        nonlocal settlement_scheduled
        if settlement_scheduled:
            return
        settlement_scheduled = True
        # Its completion callback may already have released ownership before caller cancellation.
        _attention_inflight[key] = task
        settlement = asyncio.create_task(settle_late(task))
        _attention_sends.add(settlement)
        settlement.add_done_callback(_attention_sends.discard)

    def settled(task):
        _attention_sends.discard(task)
        if not task.cancelled():
            task.exception()  # Always consume transport errors, including invalidated tokens.
        if detached:
            schedule_settlement(task)
        else:
            _attention_inflight.pop(key, None)
    send.add_done_callback(settled)
    from agent.deadline import resolve_timeout
    deadline = asyncio.get_running_loop().time() + (resolve_timeout('gateway.process_attention_send', default=10) or 10)
    try:
        while True:
            done, _ = await asyncio.wait({send}, timeout=.1)
            if not await asyncio.to_thread(attention_authorized, execution, token):
                await asyncio.to_thread(abandon_obligation, turn_id)
                return None
            if send.done():
                return send.result()
            if asyncio.get_running_loop().time() >= deadline:
                detached = True
                await asyncio.to_thread(mark_uncertain, turn_id, 'attention send deadline exceeded; transport still owns settlement; inspect conversation before manual resend')
                raise AttentionSendPending('process attention send deadline exceeded; transport settlement pending')
    except asyncio.CancelledError:
        detached = True
        await asyncio.to_thread(mark_uncertain, turn_id, 'attention caller cancelled; transport settlement unresolved')
        raise
    except AttentionSendPending:
        raise
    except Exception as exc:
        detached = True
        from tools.environments.ssh_process import safe_error
        await asyncio.to_thread(mark_uncertain, turn_id, safe_error(exc))
        raise AttentionSendPending('attention send outcome unknown; manual reconciliation required') from exc
    finally:
        if detached and send.done():
            schedule_settlement(send)
        if not detached and not send.done():
            send.cancel()
        # A transport that suppresses cancellation must not hold the watcher.
        await asyncio.wait({send}, timeout=.1)


async def _report_attention(runner, adapter, source, row, evt):
    from gateway.delivery_ledger import (turn_delivery_state, record_obligation,
        claim_pending_obligation, mark_delivered, mark_failed, mark_uncertain)
    from tools.process_registry_followups import acknowledge_attention, defer, attention_authorized
    execution, token = row['execution_id'], row['token']
    turn_id = 'process-attention:' + execution + ':' + token
    session_key = evt.get('session_key') or ''
    profile = getattr(adapter, '_owner_profile', None)
    if not await asyncio.to_thread(attention_authorized, execution, token):
        from gateway.delivery_ledger import abandon_obligation
        await asyncio.to_thread(abandon_obligation, turn_id)
        return
    try:
        state = await asyncio.to_thread(turn_delivery_state, session_key, turn_id)
        if state == 'delivered':
            await asyncio.to_thread(acknowledge_attention, execution, token)
            return
        if state == 'uncertain':
            await asyncio.to_thread(defer, execution, token, 'attention send outcome unknown; inspect conversation before any manual resend')
            return
        if state == 'abandoned':
            await asyncio.to_thread(acknowledge_attention, execution, token, delivered=False)
            return
        content = (f"Process {execution}: verification needs reconciliation. {row['reason']}. "
                   "The original execution remains recorded; no command was automatically repeated.")
        if state is None:
            await asyncio.to_thread(record_obligation, obligation_id=turn_id, session_key=session_key,
                platform=getattr(source.platform, 'value', source.platform), chat_id=source.chat_id,
                thread_id=source.thread_id, content=content, adapter_profile=profile,
                turn_id=turn_id, preserve_existing=True)
        if state in {None, 'pending'}:
            metadata = runner._thread_metadata_for_target(source.platform, source.chat_id, source.thread_id,
                chat_type=source.chat_type, reply_to_message_id=evt.get('message_id'), adapter=adapter)
            metadata = {**(metadata or {}), '_interim_send': True}
            prime = getattr(adapter, 'prime_routing_cache', None)
            if callable(prime):
                from gateway.platforms.base import MessageEvent
                prime(MessageEvent(text='', source=source, internal=True))
            # Preparation can fail without admitting a send; leave that obligation retryable.
            if not await asyncio.to_thread(claim_pending_obligation, turn_id):
                await asyncio.to_thread(defer, execution, token, row['reason'])
                return
            try:
                result = await _send_attention(adapter, source, content, metadata, execution, token, turn_id, runner=runner)
                if result is None:
                    return
            except AttentionSendPending:
                await asyncio.to_thread(defer, execution, token, 'attention transport settlement pending; no concurrent retry')
                return
            except Exception as exc:
                from tools.environments.ssh_process import safe_error
                # A storage/transport exception after admission cannot prove rejection.
                await asyncio.to_thread(mark_uncertain, turn_id, safe_error(exc))
                logger.warning('Process attention send outcome unknown: %s: %s', execution, safe_error(exc))
            else:
                if getattr(result, 'success', False) is True:
                    await asyncio.to_thread(mark_delivered, turn_id)
                    await asyncio.to_thread(acknowledge_attention, execution, token)
                    return
                from tools.environments.ssh_process import safe_error
                await asyncio.to_thread(mark_failed, turn_id, safe_error(RuntimeError(str(getattr(result, 'error', '') or 'send rejected'))))
        # Only the existing ledger retries an ambiguous send (with its visible recovered marker).
        schedule = getattr(runner, '_schedule_flood_redelivery', None)
        if callable(schedule):
            schedule(source.platform, profile=profile)
        await asyncio.to_thread(defer, execution, token, row['reason'])
    except Exception as exc:
        await asyncio.to_thread(defer, execution, token, row['reason'])
        from tools.environments.ssh_process import safe_error
        logger.warning('Process reconciliation delivery pending: %s: %s', execution, safe_error(exc))
