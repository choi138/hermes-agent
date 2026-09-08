"""Local fixtures only: no Codex, sender, Discord, or API calls."""
import json
from pathlib import Path
import subprocess

import pytest

from agent.delegation_progress import Manifest, Progress, collect, render


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout


@pytest.fixture
def lane(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "runner.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture baseline")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    (artifacts / "events.jsonl").write_text("")
    data = dict(schema_version=1, run_id="fixture-run", worktree=str(repo),
                approved_root=str(tmp_path), artifact_root=str(artifacts),
                event_path=str(artifacts / "events.jsonl"),
                receipt_path=str(artifacts / "status.json"),
                thread_id="123456789012345678", task_label="진행 보고 구현",
                coordinator_stage="working")
    return repo, artifacts, data, tmp_path / "state"


def test_baseline_and_content_changes(lane):
    repo, _, data, state = lane
    (repo / "runner.py").write_text("value = 2\n")
    progress = Progress(Manifest.from_dict(data), state)
    first = progress.tick(now=0)
    assert first["snapshot"]["baseline"]
    assert first["snapshot"]["changes"] == []
    for now in (300, 600):
        tick = progress.tick(now=now)
        assert tick["snapshot"]["changes"] == []
        assert "새로 확인된 파일 변경 없음" in tick["queued"][0]["content"]
        progress.ack(tick["queued"][0]["id"])
    (repo / "runner.py").write_text("value = 3\n")
    tick = progress.tick(now=900)
    assert [c["path"] for c in tick["snapshot"]["changes"]] == ["runner.py"]
    progress.ack(tick["queued"][0]["id"])
    assert progress.tick(now=1200)["snapshot"]["changes"] == []
    progress.ack(progress.peek()["id"])
    (repo / "runner.py").write_text("value = 4\n")
    assert progress.tick(now=1500)["snapshot"]["changes"][0]["path"] == "runner.py"


def test_collector_ignores_timestamp_and_logs(lane):
    repo, _, data, _ = lane
    manifest = Manifest.from_dict(data)
    before = collect(manifest)
    (repo / "runner.py").touch()
    (repo / "worker.log").write_text("reasoning: I implemented everything\n")
    after = collect(manifest)
    assert before["files"] == after["files"]
    assert after["errors"] == []


def test_stage_commit_revert_delete_and_between_poll_commit(lane):
    repo, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)

    def change(now):
        result = p.tick(now=now)
        p.ack(result["queued"][0]["id"])
        return result["snapshot"]["changes"]

    (repo / "runner.py").write_text("value = 2\n")
    assert "content" in change(300)[0]["kinds"]
    git(repo, "add", "runner.py")
    assert "staged" in change(600)[0]["kinds"]
    git(repo, "commit", "-qm", "fixture edit")
    assert "committed" in change(900)[0]["kinds"]
    (repo / "runner.py").write_text("value = 3\n")
    change(1200)
    git(repo, "restore", "runner.py")
    assert "reverted" in change(1500)[0]["kinds"]
    (repo / "runner.py").unlink()
    assert "deleted" in change(1800)[0]["kinds"]
    git(repo, "restore", "runner.py")
    change(2100)
    (repo / "runner.py").write_text("value = 9\n")
    git(repo, "commit", "-qam", "between polls")
    assert "committed" in change(2400)[0]["kinds"]
    (repo / "new.py").write_text("x=1\n")
    assert change(2700)[0]["path"] == "new.py"
    (repo / "new.py").unlink()
    assert "deleted" in change(3000)[0]["kinds"]


