"""Approved progress fixes: real, isolated pytest/git producers, JSONL consumer."""
import json
from dataclasses import replace
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from agent.delegation_progress import Manifest, Progress, collect, render


@pytest.fixture
def lane(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("HERMES_PYTHON", sys.executable)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.hooksPath", "/dev/null")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "test_fixture.py").write_text(
        "import pytest\ndef test_good():\n    assert True\n"
        "def test_bad():\n    assert False\n"
        "@pytest.mark.skip(reason='fixture')\ndef test_skip():\n    pass\n")
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "events.jsonl").touch()
    manifest = Manifest.from_dict(dict(run_id="regression", worktree=str(repo),
        approved_root=str(tmp_path), artifact_root=str(art),
        event_path=str(art / "events.jsonl"), thread_id="123456789012345678",
        task_label="fixture"))
    return repo, art, Progress(manifest, tmp_path / "state")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout


def run(repo, command):
    return subprocess.run(["/bin/sh", "-c", command], cwd=repo,
                          capture_output=True, text=True, timeout=30)


def event(art, item_id, command, result=None):
    item = dict(id=item_id, type="command_execution", command=command,
                status="completed" if result is not None else "in_progress")
    if result is not None:
        item.update(exit_code=result.returncode, aggregated_output=result.stdout + result.stderr)
    with (art / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(type="item.completed" if result is not None else "item.started",
                                    item=item)) + "\n")


def passed(lane):
    repo, art, progress = lane
    progress.tick()
    command = f"{sys.executable} -m pytest test_fixture.py::test_good -q"
    result = run(repo, command)
    assert result.returncode == 0 and "1 passed" in result.stdout
    event(art, "good", command, result)
    assert progress.tick()["snapshot"]["tests"]["status"] == "passed"
    return command, result


@pytest.mark.parametrize("suffix", [
    "test_fixture.py -k test_bad -q",
    "test_fixture.py -m missing -q",
    "test_fixture.py --maxfail=1 -q",
    "test_fixture.py --definitely-invalid-option",
    "test_fixture.py -k",  # actual pytest usage error
    "test_fixture.py -k 'unterminated",  # actual shell syntax error
    "test_fixture.py -k 'test_good or test_skip' -q",  # unsupported success
])
def test_f1_newer_unsupported_invocation_invalidates_pass(lane, suffix):
    repo, art, progress = lane
    passed(lane)
    command = f"{sys.executable} -m pytest {suffix}"
    event(art, "new", command)
    started = progress.tick()["snapshot"]
    assert started["tests"]["status"] != "passed", started["tests"]
    result = run(repo, command)
    event(art, "new", command, result)
    restarted = Progress(progress.manifest, progress.root)
    snapshot = restarted.tick()["snapshot"]
    assert snapshot["tests"]["status"] == "unknown", (result, snapshot["tests"])
    assert "통과" not in render(snapshot)
    assert "exit_code" not in snapshot["tests"]


@pytest.mark.parametrize("template", [
    "/bin/sh -c {quoted}",
    "env PYTHONUNBUFFERED=1 {command}",
    "PYTHONUNBUFFERED=1 {command}",
    "{python} -B -m pytest test_fixture.py -k test_bad -q",
    "{python} -W ignore -X dev -m pytest test_fixture.py -k test_bad -q",
    "{python} -Wignore -Xdev -m pytest test_fixture.py -k test_bad -q",
    "cd . && {command}",
    "{command}; printf '1 passed in 0.01s\\n'",
    "bash scripts/run_tests.sh -j 2 tests/test_fixture.py -k test_bad",
])
def test_f1_wrappers_and_compound_commands_cannot_keep_old_pass(lane, template):
    repo, art, progress = lane
    passed(lane)
    if "scripts/run_tests.sh" in template:
        install_runner(repo)
    inner = f"{sys.executable} -m pytest test_fixture.py -k test_bad -q"
    command = template.format(command=inner, quoted=shlex.quote(inner), python=sys.executable)
    event(art, "wrapped", command)
    assert progress.tick()["snapshot"]["tests"]["status"] != "passed"
    result = run(repo, command)
    event(art, "wrapped", command, result)
    assert progress.tick()["snapshot"]["tests"]["status"] == "unknown"


