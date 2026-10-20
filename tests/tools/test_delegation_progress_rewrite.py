"""Factual rewrite, delivery identity, durable replay and bounded-fallback contracts."""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import auxiliary_client as auxiliary
from hermes_constants import get_hermes_home
from tools import delegation_notifications as notifications
from tools import delegation_progress_rewrite as rewrite
from tools import process_registry_followups as followups
from tools.process_registry import ProcessSession

FACTS = '• src/my\\_file.py: 파일 내용 변경.\n• 테스트 3개 통과. 현재 변경 재검증은 미확인이에요.'
FRESH = 'src/my_file.py 변경이 확인됐어요.\n테스트 3개는 통과했지만 현재 변경 재검증은 미확인이에요.'


@pytest.fixture
def progress(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'launch'))
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    # All configuration writes in this module are disposable test fixtures.
    (home / 'config.yaml').write_text('auxiliary:\n  delegation_progress: {}\n')
    session = ProcessSession(id='proc_rewrite', command='worker', session_key='owner',
        watcher_platform='discord', watcher_chat_id='4242', watcher_thread_id='123',
        remote_connection={'profile_home': str(home)}, delegation_progress={'thread': '123'})
    source = SimpleNamespace(thread_id='123', chat_id='4242')
    send = AsyncMock(return_value=SimpleNamespace(success=True, message_id='receipt'))
    adapter = SimpleNamespace(send=send, _owner_profile=None)
    runner = SimpleNamespace(_build_process_event_source=lambda evt: source,
        _delivery_adapter_for=lambda source: adapter)
    observe = Mock(side_effect=lambda *args, **kw: {'message': {'id': kw.get('admit')}})
    monkeypatch.setattr(notifications, 'remote_observe', observe)
    llm = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=FRESH))]))
    monkeypatch.setattr(auxiliary, 'async_call_llm', llm)
    followups.reserve(session)
    return SimpleNamespace(home=home, session=session, source=source, send=send, adapter=adapter,
        runner=runner, observe=observe, llm=llm,
        message={'id': 'run:1', 'event': 'periodic', 'content': FACTS, 'private': 'do not forward'},
        oid='delegation-progress:proc_rewrite:run:1')


def stored(case):
    with followups._db(read_only=True) as db:
        return dict(db.execute('SELECT * FROM delivery_obligations WHERE obligation_id=?',
            (case.oid,)).fetchone())


async def deliver(case):
    await notifications.send_progress(case.runner, case.session, case.message)
    case.send.assert_awaited_once()
    sent = case.send.call_args.args[1]
    assert stored(case)['content'] == sent
    assert stored(case)['state'] == 'delivered'
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['started', 'periodic', 'needs_user', 'verifying', 'failure', 'cleanup_failed'])
async def test_rewrites_each_send_event_from_only_sanitized_facts(progress, monkeypatch, event):
    case = progress
    case.message['event'] = event
    case.message['content'] += '\nsecret @everyone'
    monkeypatch.setattr(followups, 'scrub_payload', lambda text: text.replace('secret', '[redacted]'))

    async def send(*args, **kwargs):
        # The final body must be durable before the adapter sees it.
        assert stored(case)['content'] == args[1] == FRESH
        return SimpleNamespace(success=True, message_id='receipt')
    case.send.side_effect = send
    assert await deliver(case) == FRESH
    case.llm.assert_awaited_once()
    request = case.llm.call_args.kwargs
    assert request['task'] == 'delegation_progress'
    assert 0 < request['timeout'] <= 20
    assert 'tools' not in request
    assert len(request['messages']) == 2
    assert json.loads(request['messages'][1]['content']) == {
        'event': event, 'fact_sheet': case.message['content'].replace('secret', '[redacted]').replace('@', '@\u200b')}
    assert request['messages'][0]['content'] == rewrite.SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_prompt_preserves_factual_and_uncertainty_contract(progress):
    await deliver(progress)
    prompt = progress.llm.call_args.kwargs['messages'][0]['content']
    for requirement in ('Korean', 'only facts', 'completion, success, passing tests, or verification',
                        '단정하지 않아요', '미확인', '확인 전', 'no emoji or headings', '5 short lines',
                        'Do not invent next steps'):
        assert requirement in prompt


