import json
import os
from pathlib import Path
import subprocess

from deploy_support import checkout

from test_review_gate import GateCase, GATE, ROOT


class ShimTests(GateCase):
    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "executed.json"
        # Installer must quote paths as well as preserving the invocation's argv.
        self.fake = self.tmp / "fake magi 'quoted $value`"
        self.fake.write_text(
            "#!/usr/bin/python3\n"
            "import json,os,pathlib,sys\n"
            "data = {'argv': sys.argv[1:], 'argv_hex': [os.fsencode(a).hex() for a in sys.argv[1:]],\n"
            "        'capture': os.environ.get('MAGI_CODEX_CAPTURE'),\n"
            "        'ticket': os.environ.get('MAGI_REVIEW_TICKET'), 'path': os.environ.get('PATH')}\n"
            "pathlib.Path(os.environ['FAKE_MAGI_MARKER']).write_text(json.dumps(data))\n"
            "print(json.dumps(data))\n")
        self.fake.chmod(0o755)
        self.shim_path = self.home / ".local/bin/magi"
        self.install_root = checkout(self.tmp / "checkout")
        self.env["FAKE_MAGI_MARKER"] = str(self.marker)
        self.empty_bin = self.tmp / "empty-bin"
        self.empty_bin.mkdir()
        self.env["PATH"] = str(self.empty_bin)
        self.install("--apply")

    def install(self, *flags, code=0):
        result = subprocess.run(
            ["/bin/sh", str(self.install_root / "install.sh"), "--host", "mac",
             "--magi-real-bin", str(self.fake), "--review-gate", str(GATE), *flags],
            env=dict(self.env, PATH=os.defpath), cwd=str(ROOT), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return result

    def shim(self, *argv, code=0, extra_env=None):
        if self.marker.exists():
            self.marker.unlink()
        env = self.env.copy()
        env.update(extra_env or {})
        result = subprocess.run([str(self.shim_path), *argv], env=env, cwd=str(ROOT),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, (argv, result.stdout, result.stderr))
        self.assertEqual(self.marker.exists(), code == 0, (result.stdout, result.stderr))
        return result

    def events(self):
        if not self.db_path.exists():
            return []
        return self.query("SELECT * FROM events ORDER BY id")

    def test_r2_r1_hand_exported_open_ticket_refused(self):
        self.open()
        ticket = self.start()
        self.cli("check-ticket", ticket)  # Validation stays read-only and permits open tickets.
        self.cli("check-ticket", ticket)
        self.shim("review", code=2, extra_env={"MAGI_REVIEW_TICKET": ticket})
        self.assertEqual(self.ticket(ticket)["status"], "open")
        self.assertEqual([e["kind"] for e in self.events()], ["rejected"])
        self.cli("check-ticket", "--consume", ticket, code=2)
        # Refused checks must not consume it: a later legitimate run still succeeds.
        self.cli("run", "--ticket", ticket, "--", str(self.shim_path), "review")
        self.assertTrue(self.marker.exists())

    def test_r2_r1_second_invocation_in_one_run_refused_executes_fake_once(self):
        self.open()
        ticket = self.start()
        result = self.cli("run", "--ticket", ticket, "--", "/bin/sh", "-c",
                          '"$1" review; "$1" review', "two-reviews", str(self.shim_path), code=2)
        # The fake emits one JSON line per actual execution.
        executions = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["ticket"], ticket)
        self.assertEqual(self.ticket(ticket)["exit_code"], 2)
        self.assertIsNone(self.ticket(ticket)["verdict"])
        self.assertTrue(any(e["kind"] == "rejected" for e in self.events()))

    def test_r2_r5_audit_lists_reuse_inside_run_and_hand_export_after_run(self):
        self.open()
        ticket = self.start()
        self.cli("run", "--ticket", ticket, "--", "/bin/sh", "-c",
                 '"$1" review; "$1" review; exit 0', "audit-reviews", str(self.shim_path))
        self.shim("review", code=2, extra_env={"MAGI_REVIEW_TICKET": ticket})
        audit = json.loads(self.cli("audit", "--json").stdout)
        reuses = [e for e in audit["events"]
                  if e["kind"] == "rejected" and "ticket-reuse" in e["detail"]]
        self.assertEqual(len(reuses), 2)
        for event in reuses:
            self.assertIn(ticket, event["detail"])
            self.assertEqual(event["cwd"], str(ROOT))
            self.assertEqual(event["user"], "review-gate-test")
        plain = self.cli("audit").stdout
        self.assertEqual(plain.count("ticket-reuse"), 2)
        self.assertIn('"kind": "rejected"', plain)

    def test_11_no_ticket_refused_and_rejection_logged(self):
        result = self.shim("review", "--target", "x", code=2)
        self.assertEqual(result.stdout, "")
        expected = ('magi: review refused — no review-gate ticket. Start one: review_gate.py start '
                    '--item <ID> --reviewer magi --commit <SHA>, then run via review_gate.py run '
                    '--ticket <T> -- magi ...  (magi development: set MAGI_GATE_BYPASS="<reason>")\n')
        self.assertEqual(result.stderr, expected)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "rejected")
        self.assertIn("subcommand=review", events[0]["detail"])

    def test_12_ticketed_run_preserves_exact_argv_and_capture(self):
        self.open()
        ticket = self.start()
        argv = ["review", "--target", "has spaces and a 'quote\"", "line1\nline2", "", "$(literal)"]
        result = self.cli("run", "--ticket", ticket, "--", str(self.shim_path), *argv)
        data = json.loads(result.stdout)
        self.assertTrue(self.marker.exists())
        self.assertEqual(data["argv"], argv)
        self.assertEqual(data["argv_hex"], [os.fsencode(arg).hex() for arg in argv])
        self.assertEqual(data["ticket"], ticket)
        self.assertEqual(data["capture"], "json-v1")
        self.assertEqual(self.events(), [])

    def test_13_closed_expired_and_garbage_tickets_refused(self):
        self.open()
        closed = self.start()
        self.close(closed)
        expired = self.start()
        self.age(expired)
        for ticket in (closed, expired, "garbage"):
            with self.subTest(ticket=ticket):
                self.shim("review", code=2, extra_env={"MAGI_REVIEW_TICKET": ticket})
        self.assertEqual([e["kind"] for e in self.events()], ["rejected"] * 3)

    def test_14_bypass_logged_and_short_reason_refused(self):
        reason = "magi self-dev test"
        self.shim("review", extra_env={"MAGI_GATE_BYPASS": reason})
        self.assertEqual(self.events()[0]["kind"], "bypass")
        self.assertIn(reason, self.events()[0]["detail"])
        self.assertIn("subcommand=review", self.events()[0]["detail"])
        self.shim("review", code=2, extra_env={"MAGI_GATE_BYPASS": "1234567"})
        self.assertEqual(self.events()[-1]["kind"], "rejected")
        self.shim("review", extra_env={"MAGI_GATE_BYPASS": "12345678"})
        self.assertEqual(self.events()[-1]["kind"], "bypass")

    def test_14_bypass_executes_when_logging_fails_with_warning(self):
        result = self.shim("review", extra_env={"MAGI_GATE_BYPASS": "magi self-dev test",
                                               "REVIEW_GATE_DB": str(self.tmp)})
        self.assertIn("warning — bypass audit logging failed", result.stderr)

    def test_15_all_allowed_commands_no_ticket_and_no_events(self):
        cases = [("lanes", "list"), ("doctor",), ("report",), ("report-plan",),
                 ("plan",), ("publish",), (), ("--help",), ("--version",),
                 ("review", "--help"), ("review", "-h"),
                 ("--verbose", "lanes", "list"), ("--quiet", "frobnicate", "--help"),
                 ("--examples", "review"), ("--examples=review",), ("--schema",),
                 ("sample", "--lane", "l", "--fixture", "f"), ("quality", "assess")]
        for argv in cases:
            with self.subTest(argv=argv):
                data = json.loads(self.shim(*argv).stdout)
                self.assertEqual(data["argv"], list(argv))
        self.assertEqual(self.events(), [])

    def test_16_default_deny_unknown_and_all_model_commands(self):
        commands = ("", "frobnicate", "review", "review-plan", "run", "auto", "gate", "stack",
                    "adjudicate")
        for command in commands:
            with self.subTest(command=command):
                self.shim("--verbose", command, code=2)
        self.assertEqual([e["kind"] for e in self.events()], ["rejected"] * len(commands))

    def test_17_legacy_repo_dir_exemption_is_contained(self):
        fido = self.home / ".fido-automation"
        checkout = fido / "x" / "wt"
        checkout.mkdir(parents=True)
        self.shim("run", "--repo-dir", str(checkout), "--target", "x")
        self.assertEqual(self.events()[-1]["kind"], "legacy-exempt")
        self.assertIn(str(checkout), self.events()[-1]["detail"])
        other = self.tmp / "other"
        other.mkdir()
        (fido / "escape").symlink_to(other, target_is_directory=True)
        bad_values = ["/tmp/other", str(other), str(fido) + "-lookalike/wt",
                      str(fido / ".." / "elsewhere"), str(fido / "escape"), str(fido)]
        for value in bad_values:
            with self.subTest(value=value):
                self.shim("run", "--repo-dir", value, code=2)
        self.shim("run", "--repo-dir", code=2)
        self.shim("run", "--repo-dir=" + str(checkout))
        self.assertEqual(self.events()[-1]["kind"], "legacy-exempt")
        (self.tmp / "link-in").symlink_to(checkout, target_is_directory=True)
        self.shim("run", "--repo-dir", str(self.tmp / "link-in"))
        self.assertEqual(self.events()[-1]["kind"], "legacy-exempt")

    def test_18_capture_default_explicit_empty_and_path_fallback(self):
        prefix = "/Users/choegeun-won/.asdf/installs/nodejs/22.19.0/bin:"
        for value in (None, "legacy", ""):
            with self.subTest(value=value):
                extra = {} if value is None else {"MAGI_CODEX_CAPTURE": value}
                data = json.loads(self.shim("doctor", extra_env=extra).stdout)
                self.assertEqual(data["capture"], "json-v1" if value is None else value)
                self.assertEqual(data["path"], prefix + str(self.empty_bin))
        # command -v checks existence only; this dummy codex must never be invoked.
        codex = self.empty_bin / "codex"
        codex.write_text("#!/bin/sh\nexit 99\n")
        codex.chmod(0o755)
        data = json.loads(self.shim("doctor").stdout)
        self.assertEqual(data["path"], str(self.empty_bin))

    def test_17_legacy_requires_successful_audit_logging(self):
        checkout = self.home / ".fido-automation" / "x" / "wt"
        checkout.mkdir(parents=True)
        result = self.shim("run", "--repo-dir", str(checkout), code=2,
                           extra_env={"REVIEW_GATE_DB": str(self.tmp)})
        self.assertIn("legacy exemption audit logging failed", result.stderr)

    def test_19_missing_gate_fail_closed_bypass_still_works(self):
        self.install("--apply", "--review-gate", str(self.tmp / "missing-gate.py"))
        result = self.shim("review", code=2, extra_env={"MAGI_REVIEW_TICKET": "some-ticket"})
        self.assertIn("missing or unrunnable", result.stderr)
        self.assertIn("fail-closed", result.stderr)
        result = self.shim("review", extra_env={"MAGI_GATE_BYPASS": "magi self-dev test"})
        self.assertIn("warning — bypass audit logging failed", result.stderr)

    def test_19_unrunnable_gate_refuses_ticket(self):
        broken = self.tmp / "broken-gate.py"
        broken.write_text("raise RuntimeError('broken gate')\n")
        self.install("--apply", "--review-gate", str(broken))
        self.shim("review", code=2, extra_env={"MAGI_REVIEW_TICKET": "some-ticket"})
        self.shim("review", extra_env={"MAGI_GATE_BYPASS": "magi self-dev test"})

    def test_20_shell_syntax_and_shellcheck(self):
        for path in (ROOT / "magi-shim.sh", self.shim_path):
            with self.subTest(path=str(path)):
                for command in (["/bin/sh", "-n", str(path)],
                                ["/opt/homebrew/bin/shellcheck", "-s", "sh", str(path)]):
                    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, (command, result.stdout, result.stderr))

    def test_install_dry_run_writes_nothing_and_apply_keeps_backup(self):
        output = self.tmp / "new-bin" / "magi"
        dry = self.install()
        self.assertIn("unchanged  mac:" + str(self.shim_path), dry.stdout)
        self.assertFalse(output.parent.exists())
        original = self.shim_path.read_text()
        self.shim_path.write_text("#!/bin/sh\nexit 91\n")
        self.shim_path.chmod(0o700)
        self.install("--apply")
        backup, = self.shim_path.parent.glob("magi.bak-*")
        self.assertEqual(backup.read_text(), "#!/bin/sh\nexit 91\n")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.shim_path.read_text(), original)
        self.assertEqual(self.shim_path.stat().st_mode & 0o777, 0o755)
        self.install("--apply")
        # Preserve an earlier backup across a later, differing installation.
        earlier = backup.with_name("magi.bak-20000101000000")
        backup.rename(earlier)
        backup = earlier
        self.shim_path.write_text("#!/bin/sh\nexit 92\n")
        self.install("--apply")
        self.assertEqual(backup.read_text(), "#!/bin/sh\nexit 91\n")
        self.assertEqual(self.shim_path.read_text(), original)

    def test_install_refuses_symlink_destination(self):
        output = self.tmp / "original-shim"
        self.shim_path.rename(output)
        self.shim_path.symlink_to(output)
        original = output.read_text()
        self.install("--apply", code=3)
        self.assertEqual(output.read_text(), original)
