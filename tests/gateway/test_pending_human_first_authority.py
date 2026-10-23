"""Human-first ordering across the session authority's FIFO and the adapter-slot wake chain.

Human busy messages are admitted to the authority's durable FIFO while internal wakes stay in the
adapter's pending slot, so the slot's human-first pick could not see the human: when a turn ended,
the wake ran as its in-band follow-up (or the adapter's drain started it as a classic turn) and the
authority's human waited behind every wake (3-47 min on Discord). The human now runs first, and the
wake runs after it.
"""
import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from hermes_state_runtime import claim_session_input, list_session_admissions, settle_session_input

CHAT = '15551234567@s.whatsapp.net'


class _RunningAgent:
    def steer(self, text):
        return True

    def interrupt(self, *args):
        pass

    def get_activity_summary(self):
        return {}


async def _setup(tmp_path, monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='human-first')
    runner._wire_adapter_handlers(adapter)

    async def send(chat_id, content, **_):
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_effective_busy_input_mode', lambda source: 'queue')
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm')
    return runner, adapter, authority, source, runner._session_key_for_source(source)


def _wake(source, text):
    return MessageEvent(text=text, source=source, internal=True,
                        metadata={'notification_origin': 'process_registry_synthetic'})


async def _admit_human(runner, authority, source, text):
    """Admit a human message to the authority FIFO; returns its live session id and admission task."""
    task = asyncio.ensure_future(runner._handle_message(
        MessageEvent(text=text, source=source, message_id=f'm-{text}', reply_expected=True)))
    for _ in range(200):
        rows = [row for sid in authority.sessions for row in list_session_admissions(authority.db, session_id=sid)]
        if rows:
            break
        await asyncio.sleep(0.01)
    assert rows and rows[0]['status'] == 'queued', 'the human message never reached the authority FIFO'
    sid = rows[0]['target_session_id']
    # The live drain _schedule would start; a paused FIFO (no drain) must not hold wakes.
    authority.sessions[sid].task = asyncio.get_running_loop().create_future()
    return sid, task


def _claim_and_settle(authority, sid):
    row = claim_session_input(authority.db, epoch=authority.epoch, session_id=sid)
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=row['admission_id'],
                         generation=row['generation'], outcome='completed')
    return row


def _slot_texts(adapter, key):
    from gateway.pending_priority import pending_chain
    return [event.text for event in pending_chain(adapter._pending_messages, key)]


async def _cleanup(authority, task):
    task.cancel()
    for live in authority.sessions.values():
        if live.task is not None and not live.task.done():
            live.task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_turn_end_runs_the_authority_human_before_a_pending_wake(tmp_path, monkeypatch):
    runner, adapter, authority, source, key = await _setup(tmp_path, monkeypatch)
    turn = runner._session_state(key).turn
    turn.agent, turn.event = _RunningAgent(), MessageEvent(text='long task', source=source, message_id='t0')
    sid, admitted = await _admit_human(runner, authority, source, 'approved')
    try:
        for text in ('wake-1', 'wake-2'):
            runner._enqueue_fifo(key, _wake(source, text), adapter)
        result = {'final_response': 'done', 'interrupted': True, 'interrupt_message': 'approved'}

        event, pending = await runner._run_agent_drain_pending(result, adapter, source, key)

        assert (event, pending) == (None, None), 'a wake ran as the follow-up ahead of the queued human'
        assert _slot_texts(adapter, key) == ['wake-1', 'wake-2']

        human = _claim_and_settle(authority, sid)  # the authority's human turn runs next
        event, pending = await runner._run_agent_drain_pending({'final_response': 'ok'}, adapter, source, key)

        assert human['request_id'] == 'm-approved'
        assert pending == 'wake-1\n\nwake-2', 'the wakes must still run (batched) after the human turn'
        assert _slot_texts(adapter, key) == []
    finally:
        await _cleanup(authority, admitted)


