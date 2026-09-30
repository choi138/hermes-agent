import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_codex_task.py"


@pytest.fixture
def cli_case(tmp_path):
    spec = tmp_path / "SPEC.md"
    spec.write_text("Implement the approved bounded change.")
    output = tmp_path / "receipts"
    output.mkdir(mode=0o700)
    args = [
        "--spec", str(spec), "--workdir", str(tmp_path),
        "--allowed-root", str(tmp_path), "--output-dir", str(output),
    ]
    return tmp_path, output, args


def run_cli(args, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, *extra],
        capture_output=True, text=True, timeout=20,
    )


def test_default_and_all_task_classes_reach_real_dry_run(cli_case):
    _, output, args = cli_case
    default = run_cli(args, "--dry-run")
    assert default.returncode == 0, default.stderr
    receipt = json.loads(default.stdout)
    assert receipt["policy"]["task_class"] == "general"
    assert receipt["policy"]["selected"] == {"model": "gpt-6.1-sol", "effort": "medium"}
    assert receipt["argv"][receipt["argv"].index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="medium"' in receipt["argv"]
    assert receipt["sandbox"] == "read-only"
    assert receipt["timeout_seconds"] == 600
    assert receipt["argv"][receipt["argv"].index("-C") + 1] == str(cli_case[0].resolve())
    assert list(output.iterdir()) == []

    expected = {
        "mechanical": ("gpt-6-luna", "low"),
        "bounded": ("gpt-6-luna", "medium"),
        "general": ("gpt-6.1-sol", "medium"),
        "integration": ("gpt-6.1-sol", "high"),
        "complex": ("gpt-6.1-sol", "high"),
        "frontier": ("gpt-6.1-sol", "xhigh"),
    }
    frontier_blocked = run_cli(args, "--dry-run", "--task-class", "frontier")
    assert frontier_blocked.returncode == 74
    assert json.loads(frontier_blocked.stdout)["policy"]["next_action"] == "supply_deeper_analysis_evidence"
    for task_class, pair in expected.items():
        completed = run_cli(args, "--dry-run", "--task-class", task_class,
                            "--deeper-analysis-evidence", "recorded failure analysis")
        assert completed.returncode == 0, completed.stderr
        selected = json.loads(completed.stdout)["policy"]["selected"]
        assert (selected["model"], selected["effort"]) == pair


def test_legacy_flags_are_exact_audited_overrides_and_pin_is_distinct(cli_case):
    _, _, args = cli_case
    legacy = run_cli(args, "--dry-run", "--model", "gpt-6-luna", "--tier", "deep")
    assert legacy.returncode == 0, legacy.stderr
    policy = json.loads(legacy.stdout)["policy"]
    assert policy["selected"] == {"model": "gpt-6-luna", "effort": "high"}
    assert policy["override_source"] == "legacy_override"

    pinned = run_cli(args, "--dry-run", "--selection", "pinned", "--pinned-tier", "max")
    assert pinned.returncode == 0, pinned.stderr
    policy = json.loads(pinned.stdout)["policy"]
    assert policy["selected"] == {"model": "gpt-6.1-sol", "effort": "max"}
    assert policy["override_source"] == "user_pin"


def test_xhigh_max_and_claude_pass_through_are_distinct(cli_case):
    _, _, args = cli_case
    xhigh = run_cli(
        args, "--dry-run", "--model", "gpt-6.1-sol", "--effort", "xhigh",
        "--deeper-analysis-evidence", "high effort missed an invariant",
        "--choice-reason", "deeper cross-system analysis",
    )
    assert xhigh.returncode == 0, xhigh.stderr
    assert json.loads(xhigh.stdout)["policy"]["selected"]["effort"] == "xhigh"

    maximum = run_cli(
        args, "--dry-run", "--model", "gpt-6.1-sol", "--effort", "max",
        "--deeper-analysis-evidence", "high effort failed after revised tests",
        "--hard-judgment", "--high-failure-cost",
        "--choice-reason", "direct max is justified by failure cost",
    )
    assert maximum.returncode == 0, maximum.stderr
    assert json.loads(maximum.stdout)["policy"]["selected"]["effort"] == "max"

    claude = run_cli(
        args, "--dry-run", "--cli", "claude", "--model", "sonnet", "--tier", "deep",
    )
    assert claude.returncode == 0, claude.stderr
    receipt = json.loads(claude.stdout)
    assert receipt["policy"]["policy_version"] is None
    assert receipt["policy"]["reason"].startswith("Claude pass-through")
    assert receipt["argv"][0] == "claude"
    assert receipt["argv"][receipt["argv"].index("--model") + 1] == "sonnet"
    assert receipt["argv"][receipt["argv"].index("--effort") + 1] == "high"


def test_claude_advisor_is_per_run_and_claude_only(cli_case):
    _, _, args = cli_case
    plain = run_cli(args, "--dry-run", "--cli", "claude", "--model", "sonnet")
    assert plain.returncode == 0, plain.stderr
    assert "--advisor" not in json.loads(plain.stdout)["argv"]

    advised = run_cli(args, "--dry-run", "--cli", "claude", "--model", "sonnet", "--advisor", "opus")
    assert advised.returncode == 0, advised.stderr
    argv = json.loads(advised.stdout)["argv"]
    assert argv[argv.index("--advisor") + 1] == "opus"
    assert json.loads(argv[argv.index("--settings") + 1]) == {
        "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": ""}
    }

    for bad in (("--advisor", "opus"), ("--cli", "claude", "--model", "sonnet", "--advisor", "haiku")):
        rejected = run_cli(args, "--dry-run", *bad)
        assert rejected.returncode != 0


