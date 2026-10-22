import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from hooks import guard_review_gate as hook


ROOT = Path(__file__).resolve().parents[4] / "scripts/ops/review-gate"
HOOK = ROOT / "hooks/guard_review_gate.py"

BLOCK = [
    'codex exec -s read-only -m gpt-6.1-sol "x"',
    'codex exec --ephemeral -m m - < p.md',
    'codex review --base main',
    'claude -p --model opus --permission-mode dontAsk --allowedTools "Read,Glob,Grep,Bash(git diff:*)" --output-format text',
    "/bin/zsh -ic 'export X=1; codex exec -s read-only -o out.md - < p.md'",
    "ssh -o BatchMode=yes choi138-ri 'claude --print \"review\"'",
    'echo $(codex review)',
    'magi review --target abc',
    'python3 x/scripts/run_codex_task.py --spec s --sandbox read-only',
    'review-gate run --ticket T -- true; codex review',
    "gtimeout 120 zsh -ic 'codex exec --ephemeral -s read-only \"Reply with exactly: AUTH_OK\"'",
    "cmux send --surface s 'claude -p hi'",
    'codex -m gpt-6.1-sol exec -s read-only x',
    '(codex review)',
    'if true; then codex review; fi',
    '{ claude -p hi; }',
    'claude ultrareview',
    'echo "$(codex review)"',
    "echo 'broken\nreview-gate run --ticket T -- true\ncodex review",
    'echo `claude -p hi`',
    "env -i FOO=x nohup sudo -u user nice -n 5 caffeinate -dimsu timeout --signal TERM 20 command builtin exec codex e x",
    'ssh -p22 -i key -J host user@remote codex --model=m exec --sandbox=read-only x',
    'while false; do claude -p hi; done',
    'for i in a b; do codex review; done',
    '! codex review',
    'codex --enable feature --local-provider ollama --add-dir repo -p p -a never exec x',
    'codex --oss-provider ollama exec --config=sandbox_mode=read-only x',
    'claude --model opus ultrareview --permission-mode acceptEdits',
    'codex exec "--full-auto is a flag in this prompt"',
    'REVIEW_GATE_NOT_REVIEW=research git log; claude -p hi',
    "echo 'bad\nREVIEW_GATE_NOT_REVIEW=research true; codex review",
    "echo 'bad\nreview-gate run --ticket T -- true; codex review",
    "echo 'bad\npython -c 'codex review'",
    'X="$(codex review)" true',
    'review-gate run --ticket "$(codex review)" -- true',
    '''bash -c "echo '$(codex review)'"''',
    'ssh -o "ProxyCommand=$(codex review)" host true',
    'sudo -u "$(claude -p hi)" true',
    '>out codex review',
    'codex <prompt exec 2>out',
    '2>&1 claude -p hi',
    'python3 -X dev -W ignore x/run_codex_task.py --sandbox read-only',
]

ALLOW = [
    'grep -n "codex exec -s read-only" notes.md',
    "echo 'claude -p'",
    'codex exec -s workspace-write "impl"',
    'codex exec --full-auto x',
    'claude -p --permission-mode acceptEdits x',
    'claude -p --allowedTools "Read,Edit" x',
    'magi lanes list',
    'magi --version',
    'python3 run_codex_task.py --sandbox workspace-write',
    'git log',
    'codex exec --approve-for-me x',
    'codex exec -c sandbox_mode=workspace-write x',
    "codex -c 'sandbox_mode=\"workspace-write\"' exec x",
    "echo '$(codex review)'",
    'magi review --help',
    'magi',
    'codex exec --sandbox=danger-full-access x',
    'codex --sandbox=workspace-write exec x',
    'codex exec --dangerously-bypass-approvals-and-sandbox x',
    'codex exec --yolo x',
    'claude -p --permission-mode=bypassPermissions x',
    'claude -p --dangerously-skip-permissions x',
    'claude -p --allowed-tools="Read NotebookEdit(cell)" hi',
    'claude -p --allowedTools Read MultiEdit hi',
    'claude -p hi --allowedTools=Write',
    'echo "\\$(codex review)"',
    'echo \\`codex review\\`',
    "echo '`claude -p hi`'",
    'magi --model lanes review',  # Shim deliberately does not consume option values.
    'magi review -h',
    'magi doctor',
    '# codex review\necho okay',
    'python3 -c run_codex_task.py --sandbox read-only',
    'python3 -m run_codex_task.py --sandbox read-only',
]

WRAPPED = [
    'cd repo && /usr/bin/python3 review_gate.py run --ticket $(cat ../run/r2.ticket) --timeout 2400 -- /bin/zsh -ic \'codex exec -s read-only -o o.md - < p.md\'',
    'review-gate run --ticket "$T" -- claude -p --model opus',
    'review_gate.py run --ticket=T -- codex review',
    'review-gate run --ticket ${T} -- echo "$(codex review)"',
    "echo 'bad\nreview-gate run --ticket T -- codex review",
    'python3 -X dev -u review_gate.py run --ticket T -- codex review',
]

