import io
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import hermes_delegate as hd  # noqa: E402

SCRIPT = str(HERE / "hermes_delegate.py")
REAL_GATE = "/Users/choegeun-won/.local/share/review-gate/review_gate.py"
METACHARS = set("$`~;|&*?{}()#<>[]\n\r")
RUNNER_AVAILABLE = hd.RUNNER.exists() and hd.RUNNER_PYTHON.exists()
VALID_SPEC = """## External contracts
- N/A: This isolated fixture has no external contract.

## Failure paths
- Ordering/concurrency: Each operation is serialized and its result is checked.
- Interruption: The interrupted operation leaves no generated artifacts.
- Rejection/partial failure: Each rejected operation returns the expected error.
- Repeat/retry: Repeated operations return exactly the same expected result.
- External: N/A: No external services are called by this fixture.

## Acceptance
- Verify every expected result matches its exact expected value.
"""


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def make_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo + "/src/pkg")
    os.makedirs(repo + "/docs")
    for name, body in (("src/a.py", "print(1)\n"), ("src/pkg/b.ts", "export {}\n"),
                       ("src/data.json", "{}\n"), ("docs/x.md", "# x\n"), ("SPEC.md", VALID_SPEC)):
        Path(repo, name).write_text(body)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hd-test-", dir="/private/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cli(self, *argv, env=None):
        merged = dict(os.environ, **(env or {}))
        return subprocess.run([sys.executable, SCRIPT, *argv], capture_output=True, text=True,
                              env=merged, timeout=120)


class SpecGateTests(Base):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp, "work")
        self.docs = Path(self.tmp, "docs")
        self.work.mkdir()
        self.docs.mkdir()
        self.spec = self.docs / "SPEC.md"
        self.spec.write_text(VALID_SPEC)

    def problems(self, text=VALID_SPEC, kind="codex"):
        return hd.spec_problems(text, kind, self.work, self.docs)

    def contracts(self, items):
        return VALID_SPEC.replace("- N/A: This isolated fixture has no external contract.", items)

    def test_missing_sections_per_kind(self):
        for kind in ("codex", "claude", "qa"):
            with self.subTest(kind=kind):
                problems = self.problems("", kind)
                names = ["Acceptance"] if kind == "qa" else list(hd.SPEC_SECTIONS)
                self.assertEqual(problems, [name + ": missing section" for name in names])
                for name in names:
                    text = re.sub(r"## " + name + r"\n.*?(?=\n## |\Z)", "", VALID_SPEC, flags=re.S)
                    self.assertIn(name + ": missing section", self.problems(text, kind))
        self.assertEqual(self.problems("## Acceptance\n- Every expected result matches exactly.", "qa"), [])

    def test_duplicate_section(self):
        for kind in ("codex", "claude", "qa"):
            self.assertIn("Acceptance: duplicated section",
                          self.problems(VALID_SPEC + "\n## 완료 검증\n- Exact results match.", kind))

    def test_korean_and_case_insensitive_headings(self):
        self.assertEqual(self.problems(), [])
        text = VALID_SPEC
        for name, aliases in hd.SPEC_SECTIONS.items():
            text = text.replace("## " + name, "##  " + aliases[1] + "  ")
        self.assertEqual(self.problems(text), [])
        self.assertEqual(self.problems(VALID_SPEC.upper()), [])

    def test_only_exact_level_two_names_count(self):
        for title in ("### Acceptance", "# Acceptance", "## Acceptance extra", "## Acceptance ##"):
            self.assertIn("Acceptance: missing section", self.problems(VALID_SPEC.replace("## Acceptance", title)))

    def test_empty_sections_and_heading_boundaries(self):
        for name in hd.SPEC_SECTIONS:
            text = re.sub(r"(## " + name + r"\n).*?(?=\n## |\Z)", r"\1", VALID_SPEC, flags=re.S)
            self.assertIn(name + ": no items", self.problems(text))
        self.assertIn("Acceptance: no items", self.problems("## Acceptance\n# End\n- result", "qa"))
        self.assertIn("Acceptance: no items", self.problems("## Acceptance\n  - result", "qa"))

    def test_all_item_markers_and_multiline_fenced_capture(self):
        for marker in ("- ", "* ", "12. ", "12) "):
            self.assertEqual(self.problems(VALID_SPEC.replace("- ", marker)), [])
        text = "## Acceptance\n- Verify output:\n```text\n## Acceptance\n- exact result\n```\n"
        self.assertEqual(self.problems(text, "qa"), [])
        self.assertEqual(self.problems("```md\n## Acceptance\n- result\n```", "qa"),
                         ["Acceptance: missing section"])

    def test_contract_na_alone_and_mixed(self):
        for na in ("N/A: abcdefgh", "해당 없음： 가나다라마바 사아"):
            self.assertEqual(self.problems(self.contracts("- " + na)), [])
            problems = self.problems(self.contracts("- " + na + "\n- `missing`"))
            self.assertTrue(any("only item" in p for p in problems))
        self.assertTrue(self.problems(self.contracts("- N/A: a b c d e f g")))

    def test_nonexistent_contract_and_directory(self):
        for path in ("missing", str(self.work)):
            self.assertTrue(any("no resolving" in p for p in self.problems(self.contracts("- `" + path + "`"))))

    def test_invalid_line_cannot_hide_behind_valid_span(self):
        (self.work / "capture").write_bytes(b"\xff\nlast")
        for ref in ("capture:3", "capture:0", "capture:2-1", "capture:1-3", "missing:1"):
            with self.subTest(ref=ref):
                problems = self.problems(self.contracts("- `capture` and `" + ref + "`"))
                self.assertTrue(any("invalid file/line" in p for p in problems))
        for ref in ("capture:1", "capture:1-2", "capture:2"):
            self.assertEqual(self.problems(self.contracts("- `" + ref + "`")), [])

    def test_relative_workdir_and_spec_directory_references(self):
        (self.work / "work.txt").write_bytes(b"work\n")
        (self.docs / "docs.txt").write_bytes(b"docs\n")
        self.assertEqual(self.problems(self.contracts("- `work.txt:1`\n- `docs.txt:1`")), [])
        (self.work / "docs.txt").mkdir()
        self.assertEqual(self.problems(self.contracts("- `docs.txt:1`")), [])

    def test_absolute_and_home_references(self):
        capture = self.docs / "capture"
        capture.write_bytes(b"\xff\n")
        self.assertEqual(self.problems(self.contracts("- `{}:1`".format(capture))), [])
        with mock.patch.dict(os.environ, HOME=str(self.docs)):
            self.assertEqual(self.problems(self.contracts("- `~/capture:1`")), [])

    def test_unreadable_reference(self):
        capture = self.work / "capture"
        capture.write_bytes(b"data")
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertTrue(self.problems(self.contracts("- `capture:1`")))

    def test_each_failure_topic_missing(self):
        labels = ("Ordering/concurrency", "Interruption", "Rejection/partial failure", "Repeat/retry", "External")
        for topic, label in zip(hd.SPEC_TOPICS, labels):
            text = re.sub(r"^- " + re.escape(label) + r":.*\n", "", VALID_SPEC, flags=re.M)
            self.assertIn("Failure paths: missing {} topic".format(topic), self.problems(text))

    def test_short_failure_answer_and_na(self):
        text = VALID_SPEC.replace("Each operation is serialized and its result is checked.", "a b c")
        self.assertTrue(any("too-short ordering" in p for p in self.problems(text)))
        for answer in ("N/A: abcdefgh", "해당 없음： 가나다라마바사아", "123456789012345"):
            self.assertEqual(self.problems(text.replace("a b c", answer)), [])

    def test_korean_failure_labels(self):
        text = VALID_SPEC
        for old, new in zip(("Ordering/concurrency", "Interruption", "Rejection/partial failure", "Repeat/retry", "External:"),
                            ("동시성", "중단", "부분 실패", "재시도", "외부：")):
            text = text.replace(old, new)
        self.assertEqual(self.problems(text), [])

    def test_each_vague_acceptance_phrase(self):
        phrases = ("1건 이상", "한 건 이상", "하나 이상", "1개 이상", "최소 1", "at least one",
                   "at least 1", "one or more", "1 or more", ">= 1", ">=1", "≥ 1", "≥1", "etc.")
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                problems = self.problems("## Acceptance\n- " + phrase.upper(), "qa")
                self.assertTrue(any(repr(phrase) in p for p in problems), problems)
                problems = self.problems("## Acceptance\n- result (" + phrase + ") seen", "qa")
                self.assertTrue(any(repr(phrase) in p for p in problems), problems)

    def test_exact_acceptance_is_not_vague(self):
        for item in ("count >= 10 after the change", "count ≥10 rows", "11 or more files are listed",
                     "21건 이상 저장", "최소 10개", "bad input exits non-zero with message X",
                     "exit code가 0이 아님을 확인", "at least 12 lines", "v1.2etc.md is written"):
            with self.subTest(item=item):
                self.assertEqual(self.problems("## Acceptance\n- " + item, "qa"), [])

    def test_presence_only_acceptance_is_vague(self):
        for item in ("events>=1", "n≥1 rows", "count>=1", "row count is non-zero", "result is nonzero",
                     "count > 0", "count>0 after sync", "저장 건수가 0이 아님", "rows: non-zero",
                     "exits 0 and row count is non-zero", "the log has at least\n  one row", "1 or\n  more files",
                     "at least  one row", "1건\n  이상 저장", "1건이상 저장", "최소1건", "하나이상", "1개이상",
                     "list is non-empty", "목록이 비어 있지 않음", "목록이 비어있지 않음", "적어도 하나", "최소 한 개",
                     "count => 1", "1+ rows", "count != 0", "count ≠ 0", "count\n  > 0"):
            with self.subTest(item=item):
                self.assertTrue(any("vague phrase" in p for p in self.problems("## Acceptance\n- " + item, "qa")), item)
        for item in ("exits non-zero on bad input", "exit code is non-zero", "rc > 0 when the lock is held",
                     "exit status: nonzero", "returncode non-zero", "종료 코드가 0이 아님", "count >= 10",
                     "exit code\n  is non-zero", "rc != 0 when the lock is held", "11+ rows", "v1+2 build",
                     "등록된 행이 정확히 3개", "the file exists with 3 lines"):
            with self.subTest(item=item):
                self.assertEqual(self.problems("## Acceptance\n- " + item, "qa"), [])

    def test_markup_and_other_presence_phrasings_are_vague(self):
        for item in ("the log has at least `1` row", "`events` 테이블에 `1`건 이상 저장", "최소 `1`건 저장",
                     "at least **one** event per tap", "1건 *이상* 저장", "저장 건수가 1 이상", "이벤트가 한 개 이상",
                     "한 번 이상 기록됨", "1회 이상 호출", "1번 이상 호출", "the list is not empty", "row count is greater than 0",
                     "more than zero rows", "more than 0 rows", "행 수가 0보다 큼", "행 수가 0보다 커야 함", "0건 초과", "`count` > `0`",
                     "at least _one_ row", "list is ~~empty~~ not empty"):
            with self.subTest(item=item):
                self.assertTrue(any("vague phrase" in p for p in self.problems("## Acceptance\n- " + item, "qa")), item)
        for item in ("Python 3.1 이상에서 실행", "score is greater than 0.5", "`rc` != `0` when the lock is held",
                     "exit code is greater than 0", "`events` 테이블에 정확히 `3`건 저장", "`test_events_1` passes",
                     "11 이상 21 이하", "10건 초과 시 거절"):
            with self.subTest(item=item):
                self.assertEqual(self.problems("## Acceptance\n- " + item, "qa"), [])

    def test_non_path_colon_spans_are_ignored(self):
        Path(self.work, "real.txt").write_text("one\n")
        na = "- N/A: This isolated fixture has no external contract.\n"
        for span in ("127.0.0.1:18190", "localhost:8080", "LOCALHOST:3000", "[::1]:8080"):
            with self.subTest(span=span):
                text = VALID_SPEC.replace(na, "- `{}` and `real.txt:1`\n".format(span), 1)
                self.assertEqual(self.problems(text), [])
        for span in ("missing.py:3", "dir/missing:3", "~/nope/x:2", "real.txt:99", "Makefile:3", "example.com:443"):
            with self.subTest(span=span):
                text = VALID_SPEC.replace(na, "- `{}` and `real.txt:1`\n".format(span), 1)
                self.assertTrue(any("invalid file/line reference" in p for p in self.problems(text)), span)

    def test_acceptance_na_forbidden(self):
        for answer in ("N/A: abcdefgh", "해당 없음： abcdefgh"):
            self.assertTrue(any("N/A is not allowed" in p for p in self.problems("## Acceptance\n- " + answer, "qa")))

    def test_non_utf8_empty_and_oversize(self):
        self.spec.write_bytes(b"\xff")
        self.assertEqual(hd.read_spec_gate(self.spec, "codex", self.work)[1], ["SPEC: not UTF-8"])
        for data in (b"", b"x" * (1024 * 1024 + 1)):
            self.spec.write_bytes(data)
            self.assertIn("SPEC: must be non-empty and at most 1 MiB", hd.read_spec_gate(self.spec, "qa", self.work)[1])

    def test_all_problems_in_one_refusal(self):
        text = "## External contracts\n- `missing:9`\n## Failure paths\n- Order: short\n## Acceptance\n- at least one result etc.\n"
        self.spec.write_text(text)
        problems = self.problems(text)
        with self.assertRaises(hd.Fail) as caught:
            hd.require_spec_gate(self.spec, "codex", self.work)
        self.assertEqual(caught.exception.code, 2)
        message = str(caught.exception)
        self.assertIn("{} problem(s)".format(len(problems)), message)
        for i, problem in enumerate(problems, 1):
            self.assertIn("({}) {}".format(i, problem), message)
        self.assertIn("check with: hermes-delegate spec-check --spec", message)

    def test_item_excerpt_limited_to_sixty_characters(self):
        item = "x" * 90
        problems = self.problems("## Acceptance\n- " + item + " etc.", "qa")
        self.assertIn(repr(item[:60]), problems[0])
        self.assertNotIn(item[:61], problems[0])

    def test_spec_check_cli_pass_failure_and_defaults(self):
        (self.docs / "capture").write_bytes(b"line\n")
        self.spec.write_text(self.contracts("- `capture:1`"))
        result = self.cli("spec-check", "--spec", str(self.spec))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "ok\n", ""))
        self.spec.write_text("invalid")
        result = self.cli("spec-check", "--spec", str(self.spec))
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertEqual(result.stderr.splitlines(), self.problems("invalid"))
        self.spec.write_text("## Acceptance\n- Verify each expected result.")
        self.assertEqual(self.cli("spec-check", "--spec", str(self.spec), "--kind", "qa",
                                  "--workdir", str(self.work)).returncode, 0)

    def test_template_cli(self):
        result = self.cli("spec-check", "--template")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        for heading in hd.SPEC_SECTIONS:
            self.assertIn("## " + heading, result.stdout)
        for label in ("Ordering/concurrency", "Interruption", "Rejection/partial failure", "Repeat/retry", "External"):
            self.assertIn("- " + label + ":", result.stdout)

    def test_codex_refusal_has_no_output_or_scope_resolution(self):
        self.spec = self.work / "SPEC.md"
        self.spec.write_text("invalid")
        output = self.work / "output"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(hd, "RUNNER", self.spec), mock.patch.object(hd, "RUNNER_PYTHON", self.spec), \
                mock.patch.object(hd, "resolve_code_scope") as scope, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = hd.main(["codex", "--spec", str(self.spec), "--workdir", str(self.work),
                          "--class", "general", "--timeout", "900", "--output-dir", str(output)])
        self.assertEqual((rc, out.getvalue()), (2, ""))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("SPEC gate refused", err.getvalue())
        self.assertFalse(output.exists())
        scope.assert_not_called()

    def test_worker_refusal_creates_no_run_directory(self):
        self.spec.write_text("invalid")
        for kind in ("claude", "qa"):
            state = self.work / kind
            extra = ["--class", "ambiguous"] if kind == "claude" else []
            result = self.cli(kind, "--spec", str(self.spec), "--workdir", str(self.work),
                              "--timeout", "900", "--no-launch", *extra,
                              env={"HERMES_DELEGATE_STATE_ROOT": str(state)})
            self.assertEqual((result.returncode, result.stdout), (2, ""))
            self.assertIn("SPEC gate refused", result.stderr)
            self.assertFalse(state.exists())

    def test_workers_store_gated_bytes_even_if_source_changes(self):
        repo = make_repo(self.work)
        data = VALID_SPEC.replace("\n", "\r\n").encode("utf-8")
        for kind in ("claude", "qa"):
            self.spec.write_bytes(data)
            root = self.work / (kind + "-runs")
            root.mkdir()

            def state_root(_):
                self.spec.write_bytes(b"changed after validation")
                return str(root)

            extra = ["--class", "ambiguous"] if kind == "claude" else []
            with mock.patch.object(hd, "state_root", side_effect=state_root), \
                    mock.patch.object(hd, "worker_binary", return_value="/bin/echo"), \
                    mock.patch.object(hd, "GTIMEOUT", Path("/bin/echo")):
                self.assertEqual(hd.cmd_worker(kind, ["--spec", str(self.spec), "--workdir", repo,
                                                     "--timeout", "900", "--no-launch", *extra],
                                               out=io.StringIO(), err=io.StringIO()), 0)
            run = next(root.iterdir())
            self.assertEqual((run / "spec.md").read_bytes(), data)


