"""Bounded, checksum-selective SSH sync-back transport (Python 3 on the remote host)."""
import json
import os
import selectors
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from tools.environments.file_sync import SyncBackRefused

# Self-contained standard-library program: no Hermes installation on the SSH target required.
# stdin carries the manifest, stdout carries ONLY the tar, stderr carries short diagnostics.
_REMOTE_PROGRAM = r'''
import hashlib, json, os, stat, sys, tarfile, time
request = json.load(sys.stdin)
deadline = time.monotonic() + request['timeout']
limit = request['max_bytes']
excluded = set(request['excluded'])
class Refused(Exception): pass
def check():
    if time.monotonic() >= deadline:
        raise Refused('sync-back deadline exceeded')
class Output:
    def __init__(self): self.size = 0
    def write(self, data):
        check()
        if self.size + len(data) > limit:
            raise Refused('sync-back archive exceeds byte limit')
        sys.stdout.buffer.write(data)
        self.size += len(data)
    def flush(self): sys.stdout.buffer.flush()
def paths():
    roots = []
    for root in sorted(set(request['roots']), key=len):
        if not any(root == p or root.startswith(p + '/') for p in roots):
            roots.append(root)
    for root in roots:
        check()
        if os.path.islink(root) or not os.path.isdir(root): continue
        def fail(error): raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
            check()
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(directory, d))]
            for name in files:
                path = os.path.join(directory, name)
                if path not in excluded: yield path
try:
    output = Output()
    with tarfile.open(fileobj=output, mode='w|', format=tarfile.PAX_FORMAT) as archive:
        for path in paths():
            check()
            try:
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode): continue
                # Nonblocking + nofollow also handles replacement with a FIFO/symlink.
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            with os.fdopen(fd, 'rb') as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode): continue
                digest = hashlib.sha256()
                while True:
                    check()
                    chunk = source.read(65536)
                    if not chunk: break
                    digest.update(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise Refused('file changed while hashing')
                if digest.hexdigest() == request['hashes'].get(path): continue
                if before.st_size > limit: raise Refused('sync-back file exceeds byte limit')
                source.seek(0)
                member = tarfile.TarInfo(path.lstrip('/'))
                member.size = before.st_size
                member.mode = stat.S_IMODE(before.st_mode)
                member.mtime = before.st_mtime
                archive.addfile(member, source)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise Refused('file changed while archiving')
    output.flush()
except (Refused, OSError) as error:
    print(str(error), file=sys.stderr)
    sys.exit(65)
'''


if os.name == 'nt':
    # Windows selectors cannot watch anonymous pipes. Peek without consuming so
    # os.read never blocks beyond the shared deadline, including during shutdown.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _peek_pipe = ctypes.WinDLL('kernel32', use_last_error=True).PeekNamedPipe
    _peek_pipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _peek_pipe.restype = wintypes.BOOL


def _read_chunk(pipe, size: int, deadline: float) -> bytes:
    """Wait for pipe data without a background thread or an unbounded read."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SSH sync-back deadline exceeded')
        if os.name == 'nt':
            available = wintypes.DWORD()
            if not _peek_pipe(msvcrt.get_osfhandle(pipe.fileno()), None, 0,
                              None, ctypes.byref(available), None):
                error = ctypes.get_last_error()
                if error == 109:  # ERROR_BROKEN_PIPE: all writers closed
                    return b''
                raise ctypes.WinError(error)
            if available.value:
                return os.read(pipe.fileno(), min(size, available.value))
            time.sleep(min(0.01, remaining))
        else:
            with selectors.DefaultSelector() as selector:
                selector.register(pipe, selectors.EVENT_READ)
                if selector.select(remaining):
                    return os.read(pipe.fileno(), size)


def download_changes(ssh_command: list[str], dest: Path, request: dict, timeout: float) -> None:
    """Stream with a deadline and byte cap, even from interpreter-finalization cleanup."""
    deadline = time.monotonic() + timeout
    payload = dict(request, timeout=timeout)
    command = ssh_command + ['python3 -c ' + shlex.quote(_REMOTE_PROGRAM)]
    # File-backed input/error channels cannot deadlock on full subprocess pipes.
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stderr:
        stdin.write(json.dumps(payload).encode()); stdin.seek(0)
        with subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr) as proc:
            try:
                remaining = request['max_bytes']
                with dest.open('wb') as output:
                    while True:
                        chunk = _read_chunk(proc.stdout, min(65536, remaining + 1), deadline)
                        if not chunk:
                            break
                        if len(chunk) > remaining:
                            raise SyncBackRefused('sync-back archive exceeds byte limit')
                        output.write(chunk)
                        remaining -= len(chunk)
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise TimeoutError('SSH sync-back deadline exceeded') from error
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            if proc.returncode:
                stderr.seek(0)
                diagnostic = stderr.read(8192).decode(errors='replace').strip()
                if proc.returncode in (65, 127):
                    raise SyncBackRefused(f'SSH sync-back refused: {diagnostic}')
                raise OSError(f'SSH sync-back failed (rc={proc.returncode}): {diagnostic}')