@pytest.mark.parametrize("template", [
    "echo {quoted}", "printf '%s\\n' {quoted}",
    "{python} -c {source}", "/bin/sh -c {echo}",
])
def test_f1_text_is_not_test_execution(lane, template):
    repo, art, progress = lane
    command, result = passed(lane)
    before = progress.tick()["snapshot"]["tests"]
    text = "python -m pytest test_fixture.py; 1 passed in 0.01s"
    spoof = template.format(quoted=shlex.quote(text), python=sys.executable,
        source=shlex.quote(f"print({text!r})"), echo=shlex.quote(f"echo {shlex.quote(text)}"))
    actual = run(repo, spoof)
    assert actual.returncode == 0 and "pytest" in actual.stdout
    event(art, "text", spoof)
    event(art, "text", spoof, actual)
    assert progress.tick()["snapshot"]["tests"] == before


def test_f1_pairing_old_completion_and_duplicates_across_restart(lane):
    repo, art, progress = lane
    good, success = passed(lane)
    event(art, "slow", good)
    progress.tick()
    bad = f"{sys.executable} -m pytest test_fixture.py -k test_bad -q"
    event(art, "new", bad)
    progress.tick()
    event(art, "slow", good, success)  # older start completes after newer start
    progress = Progress(progress.manifest, progress.root)
    assert progress.tick()["snapshot"]["tests"]["status"] != "passed"
    failure = run(repo, bad)
    assert failure.returncode == 1 and "1 failed" in failure.stdout
    event(art, "new", bad, failure)
    assert progress.tick()["snapshot"]["tests"]["status"] == "unknown"
    event(art, "good", good, success)  # duplicate old completed-only item
    event(art, "good", good)  # delayed start must not reopen completed item
    assert progress.tick()["snapshot"]["tests"]["status"] == "unknown"
    event(art, "mismatch", bad)
    progress.tick()
    event(art, "mismatch", good, success)
    assert progress.tick()["snapshot"]["tests"]["status"] == "unknown"
    direct = f"{sys.executable} -m pytest test_fixture.py::test_bad -q"
    event(art, "control", direct, run(repo, direct))
    assert progress.tick()["snapshot"]["tests"]["status"] == "failed"
    event(art, "new-good", good, success)
    assert progress.tick()["snapshot"]["tests"]["status"] == "passed"
    (repo / "new_edit.py").write_text("value=2\n")
    snapshot = progress.tick()["snapshot"]
    assert "현재 변경 전체의 재검증 여부는 미확인" in render(snapshot)


def install_runner(repo):
    source = Path(__file__).resolve().parents[2]
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    for name in ("run_tests.sh", "run_tests_parallel.py"):
        shutil.copyfile(source / "scripts" / name, repo / "scripts" / name)
    (repo / "tests/test_fixture.py").write_text(
        "import pytest\ndef test_good():\n    pass\n"
        "@pytest.mark.skip(reason='fixture')\ndef test_skip():\n    pass\n")


def test_f1_real_canonical_runner_and_skip_counts(lane, monkeypatch):
    repo, art, progress = lane
    install_runner(repo)
    monkeypatch.setenv("HERMES_PYTHON", sys.executable)
    progress.tick()
    command = "bash scripts/run_tests.sh -j 2 tests/test_fixture.py"
    result = run(repo, command)
    assert result.returncode == 0, result
    assert "100% complete" in result.stdout and "1 skipped" in result.stdout
    event(art, "canonical", command)
    progress.tick()
    event(art, "canonical", command, result)
    tests = progress.tick()["snapshot"]["tests"]
    assert tests["status"] == "passed" and tests["scope"] == "canonical_command"
    assert tests["passed"] == 1 and tests["skipped"] == 1