BYPASS = [
    'REVIEW_GATE_NOT_REVIEW="auth smoke" codex exec -s read-only "AUTH_OK"',
    'env REVIEW_GATE_NOT_REVIEW=research claude -p hi',
    'FOO=x REVIEW_GATE_NOT_REVIEW=research zsh -lc "codex review"',
    "echo 'bad\nREVIEW_GATE_NOT_REVIEW=research codex review",
]


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-hook-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.log = self.tmp / "logs/hook.jsonl"
        self.env = patch.dict(os.environ, REVIEW_GATE_HOOK_LOG=str(self.log))
        self.env.start()
        self.addCleanup(self.env.stop)

    def invoke(self, command=None, tool="terminal", data=None):
        payload = dict(hook_event_name="pre_tool_call", tool_name=tool,
                       tool_input=data if data is not None else dict(command=command),
                       session_id="session-1", cwd="/private")
        stdout = io.StringIO()
        code = hook.main(io.StringIO(json.dumps(payload)), stdout)
        records = [json.loads(s) for s in self.log.read_text().splitlines()] if self.log.exists() else []
        return code, stdout.getvalue(), records

    def assert_decision(self, command, decision):
        code, out, records = self.invoke(command)
        self.assertEqual(code, 2 if decision == "blocked" else 0, (command, out, records))
        self.assertEqual(any(r["decision"] == decision for r in records), decision is not None,
                         (command, records))
        if decision is None:
            self.assertEqual(records, [])
        if code == 2:
            message = json.loads(out)
            self.assertEqual(message["action"], "block")
            kind = next(r["reviewer"] for r in records if r["decision"] == "blocked")
            route = {"claude": "hermes-delegate review --reviewer claude",
                     "codex": "hermes-delegate review --reviewer codex",
                     "runner": "hermes-delegate codex",
                     "magi": 'review-gate run --ticket "$T"'}[kind]
            self.assertIn(route, message["message"])
            self.assertIn("hermes-delegate smoke", message["message"])
            if kind != "magi":
                self.assertNotIn('review-gate run --ticket "$T"', message["message"])
        else:
            self.assertEqual(out, "")

    def test_execute_code_only_execution_arguments(self):
        for code in ['print("claude -p")', '"""codex review"""',
                     'subprocess.run(["echo", "$(codex review)"])']:
            with self.subTest(code=code):
                self.assertEqual(self.invoke(tool="execute_code", data=dict(code=code)), (0, "", []))

    def test_execute_code_execution_forms(self):
        for code in ['terminal("codex exec -s read-only x")',
                     'subprocess.run(["codex", "review"])',
                     'subprocess.Popen(("claude", "-p", "hi"))',
                     'os.system(command="magi review")',
                     'terminal(command=f"codex exec {prompt}")',
                     'subprocess.check_call("codex review")',
                     'subprocess.check_output("codex review")',
                     'subprocess.call("codex review")', 'os.popen("codex review")']:
            with self.subTest(code=code):
                self.assertEqual(self.invoke(tool="execute_code", data=dict(code=code))[0], 2)

    def test_write_and_patch_paths(self):
        for tool, key in [("write_file", "content"), ("patch", "new_string")]:
            for path in ["run/x.zsh", "x.sh", "x.bash", "x.command"]:
                with self.subTest(tool=tool, path=path):
                    self.assertEqual(self.invoke(tool=tool, data={"path": path, key: "claude -p hi"})[0], 2)
            self.assertEqual(self.invoke(tool=tool, data={"path": "notes.md", key: "claude -p"})[0], 0)
            self.assertEqual(self.invoke(tool=tool, data={"path": "script", key: "#!/bin/sh\nclaude -p hi"})[0], 2)

    def test_other_tools_ignored(self):
        self.assertEqual(self.invoke(tool="read_file", data=dict(content="codex review")), (0, "", []))

    def test_malformed_stdin(self):
        for text in ['{', 'null', '[]', '42', '{"tool_name":[]}']:
            output = io.StringIO()
            self.assertEqual(hook.main(io.StringIO(text), output), 0)
            self.assertEqual(output.getvalue(), "")
        self.assertFalse(self.log.exists())

    def test_error_is_fail_open_without_exception_message(self):
        with patch.object(hook, "detect", side_effect=RuntimeError("SECRET_SENTINEL_9f3")):
            code, out, records = self.invoke("codex review")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(records[0]["decision"], "error")
        self.assertEqual(records[0]["error"], "RuntimeError")
        self.assertNotIn("SECRET_SENTINEL_9f3", self.log.read_text())

    def test_log_privacy_schema_and_permissions(self):
        self.invoke('codex review "SECRET_SENTINEL_9f3"')
        self.invoke('review-gate run --ticket "$(cat SECRET_SENTINEL_9f3)" -- codex review')
        text = self.log.read_text()
        self.assertNotIn("SECRET_SENTINEL_9f3", text)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(set(records[0]), {"ts", "session_id", "tool", "decision", "reviewer", "ticket",
                                           "bypass_reason", "parse", "error"})
        self.assertEqual(records[0]["reviewer"], "codex")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertIsNone(records[1]["ticket"])

    def test_ticket_and_bypass_fields(self):
        _, _, records = self.invoke('review-gate run --ticket "$T" -- codex review')
        self.assertEqual(records[-1]["ticket"], "$T")
        _, _, records = self.invoke('REVIEW_GATE_NOT_REVIEW="' + 'x' * 300 + '" claude -p hi')
        self.assertEqual(records[-1]["bypass_reason"], 'x' * 200)

    def test_independent_launches_logged_once_each(self):
        _, _, records = self.invoke('codex review; claude -p hi | magi review')
        self.assertEqual([r["reviewer"] for r in records], ["codex", "claude", "magi"])

    def test_substitution_across_shell_layers_logged_once(self):
        _, _, records = self.invoke('bash -c "echo $(codex review)"')
        self.assertEqual([r["reviewer"] for r in records], ["codex"])

    def test_invalid_python_is_fail_open_error(self):
        code, out, records = self.invoke(tool="execute_code", data=dict(code='print("claude -p")\nif'))
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(records[-1]["error"], "SyntaxError")

    def assert_r2_result(self, command=None, decision=None, code=None, parse="ok"):
        if self.log.exists():
            self.log.unlink()
        result, out, records = self.invoke(command) if code is None else self.invoke(
            tool="execute_code", data=dict(code=code))
        self.assertEqual(result, 2 if decision == "blocked" else 0, (out, records))
        self.assertEqual([r["decision"] for r in records], [decision] if decision else [])
        self.assertTrue(all(r["parse"] == parse for r in records), records)
        if decision != "blocked":
            self.assertEqual(out, "")

    def test_r2_heredoc_plain_data(self):
        for command in [
                "git commit -F - <<'EOF'\nfeat: guard codex review launches\nEOF",
                "cat > notes.md <<'EOF'\nrun claude -p later\nEOF",
                "cat <<EOF\nclaude -p hi\nEOF",
                "python3 - <<'EOF'\nprint(\"claude -p\")\nEOF"]:
            with self.subTest(command=command):
                self.assert_r2_result(command)

    def test_r2_heredoc_quoted_delimiters(self):
        for delimiter in ["'EOF'", '"EOF"', r"\EOF", "E'O'F"]:
            with self.subTest(delimiter=delimiter):
                self.assert_r2_result("cat <<" + delimiter + "\n$(codex review)\n`claude -p hi`\nEOF")
        self.assert_r2_result("cat <<-'EOF'\n\t$(codex review)\n\tEOF")

    def test_r2_heredoc_wrapped_prompt(self):
        for command in [
                'review-gate run --ticket "$T" -- codex exec -s read-only - <<\'EOF\'\n'
                'Check the magi review-class gate and codex review paths.\nEOF',
                'env X=1 review-gate run --ticket T -- bash <<EOF\n$(codex review)\nEOF',
                'review-gate run --ticket T -- bash <<< "$(codex review)"']:
            with self.subTest(command=command):
                self.assert_r2_result(command, "wrapped")

    def test_r2_heredoc_multiple_and_following_commands(self):
        for command, decision in [
                ("cat <<A <<-'B'\ncodex review\nA\n\tclaude -p hi\n\tB", None),
                ("cat <<'EOF' > a\nhi\nEOF\ncodex review", "blocked"),
                ("cat <<A; cat <<B\nplain\nA\n$(codex review)\nB", "blocked"),
                ("cat <<A; bash <<'B'\nplain\nA\ncodex review\nB", "blocked"),
                ("bash <<A; cat <<'B'\necho okay\nA\ncodex review\nB", None),
                ("cat <<-'EOF'\n\thi\n\tEOF\nclaude -p hi", "blocked")]:
            with self.subTest(command=command):
                self.assert_r2_result(command, decision)

    def test_r2_heredoc_substitutions(self):
        for body, decision in [
                ("$(codex review)", "blocked"), ("`claude -p hi`", "blocked"),
                ("'$(codex review)'", "blocked"), ('"$(codex review)"', "blocked"),
                (r"\$(codex review)", None), (r"\`claude -p hi\`", None)]:
            with self.subTest(body=body):
                self.assert_r2_result("cat <<EOF\n" + body + "\nEOF", decision)

    def test_r2_heredoc_shell_receivers(self):
        for receiver in ["bash", "sh", "zsh", "dash", "env X=1 nohup bash",
                         "ssh h", "ssh -o BatchMode=yes h bash", "ssh h 'env X=1 sh'"]:
            with self.subTest(receiver=receiver):
                self.assert_r2_result(receiver + " <<'EOF'\nclaude -p hi\nEOF", "blocked")
        for receiver in ["bash -c true", "ssh h cat", "ssh h bash -lc true"]:
            with self.subTest(receiver=receiver):
                self.assert_r2_result(receiver + " <<'EOF'\ncodex review\nEOF")

    def test_r2_here_strings(self):
        for command, decision in [
                ('cat <<< "codex review"', None), ('bash <<< "codex review"', "blocked"),
                ("env X=1 sh <<< 'claude -p hi'", "blocked"),
                ('ssh h bash <<< "codex review"', "blocked"),
                ('cat <<< "$(codex review)"', "blocked"),
                ("cat <<< '$(codex review)'", None)]:
            with self.subTest(command=command):
                self.assert_r2_result(command, decision)

    def test_r2_heredoc_in_substitution(self):
        for command, decision in [
                ('gh pr create --title x --body "$(cat <<\'EOF\'\n'
                 'uses codex exec -s read-only\nEOF\n)"', None),
                ('echo "$(cat <<\'EOF\'\n) \' ` $(codex review)\nEOF\n)"', None),
                ('echo "$(cat <<EOF\n$(codex review)\nEOF\n)"', "blocked")]:
            with self.subTest(command=command):
                self.assert_r2_result(command, decision)
        self.assert_r2_result('echo "$(cat <<< \'codex review\')"')
        self.assert_r2_result('echo "$(bash <<< \'codex review\')"', "blocked")

    def test_r2_heredoc_scoped_fallback(self):
        self.assert_r2_result("cat <<'EOF'\ncodex review\nEOF")
        for command in ["cat <<EOF\ncodex review", "cat <<EOF\nclaude -p hi\n EOF",
                        "cat <<EOF\nreview-gate run --ticket T -- true\ncodex review",
                        "cat <<EOF\nREVIEW_GATE_NOT_REVIEW=research true; codex review",
                        "echo " + "$(" * 100 + "true" + ")" * 100 + "\ncodex review"]:
            with self.subTest(command=command):
                self.assert_r2_result(command, "blocked", parse="fallback")

    def test_r2_execute_dynamic_argv(self):
        for code in [
                'import subprocess\nsubprocess.run(["codex","exec","-s","read-only",prompt])',
                'subprocess.run(["claude","-p",prompt])',
                'subprocess.Popen(("codex","review",*args))',
                'subprocess.call(["claude","-p",make_prompt()])',
                'terminal(command=["codex","exec",None])']:
            with self.subTest(code=code):
                self.assert_r2_result(code=code, decision="blocked")
        for code in [
                'subprocess.run(["codex","exec","-s","workspace-write",prompt])',
                'subprocess.run(["codex",subcommand,"review"])',
                'subprocess.run([program,"claude","-p",prompt])',
                'subprocess.run([*args,"claude","-p"])',
                'subprocess.run([f"claude{suffix}","-p",prompt])',
                'print("claude -p")']:
            with self.subTest(code=code):
                self.assert_r2_result(code=code)

    def test_fallback_and_heredoc(self):
        _, _, records = self.invoke("echo 'bad\ncodex review")
        self.assertEqual(records[0]["parse"], "fallback")
        self.assertEqual(self.invoke("cat <<EOF\nclaude -p hi\nEOF")[0], 0)

    def test_empty_bypass_does_not_exempt(self):
        self.assertEqual(self.invoke('env REVIEW_GATE_NOT_REVIEW="" codex review')[0], 2)

    def test_fifty_kb_under_200ms_including_process_start(self):
        for command in ['echo "' + 'x' * 50000 + '"; codex review',
                        'echo "' + 'x' * 50000 + '\ncodex review',
                        'codex review;' * 3846]:
            payload = dict(tool_name="terminal", tool_input=dict(command=command))
            begin = time.perf_counter()
            result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                    capture_output=True, text=True, timeout=2)
            elapsed = time.perf_counter() - begin
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertLess(elapsed, 0.2)


def add_cases():
    for group, commands, decision in [("block", BLOCK, "blocked"), ("allow", ALLOW, None),
                                       ("wrapped", WRAPPED, "wrapped"), ("bypass", BYPASS, "bypass")]:
        for index, command in enumerate(commands):
            def test(self, command=command, decision=decision):
                self.assert_decision(command, decision)
            setattr(HookTests, "test_{}_{:02d}".format(group, index + 1), test)


add_cases()
