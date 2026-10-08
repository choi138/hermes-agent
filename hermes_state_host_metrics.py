"""Small, best-effort Linux snapshots for slow SQLite events, off the writer."""
from __future__ import annotations

from pathlib import Path
import sys
import time


def snapshot() -> dict:
    """Cumulative counters permit deltas between events; PSI averages cover 10s.

    These are correlation evidence, not proof of a particular external process.
    Read only kernel pseudo-files, never database/user files or process listings.
    A missing procfs/PSI interface must not disable transaction diagnostics.
    """
    if sys.platform != 'linux':
        return {}
    result = {'sample_monotonic': time.monotonic()}
    try:
        with open('/proc/stat') as stream:
            ticks = [int(n) for n in stream.readline().split()[1:]]
        # guest/guest_nice already contribute to user/nice; don't count twice.
        result['cpu_ticks'] = {'total': sum(ticks[:8]), 'iowait': ticks[4]}
    except (OSError, ValueError, IndexError):
        pass
    for resource in ('io', 'cpu'):
        try:
            lines = Path(f'/proc/pressure/{resource}').read_text().splitlines()
            result[f'{resource}_pressure'] = {
                fields[0]: {key: float(value) for key, value in
                            (item.split('=', 1) for item in fields[1:])
                            if key in {'avg10', 'total'}}
                for fields in (line.split() for line in lines)
            }
        except (OSError, ValueError, IndexError):
            pass
    try:
        entries = (line.split(':', 1) for line in Path('/proc/self/io').read_text().splitlines())
        result['process_io_bytes'] = {key: int(value) for key, value in entries
                                      if key in {'read_bytes', 'write_bytes', 'rchar', 'wchar'}}
    except (OSError, ValueError):
        pass
    return result
