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
    snapshot = json.loads(invoke(fixture_lane, 'snapshot').stdout)
    assert snapshot['baseline'] and not snapshot['changes'] and not state.exists()
    invoke(fixture_lane, 'tick', '--dry-run')
    assert not state.exists()
    result = json.loads(invoke(fixture_lane, 'tick').stdout)
    message = json.loads(invoke(fixture_lane, 'peek').stdout)
    assert message == result['queued'][0] and message['operation'] == 'CARD_CREATE'
    state_file = state / 'cli-fixture/state.json'
    before = state_file.read_bytes()
    (repo / 'fixture.py').write_text('value=2\n')
    preview = json.loads(invoke(fixture_lane, 'tick', '--interval', '1', '--dry-run').stdout)
    assert preview['snapshot']['changes'] and state_file.read_bytes() == before
    assert not preview['queued']
    assert invoke(fixture_lane, 'tick', '--interval', '.001', check=False).returncode == 74
    assert invoke(fixture_lane, 'peek', '--format', 'text').stdout.strip() == message['content']
    assert invoke(fixture_lane, 'ack', '--id', 'wrong', check=False).returncode == 74
    invoke(fixture_lane, 'ack', '--id', message['id'], '--dry-run')
    assert json.loads(invoke(fixture_lane, 'peek').stdout) == message
    invoke(fixture_lane, 'ack', '--id', message['id'])
    assert json.loads(invoke(fixture_lane, 'peek').stdout) is None


def wait_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    pytest.fail("fixture watcher did not reach expected state")


def test_watch_fast_exit_duplicate_fencing_and_final_reload(fixture_lane):
    _, artifacts, data, manifest, state = fixture_lane
    argv = [sys.executable, str(CLI), 'watch', '--manifest', str(manifest),
            '--state-dir', str(state), '--poll-interval', '.05']
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    state_file = state / 'cli-fixture/state.json'
    try:
        wait_until(state_file.exists)
        assert subprocess.run(argv, capture_output=True, text=True, timeout=10).returncode == 74
        first = json.loads(invoke(fixture_lane, 'peek').stdout)
        assert first['operation'] == 'CARD_CREATE'
        invoke(fixture_lane, 'ack', '--id', first['id'])
        (artifacts / 'status.json').write_text('{"status":"cli_completed","exit_code":0}')
        wait_until(lambda: json.loads(state_file.read_text()).get('receipt'))
        # Worker exit now queues an immediate transition before parent review.
        wait_until(lambda: len(json.loads(state_file.read_text())['pending']) == 2)
        for message in json.loads(state_file.read_text())['pending']:
            invoke(fixture_lane, 'ack', '--id', message['id'])
        assert process.poll() is None
        data['coordinator_stage'] = 'stopped'
        replacement = manifest.with_suffix('.tmp')
        replacement.write_text(json.dumps(data))
        os.replace(replacement, manifest)
        wait_until(lambda: json.loads(state_file.read_text()).get('closing'))
        assert process.poll() is None
        pending = json.loads(state_file.read_text())['pending']
        assert [m['operation'] for m in pending] == ['CARD_PATCH', 'NOTICE']
        for message in pending:
            invoke(fixture_lane, 'ack', '--id', message['id'])
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert json.loads(state_file.read_text())['stopped']
    finally:
        if process.poll() is None:
            data['coordinator_stage'] = 'stopped'
            manifest.write_text(json.dumps(data))
            for _ in range(20):
                if state_file.exists():
                    for message in json.loads(state_file.read_text())['pending']:
                        invoke(fixture_lane, 'ack', '--id', message['id'], check=False)
                if process.poll() is not None:
                    break
                time.sleep(.1)
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
