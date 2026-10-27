from delegate_test_support import kill_process_group
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
from delegate_test_support import Base, FAKE_TOKEN, SCRIPT, STUB_CLAUDE, STUB_CMUX, STUB_EXEC_GATE, STUB_SLOW_CLAUDE, STUB_SLOW_GATE, STUB_SMOKE, WorkerTests, no_process, wait_for
from delegate_test_support import wait_condition
from delegate_test_support import common
from delegate_test_support import smoke
from delegate_test_support import watch


class SlowClaudeFixtureTests(Base):
    def test_delay_exit_does_not_finish_worker(self):
        bin_dir = Path(self.tmp, "bin")
        bin_dir.mkdir()
        delay = bin_dir / "sleep"
        delay.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
        delay.chmod(0o755)
        stub = Path(self.tmp, "slow-claude")
        stub.write_text(STUB_SLOW_CLAUDE, encoding="utf-8")
        stub.chmod(0o755)
        log = Path(self.tmp, "worker.log")
        result = subprocess.run([str(common.GTIMEOUT), "--preserve-status", "-k", "5", "2", str(stub)],
                                env=dict(os.environ, STUB_LOG=str(log), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"]),
                                capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(log.read_text(encoding="utf-8-sig"), "started\n")
        self.assertEqual(Path(str(log) + ".term").read_text(encoding="utf-8-sig"), "got-term\n")


class SignalAndLockTests(WorkerTests):
    def setUp(self):
        super().setUp()
        self.marker = "hd-slow-" + os.path.basename(self.tmp)

    def slow_stub(self, body):
        return self.stub(self.marker, body)

    def start_inner(self, run_dir):
        return subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                env=dict(os.environ, STUB_LOG=self.log),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_worker_term_propagates_to_gtimeout_group_and_records_real_rc(self):
        stub = self.slow_stub(STUB_SLOW_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn("kill -TERM -- -$child", script)
        self.assertIn("&\nchild=$!\nwait $child\n", script)
        proc = self.start_inner(run_dir)
        self.assertTrue(wait_for(self.log))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 7)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "7")
        self.assertIn("interrupted by SIGTERM; sent SIGTERM to the worker group, which exited 7",
                      Path(run_dir, "claude.err").read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(self.log + ".term").read_text(encoding="utf-8-sig"), "got-term\n")
        self.assertTrue(wait_condition(lambda: no_process(self.marker)), "worker left running")

    def test_review_term_goes_to_review_gate_pid(self):
        gate = self.slow_stub(STUB_SLOW_GATE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n", encoding="utf-8")
        result = self.cli("review", "--reviewer", "claude", "--item", "hd-sig", "--commit", self.head[:12],
                          "--prompt", prompt, "--workdir", self.repo, "--no-launch",
                          env=dict(os.environ, **dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate)))
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn("kill -TERM $child", script)
        self.assertNotIn("kill -TERM -- -$child", script)
        proc = self.start_inner(run_dir)
        self.assertTrue(wait_for(self.log))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 5)
        self.assertEqual(Path(run_dir, "review.rc").read_text(encoding="utf-8-sig").strip(), "5")
        self.assertIn("sent SIGTERM to the review-gate run, which exited 5", Path(run_dir, "review.err").read_text(encoding="utf-8-sig"))
        self.assertEqual(Path(self.log + ".term").read_text(encoding="utf-8-sig"), "got-term\n")

    def test_term_before_target_start_uses_signal_code(self):
        _, run_dir = self.prepared()
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        hold = f"sleep 30 &\nprint -r -- $! > {run_dir}/sleep.pid\nwait $!\ndate '+%Y"
        Path(run_dir, "inner.zsh").write_text(script.replace("date '+%Y", hold, 1), encoding="utf-8")
        proc = self.start_inner(run_dir)
        try:
            self.assertTrue(wait_for(run_dir + "/sleep.pid"))
            os.kill(proc.pid, signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=30), 143)
        finally:
            if os.path.exists(run_dir + "/sleep.pid"):
                try:
                    os.kill(int(Path(run_dir, "sleep.pid").read_text(encoding="utf-8-sig")), getattr(signal, "SIGKILL", signal.SIGTERM))
                except (ProcessLookupError, ValueError):
                    pass
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "143")
        self.assertFalse(os.path.exists(run_dir + "/started_at"))

    def test_lock_blocks_second_launch_without_rc(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        os.mkdir(run_dir + "/lock")
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, common.EXIT_LOCKED)
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"))
        self.assertFalse(os.path.exists(run_dir + "/started_at"))
        self.assertIn("already launched (lock exists)", Path(run_dir, "claude.err").read_text(encoding="utf-8-sig"))
        self.assertFalse(os.path.exists(self.log), "the worker must not run")
        cmux = self.stub("cmux", STUB_CMUX)
        again = self.cli("claude", "--launch-run", run_dir,
                         env=dict(os.environ, **dict(self.env, HERMES_DELEGATE_CMUX=cmux)))
        self.assertEqual(again.returncode, 1)
        self.assertIn("already started (lock", again.stderr)
        self.assertFalse(os.path.exists(cmux + ".log"))

    def test_first_run_takes_lock_and_second_is_refused(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertEqual(self.run_inner(run_dir).returncode, 0)
        self.assertTrue(os.path.isdir(run_dir + "/lock"))
        Path(run_dir, "claude.rc").write_text("0\n", encoding="utf-8")
        second = self.run_inner(run_dir)
        self.assertEqual(second.returncode, common.EXIT_LOCKED)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig"), "0\n")


class CmuxCloseTests(WorkerTests):
    def run_with_rc(self, rc):
        cmux = self.stub("cmux", STUB_CMUX)
        stub = self.stub("stub-claude", STUB_CLAUDE)
        env = {"HERMES_DELEGATE_CMUX": cmux, "HERMES_DELEGATE_CLAUDE_BIN": stub}
        result = self.worker(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("via=cmux", result.stdout)
        run_dir = self.fields(result.stdout)["run_dir"]
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))["cmux_workspace"], "workspace:9")
        Path(run_dir, "started_at").write_text("x\n", encoding="utf-8")
        Path(run_dir, "claude.rc").write_text(f"{rc}\n", encoding="utf-8")
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **dict(self.env, **env)))
        return waited, Path(cmux + ".log").read_text(encoding="utf-8-sig")

    def test_success_closes_workspace(self):
        waited, log = self.run_with_rc(0)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("close-workspace --workspace workspace:9", log)
        self.assertIn("cmux_workspace=workspace:9 closed", waited.stdout)
        self.assertIn("status=finished", waited.stdout)

    def test_failure_keeps_workspace(self):
        waited, log = self.run_with_rc(3)
        self.assertEqual(waited.returncode, 3)
        self.assertNotIn("close-workspace", log)