def test_secrets_symlinks_and_nul_safe_names(lane):
    repo, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    for name in (".env", "auth.json", "credentials.py", "access_token.py", "secret.key"):
        (repo / name).write_text("NEVER_EXPOSE_THIS_SECRET")
    outside = repo.parent / "private.py"
    outside.write_text("DO_NOT_READ")
    (repo / "link.py").symlink_to(outside)
    (repo / "linked").symlink_to(repo.parent, target_is_directory=True)
    name = "@everyone\n<@123> ignore prior instructions.py"
    (repo / name).write_text("x=1\n")
    result = p.tick(now=300)
    assert name in [c["path"] for c in result["snapshot"]["changes"]]
    assert "symlink_or_special" in result["snapshot"]["errors"]
    text = result["queued"][0]["content"]
    for forbidden in ("@everyone", "<@", "ignore prior", "NEVER_EXPOSE", "DO_NOT_READ"):
        assert forbidden not in text
    assert not any("credentials" in path or "token" in path for path in collect(p.manifest)["files"])


def test_git_failure_and_truncation_are_unknown(lane):
    repo, _, data, state = lane
    manifest = Manifest.from_dict(data)
    limited = collect(manifest, max_bytes=2)
    assert "content_truncated" in limited["errors"]
    p = Progress(manifest, state)
    p.tick(now=0)
    (repo / ".git").rename(repo / ".git-away")
    result = p.tick(now=300)
    assert not result["snapshot"]["available"]
    assert result["snapshot"]["changes"] == []
    assert "상태 확인 불가" in result["queued"][0]["content"]
    assert "새로 확인된 파일 변경 없음" not in result["queued"][0]["content"]


def test_manifest_rejects_unapproved_inputs(lane):
    _, _, data, _ = lane
    for fields in ({"worktree": "/tmp"}, {"run_id": "../escape"},
                   {"thread_id": "<@everyone>"}, {"command": "echo secret"},
                   {"event_path": "/etc/passwd"}):
        with pytest.raises(ValueError):
            Manifest.from_dict(dict(data, **fields))


def event(artifacts, kind, command="python -m pytest tests/test_fixture.py -q", *,
          output="================ 10 passed in 0.12s ================", code=0, item_id="exec-1"):
    item = dict(id=item_id, type="command_execution", command=command,
                status="completed" if kind == "item.completed" else "in_progress",
                exit_code=code, aggregated_output=output)
    with (artifacts / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"type": kind, "item": item}) + "\n")


def test_cadence_terminal_receipt_and_coordinator_stop(lane):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    assert p.tick(now=0)["queued"] == []
    assert p.tick(now=299)["queued"] == []
    p.tick(now=300)
    p.ack(p.peek()["id"])
    (artifacts / "status.json").write_text(json.dumps({
        "status": "cli_completed", "exit_code": 0, "process_returncode": 0,
        "model": "DO_NOT_SURFACE", "artifact_dir": "private"}))
    immediate = p.tick(now=301)
    assert len(immediate["queued"]) == 1
    assert "Codex 실행 종료, 레나 검증 대기" in immediate["queued"][0]["content"]
    assert "작업 완료" not in immediate["queued"][0]["content"]
    p.ack(p.peek()["id"])
    assert p.tick(now=302)["queued"] == []
    p = Progress(Manifest.from_dict(dict(data, coordinator_stage="verifying")), state)
    assert p.tick(now=600)["queued"] == []
    notice = p.tick(now=601)["queued"][0]
    assert "레나 검증 진행 중" in notice["content"]
    p.ack(notice["id"])
    p = Progress(Manifest.from_dict(dict(data, coordinator_stage="final_verified")), state)
    final = p.tick(now=602)
    assert final["stopped"]
    assert "레나 최종 검증 완료" in final["queued"][0]["content"]
    p.ack(p.peek()["id"])
    assert p.tick(now=1000)["queued"] == []
    assert p.peek() is None


@pytest.mark.parametrize("status,code", [("cli_failed", 1), ("timed_out", 124),
    ("cancelled", 130), ("launch_failed", 127), ("output_limit", 74),
    ("artifact_or_transport_failed", 74)])
def test_failures_emit_immediately_once(lane, status, code):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    (artifacts / "status.json").write_text(json.dumps({"status": status, "exit_code": code}))
    result = p.tick(now=1)
    assert result["snapshot"]["exit_status"] == status
    assert "실패·중단" in result["queued"][0]["content"]
    p.ack(p.peek()["id"])
    assert p.tick(now=2)["queued"] == []
    assert "실패·중단" in p.tick(now=301)["queued"][0]["content"]