@pytest.mark.asyncio
async def test_idle_wake_dispatch_waits_for_the_authority_human_turn(tmp_path, monkeypatch):
    runner, adapter, authority, source, key = await _setup(tmp_path, monkeypatch)
    started = []

    async def run_turn(event, *_):
        started.append(event.text)
        return 'ran'

    monkeypatch.setattr(runner, '_handle_message_with_agent', run_turn)
    sid, admitted = await _admit_human(runner, authority, source, 'approved')
    try:
        wake = _wake(source, 'wake-1')
        wake._gateway_accepted = True  # the adapter acknowledged it when it started processing

        assert await runner._handle_message(wake) is None
        assert started == [] and _slot_texts(adapter, key) == ['wake-1']

        authority.executing[sid] = 1  # claimed: the human turn is starting, the route is not free
        assert await runner._handle_message(adapter._pending_messages.pop(key)) is None
        assert started == [] and _slot_texts(adapter, key) == ['wake-1']

        authority.executing.pop(sid)
        _claim_and_settle(authority, sid)
        await runner._handle_message(adapter._pending_messages.pop(key))

        assert started == ['wake-1'], 'the parked wake was lost instead of running after the human'
    finally:
        await _cleanup(authority, admitted)


@pytest.mark.asyncio
async def test_paused_fifo_does_not_park_wakes(tmp_path, monkeypatch):
    runner, _adapter, authority, source, _key = await _setup(tmp_path, monkeypatch)
    started = []

    async def run_turn(event, *_):
        started.append(event.text)
        return 'ran'

    monkeypatch.setattr(runner, '_handle_message_with_agent', run_turn)
    sid, admitted = await _admit_human(runner, authority, source, 'approved')
    try:
        authority.sessions[sid].task = None  # paused (unknown turn / busy worker): no drain will run it

        await runner._handle_message(_wake(source, 'wake-1'))

        assert started == ['wake-1']
    finally:
        await _cleanup(authority, admitted)


def test_authority_claims_a_human_queued_behind_process_notices(tmp_path):
    from hermes_state import SessionDB
    import hermes_state_runtime as rt
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    try:
        epoch = rt.begin_runtime_epoch(db, instance_id='boot')
        notices = [rt.admit_session_input(db, epoch=epoch, principal_id='automation:route', session_id='s',
                                          request_id=f'notice-{n}', payload={'text': f'notice {n}'})
                   for n in range(3)]
        human = rt.admit_session_input(db, epoch=epoch, principal_id='messaging:user', session_id='s',
                                       request_id='human', payload={'text': 'approved'})
        rows = rt.list_session_admissions(db, session_id='s')
        assert rt.queued_head(rows)['admission_id'] == notices[0]['admission_id']
        head = rt.queued_head(rows, human_first=True)
        assert head['admission_id'] == human['admission_id']
        # A human-first claim validated against the FIFO head is refused, never claims the wrong row.
        assert rt.claim_session_input(db, epoch=epoch, session_id='s', head=notices[0]['admission_id'],
                                      human_first=True) is False
        claimed = rt.claim_session_input(db, epoch=epoch, session_id='s', head=head['admission_id'],
                                         human_first=True)
        assert claimed['admission_id'] == human['admission_id']
        rt.settle_session_input(db, epoch=epoch, admission_id=claimed['admission_id'],
                                generation=claimed['generation'], outcome='completed')
        nxt = rt.claim_session_input(db, epoch=epoch, session_id='s', human_first=True)
        assert nxt['admission_id'] == notices[0]['admission_id'], 'notices keep their FIFO order'
    finally:
        db.close()


async def _live_setup(tmp_path, monkeypatch):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    monkeypatch.setenv('WHATSAPP_ALLOWED_USERS', '15551234567')
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={'session_path': str(tmp_path / 'wa')}))
    adapter.gateway_runner, runner.adapters = runner, {Platform.WHATSAPP: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='human-first-live')
    runner._wire_adapter_handlers(adapter)

    async def send(chat_id, content, **_):
        return SendResult(success=True, message_id='s')

    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(runner, '_effective_busy_input_mode', lambda source: 'queue')
    source = adapter.build_source(chat_id=CHAT, user_id=CHAT, chat_type='dm')
    return runner, adapter, authority, source, runner._session_key_for_source(source)


async def _until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'timed out'
        await asyncio.sleep(0.02)


async def _settle_all(runner, adapter, authority, key):
    def idle():
        drains = [live.task for live in authority.sessions.values() if live.task is not None]
        tasks = [task for task in adapter._session_tasks.values() if task is not None]
        return (all(task.done() for task in drains + tasks) and key not in adapter._pending_messages
                and not runner._is_session_running(key))
    await _until(idle)