class CodexArgvTests(Base):
    def codex_args(self, *extra):
        return hd.codex_parser().parse_args(
            ["--spec", "x", "--workdir", "x", "--class", "general", "--timeout", "900", *extra])

    def build(self, *extra, code_scope=("src/a.py",)):
        return hd.build_codex_argv(self.codex_args(*extra), spec="/w/SPEC.md", workdir="/w",
                                   allowed_root="/w", output_dir="/o/run", code_scope=list(code_scope))

    def test_argv_shape_and_gateway_recognition(self):
        argv = self.build("--risk", "high", "--ambiguity", "unknown", "--choice-reason", "needs care",
                          "--failure-kind", "shallow_reasoning", "--prior-model", "gpt-6.1-sol",
                          "--prior-effort", "high", "--attempt", "2", "--max-attempts", "3",
                          "--handoff-ref", "run-1", "--correction-evidence", "added test",
                          "--contract-resolved", "--sandbox", "workspace-write", "--timeout", "900")
        self.assertEqual(argv[:2], [str(hd.RUNNER_PYTHON), str(hd.RUNNER)])
        command = hd.render_command(argv)
        self.assertFalse(set(command) & METACHARS)
        self.assertTrue(hd.gateway_accepts(command))
        self.assertEqual(shlex.split(command), argv)
        for injected in ("--progress-manifest", "--progress-state-dir", "--progress-thread",
                         "--progress-label", "--progress-owner", "--dry-run"):
            self.assertNotIn(injected, argv)
        joined = " ".join(argv)
        self.assertIn("--progress-login-shell zsh", joined)
        self.assertIn("--progress-sender-profile koharu --progress-expected-bot-id " + hd.EXPECTED_BOT_ID, joined)
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
        args = hd.codex_parser().parse_args(["--spec", "x", "--workdir", "x", "--task-class", "bounded",
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
        good = hd.render_command(self.build())
        self.assertTrue(hd.gateway_accepts(good))
        for bad in ("cd /w; " + good, good + " | tee x", "zsh -ic " + shlex.quote(good),
                    good + " --dry-run", good + " --progress-manifest /m", "X=$HOME " + good,
                    good.replace("/w", "~/w", 1), good + " #c"):
            self.assertFalse(hd.gateway_accepts(bad), bad)

    def test_render_refuses_metacharacters(self):
        argv = self.build()
        argv[argv.index("--spec") + 1] = "/w/$SPEC"
        with self.assertRaises(hd.Fail):
            hd.render_command(argv)

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
        with self.assertRaises(hd.Fail):
            hd.codex_parser().parse_args(["--spec", "x", "--workdir", "x", "--cla", "general"])

    def test_free_text_metacharacters_rejected(self):
        for value in ("a;b", "$(x)", "a`b`", "~", "a\nb", "it's", "x" * 513):
            with self.assertRaises(hd.Fail):
                hd.check_free_text("choice_reason", value)
        self.assertEqual(hd.check_free_text("choice_reason", "plain words, ok"), "plain words, ok")


class CodexReportTests(Base):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp, "work")
        self.work.mkdir()
        self.spec = self.work / "SPEC.md"
        self.spec.write_text(VALID_SPEC)
        self.output = Path(self.tmp, "output")
        self.runner = Path(self.tmp, "scripts/run_codex_task.py")
        self.runner.parent.mkdir()
        self.runner.touch()

    def codex(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(hd, "RUNNER", self.runner), \
                mock.patch.object(hd, "RUNNER_PYTHON", Path(sys.executable)), \
                mock.patch.object(hd, "check_worktree_root"), \
                mock.patch.object(hd, "resolve_code_scope", return_value=[]), \
                mock.patch.object(hd, "dry_run", return_value={}) as dry, \
                mock.patch.object(hd, "baseline_errors", return_value=[]), \
                mock.patch.object(hd, "ensure_output_dir", wraps=hd.ensure_output_dir) as ensure, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = hd.main(["codex", "--spec", str(self.spec), "--workdir", str(self.work),
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
            self.spec.write_text(VALID_SPEC + "\nReport: " + report + "\n")
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
                    self.assertTrue(hd.gateway_accepts(first[1].strip()))
                    self.assertEqual(hd.render_command(argv) + "\n", first[1])
                    self.assertEqual(first[4].call_args[0][0], argv)

    def test_codex_report_absent_argv_unchanged(self):
        for task_class in ("general", "frontier"):
            with self.subTest(task_class=task_class):
                rc, out, err, _, _ = self.codex("--class", task_class)
                expected = [sys.executable, str(self.runner), "--spec", str(self.spec),
                            "--workdir", str(self.work), "--allowed-root", str(self.work),
                            "--output-dir", str(self.output), "--task-class", task_class,
                            "--timeout", "900", "--progress-login-shell", "zsh",
                            "--progress-sender-profile", hd.SENDER_PROFILE,
                            "--progress-expected-bot-id", hd.EXPECTED_BOT_ID]
                self.assertEqual(rc, 0, err)
                self.assertEqual(out, shlex.join(expected) + "\n")


class PathTests(Base):
    def test_symlink_rejected(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        with self.assertRaises(hd.Fail) as caught:
            hd.canonical(link, "--workdir", kind="dir")
        self.assertIn("symlink", str(caught.exception))
        with self.assertRaises(hd.Fail):
            hd.canonical("/tmp", "--workdir", kind="dir")

    def test_noncanonical_rejected(self):
        for value in (self.tmp + "/", self.tmp + "/../x", self.tmp + "//a", "relative/path"):
            with self.assertRaises(hd.Fail):
                hd.canonical(value, "--spec", must_exist=False)

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
        files = hd.resolve_code_scope(["src", "docs/x.md"], self.repo, notes)
        self.assertEqual(files, ["docs/x.md", "src/a.py", "src/pkg/b.ts"])
        self.assertTrue(any("skipped 1" in note for note in notes))

    def test_absolute_dir_and_untracked_file(self):
        Path(self.repo, "src/new.py").write_text("x\n")
        files = hd.resolve_code_scope([self.repo + "/src/pkg", self.repo + "/src/new.py"], self.repo, [])
        self.assertEqual(files, ["src/new.py", "src/pkg/b.ts"])

    def test_explicit_non_source_file_fails(self):
        with self.assertRaises(hd.Fail):
            hd.resolve_code_scope(["src/data.json"], self.repo, [])

    def test_outside_or_dotdot_or_symlink_fails(self):
        os.symlink(self.repo + "/src", self.repo + "/linked")
        for entry in ("../x.py", "src/../docs/x.md", "/private/tmp/elsewhere.py", "linked/a.py", "linked"):
            with self.assertRaises(hd.Fail, msg=entry):
                hd.resolve_code_scope([entry], self.repo, [])

    def test_empty_directory_fails(self):
        os.mkdir(self.repo + "/empty")
        with self.assertRaises(hd.Fail):
            hd.resolve_code_scope(["empty"], self.repo, [])


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
        Path(self.repo, "SPEC.md").write_text(VALID_SPEC + "\nReport: reports/entry.md\n")
        result = self.codex("--class", "bounded", "--code-scope", "src",
                            "--report", "reports/entry.md", "--sandbox", "workspace-write")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(hd.gateway_accepts(lines[0]))
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
        Path(self.repo, "SPEC.md").write_text(VALID_SPEC + "\nReport: reports/entry.md\n")
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
        self.assertEqual(result.returncode, hd.EXIT_SKIPPED, result.stderr)
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
        Path(self.repo, "src/SPEC.md").write_text(VALID_SPEC)
        result = self.codex("--class", "general", "--code-scope", "a.py",
                            workdir=self.repo + "/src", spec=self.repo + "/src/SPEC.md")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("must be a git worktree root", result.stderr)

    def test_non_git_workdir_rejected(self):
        plain = os.path.join(self.tmp, "plain")
        os.mkdir(plain)
        Path(plain, "SPEC.md").write_text(VALID_SPEC)
        result = self.codex("--class", "general", workdir=plain, spec=plain + "/SPEC.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be a git worktree root", result.stderr)

    def test_baseline_matches_runner_collect(self):
        os.mkdir(self.out, 0o700)
        self.assertEqual(hd.baseline_errors(self.repo, self.repo, self.out, ["src/a.py"]), [])
        unborn = os.path.join(self.tmp, "unborn")
        os.makedirs(unborn)
        git(unborn, "init", "-q")
        Path(unborn, "a.py").write_text("x\n")
        self.assertEqual(hd.baseline_errors(unborn, unborn, self.out, []), [])


STUB_CMUX = """#!/bin/sh
echo "$@" >> "$0.log"
case "$1" in
  ping) echo PONG ;;
  new-workspace) [ -e "$0.fail" ] && { echo "boom" >&2; exit 7; }; echo "OK workspace:9" ;;
esac
"""

STUB_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) [ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
       shift; printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; echo review-body
       if [ -n "${STUB_SLEEP:-}" ]; then sleep "$STUB_SLEEP"; fi ;;
esac
"""


class ReviewBase(Base):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.prompt = os.path.join(self.tmp, "prompt.md")
        Path(self.prompt).write_text("Review the diff.\n")
        self.state = os.path.join(self.tmp, "state")
        self.db = os.path.join(self.tmp, "ledger.sqlite")
        self.gate = os.path.join(self.tmp, "review-gate")
        Path(self.gate).write_text("#!/bin/sh\nexec /usr/bin/python3 {} \"$@\"\n".format(REAL_GATE))
        os.chmod(self.gate, 0o755)
        self.env = {"REVIEW_GATE_DB": self.db, "HERMES_DELEGATE_STATE_ROOT": self.state,
                    "HERMES_DELEGATE_REVIEW_GATE": self.gate,
                    "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def review(self, *extra, env=None):
        return self.cli("review", "--reviewer", "claude", "--item", "hd-test-item", "--commit",
                        self.head[:12], "--prompt", self.prompt, "--workdir", self.repo, "--no-launch",
                        *extra, env=env or self.env)

    def open_item(self):
        subprocess.run(["/usr/bin/python3", REAL_GATE, "open", "--item", "hd-test-item", "--goal", "t"],
                       env=dict(os.environ, REVIEW_GATE_DB=self.db), check=True, capture_output=True)

    def fields(self, stdout):
        return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line.split(" ")[0])

    def stub_cmux(self, fail=False):
        cmux = os.path.join(self.tmp, "cmux")
        Path(cmux).write_text(STUB_CMUX)
        os.chmod(cmux, 0o755)
        if fail:
            Path(cmux + ".fail").write_text("")
        return cmux

    def run_args(self, *extra, env=None):
        return self.cli("review", "--reviewer", "claude", "--item", "hd-test-item", "--commit",
                        self.head[:12], "--prompt", self.prompt, "--workdir", self.repo, *extra,
                        env=env or self.env)

    def stub_run(self, extra_env=None):
        stub = os.path.join(self.tmp, "stub-gate")
        Path(stub).write_text(STUB_GATE)
        os.chmod(stub, 0o755)
        result = self.review(env=dict(self.env, HERMES_DELEGATE_REVIEW_GATE=stub))
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.fields(result.stdout)["run_dir"], os.path.join(self.tmp, "stub.log")


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
        watcher = [line for line in result.stdout.splitlines() if line.startswith("watcher:")][0]
        self.assertIn("{} wait {} --timeout".format(hd.HD_BIN, run_dir), watcher)
        self.assertIn("notify_on_complete=true", watcher)
        self.assertIn("continue_on_complete=true, persist_on_release=true", watcher)
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("{} run --ticket {} --timeout 2700 -- ".format(self.gate, ticket), script)
        self.assertIn("-p --model opus --effort high --tools Read,Glob,Grep --allowedTools Read,Glob,Grep "
                      "--permission-mode dontAsk --strict-mcp-config", script)
        self.assertIn("export REVIEW_GATE_DB=" + self.db, script)
        self.assertNotIn(" start ", script)
        self.assertNotIn(" close ", script)
        self.assertNotIn("owner-approval", script)
        prompt = Path(run_dir, "prompt.md").read_text()
        self.assertTrue(prompt.startswith("Review the diff.\n"))
        self.assertIn(run_dir + "/diff.patch", prompt)
        self.assertIn(run_dir + "/log.txt", prompt)
        self.assertTrue(prompt.endswith("\nAPPROVED: no P0/P1\n"))
        launch = json.loads(Path(run_dir, "launch.json").read_text())
        self.assertEqual(launch[1:2], ["new-workspace"])
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        self.assertIn(shlex.quote(run_dir + "/inner.zsh"), launch[launch.index("--command") + 1])
        syntax = subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"], capture_output=True)
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
        Path(self.repo, "src/a.py").write_text("print(2)\n")
        git(self.repo, "commit", "-qam", "two")
        result = self.review()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not --commit", result.stderr)

    def test_inner_script_runs_gate_and_writes_rc(self):
        stub = os.path.join(self.tmp, "stub-gate")
        Path(stub).write_text(STUB_GATE)
        os.chmod(stub, 0o755)
        env = dict(self.env, HERMES_DELEGATE_REVIEW_GATE=stub)
        result = self.review(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        log = os.path.join(self.tmp, "stub.log")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(run_dir, "review.rc").read_text().strip(), "0")
        self.assertEqual(Path(run_dir, "review.out").read_text(), "review-body\n")
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text())["commit"], self.head)
        argv = Path(log).read_text().splitlines()
        self.assertEqual(argv[:5], ["--ticket", "0123456789abcdef0123456789abcdef", "--timeout", "2700", "--"])
        self.assertEqual(argv[5], str(hd.CLAUDE_BIN))
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertTrue(Path(log + ".stdin").read_text().startswith("Review the diff.\n"))
        waited = self.cli("wait", run_dir, "--timeout", "5")
        self.assertEqual(waited.returncode, 0)
        self.assertIn("review rc=0", waited.stdout)

    def test_invalid_label_rejected(self):
        result = self.review("--label", "bad label")
        self.assertEqual(result.returncode, 2)

    def test_dirty_tree_refused_without_ticket(self):
        self.open_item()
        Path(self.repo, "src/a.py").write_text("dirty\n")
        result = self.review()
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)
        Path(self.repo, "src/a.py").write_text("print(1)\n")
        Path(self.repo, "new.py").write_text("x\n")
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
        log = Path(cmux + ".log").read_text()
        self.assertEqual(log.count("new-workspace"), 2)
        self.assertIn("--focus false", log)
        Path(run_dir, "started_at").write_text("x\n")
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
        Path(self.repo, "src/a.py").write_text("print(3)\n")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(Path(run_dir, "review.rc").read_text().strip(), "2")
        self.assertIn("became dirty", Path(run_dir, "review.err").read_text())
        self.assertFalse(os.path.exists(log), "the gate must not run")

    def test_inner_script_writes_rc_on_hangup(self):
        run_dir, log = self.stub_run()
        proc = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                env=dict(os.environ, STUB_LOG=log, STUB_SLEEP="30"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not os.path.exists(log) and time.monotonic() < deadline:
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGHUP)
        self.assertEqual(proc.wait(timeout=20), 129)
        self.assertEqual(Path(run_dir, "review.rc").read_text().strip(), "129")
        self.assertIn("SIGHUP", Path(run_dir, "review.err").read_text())


STUB_CLAUDE = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"
echo changed >> src/a.py
echo '{"type":"system","subtype":"init","model":"claude-sonnet-5-5"}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit"}]}}'
printf '%s\\n' '{"type":"result","num_turns":2,"is_error":false,"permission_denials":[],"result":"done\\nIMPLEMENTED"}'
"""

STUB_CODEX = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; env > "$STUB_LOG.env"
final=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
printf 'QA report\\nQA: PASS ios\\n' > "$final"
echo '{"type":"thread.started"}'
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
echo '{"type":"item.completed","item":{"type":"agent_message"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":5}}'
"""


@unittest.skipUnless(hd.GTIMEOUT.exists(), "gtimeout missing")
class WorkerTests(Base):
    kind = "claude"

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.tmp)
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.spec = os.path.join(self.tmp, "SPEC.md")
        Path(self.spec).write_text(VALID_SPEC)
        self.state = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"HERMES_DELEGATE_STATE_ROOT": self.state, "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def worker(self, *extra, env=None, kind=None):
        kind = kind or self.kind
        base = ["--spec", self.spec, "--workdir", self.repo, "--timeout", "900"]
        if kind == "claude" and not any(word.startswith("--class") for word in extra):
            base += ["--class", "ambiguous"]
        return self.cli(kind, *base, *extra, env=dict(self.env, **(env or {})))

    def fields(self, stdout):
        return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line.split(" ")[0])

    def prepared(self, *extra, env=None, kind=None):
        result = self.worker("--no-launch", *extra, env=env, kind=kind)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, self.fields(result.stdout)["run_dir"]

    def stub(self, name, body):
        path = os.path.join(self.tmp, name)
        Path(path).write_text(body)
        os.chmod(path, 0o755)
        return path

    def run_inner(self, run_dir):
        return subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=self.log),
                              capture_output=True, text=True, timeout=60)


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
        self.assertEqual(watcher, ['watcher: terminal(command="{} wait {} --timeout 1800", background=true, '
                                   'notify_on_complete=true, continue_on_complete=true, persist_on_release=true)'
                                   .format(hd.HD_BIN, run_dir)])
        self.assertIn("class=ambiguous model=opus effort=medium advisor=off", result.stderr)
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("{} -k 60 900 {} -p --model opus --effort medium --tools Read,Glob,Grep,Edit,Write,Bash "
                      "--allowedTools Bash --permission-mode acceptEdits --permission-prompts none "
                      "--strict-mcp-config".format(hd.GTIMEOUT, hd.CLAUDE_BIN), script)
        self.assertIn("< {0}/spec.md > {0}/claude.out 2>> {0}/claude.err".format(run_dir), script)
        for word in ("--dangerously-skip-permissions", "bypassPermissions", "--advisor", "review-gate",
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
            self.assertNotIn(word, script)
        self.assertIn("[[ \"$(", script)
        self.assertIn(self.head, script)
        self.assertEqual(Path(run_dir, "spec.md").read_text(), VALID_SPEC)
        launch = json.loads(Path(run_dir, "launch.json").read_text())
        self.assertEqual(launch[:4], ["/nonexistent/cmux", "new-workspace", "--name", "claude-repo"])
        self.assertEqual(launch[launch.index("--cwd") + 1], self.repo)
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        self.assertEqual(launch[launch.index("--command") + 1],
                         "/bin/zsh -ic " + shlex.quote("exec /bin/zsh " + shlex.quote(run_dir + "/inner.zsh")))
        meta = json.loads(Path(run_dir, "meta.json").read_text())
        self.assertEqual((meta["kind"], meta["class"], meta["model"], meta["effort"], meta["advisor"],
                          meta["head"]), ("claude", "ambiguous", "opus", "medium", None, self.head))
        syntax = subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"], capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_class_table_is_the_only_model_source(self):
        for name, policy in hd.CLAUDE_IMPL_POLICY.items():
            _, run_dir = self.prepared("--class", name, "--label", "c-" + name)
            script = Path(run_dir, "inner.zsh").read_text()
            self.assertIn("-p --model {} --effort {} ".format(policy["model"], policy["effort"]), script)
        self.assertEqual(hd.CLAUDE_IMPL_POLICY["judgment"]["model"], "opus")
        self.assertEqual(hd.CLAUDE_IMPL_POLICY["ambiguous"], {"model": "opus", "effort": "medium"})
        self.assertEqual(hd.CLAUDE_IMPL_POLICY["mechanical"], {"model": "sonnet", "effort": "medium"})
        self.assertEqual(hd.CLAUDE_IMPL_POLICY["judgment"], {"model": "opus", "effort": "high"})
        result = self.worker("--class", "frontier")
        self.assertEqual(result.returncode, 2)

    def test_advisor_is_opt_in_with_per_run_settings(self):
        _, run_dir = self.prepared("--class", "mechanical", "--advisor", "opus")
        line = next(l for l in Path(run_dir, "inner.zsh").read_text().splitlines() if " -p " in l)
        argv = shlex.split(line)
        self.assertEqual(argv[argv.index("--advisor") + 1], "opus")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         {"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": ""}})
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text())["advisor"], "opus")
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
        Path(empty).write_text("")
        result = self.cli("claude", "--spec", empty, "--workdir", self.repo, "--class", "ambiguous",
                          "--timeout", "900", "--no-launch", env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, 2)

    def test_dirty_or_non_root_worktree_refused(self):
        Path(self.repo, "src/a.py").write_text("dirty\n")
        result = self.worker("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)
        Path(self.repo, "src/a.py").write_text("print(1)\n")
        Path(self.repo, "new.py").write_text("x\n")
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
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "0")
        argv = Path(self.log).read_text().splitlines()
        self.assertEqual(argv[:5], ["-p", "--model", "opus", "--effort", "medium"])
        self.assertIn('{"mcpServers":{}}', argv)
        self.assertEqual(Path(self.log + ".stdin").read_text(), VALID_SPEC)
        waited = self.cli("wait", run_dir, "--timeout", "5")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("claude rc=0", waited.stdout)
        self.assertIn("turns=2 tool_uses=1 (Edit:1)", waited.stdout)
        self.assertIn("last_line=IMPLEMENTED", waited.stdout)
        self.assertIn("model=claude-sonnet-5-5", waited.stdout)
        self.assertIn("worktree: HEAD {} unchanged, 1 uncommitted entries".format(self.head[:12]), waited.stdout)
        self.assertEqual(Path(run_dir, "claude.result.md").read_text(), "done\nIMPLEMENTED")

    def test_inner_script_refuses_changed_tree(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        Path(self.repo, "src/a.py").write_text("print(3)\n")
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "2")
        self.assertIn("became dirty", Path(run_dir, "claude.err").read_text())
        self.assertFalse(os.path.exists(self.log), "the worker must not run")

    def test_launch_through_stub_cmux_and_launch_run(self):
        cmux = self.stub("cmux", STUB_CMUX)
        Path(cmux + ".fail").write_text("")
        result = self.worker(env={"HERMES_DELEGATE_CMUX": cmux})
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing ran: retry with hermes-delegate claude --launch-run", result.stderr)
        run_dir = os.path.join(self.state, "claude", os.listdir(self.state + "/claude")[0])
        os.remove(cmux + ".fail")
        env = dict(os.environ, **dict(self.env, HERMES_DELEGATE_CMUX=cmux))
        relaunch = self.cli("claude", "--launch-run", run_dir, env=env)
        self.assertEqual(relaunch.returncode, 0, relaunch.stderr)
        self.assertIn("launched=yes", relaunch.stdout)
        self.assertIn("rc={}/claude.rc".format(run_dir), relaunch.stdout)
        log = Path(cmux + ".log").read_text()
        self.assertEqual(log.count("new-workspace"), 2)
        self.assertIn("--name claude-repo", log)
        self.assertIn("--focus false", log)
        Path(run_dir, "started_at").write_text("x\n")
        again = self.cli("claude", "--launch-run", run_dir, env=env)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already started", again.stderr)
        foreign = self.cli("qa", "--launch-run", run_dir, env=env)
        self.assertEqual(foreign.returncode, 1)

    def test_never_started_names_launch_run(self):
        _, run_dir = self.prepared()
        with self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "5", "--start-grace", "1"], out=io.StringIO(), err=io.StringIO())
        self.assertEqual(caught.exception.code, 90)
        self.assertIn("hermes-delegate claude --launch-run " + run_dir, str(caught.exception))


class QaTests(WorkerTests):
    kind = "qa"

    def test_generates_fixed_astra_script(self):
        result, run_dir = self.prepared("--label", "silvia-ios")
        fields = self.fields(result.stdout)
        self.assertTrue(run_dir.startswith(self.state + "/qa/silvia-ios-"))
        self.assertEqual(fields["rc"], run_dir + "/qa.rc")
        self.assertIn("watcher: terminal(command=\"{} wait {} --timeout 1800\"".format(hd.HD_BIN, run_dir),
                      result.stdout)
        self.assertIn("model=gpt-6-astra effort=high sandbox=danger-full-access", result.stderr)
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("export ASDF_NODEJS_VERSION=22.19.0\nexport PATH={}:$PATH\n".format(hd.CODEX_NODE_BIN),
                      script)
        self.assertIn("{} -k 60 900 {} exec --ephemeral -m gpt-6-astra -c 'model_reasoning_effort=\"high\"' "
                      "--sandbox danger-full-access -C {} --json -o {}/qa.final.md - < {}/spec.md > {}/qa.out"
                      .format(hd.GTIMEOUT, hd.CODEX_BIN, self.repo, run_dir, run_dir, run_dir), script)
        launch = json.loads(Path(run_dir, "launch.json").read_text())
        self.assertEqual(launch[1:4], ["new-workspace", "--name", "qa-silvia-ios"])
        self.assertEqual(launch[launch.index("--focus") + 1], "false")
        meta = json.loads(Path(run_dir, "meta.json").read_text())
        self.assertEqual((meta["kind"], meta["model"], meta["effort"], meta["sandbox"]),
                         ("qa", "gpt-6-astra", "high", "danger-full-access"))
        self.assertNotIn("class", meta)
        self.assertEqual(subprocess.run(["/bin/zsh", "-n", run_dir + "/inner.zsh"]).returncode, 0)

    def test_rejects_any_model_effort_or_sandbox_choice(self):
        for word in ("--model", "--effort", "--effort=xhigh", "--class", "--task-class", "--advisor", "--sandbox",
                     "--pinned-model", "--tier"):
            result = self.worker(word, "x")
            self.assertEqual(result.returncode, 2, word)
            self.assertEqual(result.stdout, "")
            self.assertIn("device QA is fixed at Codex gpt-6-astra/high", result.stderr)
        self.assertFalse(os.path.exists(self.state + "/qa"))

    def test_dirty_tree_refused(self):
        Path(self.repo, "untracked.txt").write_text("x\n")
        result = self.worker("--no-launch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted or untracked changes", result.stderr)

    def test_inner_script_runs_codex_and_wait_summarizes(self):
        stub = self.stub("stub-codex", STUB_CODEX)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CODEX_BIN": stub})
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = Path(self.log).read_text().splitlines()
        self.assertEqual(argv[:8], ["exec", "--ephemeral", "-m", "gpt-6-astra", "-c",
                                    'model_reasoning_effort="high"', "--sandbox", "danger-full-access"])
        self.assertIn("ASDF_NODEJS_VERSION=22.19.0", Path(self.log + ".env").read_text())
        self.assertEqual(Path(self.log + ".stdin").read_text(), VALID_SPEC)
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("qa rc=0", text)
        self.assertIn("items=(agent_message:1,command_execution:1)", text)
        self.assertIn("last_line=QA: PASS ios", text)
        self.assertIn("worktree: HEAD {} unchanged, 0 uncommitted entries".format(self.head[:12]), text)
        self.assertNotIn("WARNING", text)

    def test_summary_warns_without_commands(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "qa.out").write_text('{"type":"error","message":"stream disconnected"}\n')
        summary = hd.summarize_codex(run_dir + "/qa.out", run_dir)
        self.assertIn("error=stream disconnected", summary)
        self.assertIn("result=missing", summary[0])
        self.assertTrue(any("ran no commands" in line for line in summary))


class WaitTests(Base):
    def test_wakes_on_rc_file(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "review.err").write_text("review-gate: boom\n")

        def finish():
            time.sleep(0.5)
            Path(run_dir, "review.rc.tmp").write_text("3\n")
            os.rename(run_dir + "/review.rc.tmp", run_dir + "/review.rc")

        threading.Thread(target=finish).start()
        out = io.StringIO()
        started = time.monotonic()
        code = hd.cmd_wait([run_dir, "--timeout", "20"], out=out, err=io.StringIO())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 3)
        self.assertIn("review rc=3", out.getvalue())
        self.assertIn("review-gate: boom", out.getvalue())

    def test_times_out_after_start(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "started_at").write_text("2026-10-20T00:00:00+0900\n")
        out = io.StringIO()
        with self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "1"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, 91)
        self.assertEqual(out.getvalue().splitlines()[-2:], ["status=no_rc", hd.watcher_line(run_dir, 1)])
        self.assertIn("started at 2026-10-20", str(caught.exception))

    def test_never_started_fails_early(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        started = time.monotonic()
        out = io.StringIO()
        with self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "60", "--start-grace", "1"], out=out, err=io.StringIO())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.code, 90)
        self.assertEqual(out.getvalue(), "status=never_started\n")
        self.assertIn("never started", str(caught.exception))

    def test_stream_json_summary(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        events = [{"type": "system", "subtype": "init"},
                  {"type": "assistant", "message": {"content": [
                      {"type": "tool_use", "name": "Read", "input": {}},
                      {"type": "tool_use", "name": "Grep", "input": {}}]}},
                  {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                  {"type": "result", "num_turns": 4, "is_error": False, "permission_denials": [],
                   "result": "No P0/P1.\nVERDICT: APPROVED"}]
        Path(run_dir, "review.out").write_text("".join(json.dumps(e) + "\n" for e in events))
        Path(run_dir, "started_at").write_text("x\n")
        Path(run_dir, "review.rc").write_text("0\n")
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("turns=4 tool_uses=3 (Grep:1,Read:2)", text)
        self.assertIn("verdict_line=VERDICT: APPROVED", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(Path(run_dir, "review.result.md").read_text(), "No P0/P1.\nVERDICT: APPROVED")
        Path(run_dir, "review.out").write_text(json.dumps(events[-1]) + "\n")
        out = io.StringIO()
        hd.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO())
        self.assertIn("WARNING: the reviewer used no tools", out.getvalue())


FAKE_TOKEN = "sk-ant-oat01-" + "Zz9" * 30

STUB_SLOW_CLAUDE = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
trap 'echo got-term >> "$STUB_LOG.term"; exit 7' TERM
echo started > "$STUB_LOG"
sleep 60 &
wait
"""

STUB_SLOW_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) [ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
       trap 'echo got-term >> "$STUB_LOG.term"; exit 5' TERM
       echo started > "$STUB_LOG"; sleep 60 & wait ;;
esac
"""

STUB_EXEC_GATE = """#!/bin/sh
case "$1" in
  start) echo 0123456789abcdef0123456789abcdef ;;
  run) while [ "$1" != "--" ]; do shift; done; shift; exec "$@" ;;
esac
"""

STUB_ENV_CLAUDE = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
cat > /dev/null
printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN:-}" | wc -c | tr -d ' ' > "$STUB_LOG.toklen"
echo "api_key=${ANTHROPIC_API_KEY:-unset} base_url=${ANTHROPIC_BASE_URL:-unset}" > "$STUB_LOG.anthropic"
if [ -n "${STUB_LEAK:-}" ]; then echo "leak $CLAUDE_CODE_OAUTH_TOKEN"; fi
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}'
printf '%s\\n' '{"type":"result","num_turns":1,"is_error":false,"permission_denials":[],"result":"APPROVED: no P0/P1"}'
"""

STUB_REVIEW_CODEX = """#!/bin/sh
[ -n "$STUB_LOG" ] || { echo "STUB_LOG is unset" >&2; exit 97; }
printf '%s\\n' "$@" > "$STUB_LOG"; cat > "$STUB_LOG.stdin"; env > "$STUB_LOG.env"
final=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
printf 'No findings.\\nAPPROVED: no P0/P1\\n' > "$final"
echo '{"type":"item.completed","item":{"type":"command_execution"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":5}}'
"""

STUB_SMOKE = """#!/bin/sh
for a in "$@"; do [ "$prev" = "-o" ] && final="$a"; prev="$a"; done
if [ -n "${STUB_SMOKE_FAIL:-}" ]; then echo "auth failed" >&2; exit 3; fi
if [ -n "$final" ]; then echo ok > "$final"; else echo ok; fi
"""


def wait_for(path, seconds=15):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


def no_process(pattern):
    found = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True)
    return found.returncode == 1


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
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("kill -TERM -- -$child", script)
        self.assertIn("&\nchild=$!\nwait $child\n", script)
        proc = self.start_inner(run_dir)
        self.assertTrue(wait_for(self.log))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 7)
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "7")
        self.assertIn("interrupted by SIGTERM; sent SIGTERM to the worker group, which exited 7",
                      Path(run_dir, "claude.err").read_text())
        self.assertEqual(Path(self.log + ".term").read_text(), "got-term\n")
        time.sleep(0.3)
        self.assertTrue(no_process(self.marker), "worker left running")

    def test_review_term_goes_to_review_gate_pid(self):
        gate = self.slow_stub(STUB_SLOW_GATE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n")
        result = self.cli("review", "--reviewer", "claude", "--item", "hd-sig", "--commit", self.head[:12],
                          "--prompt", prompt, "--workdir", self.repo, "--no-launch",
                          env=dict(os.environ, **dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate)))
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("kill -TERM $child", script)
        self.assertNotIn("kill -TERM -- -$child", script)
        proc = self.start_inner(run_dir)
        self.assertTrue(wait_for(self.log))
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 5)
        self.assertEqual(Path(run_dir, "review.rc").read_text().strip(), "5")
        self.assertIn("sent SIGTERM to the review-gate run, which exited 5", Path(run_dir, "review.err").read_text())
        self.assertEqual(Path(self.log + ".term").read_text(), "got-term\n")

    def test_term_before_target_start_uses_signal_code(self):
        _, run_dir = self.prepared()
        script = Path(run_dir, "inner.zsh").read_text()
        hold = "sleep 30 &\nprint -r -- $! > {0}/sleep.pid\nwait $!\ndate '+%Y".format(run_dir)
        Path(run_dir, "inner.zsh").write_text(script.replace("date '+%Y", hold, 1))
        proc = self.start_inner(run_dir)
        try:
            self.assertTrue(wait_for(run_dir + "/sleep.pid"))
            time.sleep(0.2)
            os.kill(proc.pid, signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=30), 143)
        finally:
            if os.path.exists(run_dir + "/sleep.pid"):
                try:
                    os.kill(int(Path(run_dir, "sleep.pid").read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "143")
        self.assertFalse(os.path.exists(run_dir + "/started_at"))

    def test_lock_blocks_second_launch_without_rc(self):
        stub = self.stub("stub-claude", STUB_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        os.mkdir(run_dir + "/lock")
        done = self.run_inner(run_dir)
        self.assertEqual(done.returncode, hd.EXIT_LOCKED)
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"))
        self.assertFalse(os.path.exists(run_dir + "/started_at"))
        self.assertIn("already launched (lock exists)", Path(run_dir, "claude.err").read_text())
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
        Path(run_dir, "claude.rc").write_text("0\n")
        second = self.run_inner(run_dir)
        self.assertEqual(second.returncode, hd.EXIT_LOCKED)
        self.assertEqual(Path(run_dir, "claude.rc").read_text(), "0\n")


class CmuxCloseTests(WorkerTests):
    def run_with_rc(self, rc):
        cmux = self.stub("cmux", STUB_CMUX)
        stub = self.stub("stub-claude", STUB_CLAUDE)
        env = {"HERMES_DELEGATE_CMUX": cmux, "HERMES_DELEGATE_CLAUDE_BIN": stub}
        result = self.worker(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("via=cmux", result.stdout)
        run_dir = self.fields(result.stdout)["run_dir"]
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text())["cmux_workspace"], "workspace:9")
        Path(run_dir, "started_at").write_text("x\n")
        Path(run_dir, "claude.rc").write_text("{}\n".format(rc))
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **dict(self.env, **env)))
        return waited, Path(cmux + ".log").read_text()

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


