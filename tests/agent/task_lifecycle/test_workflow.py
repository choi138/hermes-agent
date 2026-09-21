"""Real detached supervisors and harmless Python workloads; no installed LLM calls."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from agent.codex_task_runner import TaskRequest
from agent.task_lifecycle.intake import create_grant
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.types import LifecycleError, Phase
from agent.task_lifecycle import workflow
from agent.task_lifecycle.verification import verify

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def case(tmp_path, monkeypatch):
    # Only tests needing a real Mac grant/bootstrap use this fixture; portable
    # transport/registry/contract tests remain active on Linux.
    if sys.platform != "darwin":
        pytest.skip("Mac grant and subprocess handoff require Darwin openat/fchdir/kqueue")
    home = tmp_path / 'profile'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME', str(home))
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    spec = repo / 'SPEC.md'
    spec.write_text('실행 종료만으로 완료라고 하지 마세요.')
    artifact = repo / 'answer.json'
    artifact.write_text('{"status":"전달 미확인"}')
    output = tmp_path / 'receipts'
    output.mkdir(mode=0o700)
    request = TaskRequest(spec, repo, repo, output, timeout=15)
    check = {'name': 'json-boundary', 'kind': 'boundary', 'argv': [sys.executable, '-c',
        'import json; assert json.load(open("answer.json"))["status"] == "전달 미확인"']}
    grant = create_grant(request=request, request_text=spec.read_text(), objective='상태 응답 검증',
        owner='operator', origin='discord:thread:123:message:456', request_revision='1', profile='default',
        checks=[check], artifacts=['answer.json'], destination={
            'session_key':'agent:default:discord:thread:123', 'platform':'discord',
            'channel_id':'123', 'thread_id':'123', 'adapter_profile':'default'})
    return dict(home=home, repo=repo, output=output, grant=grant, request=request)


def detached_popen(case, body):
    child = case['output'] / 'harmless.py'
    child.write_text(body)
    bootstrap = case['output'] / 'worker_bootstrap.py'
    bootstrap.write_text('''import sys, subprocess
sys.path.insert(0, sys.argv[1])
from agent.task_lifecycle.workflow import worker
child, run_id = sys.argv[2:4]
def popen(argv, **kwargs):
    marker = argv.index('--')
    return subprocess.Popen([*argv[:marker+1], sys.executable, child], **kwargs)
raise SystemExit(worker(run_id, popen=popen))
''')
    def spawn(argv, **kwargs):
        return subprocess.Popen([sys.executable, str(bootstrap), str(ROOT), str(child), argv[-1]], **kwargs)
    return spawn


def await_exit(run_id):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = workflow.status(run_id)
        if result['execution']:
            return result
        time.sleep(.05)
    raise AssertionError(f'No durable receipt: {workflow.status(run_id)}')


def test_detached_duplicate_receipts_and_restart_verification(case):
    spawn = detached_popen(case, 'import sys,time; data=sys.stdin.read(); time.sleep(.3); print(data)')
    submitted = workflow.submit(case['grant'], popen=spawn)
    duplicate = workflow.submit(case['grant'], popen=spawn)
    assert submitted['run_id'] == duplicate['run_id']
    result = await_exit(submitted['run_id'])
    assert result['phase'] == 'execution_finished' and not result['complete']
    assert result['execution']['input_receipt']['pipe_complete']
    assert len(list(case['output'].glob('codex-task-*'))) == 1
    assert verify(submitted['run_id'])['accepted']
    # Independent CLI process recovers state, not the former caller's cache.
    command = [sys.executable, str(ROOT/'scripts/run_codex_task.py'), 'lifecycle', 'verify', submitted['run_id']]
    readback = subprocess.run(command, capture_output=True, text=True, timeout=20, env=os.environ)
    assert readback.returncode == 0, readback.stderr
    assert json.loads(readback.stdout)['accepted']
    (case['repo']/'answer.json').write_text('{"status":"완료"}')
    with pytest.raises(LifecycleError, match='changed'):
        verify(submitted['run_id'])


def test_cancel_is_mailbox_for_owned_supervisor(case):
    spawn = detached_popen(case, 'import sys,time; sys.stdin.read(); time.sleep(30)')
    run_id = workflow.submit(case['grant'], popen=spawn)['run_id']
    assert workflow.cancel(run_id)['cancel_requested']
    result = await_exit(run_id)
    assert result['execution']['status'] == 'cancelled'
    assert not result['complete']


def test_tampered_grant_and_cross_profile_are_rejected(case, monkeypatch, tmp_path):
    grant = json.loads(case['grant'].read_text())
    grant['prompt'] += 'injected scope expansion'
    case['grant'].write_text(json.dumps(grant))
    with pytest.raises(LifecycleError, match='digest'):
        workflow.submit(case['grant'])
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'other-profile'))
    with pytest.raises(LifecycleError, match='profile grant'):
        workflow.submit(case['grant'])


def test_claim_generation_and_unknown_receipt_recovery(case):
    grant, contract, _, _ = workflow.load_grant(case['grant'])
    registry = Registry()
    try:
        run_id = registry.submit(contract).run_id
        registry.prepare_job(run_id, grant)
        assert registry.claim_job(run_id, 'first', workflow.process_identity(os.getpid()))
        assert not registry.claim_job(run_id, 'second', workflow.process_identity(os.getpid()))
        with pytest.raises(LifecycleError, match='generation'):
            registry.finish_job(run_id, 'second', {'process_returncode':0})
        registry.mark_ready(run_id)
        registry.mark_running(run_id, start_evidence={'pid':os.getpid(), 'started_at':time.time(), 'executor':'test'})
        registry.record_phase(run_id, Phase.UNKNOWN)
        assert run_id in [r.run_id for r in registry.open_runs()]
        registry.finish_job(run_id, 'first', {'status':'cli_completed','exit_code':0,'process_returncode':0})
    finally:
        registry.close()
    assert workflow.status(run_id)['phase'] == 'execution_finished'


@pytest.mark.asyncio
async def test_actual_ledger_finalizer_readback_and_ambiguous_send(case):
    import hashlib
    from types import SimpleNamespace
    from agent.task_lifecycle.handoff import queue_result, deliver_result
    spawn = detached_popen(case, 'import sys; sys.stdin.read(); print("done")')
    run_id = workflow.submit(case['grant'], popen=spawn)['run_id']
    await_exit(run_id)
    verify(run_id)
    oid = queue_result(run_id, '검증한 JSON 결과입니다.', attachments=['answer.json'])
    assert queue_result(run_id, '검증한 JSON 결과입니다.', attachments=['answer.json']) == oid

    class Adapter:
        sends = 0
        wrong_thread = True
        async def send_lifecycle_result(self, **kw):
            self.sends += 1
            self.sent = kw
            return SimpleNamespace(success=True, message_id='999')
        async def read_lifecycle_result(self, target, message_id):
            data = self.sent['attachment']['data']
            return dict(message_id=message_id, channel_id='wrong' if self.wrong_thread else target,
                content_digest=hashlib.sha256(self.sent['content'].encode()).hexdigest(),
                attachments=[dict(name=self.sent['attachment']['name'],bytes=len(data),sha256=hashlib.sha256(data).hexdigest())])

    adapter = Adapter()
    assert not await deliver_result(oid, adapter, adapter_profile='default')
    assert workflow.status(run_id)['phase'] == 'delivery_unconfirmed'
    adapter.wrong_thread = False
    assert await deliver_result(oid, adapter, adapter_profile='default')
    assert await deliver_result(oid, adapter, adapter_profile='default')
    assert adapter.sends == 1
    assert workflow.status(run_id)['complete']
    with pytest.raises(LifecycleError, match='profile'):
        await deliver_result(oid, adapter, adapter_profile='other')


def test_gateway_intake_uses_original_source_and_mac_spec_digest(case, monkeypatch):
    import hashlib
    from gateway.config import Platform
    from gateway.session import SessionSource
    from agent.direct_agent_policy import ExecutionDecision
    from tools.approval import set_current_session_key, reset_current_session_key
    from agent.task_lifecycle.intake import gateway_envelope, import_gateway_envelope
    decision=ExecutionDecision(lane='codex',host='mac',workdir=str(case['request'].workdir),
        permissions='read_only',timeout_seconds=60,approval='not_required',refusal_reason=None,policy_trace=())
    source=SessionSource(platform=Platform.DISCORD,chat_id='123',thread_id='123',user_id='actual-user',message_id='456')
    args=dict(source=source,session_key='authenticated-turn',request_revision='2',request_text='original user request',
        decision=decision,request=workflow.request_data(case['request']),objective='bounded task',
        checks=[dict(kind='test',name='spec',argv=['/usr/bin/test','-f','SPEC.md'])],artifacts=['SPEC.md'],
        spec_sha256=hashlib.sha256(case['request'].spec.read_bytes()).hexdigest())
    with pytest.raises(LifecycleError,match='authenticated'):
        gateway_envelope(**args)
    token=set_current_session_key('authenticated-turn')
    try:
        envelope=gateway_envelope(**args)
        assert envelope['owner']=='actual-user'
        assert envelope['origin']=='authenticated-turn:message:456'
        assert envelope['destination']['thread_id']=='123'
        source.is_bot=True
        with pytest.raises(LifecycleError,match='authenticated'):
            gateway_envelope(**args)
        source.is_bot=False
        (case['repo']/'SPEC.md').write_text('changed after gateway approval')
        with pytest.raises(LifecycleError,match='approved bytes'):
            import_gateway_envelope(envelope)
    finally:
        reset_current_session_key(token)


def test_required_check_script_changes_cannot_reuse_pass(case):
    script=case['output']/'checker.py'
    script.write_text('print("check ran")')
    request=case['request']
    grant=create_grant(request=request,request_text='Check',objective='Check',owner='operator',
        origin='local:checks',request_revision='1',profile='default',
        checks=[dict(name='bound-check',kind='test',argv=[sys.executable,str(script)])],artifacts=['answer.json'])
    spawn=detached_popen(case,'import sys; sys.stdin.read(); print("done")')
    run_id=workflow.submit(grant,popen=spawn)['run_id']
    await_exit(run_id)
    script.write_text('print("substituted checker")')
    with pytest.raises(LifecycleError,match='changed since approval'):
        verify(run_id)


def test_remote_transport_preserves_literal_shell_arguments(monkeypatch):
    from types import SimpleNamespace
    import shlex
    from agent.task_lifecycle.transport import SSHExecutor
    seen=[]
    def run(argv,**kw):
        seen.append((argv,kw))
        return SimpleNamespace(returncode=0,stdout='{"phase":"running"}')
    monkeypatch.setattr(subprocess,'run',run)
    transport=SSHExecutor('mac-alias','/usr/bin/python3','/repo dir/run.py','/profile dir', '/usr/bin:/bin')
    literal='$(touch SHOULD_NOT_EXIST); `whoami`'
    assert transport.call('status',literal)['phase']=='running'
    actual=shlex.split(seen[0][0][-1])
    assert actual[-1]==literal
    assert actual[-3:] == ['lifecycle','status',literal]


def test_verified_remote_result_imports_into_original_profile_without_mac_paths(case, monkeypatch, tmp_path):
    from agent.task_lifecycle.remote_result import export_result, receive_result
    spawn=detached_popen(case,'import sys; sys.stdin.read(); print("done")')
    run_id=workflow.submit(case['grant'],popen=spawn)['run_id']
    await_exit(run_id)
    verify(run_id)
    package=export_result(run_id,content='검증 결과',attachments=['answer.json'])
    grant=package['grant'];contract=package['contract']
    envelope={k:contract[k] for k in ('owner','origin','request_revision','profile','request_text','objective','approval_ref')}
    envelope.update({k:grant[k] for k in ('request','checks','artifacts','destination','context','spec_sha256')})
    envelope['forbidden_actions']=list(contract['forbidden_actions'])
    home=tmp_path/'gateway-profile';home.mkdir(mode=0o700)
    monkeypatch.setenv('HERMES_HOME',str(home))
    case['repo'].rename(tmp_path/'mac-only-repo')
    assert not case['repo'].exists()
    imported=receive_result(package,envelope=envelope,expected_run_id=run_id,content='검증 결과',attachments=['answer.json'])
    duplicate=receive_result(package,envelope=envelope,expected_run_id=run_id,content='검증 결과',attachments=['answer.json'])
    assert imported==duplicate
    assert imported['phase']=='delivering' and not imported['complete']
    assert (home/'lifecycle/received'/run_id/'answer.json').read_bytes()=='{"status":"전달 미확인"}'.encode()
    wrong={**envelope,'owner':'another-user'}
    with pytest.raises(LifecycleError,match='identity'):
        receive_result(package,envelope=wrong,expected_run_id=run_id,content='검증 결과',attachments=['answer.json'])
    tampered=json.loads(json.dumps(package));tampered['attachments'][0]['data']='e30='
    with pytest.raises(LifecycleError,match='Downloaded'):
        receive_result(tampered,envelope=envelope,expected_run_id=run_id,content='검증 결과',attachments=['answer.json'])


def test_registry_rejects_managed_completion_without_acceptance_receipts(case):
    grant,contract,_,_=workflow.load_grant(case['grant'])
    registry=Registry()
    try:
        run_id=registry.submit(contract).run_id
        registry.prepare_job(run_id,grant)
        registry.mark_ready(run_id)
        registry.mark_running(run_id,start_evidence={'pid':1,'started_at':123.,'executor':'test'})
        registry.mark_execution_finished(run_id,exit_code=0)
        registry.record_phase(run_id,Phase.VERIFYING,evidence={'verification_ref':run_id})
        with pytest.raises(LifecycleError,match='durable acceptance'):
            registry.record_phase(run_id,Phase.VERIFIED,evidence={'artifact_revision':'invented','acceptance_digest':'invented'})
    finally:
        registry.close()


def test_attachment_copy_rejects_symlink_replacement_and_oversize(case, tmp_path):
    from agent.task_lifecycle.verification import snapshot, read_artifact
    expected = snapshot(case['repo'], ['answer.json'])['files']['answer.json']
    assert read_artifact(case['repo'], 'answer.json', expected)
    with pytest.raises(LifecycleError, match='bounded'):
        read_artifact(case['repo'], 'answer.json', expected, limit=1)
    # Identical outside bytes must not make a substituted symlink acceptable.
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'answer.json').write_bytes((case['repo']/'answer.json').read_bytes())
    (case['repo']/'answer.json').unlink()
    (case['repo']/'answer.json').symlink_to(outside/'answer.json')
    with pytest.raises(LifecycleError, match='without symlinks'):
        read_artifact(case['repo'], 'answer.json', expected)
    (case['repo']/'subdir').symlink_to(outside, target_is_directory=True)
    with pytest.raises(LifecycleError, match='without symlinks'):
        read_artifact(case['repo'], 'subdir/answer.json', expected)


@pytest.mark.parametrize('crash_stage', ['ready', 'handoff'])
def test_remote_import_recovers_hard_crash_without_second_execution(case, tmp_path, crash_stage):
    from agent.task_lifecycle.remote_result import export_result
    spawn = detached_popen(case, 'import sys; sys.stdin.read(); print("done")')
    run_id = workflow.submit(case['grant'], popen=spawn)['run_id']
    await_exit(run_id)
    verify(run_id)
    package = export_result(run_id, content='검증 결과', attachments=['answer.json'])
    grant, contract = package['grant'], package['contract']
    envelope = {k:contract[k] for k in ('owner','origin','request_revision','profile','request_text','objective','approval_ref')}
    envelope.update({k:grant[k] for k in ('request','checks','artifacts','destination','context','spec_sha256')})
    envelope['forbidden_actions'] = list(contract['forbidden_actions'])
    payload = tmp_path/'import.json'
    payload.write_text(json.dumps(dict(package=package, envelope=envelope, expected_run_id=run_id,
        content='검증 결과', attachments=['answer.json'])))
    home = tmp_path/'source-profile'
    home.mkdir(mode=0o700)
    bootstrap = '''import json, os, sys
from agent.task_lifecycle.registry import Registry
from agent.task_lifecycle.remote_result import receive_result
from gateway import delivery_ledger
stage = sys.argv[2]
if stage == 'ready':
    original = Registry.mark_ready
    def crash(self, run_id):
        original(self, run_id)
        os._exit(91)
    Registry.mark_ready = crash
elif stage == 'handoff':
    delivery_ledger.record_obligation = lambda **kw: os._exit(91)
with open(sys.argv[1]) as source:
    result = receive_result(**json.load(source))
print(json.dumps(result))
'''
    def invoke(stage):
        return subprocess.run([sys.executable, '-c', bootstrap, str(payload), stage], cwd=ROOT,
            env={**os.environ, 'HERMES_HOME':str(home)}, capture_output=True, text=True, timeout=20)
    assert invoke(crash_stage).returncode == 91
    resumed = invoke('resume')
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads(resumed.stdout)
    assert result['phase'] == 'delivering' and not result['complete']
    assert json.loads(invoke('duplicate').stdout) == result
    assert len(list(case['output'].glob('codex-task-*'))) == 1
    registry = Registry(home/'state.db')
    try:
        assert len(registry.open_runs()) == 1
        assert registry.lookup(run_id).phase is Phase.DELIVERING
    finally:
        registry.close()
