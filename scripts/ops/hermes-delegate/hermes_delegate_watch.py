"""Completion watching, cancellation and result summaries."""

import json
import math
import os
from pathlib import Path
import re
import select
import signal
import sys
import time
import hermes_delegate_common as common
import hermes_delegate_runtime as runtime


def wait_until(ready, directory, deadline):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        fd = None
    try:
        kq = select.kqueue() if fd is not None and hasattr(select, "kqueue") else None
        event = (select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE) if kq else None)
        while not ready():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if kq:
                kq.control([event], 1, min(30.0, remaining))
            else:
                time.sleep(min(5.0, remaining))
        return True
    finally:
        if fd is not None:
            os.close(fd)


def stall_seconds(kind, minutes):
    if minutes is None:
        minutes = common.STALL_DEFAULT_MINUTES[kind]
    if not math.isfinite(minutes) or minutes < 0:
        raise common.Fail("--stall-minutes must be 0 (off) or a positive number of minutes", 2)
    return minutes * 60.0


def stall_count(run_dir):
    try:
        text = Path(run_dir, "stall_count").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return 0
    return int(text) if re.fullmatch(r"[0-9]{1,4}", text) else 0


def stream_state(output):
    try:
        info = os.stat(output)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def wait_for_rc_or_stall(rc_path, run_dir, output, deadline, stall):
    """Wait for rc_path until deadline; returns None (rc or deadline) or (seconds stalled, last growth wall time).

    Growth is any change of the output's size or mtime between samples, so wall-clock jumps cannot fake or hide
    growth. Stalled time is this wait's monotonic time since max(last growth seen, this wait's start). On macOS
    monotonic time stops during sleep; a monotonic gap far beyond the requested wait is not counted either,
    because the run was suspended too."""
    def ready():
        return os.path.exists(rc_path)

    if not stall:
        wait_until(ready, run_dir, deadline)
        return None
    last = stream_state(output)
    last_wall = None
    mono = quiet_since = time.monotonic()
    while True:
        until = min(deadline, quiet_since + stall, mono + common.STALL_SAMPLE_SECONDS)
        if wait_until(ready, run_dir, until):
            return None
        now, now_wall = time.monotonic(), time.time()
        excess = (now - mono) - max(until - mono, 0.0)
        if excess > common.SLEEP_GAP_SECONDS:
            quiet_since = min(quiet_since + excess, now)
        mono = now
        state = stream_state(output)
        if state != last:
            last, last_wall, quiet_since = state, now_wall, now
        elif now - quiet_since >= stall and not ready():
            return now - quiet_since, last_wall
        if now >= deadline:
            return None


def clip(text, limit=200):
    text = common.TOKEN_PATTERN.sub(common.TOKEN_PREFIX + "[REDACTED]", " ".join(str(text).split()))
    return text if len(text) <= limit else text[:limit - 3] + "..."


def tail_events(output, limit=1024 * 1024):
    try:
        with open(output, "rb") as handle:
            start = max(os.fstat(handle.fileno()).st_size - limit, 0)
            handle.seek(start)
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return [], None
    if start:
        # The cut first line may start mid-token, where TOKEN_PATTERN cannot see the prefix.
        lines = lines[1:]
    events, last_raw = [], None
    for line in lines:
        if not line.strip():
            continue
        last_raw = line
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events, last_raw


def describe_input(value):
    if isinstance(value, dict):
        for key in ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, sort_keys=True)
    return str(value)


def describe_event(item):
    """(is a tool call, text) for one claude stream-json or codex --json event."""
    kind = item.get("type")
    if kind == "assistant":
        blocks = [b for b in (item.get("message") or {}).get("content") or [] if isinstance(b, dict)]
        tools = [b for b in blocks if b.get("type") == "tool_use"]
        if tools:
            return True, "{}: {}".format(tools[-1].get("name"), describe_input(tools[-1].get("input")))
        texts = [b.get("text") or b.get("thinking") or "" for b in blocks]
        return False, "assistant: " + (" ".join(t for t in texts if t) or "(empty)")
    if kind == "user":
        blocks = [b for b in (item.get("message") or {}).get("content") or [] if isinstance(b, dict)]
        for block in reversed(blocks):
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                return False, "tool_result{}: {}".format(" (error)" if block.get("is_error") else "", content)
        return False, "user message"
    if kind in ("item.started", "item.updated", "item.completed") and isinstance(item.get("item"), dict):
        inner = item["item"]
        name = inner.get("type") or inner.get("item_type")
        detail = inner.get("command") or inner.get("text") or inner.get("query") or inner.get("server") or ""
        if name == "command_execution":
            status = inner.get("status") or kind.split(".")[1]
            code = inner.get("exit_code")
            return kind == "item.started" or code is None, "command ({}{}): {}".format(
                status, "" if code is None else f", exit {code}", detail)
        return name in ("mcp_tool_call", "web_search", "file_change"), f"{name}: {detail}"
    if kind in ("error", "turn.failed"):
        return False, "{}: {}".format(kind, item.get("message") or item.get("error"))
    return False, "{}{}".format(kind, " " + str(item.get("subtype")) if item.get("subtype") else "")