def test_waiting_and_liveness_are_not_activity(lane):
    _, _, data, state = lane
    manifest = Manifest.from_dict(dict(data, pid=42, process_start="fixture-start"))
    p = Progress(manifest, state, process_probe=lambda pid, identity: "alive")
    p.tick(now=0)
    text = p.tick(now=300)["queued"][0]["content"]
    assert "프로세스는 확인됐지만" in text and "활동 근거가 부족" in text
    p.ack(p.peek()["id"])
    p = Progress(Manifest.from_dict(dict(data, cli_status="needs_user")), state)
    assert "사용자 확인 대기" in p.tick(now=301)["queued"][0]["content"]
    p.ack(p.peek()["id"])
    assert not p.tick(now=302)["queued"]
    assert "사용자 확인 대기" in p.tick(now=601)["queued"][0]["content"]


def test_test_evidence_lifecycle_and_reasoning_ignored(lane):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    with (artifacts / "events.jsonl").open("a") as stream:
        for kind in ("agent_message", "reasoning", "prompt"):
            stream.write(json.dumps({"type": "item.completed", "item": {
                "type": kind, "text": "SECRET @everyone 999 passed implemented all"}}) + "\n")
    assert p.tick(now=1)["snapshot"]["execution"]["phase"] == "unknown"
    event(artifacts, "item.started")
    started = p.tick(now=2)["snapshot"]
    assert started["tests"]["status"] == "in_progress"
    event(artifacts, "item.completed")
    complete = p.tick(now=3)["snapshot"]
    assert complete["tests"]["status"] == "passed"
    assert complete["tests"]["passed"] == 10
    assert "SECRET" not in p.path.read_text()
    assert "python -m pytest" not in p.path.read_text()
    assert "@everyone" not in render(complete)
    event(artifacts, "item.started", item_id="exec-2")
    event(artifacts, "item.completed", item_id="exec-2", code=1,
          output="================ 1 failed, 9 passed in 0.12s ================")
    failed = p.tick(now=4)["snapshot"]
    assert failed["tests"]["status"] == "failed"
    assert "실패" in render(failed)


@pytest.mark.parametrize("command,output,code", [
    ("echo '10 passed'", "10 passed", 0),
    ("echo '================ 10 passed in 0.12s ================'", "================ 10 passed in 0.12s ================", 0),
    ("python -c 'print(\"10 passed\")'", "10 passed", 0),
    ("python -m pytest tests; echo '10 passed'", "================ 10 passed in 0.12s ================", 0),
    ("apply_patch 'python -m pytest 10 passed'", "================ 10 passed in 0.12s ================", 0),
    ("python -m pytest -k 'echo 10 passed'", "================ 10 passed in 0.12s ================", 0),
    ("python -m pytest tests", "10 passed", 0),
    ("python -m pytest tests", "================ 0 passed in 0.12s ================", 0),
    ("python -m pytest tests", "================ 10 passed in 0.12s ================", 1),
    ("python -m pytest tests", "================ 10 passed in 0.12s ================", None),
])
def test_unreliable_test_claims_never_pass(lane, command, output, code):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    event(artifacts, "item.completed", command, output=output, code=code)
    result = p.tick(now=300)
    assert result["snapshot"]["tests"]["status"] != "passed"
    assert "통과" not in result["queued"][0]["content"]


