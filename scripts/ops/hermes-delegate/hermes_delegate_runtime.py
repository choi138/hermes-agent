"""Run scripts, launchers and review ledger preflight."""

import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import hermes_delegate_common as common


def script_header(run_dir, kind, hangup, group):
    """Lock, rc helpers and signal traps shared by every inner.zsh.

    The target runs with & and wait so a trap fires at once (zsh defers traps until a foreground
    child exits). A worker runs under gtimeout, which leads its own process group, so the TERM goes
    to that group; review-gate is not a group leader and stops its reviewer group itself."""
    q = shlex.quote
    rc, tmp_rc, errors = run_dir + f"/{kind}.rc", run_dir + f"/{kind}.rc.tmp", \
        run_dir + f"/{kind}.err"
    # Until gtimeout has made its group, only the pid exists; fall back to it.
    target = "-- -$child 2>/dev/null || kill -TERM $child" if group else "$child"
    return [
        "#!/bin/zsh",
        "mkdir {} 2>/dev/null || {{ print -r -- 'hermes-delegate inner: run dir already launched (lock exists); "
        "not started again' >> {}; exit {}; }}".format(q(run_dir + "/lock"), q(errors), common.EXIT_LOCKED),
        "set -u",
        "child=",
        "print -r -- $$ > {}".format(q(run_dir + "/inner.pid")),
        "finish() {{ [[ -e {rc} ]] || {{ print -r -- $1 > {tmp} && mv -f {tmp} {rc}; }}; }}".format(
            rc=q(rc), tmp=q(tmp_rc)),
        f"fail() {{ print -r -- \"hermes-delegate inner: $2\" >> {q(errors)}; finish $1; exit $1; }}",
        "interrupted() {",
        "  if [[ -n $child ]]; then",
        f"    kill -TERM {target} 2>/dev/null",
        "    wait $child",
        "    local code=$?",
        "    child=",
        "    (( code == 127 )) && code=$1",
        "    fail $code \"$2; sent SIGTERM to the {}, which exited $code\"".format(
            "worker group" if group else "review-gate run"),
        "  fi",
        "  fail $1 \"$2\"",
        "}",
        f"trap 'interrupted 129 \"interrupted by SIGHUP (workspace closed?); {hangup}\"' HUP",
        "trap 'interrupted 143 \"interrupted by SIGTERM\"' TERM",
        "trap 'interrupted 130 \"interrupted by SIGINT\"' INT",
    ]


def clean_checks(run_dir, workdir, git, head, what):
    q = shlex.quote
    return [
        "date '+%Y-%m-%dT%H:%M:%S%z' > {}".format(q(run_dir + "/started_at")),
        f"cd {q(workdir)} || fail 2 'cannot cd to the workdir'",
        f"[[ \"$({q(git)} rev-parse HEAD)\" == {q(head)} ]] || fail 2 'workdir HEAD moved away from the {what[0]} "
        f"before launch; nothing {what[1]}'",
        f"[[ -z \"$({q(git)} status --porcelain --untracked-files=normal)\" ]] || fail 2 'workdir became dirty "
        f"before launch; nothing {what[1]}'",
    ]


def token_checks(token_file):
    q = shlex.quote
    return [
        "[[ -f {0} && ! -L {0} ]] || fail 2 'claude OAuth token file is missing: '{0}".format(q(token_file)),
        f"[[ \"$(/usr/bin/stat -f '%Lp:%u' {q(token_file)})\" == \"600:$UID\" ]] || fail 2 'claude OAuth token file must be "
        f"mode 0600 and owned by you: '{q(token_file)}",
        # 2026-08-25: an exported ANTHROPIC_API_KEY/BASE_URL silently replaced the OAuth login.
        "unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL",
    ]


def codex_env():
    return ["export ASDF_NODEJS_VERSION=22.19.0",
            f"export PATH={shlex.quote(str(common.CODEX_NODE_BIN))}:$PATH"]


def background_target(command, *, stdin, stdout, stderr, token_file=None):
    """Start the target in the background; the token is read here, at run time, and exported only
    inside the subshell that execs the target, so it never lands in argv or a file."""
    q = shlex.quote
    redirects = f"< {q(stdin)} > {q(stdout)} 2>> {q(stderr)} &"
    if token_file:
        command = f"( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {q(token_file)})\"; exec {command} ) {redirects}"
    else:
        command = f"{command} {redirects}"
    return [command, "child=$!", "wait $child", "code=$?", "child="]