@pytest.mark.asyncio
async def test_exception_falls_back_once_without_body_in_warning(progress, caplog):
    progress.llm.side_effect = RuntimeError('private exception message ' + FACTS)
    with caplog.at_level(logging.WARNING, logger=rewrite.__name__):
        assert await deliver(progress) == FACTS
    assert 'RuntimeError' in caplog.text
    assert 'private exception' not in caplog.text
    assert FACTS not in caplog.text
    assert len([r for r in caplog.records if r.name == rewrite.__name__]) == 1


@pytest.mark.asyncio
async def test_provider_self_cancellation_falls_back(progress, caplog):
    progress.llm.side_effect = asyncio.CancelledError
    with caplog.at_level(logging.WARNING, logger=rewrite.__name__):
        assert await deliver(progress) == FACTS
    assert 'fallback: cancelled' in caplog.text


@pytest.mark.asyncio
async def test_total_timeout_does_not_wait_for_provider_cancellation(progress, monkeypatch, caplog):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def stuck(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()  # emulate an SDK slow to acknowledge cancellation
        return FRESH
    progress.llm.side_effect = stuck
    assert 0 < rewrite.REWRITE_TIMEOUT <= 20
    monkeypatch.setattr(rewrite, 'REWRITE_TIMEOUT', 0.1)
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger=rewrite.__name__):
            assert await asyncio.wait_for(deliver(progress), timeout=5) == FACTS
        assert time.monotonic() - started < 5
        assert entered.is_set()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert 'fallback: timeout' in caplog.text
    finally:
        release.set()
        if rewrite._pending:
            await asyncio.gather(*rewrite._pending, return_exceptions=True)
    assert not rewrite._pending
    progress.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('output', 'reason'), [
    ('', 'empty'), (' \n\t', 'empty'), ('가' * 1201, 'too_long'),
    ('테스트 4개', 'invented_digits'), ('테스트 30개', 'invented_digits'),
    ('src/new_file.py 변경', 'invented_path'), ('new_file.py 변경', 'invented_path'),
    ('new_file.PY를 확인', 'invented_path'), ('/unknown 변경', 'invented_path'),
    ('invented.3 확인', 'invented_path'), ('invented.한글 확인', 'invented_path'),
    ('.env 확인', 'invented_path'), ('@everyone', 'forbidden_marker'),
    ('<@123>', 'forbidden_marker'), ('<#123>', 'forbidden_marker'), ('<@&123>', 'forbidden_marker'),
    ('http://example.com', 'forbidden_marker'), ('https://example.com', 'forbidden_marker'),
    ('www.example.com', 'forbidden_marker'), ('HTTPS://EXAMPLE.COM', 'forbidden_marker'),
    ('MEDIA: image', 'forbidden_marker'),
])
async def test_rejects_invalid_output_with_unchanged_fallback(progress, caplog, output, reason):
    progress.llm.return_value = output
    with caplog.at_level(logging.WARNING, logger=rewrite.__name__):
        assert await deliver(progress) == FACTS
    progress.llm.assert_awaited_once()
    assert f'fallback: {reason}' in caplog.text
    assert FACTS not in caplog.text


@pytest.mark.parametrize('output', [
    'src/my_file.py: 변경 확인, 테스트 3개. 미확인.',
    r'`src/my\_file.py` 변경 확인. 미확인.',
    '가' * 1200,
])
def test_validator_accepts_known_numbers_paths_and_limit(output):
    assert rewrite.validation_error(output, FACTS) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['dict', 'empty_choices', 'reasoning_only'])
async def test_response_shapes_never_publish_reasoning(progress, shape):
    if shape == 'dict':
        progress.llm.return_value = {'choices': [{'message': {'content': FRESH}}]}
    elif shape == 'empty_choices':
        progress.llm.return_value = {'choices': []}
    else:
        progress.llm.return_value = {'choices': [{'message': {'content': '', 'reasoning': FRESH}}]}
    assert await deliver(progress) == (FRESH if shape == 'dict' else FACTS)


