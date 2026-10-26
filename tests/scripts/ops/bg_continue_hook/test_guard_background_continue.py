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


def run(payload, env_extra=None, raw=None, hook=HOOK):
    tmp = tempfile.mkdtemp()
    _TEMP_DIRS.append(tmp)
    log = os.path.join(tmp, "sub", "hook.jsonl")
    env = dict(os.environ, BG_CONTINUE_HOOK_LOG=log)
    env.pop("TERMINAL_MAX_FOREGROUND_TIMEOUT", None)
    env.update(env_extra or {})
    data = raw if raw is not None else json.dumps(payload)
    proc = subprocess.run([sys.executable, "-B", hook], input=data, capture_output=True, text=True, env=env)
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


STATE = "/Users/choegeun-won/.local/state/hermes-delegate"
Q = STATE + "/qa-artifacts/b2b-org-confirm-ios-a4ddb56550"
# Production koharu commands (2026-10-22..26) that sleep-polled a running delegation.
PEEK_COMMANDS = [
    "sleep 20; pgrep -f run_codex_task.py | wc -l; pgrep -x codex | wc -l",
    "sleep 45; R=%s/claude/x; ls $R" % STATE,
    "sleep 20; pgrep -f 'codex exec' | wc -l; ls %s/codex/20261023T081606-81cef" % STATE,
    "R=%s/qa/ios-r3; for i in $(seq 1 40); do [ -f $R/qa.rc ] && break; sleep 13; done; cat $R/qa.rc" % STATE,
    "until test -f $HOME/.local/state/hermes-delegate/qa/x/qa.rc; do sleep 10; done",
    "sleep 40; D=%s/qa/archive-8dbc22; ls $D; cat $D/started_at 2>&1; head -c 600 $D/qa.out" % STATE,
    'sleep 25; ps -axo pid=,etime=,command= | grep -E "[c]odex exec|[c]laude -p --model" | cut -c1-60',
    'sleep 20; for d in ~/.local/state/hermes-delegate/claude/a ~/.local/state/hermes-delegate/claude/b; '
    'do echo "$(basename $d) $(wc -c < $d/claude.out 2>/dev/null)"; done',
    "sleep 20; cd ~/Documents/x; E=shots; ls -la ~/.local/state/hermes-delegate/review/r1/ | awk '{print $5}'",
    "sleep 1m; pkill -0 -f 'claude -p'",
    "bash -lc 'sleep 30; pgrep -f hermes-delegate'",
    "ssh mac 'sleep 20; ls %s/codex/x'" % STATE,
]
CI_POLL_COMMANDS = [
    "for i in $(seq 1 14); do S=$(gh run list --repo r --branch main --limit 1 --json status --jq '.[0].status'); "
    "[ \"$S\" = completed ] && break; sleep 20; done",
    "gh pr comment 37 --body x; for i in $(seq 1 12); do sleep 10; R=$(gh run list --limit 1); "
    "case \"$R\" in *completed*) break;; esac; done",
    "until gh pr checks 5 --repo r; do sleep 30; done",
    "while true; do gh run view 1 --json conclusion -q .conclusion | grep -q success && break; sleep 20; done",
    "for i in 1 2 3 4 5; do gh api repos/o/r/actions/runs/9 -q .conclusion; sleep 15; done",
]
ALLOWED_COMMANDS = [
    "hermes-delegate wait %s/codex/run --timeout 3600" % STATE,
    "hermes-delegate status %s/codex/run" % STATE,
    "sleep 30; hermes-delegate status %s/codex/run" % STATE,
    "sleep 1; curl -s localhost:4789/api/threads",
    "pgrep -f 'codex exec' | wc -l",
    "ls %s/codex/20261023T081606-81cef" % STATE,
    "sleep 20 && gh pr checks 18 --watch --fail-fast",
    "sleep 1; pgrep -f 'codex exec'",
    "until curl -sf localhost:3000; do sleep 2; done; echo codex claude",
    # cancel-then-confirm is a one-shot check, not a poll
    "R=%s/review/x; hermes-delegate cancel $R | tail -3; sleep 3; P=$(cat $R/inner.pid); kill -0 $P" % STATE,
    "echo 'sleep 20; ls %s/x'" % STATE,
    "cat > /tmp/w.sh <<'EOF'\nsleep 20\nls %s/x\nEOF\n" % STATE,
    # P1-2 (10-24 13:06, 10-25 12:58): the check runs once, the sleep lives in a separate ssh body
    'ps -axo command= | grep -cE "[c]laude -p --model|[r]un_codex_task.py"; scp -q gw.sh h:.local/state/gw.sh '
    "&& ssh choi138-ri 'systemd-run --user --unit fdd-gw-watch gw.sh && sleep 2 && "
    "systemctl --user is-active fdd-gw-watch'",
    "pgrep -f 'codex exec' | wc -l; sleep 5; echo ok",
    "until curl -sf localhost:3000; do sleep 2; done; pgrep -f 'codex exec'",
    "sleep 20; rmdir %s/codex/x; git status" % STATE,
    # CI: watchers, startup delays and run-id lookups that feed a watcher
    "sleep 10 && gh run watch 123 --exit-status",
    "sleep 10; timeout 400 gh run watch 39738541369 --exit-status; gh run view 39738541369 --json jobs",
    "gh run rerun 1 --failed && sleep 15; gh run view 1 --json status,attempt",
    "for k in a b c; do gh workflow run x.yaml -f k=$k; done; sleep 15; gh run list -w x.yaml -L 3",
    "sha=$(gh api repos/r/commits/main -q .sha); for i in $(seq 1 12); do run=$(gh run list --commit $sha); "
    "[ -n \"$run\" ] && break; sleep 5; done; timeout 570 gh run watch \"$run\" --exit-status; gh run view $run",
    "gh run watch 3 --exit-status; sleep 5; gh pr checks 16",
    "sleep 15; gh run list --branch main --limit 1 --json databaseId",
    "gh pr checks 18; sleep 3",
    "pkill -f idb; sleep 2; xcrun simctl shutdown X; gh run list --repo r --limit 30",
    "for n in 27 30; do for i in 1 2 3; do st=$(gh pr view $n --json mergeStateStatus); "
    "[ \"$st\" != UNKNOWN ] && break; sleep 10; done; gh pr checks $n; done",
    # one-shot sleep + CI check is allowed (only polling loops are CI polls)
    "sleep 60; cd ~/Documents/fido-dashboard && export PATH=/opt/homebrew/bin:$PATH; gh pr checks 1 --repo r",
    "sleep 45; gh run view 39967795161 --repo r --json status,jobs",
    "sleep 20; gh pr view 6 -R r --json state,statusCheckRollup",
    # approval-gated deploys (10-26 08:46, 10:26, 10:39): gh run watch would not return before approval
    "R=silviahealth/silviahealth; for i in 1 2 3 4 5 6 7 8 9 10 11 12; do s=$(gh run view 40246265688 -R $R "
    "--json jobs -q '.jobs[]|.name+\"=\"+.status' | tr '\\n' ' '); echo \"$s\" | "
    "grep -q \"Check Production Deploy=in_progress\" && break; sleep 10; done; echo \"$s\"",
    "sleep 40; R=silviahealth/silviahealth; gh run view 40254185920 -R $R --json jobs,inputs; "
    "gh issue list -R $R -L 3 --search \"40254185920 in:body\" --json number,title,state",
    "sleep 30; gh run view 40255282659 -R silviahealth/sora --json status,jobs; "
    "gh issue list -R silviahealth/sora -L 2 --search \"40255282659 in:title\" --json number,state",
    "for i in $(seq 1 20); do gh api repos/o/r/actions/runs/9/pending_deployments -q length; sleep 15; done",
    "for i in $(seq 1 18); do s=$(gh pr checks 25 | grep CodeRabbit); case \"$s\" in *pending*) sleep 15;; "
    "*) break;; esac; done",
    # result-file waits for cmux jobs launched in an earlier call (no notification, Keychain/GUI)
    "for i in $(seq 1 55); do [ -f /private/tmp/b2b1648-job/install.rc ] && break; sleep 10; done; "
    "echo RC=$(cat /private/tmp/b2b1648-job/install.rc)",
    "D=$HOME/.cache/fido-cake-probe; for i in $(seq 1 45); do [ -f $D/job.rc ] && break; sleep 2; done",
    "for i in $(seq 1 18); do grep -q DONE_RC ~/hermes-workspaces/x/mutation.log && break; sleep 30; done",
    "timeout 120 bash -c 'until [ -f /tmp/a.done ]; do sleep 5; done'",
    "for i in $(seq 1 30); do curl -sf localhost:8080/healthz && break; sleep 2; done",
    "until adb shell getprop sys.boot_completed | grep -q 1; do sleep 3; done",
]
# P1-1 (10-23 11:34, 18:25, 18:44, 18:51, 19:05, 19:17): background notify watchers required by
# native-app-runtime-verification must never be blocked by the sleep rules.
OTP_WATCHER = ("Q=%s; R=%s/qa/b2b-org-qa-ios; while [ ! -f $Q/WAITING_FOR_OTP ] && [ ! -f $R/qa.rc ]; "
               "do sleep 20; done; cat $R/qa.rc" % (Q, STATE))


