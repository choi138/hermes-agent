"""Claude implementation and device QA run preparation."""

import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sys
import time
import hermes_delegate_codex as codex
import hermes_delegate_common as common
import hermes_delegate_review as review
import hermes_delegate_runtime as runtime
import hermes_delegate_spec as spec_gate


def worker_parser(kind):
    if kind == "claude":
        description = ("Launch a write-capable Claude Code implementation run (cmux workspace by default, "
                       "--via direct for a detached run with the OAuth token file).")
    else:
        description = ("Launch a device/simulator QA run (Codex {model}/{effort}, sandbox {sandbox}; "
                       "fixed) in a new cmux workspace.".format(**common.QA_POLICY))
    p = common.Parser(prog="hermes-delegate " + kind, allow_abbrev=False, description=description)
    p.add_argument("--spec", required=True)
    p.add_argument("--workdir", required=True)
    if kind == "claude":
        p.add_argument("--class", dest="task_class", required=True, choices=sorted(common.CLAUDE_IMPL_POLICY))
        p.add_argument("--advisor", choices=common.CLAUDE_ADVISORS,
                       help="opt-in per run: consult Opus as advisor (extra Opus tokens, unverified benefit)")
        p.add_argument("--via", choices=common.VIA, default="cmux", help="cmux (default) or direct")
    p.add_argument("--timeout", type=int, required=True, help="worker timeout seconds (60-86400)")
    p.add_argument("--label")
    p.add_argument("--no-launch", action="store_true",
                   help="prepare the run dir and skip the launch; launch later with --launch-run RUN_DIR")
    return p


def reject_worker_overrides(kind, argv):
    fixed = common.WORKER_OVERRIDES + (("--class", "--task-class", "--advisor", "--via") if kind == "qa" else ())
    for word in argv:
        name = word.split("=", 1)[0]
        if name in fixed:
            if kind == "qa":
                raise common.Fail("{} is not accepted: device QA is fixed at Codex {model}/{effort} with sandbox "
                           "{sandbox} in cmux (user pin)".format(name, **common.QA_POLICY), 2)
            raise common.Fail("{} is not accepted: model/effort come only from --class ({}) and the permission "
                       "flags are fixed".format(name, ", ".join(sorted(common.CLAUDE_IMPL_POLICY))), 2)


def worker_argv(kind, args, run_dir, workdir, binary):
    if kind == "claude":
        policy = common.CLAUDE_IMPL_POLICY[args.task_class]
        argv = [binary, "-p", "--model", policy["model"], "--effort", policy["effort"], *common.CLAUDE_WRITE]
        if args.advisor:
            argv += ["--advisor", args.advisor, "--settings", common.ADVISOR_SETTINGS]
        return argv, policy
    return [binary, "exec", "--ephemeral", "-m", common.QA_POLICY["model"],
            "-c", 'model_reasoning_effort="{}"'.format(common.QA_POLICY["effort"]),
            "--sandbox", common.QA_POLICY["sandbox"], "-C", workdir, "--json",
            "-o", run_dir + "/qa.final.md", "-"], common.QA_POLICY


def worker_script(*, kind, run_dir, workdir, head, git, argv, timeout, token_file=None):
    q = shlex.quote
    prefix = f"{run_dir}/{kind}"
    lines = runtime.script_header(run_dir, kind, "the worker process may still be alive", group=True)
    if kind == "qa":
        lines += runtime.codex_env()
    if token_file:
        lines += runtime.token_checks(token_file)
    lines += runtime.clean_checks(run_dir, workdir, git, head, ("recorded HEAD", "ran"))
    lines += runtime.background_target(f"{q(str(common.GTIMEOUT))} -k 60 {timeout} {shlex.join(argv)}",
                               stdin=run_dir + "/spec.md", stdout=prefix + ".out", stderr=prefix + ".err",
                               token_file=token_file)
    lines += [
        "date '+%Y-%m-%dT%H:%M:%S%z' > {}".format(q(run_dir + "/ended_at")),
        "{} rev-parse HEAD > {} 2>> {}".format(q(git), q(run_dir + "/head_after"), q(prefix + ".err")),
        "{} status --porcelain --untracked-files=normal > {} 2>> {}".format(
            q(git), q(run_dir + "/status_after"), q(prefix + ".err")),
        "finish $code",
        "exit $code",
    ]
    return "\n".join(lines) + "\n"