def stall_summary(output):
    events, last_raw = tail_events(output)
    if not events:
        return ["last_event=" + (clip(last_raw) if last_raw else f"none ({os.path.basename(output)} is empty or missing)")]
    described = [describe_event(item) for item in events]
    calls = [text for is_call, text in described if is_call]
    lines = []
    if calls:
        recent = calls[-10:]
        lines.append(f"last_tool_call={clip(calls[-1])} (same call {recent.count(calls[-1])}x in the last {len(recent)} tool calls)")
    lines.append("last_event=" + clip(described[-1][1]))
    return lines


def report_stall(out, kind, output, stalled, last_wall, run_dir, timeout, stall_minutes):
    if last_wall is not None:
        growth = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last_wall))
    else:
        state = stream_state(output)
        growth = ("{} ({} mtime; no growth seen by this wait)".format(
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(state[1] / 1e9)), kind + ".out")
            if state else f"none ({kind}.out not written yet)")
    print("status=stalled", file=out)
    print(f"last_growth={growth}", file=out)
    print(f"stalled_minutes={stalled / 60.0:.1f} ({kind}.out has not grown for this long while this wait watched)", file=out)
    for line in stall_summary(output):
        print(line, file=out)
    count = stall_count(run_dir) + 1
    try:
        Path(run_dir, "stall_count").write_text(f"{count}\n", encoding="utf-8")
    except OSError:
        pass
    print(f"stall_count={count} of {common.STALL_REARM_LIMIT} (each re-arm uses one continuation of the approval's chain cap)", file=out)
    if count < common.STALL_REARM_LIMIT:
        print(runtime.watcher_line(run_dir, timeout, stall_minutes), file=out)
    else:
        print("stall_limit_reached: re-arm only with watcher_no_stall: (later waits default to --stall-minutes 0)",
              file=out)
    print(runtime.watcher_line(run_dir, timeout, 0, label="watcher_no_stall"), file=out)
    print("the run keeps going; re-run hermes-delegate wait RUN_DIR to keep watching (optionally --stall-minutes N) "
          "or stop it with hermes-delegate cancel RUN_DIR", file=out)


