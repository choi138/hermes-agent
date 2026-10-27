"""Isolated CLI smoke checks and process cleanup."""

import io
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import hermes_delegate_common as common
import hermes_delegate_review as review
import hermes_delegate_runtime as runtime
import hermes_delegate_watch as watch


SMOKE_PROMPT = "Reply with exactly the single word ok and nothing else."


def smoke_script(target, smoke_dir, via):
    q = shlex.quote
    lines = ["#!/bin/zsh", "set -u", f"cd {q(smoke_dir)} || exit 2"]
    out, err = q(smoke_dir + "/smoke.out"), q(smoke_dir + "/smoke.err")
    if target == "codex":
        lines += runtime.codex_env()
        command = shlex.join([runtime.worker_binary("codex"), "exec", "--ephemeral", "-s", "read-only",
                              "-m", common.REVIEW_POLICY["codex"]["model"], "-c", 'model_reasoning_effort="low"',
                              *review.codex_mcp_off(), "--skip-git-repo-check", "-C", smoke_dir, "-o", smoke_dir + "/smoke.final.md",
                              SMOKE_PROMPT])
    else:
        command = shlex.join([runtime.worker_binary("claude"), "-p", "--model", "haiku", "--tools", "Read",
                              "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config",
                              '{"mcpServers":{}}', "--no-session-persistence", "--output-format", "text",
                              SMOKE_PROMPT])
        if via == "direct":
            lines += ["fail() { print -r -- \"$2\" >> " + err + "; print -r -- $1 > "
                      + q(smoke_dir + "/smoke.rc") + "; exit $1; }"]
            lines += runtime.token_checks(runtime.token_file())
            command = f"( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {q(runtime.token_file())})\"; exec {command} )"
    lines += [f"{command} < /dev/null > {out} 2>> {err}", "code=$?",
              "print -r -- $code > {}.tmp && mv -f {}.tmp {}".format(*[q(smoke_dir + "/smoke.rc")] * 3),
              "exit $code"]
    return "\n".join(lines) + "\n"


def stop_smoke(child):
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise common.Fail("cannot stop smoke process group on this host")
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        try:
            killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue
    raise common.Fail("cannot reap smoke process after TERM and KILL; outputs kept")


def run_smoke_direct(script, smoke_dir, timeout):
    child = subprocess.Popen(["/bin/zsh", "-ic", "exec /bin/zsh " + shlex.quote(script)], cwd=smoke_dir,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        child.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        stop_smoke(child)
        return False


def cmd_smoke(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    p = common.Parser(prog="hermes-delegate smoke", allow_abbrev=False,
               description="Tiny auth/exec check of a CLI; not a review, issues no ticket.")
    p.add_argument("target", choices=("codex", "claude"))
    p.add_argument("--via", choices=common.VIA, help="default: codex direct, claude cmux")
    p.add_argument("--timeout", type=int, default=180)
    args = p.parse_args(argv)
    via = args.via or common.REVIEW_POLICY[args.target]["via"]
    if not 10 <= args.timeout <= 900:
        raise common.Fail("--timeout must be between 10 and 900 seconds", 2)
    binary = runtime.worker_binary(args.target)
    cmux = runtime.tools()[1]
    review.check_launch_tools(via, cmux, binary)
    if args.target == "claude" and via == "direct":
        runtime.check_token_file(runtime.token_file())
    smoke_dir = "{}/{}-{}-{}".format(runtime.state_root("smoke"), args.target, time.strftime("%Y%m%dT%H%M%S"),
                                     secrets.token_hex(3))
    os.mkdir(smoke_dir, 0o700)
    script = smoke_dir + "/smoke.zsh"
    Path(script).write_text(smoke_script(args.target, smoke_dir, via), encoding="utf-8")
    os.chmod(script, 0o700)
    rc_path = smoke_dir + "/smoke.rc"
    workspace, passed, completed = None, False, True
    try:
        if via == "direct":
            completed = run_smoke_direct(script, smoke_dir, args.timeout)
        else:
            workspace = runtime.launch_workspace([cmux, "new-workspace", "--name", "smoke-" + args.target, "--cwd",
                                          smoke_dir, "--focus", "false", "--command", runtime.zsh_command(script)],
                                         kind="smoke", run_dir=smoke_dir)
            watch.wait_until(lambda: os.path.exists(rc_path), smoke_dir, time.monotonic() + args.timeout)
        leaked = watch.redact_tokens(smoke_dir)
        if leaked:
            print(f"WARNING: an OAuth token ({common.TOKEN_PREFIX}...) appeared in smoke output and was masked",
                  file=err)
        if not completed or not os.path.exists(rc_path):
            reason = f"no result within {args.timeout}s"
        else:
            code = Path(rc_path).read_text(encoding="utf-8-sig").strip()
            final = smoke_dir + ("/smoke.final.md" if args.target == "codex" else "/smoke.out")
            answer = Path(final).read_text(errors="replace", encoding="utf-8-sig").strip() if os.path.exists(final) else ""
            if code == "0" and answer.strip(" .\n`'\"").lower() == "ok":
                print(f"ok {args.target} via={via}", file=out)
                passed = True
                return 0
            errors = Path(smoke_dir, "smoke.err").read_text(errors="replace", encoding="utf-8-sig") if os.path.exists(
                smoke_dir + "/smoke.err") else ""
            reason = f"rc={code} answer={answer[:80]!r} stderr_last={common.last_line(errors)[:300]}"
        print(f"fail {args.target} via={via}: {reason} (kept {smoke_dir})", file=out)
        return 1
    finally:
        if workspace:
            watch.close_workspace({"via": "cmux", "cmux_workspace": workspace}, io.StringIO())
        if passed:
            shutil.rmtree(smoke_dir, ignore_errors=True)