def peek(command, **args):
    return {"hook_event_name": "pre_tool_call", "tool_name": "terminal",
            "tool_input": dict(command=command, **args), "session_id": "s1"}


class SleepPollTests(unittest.TestCase):
    def assertSleepBlocked(self, payload, decision, hook=HOOK):
        proc, lines, _ = run(payload, hook=hook)
        self.assertEqual(proc.returncode, 2, payload["tool_input"]["command"])
        body = json.loads(proc.stdout)
        self.assertEqual(body["action"], "block")
        self.assertEqual([x["decision"] for x in lines], [decision])
        return body["message"]

    def assertAllowed(self, payload, hook=HOOK):
        proc, lines, _ = run(payload, hook=hook)
        self.assertEqual((proc.returncode, proc.stdout, lines), (0, "", []), payload["tool_input"]["command"])

    def test_production_sleep_polls_are_blocked(self):
        for command in PEEK_COMMANDS:
            with self.subTest(command=command):
                message = self.assertSleepBlocked(peek(command), "blocked_peek")
                self.assertIn("hermes-delegate status PATH", message)

    def test_ci_sleep_polls_are_blocked(self):
        for command in CI_POLL_COMMANDS:
            with self.subTest(command=command):
                message = self.assertSleepBlocked(peek(command), "blocked_ci_poll")
                self.assertIn("gh pr checks <PR> --watch --fail-fast", message)
                self.assertIn("notify_on_complete=true", message)
                self.assertIn("approval gate, a single check is fine", message)

    def test_official_checks_and_unrelated_sleeps_are_allowed(self):
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertAllowed(peek(command))

    def test_background_watchers_are_never_sleep_blocked(self):
        for command in [OTP_WATCHER] + PEEK_COMMANDS[:3] + CI_POLL_COMMANDS[:2]:
            with self.subTest(command=command):
                proc, lines, _ = run(peek(command, background=True, notify_on_complete=True))
                self.assertEqual(proc.returncode, 0)
                self.assertEqual([x["decision"] for x in lines], ["modified"])
                self.assertAllowed(peek(command, background=True))
        self.assertSleepBlocked(peek(OTP_WATCHER), "blocked_peek")

    def test_long_foreground_rule_still_wins(self):
        proc, lines, _ = run(peek(PEEK_COMMANDS[0], timeout=900))
        self.assertEqual([x["decision"] for x in lines], ["blocked"])

    def test_short_or_unknown_sleep_is_not_a_poll(self):
        for command in ("sleep 1.5; pgrep -f 'codex exec'", "sleep $N; pgrep -f 'codex exec'"):
            self.assertAllowed(peek(command))
        self.assertSleepBlocked(peek("sleep 2; pgrep -f 'codex exec'"), "blocked_peek")

    def test_without_shared_tokenizer_the_fallback_still_decides(self):
        tmp = tempfile.mkdtemp()
        _TEMP_DIRS.append(tmp)
        alone = os.path.join(tmp, "guard_background_continue.py")
        shutil.copy(HOOK, alone)
        for command, decision in [(c, "blocked_peek") for c in PEEK_COMMANDS] + [
                (c, "blocked_ci_poll") for c in CI_POLL_COMMANDS]:
            with self.subTest(command=command):
                self.assertSleepBlocked(peek(command), decision, hook=alone)
        for command in ALLOWED_COMMANDS:
            with self.subTest(command=command):
                self.assertAllowed(peek(command), hook=alone)

    def test_oversized_command_uses_fallback(self):
        filler = "; ".join("echo %d" % i for i in range(12000))
        self.assertSleepBlocked(peek(filler + "; sleep 20; ls %s/x" % STATE), "blocked_peek")
        self.assertAllowed(peek(filler + "; sleep 20; echo done"))


if __name__ == "__main__":
    unittest.main()
