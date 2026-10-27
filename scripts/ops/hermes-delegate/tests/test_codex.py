import io
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock
from delegate_test_support import Base, METACHARS, RUNNER_AVAILABLE, VALID_SPEC, git, make_repo
from delegate_test_support import codex
from delegate_test_support import common
from delegate_test_support import entry


class CodexArgvTests(Base):
    def codex_args(self, *extra):
        return codex.codex_parser().parse_args(
            ["--spec", "x", "--workdir", "x", "--class", "general", "--timeout", "900", *extra])

    def build(self, *extra, code_scope=("src/a.py",)):
        return codex.build_codex_argv(self.codex_args(*extra), spec="/w/SPEC.md", workdir="/w",
                                   allowed_root="/w", output_dir="/o/run", code_scope=list(code_scope))

    def test_argv_shape_and_gateway_recognition(self):
        argv = self.build("--risk", "high", "--ambiguity", "unknown", "--choice-reason", "needs care",
                          "--failure-kind", "shallow_reasoning", "--prior-model", "gpt-6.1-sol",
                          "--prior-effort", "high", "--attempt", "2", "--max-attempts", "3",
                          "--handoff-ref", "run-1", "--correction-evidence", "added test",
                          "--contract-resolved", "--sandbox", "workspace-write", "--timeout", "900")
        self.assertEqual(argv[:2], [str(common.RUNNER_PYTHON), str(common.RUNNER)])
        command = codex.render_command(argv)
        self.assertFalse(set(command) & METACHARS)
        self.assertTrue(codex.gateway_accepts(command))
        self.assertEqual(shlex.split(command), argv)
        for injected in ("--progress-manifest", "--progress-state-dir", "--progress-thread",
                         "--progress-label", "--progress-owner", "--dry-run"):
            self.assertNotIn(injected, argv)
        joined = " ".join(argv)
        self.assertIn("--progress-login-shell zsh", joined)
        self.assertIn("--progress-sender-profile koharu --progress-expected-bot-id " + common.EXPECTED_BOT_ID, joined)
        self.assertIn("--failure-kind shallow_reasoning --prior-model gpt-6.1-sol --prior-effort high", joined)
        self.assertIn("--attempt 2 --max-attempts 3", joined)
        self.assertIn("--progress-code-scope src/a.py", joined)

    def test_only_given_options_are_passed(self):
        argv = self.build(code_scope=())
        self.assertNotIn("--risk", argv)
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--progress-code-scope", argv)
        self.assertEqual(argv[argv.index("--task-class") + 1], "general")
        self.assertEqual(argv[argv.index("--timeout") + 1], "900")

    def test_task_class_alias_and_progress_code_scope_hint(self):
        args = codex.codex_parser().parse_args(["--spec", "x", "--workdir", "x", "--task-class", "bounded",
                                             "--timeout", "60"])
        self.assertEqual(args.task_class, "bounded")
        result = self.cli("codex", "--spec", "/x", "--workdir", "/x", "--class", "general",
                          "--progress-code-scope", "a.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("pass --code-scope", result.stderr)

    def test_timeout_is_required(self):
        result = self.cli("codex", "--spec", "/x", "--workdir", "/x", "--class", "general")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--timeout", result.stderr)

    def test_gateway_mirror_rejects_non_bare_commands(self):
        good = codex.render_command(self.build())
        self.assertTrue(codex.gateway_accepts(good))
        for bad in ("cd /w; " + good, good + " | tee x", "zsh -ic " + shlex.quote(good),
                    good + " --dry-run", good + " --progress-manifest /m", "X=$HOME " + good,
                    good.replace("/w", "~/w", 1), good + " #c"):
            self.assertFalse(codex.gateway_accepts(bad), bad)

    def test_render_refuses_metacharacters(self):
        argv = self.build()
        argv[argv.index("--spec") + 1] = "/w/$SPEC"
        with self.assertRaises(common.Fail):
            codex.render_command(argv)

    def test_rejects_model_effort_tier_and_overrides(self):
        for word in ("--model", "--effort", "--tier", "--tier=deep", "--pinned-model", "--pinned-effort",
                     "--selection", "--cli", "--advisor", "--dry-run", "--progress-manifest",
                     "--progress-thread=1"):
            result = self.cli("codex", "--spec", "/x", "--workdir", "/x", "--class", "general", word, "v")
            self.assertEqual(result.returncode, 2, word)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(result.stderr.strip().splitlines()), 1)
            self.assertTrue(result.stderr.startswith("hermes-delegate: " + word.split("=")[0]), result.stderr)

    def test_no_abbreviated_options(self):
        with self.assertRaises(common.Fail):
            codex.codex_parser().parse_args(["--spec", "x", "--workdir", "x", "--cla", "general"])

    def test_free_text_metacharacters_rejected(self):
        for value in ("a;b", "$(x)", "a`b`", "~", "a\nb", "it's", "x" * 513):
            with self.assertRaises(common.Fail):
                common.check_free_text("choice_reason", value)
        self.assertEqual(common.check_free_text("choice_reason", "plain words, ok"), "plain words, ok")