def inner_script(*, run_dir, workdir, ticket, review_gate, timeout, reviewer_argv, gate_db, commit, git,
                 reviewer="claude", token_file=None):
    q = shlex.quote
    errors = run_dir + "/review.err"
    lines = script_header(run_dir, "review", "the ticket may stay running and the reviewer process "
                                             "may still be alive", group=False)
    if gate_db:
        lines.append("export REVIEW_GATE_DB=" + q(gate_db))
    if reviewer == "codex":
        lines += codex_env()
    if token_file:
        lines += token_checks(token_file)
    lines += clean_checks(run_dir, workdir, git, commit, ("reviewed commit", "was reviewed"))
    lines += background_target(
        f"{q(review_gate)} run --ticket {q(ticket)} --timeout {timeout} -- {shlex.join(reviewer_argv)}",
        stdin=run_dir + "/prompt.md", stdout=run_dir + "/review.out", stderr=errors, token_file=token_file)
    lines += [
        "date '+%Y-%m-%dT%H:%M:%S%z' > {}".format(q(run_dir + "/ended_at")),
        "finish $code",
        "exit $code",
    ]
    return "\n".join(lines) + "\n"


def state_root(kind):
    root = Path(os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(common.DEFAULT_STATE_ROOT))) / kind
    os.makedirs(str(root), mode=0o700, exist_ok=True)
    return common.canonical(str(root), kind + " state root", kind="dir")


def review_state_root():
    return state_root("review")


def tools():
    return (os.environ.get("HERMES_DELEGATE_REVIEW_GATE", str(common.DEFAULT_REVIEW_GATE)),
            os.environ.get("HERMES_DELEGATE_CMUX", str(common.DEFAULT_CMUX)))


def worker_binary(kind):
    if kind in ("qa", "codex"):
        return os.environ.get("HERMES_DELEGATE_CODEX_BIN", str(common.CODEX_BIN))
    return os.environ.get("HERMES_DELEGATE_CLAUDE_BIN", str(common.CLAUDE_BIN))


def token_file():
    return os.environ.get("HERMES_DELEGATE_CLAUDE_TOKEN_FILE", str(common.CLAUDE_TOKEN_FILE))


def check_token_file(path):
    """stat only: the token value is never read by the entry."""
    try:
        info = os.lstat(path)
    except OSError:
        raise common.Fail(f"claude OAuth token file is missing: {path} (needed for --via direct)")
    if not os.path.isfile(path) or os.path.islink(path):
        raise common.Fail("claude OAuth token file is not a regular file: " + path)
    if info.st_uid != common.numeric_uid() or info.st_mode & 0o777 != 0o600:
        raise common.Fail(f"claude OAuth token file must be mode 0600 and owned by you: {path} (mode {info.st_mode & 0o777:o})")
    if info.st_size == 0:
        raise common.Fail("claude OAuth token file is empty: " + path)


def check_launcher(cmux, binary=None):
    binary = binary or str(common.CLAUDE_BIN)
    if not os.access(cmux, os.X_OK):
        raise common.Fail("cmux is not executable: " + cmux)
    if not os.access(binary, os.X_OK):
        raise common.Fail(f"{os.path.basename(binary)} CLI is missing: {binary}")
    ping = common.run([cmux, "ping"], timeout=15)
    if ping.returncode:
        raise common.Fail(f"cmux is not reachable (cmux ping exit {ping.returncode}): {common.last_line(ping.stderr or ping.stdout)}")


def check_binary(binary):
    if not os.access(binary, os.X_OK):
        raise common.Fail(f"{os.path.basename(binary)} CLI is missing: {binary}")


def zsh_command(script_path):
    # zsh -i loads .zshrc, where the codex-lb provider key lives; -l alone would skip it.
    return "/bin/zsh -ic {}".format(shlex.quote("exec /bin/zsh " + shlex.quote(script_path)))


