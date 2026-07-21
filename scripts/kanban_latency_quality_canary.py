#!/usr/bin/env python3
"""Deterministic canaries for quality-preserving Kanban latency policy.

The canaries use temporary SQLite boards only. They exercise runtime DB/policy
boundaries without provider calls, credentials, live board mutations, or
verification skips. The JSON report combines latency samples with the work and
quality invariants that make those timings meaningful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli import kanban_db as kb


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _bounded_direct(db_path: Path) -> tuple[dict[str, Any], bool]:
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(
            conn,
            title="Apply a deterministic one-owner edit",
            assignee="shinei",
        )
        task = kb.get_task(conn, task_id)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    metrics = {
        "task_count": int(task_count),
        "goal_mode": bool(task.goal_mode),
        "goal_max_turns": task.goal_max_turns,
        "automatic_goal_loops": int(bool(task.goal_mode)),
    }
    passed = metrics == {
        "task_count": 1,
        "goal_mode": False,
        "goal_max_turns": None,
        "automatic_goal_loops": 0,
    }
    return metrics, passed


def _concurrent_correction_tool_metrics(home: Path) -> dict[str, bool]:
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "kanban.db"
    with kb.connect(db_path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    correction = {
        "root_cause_id": "canary-concurrent-root",
        "affected_scope_digest": _digest("canary-concurrent-scope"),
        "policy_or_test_plan_version": "latency-canary-v1",
        "independent_variant": "primary",
    }
    base_args = {
        "title": "Repair concurrent canary finding",
        "assignee": "shinei",
        "initial_status": "running",
        "correction": correction,
    }
    child_code = (
        "import json,sys; from tools import kanban_tools; "
        "print(kanban_tools._handle_create(json.loads(sys.argv[1])))"
    )
    child_env = {
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_DB": str(db_path),
        "HERMES_KANBAN_HOME": str(home),
        "HERMES_KANBAN_WORKSPACES_ROOT": str(home / "workspaces"),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(_REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }

    def launch(tenant: str) -> subprocess.Popen[str]:
        payload = json.dumps({**base_args, "tenant": tenant}, sort_keys=True)
        return subprocess.Popen(
            [sys.executable, "-c", child_code, payload],
            cwd=_REPO_ROOT,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def collect(process: subprocess.Popen[str]) -> dict[str, Any]:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode != 0:
            raise RuntimeError(
                "concurrent correction subprocess failed "
                f"with exit {process.returncode}: {stderr.strip()}"
            )
        output_lines = [line for line in stdout.splitlines() if line.strip()]
        if not output_lines:
            raise RuntimeError("concurrent correction subprocess returned no result")
        payload = json.loads(output_lines[-1])
        if not payload.get("ok"):
            raise RuntimeError(f"concurrent correction canary failed: {payload}")
        return payload

    same_tenant = [
        collect(process) for process in [launch("tenant-a"), launch("tenant-a")]
    ]
    other_tenant = collect(launch("tenant-b"))

    same_task = len({result["task_id"] for result in same_tenant}) == 1
    tenant_isolated = other_tenant["task_id"] != same_tenant[0]["task_id"]
    with kb.connect(db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM correction_lineages WHERE status='active'"
        ).fetchone()[0]
        tenant_a_task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE tenant='tenant-a'"
        ).fetchone()[0]
        tenant_b_task_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE tenant='tenant-b'"
        ).fetchone()[0]

    converged = bool(same_task and tenant_a_task_count == 1)
    isolated = bool(tenant_isolated and tenant_b_task_count == 1)
    return {
        "single_active_correction": bool(converged and active_count == 2),
        "concurrent_correction_tool_path": converged,
        "correction_tenant_isolated": bool(isolated and active_count == 2),
    }


def _durable_implementation_qa_final(db_path: Path) -> tuple[dict[str, Any], bool]:
    children = [
        {
            "title": "Implement the bounded change",
            "assignee": "shinei",
            "parents": [],
        },
        {
            "title": "Independently verify acceptance criteria",
            "assignee": "raiden",
            "parents": [0],
        },
        {
            "title": "Judge final completion",
            "assignee": "default",
            "parents": [1],
        },
    ]
    with kb.connect(db_path) as conn:
        root_id = kb.create_task(
            conn,
            title="Durable implementation with independent QA",
            assignee="default",
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="default",
            children=children,
            author="latency-canary",
        )
        assert child_ids is not None
        child_tasks = [kb.get_task(conn, task_id) for task_id in child_ids]

        wide_root = kb.create_task(
            conn,
            title="Four parallel specialists",
            assignee="default",
            triage=True,
        )
        wide_child_ids = kb.decompose_triage_task(
            conn,
            wide_root,
            root_assignee="default",
            children=[
                {"title": f"parallel specialist {index}", "parents": []}
                for index in range(4)
            ],
            author="latency-canary",
        )

        too_deep_root = kb.create_task(
            conn,
            title="Speculative four-stage graph",
            assignee="default",
            triage=True,
        )
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        fourth_stage_rejected = False
        try:
            kb.decompose_triage_task(
                conn,
                too_deep_root,
                root_assignee="default",
                children=[
                    {"title": "stage 1", "parents": []},
                    {"title": "stage 2", "parents": [0]},
                    {"title": "stage 3", "parents": [1]},
                    {"title": "stage 4", "parents": [2]},
                ],
                author="latency-canary",
            )
        except ValueError as exc:
            fourth_stage_rejected = "at most 3 stages" in str(exc)
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        retry_task_id = kb.create_task(
            conn,
            title="Retry one transient root cause",
            assignee="shinei",
        )
        kb.claim_task(conn, retry_task_id)
        first_blocked = kb._record_task_failure(
            conn,
            retry_task_id,
            error="provider connection reset by peer",
            outcome="crashed",
            release_claim=True,
            end_run=True,
            failure_limit=2,
        )
        kb.claim_task(conn, retry_task_id)
        second_blocked = kb._record_task_failure(
            conn,
            retry_task_id,
            error="provider connection reset by peer",
            outcome="crashed",
            release_claim=True,
            end_run=True,
            failure_limit=2,
        )
        retry_task = kb.get_task(conn, retry_task_id)

    correction_metrics = _concurrent_correction_tool_metrics(
        db_path.parent / "correction-tool-home"
    )
    repeated_root = (
        "no_retry"
        if not first_blocked and second_blocked and retry_task.status == "blocked"
        else "unexpected"
    )
    metrics = {
        "initial_card_count": len(child_ids),
        "fourth_stage_rejected": fourth_stage_rejected and before == after,
        "independent_qa": (
            child_tasks[1].assignee == "raiden"
            and child_tasks[1].assignee != child_tasks[0].assignee
        ),
        "parallel_cards_without_hard_cap": (
            wide_child_ids is not None and len(wide_child_ids) == 4
        ),
        "same_root_second_observation": repeated_root,
        **correction_metrics,
    }
    passed = metrics == {
        "initial_card_count": 3,
        "fourth_stage_rejected": True,
        "independent_qa": True,
        "parallel_cards_without_hard_cap": True,
        "same_root_second_observation": "no_retry",
        "single_active_correction": True,
        "concurrent_correction_tool_path": True,
        "correction_tenant_isolated": True,
    }
    return metrics, passed


def _high_risk_verification(db_path: Path) -> tuple[dict[str, Any], bool]:
    observed_at = int(time.time())
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(
            conn, title="Verify a high-risk release", assignee="raiden"
        )
        task = kb.claim_task(conn, task_id, claimer="latency-canary:1")
        assert task is not None and task.current_run_id is not None and task.claim_lock
        intent_id = "ti_ca7a7a7a00000001"
        handoff = {
            "result": None,
            "summary": "deterministic canary verification",
            "metadata": {"verification_class": "focused_test"},
            "verified_cards": [],
        }
        manifest = {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": task.current_run_id,
            "terminal_intent_id": intent_id,
            "action": "complete",
            "block_kind": None,
            "source_commit": _digest("canary-commit"),
            "source_tree": _digest("canary-tree"),
            "config_digest": _digest("canary-config"),
            "lockfile_digest": _digest("canary-lockfile"),
            "toolchain_digest": _digest("canary-toolchain"),
            "backend_kind": "local",
            "backend_digest": _digest("canary-backend"),
            "command_digest": _digest("canary-input"),
            "test_plan_digest": _digest("canary-test-plan"),
            "fixture_digest": _digest("canary-fixture"),
            "seed_digest": _digest("canary-seed"),
            "policy_version": "latency-canary-v1",
            "evidence_at": observed_at,
            "freshness_seconds": 3600,
            "failure_class": "none",
            "checkpoint_digest": _digest("canary-artifact"),
            "side_effect": "none",
        }
        kb.create_terminal_intent(
            conn,
            terminal_intent_id=intent_id,
            task_id=task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            action="complete",
            decision="verified",
            failure_class="none",
            manifest=manifest,
            provenance_digest=kb.evidence_manifest_digest(manifest),
            handoff=handoff,
        )
        exact = {
            "input_digest": manifest["command_digest"],
            "artifact_digest": manifest["checkpoint_digest"],
            "toolchain_digest": manifest["toolchain_digest"],
            "environment_digest": kb._run_evidence_environment_digest(manifest),
            "test_plan_digest": manifest["test_plan_digest"],
            "policy_version": manifest["policy_version"],
            "reusable_class": "focused_test",
        }
        low_risk = kb.record_shadow_evidence_decision(
            conn,
            task_id=task_id,
            terminal_intent_id=intent_id,
            risk="low",
            verdict_source="deterministic_test",
            external_side_effect=False,
            stale=False,
            flaky=False,
            observed_at=observed_at + 1,
            **exact,
        )
        high_risk = kb.record_shadow_evidence_decision(
            conn,
            task_id=task_id,
            terminal_intent_id=intent_id,
            risk="high",
            verdict_source="reviewer",
            external_side_effect=True,
            stale=False,
            flaky=False,
            observed_at=observed_at + 2,
            **{**exact, "reusable_class": "prohibited"},
        )

    verification_skips = int(low_risk["verification_skipped"]) + int(
        high_risk["verification_skipped"]
    )
    metrics = {
        "low_risk_candidate_hit": bool(low_risk["candidate_hit"]),
        "high_risk_candidate_hit": bool(high_risk["candidate_hit"]),
        "verification_skips": verification_skips,
    }
    passed = metrics == {
        "low_risk_candidate_hit": True,
        "high_risk_candidate_hit": False,
        "verification_skips": 0,
    }
    return metrics, passed


def _latency_summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "median": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_index], 3),
    }


def run_canaries(*, iterations: int = 20) -> dict[str, Any]:
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 1
    ):
        raise ValueError("iterations must be a positive integer")

    canary_functions: list[
        tuple[str, Callable[[Path], tuple[dict[str, Any], bool]]]
    ] = [
        ("bounded_direct", _bounded_direct),
        ("durable_implementation_qa_final", _durable_implementation_qa_final),
        ("high_risk_verification", _high_risk_verification),
    ]
    results = []
    for name, function in canary_functions:
        samples: list[float] = []
        observed_metrics: list[dict[str, Any]] = []
        checks: list[bool] = []
        for _ in range(iterations):
            with tempfile.TemporaryDirectory(prefix=f"hermes-{name}-") as temp_dir:
                started = time.perf_counter_ns()
                metrics, passed = function(Path(temp_dir) / "kanban.db")
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            samples.append(elapsed_ms)
            observed_metrics.append(metrics)
            checks.append(passed)
        metrics_stable = all(item == observed_metrics[0] for item in observed_metrics)
        results.append({
            "name": name,
            "passed": all(checks) and metrics_stable,
            "samples": iterations,
            "latency_ms": _latency_summary(samples),
            "metrics": observed_metrics[0],
        })

    quality_regressions = sum(not item["passed"] for item in results)
    automatic_goal_loops = sum(
        int(item["metrics"].get("automatic_goal_loops", 0)) for item in results
    )
    verification_skips = sum(
        int(item["metrics"].get("verification_skips", 0)) for item in results
    )
    summary = {
        "canaries_passed": len(results) - quality_regressions,
        "canaries_total": len(results),
        "quality_regressions": quality_regressions,
        "automatic_goal_loops": automatic_goal_loops,
        "initial_graph_max_stages": kb.MAX_INITIAL_DECOMPOSITION_STAGES,
        "verification_skips": verification_skips,
    }
    return {
        "schema_version": 1,
        "passed": quality_regressions == 0 and verification_skips == 0,
        "summary": summary,
        "canaries": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    report = run_canaries(iterations=args.iterations)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