class CodexReportTests(Base):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp, "work")
        self.work.mkdir()
        self.spec = self.work / "SPEC.md"
        self.spec.write_text(VALID_SPEC, encoding="utf-8")
        self.output = Path(self.tmp, "output")
        self.runner = Path(self.tmp, "scripts/run_codex_task.py")
        self.runner.parent.mkdir()
        self.runner.touch()

    def codex(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(common, "RUNNER", self.runner), \
                mock.patch.object(common, "RUNNER_PYTHON", Path(sys.executable)), \
                mock.patch.object(codex, "check_worktree_root"), \
                mock.patch.object(codex, "resolve_code_scope", return_value=[]), \
                mock.patch.object(codex, "dry_run", return_value={}) as dry, \
                mock.patch.object(codex, "baseline_errors", return_value=[]), \
                mock.patch.object(codex, "ensure_output_dir", wraps=codex.ensure_output_dir) as ensure, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = entry.main(["codex", "--spec", str(self.spec), "--workdir", str(self.work),
                          "--class", "general", "--timeout", "900",
                          "--output-dir", str(self.output), *extra])
        return rc, out.getvalue(), err.getvalue(), ensure, dry

    def refusal(self, message, *extra):
        rc, out, err, ensure, dry = self.codex(*extra)
        self.assertEqual((rc, out, err), (2, "", "hermes-delegate: " + message + "\n"))
        ensure.assert_not_called()
        dry.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_codex_report_required_for_mechanical_and_bounded(self):
        for task_class in ("mechanical", "bounded"):
            with self.subTest(task_class=task_class):
                self.refusal("--report is required for --class mechanical and bounded",
                             "--class", task_class)
        self.refusal("--timeout must be a finite number of seconds in (0, 86400]",
                     "--class", "mechanical", "--timeout", "0")

    def test_codex_report_path_rejections(self):
        for report in ("", "x" * 257, "a\0b", "a\nb", "a\rb", "/report.md",
                       "../report.md", "a/../report.md", ".", "./", "a/.."):
            with self.subTest(report=report):
                message = "--report must be a relative path inside the workdir: " + report
                self.refusal(message.replace("\n", " "), "--report", report)

    def test_codex_report_needs_workspace_write(self):
        for sandbox in ([], ["--sandbox", "read-only"], ["--sandbox", "danger-full-access"]):
            with self.subTest(sandbox=sandbox):
                self.refusal("--report needs --sandbox workspace-write",
                             "--report", "report.md", *sandbox)

    def test_codex_report_must_be_named_in_spec(self):
        self.refusal("SPEC does not name the --report path: reports/entry.md",
                     "--report", "reports/entry.md", "--sandbox", "workspace-write")

    def test_codex_report_appended_before_progress_flags(self):
        for report in ("reports/entry.md", "./reports//entry.md", "reports/entry with spaces.md",
                       "é" * 256):
            self.spec.write_text(VALID_SPEC + "\nReport: " + report + "\n", encoding="utf-8")
            for task_class in ("mechanical", "bounded", "general"):
                with self.subTest(report=report, task_class=task_class):
                    extra = ["--class", task_class, "--sandbox", "workspace-write", "--report", report]
                    first = self.codex("--report", "discarded.md", *extra)
                    second = self.codex(*extra)
                    self.assertEqual((first[0], second[0]), (0, 0), first[2] + second[2])
                    self.assertEqual(first[1], second[1])
                    argv = shlex.split(first[1])
                    index = argv.index("--progress-login-shell")
                    self.assertEqual(argv[index - 2:index], ["--report", report])
                    self.assertEqual(argv.count("--report"), 1)
                    self.assertTrue(codex.gateway_accepts(first[1].strip()))
                    self.assertEqual(codex.render_command(argv) + "\n", first[1])
                    self.assertEqual(first[4].call_args[0][0], argv)

    def test_codex_report_absent_argv_unchanged(self):
        for task_class in ("general", "frontier"):
            with self.subTest(task_class=task_class):
                rc, out, err, _, _ = self.codex("--class", task_class)
                expected = [sys.executable, str(self.runner), "--spec", str(self.spec),
                            "--workdir", str(self.work), "--allowed-root", str(self.work),
                            "--output-dir", str(self.output), "--task-class", task_class,
                            "--timeout", "900", "--progress-login-shell", "zsh",
                            "--progress-sender-profile", common.SENDER_PROFILE,
                            "--progress-expected-bot-id", common.EXPECTED_BOT_ID]
                self.assertEqual(rc, 0, err)
                self.assertEqual(out, shlex.join(expected) + "\n")


class PathTests(Base):
    def test_symlink_rejected(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        with self.assertRaises(common.Fail) as caught:
            common.canonical(link, "--workdir", kind="dir")
        self.assertIn("symlink", str(caught.exception))
        with self.assertRaises(common.Fail):
            common.canonical("/tmp", "--workdir", kind="dir")

    def test_noncanonical_rejected(self):
        for value in (self.tmp + "/", self.tmp + "/../x", self.tmp + "//a", "relative/path"):
            with self.assertRaises(common.Fail):
                common.canonical(value, "--spec", must_exist=False)

    def test_symlink_reported_by_cli_on_one_line(self):
        result = self.cli("codex", "--spec", "/tmp/x", "--workdir", "/tmp", "--class", "general",
                          "--timeout", "900")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, r"^hermes-delegate: --workdir is a symlink.*\n$")


@unittest.skipUnless(RUNNER_AVAILABLE, "runner release not present")
class CodeScopeTests(Base):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)

    def test_directory_expands_to_runner_accepted_files(self):
        notes = []
        files = codex.resolve_code_scope(["src", "docs/x.md"], self.repo, notes)
        self.assertEqual(files, ["docs/x.md", "src/a.py", "src/pkg/b.ts"])
        self.assertTrue(any("skipped 1" in note for note in notes))

    def test_absolute_dir_and_untracked_file(self):
        Path(self.repo, "src/new.py").write_text("x\n", encoding="utf-8")
        files = codex.resolve_code_scope([self.repo + "/src/pkg", self.repo + "/src/new.py"], self.repo, [])
        self.assertEqual(files, ["src/new.py", "src/pkg/b.ts"])

    def test_explicit_non_source_file_fails(self):
        with self.assertRaises(common.Fail):
            codex.resolve_code_scope(["src/data.json"], self.repo, [])

    def test_outside_or_dotdot_or_symlink_fails(self):
        os.symlink(self.repo + "/src", self.repo + "/linked")
        for entry in ("../x.py", "src/../docs/x.md", "/private/tmp/elsewhere.py", "linked/a.py", "linked"):
            with self.assertRaises(common.Fail, msg=entry):
                codex.resolve_code_scope([entry], self.repo, [])

    def test_empty_directory_fails(self):
        os.mkdir(self.repo + "/empty")
        with self.assertRaises(common.Fail):
            codex.resolve_code_scope(["empty"], self.repo, [])