class ReviewInputsTests(ReviewBase):
    def test_diff_and_log_files_without_base(self):
        self.open_item()
        result = self.review()
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        show = subprocess.run(["git", "-C", self.repo, "show", self.head], capture_output=True).stdout
        self.assertEqual(Path(run_dir, "diff.patch").read_bytes(), show)
        self.assertIn(b"init", Path(run_dir, "log.txt").read_bytes())
        prompt = Path(run_dir, "prompt.md").read_text()
        self.assertIn("(git show {})".format(self.head[:12]), prompt)
        for rule in ("Report only P0/P1", "file:line and a concrete trigger", "Round 2 or later",
                     "regressions introduced by those fixes", "defects in the new diff"):
            self.assertIn(rule, prompt)
        self.assertEqual(prompt.splitlines()[-1], "APPROVED: no P0/P1")

    def test_diff_with_base(self):
        base = self.head
        Path(self.repo, "src/a.py").write_text("print(2)\n")
        git(self.repo, "commit", "-qam", "two")
        self.head = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.open_item()
        result = self.review("--base", base[:12])
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        diff = subprocess.run(["git", "-C", self.repo, "diff", "--binary", base, self.head],
                              capture_output=True).stdout
        self.assertEqual(Path(run_dir, "diff.patch").read_bytes(), diff)
        log = Path(run_dir, "log.txt").read_text()
        self.assertIn("two", log)
        self.assertNotIn("    init", log)
        self.assertIn("git diff --binary {} {}".format(base[:12], self.head[:12]),
                      Path(run_dir, "prompt.md").read_text())

    def test_reviewer_tools_unchanged_but_run_dir_readable(self):
        self.open_item()
        run_dir = self.fields(self.review().stdout)["run_dir"]
        line = next(l for l in Path(run_dir, "inner.zsh").read_text().splitlines() if " run --ticket " in l)
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
        meta = json.loads(Path(self.fields(result.stdout)["run_dir"], "meta.json").read_text())
        self.assertEqual((meta["reviewer"], meta["via"]), ("claude", "cmux"))