async def _shutdown(runner, adapter, authority, *tasks):
    for task in (*tasks, *adapter._session_tasks.values(),
                 *(live.task for live in authority.sessions.values() if live.task is not None)):
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_classic_turn_end_runs_the_human_then_the_wake(tmp_path, monkeypatch):
    runner, adapter, authority, source, key = await _live_setup(tmp_path, monkeypatch)
    order = []

    async def run_turn(event, *_args, **_kw):
        order.append(event.text)
        return f'reply to {event.text}'

    monkeypatch.setattr(runner, '_handle_message_with_agent', run_turn)
    turn = runner._session_state(key).turn
    turn.agent, turn.event = _RunningAgent(), MessageEvent(text='long task', source=source, message_id='t0')
    human = asyncio.ensure_future(runner._handle_message(
        MessageEvent(text='approved', source=source, message_id='m-approved', reply_expected=True)))
    try:
        await _until(lambda: any(list_session_admissions(authority.db, session_id=sid)
                                 for sid in authority.sessions))
        await adapter.handle_message(_wake(source, 'wake-1'))
        await _until(lambda: key in adapter._pending_messages)

        event, pending = await runner._run_agent_drain_pending({'final_response': 'done'}, adapter, source, key)
        assert (event, pending) == (None, None)
        turn.agent = turn.event = None

        await _settle_all(runner, adapter, authority, key)
        assert order == ['approved', 'wake-1']
    finally:
        await _shutdown(runner, adapter, authority, human)


@pytest.mark.asyncio
async def test_wake_left_in_the_slot_by_an_authority_turn_runs_after_the_queued_humans(tmp_path, monkeypatch):
    runner, adapter, authority, source, key = await _live_setup(tmp_path, monkeypatch)
    order = []
    second_queued = asyncio.Event()

    async def run_turn(event, *_args, **_kw):
        order.append(event.text)
        if event.text == 'first':
            await second_queued.wait()
            # The turn-end drain yielded this wake to the queued human; no adapter task owns it.
            runner._enqueue_fifo(key, _wake(source, 'wake-1'), adapter)
        return f'reply to {event.text}'

    monkeypatch.setattr(runner, '_handle_message_with_agent', run_turn)
    tasks = []
    try:
        for text in ('first', 'second'):
            tasks.append(asyncio.ensure_future(runner._handle_message(
                MessageEvent(text=text, source=source, message_id=f'm-{text}', reply_expected=True))))
            await _until(lambda: order or any(
                row['request_id'] == f'm-{text}' for sid in authority.sessions
                for row in list_session_admissions(authority.db, session_id=sid)))
        second_queued.set()

        await _settle_all(runner, adapter, authority, key)
        assert order == ['first', 'second', 'wake-1']
    finally:
        await _shutdown(runner, adapter, authority, *tasks)


@pytest.mark.asyncio
async def test_authority_drain_claims_the_human_before_an_earlier_process_notice(tmp_path, monkeypatch):
    import hermes_state_runtime as rt
    runner, adapter, authority, source, key = await _live_setup(tmp_path, monkeypatch)
    order = []
    release = asyncio.Event()

    async def run_turn(event, *_args, **_kw):
        order.append(event.text)
        if event.text == 'first':
            await release.wait()
        return f'reply to {event.text}'

    monkeypatch.setattr(runner, '_handle_message_with_agent', run_turn)
    tasks = [asyncio.ensure_future(runner._handle_message(
        MessageEvent(text='first', source=source, message_id='m-first', reply_expected=True)))]
    try:
        await _until(lambda: order == ['first'])
        (sid,) = authority.sessions
        first = rt.list_session_admissions(authority.db, session_id=sid)[0]
        notice = rt.admit_session_input(authority.db, epoch=authority.epoch, principal_id='automation:' + key,
                                        session_id=sid, request_id='notice', payload=first['payload'])
        tasks.append(asyncio.ensure_future(runner._handle_message(
            MessageEvent(text='second', source=source, message_id='m-second', reply_expected=True))))
        await _until(lambda: len(rt.list_session_admissions(authority.db, session_id=sid)) == 3)
        release.set()

        await _settle_all(runner, adapter, authority, key)
        rows = {row['request_id']: row for row in
                rt.list_session_admissions(authority.db, session_id=sid, pending_only=False)}
        assert rows['notice']['seq'] < rows['m-second']['seq']
        assert rows['m-second']['generation'] < rows['notice']['generation'], 'the notice ran ahead of the human'
        assert notice['admission_id'] == rows['notice']['admission_id']
    finally:
        await _shutdown(runner, adapter, authority, *tasks)
