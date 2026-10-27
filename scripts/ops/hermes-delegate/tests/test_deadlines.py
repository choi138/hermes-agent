import importlib
import io
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
common = importlib.import_module("hermes_delegate_common")
runtime = importlib.import_module("hermes_delegate_runtime")
review = importlib.import_module("hermes_delegate_review")
smoke = importlib.import_module("hermes_delegate_smoke")
scope = importlib.import_module("scope_check")


def timed_out(argv, **kwargs):
    if kwargs.get("timeout") is not None:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    return subprocess.CompletedProcess(argv, 0, stdout=b"patch\x00\xff", stderr=b"")


class DeadlineTests(unittest.TestCase):
    def test_review_inputs_preserves_binary_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            data = b"patch\x00\xff\r\n"
            result = subprocess.CompletedProcess("git", 0, stdout=data, stderr=b"")
            with mock.patch.object(subprocess, "run", return_value=result):
                review.write_review_inputs(directory, directory, "a" * 40, None)
            self.assertEqual(Path(directory, "diff.patch").read_bytes(), data)
            self.assertEqual(Path(directory, "log.txt").read_bytes(), data)

    def test_review_inputs_timeout_is_failure_without_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(subprocess, "run", side_effect=timed_out):
                with self.assertRaisesRegex(common.Fail, "timed out"):
                    review.write_review_inputs(directory, directory, "a" * 40, None)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_review_diff_timeout_precedes_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            prompt = Path(directory, "prompt.md")
            prompt.write_text("Review", encoding="utf-8")
            with mock.patch.object(common, "git_value", return_value="a" * 40), \
                    mock.patch.object(common, "dirty_entries", return_value=""), \
                    mock.patch.object(runtime, "tools", side_effect=common.Fail("unbounded diff reached tools")) as tools, \
                    mock.patch.object(subprocess, "run", side_effect=timed_out):
                with self.assertRaisesRegex(common.Fail, "timed out"):
                    review.cmd_review(["--reviewer", "codex", "--item", "fixture", "--commit", "a" * 40,
                                   "--base", "b" * 40, "--prompt", str(prompt), "--workdir", directory],
                                  out=io.StringIO(), err=io.StringIO())
                tools.assert_not_called()

    def test_scope_git_timeout_is_clear_failure(self):
        with mock.patch.object(subprocess, "run", side_effect=timed_out):
            with self.assertRaisesRegex(ValueError, "git.*timed out"):
                scope.git("/", "status")

    def test_smoke_timeout_escalates_and_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=12345)

            def wait(timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("fixture", timeout)
                return 0

            child.wait.side_effect = wait
            with mock.patch.object(review, "check_launch_tools"), \
                    mock.patch.object(runtime, "state_root", return_value=directory), \
                    mock.patch.object(smoke, "smoke_script", return_value=""), \
                    mock.patch.object(subprocess, "Popen", return_value=child), \
                    mock.patch.object(os, "killpg") as kill:
                out = io.StringIO()
                with self.assertRaisesRegex(common.Fail, "reap|stop"):
                    smoke.cmd_smoke(["codex", "--timeout", "10"], out=out, err=io.StringIO())
                self.assertEqual([call.args[1] for call in kill.call_args_list],
                                 [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)])
                self.assertNotIn("ok codex", out.getvalue())

    def test_smoke_timeout_reaps_after_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            child = mock.Mock(pid=12345)
            child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 10),
                                      subprocess.TimeoutExpired("fixture", 5), -9]
            with mock.patch.object(review, "check_launch_tools"), \
                    mock.patch.object(runtime, "state_root", return_value=directory), \
                    mock.patch.object(smoke, "smoke_script", return_value=""), \
                    mock.patch.object(subprocess, "Popen", return_value=child), \
                    mock.patch.object(os, "killpg") as kill:
                out = io.StringIO()
                self.assertEqual(smoke.cmd_smoke(["codex", "--timeout", "10"], out=out, err=io.StringIO()), 1)
                self.assertEqual([call.args[1] for call in kill.call_args_list],
                                 [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)])
                self.assertIn("no result within 10s", out.getvalue())
                self.assertNotIn("ok codex", out.getvalue())
                self.assertTrue(list(Path(directory).iterdir()), "failed smoke evidence must remain")


if __name__ == "__main__":
    unittest.main()