@pytest.mark.asyncio
async def test_disabled_makes_no_llm_call_and_escapes_fallback_once(progress, caplog):
    (progress.home / 'config.yaml').write_text('auxiliary:\n  delegation_progress:\n    enabled: false\n')
    progress.message['content'] = '확인 전 @everyone'
    with caplog.at_level(logging.WARNING, logger=rewrite.__name__):
        assert await deliver(progress) == '확인 전 @\u200beveryone'
    progress.llm.assert_not_awaited()
    assert not [r for r in caplog.records if r.name == rewrite.__name__]


@pytest.mark.asyncio
async def test_final_rewrite_is_scrubbed_before_record_and_send(progress, monkeypatch):
    # Model-supplied content still crosses the existing egress scrubber.
    progress.llm.return_value = 'sensitive 새로 확인됐어요.'
    monkeypatch.setattr(followups, 'scrub_payload', lambda text: text.replace('sensitive', '[redacted]'))
    assert await deliver(progress) == '[redacted] 새로 확인됐어요.'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'failed', 'delivered', 'uncertain', 'abandoned', 'attempting'])
async def test_existing_obligation_reuses_recorded_content_without_llm(progress, state):
    case = progress
    notifications._record_progress(obligation_id=case.oid, session_key='owner', platform='discord',
        chat_id='4242', thread_id='123', content='이미 저장된 다른 문체예요.')
    with followups._db() as db:
        db.execute('UPDATE delivery_obligations SET state=? WHERE obligation_id=?', (state, case.oid))
    if state in {'pending', 'failed'}:
        assert await deliver(case) == '이미 저장된 다른 문체예요.'
    else:
        await notifications.send_progress(case.runner, case.session, case.message)
        case.send.assert_not_awaited()
    case.llm.assert_not_awaited()
    assert stored(case)['content'] == '이미 저장된 다른 문체예요.'


