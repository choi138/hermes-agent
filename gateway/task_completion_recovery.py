"""Resume committed lifecycle work on the existing gateway profile watcher tick."""
import asyncio
import logging
import time
from types import SimpleNamespace

logger = logging.getLogger(__name__)


def _schema(registry):
    from gateway.task_completion_revision import preparation_schema
    preparation_schema(registry)
    with registry._lock, registry._conn:
        registry._conn.execute('''CREATE TABLE IF NOT EXISTS lifecycle_gateway_recovery (
            submission_id TEXT PRIMARY KEY, phase TEXT NOT NULL, error TEXT,
            last_probed REAL NOT NULL DEFAULT 0
        )''')
        from hermes_cli.sqlite_util import add_column_if_missing
        add_column_if_missing(registry._conn, 'lifecycle_gateway_recovery',
                              'last_probed', 'last_probed REAL NOT NULL DEFAULT 0')


def _job_state(registry, identity, phase, error=None):
    with registry._lock, registry._conn:
        registry._conn.execute('INSERT OR REPLACE INTO lifecycle_gateway_recovery '
            '(submission_id,phase,error,last_probed) VALUES(?,?,?,?)',
            (identity, phase, error, time.time()))


def _resume_job(registry, ledger, job):
    from gateway.task_completion_managed import _reconcile
    identity, parent_id, payload, previous, kind, contract_digest = job
    parent = ledger.get(parent_id)
    if previous == 'awaiting_publication':
        _job_state(registry, identity, previous)
        return parent
    scope = SimpleNamespace(registry=registry, ledger=ledger, parent=parent)
    _job_state(registry, identity, 'checking')
    try:
        # An interrupted verification is ambiguous. Safe status/import can resume,
        # but checks and reviewer must not silently run a second time after a crash.
        if kind == 'preparation':
            from gateway.task_completion_revision import resume_preparation
            submitted = resume_preparation(registry, ledger, identity, parent_id,
                contract_digest, payload, allow_coverage=previous != 'checking')
            _job_state(registry, identity, 'running')
            if not submitted:
                return parent
            payload = registry._conn.execute('SELECT payload FROM lifecycle_gateway_submissions '
                'WHERE submission_id=?', (identity,)).fetchone()[0]
        result = _reconcile(scope, identity, payload, allow_verification=previous != 'checking')
        phase = result['phase']
        state = 'needs_attention'
        if phase in {'accepted', 'ready', 'running'}:
            state = 'running'
        elif phase in {'verified', 'delivering', 'delivered'}:
            state = 'awaiting_publication'
        _job_state(registry, identity, state,
            'Explicit retry required for interrupted or unsuccessful verification' if state == 'needs_attention' else None)
    except Exception as exc:
        _job_state(registry, identity, 'needs_attention', type(exc).__name__)
        logger.warning('Managed completion requires explicit retry: %s', identity, exc_info=True)
    return parent


def _jobs(registry):
    return registry._conn.execute('''SELECT j.identity,j.parent_id,j.payload,r.phase,j.kind,j.contract_digest
        FROM (
            SELECT submission_id AS identity,parent_id,payload,'submission' AS kind,NULL AS contract_digest
            FROM lifecycle_gateway_submissions
            UNION ALL
            SELECT p.preparation_id,p.parent_id,p.payload,'preparation',p.contract_digest
            FROM lifecycle_gateway_preparations p
            JOIN lifecycle_parent_contracts c ON c.parent_id=p.parent_id AND c.digest=p.contract_digest
            WHERE c.revision=(SELECT MAX(revision) FROM lifecycle_parent_contracts WHERE parent_id=p.parent_id)
              AND NOT EXISTS (SELECT 1 FROM lifecycle_gateway_submissions WHERE submission_id=p.preparation_id)
        ) j LEFT JOIN lifecycle_gateway_recovery r ON r.submission_id=j.identity
        WHERE r.phase IS NULL OR r.phase IN ('running','checking','awaiting_publication')
        ORDER BY COALESCE(r.last_probed,0),j.identity LIMIT 8''').fetchall()


def reconcile_committed():
    """Check each admitted job until execution ends; failed verification needs human retry."""
    from agent.task_lifecycle.registry import Registry
    from agent.task_lifecycle.parent import ParentLedger
    from gateway.task_completion_managed import remote_snapshot_reader
    from gateway.task_completion_modes import effective_mode, publication_decision
    from hermes_cli.config import load_config_readonly
    registry = Registry()
    try:
        exists = registry._conn.execute("SELECT 1 FROM sqlite_master WHERE "
            "name='lifecycle_gateway_submissions'").fetchone()
        if not exists:
            return []
        _schema(registry)
        ledger = ParentLedger(registry, remote_snapshot=remote_snapshot_reader(registry))
        jobs = _jobs(registry)
        ready = {}
        config = load_config_readonly()
        for job in jobs:
            identity, parent_id = job[:2]
            parent = ledger.get(parent_id)
            if effective_mode(registry, parent['profile'], config, parent) == 'support':
                _job_state(registry, identity, 'settled')
                continue
            ready[parent_id] = _resume_job(registry, ledger, job)
        return [(parent, publication_decision(ledger.decision(parent_id),
            effective_mode(registry, parent['profile'], config, parent)))
            for parent_id, parent in ready.items()]
    finally:
        registry.close()


