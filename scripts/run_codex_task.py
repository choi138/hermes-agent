#!/usr/bin/env python3
"""Manual local delegation; --help needs only Python's standard library."""
import argparse
import json
from pathlib import Path
import signal
import sys


def main():
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
                              model=args.model, selection=WorkerSelection(args.tier, args.selection, args.pinned_tier))
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


if __name__ == "__main__":
    raise SystemExit(main())
