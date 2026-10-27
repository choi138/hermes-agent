import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
from delegate_test_support import FAKE_TOKEN, STUB_CLAUDE, STUB_CMUX, STUB_CODEX, STUB_ENV_CLAUDE, STUB_EXEC_GATE, STUB_SLOW_CLAUDE, VALID_SPEC, WorkerTests, no_process, wait_for
from delegate_test_support import wait_condition
from delegate_test_support import common
from delegate_test_support import watch
from delegate_test_support import worker


class ClaudeTests(WorkerTests):
    def test_generates_script_without_launching(self):
        result, run_dir = self.prepared()
        fields = self.fields(result.stdout)
        self.assertTrue(run_dir.startswith(self.state + "/claude/repo-"))
        self.assertEqual(fields["rc"], run_dir + "/claude.rc")
        self.assertEqual(fields["launched"], "no")
        self.assertNotIn("ticket", fields)
        self.assertEqual(os.stat(run_dir).st_mode & 0o777, 0o700)
        watcher = [line for line in result.stdout.splitlines() if line.startswith("watcher:")]
        self.assertEqual(watcher, [f'watcher: terminal(command="{common.HD_BIN} wait {run_dir} --timeout 1800", background=true, '
                                   'notify_on_complete=true, continue_on_complete=true, persist_on_release=true)'
                                   ])
        self.assertIn("class=ambiguous model=opus effort=medium advisor=off", result.stderr)
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn(f"{common.GTIMEOUT} -k 60 900 {common.CLAUDE_BIN} -p --model opus --effort medium --tools Read,Glob,Grep,Edit,Write,Bash "
                      "--allowedTools Bash --permission-mode acceptEdits --permission-prompts none "
                      "--strict-mcp-config", script)
        self.assertIn(f"< {run_dir}/spec.md > {run_dir}/claude.out 2>> {run_dir}/claude.err", script)
        for word in ("--dangerously-skip-permissions", "bypassPermissions", "--advisor", "review-gate",
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
            self.assertNotIn(word, script)
        self.assertIn("[[ \"$(", script)
        self.assertIn(self.head, script)
        self.assertEqual(Path(run_dir, "spec.md").read_text(encoding="utf-8-sig"), VALID_SPEC)
        launch = json.loads(Path(run_dir, "launch.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(launch[:4], ["/nonexistent/cmux", "new-workspace", "--name", "claude-repo"])
        self.assertEqual(launch[launch.index("--cwd") + 1], self.repo)
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        self.assertEqual(launch[launch.index("--command") + 1],
                         "/bin/zsh -ic " + shlex.quote("exec /bin/zsh " + shlex.quote(run_dir + "/inner.zsh")))
        meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((meta["kind"], meta["class"], meta["model"], meta["effort"], meta["advisor"],
                          meta["head"]), ("claude", "ambiguous", "opus", "medium", None, self.head))
        syntax = subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"], capture_output=True, check=False, timeout=120)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_class_table_is_the_only_model_source(self):
        for name, policy in common.CLAUDE_IMPL_POLICY.items():
            _, run_dir = self.prepared("--class", name, "--label", "c-" + name)
            script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
            self.assertIn("-p --model {} --effort {} ".format(policy["model"], policy["effort"]), script)
        self.assertEqual(common.CLAUDE_IMPL_POLICY["judgment"]["model"], "opus")
        self.assertEqual(common.CLAUDE_IMPL_POLICY["ambiguous"], {"model": "opus", "effort": "medium"})
        self.assertEqual(common.CLAUDE_IMPL_POLICY["mechanical"], {"model": "sonnet", "effort": "medium"})
        self.assertEqual(common.CLAUDE_IMPL_POLICY["judgment"], {"model": "opus", "effort": "high"})
        result = self.worker("--class", "frontier")
        self.assertEqual(result.returncode, 2)

    def test_advisor_is_opt_in_with_per_run_settings(self):
        _, run_dir = self.prepared("--class", "mechanical", "--advisor", "opus")
        line = next(l for l in Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig").splitlines() if " -p " in l)
        argv = shlex.split(line)
        self.assertEqual(argv[argv.index("--advisor") + 1], "opus")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         {"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": ""}})
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))["advisor"], "opus")
        self.assertEqual(self.worker("--advisor", "sonnet").returncode, 2)
        for opus_class in ("judgment", "ambiguous"):
            pointless = self.worker("--class", opus_class, "--advisor", "opus")
            self.assertEqual(pointless.returncode, 2)
            self.assertIn("already runs on opus", pointless.stderr)

    def test_rejects_model_effort_and_permission_overrides(self):
        for word in ("--model", "--effort", "--model=opus", "--tier", "--pinned-model", "--cli", "--sandbox",
                     "--permission-mode", "--dangerously-skip-permissions", "--allowedTools", "--settings"):
            result = self.worker(word, "x")
            self.assertEqual(result.returncode, 2, word)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(result.stderr.strip().splitlines()), 1)
            self.assertTrue(result.stderr.startswith("hermes-delegate: " + word.split("=")[0] + " is not accepted"),
                            result.stderr)
        self.assertFalse(os.path.exists(self.state + "/claude"))

    def test_required_options_and_ranges(self):
        missing_class = self.cli("claude", "--spec", self.spec, "--workdir", self.repo, "--timeout", "900",
                                 env=dict(os.environ, **self.env))
        self.assertEqual(missing_class.returncode, 2)
        self.assertIn("--class", missing_class.stderr)
        missing_timeout = self.cli("claude", "--spec", self.spec, "--workdir", self.repo, "--class", "ambiguous",
                                   env=dict(os.environ, **self.env))
        self.assertEqual(missing_timeout.returncode, 2)
        for value in ("59", "86401"):
            self.assertEqual(self.cli("claude", "--spec", self.spec, "--workdir", self.repo, "--class",
                                      "ambiguous", "--timeout", value, "--no-launch",
                                      env=dict(os.environ, **self.env)).returncode, 2)
        self.assertEqual(self.worker("--label", "bad label").returncode, 2)
        empty = os.path.join(self.tmp, "empty.md")
        Path(empty).write_text("", encoding="utf-8")
        result = self.cli("claude", "--spec", empty, "--workdir", self.repo, "--class", "ambiguous",
                          "--timeout", "900", "--no-launch", env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, 2)

    def test_dirty_or_non_root_worktree_refused(self):
        Path(self.repo, "src/a.py").write_text("dirty\n", encoding="utf-8")
        result = self.worker("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)
        Path(self.repo, "src/a.py").write_text("print(1)\n", encoding="utf-8")
        Path(self.repo, "new.py").write_text("x\n", encoding="utf-8")
        self.assertIn("untracked", self.worker("--no-launch").stderr)
        os.remove(self.repo + "/new.py")
        sub = self.cli("claude", "--spec", self.spec, "--workdir", self.repo + "/src", "--class", "ambiguous",
                       "--timeout", "900", "--no-launch", env=dict(os.environ, **self.env))
        self.assertEqual(sub.returncode, 1)
        self.assertIn("must be a git worktree root", sub.stderr)
        self.assertFalse(os.path.exists(self.state + "/claude") and os.listdir(self.state + "/claude"))

    def test_inner_script_runs_worker_and_wait_summarizes(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "0")
        argv = Path(self.log).read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(argv[:5], ["-p", "--model", "opus", "--effort", "medium"])
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertEqual(Path(self.log + ".stdin").read_text(encoding="utf-8-sig"), VALID_SPEC)
        waited = self.cli("wait", run_dir, "--timeout", "5")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("claude rc=0", waited.stdout)
        self.assertIn("turns=2 tool_uses=1 (Edit:1)", waited.stdout)
        self.assertIn("last_line=IMPLEMENTED", waited.stdout)
        self.assertIn("model=claude-sonnet-5-5", waited.stdout)
        self.assertIn(f"worktree: HEAD {self.head[:12]} unchanged, 1 uncommitted entries", waited.stdout)
        self.assertEqual(Path(run_dir, "claude.result.md").read_text(encoding="utf-8-sig"), "done\nIMPLEMENTED")

    def test_inner_script_refuses_changed_tree(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        Path(self.repo, "src/a.py").write_text("print(3)\n", encoding="utf-8")
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "2")
        self.assertIn("became dirty", Path(run_dir, "claude.err").read_text(encoding="utf-8-sig"))
        self.assertFalse(os.path.exists(self.log), "the worker must not run")

    def test_launch_through_stub_cmux_and_launch_run(self):
        cmux = self.stub("cmux", STUB_CMUX)
        Path(cmux + ".fail").write_text("", encoding="utf-8")
        result = self.worker(env={"HERMES_DELEGATE_CMUX": cmux})
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing ran: retry with hermes-delegate claude --launch-run", result.stderr)
        run_dir = os.path.join(self.state, "claude", os.listdir(self.state + "/claude")[0])
        os.remove(cmux + ".fail")
        env = dict(os.environ, **dict(self.env, HERMES_DELEGATE_CMUX=cmux))
        relaunch = self.cli("claude", "--launch-run", run_dir, env=env)
        self.assertEqual(relaunch.returncode, 0, relaunch.stderr)
        self.assertIn("launched=yes", relaunch.stdout)
        self.assertIn(f"rc={run_dir}/claude.rc", relaunch.stdout)
        log = Path(cmux + ".log").read_text(encoding="utf-8-sig")
        self.assertEqual(log.count("new-workspace"), 2)
        self.assertIn("--name claude-repo", log)
        self.assertIn("--focus false", log)
        Path(run_dir, "started_at").write_text("x\n", encoding="utf-8")
        again = self.cli("claude", "--launch-run", run_dir, env=env)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already started", again.stderr)
        foreign = self.cli("qa", "--launch-run", run_dir, env=env)
        self.assertEqual(foreign.returncode, 1)

    def test_never_started_names_launch_run(self):
        _, run_dir = self.prepared()
        with self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "5", "--start-grace", "1"], out=io.StringIO(), err=io.StringIO())
        self.assertEqual(caught.exception.code, 90)
        self.assertIn("hermes-delegate claude --launch-run " + run_dir, str(caught.exception))


