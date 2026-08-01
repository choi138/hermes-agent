from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import kanban_latency_quality_canary as canary_module
from scripts.kanban_latency_quality_canary import run_canaries


def test_canaries_measure_avoidable_work_without_skipping_quality_gates():
    report = run_canaries(iterations=2)

    assert report["schema_version"] == 1
    assert report["passed"] is True
    assert report["summary"] == {
        "canaries_passed": 3,
        "canaries_total": 3,
        "quality_regressions": 0,
        "automatic_goal_loops": 0,
        "initial_graph_max_stages": 3,
        "verification_skips": 0,
    }

    canaries = {item["name"]: item for item in report["canaries"]}
    assert set(canaries) == {
        "bounded_direct",
        "durable_implementation_qa_final",
        "high_risk_verification",
    }

    direct = canaries["bounded_direct"]
    assert direct["passed"] is True
    assert direct["metrics"]["task_count"] == 1
    assert direct["metrics"]["goal_mode"] is False
    assert direct["metrics"]["goal_max_turns"] is None

    durable = canaries["durable_implementation_qa_final"]
    assert durable["passed"] is True
    assert durable["metrics"]["initial_card_count"] == 3
    assert durable["metrics"]["fourth_stage_rejected"] is True
    assert durable["metrics"]["independent_qa"] is True
    assert durable["metrics"]["parallel_cards_without_hard_cap"] is True
    assert durable["metrics"]["same_root_second_observation"] == "no_retry"
    assert durable["metrics"]["single_active_correction"] is True
    assert durable["metrics"]["concurrent_correction_tool_path"] is True
    assert durable["metrics"]["correction_tenant_isolated"] is True

    high_risk = canaries["high_risk_verification"]
    assert high_risk["passed"] is True
    assert high_risk["metrics"]["low_risk_candidate_hit"] is True
    assert high_risk["metrics"]["high_risk_candidate_hit"] is False
    assert high_risk["metrics"]["verification_skips"] == 0

    for item in canaries.values():
        assert item["samples"] == 2
        assert item["latency_ms"]["median"] >= 0
        assert item["latency_ms"]["p95"] >= item["latency_ms"]["median"]


def test_canary_summary_derives_work_counters_from_observations(monkeypatch):
    monkeypatch.setattr(
        canary_module,
        "_bounded_direct",
        lambda _path: ({"automatic_goal_loops": 2}, True),
    )
    monkeypatch.setattr(
        canary_module,
        "_durable_implementation_qa_final",
        lambda _path: ({"initial_card_count": 3}, True),
    )
    monkeypatch.setattr(
        canary_module,
        "_high_risk_verification",
        lambda _path: ({"verification_skips": 1}, True),
    )

    report = run_canaries(iterations=1)

    assert report["summary"]["automatic_goal_loops"] == 2
    assert report["summary"]["verification_skips"] == 1
    assert report["passed"] is False


def test_canaries_require_a_positive_iteration_count():
    with pytest.raises(ValueError, match="iterations must be a positive integer"):
        run_canaries(iterations=0)


def test_canary_cli_imports_the_checkout_outside_repo_cwd(tmp_path):
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "kanban_latency_quality_canary.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--iterations", "1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["passed"] is True
    assert report["summary"]["canaries_passed"] == 3