class OwnerApprovalTests(ReviewBase):
    approval = "1300000000000000001"

    def close(self, ticket, *verdict):
        subprocess.run(["/usr/bin/python3", REAL_GATE, "close", "--ticket", ticket, *verdict],
                       env=dict(os.environ, REVIEW_GATE_DB=self.db), check=True, capture_output=True)

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
        meta = json.loads(Path(fields["run_dir"], "meta.json").read_text())
        self.assertEqual(meta["owner_approval"], self.approval)
        self.assertNotIn("owner-approval", Path(fields["run_dir"], "inner.zsh").read_text())
        self.close(fields["ticket"], "--verdict", "BLOCK", "--blockers", "1")
        reused = self.check("--owner-approval", self.approval)
        self.assertEqual(reused.returncode, 2)
        self.assertIn("Owner approval {} was already used".format(self.approval), reused.stderr)
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
        self.assertEqual(hd.start_refusal({"item": item, "tickets": [dict(closed, status="ran", verdict=None)],
                                           "rounds_used": 1}, self.approval), hd.PENDING_REFUSAL)
        state = {"item": item, "tickets": [closed, closed], "rounds_used": 2}
        self.assertIn("--owner-approval", hd.start_refusal(state))
        self.assertIsNone(hd.start_refusal(state, self.approval))
        used = dict(state, tickets=[closed, dict(closed, owner_approval=self.approval)], rounds_used=3)
        self.assertIn("already used", hd.start_refusal(used, self.approval))

    def test_failed_review_routes_to_fallback_in_the_mirror(self):
        item = {"id": "x-item", "status": "open"}
        failed = {"round": 1, "status": "closed", "verdict": "UNREVIEWED", "fallback": 0,
                  "closed_at": "2026-10-26T10:00:00.000002+00:00", "owner_approval": None}
        passed = dict(failed, verdict="PASS", closed_at="2026-10-26T10:00:00.000001+00:00")
        state = {"item": item, "tickets": [passed, failed], "rounds_used": 1}
        self.assertIn("hermes-delegate review --fallback", hd.start_refusal(state))
        self.assertIsNone(hd.start_refusal(state, self.approval))
        self.assertIsNone(hd.start_refusal(state, fallback=True))
        self.assertIn("different reviewer", hd.start_refusal(
            dict(state, tickets=[passed, dict(failed, reviewer="claude")]), fallback=True, reviewer="claude"))
        # PASS closed last: review-gate refuses the fallback, so the new round stays open.
        pass_last = dict(state, tickets=[failed, dict(passed, closed_at="2026-10-26T10:00:01+00:00")])
        self.assertIsNone(hd.start_refusal(pass_last))
        self.assertIn("latest closed ticket", hd.start_refusal(pass_last, fallback=True))
        blocked = dict(state, tickets=[dict(passed, verdict="BLOCK"), failed])
        self.assertIsNone(hd.start_refusal(blocked))
        used = dict(state, tickets=[failed, dict(failed, fallback=1, closed_at="2026-10-26T10:00:02+00:00")])
        self.assertIn("already used", hd.start_refusal(used, fallback=True))
        self.assertIn("latest closed ticket", hd.start_refusal({"item": item, "tickets": [], "rounds_used": 0},
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
        self.assertTrue(json.loads(Path(fields["run_dir"], "meta.json").read_text())["fallback"])


class DefaultViaTests(unittest.TestCase):
    def test_claude_default_is_stated_once_in_code(self):
        worker_default = hd.worker_parser("claude").parse_args(
            ["--spec", "s", "--workdir", "w", "--class", "ambiguous", "--timeout", "900"]).via
        self.assertEqual(worker_default, hd.REVIEW_POLICY["claude"]["via"],
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
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("export ASDF_NODEJS_VERSION=22.19.0\nexport PATH={}:$PATH\n".format(hd.CODEX_NODE_BIN), script)
        self.assertIn("{} run --ticket {} --timeout 2700 -- {} exec --ephemeral -s read-only -m gpt-6.1-sol -c "
                      "'model_reasoning_effort=\"high\"' -C {} --json -o {}/review.final.md -".format(
                          self.gate, fields["ticket"], hd.CODEX_BIN, self.repo, run_dir), re.sub(r" -c mcp_servers\.[A-Za-z0-9_-]+\.enabled=false", "", script))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", script)
        meta = json.loads(Path(run_dir, "meta.json").read_text())
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
        Path(gate).write_text(STUB_EXEC_GATE)
        os.chmod(gate, 0o755)
        codex = os.path.join(self.tmp, "stub-codex")
        Path(codex).write_text(STUB_REVIEW_CODEX)
        os.chmod(codex, 0o755)
        env = dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate, HERMES_DELEGATE_CODEX_BIN=codex,
                   STUB_LOG=os.path.join(self.tmp, "preflight.log"))
        result = self.codex_review("--no-launch", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        run_dir = self.fields(result.stdout)["run_dir"]
        log = os.path.join(self.tmp, "stub.log")
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], env=dict(os.environ, STUB_LOG=log),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ASDF_NODEJS_VERSION=22.19.0", Path(log + ".env").read_text())
        self.assertIn("APPROVED: no P0/P1", Path(log + ".stdin").read_text())
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("review rc=0", text)
        self.assertIn("verdict_line=APPROVED: no P0/P1", text)
        self.assertIn("items=(command_execution:1)", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(Path(run_dir, "review.result.md").read_text(), "No findings.\nAPPROVED: no P0/P1\n")

    def test_codex_review_direct_launch_skips_cmux(self):
        self.open_item()
        result = self.codex_review("--check", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN="/bin/echo"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviewer=codex via=direct", result.stdout)
        self.assertIn("smoke codex --via direct", result.stdout)
        missing = self.codex_review("--check", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN="/nonexistent/codex"))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("CLI is missing", missing.stderr)


class DirectTokenTests(WorkerTests):
    def setUp(self):
        super().setUp()
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n")
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
        self.assertIn("must be mode 0600", Path(run_dir, "claude.err").read_text())

    def test_token_reaches_only_claude_and_is_never_written(self):
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        result, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertIn("via=direct", result.stdout)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL", script)
        self.assertIn("( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {})\"; exec ".format(self.token), script)
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log, ANTHROPIC_API_KEY="x",
                                       ANTHROPIC_BASE_URL="http://x"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(self.log + ".toklen").read_text().strip(), str(len(FAKE_TOKEN)))
        self.assertEqual(Path(self.log + ".anthropic").read_text(), "api_key=unset base_url=unset\n")
        self.assertNotIn(FAKE_TOKEN, done.stdout + done.stderr)
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **self.env))
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertNotIn("WARNING: an OAuth token", waited.stdout)
        self.assert_no_token(run_dir)

    def test_wait_masks_a_leaked_token(self):
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        _, run_dir = self.prepared("--via", "direct", env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log, STUB_LEAK="1"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(FAKE_TOKEN, Path(run_dir, "claude.out").read_text())
        waited = self.cli("wait", run_dir, "--timeout", "5", env=dict(os.environ, **self.env))
        self.assertIn("WARNING: an OAuth token (sk-ant-oat...) appeared in claude.out", waited.stdout)
        self.assertNotIn(FAKE_TOKEN, waited.stdout)
        self.assertIn("leak sk-ant-oat[REDACTED]", Path(run_dir, "claude.out").read_text())
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
        self.assertNotIn("cmux_workspace", json.loads(Path(run_dir, "meta.json").read_text()))
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "60"], out=out, err=io.StringIO()), 0, out.getvalue())
        self.assertIn("status=finished", out.getvalue())
        self.assertEqual(Path(self.log + ".toklen").read_text().strip(), str(len(FAKE_TOKEN)))
        self.assertTrue(os.path.isdir(run_dir + "/lock"))
        self.assert_no_token(run_dir)
        relaunch = self.cli("claude", "--launch-run", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(relaunch.returncode, 1)
        self.assertIn("already started", relaunch.stderr)

    def test_direct_claude_review_uses_token_and_no_cmux(self):
        gate = self.stub("exec-gate", STUB_EXEC_GATE)
        stub = self.stub("stub-claude", STUB_ENV_CLAUDE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n")
        env = dict(os.environ, **dict(self.env, HERMES_DELEGATE_REVIEW_GATE=gate, HERMES_DELEGATE_CLAUDE_BIN=stub))
        result = self.cli("review", "--reviewer", "claude", "--via", "direct", "--item", "hd-direct", "--commit",
                          self.head[:12], "--prompt", prompt, "--workdir", self.repo, "--no-launch", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("via=direct", result.stdout)
        run_dir = self.fields(result.stdout)["run_dir"]
        script = Path(run_dir, "inner.zsh").read_text()
        self.assertIn("export CLAUDE_CODE_OAUTH_TOKEN=", script)
        self.assertIn("unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL", script)
        self.assertIn("kill -TERM $child", script)
        done = subprocess.run(["/bin/zsh", run_dir + "/inner.zsh"], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, STUB_LOG=self.log))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(self.log + ".toklen").read_text().strip(), str(len(FAKE_TOKEN)))
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
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
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "7")
        time.sleep(0.3)
        self.assertTrue(no_process(marker), "claude left running")
        self.assert_no_token(run_dir)

    def test_qa_rejects_via(self):
        result = self.worker("--via", "direct", kind="qa")
        self.assertEqual(result.returncode, 2)
        self.assertIn("in cmux (user pin)", result.stderr)

    def test_impl_direct_refusal_switch(self):
        original = hd.CLAUDE_IMPL_DIRECT
        try:
            hd.CLAUDE_IMPL_DIRECT = False
            with self.assertRaises(hd.Fail) as caught:
                hd.cmd_worker("claude", ["--spec", self.spec, "--workdir", self.repo, "--class", "ambiguous",
                                         "--timeout", "900", "--via", "direct", "--no-launch"],
                              out=io.StringIO(), err=io.StringIO())
            self.assertEqual(caught.exception.code, 2)
        finally:
            hd.CLAUDE_IMPL_DIRECT = original


