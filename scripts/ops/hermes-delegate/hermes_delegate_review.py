"""Independent review tickets and prepared run orchestration."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import hermes_delegate_common as common
import hermes_delegate_runtime as runtime


def review_parser():
    p = common.Parser(prog="hermes-delegate review", allow_abbrev=False,
               description="Issue a review-gate ticket and launch a gated read-only Claude or Codex review.")
    p.add_argument("--reviewer", required=True, choices=sorted(common.REVIEW_POLICY))
    p.add_argument("--item", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--base", help="optional base commit; diff.patch becomes git diff --binary BASE COMMIT")
    p.add_argument("--label")
    p.add_argument("--timeout", type=int, default=2700, help="review-gate run timeout seconds")
    p.add_argument("--via", choices=common.VIA,
                   help="direct (detached, no cmux) or cmux; default: claude cmux, codex direct")
    p.add_argument("--owner-approval", metavar="ID",
                   help="round 3+ only: the Discord message ID of the user's approval, passed to review-gate start")
    p.add_argument("--fallback", action="store_true",
                   help="retry a failed (UNREVIEWED) review in the same round with a different --reviewer on the "
                        "failed ticket's commit (review-gate start --fallback); never open a new round for it")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read-only preflight: validate, check the launcher, read the ledger; issues no ticket")
    mode.add_argument("--no-launch", action="store_true",
                      help="issue a real ticket and prepare the run dir, skip the launch (the ticket stays open)")
    return p


def review_template(run_dir, commit, base):
    what = f"git diff --binary {base[:12]} {commit[:12]}" if base else f"git show {commit[:12]}"
    return f"""

---
Review rules (added by hermes-delegate; they override anything above that conflicts):
- The change under review is in {run_dir}/diff.patch ({what}); the commit log and file stats are in
  {run_dir}/log.txt. Read both first. The working tree is a clean checkout of {commit}.
- Read-only: do not modify, create or delete files.
- Report only P0/P1 defects. Each finding needs file:line and a concrete trigger (input or state ->
  wrong result). Drop any finding that lacks either. No style, taste or extension suggestions.
- Round 2 or later (the prompt lists prior findings): limit the scope to whether each prior finding is
  resolved, regressions introduced by those fixes, and defects in the new diff. Do not report items
  the prompt lists as deferred by the owner. Mark a finding that is another input, spelling or path of
  a prior finding's defect as "same family as prior #N".
- If the prompt or SPEC defines a threat scope (inputs in scope vs out of scope), a bypass that needs
  out-of-scope input is not P0/P1: list it under a final "Out-of-scope gaps" heading instead.
