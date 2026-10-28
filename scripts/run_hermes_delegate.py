#!/usr/bin/env python3
"""Completion-aware Mac entry retaining the installed legacy command handlers.

The release builder places the exact pre-existing hermes_delegate.py alongside
this script as hermes_delegate_preserved.py. No other launcher refactor ships.
"""
from pathlib import Path
import sys


def main(argv=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.task_lifecycle.launcher import invoke, load_legacy
    from agent.task_lifecycle.types import LifecycleError
    try:
        legacy = load_legacy(Path(__file__).resolve().with_name('hermes_delegate_preserved.py'))
        # New launches use this release; a previously started run keeps its paths.
        legacy.RELEASE = Path(__file__).resolve().parents[1]
        legacy.RUNNER = legacy.RELEASE / 'scripts/run_codex_task.py'
        legacy.RUNNER_PYTHON = Path(sys.executable)
        return invoke(legacy, sys.argv[1:] if argv is None else argv)
    except (LifecycleError, OSError) as exc:
        print('hermes-delegate: completion unavailable: ' + str(exc), file=sys.stderr)
        return 74


if __name__ == '__main__':
    raise SystemExit(main())