class CancelTests(WorkerTests):
    def setUp(self):
        super().setUp()
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n")
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
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=30)

    def watch(self, argv, name="watcher.out"):
        watch_out = os.path.join(self.tmp, name)
        with open(watch_out, "w") as sink:
            watcher = subprocess.Popen(argv, start_new_session=True, stdout=sink, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, **self.env))
        self.addCleanup(self.reap, watcher)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "status=started" not in Path(watch_out).read_text():
            time.sleep(0.05)
        return watcher, watch_out

    def assert_still_running(self, run_dir):
        time.sleep(0.5)
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"), "a watcher signal must not finish the run")
        self.assertFalse(no_process(self.marker), "a watcher signal must not stop claude")

    def test_watcher_sigterm_leaves_direct_run_running(self):
        run_dir, inner = self.running_direct()
        watcher, watch_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"])
        os.killpg(watcher.pid, signal.SIGTERM)
        watcher.wait(timeout=30)
        text = Path(watch_out).read_text()
        self.assertEqual(watcher.returncode, hd.EXIT_NO_RC, text)
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
        os.killpg(watcher.pid, signal.SIGTERM)
        watcher.wait(timeout=30)
        self.assertIn("status=no_rc", Path(watch_out).read_text())
        self.assert_still_running(run_dir)
        stopped = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(stopped.returncode, 7, stopped.stdout + stopped.stderr)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not no_process(self.marker):
            time.sleep(0.1)
        self.assertTrue(no_process(self.marker), "claude left running after cancel")

    def test_interrupted_second_wait_is_not_destructive(self):
        run_dir, inner = self.running_direct()
        first, first_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"])
        for sig in (signal.SIGHUP, signal.SIGINT):
            second, second_out = self.watch([sys.executable, SCRIPT, "wait", run_dir, "--timeout", "120"],
                                            name="second.out")
            os.killpg(second.pid, sig)
            second.wait(timeout=30)
            self.assertEqual(second.returncode, hd.EXIT_NO_RC, Path(second_out).read_text())
            self.assertIn("wait stopped by " + signal.Signals(sig).name, Path(second_out).read_text())
            self.assert_still_running(run_dir)
        timed_out = self.cli("wait", run_dir, "--timeout", "1", env=dict(os.environ, **self.env))
        self.assertEqual(timed_out.returncode, hd.EXIT_NO_RC)
        self.assertIn("it may still be running", timed_out.stderr)
        self.assertIn("re-watch with hermes-delegate wait " + run_dir, timed_out.stderr)
        self.assert_still_running(run_dir)
        self.assertIsNone(first.poll(), "the persisted watcher must keep watching")
        self.assertEqual(self.cli("cancel", run_dir, env=dict(os.environ, **self.env)).returncode, 7)
        first.wait(timeout=30)
        self.assertEqual(first.returncode, 7, Path(first_out).read_text())
        self.assertIn("status=finished", Path(first_out).read_text())

    def test_cancel_command_stops_run(self):
        run_dir, inner = self.running_direct()
        result = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("status=finished", result.stdout)
        self.assertIn("claude rc=7", result.stdout)
        self.assertEqual(inner.wait(timeout=30), 7)
        time.sleep(0.3)
        self.assertTrue(no_process(self.marker), "claude left running after cancel")
        again = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(again.returncode, 7)
        self.assertIn("already finished", again.stdout)

    def test_cmux_run_survives_watcher_signals_and_cancel_stops_it(self):
        stub = self.stub(self.marker, STUB_SLOW_CLAUDE)
        _, run_dir = self.prepared(env={"HERMES_DELEGATE_CLAUDE_BIN": stub})
        self.assertEqual(json.loads(Path(run_dir, "meta.json").read_text())["via"], "cmux")
        # Stands in for the cmux workspace shell: its own session, as cmux gives each workspace.
        inner = subprocess.Popen(["/bin/zsh", run_dir + "/inner.zsh"], start_new_session=True,
                                 env=dict(os.environ, STUB_LOG=self.log),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.reap, inner)
        self.assertTrue(wait_for(self.log))
        for sig in (signal.SIGTERM, signal.SIGHUP):
            watcher, watch_out = self.watch(["/bin/bash", "-c", shlex.join([sys.executable, SCRIPT, "wait",
                                                                            run_dir])])
            os.killpg(watcher.pid, sig)
            watcher.wait(timeout=30)
            self.assertIn("the run keeps going", Path(watch_out).read_text())
            self.assert_still_running(run_dir)
        stopped = self.cli("cancel", run_dir, env=dict(os.environ, **self.env))
        self.assertEqual(stopped.returncode, 7, stopped.stdout + stopped.stderr)
        self.assertEqual(inner.wait(timeout=30), 7)
        time.sleep(0.3)
        self.assertTrue(no_process(self.marker), "claude left running after cancel")

    def test_cancel_refuses_a_stale_or_foreign_pid(self):
        _, run_dir = self.prepared("--via", "direct")
        os.mkdir(run_dir + "/lock")
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self.reap, sleeper)
        Path(run_dir, "inner.pid").write_text("{}\n".format(sleeper.pid))
        result = self.cli("cancel", run_dir, "--grace", "1", env=dict(os.environ, **self.env))
        self.assertEqual(result.returncode, hd.EXIT_NO_RC, result.stdout + result.stderr)
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
        self.assertEqual(late.returncode, hd.EXIT_LOCKED)
        self.assertFalse(os.path.exists(self.log), "the worker must not run after cancel")
        self.assertEqual(Path(run_dir, "claude.rc").read_text().strip(), "143")

    def test_cancel_before_start_of_review_names_the_open_ticket(self):
        gate = self.stub("exec-gate", STUB_EXEC_GATE)
        prompt = os.path.join(self.tmp, "prompt.md")
        Path(prompt).write_text("Review.\n")
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
        before = {sig: signal.getsignal(sig) for sig in hd.WATCHER_SIGNALS}
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "5"], out=io.StringIO(), err=io.StringIO()), 0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in hd.WATCHER_SIGNALS}, before)


