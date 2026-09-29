#!/usr/bin/env python3
"""Local evidence/outbox CLI. No network sender and no worker mutation."""
import argparse
import json
import math
from pathlib import Path
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "tick", "peek", "ack", "watch", "set-stage", "begin-validation", "record-validation", "migrate-v1", "claim-report"))
    parser.add_argument("--stage", choices=("working", "verifying", "final_verified", "stopped"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--id", help="Exact head outbox ID for ack")
    parser.add_argument("--interval", type=float, default=1200)
    parser.add_argument("--poll-interval", type=float, default=10)
    parser.add_argument('--ticket')
    parser.add_argument('--evidence-ref', help='Relative result log under manifest artifact_root')
    parser.add_argument('--exit-code', type=int)
    parser.add_argument('--gate', choices=('pytest', 'canonical'), default='canonical')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.delegation_progress import Manifest, Progress, render

    def emit(value):
        if args.format == "json":
            print(json.dumps(value, ensure_ascii=True), flush=True)
        elif value and "content" in value:
            print(value["content"], flush=True)
        elif value and ("snapshot" in value or "changes" in value):
            print(render(value.get("snapshot", value)), flush=True)
        else:
            print("처리했어요." if value else "전송 대기 메시지 없음.", flush=True)

    try:
        progress = Progress(Manifest.load(args.manifest), args.state_dir, interval=args.interval)
        if not math.isfinite(args.poll_interval) or not 0 < args.poll_interval <= 60:
            raise ValueError("poll_interval")
        if args.command == 'claim-report':
            from agent.delegation_progress_delivery import Delivery
            value = Delivery(progress, None).claim_notice(dry_run=args.dry_run)
        elif args.command in ('begin-validation', 'record-validation', 'migrate-v1'):
            from agent.delegation_progress_evidence import begin, record, migrate
            if args.command == 'begin-validation':
                value = begin(progress, dry_run=args.dry_run)
            elif args.command == 'migrate-v1':
                value = migrate(progress, dry_run=args.dry_run)
            else:
                value = record(progress, args.ticket, args.evidence_ref, args.exit_code, args.gate, dry_run=args.dry_run)
        elif args.command == "set-stage":
            from agent.delegation_progress import set_stage
            value = set_stage(args.manifest, args.stage, dry_run=args.dry_run)
        elif args.command == "snapshot":
            value = progress.snapshot()
        elif args.command == "tick":
            value = progress.tick(dry_run=args.dry_run)
        elif args.command == "peek":
            value = progress.peek()
        elif args.command == "ack":
            value = progress.ack(args.id, dry_run=args.dry_run)
        elif args.dry_run:
            value = progress.tick(dry_run=True)
        else:
            with progress.watcher():
                while True:
                    try:
                        updated = Manifest.load(args.manifest)
                        if updated.binding() != progress.manifest.binding():
                            raise ValueError("manifest_identity_changed")
                        progress.manifest = updated
                        progress.manifest_error = False
                    except (OSError, ValueError):
                        # Retain the last trusted routing identity, report the read gap.
                        progress.manifest_error = True
                    value = progress.tick()
                    if value["queued"]:
                        emit(value)
                    if value["stopped"]:
                        return 0
                    time.sleep(args.poll_interval)
        emit(value)
        return 0
    except (ValueError, OSError, RecursionError) as exc:
        if str(exc) == 'final_verified_requires_begin_validation_and_record_validation':
            print(json.dumps({'error': str(exc), 'compatibility': 'Use begin-validation before the gate, then record-validation with the ticket and actual log.'}), file=sys.stderr)
            return 74
        print(json.dumps({"error": "상태 확인 불가", "status": "unavailable"}, ensure_ascii=True), file=sys.stderr)
        return 74
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
