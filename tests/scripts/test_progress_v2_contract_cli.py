"""Real CLI/stdin/HTTP contract. Only Discord HTTP and its token are fixtures."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from agent.delegation_progress import _atomic
from agent.delegation_progress_delivery import Delivery, SSHSender, ssh_transport
from scripts.delegation_progress_discord_send import discord_body
from tests.agent.test_delegation_progress_v2_red import lane


ROOT = Path(__file__).resolve().parents[2]


class JsonStore:
    def __init__(self, path, default):
        self.path = path
        self.path.write_text(json.dumps(default))
    def read(self):
        return json.loads(self.path.read_text())
    def __len__(self):
        return len(self.read())
    def __iter__(self):
        return iter(self.read())
    def __getitem__(self, key):
        return self.read()[key]
    def __setitem__(self, key, value):
        data = self.read()
        data[key] = value
        self.path.write_text(json.dumps(data))


@pytest.fixture(params=[True, False], ids=['post-nonce', 'post-no-nonce'])
def discord(tmp_path, request):
    # TCP listen is denied in this sandbox. socketpair is local IPC: the real
    # HTTP client and BaseHTTPRequestHandler still exchange serialized HTTP.
    messages = JsonStore(tmp_path / 'http-messages.json', {})
    traces = JsonStore(tmp_path / 'http-traces.json', [])
    boot = tmp_path / 'server_boot.py'
    boot.with_name('http-controls.json').write_text('{}')
    boot.write_text("""import sys, http.client, socket, threading, json