class SmokeTests(Base):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "stub-cli")
        Path(self.stub).write_text(STUB_SMOKE)
        os.chmod(self.stub, 0o755)
        self.token = os.path.join(self.tmp, "token")
        Path(self.token).write_text(FAKE_TOKEN + "\n")
        os.chmod(self.token, 0o600)
        self.env = {"HERMES_DELEGATE_STATE_ROOT": os.path.join(self.tmp, "state"),
                    "HERMES_DELEGATE_CODEX_BIN": self.stub, "HERMES_DELEGATE_CLAUDE_BIN": self.stub,
                    "HERMES_DELEGATE_CLAUDE_TOKEN_FILE": self.token, "HERMES_DELEGATE_CMUX": "/nonexistent/cmux"}

    def test_codex_and_claude_direct_ok(self):
        for target in ("codex", "claude"):
            result = self.cli("smoke", target, "--via", "direct", env=self.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "ok {} via=direct\n".format(target))
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
        Path(slow).write_text("#!/bin/sh\nsleep 60\n")
        os.chmod(slow, 0o755)
        started = time.monotonic()
        result = self.cli("smoke", "codex", "--timeout", "10", env=dict(self.env, HERMES_DELEGATE_CODEX_BIN=slow))
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result within 10s", result.stdout)
        time.sleep(0.3)
        self.assertTrue(no_process(marker))

    def test_smoke_script_shape(self):
        script = hd.smoke_script("codex", "/private/tmp/x", "direct")
        self.assertIn("exec --ephemeral -s read-only -m gpt-6.1-sol", script)
        self.assertIn("export ASDF_NODEJS_VERSION=22.19.0", script)
        self.assertNotIn("review-gate", script)


