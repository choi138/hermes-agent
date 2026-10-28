"""Bind saved gateway completion messages to current parent authority at send time."""
import hashlib
import json
import re

from agent.task_lifecycle.types import LifecycleError


def _schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS lifecycle_completion_publications (
        obligation_id TEXT PRIMARY KEY, binding TEXT NOT NULL
    )''')
    for action in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS lifecycle_completion_publications_no_{action.lower()}
            BEFORE {action} ON lifecycle_completion_publications BEGIN
            SELECT RAISE(ABORT, 'publication authority is immutable'); END''')


def _certified_text(content):
    return '[Hermes task: verified' in content


def certified(event):
    decision = getattr(event, '_completion_decision', None)
    return bool(decision and decision.get('accepted') is True)


def prepare_obligation(event, obligation_id, content, session_key):
    """Every delivery lane owns the same durable parent decision, before any send."""
    from gateway.delivery_ledger import compute_obligation_id
    if certified(event):
        decision = event._completion_decision
        obligation_id = 'parent-completion:' + compute_obligation_id(
            session_key, decision['parent_id'], decision['decision_id'])
    bind_obligation(event, obligation_id, content, session_key)
    return obligation_id


def existing_delivery(event, session_key, obligation_id):
    from gateway import delivery_ledger
    if not certified(event):
        return delivery_ledger.turn_delivery_state(session_key, event._gateway_active_turn_id)
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as conn:
        row = conn.execute('SELECT state FROM delivery_obligations WHERE obligation_id=?',
                           (obligation_id,)).fetchone()
    return row[0] if row else None


def bind_obligation(event, obligation_id, content, session_key):
    """Only an internal event attribute can bind a certified final, never wire metadata."""
    from hermes_constants import get_hermes_home
    from gateway import delivery_ledger
    decision = getattr(event, '_completion_decision', None)
    if not decision or decision.get('accepted') is not True:
        if _certified_text(content):
            raise LifecycleError('Certified response has no authenticated publication authority')
        return
    binding = dict(parent_id=decision['parent_id'], decision_id=decision['decision_id'],
        session_key=session_key, profile=str(event.source.profile or 'default'),
        owner=str(event.source.user_id or ''), registry_path=str(get_hermes_home() / 'state.db'),
        content_digest=hashlib.sha256(content.encode()).hexdigest())
    encoded = json.dumps(binding, sort_keys=True)
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as conn:
        _schema(conn)
        row = conn.execute('SELECT binding FROM lifecycle_completion_publications WHERE obligation_id=?',
                           (obligation_id,)).fetchone()
        if row and row[0] != encoded:
            raise LifecycleError('Queued completion publication cannot change')
        conn.execute('INSERT OR IGNORE INTO lifecycle_completion_publications VALUES(?,?)',
                     (obligation_id, encoded))
    validate_obligation(obligation_id, content)


def validate_obligation(obligation_id, content):
    """Recheck current contract and remote/local artifacts on immediate send and recovery."""
    if obligation_id is None:
        if _certified_text(content):
            raise LifecycleError('Certified completion requires a durable publication obligation')
        return
    from gateway import delivery_ledger
    from agent.task_lifecycle.registry import Registry
    from agent.task_lifecycle.parent import ParentLedger
    from gateway.task_completion_managed import remote_snapshot_reader
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_completion_publications'").fetchone()
        row = conn.execute('SELECT binding FROM lifecycle_completion_publications WHERE obligation_id=?',
                           (obligation_id,)).fetchone() if table else None
    if not row:
        if _certified_text(content):
            raise LifecycleError('Unbound certified response cannot be sent or recovered')
        return
    binding = json.loads(row[0])
    if hashlib.sha256(content.encode()).hexdigest() != binding['content_digest']:
        raise LifecycleError('Queued completion content changed')
    registry = Registry(binding['registry_path'])
    try:
        ledger = ParentLedger(registry, remote_snapshot=remote_snapshot_reader(registry))
        parent = ledger.get(binding['parent_id'])
        if any(parent[k] != binding[k] for k in ('session_key', 'profile', 'owner')):
            raise LifecycleError('Completion publication identity mismatch')
        if not ledger.authoritative(binding['parent_id'], binding['decision_id']):
            raise LifecycleError('Queued completion is no longer authoritative')
    finally:
        registry.close()


def execution_report(text):
    """Process reports remain execution facts, including pre-upgrade recovery rows."""
    text = re.sub(r'\n*\[Hermes task: [^\n]*\]', '', str(text or ''))
    prefix = '[Hermes execution result; parent completion not established]\n\n'
    return text if text.startswith(prefix) else prefix + text


async def preflight_queued(adapter, source, content, session_key, inbound_message_id, decision,
                           metadata=None, reply_to=None, coverage_selector=None):
    """Certified queued finals use the shared atomic outbox instead of a separate edit."""
    if not _certified_text(content) and not (decision and decision.get('accepted') is True):
        return
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent
    if not session_key or not isinstance(adapter, BasePlatformAdapter):
        raise LifecycleError('Certified queued final requires durable adapter support')
    event = MessageEvent(text='', source=source, ledger_message_id=inbound_message_id)
    event._completion_decision = decision
    result, _ = await adapter.send_final_ledgered(event, session_key, content, metadata or {},
        reply_to=reply_to, coverage_selector=coverage_selector)
    return result.success


async def recover_content(row):
    """Recovery owns the stored publication and may not silently upgrade its meaning."""
    import asyncio
    import logging
    from gateway.delivery_ledger import mark_failed
    content = row['content']
    try:
        if row['obligation_id'].startswith('process-report:'):
            content = execution_report(content)
        await asyncio.to_thread(validate_obligation, row['obligation_id'], content)
        return content
    except Exception:
        await asyncio.to_thread(mark_failed, row['obligation_id'], 'Completion authority unavailable; no send admitted')
        logging.getLogger(__name__).warning('Completion publication deferred: %s', row['obligation_id'], exc_info=True)
        return None


async def available_obligations(runner, claimed):
    """Adopted flood waits retain their timer; only connected transports may send."""
    for row in claimed:
        if row.get('adopted'):
            continue
        adapter = await runner._obligation_adapter(row)
        if adapter is not None:
            yield row, adapter


async def recover_lifecycle(row, adapter):
    """The result's runtime profile owns lifecycle evidence, independent of delivery bot."""
    import asyncio
    from agent.task_lifecycle.handoff import lifecycle_obligation, deliver_result
    from gateway.delivery_ledger import _followups_home_override
    from gateway.run import _async_profile_runtime_scope
    async with _async_profile_runtime_scope(_followups_home_override(row.get('session_key'))):
        if not await asyncio.to_thread(lifecycle_obligation, row['obligation_id']):
            return None
        return await deliver_result(row['obligation_id'], adapter,
                                    adapter_profile=row.get('profile') or 'default')