def launch_direct(run_dir, script_path, workdir):
    """Detach inner.zsh into its own session and return at once; the watcher line follows the rc."""
    log = run_dir + "/launcher.log"
    command = f"{zsh_command(script_path)} < /dev/null >> {shlex.quote(log)} 2>&1 &!"
    try:
        result = subprocess.run(["/bin/zsh", "-fc", command], cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8-sig",
                                start_new_session=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise common.Fail(f"direct launch failed: {exc}")
    if result.returncode:
        raise common.Fail(f"direct launch failed (exit {result.returncode}): {common.last_line(result.stderr)}")


def update_meta(run_dir, **values):
    path = Path(run_dir, "meta.json")
    meta = json.loads(path.read_text(encoding="utf-8-sig"))
    meta.update(values)
    tmp = path.with_name("meta.json.tmp")
    tmp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def ledger_status(review_gate, item):
    result = common.run([review_gate, "status", "--item", item, "--json"], timeout=60)
    if result.returncode:
        raise common.Fail((result.stderr.strip() or f"review-gate status exit {result.returncode}")
                   .replace("\n", " "), result.returncode)
    return json.loads(result.stdout)


PENDING_REFUSAL = ("Previous tickets must be fully closed; reproduce each pending verdict yourself, record it "
                   "with review-gate close, then retry (tell the user only if a verdict cannot be reproduced).")


OWNER_APPROVAL_ID = re.compile(r"[0-9]{15,22}")


FALLBACK_LATEST_REFUSAL = ("--fallback needs the latest closed ticket of the current round to be UNREVIEWED; "
                           "record the failed review with review-gate close --verdict UNREVIEWED first.")


def start_refusal(state, owner_approval=None, fallback=False, reviewer=None):
    """Mirror review_gate.start() for a plain or --fallback (never --join) start, read-only.

    The ledger status lists only this item's tickets, so an approval ID reused on another item is
    caught by review-gate start itself, not here. Commit matching is left to review-gate start."""
    tickets = state["tickets"]
    current_round = max((t.get("round") or 0 for t in tickets), default=0)
    current = [t for t in tickets if (t.get("round") or 0) == current_round]
    closed = [(t.get("closed_at") or "", index, t) for index, t in enumerate(current) if t["status"] == "closed"]
    latest = max(closed, key=lambda row: row[:2])[2] if closed else None
    if fallback:
        if any(t.get("fallback") for t in current):
            return (f"Fallback already used in round {current_round}; the owner must resolve this round or approve another "
                    "with --owner-approval.")
        if latest is None or latest["verdict"] != "UNREVIEWED":
            return FALLBACK_LATEST_REFUSAL
        if reviewer and latest.get("reviewer") == reviewer:
            return f"--fallback needs a different reviewer than the failed ticket's ({reviewer})."
        return None
    if any(t["status"] != "closed" or t["verdict"] is None for t in tickets):
        return PENDING_REFUSAL
    if (latest is not None and latest["verdict"] == "UNREVIEWED" and not owner_approval
            and not any(t["verdict"] == "BLOCK" or t.get("fallback") for t in current)):
        return (f"Round {current_round} ended UNREVIEWED; retry it in the same round with hermes-delegate review --fallback "
                "--reviewer <the other CLI> --commit <the failed ticket's commit>. A new round instead needs "
                "--owner-approval.")
    next_round = state["rounds_used"] + 1
    if (next_round > 2 or state["item"]["status"] in ("cap-reached", "stopped-unreviewed")) and not owner_approval:
        return ("Item {} stopped at round {}; the owner must supply --owner-approval <Discord message ID> "
                "for a further round (ask the user in Discord, then pass the ID of their approval message "
                "with hermes-delegate review --owner-approval ID).".format(state["item"]["id"], next_round - 1))
    if owner_approval and any(t.get("owner_approval") == owner_approval for t in tickets):
        return (f"Owner approval {owner_approval} was already used; each further round needs a new owner approval "
                "message ID.")
    return None


def watcher_line(run_dir, timeout, stall_minutes=None, label="watcher"):
    command = f"{common.HD_BIN} wait {run_dir} --timeout {timeout}"
    if stall_minutes is not None:
        command += f" --stall-minutes {stall_minutes:g}"
    return (f"{label}: terminal(command=\"{command}\", background=true, notify_on_complete=true, "
            "continue_on_complete=true, persist_on_release=true)")


def rewatch_timeout(timeout, begin):
    # Floor so a re-arm near the old deadline still covers the worker's own gtimeout teardown.
    return max(math.ceil(timeout - (time.monotonic() - begin)), common.REWATCH_MIN_SECONDS)


def print_launch(out, run_dir, ticket, launched, timeout, kind="review", via="cmux"):
    print("run_dir=" + run_dir, file=out)
    print(f"rc={run_dir}/{kind}.rc", file=out)
    if ticket:
        print("ticket=" + ticket, file=out)
    print("via=" + via, file=out)
    print("launched=" + ("yes" if launched else "no"), file=out)
    print(watcher_line(run_dir, timeout + 900), file=out)


WORKSPACE_REF = re.compile(r"\bworkspace:[0-9A-Za-z-]+")


def launch_workspace(launch, ticket=None, kind="review", run_dir="RUN_DIR"):
    result = common.run(launch, timeout=30, env=dict(os.environ, CMUX_QUIET="1"))
    if result.returncode:
        detail = f"cmux new-workspace failed (exit {result.returncode}): {common.last_line(result.stderr or result.stdout)}"
        if ticket:
            raise common.Fail(f"{detail}; ticket {ticket} is still open and unused: retry with hermes-delegate review --launch-run "
                       "RUN_DIR, or report it and close it as UNREVIEWED")
        raise common.Fail(f"{detail}; nothing ran: retry with hermes-delegate {kind} --launch-run {run_dir}")
    found = WORKSPACE_REF.findall(result.stdout)
    return found[-1] if found else None


def start_run(*, via, launch, run_dir, script_path, workdir, ticket=None, kind="review"):
    if via == "direct":
        try:
            launch_direct(run_dir, script_path, workdir)
        except common.Fail as exc:
            if ticket:
                raise common.Fail(f"{exc}; ticket {ticket} is still open and unused: retry with hermes-delegate review "
                           f"--launch-run {run_dir}")
            raise common.Fail(f"{exc}; nothing ran: retry with hermes-delegate {kind} --launch-run {run_dir}")
        return
    workspace = launch_workspace(launch, ticket, kind, run_dir)
    if workspace:
        update_meta(run_dir, cmux_workspace=workspace)
