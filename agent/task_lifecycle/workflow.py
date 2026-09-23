"""Opt-in CLI workflow. Private grants are trusted operator/gateway input.

The model's SPEC is never a grant. Grants live in the active profile, outside
all worker-writable roots. This is an OS/SSH trust boundary, not authentication
of arbitrary JSON from a model tool. No service, timer, or production activation.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
from uuid import uuid4

import psutil

from agent.codex_task_runner import TaskRequest, WorkerSelection, MAX_SPEC_BYTES
from hermes_constants import get_hermes_home
from .contract import TaskContract, ExecutionAuthority, _digest
from .directory_handoff import open_directory
from .executor import MacExecutor
from .registry import Registry
from .types import LifecycleError, Phase


def read_private(path, *, limit=4 * MAX_SPEC_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LifecycleError('Grant/receipt must be a private caller-owned regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise LifecycleError('Input exceeds limit')
    return data


def write_private(path, data):
    """Exclusive durable publication; never overwrite a previous receipt."""
    path = Path(path)
    encoded = data if isinstance(data, bytes) else json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def host_identity():
    return {'host': socket.gethostname(), 'boot': psutil.boot_time()}


def process_identity(pid):
    p = psutil.Process(pid)
    return {**host_identity(), 'pid': pid, 'started_at': p.create_time(), 'uid': p.uids().real}


def alive(identity):
    if not identity or any(identity.get(k) != v for k, v in host_identity().items()):
        return False
    try:
        p = psutil.Process(identity['pid'])
        return (p.create_time() == identity['started_at'] and p.uids().real == identity['uid']
                and p.is_running() and p.status() != psutil.STATUS_ZOMBIE)
    except (psutil.NoSuchProcess, KeyError):
        return False
    except psutil.AccessDenied as exc:
        raise LifecycleError('Process identity unavailable; do not signal or replay') from exc


def decode_contract(values):
    values = dict(values)
    for key in ('allowed_paths', 'forbidden_actions', 'acceptance_checks'):
        values[key] = tuple(values[key])
    return TaskContract(**values)


def decode_request(values):
    values = dict(values)
    values['selection'] = WorkerSelection(**values['selection'])
    return TaskRequest(**values)


def request_data(request):
    return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(request).items()}


def grant_root():
    home = get_hermes_home().resolve()
    root = home / 'lifecycle' / 'grants'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def load_grant(path):
    path = Path(path)
    if path.resolve().parent != grant_root() or path.is_symlink():
        raise LifecycleError('Grant must be installed in the active profile grant directory')
    grant = json.loads(read_private(path))
    if grant.get('version') != 1 or grant.get('profile_home') != str(get_hermes_home().resolve()):
        raise LifecycleError('Grant profile mismatch')
    if grant.get('host') != socket.gethostname():
        raise LifecycleError('Grant is for a different execution host')
    contract = decode_contract(grant['contract'])
    request = decode_request(grant['request'])
    if any(Path(path).resolve().is_relative_to(Path(root)) for root in contract.allowed_paths):
        raise LifecycleError('Grant must be outside worker-writable scope')
    if request.output_dir.is_relative_to(request.allowed_root):
        raise LifecycleError('Lifecycle artifacts must be outside the worker root')
    if get_hermes_home().resolve().is_relative_to(request.allowed_root):
        raise LifecycleError('Worker root must not contain its control state')
    payload = {k: grant[k] for k in ('request', 'prompt', 'checks', 'artifacts', 'context', 'destination')}
    if 'check_files' in grant:
        payload['check_files'] = grant['check_files']
    if 'spec_sha256' in grant:
        payload['spec_sha256'] = grant['spec_sha256']
    if _digest(payload) != contract.execution_digest:
        raise LifecycleError('Grant execution digest mismatch')
    if not isinstance(grant['prompt'], str) or not 0 < len(grant['prompt'].encode()) <= MAX_SPEC_BYTES:
        raise LifecycleError('Invalid immutable prompt')
    labels = tuple(f"{c['kind']}:{c['name']}" for c in grant['checks'])
    if labels != contract.acceptance_checks or len(set(labels)) != len(labels):
        raise LifecycleError('Required checks differ from contract')
    authority = ExecutionAuthority(**grant['authority']) if isinstance(grant['authority'].get('allowed_paths'), tuple) else ExecutionAuthority(
        **{**grant['authority'], 'allowed_paths': tuple(grant['authority']['allowed_paths'])})
    expected = tuple(tuple(i) for i in grant['directory_identity'])
    if authority._directory_identity != expected:
        raise LifecycleError('Authorized directory objects changed since grant')
    contract.bind(authority, request)
    return grant, contract, request, authority


def submit(path, *, popen=subprocess.Popen):
    grant, contract, request, _ = load_grant(path)
    registry = Registry()
    try:
        submission = registry.submit(contract)
        if submission.outcome == 'conflict':
            raise LifecycleError('Idempotency key payload conflict')
        registry.prepare_job(submission.run_id, grant)
        job = registry.job(submission.run_id)
        # Re-submission may start another cheap supervisor after a lost reply,
        # but the durable worker claim admits at most one actual workload.
        if job['owner'] is None and registry.lookup(submission.run_id).phase is Phase.ACCEPTED:
            script = Path(__file__).resolve().parents[2] / 'scripts' / 'run_codex_task.py'
            with open(os.devnull, 'rb') as null_in, open(os.devnull, 'ab') as null_out:
                popen([sys.executable, str(script), 'lifecycle', '_worker', submission.run_id],
                      cwd='/', stdin=null_in, stdout=null_out, stderr=null_out,
                      start_new_session=True, close_fds=True)
        phase = registry.lookup(submission.run_id).phase
        return {'run_id': submission.run_id, 'phase': phase.value,
                'outcome': submission.outcome, 'complete': phase is Phase.DELIVERED}
    finally:
        registry.close()


class Cancellation:
    def __init__(self, registry, run_id):
        self.registry, self.run_id = registry, run_id
        self.interrupted = False

    def is_set(self):
        return self.interrupted or self.registry.job(self.run_id)['cancel_requested']


def worker(run_id, *, popen=None):
    registry = Registry()
    owner = uuid4().hex
    try:
        if not registry.claim_job(run_id, owner, process_identity(os.getpid())):
            return 0
        grant = registry.job(run_id)['payload']
        # Re-read the trusted grant at the worker boundary; changing a grant
        # after submit never substitutes a new payload for the reserved one.
        stored, contract, request, authority = load_grant(grant_root() / grant['grant_name'])
        if _digest(stored) != _digest(grant):
            raise LifecycleError('Grant changed after reservation')
        current_base = subprocess.run(['git', '-C', contract.workdir, 'rev-parse', 'HEAD'],
                                      capture_output=True, text=True, check=True).stdout.strip()
        if current_base != contract.base_revision:
            raise LifecycleError('Repository base revision changed since approval')
        cancel = Cancellation(registry, run_id)
        previous = signal.signal(signal.SIGTERM, lambda *_: setattr(cancel, 'interrupted', True))
        try:
            executor = MacExecutor(registry, authority=authority, popen=popen,
                                   prompt_bytes=grant['prompt'].encode(), cancel=cancel,
                                   on_result=lambda result: registry.finish_job(run_id, owner, result))
            with open_directory(contract.workdir, authority._directory_identity) as (fd, _):
                executor._execute_pinned(run_id, contract, request, fd)
            if grant['context'].get('corrections'):
                from .corrections import PersistentCorrectionLedger
                corrections = PersistentCorrectionLedger()
                try:
                    for item in grant['context']['corrections']:
                        corrections.observe_run(item['correction_id'], run_id)
                finally:
                    corrections.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except Exception as exc:
        job = registry.job(run_id)
        if job['owner'] == owner and job['result'] is None:
            registry.finish_job(run_id, owner, {'status': 'worker_failed', 'exit_code': 74,
                                               'error_type': type(exc).__name__})
        run = registry.lookup(run_id)
        if run.phase not in {Phase.UNKNOWN, Phase.BLOCKED, Phase.CANCELLED, Phase.DELIVERED}:
            registry.record_phase(run_id, Phase.UNKNOWN, evidence={'reason': 'worker_failed'})
        return 74
    finally:
        registry.close()


def status(run_id):
    registry = Registry()
    try:
        run, job = registry.lookup(run_id), registry.job(run_id)
        if run is None:
            raise LifecycleError('Unknown run')
        live = alive(job['worker']) if job['worker'] else False
        receipt = job['result']
        # A result is committed before the lifecycle exit event. Recover that
        # exact crash gap without replaying execution or inventing success.
        if (receipt and run.phase in {Phase.RUNNING, Phase.UNKNOWN}
                and run.start_evidence and type(receipt.get('process_returncode')) is int):
            registry.reconcile_execution(run_id)
            run = registry.lookup(run_id)
        if not live and not receipt and (run.phase in {Phase.READY, Phase.RUNNING}
                or (run.phase is Phase.ACCEPTED and job['worker'] is not None)):
            registry.record_phase(run_id, Phase.UNKNOWN, evidence={'reason': 'supervisor_missing'})
            run = registry.lookup(run_id)
        return {'run_id': run_id, 'phase': run.phase.value, 'complete': run.phase is Phase.DELIVERED,
                'worker_alive': live, 'execution': receipt,
                'recovery': 'inspect_receipts_do_not_replay' if run.phase in {Phase.UNKNOWN, Phase.BLOCKED} else None}
    finally:
        registry.close()


def cancel(run_id):
    registry = Registry()
    try:
        job = registry.job(run_id)
        if job['result'] is not None:
            raise LifecycleError('Execution has already exited')
        if job['worker'] and not alive(job['worker']):
            raise LifecycleError('Supervisor identity lost; refusing to signal a reused PID')
        # Durable mailbox, not a kill-by-number command. The owning runner
        # checks it and terminates only the Popen group it created.
        registry.request_cancel(run_id)
        return {'run_id': run_id, 'cancel_requested': True}
    finally:
        registry.close()
