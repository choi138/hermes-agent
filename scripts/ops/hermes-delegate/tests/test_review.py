from delegate_test_support import kill_process_group
import io
import json
import os
import re
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from delegate_test_support import REAL_GATE, ReviewBase, STUB_EXEC_GATE, STUB_GATE, STUB_REVIEW_CODEX, git
from delegate_test_support import common
from delegate_test_support import review
from delegate_test_support import runtime
from delegate_test_support import watch
from delegate_test_support import worker


class ReviewTests(ReviewBase):
    def test_generates_gated_script_without_launching(self):
        self.open_item()
        result = self.review()
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.fields(result.stdout)
        run_dir, ticket = fields["run_dir"], fields["ticket"]
        self.assertEqual(fields["rc"], run_dir + "/review.rc")
        self.assertEqual(fields["launched"], "no")
        self.assertTrue(run_dir.startswith(self.state + "/review/hd-test-item-"))
        self.assertEqual(os.stat(run_dir).st_mode & 0o777, 0o700)
        watcher = next(line for line in result.stdout.splitlines() if line.startswith("watcher:"))
        self.assertIn(f"{common.HD_BIN} wait {run_dir} --timeout", watcher)
        self.assertIn("notify_on_complete=true", watcher)
        self.assertIn("continue_on_complete=true, persist_on_release=true", watcher)
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn(f"{self.gate} run --ticket {ticket} --timeout 2700 -- ", script)
        self.assertIn("-p --model opus --effort high --tools Read,Glob,Grep --allowedTools Read,Glob,Grep "
                      "--permission-mode dontAsk --strict-mcp-config", script)
        self.assertIn("export REVIEW_GATE_DB=" + self.db, script)
        self.assertNotIn(" start ", script)
        self.assertNotIn(" close ", script)
        self.assertNotIn("owner-approval", script)
        prompt = Path(run_dir, "prompt.md").read_text(encoding="utf-8-sig")
        self.assertTrue(prompt.startswith("Review the diff.\n"))
        self.assertIn(run_dir + "/diff.patch", prompt)
        self.assertIn(run_dir + "/log.txt", prompt)
        self.assertTrue(prompt.endswith("\nAPPROVED: no P0/P1\n"))
        launch = json.loads(Path(run_dir, "launch.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(launch[1:2], ["new-workspace"])
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        self.assertIn(shlex.quote(run_dir + "/inner.zsh"), launch[launch.index("--command") + 1])
        syntax = subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"], capture_output=True, check=False, timeout=120)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT status, reviewer, reviewed_commit FROM tickets WHERE id=?",
                             (ticket,)).fetchone()
        self.assertEqual(row, ("open", "claude", self.head[:12]))

    def test_refusal_is_surfaced_verbatim(self):
        self.open_item()
        self.assertEqual(self.review().returncode, 0)
        result = self.review()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("review-gate: Previous tickets must be fully closed;"),
                        result.stderr)
        self.assertEqual(result.stderr.count("\n"), 1, "the gate's one-line refusal must pass through as is")
        self.assertEqual(len(os.listdir(self.state + "/review")), 1, "refused run dir must be removed")

    def test_missing_item_is_not_auto_opened(self):
        result = self.review()
        self.assertEqual(result.returncode, 4)
        self.assertEqual(result.stderr, "review-gate: items not found: hd-test-item\n")
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)

    def test_head_mismatch_rejected(self):
        Path(self.repo, "src/a.py").write_text("print(2)\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "two")
        result = self.review()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not --commit", result.stderr)

    def test_inner_script_runs_gate_and_writes_rc(self):
        stub = os.path.join(self.tmp, "stub-gate")
        Path(stub).write_text(STUB_GATE, encoding="utf-8")
        os.chmod(stub, 0o755)
        env = dict(self.env, HERMES_DELEGATE_REVIEW_GATE=stub)
        result = self.review(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        log = os.path.join(self.tmp, "stub.log")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30, check=False, encoding="utf-8-sig")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(run_dir, "review.rc").read_text(encoding="utf-8-sig").strip(), "0")
        self.assertEqual(Path(run_dir, "review.out").read_text(encoding="utf-8-sig"), "review-body\n")
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))["commit"], self.head)
        argv = Path(log).read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(argv[:5], ["--ticket", "0123456789abcdef0123456789abcdef", "--timeout", "2700", "--"])
        self.assertEqual(argv[5], str(common.CLAUDE_BIN))
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertTrue(Path(log + ".stdin").read_text(encoding="utf-8-sig").startswith("Review the diff.\n"))
        waited = self.cli("wait", run_dir, "--timeout", "5")
        self.assertEqual(waited.returncode, 0)
        self.assertIn("review rc=0", waited.stdout)

    def test_invalid_label_rejected(self):
        result = self.review("--label", "bad label")
        self.assertEqual(result.returncode, 2)

    def test_dirty_tree_refused_without_ticket(self):
        self.open_item()
        Path(self.repo, "src/a.py").write_text("dirty\n", encoding="utf-8")
        result = self.review()
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)
        Path(self.repo, "src/a.py").write_text("print(1)\n", encoding="utf-8")
        Path(self.repo, "new.py").write_text("x\n", encoding="utf-8")
        self.assertIn("untracked", self.review().stderr)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_check_is_read_only(self):
        self.open_item()
        env = dict(self.env, HERMES_DELEGATE_CMUX=self.stub_cmux())
        result = self.run_args("--check", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check=ok", result.stdout)
        self.assertIn("no ticket issued", result.stdout)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)
        self.assertFalse(os.path.exists(self.state + "/review") and os.listdir(self.state + "/review"))
        self.assertEqual(self.review().returncode, 0)
        result = self.run_args("--check", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Previous tickets must be fully closed", result.stderr)

    def test_check_reports_missing_item(self):
        env = dict(self.env, HERMES_DELEGATE_CMUX=self.stub_cmux())
        result = self.run_args("--check", env=env)
        self.assertEqual(result.returncode, 4)
        self.assertIn("items not found", result.stderr)

    def test_failed_launch_names_ticket_and_launch_run_recovers(self):
        self.open_item()
        cmux = self.stub_cmux(fail=True)
        env = dict(self.env, HERMES_DELEGATE_CMUX=cmux)
        result = self.run_args(env=env)
        self.assertEqual(result.returncode, 1)
        with sqlite3.connect(self.db) as db:
            ticket = db.execute("SELECT id, status FROM tickets").fetchone()
        self.assertEqual(ticket[1], "open")
        self.assertIn(ticket[0], result.stderr)
        self.assertIn("--launch-run", result.stderr)
        run_dir = os.path.join(self.state, "review", os.listdir(self.state + "/review")[0])
        os.remove(cmux + ".fail")
        relaunch = self.cli("review", "--launch-run", run_dir, env=env)
        self.assertEqual(relaunch.returncode, 0, relaunch.stderr)
        self.assertIn("launched=yes", relaunch.stdout)
        self.assertIn("ticket=" + ticket[0], relaunch.stdout)
        log = Path(cmux + ".log").read_text(encoding="utf-8-sig")
        self.assertEqual(log.count("new-workspace"), 2)
        self.assertIn("--focus false", log)
        Path(run_dir, "started_at").write_text("x\n", encoding="utf-8")
        again = self.cli("review", "--launch-run", run_dir, env=env)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already started", again.stderr)

    def test_launch_run_rejects_foreign_dir(self):
        other = os.path.join(self.tmp, "other")
        os.mkdir(other)
        result = self.cli("review", "--launch-run", other, env=self.env)
        self.assertEqual(result.returncode, 1)

    def test_inner_script_refuses_changed_tree(self):
        run_dir, log = self.stub_run()
        Path(self.repo, "src/a.py").write_text("print(3)\n", encoding="utf-8")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30, check=False, encoding="utf-8-sig")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(Path(run_dir, "review.rc").read_text(encoding="utf-8-sig").strip(), "2")
        self.assertIn("became dirty", Path(run_dir, "review.err").read_text(encoding="utf-8-sig"))
        self.assertFalse(os.path.exists(log), "the gate must not run")

    def test_inner_script_writes_rc_on_hangup(self):
        run_dir, log = self.stub_run()
        proc = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                env=dict(os.environ, STUB_LOG=log, STUB_SLEEP="30"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not os.path.exists(log) and time.monotonic() < deadline:
            time.sleep(0.1)
        kill_process_group(proc.pid, getattr(signal, "SIGHUP", signal.SIGTERM))
        self.assertEqual(proc.wait(timeout=20), 129)
        self.assertEqual(Path(run_dir, "review.rc").read_text(encoding="utf-8-sig").strip(), "129")
        self.assertIn("SIGHUP", Path(run_dir, "review.err").read_text(encoding="utf-8-sig"))


class ReviewInputsTests(ReviewBase):
    def test_diff_and_log_files_without_base(self):
        self.open_item()
        result = self.review()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        show = subprocess.run(["git", "-C", self.repo, "show", self.head], capture_output=True, check=False, timeout=120).stdout
        self.assertEqual(Path(run_dir, "diff.patch").read_bytes(), show)
        self.assertIn(b"init", Path(run_dir, "log.txt").read_bytes())
        prompt = Path(run_dir, "prompt.md").read_text(encoding="utf-8-sig")
        self.assertIn(f"(git show {self.head[:12]})", prompt)
        for rule in ("Report only P0/P1", "file:line and a concrete trigger", "Round 2 or later",
                     "regressions introduced by those fixes", "defects in the new diff"):
            self.assertIn(rule, prompt)
        self.assertEqual(prompt.splitlines()[-1], "APPROVED: no P0/P1")

    def test_diff_with_base(self):
        base = self.head
        Path(self.repo, "src/a.py").write_text("print(2)\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "two")
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True, timeout=120, encoding="utf-8-sig").stdout.strip()
        self.open_item()
        result = self.review("--base", base[:12])
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        diff = subprocess.run(["git", "-C", self.repo, "diff", "--binary", base, self.head],
                              capture_output=True, check=False, timeout=120).stdout
        self.assertEqual(Path(run_dir, "diff.patch").read_bytes(), diff)
        log = Path(run_dir, "log.txt").read_text(encoding="utf-8-sig")
        self.assertIn("two", log)
        self.assertNotIn("    init", log)
        self.assertIn(f"git diff --binary {base[:12]} {self.head[:12]}",
                      Path(run_dir, "prompt.md").read_text(encoding="utf-8-sig"))

    def test_reviewer_tools_unchanged_but_run_dir_readable(self):
        self.open_item()
        run_dir = self.fields(self.review().stdout)["run_dir"]
        line = next(l for l in Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig").splitlines() if " run --ticket " in l)
        argv = shlex.split(line.split(" -- ", 1)[1].rsplit(" < ", 1)[0])
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--add-dir") + 1], run_dir)
        self.assertNotIn("Bash", argv)

    def test_guidance_says_koharu_closes_after_reproducing(self):
        self.open_item()
        result = self.review()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reproduce each finding in review.result.md yourself, then record the verdict with "
                      "review-gate close (PASS: --blockers 0 or omitted; BLOCK: --blockers N>=1", result.stderr)
        self.assertIn("The user only gives --owner-approval for round 3+", result.stderr)
        self.assertNotIn("to the user, who records it", result.stderr)
        self.assertIn("koharu reproduces the findings", self.cli("--help").stdout)

    def test_check_refusal_tells_koharu_to_close_pending_verdicts(self):
        self.open_item()
        self.assertEqual(self.review().returncode, 0)
        result = self.run_args("--check", env=dict(self.env, HERMES_DELEGATE_CMUX=self.stub_cmux()))
        self.assertEqual(result.returncode, 2)
        self.assertIn("reproduce each pending verdict yourself, record it with review-gate close, then retry",
                      result.stderr)
        self.assertNotIn("the owner must record", result.stderr)

    def test_default_via_per_reviewer(self):
        self.open_item()
        result = self.review()
        self.assertIn("via=cmux", result.stdout)
        meta = json.loads(Path(self.fields(result.stdout)["run_dir"], "meta.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((meta["reviewer"], meta["via"]), ("claude", "cmux"))


class OwnerApprovalTests(ReviewBase):
    approval = "1300000000000000001"

    def close(self, ticket, *verdict):
        subprocess.run(["/usr/bin/python3", REAL_GATE, "close", "--ticket", ticket, *verdict],
                       env=dict(os.environ, REVIEW_GATE_DB=self.db), check=True, capture_output=True, timeout=120)

    def capped(self):
        self.open_item()
        for _ in range(2):
            result = self.review()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.close(self.fields(result.stdout)["ticket"], "--verdict", "BLOCK", "--blockers", "1")

    def check(self, *extra):
        return self.run_args("--check", *extra, env=dict(self.env, HERMES_DELEGATE_CMUX=self.stub_cmux()))

    def test_round_three_needs_and_passes_the_approval(self):
        self.capped()
        refused = self.check()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("stopped at round 2", refused.stderr)
        self.assertIn("hermes-delegate review --owner-approval ID", refused.stderr)
        self.assertNotEqual(self.review().returncode, 0)
        self.assertEqual(self.check("--owner-approval", self.approval).returncode, 0)
        started = self.review("--owner-approval", self.approval)
        self.assertEqual(started.returncode, 0, started.stderr)
        fields = self.fields(started.stdout)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT round, owner_approval FROM tickets WHERE id=?", (fields["ticket"],)).fetchone()
        self.assertEqual(row, (3, self.approval))
        meta = json.loads(Path(fields["run_dir"], "meta.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(meta["owner_approval"], self.approval)
        self.assertNotIn("owner-approval", Path(fields["run_dir"], "inner.zsh").read_text(encoding="utf-8-sig"))
        self.close(fields["ticket"], "--verdict", "BLOCK", "--blockers", "1")
        reused = self.check("--owner-approval", self.approval)
        self.assertEqual(reused.returncode, 2)
        self.assertIn(f"Owner approval {self.approval} was already used", reused.stderr)
        self.assertEqual(self.check("--owner-approval", "1300000000000000002").returncode, 0)

    def test_approval_id_format_is_checked_before_any_ticket(self):
        self.open_item()
        for bad in ("123", "abc1300000000000000", "1300000000000000001 "):
            result = self.review("--owner-approval", bad)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--owner-approval must be a Discord message ID", result.stderr)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    def test_start_refusal_mirror(self):
        item = {"id": "x-item", "status": "cap-reached"}
        closed = {"status": "closed", "verdict": "BLOCK", "owner_approval": None}
        self.assertEqual(runtime.start_refusal({"item": item, "tickets": [dict(closed, status="ran", verdict=None)],
                                           "rounds_used": 1}, self.approval), runtime.PENDING_REFUSAL)
        state = {"item": item, "tickets": [closed, closed], "rounds_used": 2}
        self.assertIn("--owner-approval", runtime.start_refusal(state))
        self.assertIsNone(runtime.start_refusal(state, self.approval))
        used = dict(state, tickets=[closed, dict(closed, owner_approval=self.approval)], rounds_used=3)
        self.assertIn("already used", runtime.start_refusal(used, self.approval))

    def test_failed_review_routes_to_fallback_in_the_mirror(self):
        item = {"id": "x-item", "status": "open"}
        failed = {"round": 1, "status": "closed", "verdict": "UNREVIEWED", "fallback": 0,
                  "closed_at": "2026-10-26T10:00:00.000002+00:00", "owner_approval": None}
        passed = dict(failed, verdict="PASS", closed_at="2026-10-26T10:00:00.000001+00:00")
        state = {"item": item, "tickets": [passed, failed], "rounds_used": 1}
        self.assertIn("hermes-delegate review --fallback", runtime.start_refusal(state))
        self.assertIsNone(runtime.start_refusal(state, self.approval))
        self.assertIsNone(runtime.start_refusal(state, fallback=True))
        self.assertIn("different reviewer", runtime.start_refusal(
            dict(state, tickets=[passed, dict(failed, reviewer="claude")]), fallback=True, reviewer="claude"))
        # PASS closed last: review-gate refuses the fallback, so the new round stays open.
        pass_last = dict(state, tickets=[failed, dict(passed, closed_at="2026-10-26T10:00:01+00:00")])
        self.assertIsNone(runtime.start_refusal(pass_last))
        self.assertIn("latest closed ticket", runtime.start_refusal(pass_last, fallback=True))
        blocked = dict(state, tickets=[dict(passed, verdict="BLOCK"), failed])
        self.assertIsNone(runtime.start_refusal(blocked))
        used = dict(state, tickets=[failed, dict(failed, fallback=1, closed_at="2026-10-26T10:00:02+00:00")])
        self.assertIn("already used", runtime.start_refusal(used, fallback=True))
        self.assertIn("latest closed ticket", runtime.start_refusal({"item": item, "tickets": [], "rounds_used": 0},
                                                               fallback=True))

    def test_fallback_flag_retries_in_the_same_round(self):
        self.open_item()
        first = self.review()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.close(self.fields(first.stdout)["ticket"], "--verdict", "UNREVIEWED", "--reason", "stalled")
        self.assertEqual(self.check("--fallback").returncode, 2)  # same reviewer CLI (claude) is refused later
        started = self.cli("review", "--reviewer", "codex", "--item", "hd-test-item", "--commit", self.head[:12],
                           "--prompt", self.prompt, "--workdir", self.repo, "--no-launch", "--fallback",
                           env=self.env)
        self.assertEqual(started.returncode, 0, started.stderr)
        fields = self.fields(started.stdout)
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT round, fallback FROM tickets WHERE id=?", (fields["ticket"],)).fetchone()
        self.assertEqual(row, (1, 1))
        self.assertTrue(json.loads(Path(fields["run_dir"], "meta.json").read_text(encoding="utf-8-sig"))["fallback"])


class DefaultViaTests(unittest.TestCase):
    def test_claude_default_is_stated_once_in_code(self):
        worker_default = worker.worker_parser("claude").parse_args(
            ["--spec", "s", "--workdir", "w", "--class", "ambiguous", "--timeout", "900"]).via
        self.assertEqual(worker_default, common.REVIEW_POLICY["claude"]["via"],
                         "flip REVIEW_POLICY claude via and the claude --via default together")


class CodexReviewTests(ReviewBase):
    def codex_review(self, *extra, env=None):
        return self.cli("review", "--reviewer", "codex", "--item", "hd-test-item", "--commit", self.head[:12],
                        "--prompt", self.prompt, "--workdir", self.repo, *extra, env=env or self.env)

    def test_codex_review_script_shape(self):
        self.open_item()
        result = self.codex_review("--no-launch")
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = self.fields(result.stdout)
        self.assertEqual(fields["via"], "direct")
        run_dir = fields["run_dir"]
        script = Path(run_dir, "inner.zsh").read_text(encoding="utf-8-sig")
        self.assertIn(f"export ASDF_NODEJS_VERSION=22.19.0\nexport PATH={common.CODEX_NODE_BIN}:$PATH\n", script)
        self.assertIn("{} run --ticket {} --timeout 2700 -- {} exec --ephemeral -s read-only -m gpt-6.1-sol -c "
                      "'model_reasoning_effort=\"high\"' -C {} --json -o {}/review.final.md -".format(
                          self.gate, fields["ticket"], common.CODEX_BIN, self.repo, run_dir), re.sub(r" -c mcp_servers\.[A-Za-z0-9_-]+\.enabled=false", "", script))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", script)
        meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))
        self.assertEqual((meta["reviewer"], meta["model"], meta["effort"], meta["via"]),
                         ("codex", "gpt-6.1-sol", "high", "direct"))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT reviewer FROM tickets").fetchone()[0], "codex")

    def test_codex_review_rejects_model_override(self):
        self.open_item()
        result = self.codex_review("--no-launch", "--model", "gpt-6-astra")
        self.assertEqual(result.returncode, 2)

    def test_codex_review_runs_and_wait_summarizes(self):
        gate = os.path.join(self.tmp, "exec-gate")
        Path(gate).write_text(STUB_EXEC_GATE, encoding="utf-8")
        os.chmod(gate, 0o755)
        codex = os.path.join(self.tmp, "stub-codex")
        Path(codex).write_text(STUB_REVIEW_CODEX, encoding="utf-8")
        os.chmod(codex, 0o755)
        env = dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate, HERMES_DELEGATE_CODEX_BIN=codex,
                   STUB_LOG=os.path.join(self.tmp, "preflight.log"))
        result = self.codex_review("--no-launch", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        log = os.path.join(self.tmp, "stub.log")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30, check=False, encoding="utf-8-sig")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ASDF_NODEJS_VERSION=22.19.0", Path(log + ".env").read_text(encoding="utf-8-sig"))
        self.assertIn("APPROVED: no P0/P1", Path(log + ".stdin").read_text(encoding="utf-8-sig"))
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("review rc=0", text)
        self.assertIn("verdict_line=APPROVED: no P0/P1", text)
        self.assertIn("items=(command_execution:1)", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(Path(run_dir, "review.result.md").read_text(encoding="utf-8-sig"), "No findings.\nAPPROVED: no P0/P1\n")

    def test_codex_review_direct_launch_skips_cmux(self):
        self.open_item()
        result = self.codex_review("--check", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN="/bin/echo"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviewer=codex via=direct", result.stdout)
        self.assertIn("smoke codex --via direct", result.stdout)
        missing = self.codex_review("--check", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN="/nonexistent/codex"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("CLI is missing", missing.stderr)


class CodexMcpOffTests(unittest.TestCase):
    def run_with(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp, "codex")
            stub.write_text("#!/bin/sh\n" + script, encoding="utf-8")
            os.chmod(stub, 0o755)
            old = os.environ.get("HERMES_DELEGATE_CODEX_BIN")
            os.environ["HERMES_DELEGATE_CODEX_BIN"] = str(stub)
            try:
                return review.codex_mcp_off()
            finally:
                if old is None:
                    os.environ.pop("HERMES_DELEGATE_CODEX_BIN")
                else:
                    os.environ["HERMES_DELEGATE_CODEX_BIN"] = old

    def test_disables_each_enabled_server_by_name(self):
        listing = json.dumps([{"name": "figma", "enabled": True}, {"name": "aside", "enabled": True},
                              {"name": "logrocket", "enabled": False}, {"name": "bad name;rm", "enabled": True}])
        args = self.run_with(f"[ \"$1 $2 $3\" = 'mcp list --json' ] && cat <<'J'\n{listing}\nJ\n")
        self.assertEqual(args, ["-c", "mcp_servers.aside.enabled=false", "-c", "mcp_servers.figma.enabled=false"])

    def test_listing_failure_keeps_configured_servers(self):
        self.assertEqual(self.run_with("echo not json; exit 1\n"), [])

    def test_codex_reviewer_argv_carries_the_overrides(self):
        listing = json.dumps([{"name": "figma", "enabled": True}])
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp, "codex")
            stub.write_text(f"#!/bin/sh\ncat <<'J'\n{listing}\nJ\n", encoding="utf-8")
            os.chmod(stub, 0o755)
            old = os.environ.get("HERMES_DELEGATE_CODEX_BIN")
            os.environ["HERMES_DELEGATE_CODEX_BIN"] = str(stub)
            try:
                argv = review.reviewer_command("codex", tmp, tmp)
            finally:
                if old is None:
                    os.environ.pop("HERMES_DELEGATE_CODEX_BIN")
                else:
                    os.environ["HERMES_DELEGATE_CODEX_BIN"] = old
        self.assertIn("mcp_servers.figma.enabled=false", argv)
        self.assertLess(argv.index("mcp_servers.figma.enabled=false"), argv.index("-C"))