from pathlib import Path
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, ROOT)
import scripts.delegation_progress_discord_send as helper
messages_path, traces_path = Path(MESSAGES), Path(TRACES)
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass
    def handle_request(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command != 'GET' else None
        traces = json.loads(traces_path.read_text())
        traces.append({'method': self.command, 'path': self.path, 'body': body})
        traces_path.write_text(json.dumps(traces))
        messages = json.loads(messages_path.read_text())
        if self.command == 'POST':
            identity = str(1000 + len(messages))
            messages[identity] = dict(id=identity, channel_id=self.path.split('/')[4], content=body['content'], nonce=body.get('nonce'))
        else:
            identity = self.path.split('/')[-1]
            if self.command == 'PATCH':
                messages[identity]['content'] = body['content']
        messages_path.write_text(json.dumps(messages))
        response = dict(messages.get(identity, {}))
        if self.command == 'GET' or not POST_NONCE:
            response.pop('nonce', None)
        controls = json.loads(Path(__file__).with_name('http-controls.json').read_text())
        response.update(controls.get(self.command, {}))
        traces[-1]['response'] = response
        traces[-1]['status'] = controls.get(self.command + '_status', 200)
        traces_path.write_text(json.dumps(traces))
        raw = json.dumps(response).encode()
        self.send_response(controls.get(self.command + '_status', 200))
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    do_POST = handle_request
    do_PATCH = handle_request
    do_GET = handle_request
threads = []
def connection(host, timeout):
    client, peer = socket.socketpair()
    client.settimeout(timeout)
    def serve():
        try:
            Handler(peer, ('local-ipc', 0), None)
        finally:
            peer.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    threads.append(thread)
    conn = http.client.HTTPConnection('local-http-fixture', timeout=timeout)
    conn.sock = client
    return conn
helper._default_profile_token = lambda _: 'EXTERNAL_HTTP_FIXTURE'
helper.http.client.HTTPSConnection = connection
code = helper.main()
for thread in threads:
    thread.join(timeout=3)
raise SystemExit(code)
""".replace('ROOT', repr(str(ROOT))).replace('MESSAGES', repr(str(messages.path))).replace('TRACES', repr(str(traces.path))).replace('POST_NONCE', repr(request.param)))
    yield boot, traces, messages


def sender_for(boot, root):
    def popen(argv, **kwargs):
        assert argv[0] == '/usr/bin/ssh' and kwargs['shell'] is False
        remote = shlex.split(argv[-1])
        return subprocess.Popen([sys.executable, str(boot), *remote[2:]], **kwargs)
    return SSHSender('fixture-host', '/usr/bin/python3', '/srv/fixture', '/srv/helper.py', ['123'],
        server_state_dir=str(root), transport=lambda argv, data, timeout: ssh_transport(argv, data, timeout, popen=popen))


def test_producer_consumer_stable_card_dedup_and_get_only_parent_claim(tmp_path, discord):
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    first = delivery.drain_one()
    assert first['status'] == 'verified'
    card_id = first['message_id']
    assert [t['method'] for t in traces] == ['POST', 'GET']
    (p.manifest.worktree / 'a.py').write_text('value = 2\n')
    assert p.tick(now=300)['queued'][0]['operation'] == 'CARD_PATCH'
    assert delivery.drain_one()['message_id'] == card_id
    assert [t['method'] for t in traces] == ['POST', 'GET', 'PATCH', 'GET']
    for now in (600, 1200):
        assert not p.tick(now=now)['queued']
    p.manifest = replace(p.manifest, coordinator_stage='verifying')
    result = p.tick(now=1201)
    assert [m['operation'] for m in result['queued']] == ['CARD_PATCH', 'NOTICE']
    delivery.drain_one()
    head = p.peek()
    # Coordinator claims and sends the exact shared payload through the actual
    # helper. Recording that report MUST then perform only a real HTTP GET.
    import hashlib
    nonce = hashlib.sha256((head['run_id'] + ':' + str(head['sequence'])).encode()).hexdigest()[:24]
    manifest = tmp_path / 'manifest.json'
    _atomic(manifest, {k: str(v) if isinstance(v, Path) else v for k, v in vars(p.manifest).items()})
    claimed = subprocess.run([sys.executable, str(ROOT / 'scripts/delegation_progress.py'),
        'claim-report', '--manifest', str(manifest), '--state-dir', str(p.root)],
        capture_output=True, text=True, timeout=10)
    assert claimed.returncode == 0, claimed.stderr
    claim = json.loads(claimed.stdout)
    assert claim['event_id'] == head['event_id'] and discord_body(claim['payload'])['nonce'] == nonce
    before_claim = len(traces)
    assert delivery.drain_one()['status'] == 'uncertain'
    assert len(traces) == before_claim  # Claim fences automatic sending.
    manual = sender.send(claim['payload'])
    assert manual['status'] == 'verified'
    assert [t['method'] for t in traces[before_claim:]] == ['POST', 'GET']
    bridge_boot = tmp_path / 'bridge_boot.py'
    bridge_boot.write_text(f'''import sys, subprocess, shlex
sys.path.insert(0, {str(ROOT)!r})
import agent.delegation_progress_delivery as delivery
original = delivery.SSHSender.__init__
def popen(argv, **kwargs):
    remote = shlex.split(argv[-1])
    return subprocess.Popen([sys.executable, {str(boot)!r}, *remote[2:]], **kwargs)
def init(self, *args, **kwargs):
    original(self, *args, **kwargs, transport=lambda argv, data, timeout: delivery.ssh_transport(argv, data, timeout, popen=popen))
delivery.SSHSender.__init__ = init
from scripts.delegation_progress_bridge import main
raise SystemExit(main())
''')
    before = len(traces)
    result = subprocess.run([sys.executable, str(bridge_boot), '--manifest', str(manifest),
        '--state-dir', str(p.root), '--ssh-host', 'fixture-host', '--remote-python', '/usr/bin/python3',
        '--runtime-root', '/srv/fixture', '--helper-path', '/srv/helper.py', '--allow-thread', '123',
        '--server-state-dir', str(tmp_path / 'server-state'), '--record-reported-message', manual['message_id'], '--once'],
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [t['method'] for t in traces[before:]] == ['GET']
    assert not p.peek()
    assert not [m for m in p.tick(now=2400)['queued'] if m['operation'] == 'NOTICE']
    assert sum(t['method'] == 'POST' for t in traces) == 2  # One card, one claimed milestone.
    assert {t['path'].split('/')[-1] for t in traces if t['method'] == 'PATCH'} == {card_id}
    for trace in traces:
        if trace['body']:
            assert trace['body']['allowed_mentions'] == {'parse': [], 'replied_user': False}
        if trace['method'] == 'GET':
            assert 'nonce' not in trace['response']
        if trace['method'] == 'POST':
            assert trace['body']['enforce_nonce'] is True and trace['body']['nonce']
    # Export actual wire traces from the external HTTP fixture, no credentials.
    evidence = ROOT / '.hermes/verification/progress-v2-fix'
    evidence.mkdir(parents=True, exist_ok=True)
    variant = 'post-nonce' if 'not True' in boot.read_text() else 'post-no-nonce'
    (evidence / f'http-ipc-traces-{variant}.json').write_text(json.dumps(traces.read(), ensure_ascii=False, indent=2))


def test_server_binding_obsolete_sequence_uncertain_get_only(tmp_path, discord):
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    create = delivery.drain_one()
    (p.manifest.worktree / 'a.py').write_text('value = 2\n')
    p.tick(now=40)
    # Actual response lost at the caller AFTER server fsynced its GET receipt.
    original = sender.transport
    sender.transport = lambda *args: (original(*args), b'')[1]
    assert delivery.drain_one()['status'] == 'uncertain'
    count = len(traces)
    sender.transport = original
    assert Delivery(p, sender).drain_one()['status'] == 'uncertain'
    assert len(traces) == count

    assert delivery.drain_one(reconcile_message=create['message_id'])['status'] == 'verified'
    assert [t['method'] for t in traces[count:]] == ['GET']
    count = len(traces)
    card = json.loads(delivery.path.read_text())['card']
    from scripts.delegation_progress_discord_send import validate
    import hashlib
    content = messages[create['message_id']]['content']
    value = dict(run_id=p.manifest.run_id, sequence=999, thread_id='123', content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(), operation='CARD_PATCH',
        event_id='red:card:999', card_receipt=card)
    forged = dict(value, run_id='other', event_id='other:card:999')
    with pytest.raises(ValueError):
        validate(forged, {'123'})
    forged['card_receipt'] = dict(card, run_id='other')
    assert sender.send(forged)['status'] == 'uncertain'
    assert len(traces) == count
    assert sender.send(value)['status'] == 'verified'
    older = dict(value, sequence=998, event_id='red:card:998')
    count = len(traces)
    assert sender.send(older)['status'] == 'uncertain'
    assert len(traces) == count


def test_validation_cli_with_real_canonical_gate_log(tmp_path):
    p = lane(tmp_path)
    repo = p.manifest.worktree
    (repo / 'scripts').mkdir()
    (repo / 'tests').mkdir()
    # Match the outer requested interpreter; run_tests.sh otherwise prefers a
    # machine-wide shared venv ahead of HERMES_PYTHON in this temporary repo.
    (repo / '.venv').symlink_to(sys.prefix, target_is_directory=True)
    for name in ('run_tests.sh', 'run_tests_parallel.py'):
        shutil.copyfile(ROOT / 'scripts' / name, repo / 'scripts' / name)
    (repo / 'tests/test_gate.py').write_text('def test_gate():\n    assert 1 + 1 == 2\n')
    manifest = tmp_path / 'manifest.json'
    _atomic(manifest, {k: str(v) if isinstance(v, Path) else v for k, v in vars(p.manifest).items()})
    def cli(command, *args, check=True):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/delegation_progress.py'), command,
            '--manifest', str(manifest), '--state-dir', str(p.root), *args], capture_output=True, text=True, timeout=15)
        if check:
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
        return result
    cli('tick')
    ticket = cli('begin-validation')
    env = dict(os.environ, HERMES_PYTHON=sys.executable, HERMES_TEST_FILE_RETRIES='0')
    result = subprocess.run(['bash', 'scripts/run_tests.sh', '-j', '3', '--file-timeout', '180', 'tests/test_gate.py'],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    (p.manifest.artifact_root / 'gate.log').write_text(result.stdout + result.stderr)
    receipt = cli('record-validation', '--ticket', ticket['ticket'], '--evidence-ref', 'gate.log',
                  '--exit-code', str(result.returncode))
    assert receipt['passed'] == 1 and receipt['run_id'] == p.manifest.run_id
    snapshot = cli('snapshot')
    assert snapshot['validation']['applicable'] and '레나 검증 1개 통과' in __import__(
        'agent.delegation_progress', fromlist=['render']).render(snapshot)
    result = cli('set-stage', '--stage', 'final_verified', check=False)
    assert result.returncode == 74 and 'begin-validation' in result.stderr


@pytest.mark.parametrize('terminal', ['final_verified', 'stopped'])
def test_public_closed_snapshot_frozen_pending_drain_restart(tmp_path, terminal):
    from agent.delegation_progress import Manifest, Progress, render
    from agent.delegation_progress_policy import stage
    p = lane(tmp_path)
    manifest = tmp_path / 'manifest.json'
    def save_manifest():
        _atomic(manifest, {k: str(v) if isinstance(v, Path) else v for k, v in vars(p.manifest).items()})
    save_manifest()
    def cli(command, *args, check=True):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/delegation_progress.py'), command,
            '--manifest', str(manifest), '--state-dir', str(p.root), *args], capture_output=True, text=True, timeout=15)
        if check:
            assert result.returncode == 0, result.stderr
        return result
    cli('tick')  # Leave initial card pending: closing must preserve its drain order.
    ticket = json.loads(cli('begin-validation').stdout)
    if terminal == 'final_verified':
        gate = tmp_path / 'test_acceptance.py'
        gate.write_text('def test_acceptance():\n    assert 2 + 2 == 4\n')
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', str(gate)],
            cwd=tmp_path, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        (p.manifest.artifact_root / 'gate.log').write_text(result.stdout + result.stderr)
        cli('record-validation', '--ticket', ticket['ticket'], '--evidence-ref', 'gate.log',
            '--exit-code', '0', '--gate', 'pytest')
    else:
        cli('set-stage', '--stage', 'stopped')
    # Existing policy waits for the preceding card before queuing final output.
    waiting = json.loads(cli('tick').stdout)
    assert not waiting['queued'] and not p._load().get('closing')
    assert p.peek()['operation'] == 'CARD_CREATE'
    cli('ack', '--id', p.peek()['id'])
    closed = json.loads(cli('tick').stdout)
    frozen = closed['snapshot']
    assert stage(frozen)[0] == terminal and not closed['stopped']
    assert p.peek()['operation'] == 'CARD_PATCH' and p._load()['closing']
    assert json.loads(cli('snapshot').stdout) == frozen  # positive control
    assert cli('snapshot', '--format', 'text').stdout.strip() == render(frozen)
    for phase in ('pending', 'acked'):
        (p.manifest.worktree / 'a.py').write_text(f'value = {phase!r}\n')
        before = p.path.read_bytes()
        assert json.loads(cli('snapshot').stdout) == frozen
        assert cli('snapshot', '--format', 'text').stdout.strip() == render(frozen)
        assert p.path.read_bytes() == before  # read paths cannot mutate the closed run
        dry = json.loads(cli('tick', '--dry-run').stdout)
        assert dry['snapshot'] == frozen and dry['queued'] == []
        assert dry['stopped'] == (phase == 'acked')
        assert p.path.read_bytes() == before
        assert cli('begin-validation', check=False).returncode == 74
        assert cli('record-validation', '--ticket', ticket['ticket'], '--evidence-ref', 'gate.log',
                   '--exit-code', '0', '--gate', 'pytest', check=False).returncode == 74
        assert p.path.read_bytes() == before
        p = Progress(Manifest.load(manifest), p.root)  # restart preserves frozen run
        operations = []
        while p.peek():
            operations.append(p.peek()['operation'])
            cli('ack', '--id', p.peek()['id'])
        if phase == 'pending':
            assert operations == ['CARD_PATCH', 'NOTICE']
        assert p.tick(dry_run=True)['stopped']


@pytest.mark.parametrize('method,wrong', [
    ('POST', {'id': 'bad'}), ('POST', {'channel_id': '999'}),
    ('POST', {'content': 'different'}), ('POST', {'nonce': 'wrong'}),
    ('GET', {'id': '999'}), ('GET', {'channel_id': '999'}), ('GET', {'content': 'different'}),
])
def test_http_response_correlation_and_bound_recovery(tmp_path, discord, method, wrong):
    boot, traces, messages = discord
    controls = boot.with_name('http-controls.json')
    controls.write_text(json.dumps({method: wrong}))
    p = lane(tmp_path)
    p.tick(now=0)
    server = tmp_path / 'server-state'
    sender = sender_for(boot, server)
    delivery = Delivery(p, sender)
    assert delivery.drain_one()['status'] == 'uncertain'
    journal = server / 'red.json'
    state = json.loads(journal.read_text())
    known = state['records']['1']['known_message']
    assert known == ('1000' if method == 'GET' else None)
    before = len(traces)
    assert Delivery(p, sender).drain_one()['status'] == 'uncertain'
    assert len(traces) == before
    controls.write_text('{}')
    assert delivery.drain_one(reconcile_message='9999')['status'] == 'uncertain'
    assert len(traces) == before
    result = delivery.drain_one(reconcile_message='1000')
    if method == 'GET':
        assert result['status'] == 'verified' and result['message_id'] == '1000'
        assert [t['method'] for t in traces[before:]] == ['GET']
        assert p.peek() is None
    else:
        assert result['status'] == 'uncertain' and p.peek() is not None
        assert len(traces) == before  # Identical GET would not establish this run.
    assert sum(t['method'] == 'POST' for t in traces) == 1


def test_unbound_claim_and_wrong_run_channel_do_not_ack(tmp_path, discord):
    from scripts.delegation_progress_discord_send import discord_body
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    assert delivery.drain_one()['status'] == 'verified'
    p.manifest = replace(p.manifest, coordinator_stage='verifying')
    p.tick(now=1200)
    assert delivery.drain_one()['operation'] == 'CARD_PATCH'
    claim = delivery.claim_notice()
    payload = claim['payload']
    # Even identical content AND nonce in an arbitrary message cannot bind it.
    messages['9999'] = dict(id='9999', channel_id='123', content=payload['content'],
                            nonce=discord_body(payload)['nonce'])
    before = len(traces)
    assert delivery.drain_one(reported_message='9999')['status'] == 'uncertain'
    assert len(traces) == before and p.peek()['event_id'] == claim['event_id']
    assert Delivery(p, sender).drain_one()['status'] == 'uncertain'
    assert sender.send(payload)['status'] == 'uncertain'  # Server also fences this unknown response.
    assert sender.send(dict(payload, run_id='other', event_id='other:verifying:1'),
                       reconcile_message='9999')['status'] == 'uncertain'
    with pytest.raises(ValueError, match='payload_identity'):
        sender.send(dict(payload, thread_id='456'), reconcile_message='9999')
    assert len(traces) == before


def test_patch_unknown_response_cannot_use_create_binding(tmp_path, discord):
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    create = delivery.drain_one()
    (p.manifest.worktree / 'a.py').write_text('value = 22\n')
    p.tick(now=40)
    boot.with_name('http-controls.json').write_text(json.dumps({'PATCH': {'id': '9999'}}))
    assert delivery.drain_one()['status'] == 'uncertain'
    before = len(traces)
    boot.with_name('http-controls.json').write_text('{}')
    assert delivery.drain_one(reconcile_message=create['message_id'])['status'] == 'uncertain'
    assert len(traces) == before
    assert p.peek()['operation'] == 'CARD_PATCH'


def test_automatic_journal_recovery_is_get_only(tmp_path, discord):
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    original = sender.transport
    sender.transport = lambda *args: (original(*args), b'')[1]
    assert delivery.drain_one()['status'] == 'uncertain'
    count = len(traces)
    sender.transport = original
    result = delivery.drain_one(recover_journal=True)
    assert result['status'] == 'verified'
    assert [t['method'] for t in traces[count:]] == ['GET']
    assert p.peek() is None


def test_journal_recovery_without_response_binding_never_posts(tmp_path, discord):
    boot, traces, messages = discord
    p = lane(tmp_path)
    p.tick(now=0)
    sender = sender_for(boot, tmp_path / 'server-state')
    delivery = Delivery(p, sender)
    original = sender.transport
    sender.transport = lambda *args: b''  # Failure before any server request.
    assert delivery.drain_one()['status'] == 'uncertain'
    sender.transport = original
    assert delivery.drain_one(recover_journal=True)['status'] == 'uncertain'
    assert not traces and p.peek() is not None
