"""Behavioral review reproductions; real lifecycle/adapter with an in-memory SDK boundary."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.agent.task_lifecycle.test_workflow import ROOT, case, detached_popen, await_exit
from agent.task_lifecycle import workflow
from agent.task_lifecycle.handoff import queue_result, deliver_result
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.verification import verify, run_lock
from gateway import delivery_ledger


def verified_run(case):
    spawn = detached_popen(case, 'import sys; sys.stdin.read(); print("done")')
    run_id = workflow.submit(case['grant'], popen=spawn)['run_id']
    await_exit(run_id)
    assert verify(run_id)['accepted']
    return run_id


class Channel:
    id = 123

    def __init__(self, *, lose_ack=False):
        self.messages = []
        self.lose_ack = lose_ack

    async def send(self, *, content, files=(), file=None, **kwargs):
        attachments = []
        for item in [*files, *([file] if file else [])]:
            data = item.fp.read()
            attachments.append(SimpleNamespace(filename=item.filename, size=len(data),
                                                read=AsyncMock(return_value=data)))
            item.close()
        message = SimpleNamespace(id=900 + len(self.messages), author=SimpleNamespace(id=1),
                                  channel=self, content=content, attachments=attachments)
        self.messages.append(message)
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError('Server accepted message, client lost ACK')
        return message

    async def fetch_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)

    async def history(self, *, limit):
        for message in reversed(self.messages[-limit:]):
            yield message


def discord_adapter(channel):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = DiscordAdapter(PlatformConfig())
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=1), get_channel=lambda _: channel)
    return adapter


def gateway_runner(adapter):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: 'default'
    runner.session_store = None
    runner._async_session_store = SimpleNamespace(_store=None, finish_active_turn=AsyncMock())
    return runner


def claimed_row(oid, session_key='agent:default:discord:thread:123'):
    return dict(obligation_id=oid, session_key=session_key, platform='discord', chat_id='123',
                thread_id='123', content='ordinary result', attempts=1)


@pytest.mark.asyncio
async def test_review_discord_table_keeps_exact_delivery_content(case):
    run_id = verified_run(case)
    content = '| State | Count |\n| --- | --- |\n| Verified | 1 |'
    oid = queue_result(run_id, content)
    channel = Channel()
    assert await deliver_result(oid, discord_adapter(channel), adapter_profile='default')
    assert channel.messages[0].content == content + f'\n\n참조: {run_id}'
    assert len(channel.messages) == 1
    duplicate = workflow.submit(case['grant'])
    assert duplicate['run_id'] == run_id and duplicate['complete']


@pytest.mark.asyncio
async def test_review_lost_attachment_ack_does_not_send_fallback(case):
    run_id = verified_run(case)
    oid = queue_result(run_id, 'verified result', attachments=['answer.json'])
    channel = Channel(lose_ack=True)
    adapter = discord_adapter(channel)
    assert not await deliver_result(oid, adapter, adapter_profile='default')
    assert len(channel.messages) == 1
    assert await deliver_result(oid, adapter, adapter_profile='default')
    assert len(channel.messages) == 1


@pytest.mark.asyncio
async def test_review_gateway_lifecycle_retires_active_turn(case):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource, SessionStore
    run_id = verified_run(case)
    oid = queue_result(run_id, 'verified result')
    runner = gateway_runner(discord_adapter(Channel()))
    sessions = case['home'] / 'sessions'
    source = SessionSource(platform=Platform.DISCORD, chat_id='123', thread_id='123', user_id='operator')
    store = SessionStore(sessions, GatewayConfig())
    entry = store.get_or_create_session(source)
    assert store.begin_active_turn(entry.session_key, 'finished-turn', 'previous-boot')
    runner.session_store = store
    row = claimed_row(oid, session_key=entry.session_key)
    assert await runner._redeliver_claimed_obligations([row]) == 1
    reopened = SessionStore(sessions, GatewayConfig())
    assert reopened.get_entry(entry.session_key).active_turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_state', ['pending', 'attempting', 'failed'])
async def test_review_gateway_busy_lifecycle_does_not_abort_other_delivery(case, initial_state):
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionStore
    run_id = verified_run(case)
    # Record in a real process that exits, so startup must claim a dead owner.
    bootstrap = '''import sys
from agent.task_lifecycle.handoff import queue_result
from gateway import delivery_ledger
oid = queue_result(sys.argv[1], 'verified result')
if sys.argv[2] == 'attempting':
    delivery_ledger.mark_attempting(oid)
elif sys.argv[2] == 'failed':
    delivery_ledger.mark_failed(oid, 'previous failure')
delivery_ledger.record_obligation(obligation_id='ordinary', session_key='ordinary-session',
    platform='discord', chat_id='123', thread_id='123', content='ordinary result')
print(oid)
'''
    child = subprocess.run([sys.executable, '-c', bootstrap, run_id, initial_state], cwd=ROOT,
                           env=os.environ, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    oid = child.stdout.strip()
    channel = Channel()
    runner = gateway_runner(discord_adapter(channel))
    runner.session_store = SessionStore(case['home'] / 'sessions', GatewayConfig())
    claimed = await runner._claim_pending_obligations()
    assert {row['obligation_id'] for row in claimed} == {oid, 'ordinary'}
    claimed.sort(key=lambda row: row['obligation_id'] != oid)
    with run_lock(run_id):
        assert await runner._redeliver_claimed_obligations(claimed) == 1
    assert [m.content for m in channel.messages] == ['ordinary result']
    assert not workflow.status(run_id)['complete']
    deferred = next(row for row in json.loads(delivery_ledger.debug_rows()) if row['id'] == oid)
    assert deferred['state'] == 'failed' and deferred['last_error'] == 'lifecycle_deferred'
    assert deferred['attempts'] == 1
    # A second lock conflict on reconnect stays retryable without refunding
    # the bounded budget; the next reconnect succeeds in the same process.
    with run_lock(run_id):
        assert await runner._redeliver_failed_obligations_for_platform(Platform.DISCORD) == 0
    deferred = next(row for row in json.loads(delivery_ledger.debug_rows()) if row['id'] == oid)
    assert deferred['state'] == 'failed' and deferred['attempts'] == 2
    assert not delivery_ledger.defer_lifecycle_claim(oid, attempts=1)
    assert await runner._redeliver_failed_obligations_for_platform(Platform.DISCORD) == 1
    assert workflow.status(run_id)['complete']
    assert not delivery_ledger.defer_lifecycle_claim(oid, attempts=3)
    assert [m.content for m in channel.messages] == [
        'ordinary result', 'verified result' + f'\n\n참조: {run_id}']
    assert await runner._redeliver_failed_obligations_for_platform(Platform.DISCORD) == 0
    assert len(channel.messages) == 2


def test_review_lifecycle_deferral_preserves_foreign_owner_and_retry_cap(case):
    run_id = verified_run(case)
    oid = queue_result(run_id, 'verified result')
    bootstrap = '''import sys
from gateway.delivery_ledger import defer_lifecycle_claim
assert not defer_lifecycle_claim(sys.argv[1], attempts=0)
'''
    child = subprocess.run([sys.executable, '-c', bootstrap, oid], cwd=ROOT,
                           env=os.environ, capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    row = next(row for row in json.loads(delivery_ledger.debug_rows()) if row['id'] == oid)
    assert row['state'] == 'pending' and row['attempts'] == 0
    assert delivery_ledger.defer_lifecycle_claim(oid, attempts=0)
    for attempt in range(1, delivery_ledger.MAX_ATTEMPTS + 1):
        claimed = delivery_ledger.sweep_failed_for_runtime('discord')
        assert len(claimed) == 1 and claimed[0]['attempts'] == attempt
        assert delivery_ledger.defer_lifecycle_claim(oid, attempts=attempt)
    assert delivery_ledger.sweep_failed_for_runtime('discord') == []
    assert not delivery_ledger.defer_lifecycle_claim(oid, attempts=delivery_ledger.MAX_ATTEMPTS)
    row = next(row for row in json.loads(delivery_ledger.debug_rows()) if row['id'] == oid)
    assert row['state'] == 'abandoned' and row['attempts'] == delivery_ledger.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_review_duplicate_remote_import_reports_delivered(case, monkeypatch, tmp_path):
    from agent.task_lifecycle.remote_result import export_result, receive_result
    run_id = verified_run(case)
    package = export_result(run_id, content='verified result')
    grant, contract = package['grant'], package['contract']
    envelope = {k:contract[k] for k in ('owner','origin','request_revision','profile','request_text','objective','approval_ref')}
    envelope.update({k:grant[k] for k in ('request','checks','artifacts','destination','context','spec_sha256')})
    envelope['forbidden_actions'] = list(contract['forbidden_actions'])
    home = tmp_path / 'receiving-profile'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    args = dict(envelope=envelope, expected_run_id=run_id, content='verified result')
    imported = receive_result(package, **args)
    assert await deliver_result(imported['obligation_id'], discord_adapter(Channel()), adapter_profile='default')
    duplicate = receive_result(package, **args)
    assert duplicate['phase'] == 'delivered'
    assert duplicate['complete'] is True


def correction_run(case, check_body):
    import sys
    from agent.task_lifecycle.corrections import PersistentCorrectionLedger
    from agent.task_lifecycle.intake import create_grant
    ledger = PersistentCorrectionLedger()
    ledger.record(correction_id='review-json', revision=1, source='fixture:user:correction',
        rule_text='Use the required JSON status', confirmed=True,
        scope=dict(owner='operator', project=str(case['repo']), profile='default'),
        environments=[str(case['repo']) + ':codex'])
    grant = create_grant(request=case['request'], request_text='JSON correction check', objective='Check JSON',
        owner='operator', origin='local:review:correction', request_revision='1', profile='default',
        checks=[dict(name='status', kind='boundary', argv=[sys.executable, '-c', check_body],
                     correction={'id':'review-json', 'revision':1})], artifacts=['answer.json'],
        correction_ids=['review-json'])
    spawn = detached_popen(case, 'import sys; sys.stdin.read(); print("done")')
    run_id = workflow.submit(grant, popen=spawn)['run_id']
    await_exit(run_id)
    return ledger, run_id


def test_review_stale_behavior_check_cannot_claim_correction_followed(case):
    ledger, run_id = correction_run(case,
        'from pathlib import Path; p=Path("answer.json"); assert p.read_text(); p.write_text("changed after check")')
    try:
        assert not verify(run_id)['accepted']
        assert not ledger.status('review-json')['followed_everywhere']
    finally:
        ledger.close()


def test_review_reverified_correction_uses_latest_behavior_and_keeps_history(case):
    original = (case['repo'] / 'answer.json').read_bytes()
    (case['repo'] / 'answer.json').write_text('{}')
    ledger, run_id = correction_run(case,
        'import json; assert json.load(open("answer.json")).get("status") == "전달 미확인"')
    try:
        assert not verify(run_id)['accepted']
        assert not ledger.status('review-json')['followed_everywhere']
        (case['repo'] / 'answer.json').write_bytes(original)
        assert verify(run_id)['accepted']
        assert ledger.status('review-json')['followed_everywhere']
        rows = ledger._conn.execute('SELECT data FROM lifecycle_correction_evidence WHERE correction_id=?',
                                    ('review-json',)).fetchall()
        assert any(json.loads(data).get('observed') == 'violated' for (data,) in rows)
    finally:
        ledger.close()


def test_review_crash_after_claim_before_ready_is_unknown_without_replay(case):
    grant, contract, _, _ = workflow.load_grant(case['grant'])
    registry = Registry()
    try:
        run_id = registry.submit(contract).run_id
        registry.prepare_job(run_id, grant)
    finally:
        registry.close()
    assert workflow.status(run_id)['phase'] == 'accepted'
    bootstrap = '''import os, sys
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.workflow import process_identity
registry = Registry()
assert registry.claim_job(sys.argv[1], 'owned-before-crash', process_identity(os.getpid()))
os._exit(91)
'''
    child = subprocess.run([sys.executable, '-c', bootstrap, run_id], cwd=ROOT,
                           env=os.environ, capture_output=True, text=True, timeout=20)
    assert child.returncode == 91, child.stderr
    state = workflow.status(run_id)
    assert state['phase'] == 'unknown'
    assert state['recovery'] == 'inspect_receipts_do_not_replay'
    launched = []
    duplicate = workflow.submit(case['grant'], popen=lambda *a, **kw: launched.append(a))
    assert duplicate['run_id'] == run_id and duplicate['phase'] == 'unknown'
    assert not launched
