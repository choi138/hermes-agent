import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest import mock
from delegate_test_support import Base, FAKE_TOKEN, SCRIPT, bash_call, stall_run, observing_wait
from delegate_test_support import common
from delegate_test_support import runtime
from delegate_test_support import watch


class WaitTests(Base):
    def test_wakes_on_rc_file(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "review.err").write_text("review-gate: boom\n", encoding="utf-8")
        ready = threading.Event()

        def finish():
            if not ready.wait(timeout=15):
                return
            Path(run_dir, "review.rc.tmp").write_text("3\n", encoding="utf-8")
            os.rename(run_dir + "/review.rc.tmp", run_dir + "/review.rc")

        writer = threading.Thread(target=finish)
        self.addCleanup(writer.join, 20)
        out = io.StringIO()
        started = time.monotonic()
        with observing_wait(ready):
            writer.start()
            code = watch.cmd_wait([run_dir, "--timeout", "20"], out=out, err=io.StringIO())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 3)
        self.assertIn("review rc=3", out.getvalue())
        self.assertIn("review-gate: boom", out.getvalue())

    def test_times_out_after_start(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        Path(run_dir, "started_at").write_text("2026-10-20T00:00:00+0900\n", encoding="utf-8")
        out = io.StringIO()
        with self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "1"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, 91)
        self.assertEqual(out.getvalue().splitlines()[-2:], ["status=no_rc", runtime.watcher_line(run_dir, 1)])
        self.assertIn("started at 2026-10-20", str(caught.exception))

    def test_never_started_fails_early(self):
        run_dir = os.path.join(self.tmp, "run")
        os.mkdir(run_dir)
        started = time.monotonic()
        out = io.StringIO()
        with self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "60", "--start-grace", "1"], out=out, err=io.StringIO())
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
        Path(run_dir, "review.out").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        Path(run_dir, "started_at").write_text("x\n", encoding="utf-8")
        Path(run_dir, "review.rc").write_text("0\n", encoding="utf-8")
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO()), 0)
        text = out.getvalue()
        self.assertIn("turns=4 tool_uses=3 (Grep:1,Read:2)", text)
        self.assertIn("verdict_line=VERDICT: APPROVED", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(Path(run_dir, "review.result.md").read_text(encoding="utf-8-sig"), "No P0/P1.\nVERDICT: APPROVED")
        Path(run_dir, "review.out").write_text(json.dumps(events[-1]) + "\n", encoding="utf-8")
        out = io.StringIO()
        watch.cmd_wait([run_dir, "--timeout", "5"], out=out, err=io.StringIO())
        self.assertIn("WARNING: the reviewer used no tools", out.getvalue())


class StallTests(Base):
    def wait(self, run_dir, *extra):
        out = io.StringIO()
        started = time.monotonic()
        code = watch.cmd_wait([run_dir, "--timeout", "30", *extra], out=out, err=io.StringIO())
        return code, out.getvalue(), time.monotonic() - started

    def test_fires_after_no_growth_and_redacts(self):
        events = [{"type": "system", "subtype": "init"}, bash_call("sleep 30"),
                  {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                  bash_call("sleep 30"), bash_call(f"curl -H 'x-api-key: {FAKE_TOKEN}' https://x")]
        run_dir = stall_run(self.tmp, events=events)
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, common.EXIT_STALLED)
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
        self.assertEqual(lines[-3], f'watcher: terminal(command="{common.HD_BIN} wait {run_dir} --timeout {common.REWATCH_MIN_SECONDS} --stall-minutes 0.02"{flags}')
        self.assertEqual(lines[-2], f'watcher_no_stall: terminal(command="{common.HD_BIN} wait {run_dir} --timeout {common.REWATCH_MIN_SECONDS} --stall-minutes 0"{flags}'
                         )
        self.assertEqual(lines[-4], "stall_count=1 of 2 (each re-arm uses one continuation of the approval's "
                                    "chain cap)")
        self.assertEqual(Path(run_dir, "stall_count").read_text(encoding="utf-8-sig"), "1\n")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn("status=finished", text)
        self.assertFalse(os.path.exists(run_dir + "/claude.rc"))
        self.assertIn(FAKE_TOKEN, Path(run_dir, "claude.out").read_text(encoding="utf-8-sig"), "a stall must not rewrite a live run")

    def test_second_stall_offers_only_no_stall_and_disables_default_check(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        Path(run_dir, "stall_count").write_text("1\n", encoding="utf-8")
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, common.EXIT_STALLED)
        lines = text.splitlines()
        self.assertIn("stall_count=2 of 2 (each re-arm uses one continuation of the approval's chain cap)", lines)
        self.assertFalse(any(line.startswith("watcher:") for line in lines), text)
        self.assertEqual(lines[-3], "stall_limit_reached: re-arm only with watcher_no_stall: "
                                    "(later waits default to --stall-minutes 0)")
        self.assertEqual(lines[-2], runtime.watcher_line(run_dir, common.REWATCH_MIN_SECONDS, 0, label="watcher_no_stall"))
        out = io.StringIO()
        with mock.patch.dict(common.STALL_DEFAULT_MINUTES, {"claude": 0.01}), self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "2"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, common.EXIT_NO_RC)
        self.assertIn("stall_check=off (2 stalls already reported for this run; pass --stall-minutes M to check "
                      "again)", out.getvalue())
        self.assertEqual(out.getvalue().splitlines()[-1], runtime.watcher_line(run_dir, 2, 0.0))
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, common.EXIT_STALLED, "an explicit --stall-minutes still checks")
        self.assertIn("stall_count=3 of 2", text)

    def test_repeated_call_count(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")] * 4)
        _, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertIn("last_tool_call=Bash: sleep 30 (same call 4x in the last 4 tool calls)", text)
        self.assertIn("last_event=Bash: sleep 30", text)

    def test_growth_resets_the_stall_clock(self):
        run_dir = stall_run(self.tmp, events=[bash_call("make")])
        stop = time.monotonic() + 2.5
        sampled = threading.Condition()
        observations = []
        original = watch.stream_state

        def observe(output):
            value = original(output)
            with sampled:
                observations.append(value)
                sampled.notify()
            return value

        def grow():
            seen = 0
            while time.monotonic() < stop:
                with sampled:
                    if not sampled.wait_for(lambda: len(observations) > seen, timeout=15):
                        return
                    seen = len(observations)
                if time.monotonic() >= stop:
                    return
                with open(run_dir + "/claude.out", "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(bash_call("make step")) + "\n")

        writer = threading.Thread(target=grow)
        writer.start()
        self.addCleanup(writer.join, 20)
        with mock.patch.object(watch, "stream_state", side_effect=observe):
            code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, common.EXIT_STALLED)
        self.assertGreaterEqual(elapsed, 2.5 + 1.1)
        growth = next(line for line in text.splitlines() if line.startswith("last_growth="))
        self.assertNotIn("no growth seen", growth)
        self.assertIn("last_tool_call=Bash: make step", text)

    def test_rerun_wait_on_old_output_does_not_fire_immediately(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        old = time.time() - 7200
        os.utime(run_dir + "/claude.out", (old, old))
        code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.02")
        self.assertEqual(code, common.EXIT_STALLED)
        self.assertGreaterEqual(elapsed, 1.1)
        self.assertIn("stalled_minutes=0.0", text)
        self.assertIn(time.strftime("last_growth=%Y-%m-%dT%H:%M", time.localtime(old)), text)

    def test_missing_output_still_stalls(self):
        run_dir = stall_run(self.tmp, kind="qa")
        os.remove(run_dir + "/qa.out")
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, common.EXIT_STALLED)
        self.assertIn("last_growth=none (qa.out not written yet)", text)
        self.assertIn("last_event=none (qa.out is empty or missing)", text)

    def test_rc_during_stall_window_wins(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        ready = threading.Event()

        def finish():
            if not ready.wait(timeout=15):
                return
            Path(run_dir, "claude.rc.tmp").write_text("4\n", encoding="utf-8")
            os.rename(run_dir + "/claude.rc.tmp", run_dir + "/claude.rc")

        writer = threading.Thread(target=finish)
        self.addCleanup(writer.join, 20)
        with observing_wait(ready):
            writer.start()
            code, text, elapsed = self.wait(run_dir, "--stall-minutes", "0.05")
        self.assertEqual(code, 4)
        self.assertLess(elapsed, 2.5)
        self.assertIn("status=finished", text)
        self.assertNotIn("status=stalled", text)

    def test_existing_rc_wins_over_stale_output(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        Path(run_dir, "claude.rc").write_text("0\n", encoding="utf-8")
        old = time.time() - 7200
        os.utime(run_dir + "/claude.out", (old, old))
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, 0)
        self.assertNotIn("stalled", text)

    def test_zero_disables(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        started = time.monotonic()
        with self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "2", "--stall-minutes", "0"], out=out, err=io.StringIO())
        self.assertGreaterEqual(time.monotonic() - started, 1.9)
        self.assertEqual(caught.exception.code, common.EXIT_NO_RC)
        self.assertEqual(out.getvalue().splitlines()[-2:], ["status=no_rc", runtime.watcher_line(run_dir, 2, 0.0)])

    def test_timeout_before_stall_is_91(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        with self.assertRaises(common.Fail) as caught:
            watch.cmd_wait([run_dir, "--timeout", "1", "--stall-minutes", "5"], out=out, err=io.StringIO())
        self.assertEqual(caught.exception.code, common.EXIT_NO_RC)
        self.assertEqual(out.getvalue().splitlines()[-1], runtime.watcher_line(run_dir, 1, 5.0))

    def test_rewatch_timeout_is_remaining_with_floor(self):
        now = time.monotonic()
        self.assertEqual(runtime.rewatch_timeout(3600, now - 600), 3000)
        self.assertEqual(runtime.rewatch_timeout(3600, now - 3590), common.REWATCH_MIN_SECONDS)
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        out = io.StringIO()
        self.assertEqual(watch.cmd_wait([run_dir, "--timeout", "4000", "--stall-minutes", "0.01"], out=out,
                                     err=io.StringIO()), common.EXIT_STALLED)
        watcher = next(line for line in out.getvalue().splitlines() if line.startswith("watcher: "))
        self.assertRegex(watcher, r" --timeout (399[0-9]|4000) --stall-minutes 0\.01\"")

    def test_signal_during_stall_watch_is_still_91(self):
        run_dir = stall_run(self.tmp, events=[bash_call("sleep 30")])
        log = os.path.join(self.tmp, "watch.out")
        with open(log, "w", encoding="utf-8") as sink:
            proc = subprocess.Popen([sys.executable, SCRIPT, "wait", run_dir, "--stall-minutes", "1"],
                                    stdout=sink, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "status=started" not in Path(log).read_text(encoding="utf-8-sig"):
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), common.EXIT_NO_RC)
        text = Path(log).read_text(encoding="utf-8-sig")
        self.assertIn("wait stopped by SIGTERM; the run keeps going", text)
        self.assertIn(f'watcher: terminal(command="{common.HD_BIN} wait {run_dir} --timeout 3', text)
        self.assertIn(' --stall-minutes 1", background=true, notify_on_complete=true', text)
        self.assertNotIn("status=stalled", text)

    def test_codex_stream_summary(self):
        events = [{"type": "thread.started"}] + [
            {"type": "item.started", "item": {"type": "command_execution", "command": "xcrun simctl list",
                                              "status": "in_progress"}}] * 3
        run_dir = stall_run(self.tmp, kind="qa", events=events)
        code, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertEqual(code, common.EXIT_STALLED)
        self.assertIn("last_tool_call=command (in_progress): xcrun simctl list (same call 3x in the last 3 tool "
                      "calls)", text)

    def test_non_json_output_tail(self):
        run_dir = stall_run(self.tmp, kind="review")
        Path(run_dir, "review.out").write_text(f"plain line\nwaiting for input {FAKE_TOKEN}\n", encoding="utf-8")
        _, text, _ = self.wait(run_dir, "--stall-minutes", "0.01")
        self.assertIn("last_event=waiting for input sk-ant-oat[REDACTED]", text)
        self.assertNotIn(FAKE_TOKEN, text)

    def test_cut_tail_line_is_dropped(self):
        path = os.path.join(self.tmp, "big.out")
        Path(path).write_text("x" * 50 + FAKE_TOKEN[3:] + "\n" + json.dumps(bash_call("make")) + "\n", encoding="utf-8")
        events, last_raw = watch.tail_events(path, limit=len(FAKE_TOKEN) + 30 + len(json.dumps(bash_call("make"))))
        self.assertEqual(len(events), 1)
        self.assertNotIn(FAKE_TOKEN[10:], "".join(watch.stall_summary(path)))
        events, last_raw = watch.tail_events(path, limit=40)
        self.assertEqual(events, [])
        self.assertNotIn(FAKE_TOKEN[3:20], last_raw or "")

    def test_per_kind_defaults_and_validation(self):
        self.assertEqual(watch.stall_seconds("claude", None), 1200)
        self.assertEqual(watch.stall_seconds("review", None), 1200)
        self.assertEqual(watch.stall_seconds("qa", None), 1800)
        self.assertEqual(watch.stall_seconds("qa", 5), 300)
        self.assertEqual(watch.stall_seconds("claude", 0), 0)
        for bad in ("-1", "nan", "inf"):
            run_dir = stall_run(self.tmp, name="bad" + bad)
            with self.assertRaises(common.Fail) as caught:
                watch.cmd_wait([run_dir, "--stall-minutes", bad], out=io.StringIO(), err=io.StringIO())
            self.assertEqual(caught.exception.code, 2)

    def test_default_stall_used_when_flag_omitted(self):
        seen = []
        original = watch.wait_for_rc_or_stall
        watch.wait_for_rc_or_stall = lambda rc, run_dir, output, deadline, stall: seen.append((output, stall))
        try:
            for kind in ("claude", "review", "qa"):
                run_dir = stall_run(self.tmp, kind=kind, name=kind)
                Path(run_dir, kind + ".rc").write_text("0\n", encoding="utf-8")
                watch.cmd_wait([run_dir], out=io.StringIO(), err=io.StringIO())
        finally:
            watch.wait_for_rc_or_stall = original
        self.assertEqual([(os.path.basename(o), s) for o, s in seen],
                         [("claude.out", 1200), ("review.out", 1200), ("qa.out", 1800)])

    def test_slept_time_is_not_counted(self):
        run_dir = stall_run(self.tmp)
        clock = {"t": 1000.0, "calls": 0}

        def fake_until(ready, directory, deadline):
            clock["calls"] += 1
            clock["t"] = deadline + (3600 if clock["calls"] == 1 else 0)
            return ready()

        originals = watch.wait_until, time.monotonic
        watch.wait_until, time.monotonic = fake_until, lambda: clock["t"]
        try:
            result = watch.wait_for_rc_or_stall(run_dir + "/claude.rc", run_dir, run_dir + "/claude.out",
                                             1000.0 + 86400, 1200)
        finally:
            watch.wait_until, time.monotonic = originals
        self.assertEqual(result[0], 1200)
        self.assertGreaterEqual(clock["t"], 1000.0 + 3600 + 1200)
