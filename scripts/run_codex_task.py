#!/usr/bin/env python3
"""Manual local delegation; --help needs only Python's standard library."""
import argparse
from dataclasses import asdict
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
    parser.add_argument("--tier", choices=("light", "standard", "deep", "max"),
                        help="Legacy effort override; not a classifier")
    parser.add_argument("--selection", choices=("auto", "pinned"),
                        help="Legacy tier selection mode")
    parser.add_argument("--pinned-tier", choices=("light", "standard", "deep", "max"))
    parser.add_argument("--task-class", choices=("mechanical", "bounded", "general", "integration", "complex", "frontier"), default="general")
    parser.add_argument("--risk", choices=("normal", "high"), default="normal")
    parser.add_argument("--ambiguity", choices=("resolved", "unknown", "ambiguous"), default="resolved")
    parser.add_argument("--phase", choices=("single", "analysis", "implementation", "verification"), default="single")
    parser.add_argument("--contract-resolved", action="store_true")
    parser.add_argument("--implementation-class", choices=("bounded", "general"), default="general")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--pinned-model", choices=("gpt-6-luna", "gpt-6.1-sol"))
    parser.add_argument("--pinned-effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--choice-reason")
    parser.add_argument("--deeper-analysis-evidence")
    parser.add_argument("--hard-judgment", action="store_true")
    parser.add_argument("--high-failure-cost", action="store_true")
    prerequisites = parser.add_mutually_exclusive_group()
    prerequisites.add_argument("--missing-context", action="store_true")
    prerequisites.add_argument("--broken-environment", action="store_true")
    prerequisites.add_argument("--deterministic-tool-sufficient", action="store_true")
    parser.add_argument("--failure-kind", choices=("missing_information", "environment", "shallow_reasoning", "misunderstanding", "approach_failure", "repeated_same_defect"))
    parser.add_argument("--prior-model", choices=("gpt-6-luna", "gpt-6.1-sol"))
    parser.add_argument("--prior-effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--prior-action")
    parser.add_argument("--correction-evidence")
    parser.add_argument("--handoff-ref", action="append", default=[])
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="read-only")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--model")
    parser.add_argument("--cli", choices=("codex", "claude"), default="codex")
    parser.add_argument("--advisor", choices=("opus",), help="Claude only: consult this model as advisor for this run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-manifest", help="New absolute manifest path, published before spawn")
    parser.add_argument("--progress-state-dir", help="Private state root outside worktree")
    parser.add_argument("--progress-thread", help="Exact Discord channel/thread ID")
    parser.add_argument("--progress-label", help="Operator task label (local metadata only)")
    parser.add_argument("--progress-code-scope", action="append", default=[], help="Approved relative source file for bounded progress evidence; repeat for the complete task scope")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.codex_task_runner import TaskRequest, WorkerSelection, run_task
    from agent.codex_worker_policy import PolicyInput, decide_worker

    if args.advisor is not None and args.cli != "claude":
        parser.error("--advisor requires --cli claude")

    def cancelled(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, cancelled)
    try:
        legacy = any(value is not None for value in (args.tier, args.selection, args.pinned_tier))
        if args.cli == "claude":
            codex_only = (
                args.task_class != "general" or args.risk != "normal"
                or args.ambiguity != "resolved" or args.phase != "single"
                or args.contract_resolved or args.implementation_class != "general" or args.effort is not None
                or args.pinned_model is not None or args.pinned_effort is not None
                or args.choice_reason is not None or args.deeper_analysis_evidence is not None
                or args.hard_judgment or args.high_failure_cost or args.failure_kind is not None
                or args.prior_model is not None or args.prior_effort is not None
                or args.prior_action is not None or args.correction_evidence is not None
                or args.handoff_ref or args.attempt != 1 or args.max_attempts != 3
            )
            if codex_only or args.model is None:
                raise ValueError("Claude pass-through requires --model and rejects Codex policy flags")
            tier = args.tier or "standard"
            selection_mode = args.selection or "auto"
            selection = WorkerSelection(tier, selection_mode, args.pinned_tier)
            if args.missing_context:
                action, reason, next_action = "blocked", "Required task context is missing", "supply_missing_context"
            elif args.broken_environment:
                action, reason, next_action = "blocked", "The execution environment is broken", "repair_environment"
            elif args.deterministic_tool_sufficient:
                action, reason, next_action = "skipped", "A deterministic tool is sufficient", "use_deterministic_tool"
            else:
                action, reason, next_action = "spawn", "Claude pass-through; Codex policy not applied", "spawn_once"
            policy_receipt = {
                "policy_version": None, "action": action, "task_class": None,
                "selected": {"model": args.model, "effort": selection.metadata()["effort"]},
                "reason": reason,
                "override_source": "user_pin" if selection_mode == "pinned" else "caller_override",
                "prior_configuration": None, "prior_action": None,
                "next_action": next_action, "attempt": 1, "max_attempts": 1,
                "escalated": False, "handoff_refs": [],
            }
            selected_model = args.model
        else:
            if legacy and (args.effort is not None or args.pinned_effort is not None):
                raise ValueError("Do not mix legacy tier flags with direct effort flags")
            tier = args.tier or "standard"
            selection_mode = args.selection or "auto"
            legacy_selection = WorkerSelection(tier, selection_mode, args.pinned_tier)
            model_override = args.model
            effort_override = args.effort
            pinned_model = args.pinned_model
            pinned_effort = args.pinned_effort
            override_source = "caller_override"
            if legacy:
                override_source = "legacy_override"
                if selection_mode == "pinned":
                    pinned_effort = legacy_selection.metadata()["effort"]
                    if model_override is None:
                        pinned_model = "gpt-6.1-sol"
                else:
                    effort_override = legacy_selection.metadata()["effort"]
                    if model_override is None:
                        model_override = "gpt-6.1-sol"
            policy_input = PolicyInput(
                task_class=args.task_class, risk=args.risk, ambiguity=args.ambiguity,
                phase=args.phase, contract_resolved=args.contract_resolved,
                implementation_class=args.implementation_class,
                missing_context=args.missing_context, broken_environment=args.broken_environment,
                deterministic_tool_sufficient=args.deterministic_tool_sufficient,
                model_override=model_override, effort_override=effort_override,
                pinned_model=pinned_model, pinned_effort=pinned_effort,
                override_source=override_source, choice_reason=args.choice_reason,
                deeper_analysis_evidence=args.deeper_analysis_evidence,
                hard_judgment=args.hard_judgment, high_failure_cost=args.high_failure_cost,
                failure_kind=args.failure_kind, prior_model=args.prior_model,
                prior_effort=args.prior_effort, prior_action=args.prior_action,
                correction_evidence=args.correction_evidence,
                handoff_refs=tuple(args.handoff_ref), attempt=args.attempt,
                max_attempts=args.max_attempts,
            )
            decision = decide_worker(policy_input)
            policy_receipt = decision.receipt()
            action, selected_model = decision.action, decision.model
            selection = (legacy_selection if legacy else WorkerSelection.for_effort(
                decision.effort, pinned=decision.override_source == "user_pin"))
        request = None
        if action == "spawn":
            request = TaskRequest(spec=args.spec, workdir=args.workdir, allowed_root=args.allowed_root,
                                  output_dir=args.output_dir, sandbox=args.sandbox, timeout=args.timeout,
                                  model=selected_model, cli=args.cli, selection=selection, advisor=args.advisor,
                                  policy_input=asdict(policy_input) if args.cli == "codex" else None)
        options = (args.progress_manifest, args.progress_state_dir, args.progress_thread, args.progress_label)
        if (any(options) or args.progress_code_scope) and not all(options):
            raise ValueError("All progress options are required")
        if action != "spawn":
            result = {"status": action, "exit_code": 0 if action == "skipped" else 74,
                      "policy": policy_receipt,
                      "configuration": {"requested": policy_receipt["selected"],
                                        "serialized": policy_receipt["selected"],
                                        "observed": {"model": None, "effort": None}},
                      "acceptance": {"status": "unknown", "independent_validation": False}}
        elif any(options):
            from agent.delegation_progress import Progress, register_run, validate_registration
            validate_registration(request, *options, code_scope=args.progress_code_scope)
            def register(checked, run_dir):
                manifest = register_run(checked, run_dir, *options, code_scope=args.progress_code_scope)
                baseline = Progress(manifest, args.progress_state_dir)._load()
                print(json.dumps({"status": "progress_registered", "run_id": manifest.run_id,
                                  "manifest": args.progress_manifest,
                                  "baseline_complete": not baseline.get('observation_errors'),
                                  "baseline_errors": baseline.get('observation_errors', [])}), flush=True)
            result = (request.inspect(policy_receipt) if args.dry_run
                      else run_task(request, before_spawn=register, policy_receipt=policy_receipt))
        else:
            result = (request.inspect(policy_receipt) if args.dry_run
                      else run_task(request, policy_receipt=policy_receipt))
        print(json.dumps(result, ensure_ascii=True))
        return result.get("exit_code", 0)
    except ValueError as exc:
        print(json.dumps({"status": "invalid_request_or_artifact_error", "exit_code": 74,
                          "reason": str(exc)}), file=sys.stderr)
        return 74
    except OSError:
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
