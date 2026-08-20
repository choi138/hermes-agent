from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_run_tests_shell_prefers_valid_explicit_hermes_python(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)

    source = Path(__file__).resolve().parents[2] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")
    (scripts / "run_tests_parallel.py").write_text(
        "import sys\nprint(f'RUNNER_PYTHON={sys.executable}')\n",
        encoding="utf-8",
    )

    fake_venv = repo / ".venv" / "bin"
    fake_venv.mkdir(parents=True)
    (fake_venv / "activate").write_text("# marker\n", encoding="utf-8")
    fake_python = fake_venv / "python"
    fake_python.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    fake_python.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "HERMES_PYTHON": sys.executable,
    }
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert f"RUNNER_PYTHON={sys.executable}" in result.stdout
