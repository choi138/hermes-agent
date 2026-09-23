"""Execute contract-bound checks and retain artifact evidence across restarts."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from agent.codex_task_runner import _stop_group
from .acceptance import AcceptanceGate, Artifact, CheckResult
from .contract import _digest
from .directory_handoff import open_directory
from .registry import Registry
from .types import LifecycleError, Phase


def validate_checks(checks):
    if not isinstance(checks, (tuple, list)) or not checks:
        raise LifecycleError('At least one required check is necessary')
    names = set()
    for check in checks:
        if set(check) - {'name', 'kind', 'argv', 'timeout', 'reviewer', 'correction'}:
            raise LifecycleError('Unknown check option')
        name, kind, argv = (check.get(k) for k in ('name', 'kind', 'argv'))
        if not isinstance(name, str) or not name or kind not in {'test', 'review', 'boundary'}:
            raise LifecycleError('Invalid check name or kind')
        if (kind, name) in names:
            raise LifecycleError('Duplicate check')
        names.add((kind, name))
        if (not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and '\0' not in x for x in argv)
                or not Path(argv[0]).is_absolute()):
            raise LifecycleError('Checks require a trusted absolute executable and argv; no shell text')
        timeout = check.get('timeout', 300)
        if type(timeout) not in {int, float} or not 0 < timeout <= 1800:
            raise LifecycleError('Invalid check timeout')
        if kind == 'review' and check.get('reviewer') not in {'codex', 'claude', 'human'}:
            raise LifecycleError('Review must name an independent reviewer')


def check_files(checks, worker_root):
    """Bind executables/scripts outside the implementation's writable root."""
    files = {}
    for check in checks:
        for arg in check['argv']:
            path = Path(arg)
            if path.is_absolute() and path.is_file():
                path = path.resolve(strict=True)
                if path.is_relative_to(Path(worker_root)):
                    raise LifecycleError('Acceptance executable/script must be outside worker-writable scope')
                files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] == '.git':
        raise LifecycleError('Artifact paths must be relative and inside the workdir')
    return path


