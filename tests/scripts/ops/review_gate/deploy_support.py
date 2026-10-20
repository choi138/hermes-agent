"""Disposable source checkout and SSH transport shared by deployment contract tests."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

OPS = Path(__file__).resolve().parents[4] / "scripts/ops"


def checkout(path):
    shutil.copytree(OPS, path / "scripts/ops", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".review-gate-test-*"))
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for args in (("init", "-q"), ("add", "scripts/ops"),
                 ("-c", "user.name=Ops Test", "-c", "user.email=ops@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(path), *args], check=True, env=env,
                       capture_output=True, text=True)
    return path / "scripts/ops/review-gate"


def fake_ssh(path, server_home, log):
    path.write_text(
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.environ['HOME'] = {str(server_home)!r}\n"
        "assert sys.argv[-2:] == ['/usr/bin/python3', '-']\n"
        "os.execv('/usr/bin/python3', ['/usr/bin/python3', '-B', '-'])\n")
    return shlex.join([sys.executable, str(path)])
