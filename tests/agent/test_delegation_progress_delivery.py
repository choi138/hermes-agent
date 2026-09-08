"""HTTP/SSH fixtures only. No real credentials or network execution."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


def payload():
    text = '작업 진행 상황을 전해드려요.\n• runner.py: 파일 내용 변경.'
    return dict(run_id='fixture-run', sequence=1, thread_id='123456789', content=text,
                content_digest=hashlib.sha256(text.encode()).hexdigest())


class Response:
    def __init__(self, value, status=200):
        self.status = status
        self.data = json.dumps(value).encode()
    def read(self, limit):
        return self.data[:limit]


class HTTPFixture:
    def __init__(self, *, wrong=None, failure=None):
        self.calls = []
        self.wrong, self.failure = wrong, failure
    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        if self.failure == method:
            raise TimeoutError('PRIVATE_TOKEN_MUST_NOT_LEAK')
    def getresponse(self):
        value = dict(id='987654321', channel_id='123456789', content=payload()['content'])
        if self.wrong:
            value.update(self.wrong)
        return Response(value)
    def close(self):
        pass


def test_http_serialization_post_then_exact_get():
    from scripts.delegation_progress_discord_send import deliver
    http = HTTPFixture()
    result = deliver(payload(), {'123456789'}, token_loader=lambda: 'FIXTURE_ONLY', connection=http)
    assert result['status'] == 'verified'
    assert result['message_id'] == '987654321'
    assert [c[:2] for c in http.calls] == [('POST', '/api/v10/channels/123456789/messages'),
        ('GET', '/api/v10/channels/123456789/messages/987654321')]
    body = json.loads(http.calls[0][2])
    assert body == {'content':payload()['content'], 'allowed_mentions':{'parse':[], 'replied_user':False}}
    assert 'FIXTURE_ONLY' not in json.dumps(result)


@pytest.mark.parametrize('wrong', [{'channel_id':'999'}, {'id':'bad'}, {'content':'changed'}])
def test_http_mismatch_never_verified(wrong):
    from scripts.delegation_progress_discord_send import deliver
    assert deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY',
                   connection=HTTPFixture(wrong=wrong))['status'] == 'uncertain'


@pytest.mark.parametrize('method', ['POST','GET'])
def test_lost_response_is_uncertain(method):
    from scripts.delegation_progress_discord_send import deliver
    result = deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY',
                     connection=HTTPFixture(failure=method))
    assert result == {'status':'uncertain'}


def test_cross_thread_and_unsafe_payload_before_credentials():
    from scripts.delegation_progress_discord_send import deliver
    def forbidden():
        pytest.fail('credential loader must not execute')
    assert deliver(payload(), {'999'}, token_loader=forbidden)['status'] == 'rejected'
    for text in ['@everyone', 'MEDIA:/tmp/a', 'https://evil', 'x\x1b[2J']:
        value = dict(payload(), content=text, content_digest=hashlib.sha256(text.encode()).hexdigest())
        assert deliver(value, {'123456789'}, token_loader=forbidden)['status'] == 'rejected'


def test_empty_malformed_or_forged_ssh_success():
    from agent.delegation_progress_delivery import SSHSender
    for output in [b'', b'{}', b'{"skipped":true}', b'not json']:
        sender = SSHSender('fixture-host', '/usr/bin/python3', '/srv/hermes', '/srv/helper.py',
                           ['123456789'], transport=lambda *args: output)
        assert sender.send(payload()) == {'status':'uncertain'}


def test_ssh_argv_is_fixed_and_quoted():
    import shlex
    from agent.delegation_progress_delivery import SSHSender
    sender = SSHSender('operator@fixture-host', '/srv/python space/bin/python', '/srv/runtime root',
                       '/srv/helper path.py', ['123456789'])
    args = sender.argv()
    assert args[:5] == ['/usr/bin/ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    assert 'StrictHostKeyChecking=no' not in args
    assert shlex.split(args[-1]) == ['/srv/python space/bin/python', '/srv/helper path.py',
        '--runtime-root', '/srv/runtime root', '--allow-thread', '123456789']
    with pytest.raises(ValueError):
        SSHSender('-oProxyCommand=evil', '/usr/bin/python3', '/srv/hermes', '/srv/helper.py', ['123456789'])


@pytest.fixture
def progress(tmp_path):
    from agent.delegation_progress import Manifest, Progress
    repo, artifacts = tmp_path / 'repo', tmp_path / 'artifacts'
    repo.mkdir()
    artifacts.mkdir(mode=0o700)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'runner.py').write_text('value=1')
    p = Progress(Manifest('fixture-run', repo, repo, artifacts, '123456789', 'fixture'), tmp_path / 'state')
    p.tick(now=0)
    p.tick(now=300)
    return p


class SenderFixture:
    def __init__(self, result=None):
        self.calls, self.result = [], result
    def send(self, value, *, reconcile_message=None):
        from scripts.delegation_progress_discord_send import receipt
        self.calls.append((value, reconcile_message))
        return self.result if self.result is not None else receipt(value, str(987654321 + value['sequence']))


def test_receipt_fsync_before_ack_and_restart_without_resend(progress, monkeypatch):
    from agent.delegation_progress_delivery import Delivery
    sender = SenderFixture()
    delivery = Delivery(progress, sender)
    original = progress.ack
    def fail_ack(*args):
        disk = json.loads(delivery.path.read_text())
        assert disk['records']['1']['status'] == 'verified'
        raise OSError('FIXTURE_ACK_FAILURE')
    monkeypatch.setattr(progress, 'ack', fail_ack)
    with pytest.raises(OSError):
        delivery.drain_one()
    assert progress.peek() is not None
    monkeypatch.setattr(progress, 'ack', original)
    assert Delivery(progress, sender).drain_one()['status'] == 'verified'
    assert len(sender.calls) == 1 and progress.peek() is None


def test_uncertain_restart_holds_and_get_only_reconciliation(progress):
    from agent.delegation_progress_delivery import Delivery
    sender = SenderFixture({'status':'uncertain'})
    assert Delivery(progress, sender).drain_one() == {'status':'uncertain'}
    assert Delivery(progress, sender).drain_one() == {'status':'uncertain'}
    assert len(sender.calls) == 1 and progress.peek()
    sender.result = None
    assert Delivery(progress, sender).drain_one(reconcile_message='987654322')['status'] == 'verified'
    assert sender.calls[-1][1] == '987654322' and progress.peek() is None


def test_reconciliation_failure_must_not_enable_post_retry(progress):
    from agent.delegation_progress_delivery import Delivery
    sender = SenderFixture({'status':'uncertain'})
    Delivery(progress, sender).drain_one()
    sender.result = {'status':'rejected'}
    assert Delivery(progress, sender).drain_one(reconcile_message='987654322') == {'status':'uncertain'}
    Delivery(progress, sender).drain_one()
    assert len(sender.calls) == 2


def test_network_rejection_keeps_pending_for_explicit_restart(progress):
    from agent.delegation_progress_delivery import Delivery
    sender = SenderFixture({'status':'rejected'})
    assert Delivery(progress, sender).drain_one() == {'status':'rejected'}
    assert progress.peek() is not None
    sender.result = None
    assert Delivery(progress, sender).drain_one()['status'] == 'verified'


def test_duplicate_discord_id_and_cross_thread_never_ack(progress):
    from agent.delegation_progress import _atomic
    from agent.delegation_progress_delivery import Delivery
    from scripts.delegation_progress_discord_send import receipt
    sender = SenderFixture()
    first = Delivery(progress, sender).drain_one()
    progress.tick(now=600)
    class Duplicate:
        def send(self, value, **kwargs):
            return receipt(value, first['message_id'])
    assert Delivery(progress, Duplicate()).drain_one()['status'] == 'uncertain'
    state = progress._load()
    state['pending'][0]['thread_id'] = '999'
    _atomic(progress.path, state)
    with pytest.raises(ValueError, match='outbox_identity'):
        Delivery(progress, sender).drain_one()
    assert len(sender.calls) == 1


def test_final_pending_is_drained_even_when_watcher_stopped(progress):
    from dataclasses import replace
    from agent.delegation_progress_delivery import Delivery
    progress.manifest = replace(progress.manifest, coordinator_stage='final_verified')
    assert progress.tick(now=301)['stopped']
    sender = SenderFixture()
    with progress.watcher():
        assert Delivery(progress, sender).drain_one()['sequence'] == 1
        assert Delivery(progress, sender).drain_one()['sequence'] == 2
    assert progress.peek() is None


def test_delivery_and_watcher_fencing(progress):
    from agent.delegation_progress import Progress, _lock
    from agent.delegation_progress_delivery import Delivery
    with progress.watcher():
        with pytest.raises(ValueError, match='run_locked'):
            with Progress(progress.manifest, progress.root).watcher():
                pytest.fail('duplicate watcher')
    with _lock(progress.directory / 'delivery.lock'):
        with pytest.raises(ValueError, match='run_locked'):
            Delivery(progress, SenderFixture()).drain_one()


def test_real_http_client_wire_serialization_with_fake_socket():
    import io
    import http.client
    from scripts.delegation_progress_discord_send import deliver
    class SocketFixture:
        def __init__(self):
            self.sent = []
        def sendall(self, data):
            self.sent.append(bytes(data))
        def makefile(self, *args):
            body = json.dumps(dict(id='987654321', channel_id='123456789', content=payload()['content'])).encode()
            return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        def close(self):
            pass
    sock = SocketFixture()
    conn = http.client.HTTPConnection('fixture.invalid')
    conn.sock = sock
    result = deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY', connection=conn)
    assert result['status'] == 'verified'
    wire = b''.join(sock.sent)
    assert b'POST /api/v10/channels/123456789/messages HTTP/1.1' in wire
    assert b'GET /api/v10/channels/123456789/messages/987654321 HTTP/1.1' in wire
    assert b'"allowed_mentions": {"parse": [], "replied_user": false}' in wire
    assert b'attachments' not in wire


@pytest.mark.parametrize('status', [400,401,403,404,429,500])
def test_http_rejection_or_server_uncertainty(status):
    from scripts.delegation_progress_discord_send import deliver
    http = HTTPFixture()
    http.getresponse = lambda: Response({'message':'PRIVATE_REMOTE_ERROR'}, status=status)
    result = deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY', connection=http)
    assert result == {'status':'uncertain' if status == 500 else 'rejected'}


def test_get_mismatch_after_successful_post():
    from scripts.delegation_progress_discord_send import deliver
    for wrong in [{'id':'111'}, {'channel_id':'999'}, {'content':'edited'}]:
        http = HTTPFixture()
        original = http.getresponse
        def response():
            if len(http.calls) == 2:
                http.wrong = wrong
            return original()
        http.getresponse = response
        assert deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY', connection=http) == {'status':'uncertain'}


def test_get_only_reconciliation():
    from scripts.delegation_progress_discord_send import deliver
    http = HTTPFixture()
    assert deliver(payload(), {'123456789'}, token_loader=lambda:'FIXTURE_ONLY', connection=http,
                   reconcile_message='987654321')['status'] == 'verified'
    assert [c[0] for c in http.calls] == ['GET']


def test_receipt_boolean_sequence_is_not_integer_identity():
    from scripts.delegation_progress_discord_send import receipt, validate_receipt
    value = dict(receipt(payload(), '987654321'), sequence=True)
    with pytest.raises(ValueError):
        validate_receipt(value, payload())


@pytest.mark.parametrize('name', ['@everyone.py', '<@123>.py', '<@&123>.py', 'MEDIA:payload.py',
    'media.py', 'https://example.py', '[click](evil).py', 'a`b.py', 'x\x1b[2J.py', 'x\u202e.py',
    'sk-' + 'a'*45 + '.py', 'credentials.py', 'www.evil.py'])
def test_each_pathological_name_is_safe_fallback(name):
    from agent.delegation_progress import render
    text = render(dict(available=True, changes=[dict(path=name, **{'class':'source'}, kinds=['added'])],
                       tests={'status':'unknown'}))
    assert '안전한 이름 표시 불가' in text
    assert name not in text
    value = dict(payload(), content=text, content_digest=hashlib.sha256(text.encode()).hexdigest())
    from scripts.delegation_progress_discord_send import validate
    validate(value, {'123456789'})