@pytest.mark.asyncio
async def test_reentry_after_rewrite_and_unsent_admission_failure_reuses_text(progress):
    case = progress
    case.observe.side_effect = lambda *args, **kw: {'message': {'id': 'new-head'}}
    await notifications.send_progress(case.runner, case.session, case.message)
    assert stored(case)['state'] == 'failed'
    assert stored(case)['content'] == FRESH
    case.send.assert_not_awaited()
    case.observe.side_effect = lambda *args, **kw: {'message': {'id': kw.get('admit')}}
    assert await deliver(case) == FRESH
    # An already delivered duplicate is ACKed, never resent or regenerated.
    await notifications.send_progress(case.runner, case.session, case.message)
    case.llm.assert_awaited_once()
    case.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_reentry_never_rewrites_or_sends_twice(progress):
    case = progress
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(**kwargs):
        entered.set()
        await release.wait()
        return FRESH
    case.llm.side_effect = model
    first = asyncio.create_task(notifications.send_progress(case.runner, case.session, case.message))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await notifications.send_progress(case.runner, case.session, case.message)
        case.llm.assert_awaited_once()
        case.send.assert_not_awaited()
    finally:
        release.set()
        await first
    assert stored(case)['content'] == case.send.call_args.args[1] == FRESH
    case.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_reads_final_ledger_body_after_claim_even_if_reservation_was_stale(progress, monkeypatch):
    case = progress
    rewriting, release_model = asyncio.Event(), asyncio.Event()
    reserved, release_retry = threading.Event(), threading.Event()
    record = notifications._record_progress

    def reserve(**kwargs):
        result = record(**kwargs)
        if not result[1]:
            reserved.set()
            assert release_retry.wait(timeout=5)
        return result
    monkeypatch.setattr(notifications, '_record_progress', reserve)
    async def model(**kwargs):
        rewriting.set()
        await release_model.wait()
        return FRESH
    case.llm.side_effect = model
    case.observe.side_effect = lambda *args, **kw: {'message': {'id': 'superseded'}}
    first = asyncio.create_task(notifications.send_progress(case.runner, case.session, case.message))
    retry = None
    try:
        await asyncio.wait_for(rewriting.wait(), timeout=5)
        retry = asyncio.create_task(notifications.send_progress(case.runner, case.session, case.message))
        assert await asyncio.to_thread(reserved.wait, 5)
        release_model.set()
        await first
        assert stored(case)['content'] == FRESH
        case.observe.side_effect = lambda *args, **kw: {'message': {'id': kw.get('admit')}}
    finally:
        release_model.set()
        release_retry.set()
        await asyncio.gather(*(task for task in (first, retry) if task is not None))
    assert case.send.call_args.args[1] == stored(case)['content'] == FRESH
    case.llm.assert_awaited_once()
    case.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_model_uses_sending_profile_instead_of_inherited_runtime(progress):
    case = progress
    bot_home = case.home / 'profiles' / 'koharu'
    bot_home.mkdir(parents=True)
    (bot_home / 'config.yaml').write_text('model:\n  provider: anthropic\n  default: owner-main-model\n')
    case.adapter._owner_profile = 'koharu'
    case.runner._served_profile_homes = {'koharu': bot_home}
    observed = {}

    async def model(**kwargs):
        # Exercise the same runtime binder the auxiliary entry point uses.
        with auxiliary.scoped_runtime_main(kwargs['main_runtime']):
            observed.update(model=auxiliary._read_main_model(), provider=auxiliary._read_main_provider())
        return FRESH
    case.llm.side_effect = model
    with auxiliary.scoped_runtime_main({'provider': 'custom', 'model': 'wrong-ambient-model'}):
        assert await deliver(case) == FRESH
        assert auxiliary._read_main_model() == 'wrong-ambient-model'
    assert observed == {'model': 'owner-main-model', 'provider': 'anthropic'}


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_sending_profile_controls_enabled_style_model_provider_and_secrets(progress, enabled):
    from agent.secret_scope import get_secret
    case = progress
    bot_home = case.home / 'profiles' / 'koharu'
    bot_home.mkdir(parents=True)
    (case.home / 'config.yaml').write_text('auxiliary:\n  delegation_progress:\n    enabled: false\n    style_prompt: wrong ambient voice\n')
    (bot_home / '.env').write_text('VOICE_TOKEN=owner-secret\n')
    (bot_home / 'config.yaml').write_text(
        'auxiliary:\n  delegation_progress:\n'
        f'    enabled: {str(enabled).lower()}\n'
        '    provider: anthropic\n    model: owner-model\n    style_prompt: 다정한 존댓말을 써요.\n')
    case.adapter._owner_profile = 'koharu'
    case.runner._served_profile_homes = {'koharu': bot_home}
    observed = {}

    async def model(**kwargs):
        observed.update(home=get_hermes_home(), secret=get_secret('VOICE_TOKEN'),
            route=auxiliary._resolve_task_provider_model(kwargs['task'])[:2],
            prompt=kwargs['messages'][0]['content'])
        return FRESH
    case.llm.side_effect = model
    assert await deliver(case) == (FRESH if enabled else FACTS)
    assert case.llm.await_count == int(enabled)
    if enabled:
        assert observed == dict(home=bot_home, secret='owner-secret', route=('anthropic', 'owner-model'),
            prompt=rewrite.SYSTEM_PROMPT + '\n\n다정한 존댓말을 써요.')
    assert get_hermes_home() == case.home
    assert stored(case)['adapter_profile'] == 'koharu'


def test_builtin_slot_has_standard_routing_and_opt_out():
    from hermes_cli.config import DEFAULT_CONFIG
    slot = DEFAULT_CONFIG['auxiliary']['delegation_progress']
    assert {'provider', 'model', 'base_url', 'api_key', 'timeout', 'extra_body'} <= slot.keys()
    assert slot['enabled'] is True
    assert isinstance(slot['style_prompt'], str)
    assert slot['timeout'] <= 20
