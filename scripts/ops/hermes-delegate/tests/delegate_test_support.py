import importlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
common = importlib.import_module("hermes_delegate_common")
codex = importlib.import_module("hermes_delegate_codex")
entry = importlib.import_module("hermes_delegate")
review = importlib.import_module("hermes_delegate_review")
runtime = importlib.import_module("hermes_delegate_runtime")
smoke = importlib.import_module("hermes_delegate_smoke")
spec = importlib.import_module("hermes_delegate_spec")
status = importlib.import_module("hermes_delegate_status")
watch = importlib.import_module("hermes_delegate_watch")
worker = importlib.import_module("hermes_delegate_worker")
RUNNER_AVAILABLE = common.RUNNER.exists() and common.RUNNER_PYTHON.exists()


SCRIPT = str(HERE / "hermes_delegate.py")


REAL_GATE = "/Users/choegeun-won/.local/share/review-gate/review_gate.py"


METACHARS = set("$`~;|&*?{}()#<>[]\n\r")


VALID_SPEC = """## External contracts
- N/A: This isolated fixture has no external contract.

## Failure paths
- Ordering/concurrency: Each operation is serialized and its result is checked.
- Interruption: The interrupted operation leaves no generated artifacts.
- Rejection/partial failure: Each rejected operation returns the expected error.
- Repeat/retry: Repeated operations return exactly the same expected result.
- External: N/A: No external services are called by this fixture.

## Acceptance
- Verify every expected result matches its exact expected value.
"""


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True, timeout=120)


def make_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo + "/src/pkg")
    os.makedirs(repo + "/docs")
    for name, body in (("src/a.py", "print(1)\n"), ("src/pkg/b.ts", "export {}\n"),
                       ("src/data.json", "{}\n"), ("docs/x.md", "# x\n"), ("SPEC.md", VALID_SPEC)):
        Path(repo, name).write_text(body, encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hd-test-", dir="/private/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cli(self, *argv, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, text=True, encoding="utf-8-sig",
                              env=merged, timeout=120, check=False)


STUB_CMUX = """#!/bin/sh
echo "$@" >> "$0.log"
case "$1" in
  ping) echo PONG ;;
  new-workspace) [ -e "$0.fail" ] && { echo "boom" >&2; exit 7; }; echo "OK workspace:9" ;;
esac
"""


STUB_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) [ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
       shift; printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; echo review-body
       if [ -n "${STUB_SLEEP:-}" ]; then sleep "$STUB_SLEEP"; fi ;;
esac
"""


class ReviewBase(Base):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True, timeout=120, encoding="utf-8-sig").stdout.strip()
        self.prompt = os.path.join(self.tmp, "prompt.md")
        Path(self.prompt).write_text("Review the diff.\n", encoding="utf-8")
        self.state = os.path.join(self.tmp, "state")
        self.db = os.path.join(self.tmp, "ledger.sqlite")
        self.gate = os.path.join(self.tmp, "review-gate")
        Path(self.gate).write_text(f"#!/bin/sh\nexec /usr/bin/python3 {REAL_GATE} \"$@\"\n", encoding="utf-8")
        os.chmod(self.gate, 0o755)
        self.env = {"REVIEW_GATE_DB": self.db, "HERMES_DELEGATE_STATE_ROOT": self.state,
                    "HERMES_DELEGATE_REVIEW_GATE": self.gate,
                    "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def review(self, *extra, env=None):
        return self.cli("review", "--reviewer", "claude", "--item", "hd-test-item", "--commit",
                        self.head[:12], "--prompt", self.prompt, "--workdir", self.repo, "--no-launch",
                        *extra, env=env or self.env)

    def open_item(self):
        subprocess.run(["/usr/bin/python3", REAL_GATE, "open", "--item", "hd-test-item", "--goal", "t"],
                       env=dict(os.environ, REVIEW_GATE_DB=self.db), check=True, capture_output=True, timeout=120)

    def fields(self, stdout):
        return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line.split(" ")[0])

    def stub_cmux(self, fail=False):
        cmux = os.path.join(self.tmp, "cmux")
        Path(cmux).write_text(STUB_CMUX, encoding="utf-8")
        os.chmod(cmux, 0o755)
        if fail:
            Path(cmux + ".fail").write_text("", encoding="utf-8")
        return cmux

    def run_args(self, *extra, env=None):
        return self.cli("review", "--reviewer", "claude", "--item", "hd-test-item", "--commit",
                        self.head[:12], "--prompt", self.prompt, "--workdir", self.repo, *extra,
                        env=env or self.env)

    def stub_run(self, extra_env=None):
        stub = os.path.join(self.tmp, "stub-gate")
        Path(stub).write_text(STUB_GATE, encoding="utf-8")
        os.chmod(stub, 0o755)
        result = self.review(env=dict(self.env, HERMES_DELEGATE_REVIEW_GATE=stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fields(result.stdout)["run_dir"], os.path.join(self.tmp, "stub.log")


STUB_CLAUDE = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"
echo changed >> src/a.py
echo '{"type":"system","subtype":"init","model":"claude-sonnet-5-5"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit"}]}}'
printf '%s\\n' '{"type":"result","num_turns":2,"is_error":false,"permission_denials":[],"result":"done\\nIMPLEMENTED"}'
"""


STUB_CODEX = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; env > "$STUB_LOG.env"
final=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
printf 'QA report\\nQA: PASS ios\\n' > "$final"
echo '{"type":"thread.started"}'
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
echo '{"type":"item.completed","item":{"type":"agent_message"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":5}}'
"""


@unittest.skipUnless(common.GTIMEOUT.exists(), "gtimeout missing")
class WorkerTests(Base):
    kind = "claude"

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True, timeout=120, encoding="utf-8-sig").stdout.strip()
        self.spec = os.path.join(self.tmp, "SPEC.md")
        Path(self.spec).write_text(VALID_SPEC, encoding="utf-8")
        self.state = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"HERMES_DELEGATE_STATE_ROOT": self.state, "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def worker(self, *extra, env=None, kind=None):
        kind = kind or self.kind
        base = ["--spec", self.spec, "--workdir", self.repo, "--timeout", "900"]
        if kind == "claude" and not any(word.startswith("--class") for word in extra):
            base += ["--class", "ambiguous"]
        return self.cli(kind, *base, *extra, env=dict(self.env, **(env or {})))

    def fields(self, stdout):
        return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line.split(" ")[0])

    def prepared(self, *extra, env=None, kind=None):
        result = self.worker("--no-launch", *extra, env=env, kind=kind)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, self.fields(result.stdout)["run_dir"]

    def stub(self, name, body):
        path = os.path.join(self.tmp, name)
        Path(path).write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
        return path

    def run_inner(self, run_dir):
        return subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=self.log),
                              capture_output=True, text=True, timeout=60, check=False, encoding="utf-8-sig")


FAKE_TOKEN = "sk-ant-oat01-" + "Zz9" * 30


STUB_SLOW_CLAUDE = f"""#!{sys.executable}
import os
from pathlib import Path
import signal
import sys

log = os.environ.get("STUB_LOG")
if not log:
    print("STUB_LOG is unset", file=sys.stderr)
    sys.exit(97)
signals = {{signal.SIGTERM, signal.SIGUSR1}}
signal.pthread_sigmask(signal.SIG_BLOCK, signals)
Path(log + ".pid").write_text(str(os.getpid()) + "\\n", encoding="utf-8")
Path(log).write_text("started\\n", encoding="utf-8")
while signal.sigwait(signals) != signal.SIGTERM:
    Path(log + ".alive").write_text("alive\\n", encoding="utf-8")
with Path(log + ".term").open("a", encoding="utf-8") as receipt:
    receipt.write("got-term\\n")
sys.exit(7)
"""


STUB_SLOW_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) [ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
       trap 'echo got-term >> "$STUB_LOG.term"; exit 5' TERM
       echo started > "$STUB_LOG"; sleep 60 & wait ;;
esac
"""


STUB_EXEC_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) while [ "$1" != "--" ]; do shift; done; shift; exec "$@" ;;
esac
"""


STUB_ENV_CLAUDE = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
cat > /dev/null
printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN:-}" | wc -c | tr -d ' ' > "$STUB_LOG.toklen"
echo "api_key=${ANTHROPIC_API_KEY:-unset} base_url=${ANTHROPIC_BASE_URL:-unset}" > "$STUB_LOG.anthropic"
if [ -n "${STUB_LEAK:-}" ]; then echo "leak $CLAUDE_CODE_OAUTH_TOKEN"; fi
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}'
printf '%s\\n' '{"type":"result","num_turns":1,"is_error":false,"permission_denials":[],"result":"APPROVED: no P0/P1"}'
"""


