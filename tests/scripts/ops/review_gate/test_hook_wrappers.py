"""Launchers, dynamic programs, Python import aliases, help-only exemptions and large scripts."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from hooks import guard_review_gate as hook


HOOK = Path(__file__).resolve().parents[4] / "scripts/ops/review-gate/hooks/guard_review_gate.py"

BLOCKED_SHELL = [
    'eval "codex exec -s read-only x"',
    "eval codex review",
    "tmux new -d -s r 'codex review'",
    "tmux new-session -d -s r codex exec -s read-only x",
    "tmux -L sock split-window -h 'claude -p hi'",
    "tmux new -d \\; send-keys 'claude -p hi' Enter",
    "tmux -c 'codex review'",
    "echo x | xargs -I{} codex exec -s read-only {}",
    "xargs -n 1 claude -p < files",
    "stdbuf -oL codex review",
    "stdbuf --output=L claude -p hi",
    "stdbuf -o L codex review",
    "script -q /dev/null codex review",
    "script -t 0 out claude -p hi",
    "script -q -c 'claude -p hi' /dev/null",
    "script --command='codex review' /dev/null",
    "npx -y @openai/codex exec -s read-only x",
    "npx @openai/codex@latest review",
    "npx --package @openai/codex codex review",
    "npx -c 'codex review'",
    "npx @anthropic-ai/claude-code -p hi",
    "$CODEX exec -s read-only x",
    '"${CLAUDE_BIN}" -p hi',
    'C=$(command -v codex); "$C" review',
    "export CB=/opt/bin/claude; $CB -p hi",
    "$(command -v codex) review",
    "nohup $CODEX review",
    "env X=1 $MAGI review --target HEAD",
]

ALLOWED_SHELL = [
    "codex exec --help",
    "codex --version",
    "codex review -h",
    "codex exec -s read-only --help",
    "magi --examples review",
    "magi --examples=review",
    "magi --schema",
    "magi sample --lane l --fixture f",
    "magi quality assess x",
    'eval "$(ssh-agent -s)"',
    "tmux new -d -s x 'npm test'",
    "tmux ls",
    "xargs rm",
    "stdbuf -oL make",
    "script -q /dev/null make",
    "npx prettier --check .",
    "npx -y @openai/codex exec -s workspace-write x",
    "$EDITOR notes.md",
    'HD=~/.local/bin/hermes-delegate; "$HD" review --reviewer codex --item i --commit abcdef1',
    "/Users/me/.local/bin/hermes-delegate review --reviewer codex --item i --commit abcdef1 "
    "--prompt /p.md --workdir /w",
    "/Users/me/.local/bin/hermes-delegate smoke codex",
    "$CODEX_HOME/bin/tool review",
]

BLOCKED_PYTHON = [
    'import subprocess as sp\nsp.run(["codex", "review"])',
    'from subprocess import run\nrun("codex review", shell=True)',
    'from subprocess import Popen as P\nP(["claude", "-p", "x"])',
    'import os as o\no.system("codex review")',
    'from hermes_tools import terminal as t\nt("codex review")',
    'codex_bin = "/x/codex"\nsubprocess.run([codex_bin, "exec", "-s", "read-only", prompt])',
    'subprocess.run([CODEX, "review"])',
    'subprocess.run([self.claude, "-p", prompt])',
    'cmd = "claude -p hi"\nterminal(cmd)',
]

ALLOWED_PYTHON = [
    'run("codex review")',
    'sp.run(["codex", "review"])',
    'from subprocess import run as go\nrun("codex review")',
    'subprocess.run([program, "claude", "-p", prompt])',
    'cmd = "claude -p hi"\ncmd = make()\nterminal(cmd)',
    'subprocess.run([sys.executable, "-m", "pytest"])',
]


class WrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-wrappers-")
        self.addCleanup(temporary.cleanup)
        self.log = Path(temporary.name) / "hook.jsonl"
        env = patch.dict(os.environ, REVIEW_GATE_HOOK_LOG=str(self.log))
        env.start()
        self.addCleanup(env.stop)

    def invoke(self, tool, data):
        payload = dict(tool_name=tool, tool_input=data, session_id="s")
        stdout = io.StringIO()
        return hook.main(io.StringIO(json.dumps(payload)), stdout), stdout.getvalue()

    def test_shell_launchers_and_dynamic_programs(self):
        for commands, code in ((BLOCKED_SHELL, 2), (ALLOWED_SHELL, 0)):
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(self.invoke("terminal", dict(command=command))[0], code)

    def test_execute_code_import_aliases_and_constants(self):
        for codes, code in ((BLOCKED_PYTHON, 2), (ALLOWED_PYTHON, 0)):
            for source in codes:
                with self.subTest(source=source):
                    self.assertEqual(self.invoke("execute_code", dict(code=source))[0], code)

    def test_block_messages_point_to_entries(self):
        for command, route in (("codex review", "hermes-delegate review --reviewer codex"),
                               ("claude -p hi", "hermes-delegate review --reviewer claude"),
                               ("claude -p hi", "hermes-delegate claude"),
                               ("codex review", "review-gate close")):
            with self.subTest(command=command, route=route):
                code, out = self.invoke("terminal", dict(command=command))
                self.assertEqual(code, 2)
                self.assertIn(route, json.loads(out)["message"])

    def run_hook(self, content):
        payload = json.dumps(dict(tool_name="write_file", tool_input=dict(path="/r/x.sh", content=content)))
        begin = time.perf_counter()
        result = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                                text=True, timeout=30, check=False, env=dict(os.environ, REVIEW_GATE_HOOK_LOG=str(self.log)))
        return result, time.perf_counter() - begin

    def test_large_scripts_finish_under_one_second_on_any_host(self):
        # 551KB took 15.8-20s on the server and failed open at the 10s hook timeout.
        for line, count in (("echo a>b<c>d 2>&1\n", 29000), ("echo a>b<c>d 2>&1\n", 60000),
                            ('if [ -n "$X" ]; then printf "%s\\n" "$X" | grep -v f > o; fi\n', 16000)):
            with self.subTest(line=line, count=count):
                result, elapsed = self.run_hook(line * count + "codex review\n")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertLess(elapsed, 1.0)

    def test_parse_budget_hands_over_to_the_conservative_fallback(self):
        script = "echo a>b 2>&1\n" * 20 + "codex review\n" + "review-gate run --ticket T -- codex review\n"
        payload = dict(tool_name="write_file", tool_input=dict(path="/r/x.sh", content=script))
        with patch.object(hook, "PARSE_BUDGET_SECONDS", -1):
            records = hook.detect(payload)
        self.assertEqual([(r["decision"], r["parse"]) for r in records],
                         [("blocked", "budget"), ("wrapped", "budget")])
        self.assertEqual(hook.PARSE_DEADLINE[0], float("inf"))
        self.assertEqual([r["parse"] for r in hook.detect(payload)], ["ok", "ok"])

    def test_scripts_over_the_size_limit_skip_the_parser(self):
        filler = "echo ok\n" * (hook.PARSE_SIZE_LIMIT // 8 + 1)
        for content, decision in ((filler + "codex review\n", "blocked"),
                                  (filler + 'REVIEW_GATE_NOT_REVIEW="docs" codex --help\n', None)):
            with self.subTest(decision=decision):
                payload = dict(tool_name="write_file", tool_input=dict(path="/r/x.sh", content=content))
                with patch.object(hook, "scan_shell", side_effect=AssertionError("parsed")):
                    records = hook.detect(payload)
                self.assertEqual([r["decision"] for r in records if r["decision"] == "blocked"],
                                 [decision] if decision else [])


if __name__ == "__main__":
    unittest.main()
