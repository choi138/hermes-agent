"""Cover launch forms added in 37f6eafeda without changing hook detection."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hooks import guard_review_gate as hook


class LaunchTests(unittest.TestCase):
    def check(self, command, blocked):
        with tempfile.TemporaryDirectory(prefix="review-launch-") as temporary:
            log = Path(temporary) / "hook.jsonl"
            payload = dict(hook_event_name="pre_tool_call", tool_name="terminal",
                           tool_input=dict(command=command), session_id="test")
            stdout = io.StringIO()
            with patch.dict(os.environ, REVIEW_GATE_HOOK_LOG=str(log)):
                code = hook.main(io.StringIO(json.dumps(payload)), stdout)
            self.assertEqual(code, 2 if blocked else 0, stdout.getvalue())
            if blocked:
                self.assertEqual(json.loads(stdout.getvalue())["action"], "block")
                self.assertTrue(any(json.loads(line)["decision"] == "blocked" for line in log.read_text().splitlines()))
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertFalse(log.exists())

    def test_cmux_workspace_claude(self):
        self.check("cmux new-workspace --command 'claude -p review this diff'", True)

    def test_cmux_pane_codex(self):
        self.check('cmux new-pane --command="codex review --uncommitted"', True)

    def test_cat_script_heredoc(self):
        self.check("cat > /tmp/x.sh <<'EOF'\ncodex review --uncommitted\nEOF", True)

    def test_tee_script_heredoc(self):
        self.check("tee run.zsh <<'EOF'\ncodex review --uncommitted\nEOF", True)

    def test_screen_launch(self):
        self.check("screen -S r codex review --uncommitted", True)

    def test_cmux_tests_allowed(self):
        self.check("cmux new-workspace --command 'npm test'", False)

    def test_notes_heredoc_allowed(self):
        self.check("cat > notes.md <<'EOF'\ncodex review is mentioned here\nEOF", False)

    def test_screen_list_allowed(self):
        self.check("screen -ls", False)