STUB_REVIEW_CODEX = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; env > "$STUB_LOG.env"
final=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
printf 'No findings.\\nAPPROVED: no P0/P1\\n' > "$final"
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":5}}'
"""


STUB_SMOKE = """#!/bin/sh
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
if [ -n "${STUB_SMOKE_FAIL:-}" ]; then echo "auth failed" >&2; exit 3; fi
if [ -n "$final" ]; then echo ok > "$final"; else echo ok; fi
"""


def wait_for(path, seconds=15):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


def no_process(pattern):
    found = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True, check=False, timeout=120, encoding="utf-8-sig")
    return found.returncode == 1


def wait_condition(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@contextmanager
def observing_wait(ready):
    original = watch.select.kqueue

    class ObservedQueue:
        def __init__(self):
            self.queue = original()

        def control(self, changes, count, timeout):
            self.queue.control(changes, 0, 0)
            ready.set()
            return self.queue.control([], count, timeout)

    with mock.patch.object(watch.select, "kqueue", ObservedQueue):
        yield


def stall_run(root, kind="claude", events=(), name="run"):
    run_dir = os.path.join(root, name)
    os.mkdir(run_dir)
    Path(run_dir, "meta.json").write_text(json.dumps({"kind": kind, "via": "direct"}), encoding="utf-8")
    Path(run_dir, "started_at").write_text("2026-10-22T10:00:00+0900\n", encoding="utf-8")
    Path(run_dir, kind + ".out").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return run_dir


def bash_call(command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def kill_process_group(pid, sig):
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise RuntimeError("process-group tests require POSIX process groups")
    killpg(pid, sig)