class QaTests(WorkerTests):
    kind = "qa"

    def test_generates_fixed_astra_script(self):
        result, run_dir = self.prepared("--label", "silvia-ios")
        fields = self.fields(result.stdout)
        self.assertTrue(run_dir.startswith(self.state + "/qa/silvia-ios-"))
        self.assertEqual(fields["rc"], run_dir + "/qa.rc")
        self.assertIn(f"watcher: terminal(command=\"{common.HD_BIN} wait {run_dir} --timeout 1800\"",
                      result.stdout)
        self.assertIn("model=gpt-6-astra effort=high sandbox=danger-full-access", result.stderr)
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn(f"export ASDF_NODEJS_VERSION=22.19.0\nexport PATH={common.CODEX_NODE_BIN}:$PATH\n",
                      script)
        self.assertIn(f"{common.GTIMEOUT} -k 60 900 {common.CODEX_BIN} exec --ephemeral -m gpt-6-astra -c 'model_reasoning_effort=\"high\"' "
                      f"--sandbox danger-full-access -C {self.repo} --json -o {run_dir}/qa.final.md - < {run_dir}/spec.md > {run_dir}/qa.out"
                      , script)
        launch = json.loads(Path(run_dir, "launch.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(launch[1:4], ["new-workspace", "--name", "qa-silvia-ios"])
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((meta["kind"], meta["model"], meta["effort"], meta["sandbox"]),
                         ("qa", "gpt-6-astra", "high", "danger-full-access"))
        self.assertNotIn("class", meta)
        self.assertEqual(subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"], check=False, timeout=120).returncode, 0)

    def test_rejects_any_model_effort_or_sandbox_choice(self):
        for word in ("--model", "--effort", "--effort=xhigh", "--class", "--task-class", "--advisor", "--sandbox",
                     "--pinned-model", "--tier"):
            result = self.worker(word, "x")
            self.assertEqual(result.returncode, 2, word)
            self.assertEqual(result.stdout, "")
            self.assertIn("device QA is fixed at Codex gpt-6-astra/high", result.stderr)
        self.assertFalse(os.path.exists(self.state + "/qa"))

    def test_dirty_tree_refused(self):
        Path(self.repo, "untracked.txt").write_text("x\n", encoding="utf-8")
        result = self.worker("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)

    def test_inner_script_runs_codex_and_wait_summarizes(self):
        stub = self.stub("stub-codex", STUB_CODEX)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CODEX_BIN": stub})
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = Path(self.log).read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(argv[:8], ["exec", "--ephemeral", "-m", "gpt-6-astra", "-c",
                                    'model_reasoning_effort="high"', "--sandbox", "danger-full-access"])
        self.assertIn("ASDF_NODEJS_VERSION=22.19.0", Path(self.log + ".env").read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(self.log + ".stdin").read_text(encoding="utf-8-sig"), VALID_SPEC)
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("qa rc=0", text)
        self.assertIn("items=(agent_message:1,command_execution:1)", text)
        self.assertIn("last_line=QA: PASS ios", text)
        self.assertIn(f"worktree: HEAD {self.head[:12]} unchanged, 0 uncommitted entries", text)
        self.assertNotIn("WARNING", text)

    def test_summary_warns_without_commands(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "qa.out").write_text('{"type":"error","message":"stream disconnected"}\n', encoding="utf-8")
        summary = watch.summarize_codex(run_dir + "/qa.out", run_dir)
        self.assertIn("error=stream disconnected", summary)
        self.assertIn("result=missing", summary[0])
        self.assertTrue(any("ran no commands" in line for line in summary))


class DirectTokenTests(WorkerTests):
    def setUp(self):
        super().setUp()
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n", encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.env["HERMES_DELEGATE_CLAUDE_TOKEN_FILE"] = self.token

    def assert_no_token(self, run_dir):
        for path in Path(run_dir).rglob("*"):
            if path.is_file():
                self.assertNotIn(FAKE_TOKEN.encode(), path.read_bytes(), str(path))
                self.assertNotIn(b"sk-ant-oat", path.read_bytes(), str(path))

    def test_token_file_checks(self):
        os.chmod(self.token, 0o644)
        result = self.worker("--via", "direct", "--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be mode 0600", result.stderr)
        self.assertNotIn(FAKE_TOKEN, result.stderr)
        os.remove(self.token)
        result = self.worker("--via", "direct", "--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("token file is missing", result.stderr)
        self.assertEqual(self.worker("--no-launch").returncode, 0, "cmux runs do not need the token")

    def test_inner_checks_token_at_run_time(self):
        _, run_dir = self.prepared("--via", "direct")
        os.chmod(self.token, 0o640)
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 2)
        self.assertIn("must be mode 0600", Path(run_dir, "claude.err").read_text(encoding="utf-8-sig"))

    def test_token_reaches_only_claude_and_is_never_written(self):
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        result, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertIn("via=direct", result.stdout)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn("unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL", script)
        self.assertIn(f"( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {self.token})\"; exec ", script)
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, encoding="utf-8-sig", timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log, ANTHROPIC_API_KEY="x",
                                       ANTHROPIC_BASE_URL="http://x"), check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(self.log + ".toklen").read_text(encoding="utf-8-sig").strip(), str(len(FAKE_TOKEN)))
        self.assertEqual(Path(self.log + ".anthropic").read_text(encoding="utf-8-sig"), "api_key=unset base_url=unset\n")
        self.assertNotIn(FAKE_TOKEN, done.stdout + done.stderr)
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **self.env))
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertNotIn("WARNING: an OAuth token", waited.stdout)
        self.assert_no_token(run_dir)

    def test_wait_masks_a_leaked_token(self):
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, encoding="utf-8-sig", timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log, STUB_LEAK="1"), check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(FAKE_TOKEN, Path(run_dir, "claude.out").read_text(encoding="utf-8-sig"))
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **self.env))
        self.assertIn("WARNING: an OAuth token (sk-ant-oat...) appeared in claude.out", waited.stdout)
        self.assertNotIn(FAKE_TOKEN, waited.stdout)
        self.assertIn("leak sk-ant-oat[REDACTED]", Path(run_dir, "claude.out").read_text(encoding="utf-8-sig"))
        self.assertNotIn(FAKE_TOKEN.encode(), Path(run_dir, "claude.out").read_bytes())

    def test_direct_launch_detaches_and_wait_finishes(self):
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        started = time.monotonic()
        result = self.worker("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub,
                                                     "STUB_LOG": self.log})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 30)
        fields = self.fields(result.stdout)
        self.assertEqual((fields["via"], fields["launched"]), ("direct", "yes"))
        self.assertTrue(any(line.startswith("watcher: terminal(command=") for line in result.stdout.splitlines()))
        run_dir = fields["run_dir"]
        self.assertNotIn("cmux_workspace", json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig")))
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "60"], out=out, err=io.StringIO()), 0, out.getvalue())
        self.assertIn("status=finished", out.getvalue())
        self.assertEqual(Path(self.log + ".toklen").read_text(encoding="utf-8-sig").strip(), str(len(FAKE_TOKEN)))
        self.assertTrue(os.path.isdir(run_dir + "/lock"))
        self.assert_no_token(run_dir)
        relaunch = self.cli("claude", "--launch-run", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(relaunch.returncode, 1)
        self.assertIn("already started", relaunch.stderr)

    def test_direct_claude_review_uses_token_and_no_cmux(self):
        gate = self.stub("exec-gate", STUB_EXEC_GATE)
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n", encoding="utf-8")
        env = dict(os.environ, **dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate, HERMES_DELEGATE_CLAUDE_BIN=stub))
        result = self.cli("review", "--reviewer", "claude", "--via", "direct", "--item", "hd-direct", "--commit",
                          self.head[:12], "--prompt", prompt, "--workdir", self.repo, "--no-launch", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("via=direct", result.stdout)
        run_dir = self.fields(result.stdout)["run_dir"]
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn("export CLAUDE_CODE_OAUTH_TOKEN=", script)
        self.assertIn("unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL", script)
        self.assertIn("kill -TERM $child", script)
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, encoding="utf-8-sig", timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log), check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(self.log + ".toklen").read_text(encoding="utf-8-sig").strip(), str(len(FAKE_TOKEN)))
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        self.assertIn("verdict_line=APPROVED: no P0/P1", out.getvalue())
        self.assert_no_token(run_dir)

    def test_direct_term_reaches_claude_through_token_subshell(self):
        marker = "hd-slow-direct-" + os.path.basename(self.tmp)
        stub = self.stub(marker, STUB_SLOW_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        proc = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                env=dict(os.environ, STUB_LOG=self.log),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_for(self.log))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 7)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "7")
        self.assertTrue(wait_condition(lambda: no_process(marker)), "claude left running")
        self.assert_no_token(run_dir)

    def test_qa_rejects_via(self):
        result = self.worker("--via", "direct", kind="qa")
        self.assertEqual(result.returncode, 2)
        self.assertIn("in cmux (user pin)", result.stderr)

    def test_impl_direct_refusal_switch(self):
        original = common.CLAUDE_IMPL_DIRECT
        try:
            common.CLAUDE_IMPL_DIRECT = False
            with self.assertRaises(common.Fail) as caught:
                worker.cmd_worker("claude", ["--spec", self.spec, "--workdir", self.repo, "--class", "ambiguous",
                                         "--timeout", "900", "--via", "direct", "--no-launch"],
                              out=io.StringIO(), err=io.StringIO())
            self.assertEqual(caught.exception.code, 2)
        finally:
            common.CLAUDE_IMPL_DIRECT = original