def json_lines(output):
    try:
        lines = Path(output).read_text(errors="replace", encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


def summarize_stream(output, run_dir, kind="review"):
    """Turn claude stream-json into a verdict summary and <kind>.result.md."""
    tool_uses, names, final, model = 0, {}, None, None
    for item in json_lines(output):
        if item.get("type") == "system" and item.get("subtype") == "init":
            model = item.get("model")
        if item.get("type") == "assistant":
            for block in (item.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses += 1
                    names[block.get("name")] = names.get(block.get("name"), 0) + 1
        elif item.get("type") == "result":
            final = item
    if final is None:
        return [f"result=missing (no stream-json result event in {kind}.out)"]
    text = final.get("result") if isinstance(final.get("result"), str) else ""
    result = f"{run_dir}/{kind}.result.md"
    Path(result).write_text(text, encoding="utf-8")
    summary = ["result={} turns={} tool_uses={} ({}) is_error={} permission_denials={}".format(
        result, final.get("num_turns"), tool_uses,
        ",".join(f"{k}:{v}" for k, v in sorted(names.items())) or "none",
        final.get("is_error"), len(final.get("permission_denials") or [])),
        ("verdict_line=" if kind == "review" else "last_line=") + common.last_line(text)]
    if model:
        summary.append("model=" + str(model))
    if tool_uses == 0:
        summary.append("WARNING: the reviewer used no tools; do not accept an approval without a transcript"
                       if kind == "review" else "WARNING: the worker used no tools; it changed nothing")
    return summary


def summarize_codex(output, run_dir, kind="qa"):
    """Turn codex exec --json events and the -o last message into a summary."""
    items, usage, errors = {}, None, []
    for event in json_lines(output):
        event_type = event.get("type")
        if event_type == "item.completed" and isinstance(event.get("item"), dict):
            name = event["item"].get("type") or event["item"].get("item_type")
            items[name] = items.get(name, 0) + 1
        elif event_type == "turn.completed":
            usage = event.get("usage")
        elif event_type in ("error", "turn.failed"):
            errors.append(str(event.get("message") or event.get("error"))[:300])
    final = f"{run_dir}/{kind}.final.md"
    text = Path(final).read_text(errors="replace", encoding="utf-8-sig") if os.path.exists(final) else ""
    summary = ["result={} items=({}) usage={}".format(
        final if text else "missing", ",".join(f"{k}:{v}" for k, v in sorted(items.items())) or "none",
        json.dumps(usage) if usage else "none"), ("verdict_line=" if kind == "review" else "last_line=")
        + common.last_line(text)]
    if kind == "review" and text:
        Path(run_dir, "review.result.md").write_text(text, encoding="utf-8")
    summary += ["error=" + error for error in errors[-3:]]
    if not items.get("command_execution"):
        summary.append("WARNING: the reviewer ran no commands; do not accept an approval without a transcript"
                       if kind == "review" else "WARNING: the QA worker ran no commands; there is no device evidence")
    return summary


def redact_tokens(run_dir):
    """Mask any OAuth token value that leaked into run dir outputs; returns the touched file names."""
    touched = []
    for path in sorted(Path(run_dir).iterdir()):
        if not path.is_file() or path.name in ("inner.zsh", "lock") or path.stat().st_size > 64 * 1024 * 1024:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if common.TOKEN_PREFIX.encode() not in data:
            continue
        text = data.decode("utf-8", errors="surrogateescape")
        path.write_bytes(common.TOKEN_PATTERN.sub(common.TOKEN_PREFIX + "[REDACTED]", text).encode(
            "utf-8", errors="surrogateescape"))
        touched.append(path.name)
    return touched


def close_workspace(meta, out):
    workspace = meta.get("cmux_workspace")
    if meta.get("via", "cmux") != "cmux" or not workspace:
        return
    cmux = runtime.tools()[1]
    detail = "cmux not executable"
    # inner.zsh writes the rc just before it exits; cmux refuses to close a workspace whose process is
    # still running, and --force could kill something else, so retry briefly instead.
    for attempt in range(6):
        if not os.access(cmux, os.X_OK):
            break
        result = common.run([cmux, "close-workspace", "--workspace", workspace], timeout=15,
                     env=dict(os.environ, CMUX_QUIET="1"))
        if result.returncode == 0:
            print(f"cmux_workspace={workspace} closed", file=out)
            return
        detail = common.last_line(result.stderr or result.stdout)
        if "not_found" in detail:
            print(f"cmux_workspace={workspace} already gone", file=out)
            return
        time.sleep(1 + attempt)
    print(f"WARNING: cmux_workspace={workspace} left open: {detail}", file=out)


def worktree_summary(run_dir, meta):
    try:
        after = Path(run_dir, "head_after").read_text(encoding="utf-8-sig").strip()
        status = Path(run_dir, "status_after").read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ["worktree=unknown (no head_after/status_after)"]
    moved = "unchanged" if after == meta.get("head") else "moved to " + after[:12]
    return ["worktree: HEAD {} {}, {} uncommitted entries (status_after)".format(
        (meta.get("head") or "?")[:12], moved, len([line for line in status if line.strip()]))]


class WatcherStopped(Exception):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def inner_pid(run_dir):
    """The pid in RUN_DIR/inner.pid, only while that process is still this run's inner.zsh."""
    try:
        pid = int(Path(run_dir, "inner.pid").read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None
    result = common.run(["/bin/ps", "-o", "command=", "-p", str(pid)], timeout=10)
    script = os.path.realpath(os.path.join(run_dir, "inner.zsh"))
    words = result.stdout.split() if result.returncode == 0 else []
    return pid if any(w.endswith("inner.zsh") and os.path.realpath(w) == script for w in words) else None


def cancel_run(run_dir, kind, grace):
    """SIGTERM this run's inner.zsh (its traps stop the worker group or review-gate and record the rc).

    A run whose inner.zsh has not taken the lock yet is fenced off by taking the lock here, so a late
    launcher exits without starting anything. Returns the lines to print and whether an rc exists."""
    rc_path = f"{run_dir}/{kind}.rc"
    if os.path.exists(rc_path):
        return [f"cancel: {kind} already finished"], True
    lines = []
    try:
        os.mkdir(run_dir + "/lock")
    except FileExistsError:
        pass
    else:
        tmp = rc_path + ".tmp"
        with open(f"{run_dir}/{kind}.err", "a", encoding="utf-8") as handle:
            handle.write("hermes-delegate cancel: cancelled before inner.zsh started; nothing ran\n")
        Path(tmp).write_text("143\n", encoding="utf-8")
        os.replace(tmp, rc_path)
        lines.append("cancel: inner.zsh had not started; took the lock so it never runs, rc=143")
        if kind == "review":
            lines.append("cancel: the ticket is still open and unused; close it as UNREVIEWED")
        return lines, True
    deadline = time.monotonic() + grace
    pid = inner_pid(run_dir)
    while pid is None and not os.path.exists(rc_path) and time.monotonic() < deadline:
        time.sleep(0.1)
        pid = inner_pid(run_dir)
    if pid is None:
        if os.path.exists(rc_path):
            return [f"cancel: {kind} finished meanwhile"], True
        return ["cancel: lock held but no live inner.zsh for this run (inner.pid missing or stale)"], False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        lines.append(f"cancel: sent SIGTERM to inner.zsh pid {pid}")
    found = wait_until(lambda: os.path.exists(rc_path), run_dir, deadline)
    if not found:
        lines.append(f"cancel: no {kind}.rc within {grace}s; inner.zsh pid {pid} may still be stopping")
    return lines, found


WATCHER_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP", "SIGINT")
                        if hasattr(signal, name))


def raise_watcher_stopped(signum, frame):
    for sig in WATCHER_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)
    raise WatcherStopped(signum)


def watch_signals():
    try:
        return {sig: signal.signal(sig, raise_watcher_stopped) for sig in WATCHER_SIGNALS}
    except ValueError:
        return {}


def restore_signals(previous):
    for sig, handler in previous.items():
        signal.signal(sig, handler)


def cmd_cancel(argv, out=None, err=None):
    out = out or sys.stdout
    p = common.Parser(prog="hermes-delegate cancel", allow_abbrev=False)
    p.add_argument("run_dir")
    p.add_argument("--grace", type=int, default=30, help="seconds to wait for the rc after SIGTERM")
    args = p.parse_args(argv)
    run_dir = common.canonical(args.run_dir, "run dir", kind="dir")
    kind = common.run_kind(run_dir)
    lines, found = cancel_run(run_dir, kind, max(args.grace, 1))
    for line in lines:
        print(line, file=out)
    if not found:
        print("status=no_rc", file=out)
        return common.EXIT_NO_RC
    text = Path(run_dir, kind + ".rc").read_text(encoding="utf-8-sig").strip()
    print("status=finished", file=out)
    print(f"{kind} rc={text}", file=out)
    return min(int(text), 255) if re.fullmatch(r"[0-9]{1,3}", text) else 1


STATUS_KINDS = ("review", "claude", "qa", "codex")


EXIT_STATUS_UNKNOWN = 3


STATUS_START_GRACE_SECONDS = 30


CODEX_EMPTY_STALE_SECONDS = 300


HINT_RUNNING = "completion arrives via the watcher/gateway notification; do not poll"


HINT_FINISHED = "run the watcher line or read the result"


HINT_CODEX_FINISHED = "the gateway reports completion; read status.json in task_dir"


HINT_CODEX_NOT_STARTED = "the gateway has not started the printed argv yet (normal for a few seconds)"


def wait_for_rc(args, run_dir, kind, via, launcher, out):
    rc_path, started_path = f"{run_dir}/{kind}.rc", run_dir + "/started_at"
    begin = time.monotonic()
    deadline = begin + args.timeout
    grace = begin + min(args.start_grace, args.timeout)
    if not wait_until(lambda: os.path.exists(rc_path) or os.path.exists(started_path), run_dir, grace):
        print("status=never_started", file=out)
        if kind == "review":
            raise common.Fail(f"review never started: no started_at after {min(args.start_grace, args.timeout)}s in {run_dir}; {launcher} (ticket still open; "
                       f"hermes-delegate review --launch-run {run_dir} can retry)", common.EXIT_NEVER_STARTED)
        raise common.Fail(f"{kind} run never started: no started_at after {min(args.start_grace, args.timeout)}s in {run_dir}; {launcher} (hermes-delegate {kind} --launch-run {run_dir} "
                   "can retry)",
                   common.EXIT_NEVER_STARTED)
    if os.path.exists(started_path) and not os.path.exists(rc_path):
        print("status=started started_at={}".format(Path(started_path).read_text(encoding="utf-8-sig").strip()), file=out, flush=True)
    stalled = wait_for_rc_or_stall(rc_path, run_dir, f"{run_dir}/{kind}.out", deadline,
                                   stall_seconds(kind, args.stall_minutes))
    if stalled:
        return stalled
    if not os.path.exists(rc_path):
        print("status=no_rc", file=out)
        print(runtime.watcher_line(run_dir, args.timeout, args.stall_minutes), file=out)
        raise common.Fail("{} started at {} but wrote no {}.rc after {}s in {}; it may still be running, or the {} was "
                   "killed without a trap or the {} is hung (re-watch with hermes-delegate wait {}, stop it with "
                   "hermes-delegate cancel {})".format(
                       kind, Path(started_path).read_text(encoding="utf-8-sig").strip() if os.path.exists(started_path) else "?",
                       kind, args.timeout, run_dir, "workspace" if via == "cmux" else "detached inner.zsh",
                       "reviewer" if kind == "review" else "worker", run_dir, run_dir), common.EXIT_NO_RC)


def cmd_wait(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    p = common.Parser(prog="hermes-delegate wait", allow_abbrev=False)
    p.add_argument("run_dir")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--start-grace", type=int, default=180,
                   help="fail early when inner.zsh has not written started_at by then")
    p.add_argument("--stall-minutes", type=float, default=None,
                   help="exit 92 (status=stalled) when <kind>.out has not grown for this long; 0 disables "
                        "(default: claude 20, review 20, qa 30)")
    args = p.parse_args(argv)
    run_dir = common.canonical(args.run_dir, "run dir", kind="dir")
    kind = common.run_kind(run_dir)
    stall_seconds(kind, args.stall_minutes)
    if args.stall_minutes is None and stall_count(run_dir) >= common.STALL_REARM_LIMIT:
        args.stall_minutes = 0.0
        print(f"stall_check=off ({stall_count(run_dir)} stalls already reported for this run; pass --stall-minutes M to check "
              "again)", file=out)
    meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig")) if os.path.exists(run_dir + "/meta.json") else {}
    via = meta.get("via", "cmux")
    launcher = ("the cmux workspace did not run inner.zsh" if via == "cmux"
                else "the direct launcher did not run inner.zsh (see launcher.log)")
    # A watcher signal is not a cancel intent: gateway shutdown/restart and status re-waits are signalled too.
    previous = watch_signals()
    begin = time.monotonic()
    try:
        stalled = wait_for_rc(args, run_dir, kind, via, launcher, out)
    except WatcherStopped as stopped:
        print("status=no_rc", file=out)
        print(runtime.watcher_line(run_dir, runtime.rewatch_timeout(args.timeout, begin), args.stall_minutes), file=out)
        raise common.Fail(f"wait stopped by {signal.Signals(stopped.signum).name}; the run keeps going (no {kind}.rc yet; re-watch with hermes-delegate wait {run_dir}, "
                   f"stop it only with hermes-delegate cancel {run_dir})", common.EXIT_NO_RC)
    finally:
        restore_signals(previous)
    if stalled:
        report_stall(out, kind, f"{run_dir}/{kind}.out", *stalled, run_dir=run_dir,
                     timeout=runtime.rewatch_timeout(args.timeout, begin), stall_minutes=args.stall_minutes)
        return common.EXIT_STALLED
    rc_path = f"{run_dir}/{kind}.rc"
    text = Path(rc_path).read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"[0-9]{1,3}", text):
        raise common.Fail(f"malformed rc file: {text[:40]!r}")
    code = int(text)
    leaked = redact_tokens(run_dir)
    output = f"{run_dir}/{kind}.out"
    size = os.path.getsize(output) if os.path.exists(output) else 0
    print("status=finished", file=out)
    print(f"{kind} rc={code} out={output} bytes={size}", file=out)
    if leaked:
        print("WARNING: an OAuth token ({}...) appeared in {} and was masked in place; rotate the token".format(
            common.TOKEN_PREFIX, ",".join(leaked)), file=out)
    if kind == "qa" or meta.get("reviewer") == "codex":
        summary = summarize_codex(output, run_dir, kind)
    else:
        summary = summarize_stream(output, run_dir, kind)
    if kind != "review":
        summary += worktree_summary(run_dir, meta)
    for line in summary:
        print(line, file=out)
    if code:
        errors = f"{run_dir}/{kind}.err"
        if os.path.exists(errors):
            tail = Path(errors).read_text(errors="replace", encoding="utf-8-sig").splitlines()[-20:]
            for line in tail:
                print(line, file=out)
    else:
        close_workspace(meta, out)
    return min(code, 255)
