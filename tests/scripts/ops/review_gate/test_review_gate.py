from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[4] / "scripts/ops/review-gate"
PYTHON = "/usr/bin/python3"
GATE = ROOT / "review_gate.py"
SHA = "0123456789abcdef0123456789abcdef01234567"
APPROVAL = "1561666040025321615"


class GateCase(unittest.TestCase):
    """All writes and child processes belong to a task-scoped temporary directory."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-gate-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.db_path = self.tmp / "state" / "ledger.sqlite"
        self.env = os.environ.copy()
        for key in ("MAGI_REVIEW_TICKET", "MAGI_GATE_BYPASS", "MAGI_CODEX_CAPTURE", "REVIEW_GATE_NOW"):
            self.env.pop(key, None)
        self.env.update(REVIEW_GATE_DB=str(self.db_path), HOME=str(self.home),
                        USER="review-gate-test", PYTHONDONTWRITEBYTECODE="1")

    def cli(self, *argv, code=0, env=None):
        result = subprocess.run([PYTHON, str(GATE)] + list(argv), env=env or self.env,
                                cwd=str(ROOT), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, (argv, result.stdout, result.stderr))
        return result

    def open(self, item="test-item"):
        self.cli("open", "--item", item, "--goal", "implement review gate")

    def start(self, item="test-item", reviewer="magi", flags=(), code=0):
        result = self.cli("start", "--item", item, "--reviewer", reviewer, "--commit", SHA,
                          *flags, code=code)
        if code:
            self.assertEqual(result.stdout, "")
            return result
        self.assertRegex(result.stdout, r"\A[0-9a-f]{32}\n\Z")
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

    def close(self, ticket, verdict="PASS", *flags):
        return self.cli("close", "--ticket", ticket, "--verdict", verdict, *flags)

    def query(self, sql, params=()):
        with closing(sqlite3.connect(str(self.db_path))) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def update(self, sql, params=()):
        with closing(sqlite3.connect(str(self.db_path))) as db:
            db.execute(sql, params)
            db.commit()

    def ticket(self, ticket):
        return self.query("SELECT * FROM tickets WHERE id=?", (ticket,))[0]

    def item_status(self, item="test-item"):
        return json.loads(self.cli("status", "--item", item, "--json").stdout)

    def age(self, ticket, hours=7):
        issued = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        self.update("UPDATE tickets SET issued_at=? WHERE id=?", (issued, ticket))

    def stop_child(self, child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


class LedgerTests(GateCase):
    def test_r2_r1_concurrent_consume_eight_processes_five_times(self):
        launcher = (
            "import os,sys,time\n"
            "from pathlib import Path\n"
            "while not Path(sys.argv[1]).exists(): time.sleep(0.005)\n"
            "os.execv(sys.argv[2], sys.argv[2:])\n"
        )
        for iteration in range(5):
            with self.subTest(iteration=iteration):
                item = "consume-race-{}".format(iteration)
                self.open(item)
                ticket = self.start(item)
                ready = self.tmp / (item + ".running")
                release = self.tmp / (item + ".release")
                script = ("import pathlib,sys,time\n"
                          "pathlib.Path(sys.argv[1]).touch()\n"
                          "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n")
                runner = subprocess.Popen(
                    [PYTHON, str(GATE), "run", "--ticket", ticket, "--", PYTHON, "-c",
                     script, str(ready), str(release)], env=self.env, cwd=str(ROOT),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.addCleanup(self.stop_child, runner)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    # Read-only checks must not consume a ticket.
                    self.cli("check-ticket", ticket)
                    self.cli("check-ticket", ticket)
                    barrier = self.tmp / (item + ".ready")
                    argv = [PYTHON, "-c", launcher, str(barrier), PYTHON, str(GATE),
                            "check-ticket", "--consume", ticket]
                    children = []
                    for _ in range(8):
                        child = subprocess.Popen(argv, env=self.env, cwd=str(ROOT),
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                 text=True)
                        self.addCleanup(self.stop_child, child)
                        children.append(child)
                    barrier.touch()
                    outputs = [child.communicate(timeout=20) for child in children]
                    codes = [child.returncode for child in children]
                    self.assertEqual(codes.count(0), 1, (codes, outputs))
                    self.assertEqual(codes.count(2), 7, (codes, outputs))
                    self.assertEqual(self.ticket(ticket)["status"], "running")
                    self.cli("check-ticket", ticket)  # Still read-only after consumption.
                    events = json.loads(self.cli("audit", "--json").stdout)["events"]
                    reuses = [e for e in events if e["kind"] == "rejected"
                              and "ticket-reuse" in e["detail"] and ticket in e["detail"]]
                    self.assertEqual(len(reuses), 7)
                finally:
                    release.touch()
                    runner.communicate(timeout=10)
                self.assertEqual(runner.returncode, 0)

    def test_r2_r2_join_requires_open_or_running_ticket(self):
        self.open()
        first = self.start()
        second = self.start(reviewer="codex", flags=("--join",))
        self.close(first)
        self.cli("run", "--ticket", second, "--", PYTHON, "-c", "pass")
        self.start(reviewer="claude", flags=("--join",), code=2)
        self.assertEqual(len(self.item_status()["tickets"]), 2)

    def test_r2_r2_join_matches_first_ticket_commit_prefix(self):
        for index, (first_sha, join_sha) in enumerate(((SHA, SHA[:7]), (SHA[:7], SHA))):
            with self.subTest(first_sha=first_sha):
                item = "join-prefix-{}".format(index)
                self.open(item)
                self.cli("start", "--item", item, "--reviewer", "magi", "--commit", first_sha)
                joined = self.cli("start", "--item", item, "--reviewer", "codex",
                                  "--commit", join_sha, "--join").stdout.strip()
                self.assertEqual(self.ticket(joined)["round"], 1)
                self.cli("start", "--item", item, "--reviewer", "claude",
                         "--commit", "abcdef0", "--join", code=2)
                self.assertEqual(len(self.item_status(item)["tickets"]), 2)

    def test_r2_r2_join_does_not_use_shorter_joined_commit_as_anchor(self):
        self.open()
        self.start()
        self.cli("start", "--item", "test-item", "--reviewer", "codex", "--commit", SHA[:7],
                 "--join")
        self.cli("start", "--item", "test-item", "--reviewer", "claude", "--commit",
                 SHA[:7] + "f" * (len(SHA) - 7), "--join", code=2)
        self.assertEqual(len(self.item_status()["tickets"]), 2)

    def test_r2_r3_fallback_matches_failed_ticket_commit_prefix(self):
        for index, (failed_sha, fallback_sha) in enumerate(((SHA, SHA[:7]), (SHA[:7], SHA))):
            with self.subTest(failed_sha=failed_sha):
                item = "fallback-prefix-{}".format(index)
                self.open(item)
                first = self.cli("start", "--item", item, "--reviewer", "magi",
                                 "--commit", failed_sha).stdout.strip()
                self.close(first, "UNREVIEWED", "--reason", "auth failed")
                self.cli("start", "--item", item, "--reviewer", "codex", "--commit",
                         "abcdef0", "--fallback", code=2)
                fallback = self.cli("start", "--item", item, "--reviewer", "codex",
                                    "--commit", fallback_sha, "--fallback").stdout.strip()
                self.assertEqual(self.ticket(fallback)["fallback_for"], first)
                self.assertEqual(self.ticket(fallback)["round"], 1)

    def test_r2_r3_fallback_uses_failed_ticket_not_round_commit(self):
        self.open()
        first = self.cli("start", "--item", "test-item", "--reviewer", "magi", "--commit",
                         SHA[:7]).stdout.strip()
        joined = self.start(reviewer="claude", flags=("--join",))
        self.close(first)
        self.close(joined, "UNREVIEWED", "--reason", "crash")
        self.cli("start", "--item", "test-item", "--reviewer", "codex", "--commit",
                 SHA[:7] + "f" * (len(SHA) - 7), "--fallback", code=2)
        fallback = self.start(reviewer="codex", flags=("--fallback",))
        self.assertEqual(self.ticket(fallback)["fallback_for"], joined)

    def test_r2_r4_ran_requires_verdict_before_new_round_even_with_approval(self):
        self.open()
        first = self.start()
        second = self.start(reviewer="codex", flags=("--join",))
        self.close(first)
        self.cli("run", "--ticket", second, "--", PYTHON, "-c", "pass")
        for flags in ((), ("--owner-approval", APPROVAL)):
            with self.subTest(flags=flags):
                result = self.start(flags=flags, code=2)
                self.assertIn("verdict", result.stderr.lower())
                self.assertIn("first", result.stderr.lower())
        self.assertEqual(len(self.item_status()["tickets"]), 2)
        self.close(second)
        third = self.start()
        self.assertEqual(self.ticket(third)["round"], 2)

    def test_01_single_active_round_and_reopening_refused(self):
        self.open()
        ticket = self.start()
        self.assertEqual(self.ticket(ticket)["round"], 1)
        self.start(code=2)
        self.cli("open", "--item", "test-item", "--goal", "new goal", code=2)
        self.assertEqual(len(self.query("SELECT * FROM tickets")), 1)
        self.assertEqual(self.item_status()["item"]["goal"], "implement review gate")

    def test_02_round_cap_and_owner_approval(self):
        self.open()
        first = self.start()
        self.close(first, "BLOCK", "--blockers", "2")
        self.assertEqual(self.item_status()["item"]["status"], "open")
        second = self.start()
        result = self.close(second, "BLOCK", "--blockers", "1")
        self.assertIn("Owner notice", result.stderr)
        self.assertIn("round 2", result.stderr)
        self.assertIn("--owner-approval", result.stderr)
        self.assertEqual(self.item_status()["item"]["status"], "cap-reached")
        self.start(code=2)
        third = self.start(flags=("--owner-approval", APPROVAL, "--note", "owner approved"))
        self.assertEqual(self.ticket(third)["round"], 3)
        self.assertEqual(self.ticket(third)["owner_approval"], APPROVAL)
        self.assertEqual(self.ticket(third)["note"], "owner approved")
        self.close(third, "BLOCK", "--blockers", "1")
        self.start(code=2)  # An approval authorizes its own round, not all future rounds.

    def test_03_counts_survive_new_process_reviewer_and_pr_bot(self):
        self.open()
        first = self.start()
        self.close(first)
        second = self.start(reviewer="pr-bot")
        self.assertEqual(self.ticket(second)["round"], 2)
        self.close(second)
        self.start(reviewer="claude", code=2)
        third = self.start(reviewer="codex", flags=("--owner-approval", APPROVAL))
        self.assertEqual(self.ticket(third)["round"], 3)
        self.open("independent-item")
        independent = self.start(item="independent-item", reviewer="other")
        self.assertEqual(self.ticket(independent)["round"], 1)

    def test_04_fallback_guards_and_stopped_unreviewed(self):
        self.open()
        self.start(reviewer="codex", flags=("--fallback",), code=2)
        first = self.start()
        self.start(reviewer="codex", flags=("--fallback",), code=2)
        self.close(first, "UNREVIEWED", "--reason", "auth failed")
        self.start(flags=("--fallback",), code=2)
        fallback = self.start(reviewer="codex", flags=("--fallback",))
        self.assertEqual(self.ticket(fallback)["round"], 1)
        self.assertEqual(self.ticket(fallback)["fallback_for"], first)
        self.start(reviewer="claude", flags=("--fallback",), code=2)
        result = self.close(fallback, "UNREVIEWED", "--reason", "timeout")
        self.assertIn("stopped-unreviewed at round 1", result.stderr)
        self.assertIn("--owner-approval", result.stderr)
        self.assertEqual(self.item_status()["item"]["status"], "stopped-unreviewed")
        self.start(reviewer="claude", flags=("--fallback",), code=2)
        self.start(code=2)
        second = self.start(flags=("--owner-approval", APPROVAL))
        self.assertEqual(self.ticket(second)["round"], 2)

    def test_04_fallback_pass_resolves_only_the_failed_ticket(self):
        self.open()
        first = self.start()
        joined = self.start(reviewer="claude", flags=("--join",))
        self.close(first, "UNREVIEWED", "--reason", "crashed")
        self.close(joined, "UNREVIEWED", "--reason", "unavailable")
        fallback = self.start(reviewer="codex", flags=("--fallback",))
        self.close(fallback)
        self.assertEqual(self.item_status()["item"]["status"], "open")
        self.open("fallback-pass")
        first = self.start("fallback-pass")
        self.close(first, "UNREVIEWED", "--reason", "crashed")
        fallback = self.start("fallback-pass", "codex", ("--fallback",))
        self.close(fallback)
        self.assertEqual(self.item_status("fallback-pass")["item"]["status"], "passed")

    def test_04_fallback_requires_latest_closed_verdict(self):
        self.open()
        first = self.start()
        joined = self.start(reviewer="claude", flags=("--join",))
        # Close in reverse issue order to distinguish latest close from latest issue.
        self.close(joined, "UNREVIEWED", "--reason", "unavailable")
        self.close(first)
        self.start(reviewer="codex", flags=("--fallback",), code=2)

    def test_05_join_same_round_maximum_three_and_not_after_closed(self):
        self.open()
        self.start(flags=("--join",), code=2)
        first = self.start()
        second = self.start(reviewer="codex", flags=("--join",))
        third = self.start(reviewer="pr-bot", flags=("--join",))
        self.assertEqual([self.ticket(t)["round"] for t in (first, second, third)], [1, 1, 1])
        self.assertEqual(self.ticket(second)["joined"], 1)
        self.start(reviewer="claude", flags=("--join",), code=2)
        self.close(first)
        self.assertEqual(self.item_status()["item"]["status"], "open")
        self.close(second)
        self.close(third)
        self.start(flags=("--join",), code=2)
        self.assertEqual(self.item_status()["item"]["status"], "passed")

    def test_06_eight_process_race_five_times(self):
        launcher = (
            "import os,sys,time\n"
            "from pathlib import Path\n"
            "while not Path(sys.argv[1]).exists(): time.sleep(0.005)\n"
            "os.execv(sys.argv[2], sys.argv[2:])\n"
        )
        for iteration in range(5):
            with self.subTest(iteration=iteration):
                item = "race-{}".format(iteration)
                self.open(item)
                barrier = self.tmp / (item + ".ready")
                argv = [PYTHON, "-c", launcher, str(barrier), PYTHON, str(GATE),
                        "start", "--item", item, "--reviewer", "magi", "--commit", SHA]
                children = []
                for _ in range(8):
                    child = subprocess.Popen(argv, env=self.env, cwd=str(ROOT),
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.addCleanup(self.stop_child, child)
                    children.append(child)
                barrier.touch()
                outputs = [child.communicate(timeout=20) for child in children]
                codes = [child.returncode for child in children]
                self.assertEqual(codes.count(0), 1, (codes, outputs))
                self.assertEqual(codes.count(2), 7, (codes, outputs))
                tickets = self.query("SELECT * FROM tickets WHERE item=?", (item,))
                self.assertEqual(len(tickets), 1)
                self.assertEqual(tickets[0]["round"], 1)

    def test_07_run_preserves_argv_env_exit_and_pending_verdict(self):
        self.open()
        ticket = self.start()
        script = ("import json,os,sys; print(json.dumps([sys.argv[1:], "
                  "os.environ['MAGI_REVIEW_TICKET']])); "
                  "print('child stderr', file=sys.stderr); sys.exit(17)")
        argv = ["has spaces", "a'quote\"", "line1\nline2", "", "$(never-execute)"]
        result = self.cli("run", "--ticket", ticket, "--", PYTHON, "-c", script, *argv, code=17)
        self.assertEqual(json.loads(result.stdout), [argv, ticket])
        self.assertIn("child stderr", result.stderr)
        row = self.ticket(ticket)
        self.assertEqual(row["status"], "ran")
        self.assertEqual(row["exit_code"], 17)
        self.assertIsNone(row["verdict"])
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(json.loads(row["argv"])[-len(argv):], argv)
        self.cli("check-ticket", ticket, code=2)
        self.cli("run", "--ticket", ticket, "--", PYTHON, "-c", "pass", code=2)

    def test_07_timeout_kills_and_reaps_child(self):
        self.open()
        ticket = self.start()
        pid_file = self.tmp / "child.pid"
        script = ("import os,pathlib,sys,time; "
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                  "time.sleep(30)")
        result = self.cli("run", "--ticket", ticket, "--timeout", "0.5", "--",
                          PYTHON, "-c", script, str(pid_file), code=124)
        self.assertEqual(result.stdout, "")
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.ticket(ticket)["exit_code"], 124)
        self.assertEqual(self.ticket(ticket)["status"], "ran")
        self.assertIsNone(self.ticket(ticket)["verdict"])

    def test_07_running_ticket_valid_and_cannot_start_or_run_twice(self):
        self.open()
        ticket = self.start()
        ready = self.tmp / "ready"
        release = self.tmp / "release"
        script = ("import pathlib,sys,time\n"
                  "pathlib.Path(sys.argv[1]).touch()\n"
                  "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n")
        runner = subprocess.Popen([PYTHON, str(GATE), "run", "--ticket", ticket, "--",
                                   PYTHON, "-c", script, str(ready), str(release)], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_child, runner)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertEqual(self.ticket(ticket)["status"], "running")
            self.cli("check-ticket", ticket)
            self.start(code=2)
            self.cli("run", "--ticket", ticket, "--", PYTHON, "-c", "pass", code=2)
        finally:
            release.touch()
            runner.communicate(timeout=10)
        self.assertEqual(runner.returncode, 0)

    def test_08_check_ticket_status_and_expiry(self):
        self.open()
        first = self.start()
        result = self.cli("check-ticket", first)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.close(first)
        self.cli("check-ticket", first, code=2)
        self.cli("check-ticket", "unknown", code=2)
        second = self.start()
        self.age(second)
        self.cli("check-ticket", second, code=2)
        self.age(second, hours=-1)
        self.cli("check-ticket", second, code=2)

    def test_09_exact_status_format_and_flags(self):
        self.open()
        first = self.start()
        joined = self.start(reviewer="pr-bot", flags=("--join",))
        self.close(first, "UNREVIEWED", "--reason", "crash")
        fallback = self.start(reviewer="codex", flags=("--fallback",))
        self.close(fallback)
        self.close(joined)
        expected = (
            "item=test-item round=1 reviewer=magi commit=0123456789ab verdict=UNREVIEWED\n"
            "item=test-item round=1 reviewer=pr-bot commit=0123456789ab verdict=PASS join\n"
            "item=test-item round=1 reviewer=codex commit=0123456789ab verdict=PASS fallback\n"
            "status=passed rounds_used=1/2\n")
        self.assertEqual(self.cli("status", "--item", "test-item").stdout, expected)
        second = self.start(flags=("--owner-approval", APPROVAL))
        text = self.cli("status", "--item", "test-item").stdout
        self.assertIn("round=2 reviewer=magi commit=0123456789ab verdict=pending "
                      "owner-approval=" + APPROVAL + "\nstatus=open rounds_used=2/2\n", text)
        self.assertEqual(self.item_status()["rounds_used"], 2)
        self.assertEqual(self.ticket(second)["owner_approval"], APPROVAL)

    def test_09_audit_events_stopped_items_and_all_stale_states(self):
        self.open()
        first = self.start()
        self.close(first, "UNREVIEWED", "--reason", "crash")
        fallback = self.start(reviewer="codex", flags=("--fallback",))
        self.close(fallback, "UNREVIEWED", "--reason", "timeout")
        self.open("cap-item")
        for _ in range(2):
            ticket = self.start("cap-item")
            self.close(ticket, "BLOCK", "--blockers", "1")
        stale = []
        for state in ("open", "running", "ran"):
            item = "stale-" + state
            self.open(item)
            ticket = self.start(item)
            self.age(ticket)
            self.update("UPDATE tickets SET status=? WHERE id=?", (state, ticket))
            stale.append(ticket)
        self.cli("log-event", "--kind", "bypass", "--detail", "reason with spaces\nand newline")
        data = json.loads(self.cli("audit", "--json").stdout)
        event = data["events"][0]
        self.assertEqual(event["kind"], "bypass")
        self.assertEqual(event["detail"], "reason with spaces\nand newline")
        self.assertEqual(event["cwd"], str(ROOT))
        self.assertEqual(event["user"], "review-gate-test")
        self.assertTrue(event["created_at"].endswith("+00:00"))
        self.assertEqual({i["status"] for i in data["items"]},
                         {"stopped-unreviewed", "cap-reached"})
        self.assertEqual({t["id"] for t in data["stale_tickets"]}, set(stale))
        text = self.cli("audit").stdout
        self.assertIn("events ", text)
        self.assertIn("stopped-unreviewed", text)
        self.assertIn("stale_tickets ", text)
        self.update("UPDATE events SET created_at='2000-01-01T00:00:00+00:00'")
        since = json.loads(self.cli("audit", "--since", "2001-01-01", "--json").stdout)
        self.assertEqual(since["events"], [])
        self.assertEqual(len(since["items"]), 2)
        self.assertEqual(len(since["stale_tickets"]), 3)

    def test_10_validation_errors_leave_database_unchanged(self):
        self.open()
        ticket = self.start()
        cases = [
            ("open", "--item", "BAD", "--goal", "new"),
            ("open", "--item", "ab", "--goal", "new"),
            ("start", "--item", "test-item", "--reviewer", "magi", "--commit", "xyz"),
            ("start", "--item", "test-item", "--reviewer", "magi", "--commit", SHA,
             "--owner-approval", "not-a-discord-id"),
            ("start", "--item", "test-item", "--reviewer", "magi", "--commit", SHA,
             "--join", "--fallback"),
            ("close", "--ticket", ticket, "--verdict", "BLOCK"),
            ("close", "--ticket", ticket, "--verdict", "BLOCK", "--blockers", "0"),
            ("close", "--ticket", ticket, "--verdict", "UNREVIEWED"),
            ("close", "--ticket", ticket, "--verdict", "UNREVIEWED", "--reason", " "),
            ("run", "--ticket", ticket, "--"),
            ("run", "--ticket", ticket, "--timeout", "nan", "--", PYTHON),
            ("audit", "--since", "2026-02-30"),
            ("log-event", "--kind", "invalid", "--detail", "no"),
            ("nonexistent-command",),
        ]
        with closing(sqlite3.connect(str(self.db_path))) as db:
            before = list(db.iterdump())
        for argv in cases:
            with self.subTest(argv=argv):
                result = self.cli(*argv, code=3)
                self.assertTrue(result.stderr)
                with closing(sqlite3.connect(str(self.db_path))) as db:
                    self.assertEqual(list(db.iterdump()), before)

    def test_10_invalid_input_does_not_create_database(self):
        self.cli("open", "--item", "!", "--goal", "bad", code=3)
        self.assertFalse(self.db_path.exists())

    def test_storage_permissions_wal_and_not_found(self):
        self.open()
        self.assertEqual(self.db_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.query("PRAGMA journal_mode")[0]["journal_mode"], "wal")
        self.cli("status", "--item", "unknown-item", code=4)
        self.start(item="unknown-item", code=4)
        self.cli("close", "--ticket", "unknown", "--verdict", "PASS", code=4)
        self.cli("run", "--ticket", "unknown", "--", PYTHON, "-c", "pass", code=4)

    def test_ran_ticket_requires_close_without_resetting_count(self):
        self.open()
        first = self.start()
        self.cli("run", "--ticket", first, "--", PYTHON, "-c", "pass")
        self.start(code=2)
        self.close(first, "BLOCK", "--blockers", "1")
        second = self.start()
        self.assertEqual(self.ticket(second)["round"], 2)
        self.close(second, "BLOCK", "--blockers", "1")
        third = self.start(flags=("--owner-approval", APPROVAL))
        self.assertEqual(self.item_status()["item"]["status"], "open")
        self.close(third)
        self.assertEqual(self.item_status()["item"]["status"], "passed")
        self.cli("close", "--ticket", first, "--verdict", "PASS", code=2)

    def test_missing_executable_still_records_pending_run(self):
        self.open()
        ticket = self.start()
        self.cli("run", "--ticket", ticket, "--", str(self.tmp / "missing-executable"), code=127)
        self.assertEqual(self.ticket(ticket)["status"], "ran")
        self.assertEqual(self.ticket(ticket)["exit_code"], 127)
        self.assertIsNone(self.ticket(ticket)["verdict"])


if __name__ == "__main__":
    unittest.main()
