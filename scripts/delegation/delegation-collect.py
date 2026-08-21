#!/usr/bin/env python3
"""delegation-collect.py — emit STABLE status lines for wrapped delegations.

Runs on the Mac (the machine that actually hosts the delegated processes).
A Hermes cron monitor script SSHes in and hashes this output, so the output
MUST be stable while nothing meaningful changes:

  * no timestamps, no durations, no byte/line counters
  * deterministic ordering (sorted by label)
  * only the coarse lifecycle state

That means a still-running delegation produces byte-identical output tick
after tick and the agent is never woken. The bytes change only when a
delegation reaches done/failed, or goes quiet long enough to be stalled.

Usage:
  delegation-collect.py            # all delegations
  delegation-collect.py <label>    # one delegation

Output (one line per delegation):
  <label> <status> exit=<code|->

status: running | done | failed | stalled | broken
Exit code is 0 when nothing is being tracked (silence is a valid state).
"""

import json
import os
import sys
import time

ROOT = os.path.join(os.path.expanduser("~"), ".delegations")

# A running delegation whose log has not been touched for this long is
# reported as stalled. Override with DELEGATION_STALL_SECONDS.
try:
    STALL_SECONDS = int(os.environ.get("DELEGATION_STALL_SECONDS", "1200"))
except ValueError:
    STALL_SECONDS = 1200


def classify(entry_dir):
    """Return the coarse lifecycle state for one delegation directory."""
    state_path = os.path.join(entry_dir, "state.json")
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (IOError, OSError, ValueError):
        # No readable state file: the wrapper never got far enough, or the
        # file is mid-write / corrupt. Report it rather than hiding it.
        return "broken", "-"

    status = str(state.get("status") or "broken")
    raw_code = state.get("exit_code")
    code = "-" if raw_code is None else str(raw_code)

    if status != "running":
        # Terminal states are authoritative and freeze the output.
        return status, code

    # Running: escalate to stalled when the log has gone quiet. Compare
    # against the log, not state.json, because the wrapper only rewrites
    # state.json at start and at exit.
    log_path = os.path.join(entry_dir, "log")
    try:
        quiet_for = time.time() - os.path.getmtime(log_path)
    except (IOError, OSError):
        return "broken", code
    if quiet_for > STALL_SECONDS:
        return "stalled", code
    return "running", code


def main(argv):
    wanted = argv[1] if len(argv) > 1 else None

    if not os.path.isdir(ROOT):
        print("none")
        return 0

    if wanted is not None:
        labels = [wanted] if os.path.isdir(os.path.join(ROOT, wanted)) else []
    else:
        labels = sorted(
            name for name in os.listdir(ROOT)
            if os.path.isdir(os.path.join(ROOT, name))
        )

    if not labels:
        print("none")
        return 0

    for label in labels:
        status, code = classify(os.path.join(ROOT, label))
        print("%s %s exit=%s" % (label, status, code))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
