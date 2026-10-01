#!/usr/bin/env python3
"""Per-run macOS launchd owner for the progress bridge; never controls workers.

Start with `start -- <normal bridge arguments>`, using the parent terminal's
background completion notification. The waiter reports attention as exit 74.
launchd restarts a crashed monitor; persistent recovery budget cannot be reset
by restarting this script. A healthy lifetime renewal does not spend the budget.
"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.delegation_progress import Manifest, Progress, _atomic, _lock, _read
from scripts.delegation_progress_bridge import main as bridge_main

MAX_FAILURES = 5
BACKOFF = (10, 30, 60, 120, 300)


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def read_json(path):
    return json.loads(_read(path.parent, path.name, 32768))


def owned_entry(config_path):
    """Bound even crashes before config/state parsing; a corrupt budget fails closed."""
    directory = config_path.parent
    if not config_path.is_absolute() or config_path != config_path.resolve():
        return 0
    try:
        with _lock(directory / 'launch-budget.lock'):
            budget_path = directory / 'launch-budget.json'
            try:
                budget = read_json(budget_path)
            except FileNotFoundError:
                budget = {'starts': 0}
            if type(budget.get('starts')) is not int or not 0 <= budget['starts'] < MAX_FAILURES:
                state = dict(status='attention', reason='monitor_launch_budget_exhausted', updated_at=time.time())
                _atomic(directory / 'supervision.json', state)
                emit(state)
                return 0
            budget['starts'] += 1
            _atomic(budget_path, budget)
            return supervise(config_path)
    except (ValueError, OSError) as exc:
        if str(exc) == 'run_locked':
            return 0  # An existing owner remains responsible.
        # Parsing failures cannot be repaired by endless launchd restarts.
        state = dict(status='attention', reason='monitor_state_invalid_pending_preserved', updated_at=time.time())
        try:
            _atomic(directory / 'supervision.json', state)
        except OSError:
            pass
        emit(state)
        return 0


def context(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--state-dir', required=True)
    args, _ = parser.parse_known_args(argv)
    manifest = Manifest.load(args.manifest)
    progress = Progress(manifest, args.state_dir)
    if progress._load()['previous'] is None:
        raise ValueError('registration_baseline_required')
    return progress


def supervise(config_path, *, run_bridge=bridge_main, sleep=time.sleep):
    config = read_json(config_path)
    progress = context(config['argv'])
    if config['binding'] != progress.manifest.binding() or config_path != progress.directory / 'supervisor-config.json':
        raise ValueError('supervisor_identity')
    path = progress.directory / 'supervision.json'
    with _lock(progress.directory / 'supervisor.lock'):
        try:
            state = read_json(path)
        except FileNotFoundError:
            state = dict(binding=config['binding'], failures=0, renewals=0, status='starting', in_flight=False)
        if state['binding'] != config['binding']:
            raise ValueError('supervisor_identity')
        if state['status'] in ('finished', 'attention'):
            return 0  # Successful exit disables launchd KeepAlive; waiter reports outcome.
        if state['in_flight']:
            state['failures'] += 1  # Previous process died without returning a receipt.
        while True:
            if state['failures'] >= MAX_FAILURES:
                state.update(status='attention', in_flight=False, updated_at=time.time(),
                             reason='monitor_recovery_exhausted_pending_preserved')
                _atomic(path, state)
                emit(state)
                return 0
            if state['failures']:
                state.update(status='retry_wait', in_flight=False, updated_at=time.time())
                _atomic(path, state)
                sleep(BACKOFF[state['failures'] - 1])
            state.update(status='monitoring', in_flight=True, updated_at=time.time())
            _atomic(path, state)
            started = time.monotonic()
            code = run_bridge(config['argv'])
            state.update(in_flight=False, last_exit=code, updated_at=time.time())
            if code == 0:
                # Never trust a one-shot or an accidental success exit as completion.
                if progress._load()['stopped'] and not progress.peek():
                    state['status'] = 'finished'
                    _atomic(path, state)
                    emit(state)
                    return 0
                code = 74
            if code == 130:
                state.update(status='attention', reason='monitor_interrupted_pending_preserved')
                _atomic(path, state)
                emit(state)
                return 0
            if code == 78:
                state.update(status='attention', reason='profile_delivery_rejected_pending_preserved')
                _atomic(path, state)
                emit(state)
                return 0
            if code == 76:
                state['renewals'] += 1
                # Clear failures only after a full healthy lifetime, never a retry.
                state['failures'] = 0
                state['status'] = 'renewing'
                _atomic(path, state)
                continue
            state['failures'] += 1
            state.update(status='retry_wait', last_runtime=time.monotonic() - started)
            _atomic(path, state)


def launch(argv, *, dry_run=False, wait=True):
    forbidden = {'--once', '--dry-run', '--preflight', '--reconcile-message', '--record-reported-message'}
    if any(arg.split('=', 1)[0] in forbidden for arg in argv):
        raise ValueError('supervisor_requires_continuous_bridge')
    if '--recover-journal' not in argv:
        argv = [*argv, '--recover-journal']
    if bridge_main([*argv, '--dry-run']) != 0:
        raise ValueError('bridge_validation')
    progress = context(argv)
    label = 'ai.hermes.progress.' + progress.manifest.run_id
    config_path = progress.directory / 'supervisor-config.json'
    config = dict(binding=progress.manifest.binding(), argv=argv, label=label)
    if dry_run:
        emit(dict(status='supervisor_validated', run_id=progress.manifest.run_id, owner='launchd'))
        return 0
    if progress.manifest.sender_profile is not None:
        preflight_args = [arg for arg in argv if arg != '--recover-journal']
        if bridge_main([*preflight_args, '--preflight']) != 0:
            raise ValueError('profile_preflight_failed_no_supervisor_started')
    if sys.platform != 'darwin':
        raise ValueError('macos_launchd_required')
    # macOS agent jobs belong to the logged-in GUI bootstrap domain. The user
    # domain can exist yet reject a LaunchAgent bootstrap with error 5.
    domain = f'gui/{os.getuid()}'
    plist_path = progress.directory / 'supervisor.plist'
    with _lock(progress.directory / 'supervisor-install.lock'):
        if config_path.exists():
            if read_json(config_path) != config:
                raise ValueError('supervisor_configuration_changed')
        else:
            _atomic(config_path, config)
        loaded = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
        if not loaded:
            value = dict(Label=label, ProgramArguments=[sys.executable, str(Path(__file__).resolve()),
                         'run', '--config', str(config_path)], RunAtLoad=True,
                         KeepAlive={'SuccessfulExit': False}, ThrottleInterval=10,
                         StandardOutPath=str(progress.directory / 'supervisor.log'),
                         StandardErrorPath=str(progress.directory / 'supervisor-error.log'),
                         WorkingDirectory=str(Path(__file__).resolve().parents[1]), Umask=0o077)
            # launchd plist contains only bounded operator args, never credentials.
            fd = os.open(plist_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                plistlib.dump(value, stream)
            result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist_path)],
                                    capture_output=True, timeout=15)
            if result.returncode:
                raise ValueError('launchd_bootstrap_failed')
    emit(dict(status='supervisor_owned', run_id=progress.manifest.run_id, owner=f'{domain}/{label}'))
    if not wait:
        return 0
    while True:
        try:
            state = read_json(progress.directory / 'supervision.json')
        except FileNotFoundError:
            state = {}
        if state.get('status') in ('finished', 'attention'):
            emit(state)
            return 0 if state['status'] == 'finished' else 74
        # Detect loss of the launchd owner too; pending delivery remains untouched.
        owner = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'],
                               capture_output=True, text=True, timeout=10)
        exited_without_receipt = ('state = not running' in owner.stdout and 'last exit code = 0' in owner.stdout)
        if owner.returncode or exited_without_receipt:
            # Completion can race the first state read and launchctl's response.
            try:
                terminal = read_json(progress.directory / 'supervision.json')
            except (OSError, ValueError):
                terminal = {}
            if terminal.get('status') in ('finished', 'attention'):
                emit(terminal)
                return 0 if terminal['status'] == 'finished' else 74
            emit(dict(status='attention', reason='launchd_owner_unavailable_pending_preserved'))
            return 74
        time.sleep(10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start')
    start.add_argument('--dry-run', action='store_true')
    start.add_argument('--no-wait', action='store_true')
    start.add_argument('bridge_args', nargs=argparse.REMAINDER)
    run = sub.add_parser('run')
    run.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return owned_entry(Path(args.config))
        rest = args.bridge_args[1:] if args.bridge_args[:1] == ['--'] else args.bridge_args
        return launch(rest, dry_run=args.dry_run, wait=not args.no_wait)
    except KeyboardInterrupt:
        emit(dict(status='waiter_interrupted_monitor_remains_owned'))
        return 130
    except Exception as exc:
        emit(dict(status='supervisor_unavailable_pending_preserved', error=type(exc).__name__))
        return 74


if __name__ == '__main__':
    raise SystemExit(main())