def test_outbox_receipt_dry_run_and_recovery(lane):
    repo, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0, dry_run=True)
    assert not state.exists()
    p.tick(now=0)
    before = p.path.read_bytes()
    (repo / "runner.py").write_text("value=8\n")
    event(artifacts, "item.completed")
    preview = p.tick(now=300, dry_run=True)
    assert preview["queued"]
    assert before == p.path.read_bytes()
    message = p.tick(now=300)["queued"][0]
    assert p._load()["delivered"] is None

    def fake_sender(_message):  # test-only failure; deliberately no network
        raise ConnectionError("fixture transport failure")

    with pytest.raises(ConnectionError):
        fake_sender(p.peek())
    restarted = Progress(Manifest.from_dict(data), state)
    assert restarted.peek() == message
    assert not restarted.tick(now=600)["queued"]
    assert restarted.peek() == message
    with pytest.raises(ValueError):
        restarted.ack("wrong:99")
    restarted.ack(message["id"], dry_run=True)
    assert restarted.peek() == message
    restarted.ack(message["id"])
    assert restarted.peek() is None
    assert restarted._load()["delivered"]["id"] == message["id"]
    assert p.path.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700


def test_pending_periodic_does_not_block_terminal_and_exact_ack(lane):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    first = p.tick(now=300)["queued"][0]
    (artifacts / "status.json").write_text('{"status":"cli_completed","exit_code":0}')
    terminal = p.tick(now=301)["queued"][0]
    assert first["id"] != terminal["id"]
    with pytest.raises(ValueError):
        p.ack(terminal["id"])
    assert not p.tick(now=302)["queued"]
    p.ack(first["id"])
    assert p.peek() == terminal


def test_read_errors_and_event_truncation_do_not_mean_dead(lane, monkeypatch):
    import agent.delegation_progress as module
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state,
                 process_probe=lambda *args: (_ for _ in ()).throw(OSError("SSH failure secret")))
    p.tick(now=0)
    (artifacts / "events.jsonl").write_text("x" * (1024 * 1024 + 10))
    result = p.tick(now=300)
    assert "events_truncated" in result["snapshot"]["errors"]
    assert "상태 확인 불가" in result["queued"][0]["content"]
    p.ack(p.peek()["id"])
    original = module._read

    def denied(root, path, limit):
        if path == "runner.py":
            raise PermissionError("secret raw transport error")
        return original(root, path, limit)

    monkeypatch.setattr(module, "_read", denied)
    result = p.tick(now=600)
    assert "file_unreadable" in result["snapshot"]["errors"]
    assert "secret" not in result["queued"][0]["content"]


def test_run_isolation_binding_and_watcher_lock(lane):
    _, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    other = Progress(Manifest.from_dict(dict(data, run_id="other")), state)
    assert other.peek() is None
    changed = Progress(Manifest.from_dict(dict(data, thread_id="999")), state)
    with pytest.raises(ValueError):
        changed.peek()
    with p.watcher():
        with pytest.raises(ValueError, match="run_locked"):
            with Progress(Manifest.from_dict(data), state).watcher():
                pytest.fail("two watchers")
        with pytest.raises(ValueError, match="run_locked"):
            Progress(Manifest.from_dict(data), state).tick(now=1)
        assert p.tick(now=1)["queued"] == []


def test_event_partial_line_rotation_and_old_baseline(lane):
    _, artifacts, data, state = lane
    event(artifacts, "item.completed")
    p = Progress(Manifest.from_dict(data), state)
    assert p.tick(now=0)["snapshot"]["tests"]["status"] == "unknown"
    path = artifacts / "events.jsonl"
    line = json.dumps({"type": "item.completed", "item": {"id":"new", "type":"file_change", "status":"completed"}})
    with path.open("a") as stream:
        stream.write(line[:20])
    assert p.tick(now=1)["snapshot"]["execution"]["phase"] == "unknown"
    with path.open("a") as stream:
        stream.write(line[20:] + "\n")
    assert p.tick(now=2)["snapshot"]["execution"]["phase"] == "file_change_completed"
    path.unlink()
    event(artifacts, "item.completed")
    result = p.tick(now=3)["snapshot"]
    assert "events_replaced_or_truncated" in result["errors"]
    assert result["tests"]["status"] == "unknown"