@unittest.skipUnless(RUNNER_AVAILABLE, "runner release not present")
class CodexDryRunTests(Base):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.out = os.path.join(self.tmp, "out")

    def codex(self, *extra, workdir=None, spec=None):
        return self.cli("codex", "--spec", spec or self.repo + "/SPEC.md", "--workdir", workdir or self.repo,
                        "--output-dir", self.out, "--timeout", "900", *extra)

    def test_end_to_end_prints_one_bare_line(self):
        Path(self.repo, "SPEC.md").write_text(VALID_SPEC + "\nReport: reports/entry.md\n", encoding="utf-8")
        result = self.codex("--class", "bounded", "--code-scope", "src",
                            "--report", "reports/entry.md", "--sandbox", "workspace-write")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(codex.gateway_accepts(lines[0]))
        self.assertIn("--timeout 900", lines[0])
        self.assertIn("model=gpt-6.1-terra effort=medium", result.stderr)
        self.assertEqual(os.stat(self.out).st_mode & 0o777, 0o700)

    def test_codex_report_required_for_remapped_class(self):
        for base, extra in (("complex", ()), ("frontier", ("--deeper-analysis-evidence", "prior sol high run stalled on the parser"))):
            with self.subTest(base=base):
                result = self.codex("--class", base, "--phase", "implementation", "--contract-resolved",
                                    "--implementation-class", "bounded", "--code-scope", "src", *extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("--report is required when the effective task class is bounded", result.stderr)
                self.assertFalse(os.path.exists(self.out))

    def test_codex_report_remapped_class_with_report_launches(self):
        Path(self.repo, "SPEC.md").write_text(VALID_SPEC + "\nReport: reports/entry.md\n", encoding="utf-8")
        result = self.codex("--class", "complex", "--phase", "implementation", "--contract-resolved",
                            "--implementation-class", "bounded", "--code-scope", "src",
                            "--report", "reports/entry.md", "--sandbox", "workspace-write")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("--report reports/entry.md", result.stdout)
        self.assertIn("task_class=bounded", result.stderr)

    def test_blocked_policy_maps_to_74(self):
        result = self.codex("--class", "frontier")
        self.assertEqual(result.returncode, 74)
        self.assertEqual(result.stdout, "")
        self.assertIn("xhigh requires explicit deeper-analysis evidence", result.stderr)
        self.assertFalse(os.path.exists(self.out), "created output dir must be removed on failure")

    def test_frontier_with_evidence_reaches_xhigh(self):
        result = self.codex("--class", "frontier", "--deeper-analysis-evidence", "two failed sol-high runs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("effort=xhigh", result.stderr)

    def test_skipped_policy_has_its_own_exit_code(self):
        result = self.codex("--class", "general", "--deterministic-tool-sufficient")
        self.assertEqual(result.returncode, common.EXIT_SKIPPED, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("status=skipped: no worker needed", result.stderr)

    def test_explicit_read_only_sandbox_is_dropped(self):
        result = self.codex("--class", "general", "--sandbox", "read-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--sandbox", result.stdout)
        self.assertIn("--sandbox read-only dropped", result.stderr)
        result = self.codex("--class", "general", "--sandbox", "danger-full-access")
        self.assertEqual(result.returncode, 2)

    def test_subdirectory_workdir_rejected_before_launch(self):
        Path(self.repo, "src/SPEC.md").write_text(VALID_SPEC, encoding="utf-8")
        result = self.codex("--class", "general", "--code-scope", "a.py",
                            workdir=self.repo + "/src", spec=self.repo + "/src/SPEC.md")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("must be a git worktree root", result.stderr)

    def test_non_git_workdir_rejected(self):
        plain = os.path.join(self.tmp, "plain")
        os.mkdir(plain)
        Path(plain, "SPEC.md").write_text(VALID_SPEC, encoding="utf-8")
        result = self.codex("--class", "general", workdir=plain, spec=plain + "/SPEC.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be a git worktree root", result.stderr)

    def test_baseline_matches_runner_collect(self):
        os.mkdir(self.out, 0o700)
        self.assertEqual(codex.baseline_errors(self.repo, self.repo, self.out, ["src/a.py"]), [])
        unborn = os.path.join(self.tmp, "unborn")
        os.makedirs(unborn)
        git(unborn, "init", "-q")
        Path(unborn, "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(codex.baseline_errors(unborn, unborn, self.out, []), [])