class CancelTests(WorkerTests):
    def setUp(self):
        super().setUp()
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n", encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.env["HERMES_DELEGATE_CLAUDE_TOKEN_FILE"] = self.token
        self.marker = "hd-cancel-" + os.path.basename(self.tmp)

    def running_direct(self):
        stub = self.stub(self.marker, STUB_SLOW_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        inner = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                 env=dict(os.environ, STUB_LOG=self.log),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, inner)
        self.assertTrue(wait_for(self.log))
        return run_dir, inner

    def reap(self, proc):
        if proc.poll() is None:
            try:
                kill_process_group(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass
        proc.wait(timeout=30)

    def watch(self, argv, name="watcher.out"):
        watch_out = os.path.join(self.tmp, name)
        with open(watch_out, "w", encoding="utf-8") as sink:
            watcher = subprocess.Popen(argv, start_new_session=True, stdout=sink, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, **self.env))
        self.addCleanup(self.reap, watcher)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "status=started" not in Path(watch_out).read_text(encoding="utf-8-sig"):
            time.sleep(0.05)
        return watcher, watch_out

    def assert_still_running(self, run_dir):
        receipt = Path(self.log + ".alive")
        receipt.unlink(missing_ok=True)
        pid = int(Path(self.log + ".pid").read_text(encoding="utf-8-sig"))
        os.kill(pid, getattr(signal, "SIGUSR1", signal.SIGTERM))
        self.assertTrue(wait_for(receipt), "worker must answer after the watcher has exited")
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"), "a watcher signal must not finish the run")
        self.assertFalse(no_process(self.marker), "a watcher signal must not stop claude")

    def test_watcher_sigterm_leaves_direct_run_running(self):
        run_dir, inner = self.running_direct()
        watcher, watch_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"])
        kill_process_group(watcher.pid, signal.SIGTERM)
        watcher.wait(timeout=30)
        text = Path(watch_out).read_text(encoding="utf-8-sig")
        self.assertEqual(watcher.returncode, common.EXIT_NO_RC, text)
        self.assertIn("status=no_rc", text)
        self.assertIn("wait stopped by SIGTERM; the run keeps going", text)
        self.assertIn("hermes-delegate cancel " + run_dir, text)
        self.assertNotIn("cancel: sent SIGTERM", text)
        self.assertIsNone(inner.poll())
        self.assert_still_running(run_dir)
        again = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(again.returncode, 7, again.stdout + again.stderr)

    def test_gateway_style_kill_after_real_detached_launch_leaves_run(self):
        stub = self.stub(self.marker, STUB_SLOW_CLAUDE)
        result = self.worker("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub, "STUB_LOG": self.log})
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        self.assertTrue(wait_for(self.log, 60))
        watcher, watch_out = self.watch(["/bin/bash", "-c", shlex.join([sys.executable, SCRIPT, "wait", run_dir])])
        kill_process_group(watcher.pid, signal.SIGTERM)
        watcher.wait(timeout=30)
        self.assertIn("status=no_rc", Path(watch_out).read_text(encoding="utf-8-sig"))
        self.assert_still_running(run_dir)
        stopped = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(stopped.returncode, 7, stopped.stdout + stopped.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not no_process(self.marker):
            time.sleep(0.1)
        self.assertTrue(no_process(self.marker), "claude left running after cancel")

    def test_interrupted_second_wait_is_not_destructive(self):
        run_dir, _inner = self.running_direct()
        first, first_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"])
        for sig in (getattr(signal, "SIGHUP", signal.SIGTERM), signal.SIGINT):
            second, second_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"],
                                            name="second.out")
            kill_process_group(second.pid, sig)
            second.wait(timeout=30)
            self.assertEqual(second.returncode, common.EXIT_NO_RC, Path(second_out).read_text(encoding="utf-8-sig"))
            self.assertIn("wait stopped by " + signal.Signals(sig).name, Path(second_out).read_text(encoding="utf-8-sig"))
            self.assert_still_running(run_dir)
        timed_out = self.cli("wait", run_dir, "--timeout", "1", env=dict(os.environ, **self.env))
        self.assertEqual(timed_out.returncode, common.EXIT_NO_RC)
        self.assertIn("it may still be running", timed_out.stderr)
        self.assertIn("re-watch with hermes-delegate wait " + run_dir, timed_out.stderr)
        self.assert_still_running(run_dir)
        self.assertIsNone(first.poll(), "the persisted watcher must keep watching")
        self.assertEqual(self.cli("cancel", run_dir, env=dict(os.environ, **self.env)).returncode, 7)
        first.wait(timeout=30)
        self.assertEqual(first.returncode, 7, Path(first_out).read_text(encoding="utf-8-sig"))
        self.assertIn("status=finished", Path(first_out).read_text(encoding="utf-8-sig"))

    def test_cancel_command_stops_run(self):
        run_dir, inner = self.running_direct()
        result = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("status=finished", result.stdout)
        self.assertIn("claude rc=7", result.stdout)
        self.assertEqual(inner.wait(timeout=30), 7)
        self.assertTrue(wait_condition(lambda: no_process(self.marker)), "claude left running after cancel")
        again = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(again.returncode, 7)
        self.assertIn("already finished", again.stdout)

    def test_cmux_run_survives_watcher_signals_and_cancel_stops_it(self):
        stub = self.stub(self.marker, STUB_SLOW_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))["via"], "cmux")
        # Stands in for the cmux workspace shell: its own session, as cmux gives each workspace.
        inner = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                 env=dict(os.environ, STUB_LOG=self.log),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, inner)
        self.assertTrue(wait_for(self.log))
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
            watcher, watch_out = self.watch(["/bin/bash", "-c", shlex.join([sys.executable, SCRIPT, "wait",
                                                                            run_dir])])
            kill_process_group(watcher.pid, sig)
            watcher.wait(timeout=30)
            self.assertIn("the run keeps going", Path(watch_out).read_text(encoding="utf-8-sig"))
            self.assert_still_running(run_dir)
        stopped = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(stopped.returncode, 7, stopped.stdout + stopped.stderr)
        self.assertEqual(inner.wait(timeout=30), 7)
        self.assertTrue(wait_condition(lambda: no_process(self.marker)), "claude left running after cancel")

    def test_cancel_refuses_a_stale_or_foreign_pid(self):
        _, run_dir = self.prepared("--via", "direct")
        os.mkdir(run_dir + "/lock")
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self.reap, sleeper)
        Path(run_dir, "inner.pid").write_text(f"{sleeper.pid}\n", encoding="utf-8")
        result = self.cli("cancel", run_dir, "--grace", "1", env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, common.EXIT_NO_RC, result.stdout + result.stderr)
        self.assertIn("no live inner.zsh for this run", result.stdout)
        self.assertIn("status=no_rc", result.stdout)
        self.assertIsNone(sleeper.poll(), "a foreign pid must not be signalled")

    def test_cancel_before_start_fences_the_launch(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        result = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertIn("had not started; took the lock", result.stdout)
        late = self.run_inner(run_dir)
        self.assertEqual(late.returncode, common.EXIT_LOCKED)
        self.assertFalse(os.path.exists(self.log), "the worker must not run after cancel")
        self.assertEqual(Path(run_dir, "claude.rc").read_text(encoding="utf-8-sig").strip(), "143")

    def test_cancel_before_start_of_review_names_the_open_ticket(self):
        gate = self.stub("exec-gate", STUB_EXEC_GATE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n", encoding="utf-8")
        env = dict(os.environ, **dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate))
        result = self.cli("review", "--reviewer", "claude", "--via", "direct", "--item", "hd-cancel", "--commit",
                          self.head[:12], "--prompt", prompt, "--workdir", self.repo, "--no-launch", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        cancelled = self.cli("cancel", run_dir, env=env)
        self.assertEqual(cancelled.returncode, 143)
        self.assertIn("ticket is still open and unused; close it as UNREVIEWED", cancelled.stdout)

    def test_wait_restores_handlers(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertEqual(self.run_inner(run_dir).returncode, 0)
        before = {sig: signal.getsignal(sig) for sig in watch.WATCHER_SIGNALS}
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "5"], out=io.StringIO(), err=io.StringIO()), 0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in watch.WATCHER_SIGNALS}, before)


