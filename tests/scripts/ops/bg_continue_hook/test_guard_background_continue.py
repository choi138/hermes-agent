import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOK = str(Path(__file__).resolve().parents[4] / "scripts/ops/bg-continue-hook/guard_background_continue.py")


_TEMP_DIRS = []


def teardown_module():
    for path in _TEMP_DIRS:
        shutil.rmtree(path)


def run(payload, env_extra=None, raw=None):
    tmp = tempfile.mkdtemp()
    _TEMP_DIRS.append(tmp)
    log = os.path.join(tmp, "sub", "hook.jsonl")
    env = dict(os.environ, BG_CONTINUE_HOOK_LOG=log)
    env.pop("TERMINAL_MAX_FOREGROUND_TIMEOUT", None)
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload)
    proc = subprocess.run([sys.executable, HOOK], input=data, capture_output=True, text=True, env=env)
    lines = []
    if os.path.exists(log):
        with open(log, encoding="utf-8") as handle:
            lines = [json.loads(x) for x in handle if x.strip()]
    return proc, lines, log


def term(**args):
    return {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
            "tool_input": dict(command="echo SECRET_SENTINEL_9f3", **args),
            "session_id": "s1", "cwd": "/", "extra": {}}


class HookTests(unittest.TestCase):
    def assertModified(self, payload, expected):
        proc, lines, _ = run(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {"action": "modify", "args": expected})
        self.assertEqual([x["decision"] for x in lines], ["modified"])

    def assertUntouched(self, payload):
        proc, lines, _ = run(payload)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(lines, [])

    def test_background_notify_without_continue_is_armed(self):
        self.assertModified(term(background=True, notify=True),
                            {"continue_on_complete": True, "persist_on_release": True})

    def test_notify_on_complete_alias_is_armed(self):
        self.assertModified(term(background=True, notify_on_complete=True),
                            {"continue_on_complete": True, "persist_on_release": True})

    def test_heartbeat_implies_notify(self):
        self.assertModified(term(background=True, heartbeat=60),
                            {"continue_on_complete": True, "persist_on_release": True})

    def test_explicit_persist_is_kept(self):
        self.assertModified(term(background=True, notify=True, persist_on_release=False),
                            {"continue_on_complete": True})

    def assertOptedOut(self, payload):
        proc, lines, _ = run(payload)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr)
        self.assertEqual([x["decision"] for x in lines], ["opted-out"])

    def test_explicit_continue_true_or_false_is_respected(self):
        self.assertUntouched(term(background=True, notify=True, continue_on_complete=True))
        self.assertOptedOut(term(background=True, notify=True, continue_on_complete=False))
        self.assertOptedOut(term(timeout=900, continue_on_complete=False))

    def test_only_boolean_false_is_an_opt_out(self):
        for value in (None, "false", 0):
            with self.subTest(value=value):
                self.assertUntouched(term(background=True, notify=True, continue_on_complete=value))
                proc, lines, _ = run(term(timeout=900, continue_on_complete=value))
                self.assertEqual(proc.returncode, 2)
                self.assertEqual([x["decision"] for x in lines], ["blocked"])
        proc, _, _ = run(term(timeout=900, continue_on_complete=True))
        self.assertEqual(proc.returncode, 2)

    def test_explicit_notify_wins_over_notify_on_complete(self):
        self.assertUntouched(term(background=True, notify=False, notify_on_complete=True))
        self.assertUntouched(term(background=True, notify=["ready"], notify_on_complete=True))
        self.assertUntouched(term(background=True, notify=False, continue_on_complete=False))
        self.assertModified(term(background=True, notify=["ready"], heartbeat=60),
                            {"continue_on_complete": True, "persist_on_release": True})
        self.assertUntouched(term(background=True, heartbeat=True))

    def test_background_without_exit_notification_untouched(self):
        self.assertUntouched(term(background=True))  # server/daemon
        self.assertUntouched(term(background=True, notify=False))
        self.assertUntouched(term(background=True, notify=["ready"]))  # watch patterns
        self.assertUntouched(term(background=True, heartbeat=0))

    def test_long_foreground_is_blocked(self):
        proc, lines, _ = run(term(timeout=920))
        self.assertEqual(proc.returncode, 2)
        body = json.loads(proc.stdout)
        self.assertEqual(body["action"], "block")
        self.assertIn("continue_on_complete=true", body["message"])
        self.assertEqual([x["decision"] for x in lines], ["blocked"])

    def test_foreground_at_or_below_cap_untouched(self):
        self.assertUntouched(term(timeout=600))
        self.assertUntouched(term(timeout=30))
        self.assertUntouched(term())
        self.assertUntouched(term(timeout="900"))  # not a number: leave to the tool's own validation
        self.assertUntouched(term(timeout=True))

    def test_cap_follows_env(self):
        proc, _, _ = run(term(timeout=700), {"TERMINAL_MAX_FOREGROUND_TIMEOUT": "800"})
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        proc, _, _ = run(term(timeout=700), {"TERMINAL_MAX_FOREGROUND_TIMEOUT": "junk"})
        self.assertEqual(proc.returncode, 2)

    def test_other_tools_and_events_untouched(self):
        self.assertUntouched({"hook_event_name": "pre_tool_call", "tool_name": "execute_code",
                              "tool_input": {"code": "x", "background": True, "notify": True}})
        self.assertUntouched(dict(term(background=True, notify=True), hook_event_name="post_tool_call"))

    def test_malformed_input_fails_open(self):
        for raw in ("garbage", "", "[]", '{"hook_event_name":"pre_tool_call","tool_name":"terminal","tool_input":"x"}'):
            proc, lines, _ = run(None, raw=raw)
            self.assertEqual((proc.returncode, proc.stdout, lines), (0, "", []), raw)

    def test_null_tool_input_ignores_other_keys(self):
        self.assertUntouched({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                              "tool_input": None, "args": {"timeout": 601}})
        self.assertUntouched({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                              "args": {"background": True, "notify": True}})

    def test_existing_log_is_tightened_to_0600(self):
        tmp = tempfile.mkdtemp()
        _TEMP_DIRS.append(tmp)
        log = os.path.join(tmp, "h.jsonl")
        with open(log, "w"):
            pass
        os.chmod(log, 0o644)
        proc, _, _ = run(term(background=True, notify=True), {"BG_CONTINUE_HOOK_LOG": log})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(oct(os.stat(log).st_mode & 0o777), "0o600")

    def test_log_never_contains_command_and_is_private(self):
        proc, lines, log = run(term(background=True, notify=True))
        with open(log, encoding="utf-8") as handle:
            self.assertNotIn("SECRET_SENTINEL_9f3", handle.read())
        self.assertEqual(oct(os.stat(log).st_mode & 0o777), "0o600")
        self.assertEqual(lines[0]["session_id"], "s1")

    def test_internal_error_fails_open_and_logs(self):
        tmp = tempfile.mkdtemp()
        _TEMP_DIRS.append(tmp)
        log = os.path.join(tmp, "h.jsonl")
        script = (
            "import importlib.util,sys,io,json\n"
            "spec=importlib.util.spec_from_file_location('h',%r)\n"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
            "def boom(p): raise RuntimeError('x')\n"
            "m.decide=boom\n"
            "sys.stdin=io.StringIO(json.dumps({'hook_event_name':'pre_tool_call','tool_name':'terminal','tool_input':{}}))\n"
            "sys.exit(m.main())\n" % HOOK)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              env=dict(os.environ, BG_CONTINUE_HOOK_LOG=log))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        with open(log, encoding="utf-8") as handle:
            rec = json.loads(handle.readline())
        self.assertEqual((rec["decision"], rec["error"]), ("error", "RuntimeError"))


if __name__ == "__main__":
    unittest.main()
