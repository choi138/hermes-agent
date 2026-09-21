#!/usr/bin/env python3
"""Manual local delegation; --help needs only Python's standard library."""
import argparse
import json
from pathlib import Path
import signal
import sys


def main():
    if sys.argv[1:2] == ["lifecycle"]:
        return lifecycle_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="Absolute UTF-8 SPEC file inside approved root")
    parser.add_argument("--workdir", required=True, help="Absolute verified working directory")
    parser.add_argument("--allowed-root", required=True, help="Root approved by the caller, never inferred from the SPEC")
    parser.add_argument("--output-dir", required=True, help="Existing caller-owned artifact directory; each run is unique")
    parser.add_argument("--tier", choices=("light", "standard", "deep", "max"), default="standard")
    parser.add_argument("--selection", choices=("auto", "pinned"), default="auto")
    parser.add_argument("--pinned-tier", choices=("light", "standard", "deep", "max"))
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--cli", choices=("codex", "claude"), default="codex")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-manifest", help="New absolute manifest path, published before spawn")
    parser.add_argument("--progress-state-dir", help="Private state root outside worktree")
    parser.add_argument("--progress-thread", help="Exact Discord channel/thread ID")
    parser.add_argument("--progress-label", help="Operator task label (local metadata only)")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.codex_task_runner import TaskRequest, WorkerSelection, run_task

    def cancelled(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, cancelled)
    try:
        request = TaskRequest(spec=args.spec, workdir=args.workdir, allowed_root=args.allowed_root,
                              output_dir=args.output_dir, sandbox=args.sandbox, timeout=args.timeout,
                              model=args.model, cli=args.cli,
                              selection=WorkerSelection(args.tier, args.selection, args.pinned_tier))
        options = (args.progress_manifest, args.progress_state_dir, args.progress_thread, args.progress_label)
        if any(options) and not all(options):
            raise ValueError("All progress options are required")
        if any(options):
            from agent.delegation_progress import Progress, register_run, validate_registration
            validate_registration(request, *options)
            def register(checked, run_dir):
                manifest = register_run(checked, run_dir, *options)
                baseline = Progress(manifest, args.progress_state_dir)._load()
                print(json.dumps({"status": "progress_registered", "run_id": manifest.run_id,
                                  "manifest": args.progress_manifest,
                                  "baseline_complete": not baseline.get('observation_errors'),
                                  "baseline_errors": baseline.get('observation_errors', [])}), flush=True)
            result = request.inspect() if args.dry_run else run_task(request, before_spawn=register)
        else:
            result = request.inspect() if args.dry_run else run_task(request)
        print(json.dumps(result, ensure_ascii=True))
        return result.get("exit_code", 0)
    except (ValueError, OSError):
        print(json.dumps({"status": "invalid_request_or_artifact_error", "exit_code": 74}), file=sys.stderr)
        return 74
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "exit_code": 130}), file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


def lifecycle_main(argv):
    parser = argparse.ArgumentParser(description="Opt-in durable lifecycle; active HERMES_HOME owns state")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("import-request", help="Trusted authenticated SSH intake from gateway; reads stdin")
    sub.add_parser("export-result", help="Return verified result/attachment to source gateway over SSH").add_argument("run_id")
    prepare = sub.add_parser("prepare", help="Trusted operator intake; config must be private and outside the workdir")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--request-key", required=True)
    prepare.add_argument("--revision", required=True)
    sub.add_parser("submit").add_argument("--grant", required=True)
    queue = sub.add_parser("queue-result", help="Queue verified result in active profile delivery ledger")
    queue.add_argument("run_id")
    queue.add_argument("--content-file", required=True)
    queue.add_argument("--attachment", action="append", default=[])
    for action in ("status", "receipt", "cancel", "verify", "_worker"):
        sub.add_parser(action).add_argument("run_id")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.task_lifecycle import workflow
    from agent.task_lifecycle.types import LifecycleError
    try:
        if args.action == "_worker":
            return workflow.worker(args.run_id)
        if args.action == "export-result":
            from agent.task_lifecycle.remote_result import export_result
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536:
                raise LifecycleError("Result request too large")
            result = export_result(args.run_id, **json.loads(raw))
        elif args.action == "import-request":
            from agent.task_lifecycle.intake import import_gateway_envelope
            raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise LifecycleError("Gateway envelope too large")
            result = import_gateway_envelope(json.loads(raw))
        elif args.action == "prepare":
            import os
            from agent.task_lifecycle.intake import create_grant
            config = json.loads(workflow.read_private(args.config))
            request = workflow.decode_request(config.pop("request"))
            if Path(args.config).resolve().is_relative_to(request.allowed_root):
                raise LifecycleError("Operator config must be outside worker-writable scope")
            if set(config) - {"request_text", "objective", "checks", "artifacts", "forbidden_actions",
                              "context", "note_bindings", "correction_ids", "work_class", "agentsx"}:
                raise LifecycleError("Local config cannot supply gateway identity, destination or approval")
            path = create_grant(request=request, owner=f"operator:{os.getuid()}",
                origin=f"local:{args.request_key}", request_revision=args.revision,
                profile="default", **config)
            result = {"grant": str(path), "status": "prepared", "complete": False}
        elif args.action == "submit":
            result = workflow.submit(args.grant)
        elif args.action == "queue-result":
            from agent.task_lifecycle.handoff import queue_result
            oid = queue_result(args.run_id, Path(args.content_file).read_text(), attachments=args.attachment)
            result = {"obligation_id": oid, "status": "delivery_pending", "complete": False}
        elif args.action == "verify":
            from agent.task_lifecycle.verification import verify
            result = verify(args.run_id)
        elif args.action == "cancel":
            result = workflow.cancel(args.run_id)
        else:
            result = workflow.status(args.run_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("accepted") is not False else 1
    except (LifecycleError, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc), "complete": False}), file=sys.stderr)
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
