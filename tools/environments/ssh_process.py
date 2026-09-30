"""Fixed SSH process observation protocol; never enters environment/file synchronization."""
import base64
import json
import shlex
import threading

# Four observation slots per (host, user, port) in this interpreter; not a host-wide budget.
_limits = {}
_limits_lock = threading.Lock()

# Runs on the remote host. Reads only private artifacts for one execution.
_ARTIFACT_BASE = r'''
import os, stat as _stat
def artifact_read(path, mode='r'):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PermissionError('unsafe execution artifact')
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise
def artifact_base(root, execution, create=False):
    legacy = root + '/hermes_bg_' + execution
    private = legacy + '.claim'
    if create and not os.path.exists(legacy + '.identity'):
        try: os.mkdir(private, mode=0o700)
        except FileExistsError: pass
    # Old v2 executions have an empty claim directory and sibling artifacts.
    # They remain readable; new v3 executions keep ALL artifacts inside it.
    if os.path.lexists(private):
        info = os.lstat(private)
        if not _stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('unsafe execution directory')
        if os.path.exists(private + '/process.identity') or not os.path.exists(legacy + '.identity'):
            return private + '/process'
    # A legacy artifact may only suppress dispatch or certify completion if owned safely.
    if os.path.lexists(legacy + '.identity'):
        with artifact_read(legacy + '.identity'):
            pass
    return legacy
'''

_OBSERVE = _ARTIFACT_BASE + r'''
import base64, fcntl, json, os, subprocess, sys
root, execution, offset = sys.argv[1], sys.argv[2], int(sys.argv[3])
result = {'state': 'unavailable', 'execution': execution, 'operation': 'identity'}
def failure(kind, exc):
    return kind + (': errno=' + str(exc.errno) if isinstance(exc, OSError) else ': ' + type(exc).__name__)
try:
    base = artifact_base(root, execution)
    with artifact_read(base + '.identity') as f: identity = json.load(f)
    if identity['execution'] != execution:
        result['state'] = 'identity_mismatch'
    else:
        result['identity'] = identity
        result['operation'] = 'receipt'
        try:
            with artifact_read(base + '.receipt') as f: receipt = json.load(f)
        except FileNotFoundError: receipt = None
        except (OSError, ValueError) as exc:
            receipt = None
            result['error'] = failure('invalid_receipt', exc)
        if receipt is not None:
            if receipt.get('identity') != identity:
                result['state'] = 'identity_mismatch'
            elif type(receipt.get('exit_code')) is int:
                result.update(state='exited', exit_code=receipt['exit_code'],
                              cancel_confirmed=receipt.get('cancel_confirmed') is True,
                              cancellation_scope='direct_process_group',
                              execution_tree_termination_confirmed=False,
                              execution_seconds=receipt.get('execution_seconds'))
                if receipt.get('startup_error'):
                    result['startup_error'] = receipt['startup_error']
            else:
                result['error'] = 'invalid_receipt'
        else:
            result['operation'] = 'process_identity'
            # The execution wrapper holds this kernel lock for its entire lifetime.
            # Unlike ps lstart/PID, an unrelated recycled PID cannot satisfy it.
            if identity.get('protocol') in (2, 3):
                with artifact_read(base + '.alive', 'rb') as alive:
                    try:
                        fcntl.flock(alive, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        result['state'] = 'running'
            else:
                result['error'] = 'legacy_identity_requires_receipt'
            try:
                with artifact_read(base + '.failure') as f: diagnostic = json.load(f)
            except FileNotFoundError: diagnostic = None
            if diagnostic is not None:
                if diagnostic.get('identity') != identity:
                    result['error'] = 'wrapper_failure_identity_mismatch'
                elif isinstance(diagnostic.get('error'), str):
                    result.update(error='wrapper_failure: ' + diagnostic['error'][:256], operation='wrapper')
                else:
                    result['error'] = 'invalid_wrapper_failure'
except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
    result['error'] = failure('invalid_identity_or_receipt', exc)
try:
    with artifact_read(base + '.log', 'rb') as f:
        stat = os.fstat(f.fileno())
        size = stat.st_size
        log_identity = [stat.st_dev, stat.st_ino]
        offset = offset if offset <= size else 0
        f.seek(offset)
        data = f.read(min(65536, size - offset))
    result['log'] = {'offset': offset, 'next': offset + len(data), 'size': size,
                     'identity': log_identity, 'data': base64.b64encode(data).decode('ascii')}
except (OSError, NameError) as exc:
    if not result.get('error'):
        result.update(error=failure('log_unavailable', exc), operation='log')
if result.get('error') and not result['error'].startswith('log_unavailable'):
    result['state'] = 'unavailable'
print('HERMES_PROCESS_OBSERVATION_V1:' + json.dumps(result))
'''

