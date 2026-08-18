"""Terminal children run at lower CPU priority than the gateway.

Gateway and terminal children share one systemd service cgroup, where
CPU weights don't apply between processes — plain nice(1) does. See
_terminal_child_nice() in tools/environments/local.py (2026-07-28
event-loop stall: agent-spawned codex fleet starved the gateway inside
its own cgroup).
"""

import os
import subprocess
import sys
import time

import pytest

from tools.environments.local import _nice_argv, _terminal_child_nice


def test_nice_argv_prefixes_default():
    argv = ["/bin/bash", "-lic", "set +m; true"]
    out = _nice_argv(argv)
    if sys.platform == "win32":
        assert out == argv
        return
    assert out[-3:] == argv
    assert out[0].endswith("nice")
    assert out[1:3] == ["-n", "10"]


def test_nice_disabled_via_env(monkeypatch):
    monkeypatch.setenv("HERMES_TERMINAL_CHILD_NICE", "0")
    argv = ["/bin/true"]
    assert _nice_argv(argv) == argv


def test_nice_env_clamped(monkeypatch):
    monkeypatch.setenv("HERMES_TERMINAL_CHILD_NICE", "99")
    assert _terminal_child_nice() == 19
    monkeypatch.setenv("HERMES_TERMINAL_CHILD_NICE", "garbage")
    assert _terminal_child_nice() == 10


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux-only")
def test_spawned_child_runs_10_below_parent():
    """nice(1) is RELATIVE: the child lands at parent+10 (clamped to 19).

    Asserting an absolute 10 flaked whenever the test runner itself was
    niced (e.g. `nice -n 10 pytest ...` full-shard runs → child at 19).
    Production matches the relative contract too: the gateway runs at 0,
    its terminal children at 10.
    """
    env = dict(os.environ)
    env.pop("HERMES_TERMINAL_CHILD_NICE", None)  # shard hygiene
    expected = min(19, os.nice(0) + 10)
    proc = subprocess.Popen(
        _nice_argv(["sleep", "20"]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        # Poll: under a loaded host the exec chain can lag a fixed sleep.
        deadline = time.monotonic() + 10.0
        nice_val = None
        while time.monotonic() < deadline:
            with open(f"/proc/{proc.pid}/stat") as fh:
                after_comm = fh.read().rsplit(")", 1)[1].split()
            # after ')': state ppid pgrp session tty tpgid flags minflt
            # cminflt majflt cmajflt utime stime cutime cstime priority
            # nice -> idx 16
            nice_val = int(after_comm[16])
            if nice_val == expected:
                break
            time.sleep(0.1)
        assert nice_val == expected
    finally:
        proc.kill()
        proc.wait(timeout=5)
