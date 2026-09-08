"""Exercise the real standalone CLI against disposable local git fixtures."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

import pytest


CLI = Path(__file__).resolve().parents[2] / "scripts" / "delegation_progress.py"


def test_help_standalone():
    result = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for command in ("snapshot", "tick", "peek", "ack", "watch"):
        assert command in result.stdout


@pytest.fixture
def fixture_lane(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "fixture.py").write_text("value=1\n")
    for args in (("add", "."), ("commit", "-qm", "baseline")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    (artifacts / "events.jsonl").write_text("")
    data = {"schema_version": 1, "run_id": "cli-fixture", "worktree": str(repo),
            "approved_root": str(tmp_path), "artifact_root": str(artifacts),
            "event_path": str(artifacts / "events.jsonl"),
            "receipt_path": str(artifacts / "status.json"), "task_label": "fixture",
            "thread_id": "123456789012345678", "coordinator_stage": "working"}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data))
    return repo, artifacts, data, manifest, tmp_path / "state"


def invoke(lane, command, *args, check=True):
    _, _, _, manifest, state = lane
    result = subprocess.run([sys.executable, str(CLI), command, "--manifest", str(manifest),
                             "--state-dir", str(state), *args], capture_output=True, text=True, timeout=15)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def test_real_snapshot_tick_peek_ack_and_dry_run(fixture_lane):
    repo, _, _, _, state = fixture_lane
    snapshot = json.loads(invoke(fixture_lane, "snapshot").stdout)
    assert snapshot["baseline"] and not snapshot["changes"]
    assert not state.exists()
    invoke(fixture_lane, "tick", "--dry-run")
    assert not state.exists()
    invoke(fixture_lane, "tick")
    state_file = state / "cli-fixture" / "state.json"
    before = state_file.read_bytes()
    (repo / "fixture.py").write_text("value=2\n")
    preview = json.loads(invoke(fixture_lane, "tick", "--interval", ".001", "--dry-run").stdout)
    assert preview["queued"] and state_file.read_bytes() == before
    result = json.loads(invoke(fixture_lane, "tick", "--interval", ".001").stdout)
    assert [c["path"] for c in result["snapshot"]["changes"]] == ["fixture.py"]
    message = json.loads(invoke(fixture_lane, "peek").stdout)
    assert message == result["queued"][0]
    assert len(message["content"]) <= 1200
    assert message["allowed_mentions"] == {"parse": [], "replied_user": False}
    assert invoke(fixture_lane, "peek", "--format", "text").stdout.strip() == message["content"]
    assert invoke(fixture_lane, "ack", "--id", "wrong", check=False).returncode == 74
    invoke(fixture_lane, "ack", "--id", message["id"], "--dry-run")
    assert json.loads(invoke(fixture_lane, "peek").stdout) == message
    invoke(fixture_lane, "ack", "--id", message["id"])
    assert json.loads(invoke(fixture_lane, "peek").stdout) is None


def wait_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    pytest.fail("fixture watcher did not reach expected state")


def test_watch_fast_exit_duplicate_fencing_and_final_reload(fixture_lane):
    _, artifacts, data, manifest, state = fixture_lane
    argv = [sys.executable, str(CLI), "watch", "--manifest", str(manifest),
            "--state-dir", str(state), "--poll-interval", ".05", "--interval", "300"]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    state_file = state / "cli-fixture" / "state.json"
    try:
        wait_until(state_file.exists)
        duplicate = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 74
        (artifacts / "status.json").write_text('{"status":"cli_completed","exit_code":0}')
        wait_until(lambda: bool(json.loads(state_file.read_text())["pending"]))
        pending = json.loads(invoke(fixture_lane, "peek").stdout)
        assert pending["event"] == "cli_completed"
        assert pending["sequence"] == 1
        invoke(fixture_lane, "ack", "--id", pending["id"])
        # Atomic operator edit, just like the coordinator integration contract.
        data["coordinator_stage"] = "final_verified"
        replacement = manifest.with_suffix(".tmp")
        replacement.write_text(json.dumps(data))
        os.replace(replacement, manifest)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        outputs = [json.loads(line) for line in stdout.splitlines()]
        assert [m["queued"][0]["event"] for m in outputs] == ["cli_completed", "final_verified"]
        assert "레나 최종 검증 완료" in outputs[-1]["queued"][0]["content"]
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


def test_watch_dry_run_and_invalid_manifest_errors_are_safe(fixture_lane):
    _, _, _, manifest, state = fixture_lane
    result = invoke(fixture_lane, "watch", "--dry-run")
    assert json.loads(result.stdout)["snapshot"]["baseline"]
    assert not state.exists()
    manifest.write_text('{"command":"echo RAW_SECRET"}')
    result = invoke(fixture_lane, "snapshot", check=False)
    assert result.returncode == 74
    assert "RAW_SECRET" not in result.stderr
    assert json.loads(result.stderr)["error"] == "상태 확인 불가"
