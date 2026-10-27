"""Actual V3 CLI policy and gateway-only progress; disposable workers, no network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from agent.delegation_progress import Manifest, Progress, _atomic, register_run
from agent.delegation_progress_gateway import GatewayProgress, observe
from agent.delegation_progress_delivery import Delivery
from agent.codex_task_runner import TaskRequest

ROOT = Path(__file__).resolve().parents[2]


def setup_case(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'SPEC.md').write_text('Read approved source and report.')
    (repo / 'module.py').write_text('baseline\n')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir(mode=0o700)
    registration = tmp_path / 'registration'
    registration.mkdir(mode=0o700)
    return repo, artifacts, registration


@pytest.mark.parametrize('cli', ['codex', 'claude'])
def test_actual_v3_cli_preserves_policy_and_owned_progress(tmp_path, cli):
    repo, artifacts, registration = setup_case(tmp_path)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    release = tmp_path / 'release'
    recorded = tmp_path / 'argv.json'
    worker = binaries / cli
    worker.write_text('#!' + sys.executable + '\n' +
        'import sys, json, time\nfrom pathlib import Path\nsys.stdin.read()\n' +
        f'Path({str(recorded)!r}).write_text(json.dumps(sys.argv[1:]))\n' +
        "Path('module.py').write_text('changed\\n')\n" +
        f'while not Path({str(release)!r}).exists(): time.sleep(.01)\n')
    worker.chmod(0o700)
    manifest_path = registration / 'manifest.json'
    state_dir = tmp_path / 'state'
    command = [sys.executable, str(ROOT / 'scripts/run_codex_task.py'),
        '--spec', str(repo / 'SPEC.md'), '--workdir', str(repo), '--allowed-root', str(repo),
        '--output-dir', str(artifacts), '--cli', cli, '--timeout', '60',
        '--progress-manifest', str(manifest_path), '--progress-state-dir', str(state_dir),
        '--progress-thread', '123456', '--progress-label', 'private fixture',
        '--progress-code-scope', 'module.py', '--progress-sender-profile', 'koharu',
        '--progress-expected-bot-id', '1554717538376753244', '--progress-owner', 'gateway']
    command += ['--task-class', 'mechanical'] if cli == 'codex' else ['--model', 'sonnet', '--advisor', 'opus']
    process = subprocess.Popen(command, env={**os.environ, 'PATH': str(binaries)+os.pathsep+os.environ['PATH']},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not (registration / 'manifest.json.worker-start.json').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert (registration / 'manifest.json.worker-start.json').exists(), process.communicate(timeout=1)
        first = observe(manifest_path, state_dir, '123456')
        assert first['worker']['cli'] == cli and first['message']['event'] == 'started'
        assert first['message']['sender_profile'] == 'koharu'
        assert first['message']['expected_bot_id'] == '1554717538376753244'
        started_id = first['message']['id']
        observe(manifest_path, state_dir, '123456', ack=started_id)
        observe(manifest_path, state_dir, '123456', ack=started_id)  # Lost SSH acknowledgement.
        manifest = Manifest.load(manifest_path)
        assert manifest.delivery_owner == 'gateway' and manifest.code_scope == ('module.py',)
        assert manifest.sender_profile == 'koharu'
        progress = GatewayProgress(manifest, state_dir)
        while not recorded.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        argv = json.loads(recorded.read_text())
        if cli == 'codex':
            assert argv[argv.index('-m') + 1] == 'gpt-6.1-terra'
            assert 'model_reasoning_effort="low"' in argv
        else:
            assert argv[argv.index('--advisor') + 1] == 'opus'
            assert argv[argv.index('--permission-mode') + 1] == 'dontAsk'
            assert argv[argv.index('--tools') + 1] == 'Read,Glob,Grep'
            assert '--strict-mcp-config' in argv
            assert '--no-session-persistence' in argv
        while (repo/'module.py').read_text() != 'changed\n' and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert (repo/'module.py').read_text() == 'changed\n'
        baseline = progress._load()['last_queued_at']
        result = progress.tick(now=baseline + 301)
        assert result['queued'] and 'module.py' in result['queued'][0]['content']
        progress.ack(progress.peek()['id'])
        assert not progress.tick(now=baseline + 602)['queued']
        with pytest.raises(ValueError, match='gateway_thread_binding'):
            observe(manifest_path, state_dir, '999')
        with pytest.raises(ValueError, match='manual_owner_required'):
            Progress(manifest, state_dir).tick()
        with pytest.raises(ValueError, match='manual_owner_required'):
            Delivery(progress, object()).drain_one()
        release.touch()
        assert process.wait(timeout=5) == 0
        final = observe(manifest_path, state_dir, '123456')
        assert final['exit_status'] == 'cli_completed'
        assert final['message']['event'] == 'cli_completed'
        stopped = observe(manifest_path, state_dir, '123456', stop=True)
        assert stopped['status'] == 'stopped' and progress.peek() is None
        output, error = process.communicate(timeout=1)
        receipt = json.loads(output.decode().splitlines()[-1])
        if cli == 'codex':
            assert receipt['policy']['selected'] == {'model': 'gpt-6.1-terra', 'effort': 'low'}
        else:
            assert receipt['configuration']['requested']['model'] == 'sonnet'
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_gateway_baseline_has_no_unchanged_heartbeat_and_terminal_supersedes(tmp_path):
    repo, artifacts, registration = setup_case(tmp_path)
    run_dir = artifacts / 'run'
    run_dir.mkdir(mode=0o700)
    (run_dir/"events.jsonl").write_text("")
    request = TaskRequest(repo/'SPEC.md', repo, repo, artifacts)
    manifest = register_run(request, run_dir, registration/'manifest.json', tmp_path/'state',
                            '123456', 'private label', delivery_owner='gateway', code_scope=('module.py',))
    progress = GatewayProgress(manifest, tmp_path/'state')
    baseline = progress._load()['last_queued_at']
    assert not progress.tick(now=baseline + 301)['queued']
    (repo/'module.py').write_text('changed\n')
    progress.tick(now=baseline + 302)
    old_id = progress.peek()['id']
    _atomic(run_dir/'status.json', {'status': 'cleanup_failed', 'exit_code': 74})
    terminal = progress.tick(now=baseline + 303)
    assert terminal['queued'][0]['event'] == 'cleanup_failed'
    assert progress.peek()['id'] != old_id
    assert '프로세스가 남아' in progress.peek()['content']
    with pytest.raises(ValueError):
        progress.ack(old_id)
    terminal_id = progress.peek()['id']
    progress.ack(terminal_id)
    assert progress.peek() is None
    assert not progress.tick(now=baseline + 900)['queued']
    assert manifest.binding() != replace(manifest, delivery_owner='manual').binding()


def owned_progress_case(tmp_path):
    repo, artifacts = tmp_path/'repo', tmp_path/'artifacts'
    repo.mkdir()
    artifacts.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo/'module.py').write_text('baseline\n')
    (artifacts/'events.jsonl').write_text('')
    manifest = Manifest('owned', repo, repo, artifacts, '123', 'fixture',
        event_path=artifacts/'events.jsonl', receipt_path=artifacts/'status.json', delivery_owner='gateway')
    path = tmp_path/'manifest.json'
    from dataclasses import asdict
    _atomic(path, {k: str(v) if isinstance(v, Path) else v for k,v in asdict(manifest).items()})
    progress = GatewayProgress(manifest, tmp_path/'state')
    progress.tick(now=0)
    (repo/'module.py').write_text('changed\n')
    progress.tick(now=301)
    return manifest, path, progress


def test_admitted_head_survives_terminal_and_stop_until_ack(tmp_path):
    manifest, path, progress = owned_progress_case(tmp_path)
    first = progress.peek()['id']
    assert observe(path, progress.root, '123', admit=first)['message']['id'] == first
    assert observe(path, progress.root, '123', admit=first)['message']['admitted']
    manifest.receipt_path.write_text(json.dumps({'status': 'cli_completed', 'exit_code': 0}))
    terminal = progress.tick(now=302)['queued'][0]
    assert terminal['event'] == 'cli_completed'
    assert progress.peek()['id'] == first
    progress.ack(first)
    assert progress.peek()['id'] == terminal['id']
    progress.admit(terminal['id'])
    observe(path, progress.root, '123', stop=True)
    assert progress.peek()['id'] == terminal['id']
    progress.ack(terminal['id'])
    assert progress.peek() is None
    assert not progress.tick(now=999)['queued']


def test_concurrent_gateway_ack_is_idempotent_and_exact(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    manifest, path, progress = owned_progress_case(tmp_path)
    head = progress.peek()['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: observe(path, progress.root, '123', ack=head), range(2)))
    assert len(results) == 2 and progress.peek() is None
    with pytest.raises(ValueError, match='ack_requires_exact_head_id'):
        progress.ack('other:1')


def test_sibling_manifests_have_independent_worker_start_receipts(tmp_path):
    manifest, path, progress = owned_progress_case(tmp_path)
    from dataclasses import replace
    sibling = replace(manifest, run_id='sibling')
    sibling_path = tmp_path/'sibling.json'
    from dataclasses import asdict
    _atomic(sibling_path, {k: str(v) if isinstance(v, Path) else v for k,v in asdict(sibling).items()})
    for current, current_path, pid in ((manifest, path, 123), (sibling, sibling_path, 456)):
        Path(str(current_path)+'.worker-start.json').write_text(json.dumps({'run_id': current.run_id, 'pid': pid, 'cli':'codex'}))
    assert observe(path, progress.root, '123')['worker']['pid'] == 123
    assert observe(sibling_path, progress.root, '123')['worker']['pid'] == 456


def test_gateway_derives_final_verification_from_applicable_evidence(tmp_path):
    from agent.delegation_progress_evidence import begin, record
    manifest, path, progress = owned_progress_case(tmp_path)
    progress.ack(progress.peek()['id'])
    ticket = begin(progress)
    (manifest.artifact_root/'gate.log').write_text('3 passed, 1 skipped in 0.12s\n')
    record(progress, ticket['ticket'], 'gate.log', 0, 'pytest')
    manifest.receipt_path.write_text(json.dumps({'status':'cli_completed', 'exit_code':0}))
    result = progress.tick(now=302)
    assert result['snapshot']['validation']['applicable']
    assert result['snapshot']['coordinator_stage'] == 'final_verified'
    assert result['queued'][0]['event'] == 'final_verified'
    assert '최종 검증 완료' in result['queued'][0]['content']
    assert '아직 미검증' not in result['queued'][0]['content']
    assert result['stopped']
    progress.ack(progress.peek()['id'])
    assert progress.peek() is None and not progress.tick(now=999)['queued']


def test_observer_cli_exposes_allowlisted_binding_cause(tmp_path, capsys):
    from agent.delegation_progress_gateway import main
    _, path, progress = owned_progress_case(tmp_path)
    assert main(['--manifest', str(path), '--state-dir', str(progress.root), '--thread', '999']) == 74
    result = json.loads(capsys.readouterr().out)
    assert result == {'status':'observation_unavailable', 'error':'gateway_thread_binding'}


def test_late_worker_start_does_not_reorder_admitted_head(tmp_path):
    manifest, path, progress = owned_progress_case(tmp_path)
    first = progress.peek()['id']
    progress.admit(first)
    Path(str(path)+'.worker-start.json').write_text(json.dumps({'run_id':manifest.run_id, 'pid':123, 'cli':'codex'}))
    result = observe(path, progress.root, '123')
    assert result['message']['id'] == first
    progress.ack(first)
    assert progress.peek()['event'] == 'started'
    progress.ack(progress.peek()['id'])
    assert progress.peek() is None


def test_terminal_supersedes_unadmitted_head_without_hidden_stale_queue(tmp_path):
    manifest, _, progress = owned_progress_case(tmp_path)
    first = progress.peek()['id']
    manifest.receipt_path.write_text(json.dumps({'status':'cleanup_failed', 'exit_code':74}))
    terminal = progress.tick(now=302)['queued'][0]
    assert progress.peek()['id'] == terminal['id'] != first
    with pytest.raises(ValueError, match='ack_requires_exact_head_id'):
        progress.ack(first)
    progress.ack(terminal['id'])
    assert progress.peek() is None


@pytest.mark.parametrize('status,code', [
    ('cli_completed',0),('cli_failed',1),('timed_out',124),('cancelled',130),
    ('launch_failed',127),('output_limit',75),('artifact_or_transport_failed',74),
    ('registration_failed',74),('cleanup_failed',74),('report_rejected',76),
])
def test_gateway_terminal_receipts_preserve_failure_and_verification_pending(tmp_path,status,code):
    manifest,_,progress=owned_progress_case(tmp_path)
    manifest.receipt_path.write_text(json.dumps({'status':status,'exit_code':code}))
    terminal=progress.tick(now=302)['queued'][0]
    assert terminal['event']==status
    assert '최종 검증 완료' not in terminal['content']
    if status=='cleanup_failed':
        assert '프로세스가 남아 있을 수' in terminal['content']
    elif status=='cli_completed':
        assert '결과 검증' in terminal['content']


def test_gateway_owner_cannot_be_downgraded_to_enable_manual_sender(tmp_path):
    manifest,_,progress=owned_progress_case(tmp_path)
    with pytest.raises(ValueError,match='state_identity_or_schema_mismatch'):
        Progress(replace(manifest,delivery_owner='manual'),progress.root).tick(now=302)