def test_f2_incomplete_running_observation_does_not_rearm_wait(lane):
    _, art, progress = lane
    progress.tick()
    progress.manifest = replace(progress.manifest, cli_status="needs_user")
    first = progress.tick()["queued"][0]
    progress.ack(first["id"])
    progress.manifest = replace(progress.manifest, cli_status="running")
    with (art / "events.jsonl").open("a") as stream:
        stream.write("{broken-json}\n")
    assert "events_invalid" in progress.tick()["snapshot"]["current_errors"]
    progress.manifest = replace(progress.manifest, cli_status="needs_user")
    assert not progress.tick()["queued"]
    progress.manifest = replace(progress.manifest, cli_status="running")
    assert progress.tick()["snapshot"]["current_errors"] == []
    progress = Progress(replace(progress.manifest, cli_status="needs_user"), progress.root)
    assert progress.tick()["queued"][0]["sequence"] == first["sequence"] + 1


def test_f2_legacy_wait_state_requires_observed_resume(lane):
    _, _, progress = lane
    progress.manifest = replace(progress.manifest, cli_status="needs_user")
    first = progress.tick()["queued"][0]
    progress.ack(first["id"])
    legacy = json.loads(progress.path.read_text())
    legacy.pop("waiting", None)
    legacy["terminal_seen"].append("needs_user")
    progress.path.write_text(json.dumps(legacy))
    assert not Progress(progress.manifest, progress.root).tick()["queued"]
    progress.manifest = replace(progress.manifest, cli_status="running")
    progress.tick()
    progress = Progress(replace(progress.manifest, cli_status="needs_user"), progress.root)
    assert progress.tick()["queued"][0]["event"] == "needs_user"


def test_f3_unborn_index_content_and_initial_commit(lane):
    repo, _, progress = lane
    assert collect(progress.manifest)["available"]
    assert progress.tick()["snapshot"]["changes"] == []
    git(repo, "add", "test_fixture.py")
    staged = progress.tick()["snapshot"]
    assert staged["available"], staged["errors"]
    assert staged["changes"][0]["kinds"] == ["staged"]
    # A new observer treats the populated unborn index as preexisting baseline.
    fresh = Progress(progress.manifest, progress.root.parent / "fresh-state")
    assert fresh.tick()["snapshot"]["changes"] == []
    (repo / "test_fixture.py").write_text("def test_good():\n    assert True\n")
    changed = fresh.tick()["snapshot"]
    assert changed["available"] and changed["changes"][0]["kinds"] == ["content"]
    git(repo, "add", "test_fixture.py")
    assert fresh.tick()["snapshot"]["available"]
    git(repo, "commit", "-qm", "initial fixture")
    committed = fresh.tick()["snapshot"]
    assert committed["available"] and "committed" in committed["changes"][0]["kinds"]
    assert collect(progress.manifest)["head"]


@pytest.mark.parametrize("damage", ["detached", "malformed", "missing-object", "broken-ref", "nonbranch", "index"])
def test_f3_damaged_git_is_unavailable(lane, damage):
    repo, _, progress = lane
    git(repo, "add", "test_fixture.py")
    head = repo / ".git/HEAD"
    if damage == "detached":
        head.write_text("1" * 40 + "\n")
    elif damage == "malformed":
        head.write_text("ref: refs/heads/invalid..branch\n")
    elif damage == "missing-object":
        branch = git(repo, "symbolic-ref", "HEAD").strip()
        (repo / ".git" / branch).write_text("1" * 40 + "\n")
    elif damage == "broken-ref":
        branch = git(repo, "symbolic-ref", "HEAD").strip()
        (repo / ".git" / branch).write_text("broken-ref\n")
    elif damage == "nonbranch":
        head.write_text("ref: refs/tags/nonexistent\n")
    else:
        (repo / ".git/index").write_bytes(b"broken index")
    result = collect(progress.manifest)
    assert not result["available"] and "git_unavailable" in result["errors"], result