def snapshot(root, paths, *, allow_missing=False):
    """Hash bytes through openat handles; reject symlink files and ancestors."""
    root = Path(root)
    if not paths or len(set(paths)) != len(paths):
        raise LifecycleError('Explicit unique artifact paths are required')
    hashes = {}
    with open_directory(root) as (root_fd, _):
        for value in paths:
            path = _relative(value)
            parent = os.dup(root_fd)
            try:
                try:
                    for part in path.parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                except FileNotFoundError:
                    if not allow_missing:
                        raise
                    hashes[str(path)] = None
                    continue
                try:
                    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError:
                    if not allow_missing:
                        raise
                    hashes[str(path)] = None
                    continue
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise LifecycleError('Artifacts must be regular files')
                    h = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(chunk)
                    after = os.fstat(stream.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise LifecycleError('Artifact changed while hashing')
                    hashes[str(path)] = dict(sha256=h.hexdigest(), bytes=after.st_size, executable=bool(after.st_mode & 0o111))
            finally:
                os.close(parent)
    return {'revision': _digest(hashes), 'files': hashes}


def read_artifact(root, relative, expected, *, limit=8 * 1024 * 1024):
    """Copy verified bytes without following replaced files or ancestors."""
    path = _relative(relative)
    with open_directory(root) as (root_fd, _):
        parent = os.dup(root_fd)
        try:
            for part in path.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise LifecycleError('Attachment must be a bounded regular file')
                data = stream.read(limit + 1)
        finally:
            os.close(parent)
    if (len(data) > limit or len(data) != expected['bytes']
            or hashlib.sha256(data).hexdigest() != expected['sha256']):
        raise LifecycleError('Attachment bytes changed or exceed supported size')
    return data


@contextmanager
def run_lock(run_id):
    from hermes_constants import get_hermes_home
    from uuid import UUID
    UUID(run_id)  # no path input in lock names
    root = get_hermes_home() / 'lifecycle' / 'locks'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / run_id, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise LifecycleError('Another finalizer owns this run') from exc
    finally:
        os.close(fd)


def _schema(registry):
    registry._conn.execute('''CREATE TABLE IF NOT EXISTS lifecycle_verifications (
        run_id TEXT PRIMARY KEY, contract_digest TEXT NOT NULL, evidence TEXT NOT NULL
    )''')
    registry._conn.commit()


def evidence(registry, run_id):
    _schema(registry)
    row = registry._conn.execute('SELECT contract_digest,evidence FROM lifecycle_verifications WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        return None
    if row[0] != registry.lookup(run_id).contract.digest():
        raise LifecycleError('Verification contract mismatch')
    return json.loads(row[1])


def _execute(check, root, revision, log_dir, directory_identity):
    from .workflow import process_identity, write_private
    stdout_path, stderr_path = log_dir / 'stdout', log_dir / 'stderr'
    started = time.time()
    code = 74
    with open(stdout_path, 'xb') as out, open(stderr_path, 'xb') as err:
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        from types import SimpleNamespace
        from .directory_handoff import spawn_pinned
        request = SimpleNamespace(cli='check', workdir=root, argv=lambda:check['argv'])
        with open_directory(root, tuple(tuple(i) for i in directory_identity)) as (fd, _):
            process = spawn_pinned(subprocess.Popen, check['argv'], directory_fd=fd,
                request=request, deadline=time.monotonic()+check.get('timeout',300),
                cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                shell=False, start_new_session=True,
                env={**os.environ, 'HERMES_ARTIFACT_REVISION': revision})
        try:
            import psutil
            try:
                process._hermes_started_at = process_identity(process.pid)['started_at']
            except psutil.NoSuchProcess:
                pass  # A successful short check may already have exited.
            code = process.wait(timeout=check.get('timeout', 300))
        except subprocess.TimeoutExpired:
            code = 124
        finally:
            _stop_group(process)
    passed = code == 0
    if check['kind'] == 'review':
        # CLI exit zero alone is never review approval. A trusted review
        # adapter must emit an explicit verdict for the exact artifact hash.
        try:
            if stdout_path.stat().st_size > 65536:
                raise ValueError('Review output too large')
            verdict = json.loads(stdout_path.read_text())
            passed = passed and verdict.get('approved') is True and verdict.get('revision') == revision
        except (ValueError, OSError):
            passed = False
    result = dict(name=check['name'], kind=check['kind'], argv=check['argv'],
                  revision=revision, passed=passed, exit_code=code,
                  started_at=started, finished_at=time.time(),
                  stdout_sha256=hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
                  stderr_sha256=hashlib.sha256(stderr_path.read_bytes()).hexdigest(), logs=str(log_dir))
    write_private(log_dir / 'receipt.json', result)
    return result


def verify(run_id):
    from .workflow import status
    status(run_id)
    with run_lock(run_id):
        registry = Registry()
        try:
            run, job = registry.lookup(run_id), registry.job(run_id)
            receipt, grant = job['result'], job['payload']
            if grant.get('check_files') is not None and check_files(grant['checks'], run.contract.repo_root) != grant['check_files']:
                raise LifecycleError('Trusted acceptance executable/script changed since approval')
            if not receipt or receipt.get('status') != 'cli_completed' or receipt.get('exit_code') != 0:
                raise LifecycleError('Successful execution receipt is required before verification')
            expected = hashlib.sha256(grant['prompt'].encode()).hexdigest()
            if receipt.get('input_receipt', {}).get('sha256') != expected or not receipt['input_receipt']['pipe_complete']:
                raise LifecycleError('Complete original executor input receipt is required')
            if run.phase not in {Phase.EXECUTION_FINISHED, Phase.VERIFYING, Phase.VERIFIED}:
                raise LifecycleError('Run is not ready for verification')
            for check in grant['checks']:
                if check['kind'] == 'review' and check['reviewer'] == grant['request']['cli']:
                    raise LifecycleError('Implementation CLI cannot be its own independent reviewer')
            current = snapshot(run.contract.workdir, grant['artifacts'])
            prior = evidence(registry, run_id)
            if prior and prior['accepted'] and prior['artifact'] == current:
                if run.phase is not Phase.VERIFIED:
                    registry.record_phase(run_id, Phase.VERIFIED, evidence={
                        'artifact_revision': current['revision'], 'acceptance_digest': _digest(prior)})
                return prior
            if run.phase is Phase.VERIFIED:
                raise LifecycleError('Verified artifact changed; create a new request revision')
            if run.phase is Phase.EXECUTION_FINISHED:
                registry.record_phase(run_id, Phase.VERIFYING, evidence={'verification_ref': run_id})
            gate = AcceptanceGate(root=run.contract.workdir)
            results, required = [], {}
            from tempfile import mkdtemp
            for check in grant['checks']:
                logs = Path(mkdtemp(prefix='check-', dir=grant['request']['output_dir']))
                result = _execute(check, run.contract.workdir, current['revision'], logs, grant['directory_identity'])
                results.append(result)
                gate.record(run_id, CheckResult(check['name'], check['kind'], result['passed'],
                                               current['revision'], str(logs / 'receipt.json')))
                required.setdefault(check['kind'], []).append(check['name'])
            after = snapshot(run.contract.workdir, grant['artifacts'])
            if after != current:
                for check in grant['checks']:
                    gate.record(run_id, CheckResult(check['name'], check['kind'], False,
                                                   current['revision'], 'Artifact changed during verification'))
            verdict = gate.evaluate(run_id, artifact=Artifact(after['revision'], tuple(grant['artifacts'])), required=required)
            record = dict(accepted=verdict.accepted, artifact=after, checks=results,
                          missing=verdict.missing, stale=verdict.stale)
            with registry._conn:
                registry._conn.execute('INSERT OR REPLACE INTO lifecycle_verifications VALUES(?,?,?)',
                    (run_id, run.contract.digest(), json.dumps(record, sort_keys=True)))
            if verdict.accepted:
                registry.record_phase(run_id, Phase.VERIFIED, evidence={
                    'artifact_revision': after['revision'], 'acceptance_digest': _digest(record)})
            from .corrections import PersistentCorrectionLedger
            corrections = PersistentCorrectionLedger()
            try:
                for check in grant['checks']:
                    if check.get('correction'):
                        corrections.observe_run(check['correction']['id'], run_id, check_name=check['name'])
            finally:
                corrections.close()
            return record
        finally:
            registry.close()
