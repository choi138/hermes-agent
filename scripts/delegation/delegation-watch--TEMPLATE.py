#!/usr/bin/env python3
"""delegation-watch--<label>.py — Hermes cron MONITOR SOURCE for one delegation.

WHY THIS FILE SHAPE
-------------------
``monitor_script`` accepts only a bare filename inside ~/.hermes/scripts/ —
no arguments (validated by tools/cronjob_tools.py::_validate_cron_script_path).
So the delegation label is carried in the FILENAME:

    delegation-watch--toki-auth.py   ->  label "toki-auth"
    delegation-watch--all.py         ->  every delegation

Copy this template to a new name to watch a new delegation. Do not edit the
body per-label; the label is derived from __file__.

CONTRACT WITH cron/monitor.py
-----------------------------
Output is hashed as EXACT BYTES. Identical output => the agent run is
suppressed entirely (no LLM, no delivery). So this script must be silent-safe:
no timestamps, no counters, deterministic order.

A nonzero exit or empty output is treated by cron/monitor.py as a monitor
SOURCE FAILURE, which alerts the user on EVERY tick (the stored hash is
deliberately left untouched). A flaky SSH hop would therefore spam alerts.
To avoid that we NEVER exit nonzero for a transport problem: an unreachable
host is reported as the stable state line ``<label> unreachable exit=-``.
That is one change notification when connectivity breaks and one when it
recovers, instead of an alert every tick.

The delegation host is the Mac, because that is where the wrapped processes
actually run; this scheduler runs elsewhere. Hermes scrubs the subprocess
environment (SECURITY.md 2.3), so authentication must rely only on on-disk
SSH keys: BatchMode=yes, no agent, no passphrase prompt.
"""

import os
import subprocess
import sys

# SSH alias for the delegation host, defined in the scheduler user's
# ~/.ssh/config. Overridable for testing.
SSH_HOST = os.environ.get("DELEGATION_SSH_HOST", "geunwon-mac")

# Collector living on the delegation host.
REMOTE_COLLECTOR = "$HOME/bin/delegation-collect.py"

CONNECT_TIMEOUT = 10
# Hard ceiling per attempt so a hung hop cannot stall the cron ticker.
ATTEMPT_TIMEOUT = 25
ATTEMPTS = 3


def label_from_filename():
    """Derive the watched label from this script's own filename."""
    name = os.path.basename(os.path.abspath(__file__))
    stem = name[:-3] if name.endswith(".py") else name
    prefix = "delegation-watch--"
    if not stem.startswith(prefix):
        return None
    label = stem[len(prefix):].strip()
    return label or None


def run_remote(label):
    """Return (ok, stdout) from the remote collector. ok=False = transport."""
    remote_cmd = REMOTE_COLLECTOR
    if label != "all":
        # Label is constrained to [A-Za-z0-9._-] by delegate-run.sh and by
        # the filename check below, so it cannot break out of the argument.
        remote_cmd = "%s %s" % (REMOTE_COLLECTOR, label)

    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=%d" % CONNECT_TIMEOUT,
        "-o", "StrictHostKeyChecking=yes",
        SSH_HOST,
        remote_cmd,
    ]

    last = ""
    for _ in range(ATTEMPTS):
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=ATTEMPT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last = "ssh timeout"
            continue
        except OSError as exc:
            last = "ssh spawn failed: %s" % exc
            continue

        if proc.returncode == 0:
            return True, proc.stdout.decode("utf-8", "replace")

        last = proc.stderr.decode("utf-8", "replace").strip() or (
            "ssh exit %d" % proc.returncode
        )

    return False, last


def main():
    label = label_from_filename()
    if label is None:
        # A genuine misconfiguration (wrong filename), not a transient fault.
        # Fail loudly so the broken watcher is reported instead of watching
        # nothing forever.
        sys.stderr.write(
            "filename must look like delegation-watch--<label>.py\n"
        )
        return 2

    bad = set(label) - set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    if bad:
        sys.stderr.write("label has illegal characters: %r\n" % sorted(bad))
        return 2

    ok, output = run_remote(label)
    if not ok:
        # Transport problem: emit a STABLE state line and exit 0 (see the
        # module docstring for why this must not be a source failure).
        print("%s unreachable exit=-" % label)
        return 0

    text = output.strip()
    if not text:
        # Empty output would read as a source failure to cron/monitor.py.
        # The collector already prints "none" for "nothing tracked", so an
        # empty body here means the collector itself misbehaved.
        print("%s broken exit=-" % label)
        return 0

    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
