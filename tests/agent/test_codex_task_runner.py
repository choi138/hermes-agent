"""FAKE subprocess contract tests: these never execute the installed Codex."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


def module():
    return importlib.import_module("agent.codex_task_runner")


@pytest.fixture
def inputs(tmp_path):
    spec = tmp_path / "SPEC.md"
    spec.write_text("Literal $(touch INJECTED); `echo unsafe`\n한글 SPEC")
    output = tmp_path / "artifacts"
    output.mkdir(mode=0o700)
    return dict(spec=spec, workdir=tmp_path, allowed_root=tmp_path, output_dir=output,
                timeout=5, sandbox="read-only")


def run_stub(inputs, tmp_path, body, tier="standard", timeout=5, output_limit=4096, extra_args=(), setup="", expect_status=True):
    """Actual CLI in its own process, owning only its fake Codex child.

    No test guard is disabled. In-process tests cannot prove process-group
    cleanup on hosts whose sandbox denies psutil's ancestry inspection.
    """
    script = Path(__file__).resolve().parents[2] / "scripts/run_codex_task.py"
    child = tmp_path / "fake_codex.py"
    child.write_text(body)
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text('''import runpy, subprocess, sys
sys.path.insert(0, sys.argv[1])
import agent.codex_task_runner as runner
original = runner.run_task
child, limit = sys.argv[2], int(sys.argv[3])
def popen(argv, **kw):
    return subprocess.Popen([sys.executable, child, *argv[1:]], **kw)
runner.run_task = lambda request: original(request, popen=popen, output_limit=limit)
script = sys.argv[4]
sys.argv = sys.argv[4:]
''' + setup + '\nrunpy.run_path(script, run_name="__main__")\n')
    args = ["--spec", str(inputs["spec"]), "--workdir", str(tmp_path),
            "--allowed-root", str(tmp_path), "--output-dir", str(inputs["output_dir"]),
            "--tier", tier, "--timeout", str(timeout), *extra_args]
    completed = subprocess.run([sys.executable, str(bootstrap), str(script.parent.parent),
        str(child), str(output_limit), str(script), *args], capture_output=True, text=True, timeout=15)
    if not expect_status:
        return completed
    result = json.loads(completed.stdout)
    assert completed.returncode == result["exit_code"], completed.stderr
    return result


ECHO = '''import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()}))
print("fake stderr", file=sys.stderr)
'''


@pytest.mark.parametrize("tier,effort", [("light", "low"), ("standard", "medium"), ("deep", "high"), ("max", "max")])
def test_tiers_real_subprocess_serialization(inputs, tmp_path, tier, effort):
    parent = {"route": "chat", "reasoning_config": {"effort": "max", "selection": "pinned"}}
    before = json.dumps(parent)
    result = run_stub(inputs, tmp_path, ECHO, tier=tier)
    assert result["status"] == "cli_completed"
    assert result["exit_code"] == 0
    run = Path(result["artifact_dir"])
    observed = json.loads((run / "events.jsonl").read_text())
    assert observed["stdin"] == inputs["spec"].read_text()
    assert observed["cwd"] == str(tmp_path.resolve())
    assert observed["argv"] == ["exec", "--ephemeral", "-m", "gpt-6-astra", "-c",
        f'model_reasoning_effort="{effort}"', "-s", "read-only", "-C", str(tmp_path.resolve()), "--json", "-"]
    assert json.dumps(parent) == before
    assert not (tmp_path / "INJECTED").exists()
    assert result["selection"]["selected_tier"] == tier
    assert "Literal" not in (run / "status.json").read_text()
    assert (run.stat().st_mode & 0o777) == 0o700
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in run.iterdir())


def test_pin_precedence_and_strict_values():
    m = module()
    selection = m.WorkerSelection("light", policy="pinned", pinned_tier="max")
    assert selection.metadata() == {"requested_tier": "light", "selected_tier": "max", "source": "user_pin", "policy": "pinned", "effort": "max"}
    assert m.WorkerSelection().metadata()["selected_tier"] == "standard"
    for tier in ("MAX", " high", "", None, [], 2, "deep;echo bad"):
        with pytest.raises(ValueError):
            m.WorkerSelection(tier)
    with pytest.raises(ValueError):
        m.WorkerSelection(policy="pinned")
    with pytest.raises(ValueError):
        m.WorkerSelection(pinned_tier="max")


@pytest.mark.parametrize("field,value", [("timeout", float("inf")), ("timeout", float("nan")), ("timeout", 0), ("timeout", True), ("sandbox", "danger-full-access"), ("model", "--yolo")])
def test_invalid_request(inputs, field, value):
    with pytest.raises(ValueError):
        module().TaskRequest(**{**inputs, field: value})


def test_symlink_escape_and_output_safety(inputs, tmp_path):
    m = module()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    secret = outside / "SPEC.md"
    secret.write_text("not allowed")
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        m.TaskRequest(**{**inputs, "spec": link / "SPEC.md"})
    with pytest.raises(ValueError):
        m.TaskRequest(**{**inputs, "workdir": link})
    inputs["output_dir"].chmod(0o777)
    with pytest.raises(ValueError):
        m.TaskRequest(**inputs)


def test_nonzero_timeout_cancel_and_output_limit(inputs, tmp_path):
    m = module()
    cases = [("import sys; print('failed'); sys.exit(17)", "cli_failed", 17, 5),
             ("import time; time.sleep(30)", "timed_out", 124, .2),
             ("print('x' * 100000)", "output_limit", 74, 5)]
    for body, status, code, timeout in cases:
        result = run_stub(inputs, tmp_path, body, timeout=timeout)
        assert (result["status"], result["exit_code"]) == (status, code)
        assert Path(result["artifact_dir"], "events.jsonl").stat().st_size <= 4096
    cancel = threading.Event()
    cancel.set()
    result = m.run_task(m.TaskRequest(**inputs), popen=lambda *a, **k: pytest.fail("must not spawn"), cancel=cancel)
    assert (result["status"], result["exit_code"]) == ("cancelled", 130)


def test_spawn_failure_safe_and_unique_artifacts(inputs):
    m = module()
    def missing(*a, **k):
        raise OSError("secret-like error must not enter status")
    first = m.run_task(m.TaskRequest(**inputs), popen=missing)
    second = m.run_task(m.TaskRequest(**inputs), popen=missing)
    assert first["status"] == "launch_failed" and first["exit_code"] != 0
    assert first["artifact_dir"] != second["artifact_dir"]
    assert "secret-like" not in json.dumps(first)


def test_real_cli_pin_precedence_and_workspace_write(inputs, tmp_path):
    result = run_stub(inputs, tmp_path, ECHO, tier="light",
        extra_args=("--selection", "pinned", "--pinned-tier", "max", "--sandbox", "workspace-write"))
    observed = json.loads(Path(result["artifact_dir"], "events.jsonl").read_text())
    assert 'model_reasoning_effort="max"' in observed["argv"]
    assert observed["argv"][observed["argv"].index("-s") + 1] == "workspace-write"
    assert result["selection"]["source"] == "user_pin"
    assert result["selection"]["requested_tier"] == "light"


def test_timeout_reaps_owned_child_and_sigterm_cancels_cli(inputs, tmp_path):
    body = "import os, time; print(os.getpid(), flush=True); time.sleep(30)"
    result = run_stub(inputs, tmp_path, body, timeout=.5)
    assert result["status"] == "timed_out"
    pid = int(Path(result["artifact_dir"], "events.jsonl").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # Liveness only; no test guard disabled.
    result = run_stub(inputs, tmp_path, body,
        setup="import signal, threading\nthreading.Timer(.5, lambda: signal.raise_signal(signal.SIGTERM)).start()")
    assert result["status"] == "cancelled" and result["exit_code"] == 130
    pid = int(Path(result["artifact_dir"], "events.jsonl").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_artifact_close_failure_cannot_report_success(inputs, tmp_path):
    result = run_stub(inputs, tmp_path, ECHO, setup='''
from contextlib import contextmanager
private_file = runner._private_file
@contextmanager
def failed_close(path):
    with private_file(path) as stream:
        yield stream
    if path.name == "events.jsonl":
        raise OSError("simulated full disk")
runner._private_file = failed_close
''')
    assert result["process_returncode"] == 0
    assert result["exit_code"] == 74
    assert result["status"] == "artifact_or_transport_failed"


def test_status_file_conflict_fails_closed(inputs, tmp_path):
    # The test child deliberately creates a conflicting status file. The
    # launcher's exclusive creation must fail rather than overwrite or print success.
    body = """from pathlib import Path