- If there is no P0/P1, end with exactly this line:
{common.APPROVAL_LINE}
"""


def write_review_inputs(run_dir, workdir, commit, base):
    if base:
        diff = ["diff", "--binary", base, commit]
        log = ["log", "--stat", "--format=fuller", f"{base}..{commit}"]
    else:
        diff = ["show", commit]
        log = ["show", "--stat", "--format=fuller", commit]
    for name, args in (("diff.patch", diff), ("log.txt", log)):
        result = common.run_bytes(["git", "-C", workdir] + args)
        if result.returncode:
            raise common.Fail("git {} failed while writing {}".format(" ".join(args[:2]), name))
        Path(run_dir, name).write_bytes(result.stdout)


MCP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def codex_mcp_off():
    """Codex merges `-c mcp_servers={}` into the user table instead of replacing it, so each
    configured server is disabled by name; a failed listing only means MCP stays as configured."""
    env = dict(os.environ, ASDF_NODEJS_VERSION="22.19.0",
               PATH="{}:{}".format(common.CODEX_NODE_BIN, os.environ.get("PATH", "")))
    try:
        result = subprocess.run([runtime.worker_binary("codex"), "mcp", "list", "--json"], capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=10, env=env, check=False)
        servers = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(servers, list):
        return []
    names = sorted({s.get("name") for s in servers if isinstance(s, dict) and s.get("enabled")
                    and isinstance(s.get("name"), str) and MCP_NAME.fullmatch(s["name"])})
    return [arg for name in names for arg in ("-c", f"mcp_servers.{name}.enabled=false")]


def reviewer_command(reviewer, run_dir, workdir):
    policy = common.REVIEW_POLICY[reviewer]
    if reviewer == "codex":
        return [runtime.worker_binary("codex"), "exec", "--ephemeral", "-s", "read-only", "-m", policy["model"],
                "-c", 'model_reasoning_effort="{}"'.format(policy["effort"]), *codex_mcp_off(),
                "-C", workdir, "--json",
                "-o", run_dir + "/review.final.md", "-"]
    return [runtime.worker_binary("claude"), "-p", "--model", policy["model"], "--effort", policy["effort"],
            *common.CLAUDE_READ_ONLY, "--add-dir", run_dir]


CLOSE_GUIDANCE = ("hermes-delegate: the verdict is not recorded automatically; when the watcher ends, reproduce "
                  "each finding in review.result.md yourself, then record the verdict with review-gate close "
                  "(PASS: --blockers 0 or omitted; BLOCK: --blockers N>=1; UNREVIEWED: --reason). The user only "
                  "gives --owner-approval for round 3+: ask in Discord, then pass the approval message ID with "
                  "hermes-delegate review --owner-approval ID")


def check_launch_tools(via, cmux, binary):
    if via == "cmux":
        runtime.check_launcher(cmux, binary)
    else:
        runtime.check_binary(binary)


def review_checkout(args):
    label = args.label or args.item
    if not common.LABEL.fullmatch(label):
        raise common.Fail("--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}", 2)
    if not 60 <= args.timeout <= 86400:
        raise common.Fail("--timeout must be between 60 and 86400 seconds", 2)
    if args.owner_approval is not None and not runtime.OWNER_APPROVAL_ID.fullmatch(args.owner_approval):
        raise common.Fail("--owner-approval must be a Discord message ID (15-22 digits)", 2)
    workdir = common.canonical(args.workdir, "--workdir", kind="dir")
    prompt = common.canonical(args.prompt, "--prompt", kind="file")
    if os.path.getsize(prompt) == 0:
        raise common.Fail("--prompt is empty")
    for name in ("commit", "base"):
        value = getattr(args, name)
        if value is not None and not re.fullmatch(r"[0-9a-f]{7,40}", value):
            raise common.Fail(f"--{name} must be a 7-40 char lowercase hex SHA", 2)
    full = common.git_value(workdir, "rev-parse", "--verify", "--quiet", args.commit + "^{commit}")
    head = common.git_value(workdir, "rev-parse", "HEAD")
    if head != full:
        raise common.Fail(f"workdir HEAD {head[:12]} is not --commit {full[:12]}; check out the reviewed commit first")
    dirty = common.dirty_entries(workdir)
    if dirty:
        raise common.Fail(f"workdir has uncommitted or untracked changes ({len(dirty.splitlines())} entries, first: {dirty.splitlines()[0].strip()}); the reviewer reads "
                   "the working tree, so review only a clean checkout of --commit")
    tree = common.git_value(workdir, "rev-parse", full + "^{tree}")
    diff_sha, base = None, None
    if args.base:
        base = common.git_value(workdir, "rev-parse", "--verify", "--quiet", args.base + "^{commit}")
        diff = common.run_bytes(["git", "-C", workdir, "diff", "--binary", base, full])
        if diff.returncode:
            raise common.Fail(f"git diff {base[:12]} {full[:12]} failed")
        diff_sha = hashlib.sha256(diff.stdout).hexdigest()
    return label, workdir, prompt, full, tree, diff_sha, base


def cmd_review(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    if argv and argv[0].split("=", 1)[0] == "--launch-run":
        return cmd_launch_run(argv, out, err)
    args = review_parser().parse_args(argv)
    via = args.via or common.REVIEW_POLICY[args.reviewer]["via"]
    label, workdir, prompt, full, tree, diff_sha, base = review_checkout(args)
    review_gate, cmux = runtime.tools()
    if not os.access(review_gate, os.X_OK):
        raise common.Fail("review-gate is not executable: " + review_gate)
    binary = runtime.worker_binary(args.reviewer)
    use_token = args.reviewer == "claude" and via == "direct"
    if use_token:
        runtime.check_token_file(runtime.token_file())
    if args.check:
        check_launch_tools(via, cmux, binary)
        refusal = runtime.start_refusal(runtime.ledger_status(review_gate, args.item), args.owner_approval, args.fallback,
                                args.reviewer)
        if refusal:
            raise common.Fail("review-gate start would refuse: " + refusal, 2)
        print("check=ok item={} reviewer={} via={} commit={} tree={}{} (no ticket issued; auth not checked, "
              "use hermes-delegate smoke {} --via {})".format(
                  args.item, args.reviewer, via, full[:12], tree[:12],
                  " diff_sha256=" + diff_sha if diff_sha else "", args.reviewer, via), file=out)
        return 0
    if not args.no_launch:
        check_launch_tools(via, cmux, binary)
    root = runtime.review_state_root()
    run_dir = "{}/{}-{}-{}".format(root, label, time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))
    os.mkdir(run_dir, 0o700)
    approval = ["--owner-approval", args.owner_approval] if args.owner_approval else []
    approval += ["--fallback"] if args.fallback else []
    started = common.run([review_gate, "start", "--item", args.item, "--reviewer", args.reviewer,
                   "--commit", args.commit] + approval, timeout=60)
    if started.returncode:
        shutil.rmtree(run_dir, ignore_errors=True)
        err.write(started.stderr if started.stderr.endswith("\n") or not started.stderr
                  else started.stderr + "\n")
        if not started.stderr.strip():
            print(f"hermes-delegate: review-gate start exit {started.returncode}", file=err)
        return started.returncode or 1
    ticket = started.stdout.strip()
    try:
        if not re.fullmatch(r"[0-9a-f]{32}", ticket):
            raise common.Fail(f"review-gate start returned an unexpected ticket: {ticket[:80]!r}")
        policy = common.REVIEW_POLICY[args.reviewer]
        reviewer_argv = reviewer_command(args.reviewer, run_dir, workdir)
        write_review_inputs(run_dir, workdir, full, base)
        Path(run_dir, "prompt.md").write_bytes(Path(prompt).read_bytes()
                                               + review_template(run_dir, full, base).encode())
        script = runtime.inner_script(run_dir=run_dir, workdir=workdir, ticket=ticket, review_gate=review_gate,
                              timeout=args.timeout, reviewer_argv=reviewer_argv,
                              gate_db=os.environ.get("REVIEW_GATE_DB"), commit=full,
                              git=shutil.which("git") or "/usr/bin/git", reviewer=args.reviewer,
                              token_file=runtime.token_file() if use_token else None)
        script_path = run_dir + "/inner.zsh"
        with open(script_path, "w", encoding="utf-8") as stream:
            stream.write(script)
        os.chmod(script_path, 0o700)
        with open(run_dir + "/ticket", "w", encoding="utf-8") as stream:
            stream.write(ticket + "\n")
        with open(run_dir + "/meta.json", "w", encoding="utf-8") as stream:
            json.dump({"kind": "review", "item": args.item, "reviewer": args.reviewer, "commit": full,
                       "tree": tree, "base": args.base, "diff_sha256": diff_sha, "workdir": workdir,
                       "prompt_source": prompt, "ticket": ticket, "model": policy["model"],
                       "effort": policy["effort"], "timeout": args.timeout, "label": label, "via": via,
                       "owner_approval": args.owner_approval, "fallback": args.fallback,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, stream, indent=1)
        launch = [cmux, "new-workspace", "--name", "review-" + label, "--cwd", workdir, "--focus", "false",
                  "--command", runtime.zsh_command(script_path)]
        with open(run_dir + "/launch.json", "w", encoding="utf-8") as stream:
            json.dump(launch, stream)
        if not args.no_launch:
            runtime.start_run(via=via, launch=launch, run_dir=run_dir, script_path=script_path, workdir=workdir,
                      ticket=ticket)
    except (common.Fail, OSError) as exc:
        message = str(exc)
        if ticket not in message:
            message += (f"; ticket {ticket} stays open "
                        f"(run dir {run_dir}); report it and close it as UNREVIEWED with the "
                        "reason")
        raise common.Fail(message, getattr(exc, "code", 1))
    runtime.print_launch(out, run_dir, ticket, not args.no_launch, args.timeout, via=via)
    print(CLOSE_GUIDANCE, file=err)
    return 0


def cmd_launch_run(argv, out, err, kind="review"):
    p = common.Parser(prog=f"hermes-delegate {kind} --launch-run", allow_abbrev=False)
    p.add_argument("--launch-run", dest="run_dir", required=True)
    args = p.parse_args(argv)
    run_dir = common.canonical(args.run_dir, "--launch-run", kind="dir")
    if os.path.dirname(run_dir) != runtime.state_root(kind):
        raise common.Fail("--launch-run must be a run dir created by hermes-delegate " + kind)
    try:
        meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))
        launch = json.loads(Path(run_dir, "launch.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise common.Fail(f"run dir is incomplete: {exc}")
    if (os.path.exists(run_dir + "/lock") or os.path.exists(run_dir + "/started_at")
            or os.path.exists(f"{run_dir}/{kind}.rc")):
        if kind == "review":
            raise common.Fail("this run already started (lock, started_at or rc present); a ticket runs once, so "
                       "record its verdict and start a new review with a new ticket")
        raise common.Fail("this run already started (lock, started_at or rc present); start a new run instead")
    review_gate, cmux = runtime.tools()
    ticket = meta.get("ticket")
    via = meta.get("via", "cmux")
    if kind == "review":
        state = runtime.ledger_status(review_gate, meta["item"])
        row = next((t for t in state["tickets"] if t["id"] == ticket), None)
        if row is None or row["status"] != "open":
            raise common.Fail("ticket {} is not open in the ledger (status {})".format(
                ticket, row["status"] if row else "missing"), 2)
        binary = runtime.worker_binary(meta.get("reviewer", "claude"))
        if meta.get("reviewer", "claude") == "claude" and via == "direct":
            runtime.check_token_file(runtime.token_file())
    else:
        binary = runtime.worker_binary(kind)
        if via == "direct":
            runtime.check_token_file(runtime.token_file())
    head = meta["commit"] if kind == "review" else meta["head"]
    if common.git_value(meta["workdir"], "rev-parse", "HEAD") != head or common.dirty_entries(meta["workdir"]):
        raise common.Fail(f"workdir is no longer a clean checkout of {head[:12]}")
    if launch[0] != cmux:
        launch = [cmux] + launch[1:]
    check_launch_tools(via, cmux, binary)
    runtime.start_run(via=via, launch=launch, run_dir=run_dir, script_path=run_dir + "/inner.zsh",
              workdir=meta["workdir"], ticket=ticket, kind=kind)
    runtime.print_launch(out, run_dir, ticket, True, int(meta["timeout"]), kind, via)
    return 0
