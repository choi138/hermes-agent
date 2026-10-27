import io
import json
import os
from pathlib import Path
import subprocess
import time
from unittest import mock
from delegate_test_support import Base, FAKE_TOKEN
from delegate_test_support import common
from delegate_test_support import status
from delegate_test_support import watch


class StatusTests(Base):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        patcher = mock.patch.dict(os.environ, {"HERMES_DELEGATE_STATE_ROOT": self.state})
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, path):
        out = io.StringIO()
        started = time.monotonic()
        with mock.patch.object(time, "sleep", side_effect=AssertionError("status slept")):
            code = status.cmd_status([path], out=out, err=io.StringIO())
        self.assertLess(time.monotonic() - started, 3)
        lines = out.getvalue().splitlines()
        fields = dict(line.split("=", 1) for line in lines)
        self.assertEqual(len(fields), len(lines))
        self.assertTrue(lines[-1].startswith("hint="))
        return code, fields

    def run_dir(self, kind, name="run", meta=True):
        path = os.path.join(self.state, kind, name)
        os.makedirs(path)
        if meta:
            Path(path, "meta.json").write_text(json.dumps({"kind": kind, "via": "direct"}), encoding="utf-8")
        return path

    def live(self, *words):
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", *words], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            child.kill()
            child.wait()
        self.addCleanup(stop)
        return child.pid

    def test_run_dir_states_for_each_kind(self):
        for kind in ("review", "claude", "qa"):
            path = self.run_dir(kind)
            code, fields = self.status(path)
            self.assertEqual((code, fields["kind"], fields["status"]), (0, kind, "not_started"))
            self.assertIn("--launch-run", fields["hint"])
            os.mkdir(path + "/lock")
            Path(path, "started_at").write_text("2026-10-26T10:00:00+0900\n", encoding="utf-8")
            Path(path, kind + ".out").write_text("{}\n", encoding="utf-8")
            Path(path, "stall_count").write_text("1\n", encoding="utf-8")
            pid = self.live(path + "/inner.zsh")
            Path(path, "inner.pid").write_text(f"{pid}\n", encoding="utf-8")
            code, fields = self.status(path)
            self.assertEqual((code, fields["status"], fields["pid"]), (0, "running", str(pid)))
            self.assertEqual(fields["started_at"], "2026-10-26T10:00:00+0900")
            self.assertEqual(fields["stall_count"], "1")
            self.assertLessEqual(int(fields["last_output_age_s"]), 5)
            self.assertEqual(fields["hint"], watch.HINT_RUNNING)
            Path(path, "ended_at").write_text("2026-10-26T10:05:00+0900\n", encoding="utf-8")
            Path(path, kind + ".rc").write_text("7\n", encoding="utf-8")
            code, fields = self.status(path)
            self.assertEqual((code, fields["status"], fields["rc"]), (0, "finished", "7"))
            self.assertEqual(fields["ended_at"], "2026-10-26T10:05:00+0900")
            self.assertNotIn("pid", fields)
            self.assertEqual(fields["hint"], watch.HINT_FINISHED)

    def test_run_dir_unknown_states(self):
        path = self.run_dir("claude")
        os.mkdir(path + "/lock")
        Path(path, "started_at").write_text("x\n", encoding="utf-8")
        Path(path, "inner.pid").write_text("999999\n", encoding="utf-8")
        code, fields = self.status(path)
        self.assertEqual((code, fields["status"]), (watch.EXIT_STATUS_UNKNOWN, "unknown"))
        Path(path, "claude.rc").write_text("oops\n", encoding="utf-8")
        self.assertEqual(self.status(path)[0], watch.EXIT_STATUS_UNKNOWN)
        other = self.run_dir("qa", "mismatch", meta=False)
        Path(other, "meta.json").write_text(json.dumps({"kind": "claude"}), encoding="utf-8")
        code, fields = self.status(other)
        self.assertEqual((code, fields["status"]), (watch.EXIT_STATUS_UNKNOWN, "unknown"))
        self.assertIn("does not match", fields["hint"])

    def test_lock_without_pid_is_running_only_briefly(self):
        path = self.run_dir("review")
        os.mkdir(path + "/lock")
        self.assertEqual(self.status(path)[1]["status"], "running")
        old = time.time() - 120
        os.utime(path + "/lock", (old, old))
        self.assertEqual(self.status(path)[1]["status"], "unknown")

    def test_review_dir_without_meta_defaults_to_review(self):
        path = self.run_dir("review", meta=False)
        Path(path, "review.rc").write_text("0\n", encoding="utf-8")
        code, fields = self.status(path)
        self.assertEqual((code, fields["kind"], fields["status"], fields["rc"]), (0, "review", "finished", "0"))

    def test_codex_before_and_after_runner_start(self):
        out_dir = os.path.join(self.state, "codex", "20261026T110652-abc123")
        os.makedirs(out_dir)
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["kind"], fields["status"]), (0, "codex", "not_started"))
        self.assertEqual(fields["hint"], watch.HINT_CODEX_NOT_STARTED)
        old = time.time() - 3600
        os.utime(out_dir, (old, old))
        self.assertIn("probably never happened", self.status(out_dir)[1]["hint"])
        task = os.path.join(out_dir, "codex-task-x1")
        os.mkdir(task)
        Path(task, "events.jsonl").write_text('{"type":"thread.started"}\n{"type":"turn.started"}\n', encoding="utf-8")
        Path(task, "stderr.log").write_text("", encoding="utf-8")
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"]), (watch.EXIT_STATUS_UNKNOWN, "unknown"))
        pid = self.live("/rel/scripts/run_codex_task.py", "--spec", "/s", "--output-dir", out_dir)
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"], fields["pid"]), (0, "running", str(pid)))
        self.assertEqual((fields["task_dir"], fields["events"], fields["runner_status"]), (task, "2", "running"))
        self.assertIn("started_at", fields)
        self.assertEqual(fields["hint"], watch.HINT_RUNNING)
        Path(task, "status.json").write_text(json.dumps({
            "status": "cli_failed", "exit_code": 3, "execution": {"status": "cli_failed", "cli_exit_code": 3}}), encoding="utf-8")
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"], fields["rc"], fields["runner_status"]),
                         (0, "finished", "3", "cli_failed"))
        self.assertIn("ended_at", fields)
        self.assertNotIn("pid", fields)
        Path(task, "status.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.status(out_dir)[0], watch.EXIT_STATUS_UNKNOWN)

    def test_other_output_dir_runner_is_not_matched(self):
        out_dir = os.path.join(self.state, "codex", "a")
        os.makedirs(out_dir + "/codex-task-1")
        self.live("/rel/scripts/run_codex_task.py", "--output-dir", out_dir + "b")
        self.assertEqual(self.status(out_dir)[1]["status"], "unknown")

    def test_trailing_slash_is_accepted(self):
        path = self.run_dir("qa")
        Path(path, "qa.rc").write_text("0\n", encoding="utf-8")
        code, fields = self.status(path + "/")
        self.assertEqual((code, fields["status"]), (0, "finished"))

    def test_invalid_paths(self):
        os.makedirs(os.path.join(self.state, "smoke", "x"))
        os.makedirs(os.path.join(self.state, "review", "deep", "x"))
        link = os.path.join(self.state, "review", "link")
        os.symlink(os.path.join(self.state, "smoke", "x"), link)
        for bad in ("relative/x", os.path.join(self.state, "review", "missing"), os.path.join(self.state, "smoke", "x"),
                    os.path.join(self.state, "review", "deep", "x"), os.path.join(self.state, "review"),
                    link, self.state + "/review/../smoke/x", self.tmp):
            with self.assertRaises(common.Fail) as ctx:
                status.cmd_status([bad], out=io.StringIO())
            self.assertEqual(ctx.exception.code, 2, bad)

    def test_cli_usage_redaction_and_no_token_read(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 2)
        path = self.run_dir("claude")
        os.mkdir(path + "/lock")
        Path(path, "started_at").write_text("leak " + FAKE_TOKEN + "\n", encoding="utf-8")
        token = os.path.join(self.tmp, "token")
        Path(token).write_text(FAKE_TOKEN + "\n", encoding="utf-8")
        os.chmod(token, 0)
        result = self.cli("status", path, env={"HERMES_DELEGATE_STATE_ROOT": self.state,
                                               "HERMES_DELEGATE_CLAUDE_TOKEN_FILE": token})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=running", result.stdout)
        self.assertIn("started_at=leak sk-ant-oat[REDACTED]", result.stdout)
        self.assertNotIn("Zz9", result.stdout + result.stderr)
        self.assertTrue(result.stdout.splitlines()[-1].startswith("hint="))


class HelpTests(Base):
    def test_help(self):
        result = self.cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("hermes-delegate <command>", result.stdout)
        self.assertIn("review", result.stdout)
        for text in ("90 when the run never started, 91 (status=no_rc) when no rc", "smoke   codex|claude",
                     "either --timeout passed or wait itself was signalled", "re-arm by running the printed watcher: line",
                     "The only way to stop a run", "[--owner-approval ID]",
                     "--reviewer claude|codex", "[--via cmux|direct]", "[--stall-minutes M]",
                     "92 (status=stalled) when <kind>.out has not grown for M minutes",
                     "An rc always wins over a stall", "a watcher_no_stall: line\n",
                     "From the 2nd stall of a run only watcher_no_stall: is printed",
                     "status  PATH", "check at most once with hermes-delegate status PATH; never sleep+peek"):
            self.assertIn(text, result.stdout)
        self.assertNotIn("124 when", result.stdout)
