"""Real CLI/watcher at its default 300-second cadence; no live services."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest


CLI = Path(__file__).resolve().parents[2] / "scripts/delegation_progress.py"


@pytest.fixture
def lane(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(PATH=os.environ["PATH"], HOME=str(home), HERMES_HOME=str(home / ".hermes"),
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", LC_ALL="C")
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in [("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid"),
                 ("config", "core.hooksPath", "/dev/null"), ("config", "commit.gpgsign", "false")]:
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
    (repo / "test_fixture.py").write_text(
        "def test_good():\n    assert True\ndef test_bad():\n    assert False\n")
    for args in [("add", "."), ("commit", "-qm", "fixture")]:
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
    art = tmp_path / "artifacts"
    art.mkdir()
    events = art / "events.jsonl"
    events.touch()
    data = dict(run_id="cli-regression", worktree=str(repo), approved_root=str(tmp_path),
        artifact_root=str(art), event_path=str(events), receipt_path=str(art / "status.json"),
        thread_id="123456789012345678", task_label="fixture")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data))
    return dict(root=tmp_path, repo=repo, art=art, data=data, manifest=manifest, env=env,
                state=tmp_path / "state", state_file=tmp_path / "state/cli-regression/state.json")


def argv(lane, command, *args):
    return [sys.executable, "-B", str(CLI), command, "--manifest", str(lane["manifest"]),
            "--state-dir", str(lane["state"]), *args]


def cli(lane, command, *args, check=True):
    result = subprocess.run(argv(lane, command, *args), env=lane["env"],
                            capture_output=True, text=True, timeout=15)
    if not check:
        return result
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def update(lane, **fields):
    lane["data"].update(fields)
    temporary = lane["manifest"].with_suffix(".tmp")
    temporary.write_text(json.dumps(lane["data"]))
    temporary.replace(lane["manifest"])
    return time.time()


def state(lane):
    try:
        return json.loads(lane["state_file"].read_text())
    except FileNotFoundError:
        return {}


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail("real watcher did not observe transition within 10 seconds")


def append_event(lane, item_id, command, result=None):
    item = dict(id=item_id, type="command_execution", command=command,
                status="completed" if result is not None else "in_progress")
    if result is not None:
        item.update(exit_code=result.returncode, aggregated_output=result.stdout + result.stderr)
    with (lane["art"] / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(type="item.completed" if result is not None else "item.started",
                                    item=item)) + "\n")


def start_watcher(lane, stream, restartable=False):
    command = argv(lane, "watch", "--poll-interval", ".05")
    if restartable:
        # Test-only cooperative interrupt: process ancestry lookup is unavailable
        # in the macOS sandbox. Keep the live-system signal guard intact and let
        # only this stdin-owned child interrupt its own real CLI main thread.
        driver = ("import _thread,runpy,sys,threading; "
                  "threading.Thread(target=lambda: (sys.stdin.readline(), _thread.interrupt_main()), "
                  "daemon=True).start(); sys.argv=sys.argv[1:]; "
                  "runpy.run_path(sys.argv[0],run_name='__main__')")
        command = [sys.executable, "-B", "-c", driver, *command[2:]]
    return subprocess.Popen(command, env=lane["env"], stdout=stream,
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize("ack_first", [True, False])
def test_f2_real_tick_wait_reentry_restart_fifo(lane, ack_first):
    cli(lane, "tick")
    update(lane, cli_status="needs_user")
    first = cli(lane, "tick")["queued"][0]
    assert not cli(lane, "tick")["queued"]
    if ack_first:
        cli(lane, "ack", "--id", first["id"])
    update(lane, cli_status="running")
    assert not cli(lane, "tick")["queued"]
    # Every CLI call is a new process: the observed resume must be durable.
    update(lane, cli_status="needs_user")
    result = cli(lane, "tick")
    assert [m["event"] for m in result["queued"]] == ["needs_user"], result
    second = result["queued"][0]
    assert second["sequence"] == first["sequence"] + 1
    assert second["observed_at"] - first["observed_at"] < 300
    if not ack_first:
        assert cli(lane, "ack", "--id", second["id"], check=False).returncode == 74
        assert cli(lane, "peek") == first
        cli(lane, "ack", "--id", first["id"])
    assert cli(lane, "peek") == second
    assert not cli(lane, "tick")["queued"]
    cli(lane, "ack", "--id", second["id"])
    assert cli(lane, "peek") is None
    assert cli(lane, "ack", "--id", second["id"], check=False).returncode == 74


@pytest.mark.parametrize("ack_first", [True, False])
@pytest.mark.parametrize("restart", [True, False])
def test_f2_actual_watcher_repeat_wait_unknown_gap_and_final_pending(lane, ack_first, restart):
    output = lane["root"] / "watch.jsonl"
    with output.open("w") as stream:
        process = start_watcher(lane, stream, restartable=restart)
        try:
            wait_for(lambda: state(lane).get("previous"))
            update(lane, cli_status="needs_user")
            wait_for(lambda: state(lane).get("sequence") == 1)
            first = cli(lane, "peek")
            if ack_first:
                cli(lane, "ack", "--id", first["id"])
            # Actual unreadable observation, then same wait: no invented resume.
            lane["manifest"].write_text("{")
            wait_for(lambda: "manifest_unavailable" in state(lane).get("observation_errors", []))
            stamp = update(lane, cli_status="needs_user")
            wait_for(lambda: state(lane).get("last_complete_observation_at", 0) > stamp)
            assert state(lane)["sequence"] == 1
            stamp = update(lane, cli_status="running")
            wait_for(lambda: state(lane).get("last_complete_observation_at", 0) > stamp)
            if restart:
                _, stderr = process.communicate(input="interrupt\n", timeout=10)
                assert process.returncode == 130 and not stderr
                process = start_watcher(lane, stream)
            update(lane, cli_status="needs_user")
            wait_for(lambda: state(lane).get("sequence") == 2)
            pending = state(lane)["pending"]
            assert [m["event"] for m in pending] == ["needs_user"] * (1 if ack_first else 2)
            if not ack_first:
                cli(lane, "ack", "--id", first["id"])
            second = cli(lane, "peek")
            assert second["sequence"] == 2
            cli(lane, "ack", "--id", second["id"])
            actual = subprocess.run([sys.executable, "-c", "pass"], env=lane["env"], timeout=5)
            (lane["art"] / "status.json").write_text(json.dumps(
                dict(status="cli_completed", exit_code=actual.returncode)))
            wait_for(lambda: state(lane).get("sequence") == 3)
            update(lane, coordinator_stage="final_verified")
            process.wait(timeout=10)
            assert process.returncode == 0
            assert [m["event"] for m in state(lane)["pending"]] == ["cli_completed", "final_verified"]
            for message in list(state(lane)["pending"]):
                cli(lane, "ack", "--id", message["id"])
            assert cli(lane, "peek") is None
        finally:
            if process.poll() is None:
                update(lane, coordinator_stage="stopped")
            _, stderr = process.communicate(timeout=10)
            assert not stderr
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["queued"][0]["event"] for row in rows] == [
        "needs_user", "needs_user", "cli_completed", "final_verified"]


def test_f1_real_pytest_jsonl_persistence_and_watcher_render(lane):
    cli(lane, "tick")
    good = [sys.executable, "-m", "pytest", "test_fixture.py::test_good", "-q"]
    success = subprocess.run(good, cwd=lane["repo"], env=lane["env"], capture_output=True, text=True)
    assert success.returncode == 0 and "1 passed" in success.stdout
    append_event(lane, "good", shlex.join(good), success)
    assert cli(lane, "tick")["snapshot"]["tests"]["status"] == "passed"
    bad = [sys.executable, "-m", "pytest", "test_fixture.py", "-k", "test_bad", "-q"]
    append_event(lane, "bad", shlex.join(bad))
    assert cli(lane, "tick")["snapshot"]["tests"]["status"] != "passed"
    failure = subprocess.run(bad, cwd=lane["repo"], env=lane["env"], capture_output=True, text=True)
    assert failure.returncode == 1 and "1 failed, 1 deselected" in failure.stdout
    append_event(lane, "bad", shlex.join(bad), failure)
    # A new watcher consumes the completed event, persists and renders the final outbox.
    update(lane, coordinator_stage="final_verified")
    result = cli(lane, "watch", "--poll-interval", ".05")
    assert result["snapshot"]["tests"]["status"] == "unknown"
    assert state(lane)["tests"]["status"] == "unknown"
    assert "pytest 실행 통과" not in result["queued"][0]["content"]
    assert cli(lane, "peek") == result["queued"][0]