def restored_event(runner, parent, decision):
    """Restore the original saved routing identity, never an identity from worker prose."""
    from gateway.platforms.event import MessageEvent
    store = runner.session_store
    if store is None:
        return None
    with store._lock:
        store._ensure_loaded_locked()
        entry = store._entries.get(parent['session_key'])
    source = runner._restored_source(entry) if entry else None
    if (source is None or str(source.user_id or '') != parent['owner']
            or str(source.profile or 'default') != parent['profile']):
        return None
    event = MessageEvent(text='', source=source, internal=True,
                         message_id='task-completion:' + decision['decision_id'])
    event._completion_decision = decision
    return event


def publication_exists(event, session_key, content):
    from gateway import delivery_ledger
    oid = delivery_ledger.compute_obligation_id(session_key, event.message_id, content)
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as conn:
        if conn.execute('SELECT 1 FROM delivery_obligations WHERE obligation_id=?', (oid,)).fetchone():
            return True
    return decision_owned(event._completion_decision)


def decision_owned(decision):
    from gateway import delivery_ledger
    with delivery_ledger._DB_LOCK, delivery_ledger._transaction() as conn:
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_completion_publications'").fetchone()
        return bool(table and conn.execute('SELECT 1 FROM lifecycle_completion_publications p '
            'JOIN delivery_obligations d ON d.obligation_id=p.obligation_id '
            "WHERE json_extract(p.binding,'$.parent_id')=? AND json_extract(p.binding,'$.decision_id')=?",
            (decision['parent_id'], decision['decision_id'])).fetchone())


def settle_publication(parent_id):
    from agent.task_lifecycle.registry import Registry
    registry = Registry()
    try:
        with registry._lock, registry._conn:
            registry._conn.execute("UPDATE lifecycle_gateway_recovery SET phase='settled' "
                "WHERE phase='awaiting_publication' AND submission_id IN "
                '(SELECT submission_id FROM lifecycle_gateway_submissions WHERE parent_id=?)', (parent_id,))
    finally:
        registry.close()


async def publish_ready(runner, parent, decision):
    if not decision['accepted']:
        return False
    event = await asyncio.to_thread(restored_event, runner, parent, decision)
    if event is None:
        return False
    content = ('[Hermes task: verified; parent ' + parent['parent_id']
               + '; decision ' + decision['decision_id'] + ']')
    if await asyncio.to_thread(publication_exists, event, parent['session_key'], content):
        return False  # Existing outbox owns pending/recovery or delivered state.
    adapter = runner._delivery_adapter_for(event.source)
    if adapter is None:
        return False
    sent, _ = await adapter.send_final_ledgered(event, parent['session_key'], content,
        {'thread_id': event.source.thread_id, 'notify': True}, reply_to=None)
    hook_ctx = dict(session_key=parent['session_key'], profile=parent['profile'],
        user_id=parent['owner'], platform=event.source.platform.value,
        chat_id=event.source.chat_id, response=content, completion_notification=True)
    from gateway.task_completion import hook_completion
    completion = await asyncio.to_thread(hook_completion, hook_ctx)
    await runner.hooks.emit('agent:end', {**hook_ctx, **completion})
    return sent.success


async def recover_profile(runner, home):
    from gateway.run import _async_profile_runtime_scope
    try:
        async with _async_profile_runtime_scope(home):
            for parent, decision in await asyncio.to_thread(reconcile_committed):
                await publish_ready(runner, parent, decision)
                if decision['accepted'] and await asyncio.to_thread(decision_owned, decision):
                    await asyncio.to_thread(settle_publication, parent['parent_id'])
    except Exception:
        logger.warning('Managed completion recovery deferred', exc_info=True)


def schedule_recovery(runner):
    """One bounded recovery per served home; task supervision uses the existing runner."""
    from hermes_constants import get_process_hermes_home
    homes = {get_process_hermes_home(), *(getattr(runner, '_served_profile_homes', None) or {}).values()}
    tasks = getattr(runner, '_completion_recovery_tasks', None)
    if tasks is None:
        tasks = runner._completion_recovery_tasks = {}
    for home in homes:
        key = str(home)
        if key not in tasks or tasks[key].done():
            tasks[key] = runner._retain_background_task(asyncio.create_task(recover_profile(runner, home)))
