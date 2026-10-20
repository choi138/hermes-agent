from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from audit import review_audit_mac as mac
from audit import review_audit_report as report
import review_gate


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
SINCE = "2026-10-13"


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="review-audit-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.codex = self.tmp / "codex"
        self.claude = self.tmp / "claude"
        self.db = self.tmp / "ledger.sqlite"
        self.env = patch.dict(os.environ, HOME=str(self.home), CODEX_SESSIONS_DIR=str(self.codex),
                              CLAUDE_PROJECTS_DIR=str(self.claude), REVIEW_GATE_DB=str(self.db),
                              REVIEW_GATE_PY=str(ROOT / "review_gate.py"),
                              REVIEW_GATE_HOOK_LOG=str(self.tmp / "hook.jsonl"),
                              PYTHONDONTWRITEBYTECODE="1")
        self.env.start()
        self.addCleanup(self.env.stop)

    def jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
        return path

    def codex_file(self, name="session", start="2026-10-20T10:00:00Z", cwd="/repo", policy="read-only", day="20"):
        return self.jsonl(self.codex / "2026/10" / day / (name + ".jsonl"), [
            dict(type="session_meta", payload=dict(cwd=cwd, timestamp=start, originator="codex_exec")),
            dict(type="turn_context", payload=dict(sandbox_policy=dict(type=policy))),
        ])

    def claude_file(self, name="session", start="2026-10-20T10:00:00Z", cwd="/repo", entrypoint="sdk-cli", tools=()):
        return self.jsonl(self.claude / "project" / (name + ".jsonl"), [
            dict(type="user", cwd=cwd, timestamp=start, entrypoint=entrypoint),
            dict(type="assistant", timestamp="2026-10-20T10:10:00Z", message=dict(content=[
                dict(type="tool_use", name=tool, input={}) for tool in tools])),
        ])

    def collect(self, raw=None):
        return mac.collect(SINCE, raw or {}, now=NOW)

    def empty_data(self):
        return dict(ledger={}, sessions=dict(unledgered=[]))

    def test_codex_matching_time_and_cwd(self):
        self.codex_file("matched", cwd="/repo/worktree")
        self.codex_file("wrong-cwd", cwd="/repo-other")
        self.codex_file("late", start="2026-10-20T10:14:00Z")
        runs = [dict(started_at="2026-10-20T10:03:00Z", ended_at="2026-10-20T10:10:00Z", cwd="/repo")]
        data = self.collect(dict(runs=runs))
        self.assertEqual(data["sessions"]["codex_review_like"], 3)
        self.assertEqual({Path(s["file"]).stem for s in data["sessions"]["unledgered"]}, {"wrong-cwd", "late"})

    def test_run_end_slack_boundary(self):
        run = dict(started_at="2026-10-20T10:00:00Z", ended_at="2026-10-20T10:10:00Z")
        self.assertTrue(mac.matched(dict(start="2026-10-20T10:13:00Z"), [run], NOW))
        self.assertFalse(mac.matched(dict(start="2026-10-20T10:13:01Z"), [run], NOW))
        self.assertFalse(mac.matched(dict(start="2026-10-20T09:56:59Z"), [run], NOW))

    def test_running_window_and_missing_ledger_cwd(self):
        self.codex_file()
        data = self.collect(dict(runs=[dict(started_at="2026-10-20T09:59:00Z", ended_at=None)]))
        self.assertEqual(data["sessions"]["unledgered"], [])

    def test_codex_sandbox_first_policy_and_date_filter(self):
        path = self.codex_file("writer", policy="workspace-write")
        with path.open("a") as stream:
            stream.write(json.dumps(dict(sandbox_policy=dict(type="read-only"))) + "\n")
        self.codex_file("old", start="2026-10-12T10:00:00Z", day="12")
        self.codex_file("reader")
        self.assertEqual(self.collect()["sessions"]["codex_review_like"], 1)

    def test_claude_print_and_edit_filter(self):
        self.claude_file("reader", tools=["Read", "Bash"])
        for tool in ["Edit", "Write", "MultiEdit", "NotebookEdit"]:
            self.claude_file(tool, tools=[tool])
        self.claude_file("interactive", entrypoint="cli")
        data = self.collect()
        self.assertEqual(data["sessions"]["claude_review_like"], 1)
        self.assertEqual(Path(data["sessions"]["unledgered"][0]["file"]).stem, "reader")

    def test_claude_mtime_and_earliest_timestamp(self):
        path = self.claude_file("old-mtime")
        old = mac.since_date(SINCE).timestamp() - 1
        os.utime(path, (old, old))
        other = self.claude_file("ordered")
        with other.open("a") as stream:
            stream.write(json.dumps(dict(timestamp="2026-10-20T09:00:00Z", cwd="/different")) + "\n")
        data = self.collect()
        session = data["sessions"]["unledgered"][0]
        self.assertEqual(data["sessions"]["claude_review_like"], 1)
        self.assertEqual(mac.timestamp(session["start"]).hour, 9)
        self.assertEqual(session["cwd"], "/repo")

    def test_claude_old_start_recent_mtime_is_included(self):
        self.claude_file(start="2026-10-01T00:00:00Z")
        self.assertEqual(self.collect()["sessions"]["claude_review_like"], 1)

    def test_claude_matching(self):
        self.claude_file()
        data = self.collect(dict(runs=[dict(started_at="2026-10-20T10:00:00Z", ended_at="2026-10-20T10:11:00Z", cwd="/repo")]))
        self.assertEqual(data["sessions"]["unledgered"], [])

    def test_fido_grouping_uses_path_components(self):
        self.codex_file("fido", cwd=str(self.home / ".fido-automation/repo"))
        self.claude_file("fido-wt", cwd=str(self.home / "Documents/fido-wt/task"))
        self.codex_file("lookalike", cwd=str(self.home / ".fido-automation-other"))
        groups = {Path(s["file"]).stem: s["group"] for s in self.collect()["sessions"]["unledgered"]}
        self.assertEqual(groups, {"fido": "fido", "fido-wt": "fido", "lookalike": "other"})

    def test_partial_lines_and_quoted_fake_tool(self):
        path = self.claude_file()
        with path.open("a") as stream:
            stream.write('{broken\n' + json.dumps(dict(type="assistant", message=dict(content=[
                dict(type="text", text='{"type":"tool_use","name":"Edit"}')
            ]))) + '\n')
        self.assertEqual(self.collect()["sessions"]["claude_review_like"], 1)

    def test_ledger_issues_and_24_hour_threshold(self):
        raw = dict(events=[dict(kind="bypass"), dict(kind="legacy-exempt"),
                           dict(kind="rejected", detail="ticket-reuse ticket=T"), dict(kind="rejected")],
                   items=[dict(id="stopped")], over_round_items=[dict(item="approved", round=3)],
                   stale_tickets=[dict(id=str(h), status="ran", issued_at=(NOW - timedelta(hours=h)).isoformat())
                                  for h in [6, 23, 24, 25, 48]])
        ledger = self.collect(raw)["ledger"]
        self.assertEqual(len(ledger["bypass"]), 1)
        self.assertEqual(len(ledger["legacy-exempt"]), 1)
        self.assertEqual(len(ledger["rejected"]), 2)
        self.assertEqual(len(ledger["ticket-reuse"]), 1)
        self.assertEqual({t["id"] for t in ledger["stale_tickets"]}, {"25", "48"})
        self.assertEqual(ledger["over_round_items"], raw["over_round_items"])

    def test_run_context_query_is_read_only_and_includes_closed_runs(self):
        db = review_gate.connect()
        db.execute("INSERT INTO items VALUES ('item', 'goal', 'passed', ?)", (NOW.isoformat(),))
        db.execute("""INSERT INTO tickets (id,item,round,reviewer,reviewed_commit,status,issued_at,
            started_at,ended_at,closed_at,verdict,argv) VALUES
            ('ticket','item',3,'codex','abcdef0','closed',?,?,?,?, 'PASS','SECRET_SENTINEL_9f3')""",
                   tuple((NOW - timedelta(hours=h)).isoformat() for h in [4, 3, 2, 1]))
        db.close()
        before = self.db.read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / "review_gate.py"), "audit", "--since", SINCE,
                                 "--json", "--run-context"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SECRET_SENTINEL_9f3", result.stdout)
        raw = json.loads(result.stdout)
        self.assertEqual(raw["over_round_items"], [dict(item="item", round=3)])
        self.assertEqual(raw["runs"][0]["ticket"], "ticket")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(mac.load_ledger(SINCE)["runs"], raw["runs"])

    def test_missing_run_context_database_is_not_created(self):
        result = subprocess.run([sys.executable, str(ROOT / "review_gate.py"), "audit", "--run-context", "--json"],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.exists())

    def fake_ledger(self, raw):
        script = self.tmp / "fake_gate.py"
        script.write_text("import json,sys\nassert '--run-context' in sys.argv\nprint(" + repr(json.dumps(raw)) + ")\n")
        os.environ["REVIEW_GATE_PY"] = str(script)

    def test_mac_cli_with_fake_ledger(self):
        self.fake_ledger(dict(events=[], runs=[], stale_tickets=[]))
        self.codex_file()
        result = subprocess.run([sys.executable, str(ROOT / "audit/review_audit_mac.py"), "--since", SINCE, "--json"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["since"], SINCE)
        self.assertEqual(data["sessions"]["codex_review_like"], 1)

    def test_mac_cli_fails_when_ledger_has_no_windows(self):
        self.fake_ledger(dict(events=[]))
        result = subprocess.run([sys.executable, str(ROOT / "audit/review_audit_mac.py"), "--since", SINCE, "--json"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ValueError", result.stderr)

    def test_report_nothing_notable(self):
        output = report.format_report(SINCE, [dict(decision="wrapped")], self.empty_data())
        self.assertEqual(output, "이번 주 ledger를 벗어난 리뷰가 없습니다.")

    def test_report_counts_bypass_and_ledger(self):
        records = [dict(decision=k, bypass_reason="smoke\nfor auth") for k in ["blocked", "wrapped", "bypass", "error"]]
        data = self.empty_data()
        data["ledger"] = dict(rejected=[{}], over_round_items=[{}], stale_tickets=[{}])
        output = report.format_report(SINCE, records, data)
        self.assertIn("차단 1 / wrapped 1 / bypass 1 / 오류 1", output)
        self.assertIn("smoke for auth (1건)", output)
        self.assertIn("2라운드 초과 1 / 24시간 미종결 1", output)

    def test_report_truncates_ten_newest_other_and_counts_fido(self):
        data = self.empty_data()
        data["sessions"]["unledgered"] = [dict(cli="codex", start="2026-10-20T{:02d}:00:00Z".format(i),
                                               cwd="/repo{:02d}".format(i), group="other") for i in range(13)]
        data["sessions"]["unledgered"].extend([dict(cli="claude", start="2026-10-20T23:00:00Z", group="fido")] * 8)
        records = [dict(decision="bypass", bypass_reason="reason{}".format(i)) for i in range(10)]
        output = report.format_report(SINCE, records, data)
        self.assertEqual(len([line for line in output.splitlines() if line.startswith("- ")]), 10)
        self.assertIn("외 3건", output)
        self.assertIn("미등록 fido: 8건", output)
        self.assertIn("/repo12", output)
        self.assertNotIn("/repo02", output)
        self.assertLess(output.index("/repo12"), output.index("/repo11"))
        self.assertLessEqual(len(output.splitlines()), 25)

    def test_report_mac_failure_still_prints_hook_and_exits_zero(self):
        output = io.StringIO()
        with patch.object(report, "mac_json", side_effect=subprocess.CalledProcessError(255, ["ssh"])), redirect_stdout(output):
            self.assertEqual(report.main(["--since", SINCE]), 0)
        self.assertIn("Mac 감사 실패: CalledProcessError (exit 255)", output.getvalue())
        self.assertIn("hook:", output.getvalue())

    def test_report_mac_timeout_and_invalid_json(self):
        for error in [subprocess.TimeoutExpired("ssh", 120), ValueError("SECRET")]:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(report, "mac_json", side_effect=error), redirect_stdout(output):
                    self.assertEqual(report.main(["--since", SINCE]), 0)
                self.assertIn(type(error).__name__, output.getvalue())
                self.assertNotIn("SECRET", output.getvalue())

    def test_hook_log_filters_dates_and_bad_lines(self):
        path = Path(os.environ["REVIEW_GATE_HOOK_LOG"])
        self.jsonl(path, [dict(ts="2026-10-12T23:59:59Z", decision="blocked"),
                          dict(ts="2026-10-13T00:00:00Z", decision="bypass"), dict(ts="bad")])
        with path.open("a") as stream:
            stream.write("{incomplete\n")
        self.assertEqual([r["decision"] for r in report.hook_records(SINCE)], ["bypass"])

    def test_report_executes_override_with_date_and_120_second_timeout(self):
        os.environ["REVIEW_AUDIT_MAC_CMD"] = "fake-mac --since {since} --json"
        with patch.object(report.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(self.empty_data()), "")) as run:
            self.assertEqual(report.mac_json(SINCE), self.empty_data())
        self.assertEqual(run.call_args[0][0], ["fake-mac", "--since", SINCE, "--json"])
        self.assertEqual(run.call_args[1]["timeout"], 120)

    def test_report_cli_fake_mac(self):
        script = self.tmp / "fake_mac.py"
        script.write_text("print(" + repr(json.dumps(self.empty_data())) + ")\n")
        os.environ["REVIEW_AUDIT_MAC_CMD"] = shlex.join([sys.executable, str(script), "--since", "{since}"])
        result = subprocess.run([sys.executable, str(ROOT / "audit/review_audit_report.py"), "--since", SINCE],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "이번 주 ledger를 벗어난 리뷰가 없습니다.")

    def test_server_report_deploys_as_one_file(self):
        standalone = self.tmp / "review_audit_report.py"
        standalone.write_text((ROOT / "audit/review_audit_report.py").read_text())
        script = self.tmp / "fake_mac.py"
        script.write_text("print(" + repr(json.dumps(self.empty_data())) + ")\n")
        os.environ["REVIEW_AUDIT_MAC_CMD"] = shlex.join([sys.executable, str(script), "--since", "{since}"])
        result = subprocess.run([sys.executable, str(standalone), "--since", SINCE], cwd=str(self.tmp),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "이번 주 ledger를 벗어난 리뷰가 없습니다.")

    def test_report_default_since_is_seven_utc_days(self):
        output = io.StringIO()
        with patch.object(report, "mac_json", return_value=self.empty_data()) as fetch, redirect_stdout(output):
            self.assertEqual(report.main([]), 0)
        self.assertEqual(fetch.call_args[0][0], (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat())

    def test_invalid_dates(self):
        for script in ["review_audit_mac.py", "review_audit_report.py"]:
            result = subprocess.run([sys.executable, str(ROOT / "audit" / script), "--since", "2026-02-30"],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