def stall_run(root, kind="claude", events=(), name="run"):
    run_dir = os.path.join(root, name)
    os.mkdir(run_dir)
    Path(run_dir, "meta.json").write_text(json.dumps({"kind": kind, "via": "direct"}))
    Path(run_dir, "started_at").write_text("2026-10-22T10:00:00+0900\n")
    Path(run_dir, kind + ".out").write_text("".join(json.dumps(e) + "\n" for e in events))
    return run_dir


def bash_call(command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


class StallTests(Base):
    def wait(self, run_dir, *extra):
        out = io.StringIO()
        started = time.monotonic()
        code = hd.cmd_wait([run_dir, "--timeout", "30", *extra], out=out, err=io.StringIO())
        return code, out.getvalue(), time.monotonic() - started

    def test_fires_after_no_growth_and_redacts(self):
        events = [{"type": "system", "subtype": "init"}, bash_call("sleep 30"),
                  {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                  bash_call("sleep 30"), bash_call("curl -H 'x-api-key: {}' https://x".format(FAKE_TOKEN))]
        run_dir = stall_run(self.tmp, events=events)
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, hd.EXIT_STALLED)
        self.assertGreaterEqual(elapsed, 1.1)
        self.assertLess(elapsed, 10)
        lines = text.splitlines()
        self.assertIn("status=stalled", lines)
        self.assertTrue(any(line.startswith("last_growth=") and "no growth seen by this wait" in line
                            for line in lines), text)
        self.assertTrue(any(line.startswith("stalled_minutes=0.0") for line in lines), text)
        self.assertIn("last_tool_call=Bash: curl -H 'x-api-key: sk-ant-oat[REDACTED]' https://x "
                      "(same call 1x in the last 3 tool calls)", lines)
        self.assertEqual(lines[-1], "the run keeps going; re-run hermes-delegate wait RUN_DIR to keep watching "
                                    "(optionally --stall-minutes N) or stop it with hermes-delegate cancel RUN_DIR")
        flags = ", background=true, notify_on_complete=true, continue_on_complete=true, persist_on_release=true)"
        self.assertEqual(lines[-3], 'watcher: terminal(command="{} wait {} --timeout {} --stall-minutes 0.02"{}'.format(
            hd.HD_BIN, run_dir, hd.REWATCH_MIN_SECONDS, flags))
        self.assertEqual(lines[-2], 'watcher_no_stall: terminal(command="{} wait {} --timeout {} --stall-minutes 0"{}'
                         .format(hd.HD_BIN, run_dir, hd.REWATCH_MIN_SECONDS, flags))
        self.assertEqual(lines[-4], "stall_count=1 of 2 (each re-arm uses one continuation of the approval's "
                                    "chain cap)")
        self.assertEqual(Path(run_dir, "stall_count").read_text(), "1\n")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("status=finished", text)
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"))
        self.assertIn(FAKE_TOKEN, Path(run_dir, "claude.out").read_text(), "a stall must not rewrite a live run")

    def test_second_stall_offers_only_no_stall_and_disables_default_check(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        Path(run_dir, "stall_count").write_text("1\n")
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, hd.EXIT_STALLED)
        lines = text.splitlines()
        self.assertIn("stall_count=2 of 2 (each re-arm uses one continuation of the approval's chain cap)", lines)
        self.assertFalse(any(line.startswith("watcher:") for line in lines), text)
        self.assertEqual(lines[-3], "stall_limit_reached: re-arm only with watcher_no_stall: "
                                    "(later waits default to --stall-minutes 0)")
        self.assertEqual(lines[-2], hd.watcher_line(run_dir, hd.REWATCH_MIN_SECONDS, 0, label="watcher_no_stall"))
        out = io.StringIO()
        with mock.patch.dict(hd.STALL_DEFAULT_MINUTES, {"claude": 0.01}), self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "2"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, hd.EXIT_NO_RC)
        self.assertIn("stall_check=off (2 stalls already reported for this run; pass --stall-minutes M to check "
                      "again)", out.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-1], hd.watcher_line(run_dir, 2, 0.0))
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, hd.EXIT_STALLED, "an explicit --stall-minutes still checks")
        self.assertIn("stall_count=3 of 2", text)

    def test_repeated_call_count(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")] * 4)
        _, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertIn("last_tool_call=Bash: sleep 30 (same call 4x in the last 4 tool calls)", text)
        self.assertIn("last_event=Bash: sleep 30", text)

    def test_growth_resets_the_stall_clock(self):
        run_dir = stall_run(self.tmp, events=[bash_call("make")])
        stop = time.monotonic() + 2.5

        def grow():
            while time.monotonic() < stop:
                with open(run_dir + "/claude.out", "a") as handle:
                    handle.write(json.dumps(bash_call("make step")) + "\n")
                time.sleep(0.2)

        writer = threading.Thread(target=grow)
        writer.start()
        self.addCleanup(writer.join)
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, hd.EXIT_STALLED)
        self.assertGreaterEqual(elapsed, 2.5 + 1.1)
        growth = [line for line in text.splitlines() if line.startswith("last_growth=")][0]
        self.assertNotIn("no growth seen", growth)
        self.assertIn("last_tool_call=Bash: make step", text)

    def test_rerun_wait_on_old_output_does_not_fire_immediately(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        old = time.time() - 7200
        os.utime(run_dir + "/claude.out", (old, old))
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, hd.EXIT_STALLED)
        self.assertGreaterEqual(elapsed, 1.1)
        self.assertIn("stalled_minutes=0.0", text)
        self.assertIn(time.strftime("last_growth=%Y-%m-%dT%H:%M", time.localtime(old)), text)

    def test_missing_output_still_stalls(self):
        run_dir = stall_run(self.tmp, kind="qa")
        os.remove(run_dir + "/qa.out")
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, hd.EXIT_STALLED)
        self.assertIn("last_growth=none (qa.out not written yet)", text)
        self.assertIn("last_event=none (qa.out is empty or missing)", text)

    def test_rc_during_stall_window_wins(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])

        def finish():
            time.sleep(0.5)
            Path(run_dir, "claude.rc.tmp").write_text("4\n")
            os.rename(run_dir + "/claude.rc.tmp", run_dir + "/claude.rc")

        threading.Thread(target=finish).start()
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.05")
        self.assertEqual(code, 4)
        self.assertLess(elapsed, 2.5)
        self.assertIn("status=finished", text)
        self.assertNotIn("status=stalled", text)

    def test_existing_rc_wins_over_stale_output(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        Path(run_dir, "claude.rc").write_text("0\n")
        old = time.time() - 7200
        os.utime(run_dir + "/claude.out", (old, old))
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, 0)
        self.assertNotIn("stalled", text)

    def test_zero_disables(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        started = time.monotonic()
        with self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "2", "--stall-minutes", "0"], out=out, err=io.StringIO())
        self.assertGreaterEqual(time.monotonic() - started, 1.9)
        self.assertEqual(caught.exception.code, hd.EXIT_NO_RC)
        self.assertEqual(out.getvalue().splitlines()[-2:], ["status=no_rc", hd.watcher_line(run_dir, 2, 0.0)])

    def test_timeout_before_stall_is_91(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        with self.assertRaises(hd.Fail) as caught:
            hd.cmd_wait([run_dir, "--timeout", "1", "--stall-minutes", "5"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, hd.EXIT_NO_RC)
        self.assertEqual(out.getvalue().splitlines()[-1], hd.watcher_line(run_dir, 1, 5.0))

    def test_rewatch_timeout_is_remaining_with_floor(self):
        now = time.monotonic()
        self.assertEqual(hd.rewatch_timeout(3600, now - 600), 3000)
        self.assertEqual(hd.rewatch_timeout(3600, now - 3590), hd.REWATCH_MIN_SECONDS)
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        self.assertEqual(hd.cmd_wait([run_dir, "--timeout", "4000", "--stall-minutes", "0.01"], out=out,
                                     err=io.StringIO()), hd.EXIT_STALLED)
        watcher = [line for line in out.getvalue().splitlines() if line.startswith("watcher: ")][0]
        self.assertRegex(watcher, r" --timeout (399[0-9]|4000) --stall-minutes 0\.01\"")

    def test_signal_during_stall_watch_is_still_91(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        log = os.path.join(self.tmp, "watch.out")
        with open(log, "w") as sink:
            proc = subprocess.Popen([sys.executable, SCRIPT, "wait", run_dir, "--stall-minutes", "1"],
                                    stdout=sink, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "status=started" not in Path(log).read_text():
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), hd.EXIT_NO_RC)
        text = Path(log).read_text()
        self.assertIn("wait stopped by SIGTERM; the run keeps going", text)
        self.assertIn('watcher: terminal(command="{} wait {} --timeout 3'.format(hd.HD_BIN, run_dir), text)
        self.assertIn(' --stall-minutes 1", background=true, notify_on_complete=true', text)
        self.assertNotIn("status=stalled", text)

    def test_codex_stream_summary(self):
        events = [{"type": "thread.started"}] + [
            {"type": "item.started", "item": {"type": "command_execution", "command": "xcrun simctl list",
                                              "status": "in_progress"}}] * 3
        run_dir = stall_run(self.tmp, kind="qa", events=events)
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, hd.EXIT_STALLED)
        self.assertIn("last_tool_call=command (in_progress): xcrun simctl list (same call 3x in the last 3 tool "
                      "calls)", text)

    def test_non_json_output_tail(self):
        run_dir = stall_run(self.tmp, kind="review")
        Path(run_dir, "review.out").write_text("plain line\nwaiting for input {}\n".format(FAKE_TOKEN))
        _, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertIn("last_event=waiting for input sk-ant-oat[REDACTED]", text)
        self.assertNotIn(FAKE_TOKEN, text)

    def test_cut_tail_line_is_dropped(self):
        path = os.path.join(self.tmp, "big.out")
        Path(path).write_text("x" * 50 + FAKE_TOKEN[3:] + "\n" + json.dumps(bash_call("make")) + "\n")
        events, last_raw = hd.tail_events(path, limit=len(FAKE_TOKEN) + 30 + len(json.dumps(bash_call("make"))))
        self.assertEqual(len(events), 1)
        self.assertNotIn(FAKE_TOKEN[10:], "".join(hd.stall_summary(path)))
        events, last_raw = hd.tail_events(path, limit=40)
        self.assertEqual(events, [])
        self.assertNotIn(FAKE_TOKEN[3:20], last_raw or "")

    def test_per_kind_defaults_and_validation(self):
        self.assertEqual(hd.stall_seconds("claude", None), 1200)
        self.assertEqual(hd.stall_seconds("review", None), 1200)
        self.assertEqual(hd.stall_seconds("qa", None), 1800)
        self.assertEqual(hd.stall_seconds("qa", 5), 300)
        self.assertEqual(hd.stall_seconds("claude", 0), 0)
        for bad in ("-1", "nan", "inf"):
            run_dir = stall_run(self.tmp, name="bad" + bad)
            with self.assertRaises(hd.Fail) as caught:
                hd.cmd_wait([run_dir, "--stall-minutes", bad], out=io.StringIO(), err=io.StringIO())
            self.assertEqual(caught.exception.code, 2)

    def test_default_stall_used_when_flag_omitted(self):
        seen = []
        original = hd.wait_for_rc_or_stall
        hd.wait_for_rc_or_stall = lambda rc, run_dir, output, deadline, stall: seen.append((output, stall))
        try:
            for kind in ("claude", "review", "qa"):
                run_dir = stall_run(self.tmp, kind=kind, name=kind)
                Path(run_dir, kind + ".rc").write_text("0\n")
                hd.cmd_wait([run_dir], out=io.StringIO(), err=io.StringIO())
        finally:
            hd.wait_for_rc_or_stall = original
        self.assertEqual([(os.path.basename(o), s) for o, s in seen],
                         [("claude.out", 1200), ("review.out", 1200), ("qa.out", 1800)])

    def test_slept_time_is_not_counted(self):
        run_dir = stall_run(self.tmp)
        clock = {"t": 1000.0, "calls": 0}

        def fake_until(ready, directory, deadline):
            clock["calls"] += 1
            clock["t"] = deadline + (3600 if clock["calls"] == 1 else 0)
            return ready()

        originals = hd.wait_until, hd.time.monotonic
        hd.wait_until, hd.time.monotonic = fake_until, lambda: clock["t"]
        try:
            result = hd.wait_for_rc_or_stall(run_dir + "/claude.rc", run_dir, run_dir + "/claude.out",
                                             1000.0 + 86400, 1200)
        finally:
            hd.wait_until, hd.time.monotonic = originals
        self.assertEqual(result[0], 1200)
        self.assertGreaterEqual(clock["t"], 1000.0 + 3600 + 1200)


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
        with mock.patch.object(hd.time, "sleep", side_effect=AssertionError("status slept")):
            code = hd.cmd_status([path], out=out, err=io.StringIO())
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
            Path(path, "meta.json").write_text(json.dumps({"kind": kind, "via": "direct"}))
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
            Path(path, "started_at").write_text("2026-10-26T10:00:00+0900\n")
            Path(path, kind + ".out").write_text("{}\n")
            Path(path, "stall_count").write_text("1\n")
            pid = self.live(path + "/inner.zsh")
            Path(path, "inner.pid").write_text("{}\n".format(pid))
            code, fields = self.status(path)
            self.assertEqual((code, fields["status"], fields["pid"]), (0, "running", str(pid)))
            self.assertEqual(fields["started_at"], "2026-10-26T10:00:00+0900")
            self.assertEqual(fields["stall_count"], "1")
            self.assertLessEqual(int(fields["last_output_age_s"]), 5)
            self.assertEqual(fields["hint"], hd.HINT_RUNNING)
            Path(path, "ended_at").write_text("2026-10-26T10:05:00+0900\n")
            Path(path, kind + ".rc").write_text("7\n")
            code, fields = self.status(path)
            self.assertEqual((code, fields["status"], fields["rc"]), (0, "finished", "7"))
            self.assertEqual(fields["ended_at"], "2026-10-26T10:05:00+0900")
            self.assertNotIn("pid", fields)
            self.assertEqual(fields["hint"], hd.HINT_FINISHED)

    def test_run_dir_unknown_states(self):
        path = self.run_dir("claude")
        os.mkdir(path + "/lock")
        Path(path, "started_at").write_text("x\n")
        Path(path, "inner.pid").write_text("999999\n")
        code, fields = self.status(path)
        self.assertEqual((code, fields["status"]), (hd.EXIT_STATUS_UNKNOWN, "unknown"))
        Path(path, "claude.rc").write_text("oops\n")
        self.assertEqual(self.status(path)[0], hd.EXIT_STATUS_UNKNOWN)
        other = self.run_dir("qa", "mismatch", meta=False)
        Path(other, "meta.json").write_text(json.dumps({"kind": "claude"}))
        code, fields = self.status(other)
        self.assertEqual((code, fields["status"]), (hd.EXIT_STATUS_UNKNOWN, "unknown"))
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
        Path(path, "review.rc").write_text("0\n")
        code, fields = self.status(path)
        self.assertEqual((code, fields["kind"], fields["status"], fields["rc"]), (0, "review", "finished", "0"))

    def test_codex_before_and_after_runner_start(self):
        out_dir = os.path.join(self.state, "codex", "20261026T110652-abc123")
        os.makedirs(out_dir)
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["kind"], fields["status"]), (0, "codex", "not_started"))
        self.assertEqual(fields["hint"], hd.HINT_CODEX_NOT_STARTED)
        old = time.time() - 3600
        os.utime(out_dir, (old, old))
        self.assertIn("probably never happened", self.status(out_dir)[1]["hint"])
        task = os.path.join(out_dir, "codex-task-x1")
        os.mkdir(task)
        Path(task, "events.jsonl").write_text('{"type":"thread.started"}\n{"type":"turn.started"}\n')
        Path(task, "stderr.log").write_text("")
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"]), (hd.EXIT_STATUS_UNKNOWN, "unknown"))
        pid = self.live("/rel/scripts/run_codex_task.py", "--spec", "/s", "--output-dir", out_dir)
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"], fields["pid"]), (0, "running", str(pid)))
        self.assertEqual((fields["task_dir"], fields["events"], fields["runner_status"]), (task, "2", "running"))
        self.assertIn("started_at", fields)
        self.assertEqual(fields["hint"], hd.HINT_RUNNING)
        Path(task, "status.json").write_text(json.dumps({
            "status": "cli_failed", "exit_code": 3, "execution": {"status": "cli_failed", "cli_exit_code": 3}}))
        code, fields = self.status(out_dir)
        self.assertEqual((code, fields["status"], fields["rc"], fields["runner_status"]),
                         (0, "finished", "3", "cli_failed"))
        self.assertIn("ended_at", fields)
        self.assertNotIn("pid", fields)
        Path(task, "status.json").write_text("{not json")
        self.assertEqual(self.status(out_dir)[0], hd.EXIT_STATUS_UNKNOWN)

    def test_other_output_dir_runner_is_not_matched(self):
        out_dir = os.path.join(self.state, "codex", "a")
        os.makedirs(out_dir + "/codex-task-1")
        self.live("/rel/scripts/run_codex_task.py", "--output-dir", out_dir + "b")
        self.assertEqual(self.status(out_dir)[1]["status"], "unknown")

    def test_trailing_slash_is_accepted(self):
        path = self.run_dir("qa")
        Path(path, "qa.rc").write_text("0\n")
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
            with self.assertRaises(hd.Fail) as ctx:
                hd.cmd_status([bad], out=io.StringIO())
            self.assertEqual(ctx.exception.code, 2, bad)

    def test_cli_usage_redaction_and_no_token_read(self):
        result = self.cli("status")
        self.assertEqual(result.returncode, 2)
        path = self.run_dir("claude")
        os.mkdir(path + "/lock")
        Path(path, "started_at").write_text("leak " + FAKE_TOKEN + "\n")
        token = os.path.join(self.tmp, "token")
        Path(token).write_text(FAKE_TOKEN + "\n")
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


