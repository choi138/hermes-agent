"""Mac directory-object handoff through the existing runner's Popen seam.

Trusted intake snapshots the canonical workdir's entire directory chain. A
submission opens each component relative to the previous FD, without following
symlinks, and compares identities *on those FDs*. Rename/restore cannot change
an acquired handle. Intake construction itself is the authorization boundary;
this does not authenticate the caller, protect file contents within the cwd,
or defend against kernel/process-memory control or inode deletion/reuse.

Only Darwin is supported: /dev/fd directory chdir does not work there. An
isolated Python bootstrap fchdirs then execs in the same PID/session. NOTE_EXEC
is armed before releasing the bootstrap, so its exit cannot masquerade as a
workload exit. No parent chdir, preexec_fn, shell, or path-based fallback.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import sys
import time

from agent import codex_task_runner
from .types import LifecycleError


def require_support():
    if (sys.platform != "darwin" or not hasattr(select, "kqueue")
            or os.open not in os.supports_dir_fd or not hasattr(os, "fchdir")):
        raise LifecycleError("Directory handoff requires Darwin openat/fchdir/kqueue")


@contextmanager
def open_directory(path, expected=None):
    """Yield (workdir_fd, chain identities); all acquired FDs close on exit.

    Paths here are already canonical authority paths. Never resolve them again
    during acquisition: each openat is anchored to the checked parent object.
    """
    require_support()
    parts = Path(path).parts
    if not parts or parts[0] != "/" or ".." in parts:
        raise LifecycleError("Handoff needs a canonical absolute directory")
    if expected is not None and len(expected) != len(parts):
        raise LifecycleError("Directory authority chain mismatch")
    fd = None
    identities = []
    try:
        for index, part in enumerate(parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            if fd is not None:
                os.close(fd)
            fd = child
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if expected is not None and identity != expected[index]:
                raise LifecycleError("Authorized directory object changed")
            identities.append(identity)
        yield fd, tuple(identities)
    except OSError as exc:
        raise LifecycleError("Cannot acquire authorized directory without symlinks") from exc
    finally:
        if fd is not None:
            os.close(fd)


# Inline code avoids reopening this module through a mutable source pathname
# in the child. -I -S prevents cwd/PYTHONPATH/site startup code from running.
BOOTSTRAP = """
import os, sys
directory, gate = map(int, sys.argv[1:3])
if os.read(gate, 1) != b'1':
    os._exit(126)
os.close(gate)
os.fchdir(directory)
os.close(directory)
os.execvp(sys.argv[4], sys.argv[4:])
"""


def _confirm_exec(process, queue, release, deadline):
    queue.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                flags=select.KQ_EV_ADD,
                                fflags=select.KQ_NOTE_EXEC | select.KQ_NOTE_EXIT)], 0, 0)
    os.write(release, b"1")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError("Directory bootstrap timed out before workload exec")
        events = queue.control(None, 1, remaining)
        for event in events:
            if event.ident == process.pid and event.fflags & select.KQ_NOTE_EXEC:
                return
            if event.flags & select.KQ_EV_ERROR or event.fflags & select.KQ_NOTE_EXIT:
                raise OSError("Directory bootstrap exited before workload exec")


def spawn_pinned(popen, argv, *, directory_fd, request, deadline, **kwargs):
    """Popen seam: same workload argv except -C '.', same pipes and session.

    Independent harmless probes may replace only argv after the '--' marker
    in the injected popen. Replacing the entire bootstrap would skip the
    directory handoff and will fail exec confirmation.
    """
    require_support()
    if argv != request.argv() or kwargs.get("cwd") != str(request.workdir):
        raise LifecycleError("Unexpected runner directory/argv handoff")
    if kwargs.get("shell") is not False or kwargs.get("start_new_session") is not True:
        raise LifecycleError("Handoff requires shell=False and owned process group")
    if "preexec_fn" in kwargs or "pass_fds" in kwargs or "executable" in kwargs:
        raise LifecycleError("Unexpected runner process override")
    workload = list(argv)
    if request.cli == "codex":
        workload[workload.index("-C") + 1] = "."
    read, write = os.pipe()
    queue = None
    process = None
    try:
        queue = select.kqueue()
        bootstrap = [sys.executable, "-I", "-S", "-c", BOOTSTRAP,
                     str(directory_fd), str(read), "--", *workload]
        kwargs.update(cwd="/", pass_fds=(directory_fd, read))
        process = popen(bootstrap, **kwargs)
        _confirm_exec(process, queue, write, deadline)
        return process
    except BaseException:
        if process is not None:
            try:
                codex_task_runner._stop_group(process)
            finally:
                for name in ("stdin", "stdout", "stderr"):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        stream.close()
        raise
    finally:
        if queue is not None:
            queue.close()
        os.close(read)
        os.close(write)