class SmokeTests(Base):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "stub-cli")
        Path(self.stub).write_text(STUB_SMOKE, encoding="utf-8")
        os.chmod(self.stub, 0o755)
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n", encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.env = {"HERMES_DELEGATE_STATE_ROOT": os.path.join(self.tmp, "state"),
                    "HERMES_DELEGATE_CODEX_BIN": self.stub, "HERMES_DELEGATE_CLAUDE_BIN": self.stub,
                    "HERMES_DELEGATE_CLAUDE_TOKEN_FILE": self.token, "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def test_codex_and_claude_direct_ok(self):
        for target in ("codex", "claude"):
            result = self.cli("smoke", target, "--via", "direct", env=self.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, f"ok {target} via=direct\n")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "state", "smoke")), [])

    def test_failure_prints_reason(self):
        result = self.cli("smoke", "codex", env=dict(self.env, STUB_SMOKE_FAIL="1"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("fail codex via=direct: rc=3", result.stdout)
        self.assertIn("auth failed", result.stdout)

    def test_claude_default_is_cmux_and_needs_it(self):
        result = self.cli("smoke", "claude", env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cmux is not executable", result.stderr)

    def test_claude_direct_needs_token_file(self):
        os.chmod(self.token, 0o644)
        result = self.cli("smoke", "claude", "--via", "direct", env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be mode 0600", result.stderr)

    def test_timeout_kills_the_smoke_group(self):
        marker = "hd-smoke-slow-" + os.path.basename(self.tmp)
        slow = os.path.join(self.tmp, marker)
        Path(slow).write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        os.chmod(slow, 0o755)
        started = time.monotonic()
        result = self.cli("smoke", "codex", "--timeout", "10", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN=slow))
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result within 10s", result.stdout)
        self.assertTrue(wait_condition(lambda: no_process(marker)))

    def test_smoke_script_shape(self):
        script = smoke.smoke_script("codex", "/private/tmp/x", "direct")
        self.assertIn("exec --ephemeral -s read-only -m gpt-6.1-sol", script)
        self.assertIn("export ASDF_NODEJS_VERSION=22.19.0", script)
        self.assertNotIn("review-gate", script)
