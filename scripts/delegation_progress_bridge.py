#!/usr/bin/env python3
"""Foreground Mac bridge: observe local evidence and drain via explicit SSH sender.

Exit 75 retains pending delivery for operator action; exit 0 requires final ack.
No daemon, autostart, worker control or credentials on this machine.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import signal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--ssh-host', required=True)
    parser.add_argument('--remote-python', required=True)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--helper-path', required=True)
    parser.add_argument('--allow-thread', action='append', required=True)
    parser.add_argument('--interval', type=float, default=1200)
    parser.add_argument('--poll-interval', type=float, default=10)
    parser.add_argument('--sender-timeout', type=float, default=40)
    parser.add_argument('--max-runtime', type=float, default=86400, help='Finite foreground lifetime; pending state survives exit 75')
    parser.add_argument('--reconcile-message', help='GET-only verification of exact pending Discord message ID')
    parser.add_argument('--recover-journal', action='store_true', help='Recover uncertain delivery through GET-only server journal lookup')
    parser.add_argument('--server-state-dir', help='Private durable server journal directory')
    parser.add_argument('--sender-profile', choices=('default', 'koharu'))
    parser.add_argument('--expected-bot-id')
    parser.add_argument('--preflight', action='store_true', help='Read-only server credential/identity/target check before supervisor start')
    parser.add_argument('--record-reported-message', help='GET-verify and acknowledge the exact NOTICE head sent by coordinator')
    parser.add_argument('--once', action='store_true', help='Drain at most one item, then exit')
    parser.add_argument('--dry-run', action='store_true', help='Validate and preview; no writes or SSH')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.delegation_progress import Manifest, Progress
    from agent.delegation_progress_delivery import Delivery, SSHSender
    from scripts.delegation_progress_discord_send import validate_profile

    def emit(value):
        print(json.dumps(value, ensure_ascii=True), flush=True)

    def interrupted(*_):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        if (not math.isfinite(args.poll_interval) or not 0 < args.poll_interval <= 60
                or not math.isfinite(args.max_runtime) or not 0 < args.max_runtime <= 86400):
            raise ValueError('interval')
        manifest = Manifest.load(args.manifest)
        validate_profile(args.sender_profile, args.expected_bot_id)
        if (args.sender_profile != manifest.sender_profile or args.expected_bot_id != manifest.expected_bot_id
                or args.preflight and (manifest.sender_profile is None or args.reconcile_message
                    or args.record_reported_message or args.recover_journal)):
            emit({'status': 'rejected', 'reason': 'profile_mismatch'})
            return 78
        if manifest.thread_id not in args.allow_thread:
            raise ValueError('thread_not_allowed')
        progress = Progress(manifest, args.state_dir, interval=args.interval, light_poll=True)
        sender = SSHSender(args.ssh_host, args.remote_python, args.runtime_root, args.helper_path,
                           args.allow_thread, timeout=args.sender_timeout,
                           sender_profile=args.sender_profile, expected_bot_id=args.expected_bot_id,
                           server_state_dir=args.server_state_dir or str(Path(args.runtime_root) / '.delegation-progress-delivery'))
        sender.argv(args.reconcile_message)  # Validate even in dry-run.
        if args.dry_run:
            progress.tick(dry_run=True)
            emit({'status': 'validated', 'run_id': manifest.run_id})
            return 0
        if args.preflight:
            content = '작업: 진행 중이에요.'
            value = dict(run_id=manifest.run_id, sequence=1, thread_id=manifest.thread_id,
                         content=content, content_digest=hashlib.sha256(content.encode()).hexdigest(),
                         operation='CARD_CREATE', event_id=manifest.run_id + ':preflight',
                         card_receipt=None, **manifest.sender_identity())
            result = sender.send(value, preflight=True)
            emit(result)
            return 0 if result['status'] == 'ready' else 78
        if progress._load()['previous'] is None:
            raise ValueError('registration_baseline_required')
        delivery = Delivery(progress, sender)
        deadline = time.monotonic() + args.max_runtime
        reconcile = args.reconcile_message
        reported = args.record_reported_message
        with progress.watcher():
            while True:
                updated = Manifest.load(args.manifest)
                if updated.binding() != manifest.binding():
                    raise ValueError('manifest_identity_changed')
                progress.manifest = updated
                result = ({'stopped': progress._load()['stopped']} if args.once and (reported or reconcile)
                          else progress.tick())
                while progress.peek() is not None:
                    receipt = delivery.drain_one(reconcile_message=reconcile, reported_message=reported,
                                                 recover_journal=args.recover_journal)
                    reconcile = None
                    reported = None
                    emit(receipt)
                    if receipt['status'] != 'verified':
                        if manifest.sender_profile is not None and receipt['status'] == 'rejected':
                            return 78  # Operator must repair; supervisor must not blindly retry.
                        return 75
                    if args.once:
                        return 0
                if args.once:
                    return 0
                if progress._load()['stopped']:
                    emit({'status': 'stopped_and_delivered', 'run_id': manifest.run_id})
                    return 0
                if time.monotonic() >= deadline:
                    emit({'status': 'lifetime_expired', 'run_id': manifest.run_id})
                    return 76
                time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        emit({'status': 'interrupted_pending_preserved'})
        return 130
    except Exception:
        emit({'status': 'bridge_unavailable_pending_preserved'})
        return 74
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