for p in Path('artifacts').glob('codex-task-*'):
    (p / 'status.json').write_text('existing')
"""
    result = run_stub(inputs, tmp_path, body, expect_status=False)
    assert result.returncode == 74
    assert result.stdout == ""
    assert json.loads(result.stderr)["status"] == "invalid_request_or_artifact_error"
    assert next(inputs["output_dir"].glob("*/status.json")).read_text() == "existing"


def test_cli_help_dry_run_and_real_script_with_injected_process(inputs, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/run_codex_task.py"
    args = ["--spec", str(inputs["spec"]), "--workdir", str(tmp_path),
            "--allowed-root", str(tmp_path), "--output-dir", str(inputs["output_dir"])]
    help_result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0 and "--tier" in help_result.stdout
    dry = subprocess.run([sys.executable, str(script), *args, "--dry-run"], capture_output=True, text=True)
    assert dry.returncode == 0
    assert json.loads(dry.stdout)["argv"][0] == "codex"
    assert "Literal" not in dry.stdout
    assert list(inputs["output_dir"].iterdir()) == []
    # Python DI bootstrap executes the actual __main__ CLI and argparse, with
    # a real subprocess adapter. No executable override is exposed by the CLI.
    child = tmp_path / "child.py"
    child.write_text(ECHO + "sys.exit(23)\n")
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text('''import runpy, subprocess, sys
sys.path.insert(0, sys.argv[1])
import agent.codex_task_runner as runner
original = runner.run_task
child = sys.argv[2]
def popen(argv, **kw):
    return subprocess.Popen([sys.executable, child, *argv[1:]], **kw)
runner.run_task = lambda request: original(request, popen=popen)
script = sys.argv[3]
sys.argv = sys.argv[3:]
runpy.run_path(script, run_name="__main__")
''')
    executed = subprocess.run([sys.executable, str(bootstrap), str(script.parent.parent), str(child), str(script), *args], capture_output=True, text=True, timeout=15)
    assert executed.returncode == 23, executed.stderr
    result = json.loads(executed.stdout)
    assert result["status"] == "cli_failed"
    observed = json.loads(Path(result["artifact_dir"], "events.jsonl").read_text())
    assert observed["stdin"] == inputs["spec"].read_text()
