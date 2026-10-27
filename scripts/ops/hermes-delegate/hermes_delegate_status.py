"""Instant run and gateway-owned runner status."""

import json
import os
from pathlib import Path
import re
import sys
import time
import hermes_delegate_common as common
import hermes_delegate_watch as watch


def status_target(value):
    if value and len(value) > 1:
        value = value.rstrip("/") or "/"
    try:
        path = common.canonical(value, "status path", kind="dir")
    except common.Fail as exc:
        raise common.Fail(str(exc), 2)
    root = os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(common.DEFAULT_STATE_ROOT)).rstrip("/")
    parent = os.path.dirname(path)
    kind = os.path.basename(parent)
    if os.path.dirname(parent) != root or kind not in watch.STATUS_KINDS:
        raise common.Fail("status path must be {}/{{{}}}/<name>: {}".format(root, ",".join(watch.STATUS_KINDS), path), 2)
    return path, kind


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(seconds))


def age_seconds(path, now):
    try:
        return max(int(now - os.stat(path).st_mtime), 0)
    except OSError:
        return None


def read_small(path, limit=4096):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace").strip()
    except OSError:
        return None


def count_lines(path):
    count = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                count += chunk.count(b"\n")
    except OSError:
        return None
    return count


def runner_pid(output_dir):
    """A live runner whose argv carries --output-dir OUTPUT_DIR (status.json is written only at exit)."""
    result = common.run(["/bin/ps", "-axww", "-o", "pid=,command="], timeout=10)
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        words = line.split()
        if len(words) < 3 or not any(os.path.basename(w) == "run_codex_task.py" for w in words[1:]):
            continue
        pairs = ((words[index], words[index + 1]) for index in range(1, len(words) - 1))
        if any((a == "--output-dir" and b == output_dir) for a, b in pairs) or \
                "--output-dir=" + output_dir in words:
            if words[0].isdigit() and int(words[0]) != os.getpid():
                return int(words[0])
    return None


def run_dir_status(path, kind, now):
    fields = [("kind", kind)]
    try:
        meta_kind = common.run_kind(path) if os.path.exists(path + "/meta.json") else kind
    except common.Fail:
        meta_kind = "invalid"
    rc_path = f"{path}/{kind}.rc"
    started = read_small(path + "/started_at")
    ended = read_small(path + "/ended_at")
    extra = []
    if started:
        extra.append(("started_at", started))
    if ended:
        extra.append(("ended_at", ended))
    output_age = age_seconds(f"{path}/{kind}.out", now)
    if output_age is not None:
        extra.append(("last_output_age_s", output_age))
    if os.path.exists(path + "/stall_count"):
        extra.append(("stall_count", watch.stall_count(path)))
    if meta_kind != kind:
        return fields + [("status", "unknown")] + extra, \
            f"meta.json kind {meta_kind} does not match the {kind} state dir", watch.EXIT_STATUS_UNKNOWN
    if os.path.exists(rc_path):
        text = read_small(rc_path) or ""
        if not re.fullmatch(r"[0-9]{1,3}", text):
            return fields + [("status", "unknown")] + extra, \
                f"malformed {kind}.rc: {text[:40]}", watch.EXIT_STATUS_UNKNOWN
        return fields + [("status", "finished"), ("rc", int(text))] + extra, watch.HINT_FINISHED, 0
    lock = path + "/lock"
    if not os.path.exists(lock) and not started:
        return fields + [("status", "not_started")] + extra, (
            "inner.zsh has not started yet (normal for a few seconds after launched=yes; after launched=no "
            f"run hermes-delegate {kind} --launch-run RUN_DIR)"), 0
    pid = watch.inner_pid(path)
    if pid:
        return fields + [("status", "running"), ("pid", pid)] + extra, watch.HINT_RUNNING, 0
    lock_age = age_seconds(lock, now)
    if not os.path.exists(path + "/inner.pid") and lock_age is not None and lock_age < watch.STATUS_START_GRACE_SECONDS:
        return fields + [("status", "running")] + extra, watch.HINT_RUNNING, 0
    return fields + [("status", "unknown")] + extra, (
        f"no live inner.zsh and no {kind}.rc (killed without its trap?); read {kind}.err and report it"), \
        watch.EXIT_STATUS_UNKNOWN


def codex_status(path, now):
    fields = [("kind", "codex")]
    tasks = sorted((name for name in os.listdir(path) if name.startswith("codex-task-")
                    and os.path.isdir(os.path.join(path, name)) and not os.path.islink(os.path.join(path, name))),
                   key=lambda name: os.stat(os.path.join(path, name)).st_mtime)
    if not tasks:
        age = age_seconds(path, now)
        hint = watch.HINT_CODEX_NOT_STARTED
        if age is not None and age > watch.CODEX_EMPTY_STALE_SECONDS:
            hint += f"; empty for {age}s, so the launch probably never happened"
        return fields + [("status", "not_started"), ("dir_age_s", age)], hint, 0
    task = os.path.join(path, tasks[-1])
    info = os.stat(task)
    extra = [("task_dir", task)]
    if len(tasks) > 1:
        extra.append(("task_dirs", len(tasks)))
    extra.append(("started_at", stamp(getattr(info, "st_birthtime", info.st_ctime))))
    events = task + "/events.jsonl"
    output_age = age_seconds(events, now)
    if output_age is not None:
        extra.append(("last_output_age_s", output_age))
    count = count_lines(events)
    if count is not None:
        extra.append(("events", count))
    status_path = task + "/status.json"
    if os.path.exists(status_path):
        try:
            receipt = json.loads(Path(status_path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            receipt = None
        if not isinstance(receipt, dict) or type(receipt.get("exit_code")) is not int:
            return fields + [("status", "unknown")] + extra, "status.json is unreadable or malformed", \
                watch.EXIT_STATUS_UNKNOWN
        at = [key for key, _ in extra].index("started_at") + 1
        extra.insert(at, ("ended_at", stamp(os.stat(status_path).st_mtime)))
        extra.insert(0, ("rc", receipt["exit_code"]))
        extra.append(("runner_status", receipt.get("status")))
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        if execution.get("status") not in (None, receipt.get("status")):
            extra.append(("runner_phase", execution.get("status")))
        return fields + [("status", "finished")] + extra, watch.HINT_CODEX_FINISHED, 0
    pid = runner_pid(path)
    if pid:
        return fields + [("status", "running"), ("pid", pid)] + extra + [("runner_status", "running")], \
            watch.HINT_RUNNING, 0
    return fields + [("status", "unknown")] + extra, (
        "no live runner and no status.json (runner killed?); read stderr.log in task_dir and report it"), \
        watch.EXIT_STATUS_UNKNOWN


def cmd_status(argv, out=None, err=None):
    out = out or sys.stdout
    p = common.Parser(prog="hermes-delegate status", allow_abbrev=False,
               description="Instant, non-blocking status of a run dir or a codex output dir.")
    p.add_argument("path")
    args = p.parse_args(argv)
    path, kind = status_target(args.path)
    now = time.time()
    if kind == "codex":
        fields, hint, code = codex_status(path, now)
    else:
        fields, hint, code = run_dir_status(path, kind, now)
    for key, value in fields:
        print(f"{key}={watch.clip(value, 400)}", file=out)
    print("hint=" + watch.clip(hint, 400), file=out)
    return code
