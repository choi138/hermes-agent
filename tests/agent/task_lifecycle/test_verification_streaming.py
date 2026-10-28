"""Real verifier inputs and subprocess logs fit a bounded hashing read budget."""
import hashlib
from pathlib import Path
import sys

import pytest

from agent.task_lifecycle import workflow
from agent.task_lifecycle.directory_handoff import open_directory
from agent.task_lifecycle.intake import create_grant
from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.verification import _execute, verify
from tests.agent.task_lifecycle import test_workflow

case = test_workflow.case


def bounded_reads(monkeypatch, guarded):
    original = Path.open
    observed = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            if size < 0 or size > 1024 * 1024:
                raise MemoryError('Hash input exceeds the bounded read budget')
            observed.append(size)
            return self.stream.read(size)

    def opened(path, mode='r', *args, **kwargs):
        stream = original(path, mode, *args, **kwargs)
        return Reader(stream) if mode == 'rb' and path in guarded else stream

    monkeypatch.setattr(Path, 'open', opened)
    return observed


@pytest.mark.platforms('macos')
def test_large_trusted_check_keeps_digest_and_rejects_mutation(case, tmp_path, monkeypatch):
    trusted = tmp_path / 'trusted-check.py'
    chunk = b'#' + b'x' * (128 * 1024 - 2) + b'\n'
    expected = hashlib.sha256()
    with trusted.open('wb') as stream:
        for _ in range(25):
            stream.write(chunk)
            expected.update(chunk)
        stream.write(b'pass\n')
        expected.update(b'pass\n')
    grant_path = create_grant(request=case['request'], request_text='bounded verifier hashes',
        objective='retain exact trusted verifier identity', owner='operator',
        origin='discord:thread:123:streaming-check', request_revision='1', profile='default',
        checks=[dict(name='large-boundary', kind='boundary', argv=[sys.executable, str(trusted)])],
        artifacts=['answer.json'], destination=dict(session_key='test:streaming-check',
            platform='discord', channel_id='123', thread_id='123', adapter_profile='default'))
    grant = workflow.load_grant(grant_path)[0]
    assert grant['check_files'][str(trusted)] == expected.hexdigest()
    run_id = workflow.submit(grant_path, popen=test_workflow.detached_popen(case,
        'import sys; sys.stdin.read(); print("done")'))['run_id']
    test_workflow.await_exit(run_id)
    reads = bounded_reads(monkeypatch, {trusted})
    assert verify(run_id)['accepted']
    assert reads
    with trusted.open('r+b') as stream:
        stream.seek(len(chunk) + 1)
        stream.write(b'y')
    with pytest.raises(LifecycleError, match='Trusted acceptance.*changed'):
        verify(run_id)


@pytest.mark.platforms('macos')
def test_check_subprocess_large_logs_keep_exact_receipt_hashes(case, monkeypatch):
    logs = case['output'] / 'large-check-logs'
    logs.mkdir()
    output_chunk = b'stdout:' + bytes(range(256)) * 256
    error_chunk = b'stderr:' + bytes(reversed(range(256))) * 256
    code = ('import sys\n'
        'out=b"stdout:"+bytes(range(256))*256\n'
        'err=b"stderr:"+bytes(reversed(range(256)))*256\n'
        'for _ in range(48):\n'
        ' sys.stdout.buffer.write(out)\n'
        ' sys.stderr.buffer.write(err)\n')
    reads = bounded_reads(monkeypatch, {logs / 'stdout', logs / 'stderr'})
    with open_directory(case['repo']) as (_, identity):
        result = _execute(dict(name='large-logs', kind='test', argv=[sys.executable, '-c', code],
            timeout=15), str(case['repo']), 'fixture-revision', logs, identity)
    assert result['passed'] and result['exit_code'] == 0
    for field, chunk in [('stdout', output_chunk), ('stderr', error_chunk)]:
        expected = hashlib.sha256()
        for _ in range(48):
            expected.update(chunk)
        assert result[field + '_sha256'] == expected.hexdigest()
        assert (logs / field).stat().st_size == 48 * len(chunk)
    assert reads
