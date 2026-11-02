"""Actual serializer/parser/SQLite/continuation contracts with synthetic evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from tools import process_registry_followups as f
from tools import process_registry_continuation as continuation
from tools.process_registry import ProcessSession


class Protocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name})
        env.start(); self.addCleanup(env.stop)

    def execution(self, ident='proc_fixture', request='결과를 확인해 줘'):
        session = ProcessSession(id=ident, command='fixture-command-never-executed', session_key='fixture-owner',
                                 parent_session_id='fixture-parent', exited=True, exit_code=0,
                                 verification_scope={'task_request': request, 'cwd': '/fixture'})
        f.reserve(session)
        row = self.row(ident)
        self.assertTrue(f.admission(ident, row['token']))
        self.assertTrue(f.begin(ident, row['token']))
        return ident, row['token']

    def row(self, ident):
        with f._db(read_only=True) as db:
            return dict(db.execute('SELECT * FROM process_followups WHERE execution_id=?', (ident,)).fetchone())

    def test_roundtrip_three_outcomes_two_languages_and_consumer(self):
        for n, (outcome, evidence, action) in enumerate([
            ('verified', 'artifact contents matched fixture goal', 'none'),
            ('verification_failed', 'terminal unavailable in read-only verification', 'Run the already-authorized fixture test outside this turn'),
            ('verification_failed', 'original goal unavailable after source lookup', 'Recover the original request and completion criteria'),
            ('approval_wait', 'new publication was not authorized', 'Obtain owner authorization for publication'),
        ]):
            for korean in [True, False]:
                with self.subTest(outcome=outcome, korean=korean):
                    ident, token = self.execution(f'proc_fixture_{n}_{korean}')
                    submitted = dict(outcome=outcome, evidence=[evidence], next_action=action)
                    # Model transport fixture: JSON-encoded result envelope, then real parser.
                    response = json.loads(json.dumps({'final_response':
                        '```process_verification\n' + json.dumps(submitted) + '\n```'}))
                    expected = {**submitted, 'repair_attempts': 0}
                    self.assertEqual(f.verification_outcome(response), expected)
                    report = f.verification_report(response, language_hint='확인' if korean else 'Check')
                    self.assertEqual('검증' in report, korean)
                    self.assertNotIn('process_verification', report)
                    self.assertNotIn('"outcome"', report)
                    self.assertIn(evidence, report)
                    self.assertTrue(f.finish(ident, token, response, 'fixture-turn', report_text=report))
                    f.report_state(ident, token, delivered=True)
                    state = self.row(ident)
                    self.assertEqual(state['phase'], 'reported' if outcome == 'verified' else outcome)
                    returned = json.loads(state['payload'])
                    self.assertEqual(returned['verification'], expected)
                    self.assertEqual(returned['report_text'], report)
                    returned['continuation'] = {'chain': 0, 'chain_id': 'fixture', 'origin_received_at': 1}
                    with f._db() as db, patch.object(continuation, 'settings', return_value=(True, 10, 3600)):
                        self.assertEqual(continuation._skip_reason(db, state, returned), '')

    def test_foreground_submitted_and_returned_json_shape(self):
        report = dict(execution_id='proc_fixture', report='테스트 실행은 이 검증 턴에서 막혀 있어.',
                      outcome='verification_failed', evidence=['fixture restriction'], next_action='승인된 테스트를 일반 턴에서 실행')
        result = json.loads(json.dumps({'final_response': '부모 작업은 진행 중이야.\n```process_reports\n' +
                                       json.dumps({'reports': [report]}) + '\n```'}))
        public, reports = f.foreground_reports(result)
        self.assertEqual(public.strip(), '부모 작업은 진행 중이야.')
        self.assertEqual(reports, [('proc_fixture', dict(outcome=report['outcome'], evidence=report['evidence'],
                                                        next_action=report['next_action'], repair_attempts=0), report['report'])])

    def test_original_fencing_cancellation_and_receipt_order(self):
        ident, token = self.execution()
        self.assertFalse(f.finish(ident, 'wrong-token', {}))
        f.cancel(ident)
        result = {'final_response': 'fixture\n```process_verification\n' + json.dumps(
            dict(outcome='verified', evidence=['fixture'], next_action='none')) + '\n```'}
        # A cancelled running turn retains its result but cannot become a delivered success.
        self.assertFalse(f.finish(ident, token, result))
        f.report_state(ident, token, delivered=True)
        self.assertEqual(f.get_state(ident)['phase'], 'cancelled')

    def test_missing_receipt_and_malformed_fences_fail_closed(self):
        ident, token = self.execution()
        block = '```process_verification\n' + json.dumps(dict(outcome='verified', evidence=['fixture'], next_action='none')) + '\n```'
        for text in [block + '\nextra', '````text\n' + block, block + '\n' + block,
                     '{"outcome":"verified"}', '```process_verification\n{}\n```']:
            self.assertEqual(f.verification_outcome({'final_response': text})['outcome'], 'unverified')
            self.assertNotIn('"outcome"', f.verification_report({'final_response': text}))
        self.assertTrue(f.finish(ident, token, {'final_response': block}))
        f.report_state(ident, token, delivered=False)
        self.assertEqual(f.get_state(ident)['phase'], 'needs_reconciliation')

    def test_read_only_allowed_tools_and_zero_repair_budget(self):
        token = f._verification_execution.set('proc_fixture')
        self.addCleanup(f._verification_execution.reset, token)
        for name, args in [('read_file', {}), ('search_files', {}),
                           ('process_manage', {'action': 'log', 'session_id': 'proc_fixture'})]:
            self.assertIsNone(f.verification_tool_block(name, args))
        for name, args in [('terminal', {}), ('process_manage', {'action': 'kill', 'session_id': 'proc_fixture'}),
                           ('process', {'action': 'poll', 'session_id': 'other'})]:
            self.assertIsNotNone(f.verification_tool_block(name, args))
        response = {'final_response': '```process_verification\n' + json.dumps(dict(
            outcome='verified', evidence=['fixture'], next_action='none', repair_attempts=100)) + '\n```'}
        self.assertEqual(f.verification_outcome(response)['repair_attempts'], 0)


class GatewayBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_actual_reconcile_serializes_request_context_into_injection(self):
        from gateway.process_followups import reconcile
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HERMES_HOME': tmp}):
            for index, request in enumerate(['원래 작업 결과를 확인해 줘', 'Check the original task result', '']):
                session = ProcessSession(id=f'proc_boundary_{index}', command='fixture', session_key='fixture-owner',
                                         parent_session_id='', verification_scope={'cwd': '/fixture', 'task_request': request})
                f.reserve(session)
            injections = []
            async def inject(text, evt):
                injections.append(json.loads(json.dumps({'text': text, 'event': evt})))
                return True
            runner = SimpleNamespace(
                _build_process_event_source=lambda _: SimpleNamespace(platform='fixture'),
                _resolve_injection_adapter=lambda *_: SimpleNamespace(supports_async_delivery=True),
                _inject_watch_notification=inject)
            # No authorized continuations exist; disable only the independent config lookup.
            with patch.object(continuation, 'settings', return_value=(False, 10, 3600)):
                await reconcile(runner)
            self.assertEqual(len(injections), 3)
            for index, entry in enumerate(injections):
                self.assertEqual(entry['event']['verification_scope']['task_request'],
                                 ['원래 작업 결과를 확인해 줘', 'Check the original task result', ''][index])
                self.assertEqual(entry['event']['process_followup']['execution_id'], f'proc_boundary_{index}')
                self.assertEqual('완료 기준' in entry['text'], index == 0)
                self.assertIn('verification_failed', entry['text'])
                self.assertIn('approval_wait', entry['text'])

    async def test_attention_notice_actual_ledger_content_and_receipt(self):
        from gateway import process_followups as gateway
        from gateway import delivery_ledger as delivery
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HERMES_HOME': tmp}), \
                patch.object(delivery, '_db_path', return_value=Path(tmp) / 'state.db'), \
                patch('gateway.process_continuations._owner_mention', return_value='@fixture-owner'):
            for index, request in enumerate(['결과 확인해 줘', 'Verify the result']):
                session = ProcessSession(id=f'proc_attention_{index}', command='fixture', session_key='fixture',
                                         verification_scope={'task_request': request, 'cwd': '/fixture'})
                f.reserve(session)
                row = next(r for r in f.pending() if r['execution_id'] == session.id)
                self.assertTrue(f.admission(session.id, row['token']))
                self.assertTrue(f.begin(session.id, row['token']))
                f.require_reconciliation(session.id, row['token'], reason='synthetic owner unavailable')
                with f._db(read_only=True) as db:
                    row = dict(db.execute('select * from process_followups where execution_id=?', (session.id,)).fetchone())
                evt = json.loads(row['payload'])
                source = SimpleNamespace(platform='fixture', chat_id='synthetic-chat', thread_id=None, chat_type='dm')
                runner = SimpleNamespace(_thread_metadata_for_target=lambda *_args, **_kw: {})
                transport = AsyncMock(return_value=SimpleNamespace(success=True))
                with patch.object(gateway, '_send_attention', transport):
                    await gateway._report_attention(runner, SimpleNamespace(), source, row, evt)
                self.assertEqual(transport.await_count, 1)
                content = transport.await_args.args[2]
                self.assertEqual('검증 기록' in content, index == 0)
                self.assertIn('@fixture-owner', content)
                self.assertIn('synthetic owner unavailable', content)
                turn_id = f"process-attention:{session.id}:{row['token']}"
                self.assertEqual(delivery.turn_delivery_state('fixture', turn_id), 'delivered')
                with f._db(read_only=True) as db:
                    saved = dict(db.execute('select content,state from delivery_obligations where obligation_id=?', (turn_id,)).fetchone())
                self.assertEqual(saved, {'content': content, 'state': 'delivered'})
                self.assertEqual(f.get_state(session.id)['phase'], 'needs_reconciliation_reported')


if __name__ == '__main__':
    unittest.main()
