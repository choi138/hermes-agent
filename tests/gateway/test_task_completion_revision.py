"""Real harmless workers and checks through gateway revision plumbing; local transport bridge."""
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle import workflow
from agent.task_lifecycle.intake import import_gateway_envelope
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.transport import SSHExecutor
from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.verification import check_files, verify
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.task_completion import admit_request, capture_request, completion_scope
from gateway.task_completion_managed import _prepare, _reconcile, _schema
from gateway.task_completion_revision import resume_preparation
from gateway.task_completion_recovery import reconcile_committed
from tests.waiting import wait_until
from tools.approval_context import set_current_session_key, reset_current_session_key


@pytest.mark.platforms('macos')
def test_gateway_revision_waits_for_old_writer_and_accepts_verified_combined_request(tmp_path, monkeypatch):
    home, repo, output = [tmp_path / name for name in ('profile', 'repo', 'receipts')]
    for directory in (home, repo, output):
        directory.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_cli.config.load_config_readonly',
        lambda: {'completion_gate': {'mode': 'enforce'}})
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    spec = repo / 'SPEC.md'
    spec.write_text('implement feature X')
    release = repo / 'old-release'
    bootstrap = output / 'harmless.py'
    root = Path(__file__).resolve().parents[2]
    bootstrap.write_text('''import sys,subprocess
sys.path.insert(0,sys.argv[1])
from agent.task_lifecycle.workflow import worker
body=''' + repr('''import sys,time
from pathlib import Path
prompt=sys.stdin.read()
if 'feature Y' not in prompt:
    while not Path('old-release').exists(): time.sleep(.02)
Path('x.txt').write_text('X')
if 'feature Y' in prompt: Path('y.txt').write_text('Y')
print('harmless worker exited')
''') + '''
def popen(argv,**kwargs):
    marker=argv.index('--')
    return subprocess.Popen([*argv[:marker+1],sys.executable,'-c',body],**kwargs)
raise SystemExit(worker(sys.argv[2],popen=popen))
''')
    real_submit = workflow.submit
    def spawn(argv, **kwargs):
        return subprocess.Popen([sys.executable, str(bootstrap), str(root), argv[-1]], **kwargs)
    monkeypatch.setattr(workflow, 'submit', lambda grant: real_submit(grant, popen=spawn))
    submitted = []
    def submit(self, envelope):
        receipt = import_gateway_envelope(envelope)
        submitted.append(receipt['run_id'])
        return receipt
    def call(self, action, run_id):
        return workflow.status(run_id) if action == 'status' else verify(run_id)
    monkeypatch.setattr(SSHExecutor, 'submit', submit)
    monkeypatch.setattr(SSHExecutor, 'call', call)
    # Children already occupy this native registry; remote import is covered separately.
    monkeypatch.setattr(SSHExecutor, 'receive', lambda *a, **kw: None)
    coverage = tmp_path / 'coverage.py'
    coverage.write_text('import json,sys\np=json.load(sys.stdin)\nprint(json.dumps(dict(p["binding"], approved=True, missing=[], covered_outcomes=[v["id"] for v in p["contract"]["outcomes"]])))\n')
    checks = [dict(kind='boundary', name=name, argv=[sys.executable, '-c',
        f'from pathlib import Path; assert Path("{name.lower()}.txt").read_text()=="{name}"']) for name in ('X', 'Y')]
    source = SessionSource(platform=Platform.DISCORD, chat_id='isolated', user_id='human')
    def ctx(text, message):
        event = MessageEvent(text=text, source=source, message_id=message)
        admit_request(event, capture_request(event))
        return SimpleNamespace(event=event, source=source, session_key='isolated:revision',
            user_config={'completion_gate': {'mode': 'enforce'}})
    request = workflow.request_data(TaskRequest(spec, repo, repo, output, timeout=15, sandbox='workspace-write'))
    def plan(scope, combined):
        selected = checks if combined else checks[:1]
        outcomes = [dict(item, checks=[dict(kind=check['kind'], name=check['name'])])
            for item, check in zip(scope.parent['contract']['outcomes'], selected, strict=True)]
        return dict(version=1, binding=dict(scope.identity, origin=scope.request.origin, request_text=scope.request.text),
            outcomes=outcomes, outcome_ids=[item['id'] for item in outcomes],
            transport=dict(host='local-test', python=sys.executable, script=str(root / 'scripts/run_codex_task.py'),
                profile_home=str(home), executable_path='/usr/bin:/bin'),
            policy=dict(lane='codex', workdir=str(repo), timeout_seconds=15, approval='not_required'),
            request=request, objective='implement feature X and feature Y' if combined else 'implement feature X',
            checks=selected, artifacts=['x.txt', 'y.txt'] if combined else ['x.txt'],
            spec_text=spec.read_text(), spec_sha256=sha256(spec.read_bytes()).hexdigest(),
            check_files=check_files(selected, repo), coverage=dict(argv=[sys.executable, str(coverage)],
                reviewer='claude', worker_root=str(repo)))
    registry = Registry()
    _schema(registry)
    token = set_current_session_key('isolated:revision')
    try:
        with completion_scope(ctx('implement feature X', 'm1'), lambda: None, registry=registry) as first:
            old_id = _prepare(first, plan(first, False))
            old_payload = registry._conn.execute('SELECT payload FROM lifecycle_gateway_submissions WHERE submission_id=?', (old_id,)).fetchone()[0]
            old = _reconcile(first, old_id, old_payload)
            parent_id = first.parent['parent_id']
            assert not first.decision()['accepted']
        with completion_scope(ctx('also implement feature Y', 'm2'), lambda: None, registry=registry) as revised:
            assert revised.parent['parent_id'] == parent_id
            spec.write_text('implement feature X and feature Y')
            new_id = _prepare(revised, plan(revised, True))
            assert len(submitted) == 1
            assert registry._conn.execute('SELECT count(*) FROM lifecycle_gateway_submissions').fetchone()[0] == 1
            assert not revised.decision()['accepted']
            preparation = registry._conn.execute('SELECT preparation_id,parent_id,contract_digest,payload FROM lifecycle_gateway_preparations WHERE preparation_id=?', (new_id,)).fetchone()
            release.touch()
            assert wait_until(lambda: bool(workflow.status(old['run_id'])['execution']) and not workflow.status(old['run_id'])['worker_alive'], timeout=15)
            with pytest.raises(LifecycleError, match='coverage not run'):
                resume_preparation(registry, revised.ledger, *preparation, allow_coverage=False)
            assert len(submitted) == 1
            # No user status or direct preparation call: the existing watcher's
            # bounded job function resumes this saved authenticated preparation.
            reconcile_committed()
            row = registry._conn.execute('SELECT run_id FROM lifecycle_gateway_submission_receipts WHERE submission_id=?', (new_id,)).fetchone()
            assert row is not None
            new = {'run_id': row[0]}
            assert len(submitted) == 2
            assert new['run_id'] != old['run_id']
            assert wait_until(lambda: bool(workflow.status(new['run_id'])['execution']), timeout=15)
            reconcile_committed()
            accepted = revised.decision()
            assert accepted['accepted'], accepted
            assert registry._conn.execute('SELECT count(*) FROM lifecycle_parent_executions').fetchone()[0] == 2
            assert registry._conn.execute('SELECT count(*) FROM lifecycle_parent_terminal_submissions').fetchone()[0] == 1
            assert registry._conn.execute('SELECT count(*) FROM lifecycle_parent_supersessions').fetchone()[0] == 1
            assert revised.ledger.get(parent_id)['contract']['sources'][1]['text'] == 'also implement feature Y'
    finally:
        release.touch(exist_ok=True)
        for run_id in submitted:
            wait_until(lambda: bool(workflow.status(run_id)['execution']) and not workflow.status(run_id)['worker_alive'], timeout=15)
        reset_current_session_key(token)
        registry.close()