if __name__ == "__main__":
    unittest.main()


class CodexMcpOffTests(unittest.TestCase):
    def run_with(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp, "codex")
            stub.write_text("#!/bin/sh\n" + script)
            os.chmod(stub, 0o755)
            old = os.environ.get("HERMES_DELEGATE_CODEX_BIN")
            os.environ["HERMES_DELEGATE_CODEX_BIN"] = str(stub)
            try:
                return hd.codex_mcp_off()
            finally:
                if old is None:
                    os.environ.pop("HERMES_DELEGATE_CODEX_BIN")
                else:
                    os.environ["HERMES_DELEGATE_CODEX_BIN"] = old

    def test_disables_each_enabled_server_by_name(self):
        listing = json.dumps([{"name": "figma", "enabled": True}, {"name": "aside", "enabled": True},
                              {"name": "logrocket", "enabled": False}, {"name": "bad name;rm", "enabled": True}])
        args = self.run_with("[ \"$1 $2 $3\" = 'mcp list --json' ] && cat <<'J'\n{}\nJ\n".format(listing))
        self.assertEqual(args, ["-c", "mcp_servers.aside.enabled=false", "-c", "mcp_servers.figma.enabled=false"])

    def test_listing_failure_keeps_configured_servers(self):
        self.assertEqual(self.run_with("echo not json; exit 1\n"), [])

    def test_codex_reviewer_argv_carries_the_overrides(self):
        listing = json.dumps([{"name": "figma", "enabled": True}])
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp, "codex")
            stub.write_text("#!/bin/sh\ncat <<'J'\n{}\nJ\n".format(listing))
            os.chmod(stub, 0o755)
            old = os.environ.get("HERMES_DELEGATE_CODEX_BIN")
            os.environ["HERMES_DELEGATE_CODEX_BIN"] = str(stub)
            try:
                argv = hd.reviewer_command("codex", tmp, tmp)
            finally:
                if old is None:
                    os.environ.pop("HERMES_DELEGATE_CODEX_BIN")
                else:
                    os.environ["HERMES_DELEGATE_CODEX_BIN"] = old
        self.assertIn("mcp_servers.figma.enabled=false", argv)
        self.assertLess(argv.index("mcp_servers.figma.enabled=false"), argv.index("-C"))
