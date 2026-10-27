import io
import os
import re
from pathlib import Path
from unittest import mock
from delegate_test_support import Base, VALID_SPEC, make_repo
from delegate_test_support import codex
from delegate_test_support import common
from delegate_test_support import entry
from delegate_test_support import runtime
from delegate_test_support import spec
from delegate_test_support import worker


class SpecGateTests(Base):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp, "work")
        self.docs = Path(self.tmp, "docs")
        self.work.mkdir()
        self.docs.mkdir()
        self.spec = self.docs / "SPEC.md"
        self.spec.write_text(VALID_SPEC, encoding="utf-8")

    def problems(self, text=VALID_SPEC, kind="codex"):
        return spec.spec_problems(text, kind, self.work, self.docs)

    def contracts(self, items):
        return VALID_SPEC.replace("- N/A: This isolated fixture has no external contract.", items)

    def test_missing_sections_per_kind(self):
        for kind in ("codex", "claude", "qa"):
            with self.subTest(kind=kind):
                problems = self.problems("", kind)
                names = ["Acceptance"] if kind == "qa" else list(spec.SPEC_SECTIONS)
                self.assertEqual(problems, [name + ": missing section" for name in names])
                for name in names:
                    text = re.sub(r"## " + name + r"\n.*?(?=\n## |\Z)", "", VALID_SPEC, flags=re.DOTALL)
                    self.assertIn(name + ": missing section", self.problems(text, kind))
        self.assertEqual(self.problems("## Acceptance\n- Every expected result matches exactly.", "qa"), [])

    def test_duplicate_section(self):
        for kind in ("codex", "claude", "qa"):
            self.assertIn("Acceptance: duplicated section",
                          self.problems(VALID_SPEC + "\n## 완료 검증\n- Exact results match.", kind))

    def test_korean_and_case_insensitive_headings(self):
        self.assertEqual(self.problems(), [])
        text = VALID_SPEC
        for name, aliases in spec.SPEC_SECTIONS.items():
            text = text.replace("## " + name, "##  " + aliases[1] + "  ")
        self.assertEqual(self.problems(text), [])
        self.assertEqual(self.problems(VALID_SPEC.upper()), [])

    def test_only_exact_level_two_names_count(self):
        for title in ("### Acceptance", "# Acceptance", "## Acceptance extra", "## Acceptance ##"):
            self.assertIn("Acceptance: missing section", self.problems(VALID_SPEC.replace("## Acceptance", title)))

    def test_empty_sections_and_heading_boundaries(self):
        for name in spec.SPEC_SECTIONS:
            text = re.sub(r"(## " + name + r"\n).*?(?=\n## |\Z)", r"\1", VALID_SPEC, flags=re.DOTALL)
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
        self.assertEqual(self.problems(self.contracts(f"- `{capture}:1`")), [])
        with mock.patch.dict(os.environ, HOME=str(self.docs)):
            self.assertEqual(self.problems(self.contracts("- `~/capture:1`")), [])

    def test_unreadable_reference(self):
        capture = self.work / "capture"
        capture.write_bytes(b"data")
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertTrue(self.problems(self.contracts("- `capture:1`")))

    def test_each_failure_topic_missing(self):
        labels = ("Ordering/concurrency", "Interruption", "Rejection/partial failure", "Repeat/retry", "External")
        for topic, label in zip(spec.SPEC_TOPICS, labels):
            text = re.sub(r"^- " + re.escape(label) + r":.*\n", "", VALID_SPEC, flags=re.MULTILINE)
            self.assertIn(f"Failure paths: missing {topic} topic", self.problems(text))

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
        Path(self.work, "real.txt").write_text("one\n", encoding="utf-8")
        na = "- N/A: This isolated fixture has no external contract.\n"
        for span in ("127.0.0.1:18190", "localhost:8080", "LOCALHOST:3000", "[::1]:8080"):
            with self.subTest(span=span):
                text = VALID_SPEC.replace(na, f"- `{span}` and `real.txt:1`\n", 1)
                self.assertEqual(self.problems(text), [])
        for span in ("missing.py:3", "dir/missing:3", "~/nope/x:2", "real.txt:99", "Makefile:3", "example.com:443"):
            with self.subTest(span=span):
                text = VALID_SPEC.replace(na, f"- `{span}` and `real.txt:1`\n", 1)
                self.assertTrue(any("invalid file/line reference" in p for p in self.problems(text)), span)

    def test_acceptance_na_forbidden(self):
        for answer in ("N/A: abcdefgh", "해당 없음： abcdefgh"):
            self.assertTrue(any("N/A is not allowed" in p for p in self.problems("## Acceptance\n- " + answer, "qa")))

    def test_non_utf8_empty_and_oversize(self):
        self.spec.write_bytes(b"\xff")
        self.assertEqual(spec.read_spec_gate(self.spec, "codex", self.work)[1], ["SPEC: not UTF-8"])
        for data in (b"", b"x" * (1024 * 1024 + 1)):
            self.spec.write_bytes(data)
            self.assertIn("SPEC: must be non-empty and at most 1 MiB", spec.read_spec_gate(self.spec, "qa", self.work)[1])

    def test_all_problems_in_one_refusal(self):
        text = "## External contracts\n- `missing:9`\n## Failure paths\n- Order: short\n## Acceptance\n- at least one result etc.\n"
        self.spec.write_text(text, encoding="utf-8")
        problems = self.problems(text)
        with self.assertRaises(common.Fail) as caught:
            spec.require_spec_gate(self.spec, "codex", self.work)
        self.assertEqual(caught.exception.code, 2)
        message = str(caught.exception)
        self.assertIn(f"{len(problems)} problem(s)", message)
        for i, problem in enumerate(problems, 1):
            self.assertIn(f"({i}) {problem}", message)
        self.assertIn("check with: hermes-delegate spec-check --spec", message)

    def test_item_excerpt_limited_to_sixty_characters(self):
        item = "x" * 90
        problems = self.problems("## Acceptance\n- " + item + " etc.", "qa")
        self.assertIn(repr(item[:60]), problems[0])
        self.assertNotIn(item[:61], problems[0])

    def test_spec_check_cli_pass_failure_and_defaults(self):
        (self.docs / "capture").write_bytes(b"line\n")
        self.spec.write_text(self.contracts("- `capture:1`"), encoding="utf-8")
        result = self.cli("spec-check", "--spec", str(self.spec))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "ok\n", ""))
        self.spec.write_text("invalid", encoding="utf-8")
        result = self.cli("spec-check", "--spec", str(self.spec))
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertEqual(result.stderr.splitlines(), self.problems("invalid"))
        self.spec.write_text("## Acceptance\n- Verify each expected result.", encoding="utf-8")
        self.assertEqual(self.cli("spec-check", "--spec", str(self.spec), "--kind", "qa",
                                  "--workdir", str(self.work)).returncode, 0)

    def test_template_cli(self):
        result = self.cli("spec-check", "--template")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        for heading in spec.SPEC_SECTIONS:
            self.assertIn("## " + heading, result.stdout)
        for label in ("Ordering/concurrency", "Interruption", "Rejection/partial failure", "Repeat/retry", "External"):
            self.assertIn("- " + label + ":", result.stdout)

    def test_codex_refusal_has_no_output_or_scope_resolution(self):
        self.spec = self.work / "SPEC.md"
        self.spec.write_text("invalid", encoding="utf-8")
        output = self.work / "output"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(common, "RUNNER", self.spec), mock.patch.object(common, "RUNNER_PYTHON", self.spec), \
                mock.patch.object(codex, "resolve_code_scope") as scope, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = entry.main(["codex", "--spec", str(self.spec), "--workdir", str(self.work),
                          "--class", "general", "--timeout", "900", "--output-dir", str(output)])
        self.assertEqual((rc, out.getvalue()), (2, ""))
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertIn("SPEC gate refused", err.getvalue())
        self.assertFalse(output.exists())
        scope.assert_not_called()

    def test_worker_refusal_creates_no_run_directory(self):
        self.spec.write_text("invalid", encoding="utf-8")
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
            with mock.patch.object(runtime, "state_root", side_effect=state_root), \
                    mock.patch.object(runtime, "worker_binary", return_value="/bin/echo"), \
                    mock.patch.object(common, "GTIMEOUT", Path("/bin/echo")):
                self.assertEqual(worker.cmd_worker(kind, ["--spec", str(self.spec), "--workdir", repo,
                                                     "--timeout", "900", "--no-launch", *extra],
                                               out=io.StringIO(), err=io.StringIO()), 0)
            run = next(root.iterdir())
            self.assertEqual((run / "spec.md").read_bytes(), data)