# The wrapper owns its receipt and publishes it atomically after its child has exited.
_RUN = _ARTIFACT_BASE + r'''
import base64, fcntl, json, os, shlex, signal, subprocess, sys, tempfile, time, uuid
root, execution, cwd = sys.argv[1:]
command = sys.stdin.buffer.read().decode('utf-8')
command_umask = os.umask(0o077)
os.makedirs(root, exist_ok=True)
base = artifact_base(root, execution, create=True)
if not base.endswith('.claim/process'): sys.exit(0)  # Existing legacy execution.
claim = os.path.dirname(base)
try:
    fd = os.open(claim + '/dispatch', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
except FileExistsError: sys.exit(0)
def publish(suffix, value):
    path = base + suffix
    fd, temporary = tempfile.mkstemp(dir=claim)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
alive = open(base + '.alive', 'wb')
fcntl.flock(alive, fcntl.LOCK_EX | fcntl.LOCK_NB)
identity = {'execution': execution, 'pid': os.getpid(), 'protocol': 3, 'start': uuid.uuid4().hex}
publish('.identity', identity)
def wrapper_failure(kind, exc, traceback):
    # Detached stderr has no consumer. Preserve only type/errno, never exception text.
    diagnostic = type(exc).__name__ + (': errno=' + str(exc.errno) if isinstance(exc, OSError) else '')
    try:
        publish('.failure', {'identity': identity, 'error': diagnostic})
    except OSError:
        # If all artifact writes fail, the absent receipt still prevents false completion.
        return
sys.excepthook = wrapper_failure
execution_start = time.monotonic()
cancel_confirmed = False
startup_error = None
rc = -1
child = None
script = None
try:
    with open(base + '.log', 'ab', buffering=0) as log:
        if os.path.exists(base + '.cancel'):
            rc = -15
            cancel_confirmed = True
        else:
            with tempfile.NamedTemporaryFile(dir=claim, delete=False) as script:
                if '\0' in command:
                    raise ValueError('Shell command contains a NUL byte')
                # The terminator makes every incomplete or failed read non-successful.
                script.write(command.encode('utf-8') + b'\0'); script.flush()
                # Reopen after login initialization; no inherited descriptor must survive it.
                bootstrap = 'IFS= builtin read -r -d "" _hermes_command < ' + shlex.quote(script.name) + ' || exit 125; builtin eval "$_hermes_command"'
                # Resolve relative paths once, before either Popen or login startup changes cwd.
                requested_cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else None
                if requested_cwd:
                    # Login startup may cd elsewhere; bind the requested effect directory afterward.
                    bootstrap = 'builtin cd -- ' + shlex.quote(requested_cwd) + ' || exit 125; ' + bootstrap
                if requested_cwd and not os.path.isdir(requested_cwd):
                    startup_error = 'Invalid workdir: supply an existing remote directory or omit the workdir override'
                    raise ValueError('Invalid workdir')
                child = subprocess.Popen(['bash', '-lc', bootstrap],
                                         stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True,
                                         preexec_fn=lambda: os.umask(command_umask),
                                         cwd=requested_cwd)
            signal.signal(signal.SIGTERM, lambda *_: os.killpg(child.pid, signal.SIGTERM))
            cancel_at = None
            while child.poll() is None:
                if os.path.exists(base + '.cancel'):
                    if cancel_at is None:
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                            cancel_confirmed = True
                        except ProcessLookupError: pass
                        cancel_at = time.monotonic()
                    elif time.monotonic() - cancel_at > 5:
                        try: os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                time.sleep(.1)
            rc = child.wait()
except (OSError, ValueError) as exc:
    if child is not None:
        raise  # Do not certify completion while a child may still be running.
    if startup_error is None:
        startup_error = type(exc).__name__ + (': errno=' + str(exc.errno) if isinstance(exc, OSError) else '')
    if isinstance(exc, ValueError) and '\0' in command:
        startup_error = 'Invalid command: literal NUL bytes are forbidden; use a textual shell escape when intended'
finally:
    if script is not None:
        try: os.unlink(script.name)
        except FileNotFoundError: pass
publish('.receipt', {'identity': identity, 'exit_code': rc,
                     'cancel_confirmed': cancel_confirmed and rc != 0,
                     'cancellation_scope': 'direct_process_group',
                     'execution_tree_termination_confirmed': False,
                     'startup_error': startup_error,
                     'execution_seconds': max(0, time.monotonic() - execution_start)})
'''