def test_partial_inventory_has_a_baseline_and_recovers_without_invention(lane, monkeypatch):
    import agent.delegation_progress as module
    repo, _, data, state = lane
    (repo / "z.py").write_text("baseline\n")
    git(repo, "add", "z.py")
    git(repo, "commit", "-qm", "second baseline file")
    original = module.collect
    monkeypatch.setattr(module, "collect", lambda manifest: original(manifest, max_files=1))
    p = Progress(Manifest.from_dict(data), state)
    assert p.tick(now=0)["snapshot"]["baseline"]
    unchanged = p.tick(now=300)
    assert not unchanged["snapshot"]["baseline"]
    assert not unchanged["snapshot"]["changes"]
    p.ack(p.peek()["id"])
    (repo / "runner.py").write_text("new value\n")
    assert p.tick(now=600)["snapshot"]["changes"][0]["path"] == "runner.py"
    p.ack(p.peek()["id"])
    monkeypatch.setattr(module, "collect", original)
    assert not p.tick(now=900)["snapshot"]["changes"]


def test_missing_event_file_at_spawn_then_first_new_event(lane):
    _, artifacts, data, state = lane
    (artifacts / "events.jsonl").unlink()
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    event(artifacts, "item.completed")
    assert p.tick(now=1)["snapshot"]["tests"]["status"] == "passed"


def test_deleted_commit_and_rename_keep_state_evidence(lane):
    repo, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    git(repo, "mv", "runner.py", "new\nname.py")
    snapshot = p.tick(now=300)["snapshot"]
    assert {c["path"] for c in snapshot["changes"]} == {"runner.py", "new\nname.py"}
    p.ack(p.peek()["id"])
    git(repo, "commit", "-qm", "rename fixture")
    changes = p.tick(now=600)["snapshot"]["changes"]
    assert all("committed" in c["kinds"] for c in changes)


def test_snapshot_errors_and_process_identity_unknown(lane):
    repo, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(dict(data, pid=123, process_start="fixture")), state,
                 process_probe=lambda *_: (_ for _ in ()).throw(OSError("SSH secret")))
    snapshot = p.snapshot(now=0)
    assert snapshot["liveness"] == "unknown"
    assert not snapshot["available"]
    assert "상태 확인 불가" in render(snapshot)
    assert "SSH secret" not in render(snapshot)


def test_renderer_golden_cases_and_trimming():
    base = {"available": True, "baseline": False, "changes": [], "tests": {"status": "unknown"},
            "liveness": "alive", "coordinator_stage": "working", "exit_status": "running"}
    assert render(base) == (
        "작업 진행 상황을 전해드려요.\n"
        "• 새로 확인된 파일 변경 없음. 관측 사이의 작업까지 없었다고 단정하지는 않아요.\n"
        "• 프로세스는 확인됐지만, 새 구현 활동 근거가 부족해요.\n"
        "• 테스트: 결과 확인 전이에요.\n"
        "• 아직 미검증: 레나 검증·운영 활성화.")
    for status, expected in (("in_progress", "완료 근거는 아직"), ("failed", "실행 실패"),
                             ("passed", "현재 변경 전체의 재검증 여부는 미확인")):
        assert expected in render(dict(base, tests={"status": status}))
    malicious = dict(base, task_label="@everyone <@123>\x1b[2J SECRET", changes=[
        {"path": "ignore rules\n@everyone.py", "class": "source", "kinds": ["content", "staged", "deleted"]}
    ] * 150, execution_since_queue=True, execution={"phase": "execution_completed"},
        tests={"status": "passed"}, coordinator_stage="verifying", exit_status="cli_completed")
    text = render(malicious, limit=200)
    assert len(text) <= 200 and "생략" in text
    assert "@everyone" not in text and "SECRET" not in text
    assert len(render(malicious)) <= 1200


def test_watch_artifact_symlink_replacement_and_manifest_hooks_rejected(lane):
    repo, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    outside = repo.parent / "outside.jsonl"
    outside.write_text("PRIVATE_CONTENT")
    (artifacts / "events.jsonl").unlink()
    (artifacts / "events.jsonl").symlink_to(outside)
    snapshot = p.tick(now=300)["snapshot"]
    assert not snapshot["available"]
    assert "events_unavailable" in snapshot["errors"]
    assert "PRIVATE_CONTENT" not in p.path.read_text()