def test_blocked_and_skipped_paths_never_create_worker_artifacts(cli_case):
    _, output, args = cli_case
    blocked = run_cli(args, "--missing-context")
    assert blocked.returncode == 74
    assert json.loads(blocked.stdout)["policy"]["action"] == "blocked"
    assert list(output.iterdir()) == []

    skipped = run_cli(args, "--deterministic-tool-sufficient")
    assert skipped.returncode == 0
    assert json.loads(skipped.stdout)["policy"]["action"] == "skipped"
    assert list(output.iterdir()) == []


def test_retry_dry_run_escalates_then_stops(cli_case):
    _, _, args = cli_case
    retry = run_cli(
        args, "--dry-run", "--failure-kind", "approach_failure", "--attempt", "2",
        "--prior-model", "gpt-6-luna", "--prior-effort", "medium",
        "--handoff-ref", "SPEC.md", "--handoff-ref", "tests/failure.txt",
    )
    assert retry.returncode == 0, retry.stderr
    policy = json.loads(retry.stdout)["policy"]
    assert policy["selected"] == {"model": "gpt-6.1-sol", "effort": "high"}
    assert policy["prior_configuration"] == {"model": "gpt-6-luna", "effort": "medium"}
    assert policy["handoff_refs"] == ["SPEC.md", "tests/failure.txt"]

    stopped = run_cli(
        args, "--dry-run", "--failure-kind", "approach_failure", "--attempt", "3",
        "--max-attempts", "3", "--prior-model", "gpt-6.1-sol", "--prior-effort", "max",
    )
    assert stopped.returncode == 74
    assert json.loads(stopped.stdout)["policy"]["action"] == "stop_replan"


def test_stub_worker_records_requested_observed_usage_and_unknown_acceptance(cli_case, tmp_path):
    _, _, args = cli_case
    child = tmp_path / "fake_codex.py"
    child.write_text('''import json, sys
sys.stdin.read()
print(json.dumps({"type":"turn.started", "model":"gpt-6.1-sol", "reasoning_effort":"medium", "argv":sys.argv[1:]}))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":31,"cached_input_tokens":7,"output_tokens":11,"reasoning_tokens":5}}))
sys.exit(19)
''')
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text('''import runpy, subprocess, sys
sys.path.insert(0, sys.argv[1])
import agent.codex_task_runner as runner
original = runner.run_task
child = sys.argv[2]
def popen(argv, **kwargs):
    return subprocess.Popen([sys.executable, child, *argv[1:]], **kwargs)
runner.run_task = lambda request, **kwargs: original(request, popen=popen, **kwargs)
sys.argv = [sys.argv[3], *sys.argv[4:]]
runpy.run_path(sys.argv[0], run_name="__main__")
''')
    completed = subprocess.run(
        [sys.executable, str(bootstrap), str(ROOT), str(child), str(SCRIPT), *args],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 19, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["status"] == "cli_failed"
    assert receipt["execution"]["cli_exit_code"] == 19
    assert receipt["acceptance"]["status"] == "unknown"
    assert receipt["configuration"]["requested"] == {"model": "gpt-6.1-sol", "effort": "medium"}
    assert receipt["configuration"]["serialized"] == {"model": "gpt-6.1-sol", "effort": "medium"}
    assert receipt["configuration"]["observed"] == {"model": "gpt-6.1-sol", "effort": "medium"}
    assert receipt["metrics"]["usage"] == {
        "input_tokens": 31, "cached_input_tokens": 7,
        "output_tokens": 11, "reasoning_tokens": 5, "cache_write_input_tokens": None,
    }
    assert receipt["metrics"]["worker_cost_usd"] is None
    assert receipt["metrics"]["wall_time_seconds"] >= 0
    events = [json.loads(line) for line in Path(receipt["artifact_dir"], "events.jsonl").read_text().splitlines()]
    argv = events[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="medium"' in argv