# Foreground bootstrap consumes the payload before detaching. Neither the SSH
# client, remote login shell nor detached wrapper carries the command in argv.
_LAUNCH = r'''
import os, subprocess, sys, tempfile
command_umask = os.umask(0o077)
with tempfile.TemporaryFile() as payload:
    payload.write(sys.stdin.buffer.read()); payload.flush(); payload.seek(0)
    child = subprocess.Popen([sys.executable, '-c', sys.argv[4], *sys.argv[1:4]],
                             stdin=payload, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             preexec_fn=lambda: os.umask(command_umask))
print('HERMES_PROCESS_LAUNCH_V1:' + str(child.pid))
'''


def launch_command(root, execution, cwd=''):
    """Fixed bootstrap; send the user's command separately via stdin_data."""
    return shlex.join(['python3', '-c', _LAUNCH, root, execution, cwd or '', _RUN])


class ObservationCapacityBusy(Exception):
    """No remote request was made: local observation slots are occupied."""


def decode_observation(stdout):
    """Ignore login chatter; require one complete, explicitly framed protocol payload."""
    marker = 'HERMES_PROCESS_OBSERVATION_V1:'
    frames = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
    if len(frames) != 1:
        raise ValueError('missing or duplicate process observation frame')
    return json.loads(frames[0])


def observe(env, root, execution, offset):
    if not execution.startswith('proc_') or not execution.replace('_', '').isalnum():
        raise ValueError('invalid execution identity')
    with _limits_lock:
        limit = _limits.setdefault((env.host, env.user, env.port), threading.BoundedSemaphore(4))
    if not limit.acquire(timeout=1):
        raise ObservationCapacityBusy('SSH observation concurrency limit')
    try:
        result = env._run_ssh(shlex.join(['python3', '-c', _OBSERVE, root, execution, str(offset)]), timeout=10)
    finally:
        limit.release()
    if result.returncode:
        raise ConnectionError(f'SSH observation failed (rc={result.returncode}): ' + safe_error(RuntimeError(result.stderr or 'no SSH diagnostic')))
    data = decode_observation(result.stdout)
    if data.get('execution') != execution or data.get('state') not in {
        'running', 'exited', 'unavailable', 'identity_mismatch'
    }:
        raise ValueError('invalid process observation')
    chunk = data.get('log')
    if chunk is not None:
        raw = base64.b64decode(chunk['data'], validate=True)
        if (len(raw) > 65536 or chunk['offset'] not in (0, offset)
                or chunk['next'] != chunk['offset'] + len(raw)
                or chunk['size'] < chunk['next']):
            raise ValueError('incomplete process log payload')
        chunk['bytes'] = raw
    return data


def request_cancel(env, root, execution):
    # The wrapper signals its OWN child. No PID received from a stale registry is killed.
    if not execution.startswith('proc_') or not execution.replace('_', '').isalnum():
        raise ValueError('invalid execution identity')
    script = _ARTIFACT_BASE + r'''
import sys
path = artifact_base(*sys.argv[1:], create=True) + '.cancel'
fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
try:
    if os.fstat(fd).st_uid != os.getuid(): raise PermissionError('foreign cancellation marker')
    os.fsync(fd)
finally: os.close(fd)
'''
    result = env._run_ssh(shlex.join(['python3', '-c', script, root, execution]), timeout=5)
    if result.returncode:
        raise ConnectionError(f'remote cancellation request unconfirmed (rc={result.returncode}): ' + safe_error(RuntimeError(result.stderr or 'no SSH diagnostic')))


def safe_error(exc):
    """Keep actionable transport categories without leaking subprocess argv/payload."""
    import subprocess
    from agent.redact import redact_for_egress
    if isinstance(exc, subprocess.TimeoutExpired):
        return f'TimeoutExpired: deadline {exc.timeout}s exceeded'
    if isinstance(exc, subprocess.CalledProcessError):
        return f'CalledProcessError: exit status {exc.returncode}'
    if isinstance(exc, OSError) and exc.errno is not None:
        return f'{type(exc).__name__}: errno={exc.errno}'
    return redact_for_egress(f'{type(exc).__name__}: {exc}')[:500]