def cmd_worker(kind, argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    if argv and argv[0].split("=", 1)[0] == "--launch-run":
        return review.cmd_launch_run(argv, out, err, kind)
    reject_worker_overrides(kind, argv)
    args = worker_parser(kind).parse_args(argv)
    via = getattr(args, "via", "cmux")
    if kind == "claude" and via == "direct" and not common.CLAUDE_IMPL_DIRECT:
        raise common.Fail(common.CLAUDE_IMPL_DIRECT_REFUSAL, 2)
    if not 60 <= args.timeout <= 86400:
        raise common.Fail("--timeout must be between 60 and 86400 seconds", 2)
    if kind == "claude" and args.advisor and common.CLAUDE_IMPL_POLICY[args.task_class]["model"] == args.advisor:
        raise common.Fail(f"--advisor {args.advisor} is pointless: class {args.task_class} already runs on {args.advisor}", 2)
    workdir = common.canonical(args.workdir, "--workdir", kind="dir")
    label = args.label or os.path.basename(workdir)
    if not common.LABEL.fullmatch(label):
        if args.label:
            raise common.Fail("--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}", 2)
        label = kind
    spec = common.canonical(args.spec, "--spec", kind="file")
    spec_bytes = spec_gate.require_spec_gate(spec, kind, workdir)
    codex.check_worktree_root(workdir)
    dirty = common.dirty_entries(workdir)
    if dirty:
        raise common.Fail(f"workdir has uncommitted or untracked changes ({len(dirty.splitlines())} entries, first: {dirty.splitlines()[0].strip()}); start the worker from "
                   "a clean worktree so its diff is attributable (keep the SPEC outside it or gitignored)"
                   )
    head = common.git_value(workdir, "rev-parse", "HEAD")
    binary = runtime.worker_binary(kind)
    for path, what in ((binary, kind + " worker CLI"), (str(common.GTIMEOUT), "gtimeout")):
        if not os.access(path, os.X_OK):
            raise common.Fail(f"{what} is not executable: {path}")
    if via == "direct":
        runtime.check_token_file(runtime.token_file())
    cmux = runtime.tools()[1]
    if not args.no_launch and via == "cmux":
        runtime.check_launcher(cmux, binary)
    root = runtime.state_root(kind)
    run_dir = "{}/{}-{}-{}".format(root, label, time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))
    os.mkdir(run_dir, 0o700)
    try:
        argv_out, policy = worker_argv(kind, args, run_dir, workdir, binary)
        Path(run_dir, "spec.md").write_bytes(spec_bytes)
        script = worker_script(kind=kind, run_dir=run_dir, workdir=workdir, head=head,
                               git=shutil.which("git") or "/usr/bin/git", argv=argv_out, timeout=args.timeout,
                               token_file=runtime.token_file() if via == "direct" else None)
        script_path = run_dir + "/inner.zsh"
        with open(script_path, "w", encoding="utf-8") as stream:
            stream.write(script)
        os.chmod(script_path, 0o700)
        meta = {"kind": kind, "workdir": workdir, "head": head, "spec_source": spec,
                "model": policy["model"], "effort": policy["effort"], "timeout": args.timeout, "label": label,
                "via": via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if kind == "claude":
            meta.update({"class": args.task_class, "advisor": args.advisor})
        else:
            meta["sandbox"] = policy["sandbox"]
        with open(run_dir + "/meta.json", "w", encoding="utf-8") as stream:
            json.dump(meta, stream, indent=1)
        launch = [cmux, "new-workspace", "--name", f"{kind}-{label}", "--cwd", workdir,
                  "--focus", "false", "--command", runtime.zsh_command(script_path)]
        with open(run_dir + "/launch.json", "w", encoding="utf-8") as stream:
            json.dump(launch, stream)
    except OSError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    if not args.no_launch:
        runtime.start_run(via=via, launch=launch, run_dir=run_dir, script_path=script_path, workdir=workdir, kind=kind)
    if kind == "claude":
        print("hermes-delegate: class={} model={} effort={} advisor={} via={}; Claude edits the worktree "
              "(acceptEdits, Bash allowed); review its diff in a separate session with another CLI before "
              "accepting".format(args.task_class, policy["model"], policy["effort"], args.advisor or "off", via),
              file=err)
    else:
        print("hermes-delegate: QA model={model} effort={effort} sandbox={sandbox} (fixed user pin); verify the "
              "screenshots and logs yourself before reporting".format(**common.QA_POLICY), file=err)
    runtime.print_launch(out, run_dir, None, not args.no_launch, args.timeout, kind, via)
    return 0