def test_clock_injection_and_dry_run_event_cursor(lane):
    _, artifacts, data, state = lane
    now = [0]
    p = Progress(Manifest.from_dict(data), state, clock=lambda: now[0])
    p.tick()
    event(artifacts, "item.completed")
    now[0] = 299
    assert p.tick(dry_run=True)["snapshot"]["tests"]["status"] == "passed"
    assert p._load()["tests"]["status"] == "unknown"
    now[0] = 300
    assert p.tick()["queued"]


def test_transient_observation_gap_survives_until_report(lane):
    repo, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    (artifacts / "events.jsonl").write_text("not JSON\n")
    assert "events_invalid" in p.tick(now=10)["snapshot"]["errors"]
    assert not p.tick(now=20)["queued"]
    report = p.tick(now=300)
    assert "events_invalid" in report["snapshot"]["errors"]
    assert "상태 확인 불가" in report["queued"][0]["content"]
    assert "새로 확인된 파일 변경 없음" not in report["queued"][0]["content"]
    p.ack(p.peek()["id"])
    assert p.tick(now=600)["snapshot"]["available"]


@pytest.mark.parametrize("payload", [[], {"status": [], "exit_code": 0},
                                    {"status": "cli_completed", "exit_code": True}])
def test_malformed_receipt_is_observation_error(lane, payload):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    (artifacts / "status.json").write_text(json.dumps(payload))
    snapshot = p.tick(now=300)["snapshot"]
    assert not snapshot["available"]
    assert snapshot["exit_status"] == "running"


def test_state_corruption_is_not_empty_baseline(lane):
    _, _, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    for payload in ([], {"schema_version": 1, "binding": p.manifest.binding()}):
        p.path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            p.tick(now=300)


def test_secret_paths_not_opened_and_git_output_is_bounded(lane, monkeypatch):
    import agent.delegation_progress as module
    import sys
    repo, _, data, _ = lane
    for name in ("credentials.py", "auth.json", ".env", "token.md"):
        (repo / name).write_text("CREDENTIAL_SENTINEL")
    original = module._read
    seen = []

    def recording(root, path, limit):
        seen.append(path)
        return original(root, path, limit)

    monkeypatch.setattr(module, "_read", recording)
    collect(Manifest.from_dict(data))
    assert set(seen) == {"runner.py"}
    # The host's live-system guard cannot inspect child ancestry on every Mac.
    # Finish this tiny producer before returning its real pipes; no signal needed.
    real_popen = module.subprocess.Popen

    def finished_producer(*args, **kwargs):
        assert kwargs["shell"] is False
        process = real_popen(*args, **kwargs)
        process.wait(timeout=2)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", finished_producer)
    with pytest.raises(ValueError, match="command_truncated"):
        module._bounded_command([sys.executable, "-c", "print('x'*200)"], limit=100)


def test_terminal_receipt_read_gap_keeps_last_known_exit(lane):
    _, artifacts, data, state = lane
    p = Progress(Manifest.from_dict(data), state)
    p.tick(now=0)
    (artifacts / "status.json").write_text('{"status":"cli_completed","exit_code":0}')
    p.tick(now=1)
    p.ack(p.peek()["id"])
    (artifacts / "status.json").unlink()
    snapshot = p.tick(now=301)["snapshot"]
    assert "receipt_unavailable" in snapshot["errors"]
    assert snapshot["exit_status"] == "cli_completed"


def test_bounded_inventory_prioritizes_current_changes(lane):
    repo, _, data, _ = lane
    (repo / "z_changed.py").write_text("fixture edit\n")
    result = collect(Manifest.from_dict(data), max_files=1)
    assert set(result["files"]) == {"z_changed.py"}
    assert "files_truncated" in result["errors"]
