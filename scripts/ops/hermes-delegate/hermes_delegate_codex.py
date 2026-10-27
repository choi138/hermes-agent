"""Gateway-owned Codex launch planning."""

import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
import hermes_delegate_common as common
import hermes_delegate_spec as spec_gate


def reject_forbidden(argv):
    for word in argv:
        name = word.split("=", 1)[0]
        if name == "--progress-code-scope":
            raise common.Fail("--progress-code-scope is not accepted here; pass --code-scope (files or "
                       "directories relative to --workdir) and hermes-delegate emits the runner flag", 2)
        if name in common.FORBIDDEN_CODEX or name.startswith("--progress-"):
            raise common.Fail(f"{name} is not accepted: model/effort come only from the runner policy and "
                       "progress options are gateway-owned", 2)


def classify_scope(paths):
    """Ask the runner's own predicate so directory expansion cannot drift from it."""
    script = ("import json,re,sys\n"
              "sys.path.insert(0, sys.argv[1])\n"
              "from agent.delegation_progress import file_class\n"
              "print(json.dumps([bool(re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', p)) and not p.startswith('/')"
              " and '..' not in p.split('/') and bool(file_class(p)) for p in json.load(sys.stdin)]))\n")
    result = common.run([str(common.RUNNER_PYTHON), "-I", "-c", script, str(common.RELEASE)], cwd="/",
                 stdin=json.dumps(paths))
    if result.returncode:
        raise common.Fail("code scope classification failed: " + common.last_line(result.stderr))
    return json.loads(result.stdout)


BASELINE_TOLERATED = {"files_truncated", "content_truncated", "symlink_or_special"}


BASELINE_TRANSIENT = {"file_changed_during_observation", "git_changed_during_observation"}


def check_worktree_root(workdir):
    result = common.run(["git", "-C", workdir, "rev-parse", "--show-toplevel"])
    top = result.stdout.strip()
    if result.returncode or os.path.realpath(top) != workdir:
        raise common.Fail(f"--workdir must be a git worktree root; the runner baseline needs it (git toplevel: {top or common.last_line(result.stderr)})"
                   )


def baseline_errors(workdir, allowed_root, output_dir, code_scope):
    """Run the runner's own collect() so a dry-run pass implies register_run's baseline passes."""
    script = ("import json,sys\n"
              "sys.path.insert(0, sys.argv[1])\n"
              "from pathlib import Path\n"
              "from agent.delegation_progress import Manifest, collect\n"
              "a = json.load(sys.stdin)\n"
              "m = Manifest('hd-baseline-check', Path(a['workdir']), Path(a['root']), Path(a['out']), '1',\n"
              "             'hermes-delegate baseline', code_scope=tuple(a['scope']),\n"
              "             sender_profile=a['profile'], expected_bot_id=a['bot'])\n"
              "print(json.dumps(collect(m)['errors']))\n")
    payload = json.dumps({"workdir": workdir, "root": allowed_root, "out": output_dir, "scope": code_scope,
                          "profile": common.SENDER_PROFILE, "bot": common.EXPECTED_BOT_ID})
    for attempt in (1, 2):
        result = common.run([str(common.RUNNER_PYTHON), "-I", "-c", script, str(common.RELEASE)], cwd="/", stdin=payload)
        if result.returncode:
            raise common.Fail("runner baseline check failed: " + common.last_line(result.stderr))
        errors = set(json.loads(result.stdout)) - BASELINE_TOLERATED
        if not errors or not errors <= BASELINE_TRANSIENT or attempt == 2:
            return sorted(errors)
        time.sleep(1)


def git_files(workdir, relative_dir):
    result = common.run(["git", "-C", workdir, "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard", "--", relative_dir])
    if result.returncode:
        raise common.Fail(f"git ls-files failed in {workdir}: {common.last_line(result.stderr)}")
    names = sorted(set(name for name in result.stdout.split("\0") if name))
    return [name for name in names if not os.path.islink(os.path.join(workdir, name))
            and os.path.isfile(os.path.join(workdir, name))]


def resolve_code_scope(entries, workdir, notes):
    explicit, expanded = [], []
    for entry in entries:
        if os.path.isabs(entry):
            path = entry
        else:
            if os.path.normpath(entry) != entry or entry.startswith("../") or entry == "..":
                raise common.Fail(f"code scope is not canonical: {entry}")
            path = os.path.join(workdir, entry)
        common.canonical(path, "code scope", must_exist=False)
        if not common.inside(path, workdir):
            raise common.Fail(f"code scope is outside the workdir: {entry}")
        relative = os.path.relpath(path, workdir)
        if os.path.isdir(path):
            files = git_files(workdir, "." if relative == "." else relative)
            if not files:
                raise common.Fail(f"code scope directory has no git-visible files: {entry}")
            expanded.extend(files)
        else:
            if not os.path.exists(path):
                notes.append(f"code scope {relative} does not exist yet (allowed for new files)")
            explicit.append(relative)
    accepted = []
    if explicit:
        for name, ok in zip(explicit, classify_scope(explicit)):
            if not ok:
                raise common.Fail(f"code scope file is not a runner progress source (code_scope): {name}")
            accepted.append(name)
    if expanded:
        verdicts = classify_scope(expanded)
        kept = [name for name, ok in zip(expanded, verdicts) if ok]
        skipped = len(expanded) - len(kept)
        if not kept:
            raise common.Fail("code scope directories contain no runner-accepted source/doc/build files")
        if skipped:
            notes.append(f"code scope: skipped {skipped} non-source files from directories")
        accepted.extend(kept)
    accepted = sorted(set(accepted))
    if len(accepted) > 1024:
        raise common.Fail(f"code scope has {len(accepted)} files; the runner accepts at most 1024")
    return accepted


def ensure_output_dir(value, workdir, notes):
    created = None
    if value is None:
        root = Path(os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(common.DEFAULT_STATE_ROOT)))
        value = str(root / "codex" / "{}-{}".format(time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3)))
    if not os.path.exists(value):
        parent = os.path.dirname(value)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        common.canonical(parent, "output dir parent", kind="dir")
        os.mkdir(value, 0o700)
        created = value
        notes.append("created private output dir " + value)
    common.canonical(value, "--output-dir", kind="dir")
    info = os.stat(value)
    if info.st_uid != common.numeric_uid() or info.st_mode & 0o022:
        raise common.Fail("output dir must be owned by you and not group/other writable: " + value)
    if common.inside(value, workdir):
        raise common.Fail("output dir must be outside the workdir (runner: artifacts_must_be_outside_worktree)")
    return value, created


def build_codex_argv(args, *, spec, workdir, allowed_root, output_dir, code_scope):
    argv = [str(common.RUNNER_PYTHON), str(common.RUNNER), "--spec", spec, "--workdir", workdir,
            "--allowed-root", allowed_root, "--output-dir", output_dir,
            "--task-class", args.task_class]
    for name in common.POLICY_CHOICE_OPTIONS:
        value = getattr(args, name)
        if value is not None:
            argv += [common.flag(name), value]
    for name in common.POLICY_TEXT_OPTIONS:
        value = getattr(args, name)
        if value is not None:
            argv += [common.flag(name), value]
    for name in common.POLICY_FLAGS:
        if getattr(args, name):
            argv.append(common.flag(name))
    for ref in args.handoff_ref:
        argv += ["--handoff-ref", ref]
    for name in ("attempt", "max_attempts", "sandbox", "timeout"):
        value = getattr(args, name)
        if value is not None:
            argv += [common.flag(name), value]
    if args.report is not None:
        argv += ["--report", args.report]
    argv += ["--progress-login-shell", "zsh", "--progress-sender-profile", common.SENDER_PROFILE,
             "--progress-expected-bot-id", common.EXPECTED_BOT_ID]
    for name in code_scope:
        argv += ["--progress-code-scope", name]
    return argv


def gateway_accepts(command):
    """Mirror of prepare_launch() recognition in server tools/delegation_notifications.py
    (f178a93 tree, lines 56-86) minus the session/thread checks; keep in sync."""
    if any(char in command for char in "$`\n\r~"):
        return False
    quote, escaped = None, False
    for char in command:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char == quote:
            quote = None
        elif quote is None and char in "\"'":
            quote = char
        elif quote is None and char in "*?[{()}#":
            return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)
    except ValueError:
        return False
    if any(word and all(char in ";&|<>" for char in word) for word in argv):
        return False
    return not (len(argv) < 3
                or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", os.path.basename(argv[0]))
                or not argv[1].startswith("/") or not argv[1].endswith("/scripts/run_codex_task.py")
                or any(word.startswith("--progress-") and word.split("=", 1)[0] not in
                       {"--progress-code-scope", "--progress-sender-profile",
                        "--progress-expected-bot-id", "--progress-login-shell"}
                       for word in argv[2:])
                or "--dry-run" in argv or argv[2] in {"lifecycle", "progress"})


def render_command(argv):
    command = shlex.join(argv)
    if any(char in command for char in "$`~;|&*?{}()#<>[]\n\r"):
        raise common.Fail("constructed command contains shell metacharacters")
    if shlex.split(command) != argv or not gateway_accepts(command):
        raise common.Fail("constructed command would not be recognized as a gateway-owned runner launch")
    return command


def dry_run(argv):
    tmp = tempfile.mkdtemp(prefix="hd-dry-", dir="/private/tmp")
    try:
        if os.path.realpath(tmp) != tmp:
            raise common.Fail("temporary dry-run dir is not canonical: " + tmp)
        probe = argv + ["--dry-run", "--progress-manifest", tmp + "/manifest.json",
                        "--progress-state-dir", tmp + "/state", "--progress-thread", "1",
                        "--progress-label", "hermes-delegate dry-run", "--progress-owner", "gateway"]
        result = common.run(probe, cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out, err = common.parse_json(result.stdout), common.parse_json(result.stderr)
    if result.returncode == 0 and out and out.get("status") == "planned":
        return out
    if out and out.get("policy"):
        policy = out["policy"]
        if result.returncode == 0 and out.get("status") == "skipped":
            raise common.Fail("runner status=skipped: no worker needed, nothing to launch; reason={} next_action={}"
                       .format(policy.get("reason"), policy.get("next_action")), common.EXIT_SKIPPED)
        raise common.Fail("runner status={} exit={} reason={} next_action={}".format(
            out.get("status"), result.returncode, policy.get("reason"), policy.get("next_action")),
            74 if result.returncode == 74 else 1)
    if err:
        raise common.Fail("runner exit {} {}{}".format(result.returncode, err.get("status"),
                                                 " reason=" + err["reason"] if err.get("reason") else ""),
                   74 if result.returncode == 74 else 1)
    raise common.Fail(f"runner exit {result.returncode}: {common.last_line(result.stderr or result.stdout)}",
               74 if result.returncode == 74 else 1)


def codex_parser():
    p = common.Parser(prog="hermes-delegate codex", allow_abbrev=False,
               description="Validate a gateway-owned Codex delegation and print its bare runner argv.")
    p.add_argument("--spec", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--class", "--task-class", dest="task_class", required=True)
    p.add_argument("--allowed-root")
    p.add_argument("--output-dir")
    p.add_argument("--report", metavar="REL")
    p.add_argument("--code-scope", nargs="+", action="extend", default=[])
    for name in common.POLICY_CHOICE_OPTIONS + ("sandbox",):
        p.add_argument(common.flag(name), dest=name)
    for name in common.POLICY_TEXT_OPTIONS:
        p.add_argument(common.flag(name), dest=name)
    for name in common.POLICY_FLAGS:
        p.add_argument(common.flag(name), dest=name, action="store_true")
    p.add_argument("--handoff-ref", action="append", default=[])
    p.add_argument("--attempt")
    p.add_argument("--max-attempts")
    p.add_argument("--timeout", required=True,
                   help="worker timeout seconds; required because the runner default is only 600")
    return p


def validate_codex_options(args):
    for name in ("task_class", "sandbox") + common.POLICY_CHOICE_OPTIONS:
        value = getattr(args, name)
        if value is not None and not common.CHOICE.fullmatch(value):
            raise common.Fail(f"{common.flag(name)} has an invalid value: {value}", 2)
    for name in common.POLICY_TEXT_OPTIONS:
        if getattr(args, name) is not None:
            common.check_free_text(name, getattr(args, name))
    for ref in args.handoff_ref:
        common.check_free_text("handoff_ref", ref)
    for name in ("attempt", "max_attempts"):
        value = getattr(args, name)
        if value is not None and not re.fullmatch(r"[1-9][0-9]{0,2}", value):
            raise common.Fail(f"{common.flag(name)} must be a positive integer", 2)
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", args.timeout) or not (
            math.isfinite(float(args.timeout)) and 0 < float(args.timeout) <= 86400):
        raise common.Fail("--timeout must be a finite number of seconds in (0, 86400]", 2)


def validate_report(args):
    if args.task_class in ("mechanical", "bounded") and args.report is None:
        raise common.Fail("--report is required for --class mechanical and bounded", 2)
    if args.report is not None:
        if not args.report or len(args.report) > 256:
            raise common.Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        if any(char in args.report for char in "\0\n\r"):
            raise common.Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        report = Path(args.report)
        if report.is_absolute() or ".." in report.parts or os.path.normpath(args.report) == ".":
            raise common.Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        if args.sandbox != "workspace-write":
            raise common.Fail("--report needs --sandbox workspace-write", 2)


def cmd_codex(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    reject_forbidden(argv)
    args = codex_parser().parse_args(argv)
    validate_codex_options(args)
    validate_report(args)
    notes = []
    if args.sandbox == "read-only":
        # guard_review_gate blocks a runner argv carrying an explicit read-only sandbox as a review;
        # the runner default is the same read-only mode, so the flag is dropped instead.
        args.sandbox = None
        notes.append("--sandbox read-only dropped from the printed line; the runner default is read-only")
    elif args.sandbox not in (None, "workspace-write"):
        raise common.Fail("--sandbox must be workspace-write (omit it for read-only)", 2)
    for path, what in ((common.RUNNER, "runner"), (common.RUNNER_PYTHON, "runner python")):
        if not os.path.exists(str(path)):
            raise common.Fail(f"{what} is missing: {path}")
    workdir = common.canonical(args.workdir, "--workdir", kind="dir")
    allowed_root = common.canonical(args.allowed_root or workdir, "--allowed-root", kind="dir")
    spec = common.canonical(args.spec, "--spec", kind="file")
    if not common.inside(workdir, allowed_root):
        raise common.Fail("--workdir is outside --allowed-root")
    if not common.inside(spec, allowed_root):
        raise common.Fail(f"--spec must be inside --allowed-root ({allowed_root})")
    spec_data = spec_gate.require_spec_gate(spec, "codex", workdir)
    if args.report is not None and args.report not in spec_data.decode("utf-8"):
        raise common.Fail("SPEC does not name the --report path: " + args.report, 2)
    check_worktree_root(workdir)
    code_scope = resolve_code_scope(args.code_scope, workdir, notes)
    if args.output_dir is not None:
        common.canonical(args.output_dir, "--output-dir", must_exist=False)
    output_dir, created = ensure_output_dir(args.output_dir, workdir, notes)
    try:
        argv_out = build_codex_argv(args, spec=spec, workdir=workdir, allowed_root=allowed_root,
                                    output_dir=output_dir, code_scope=code_scope)
        command = render_command(argv_out)
        planned = dry_run(argv_out)
        effective = (planned.get("policy") or {}).get("effective_task_class")
        if effective in ("mechanical", "bounded") and args.report is None:
            raise common.Fail("--report is required when the effective task class is " + effective, 2)
        errors = baseline_errors(workdir, allowed_root, output_dir, code_scope)
        if errors:
            raise common.Fail("the real launch would fail with registration_baseline_unavailable; runner "
                       "baseline errors: {}".format(",".join(errors)))
    except common.Fail:
        if created and not os.listdir(created):
            os.rmdir(created)
        raise
    policy = planned.get("policy") or {}
    selected = policy.get("selected") or {}
    for note in notes:
        print("hermes-delegate: " + note, file=err)
    print("hermes-delegate: dry-run planned model={} effort={} task_class={} reason={}".format(
        selected.get("model"), selected.get("effort"), policy.get("effective_task_class"),
        policy.get("reason")), file=err)
    if args.sandbox is None:
        print("hermes-delegate: note: sandbox is the runner default read-only", file=err)
    print("hermes-delegate: launch the next line alone via terminal(background=true, "
          "notify_on_complete=true); the gateway injects the progress manifest/state/thread/label/owner "
          "and reports progress and completion, so arm no watcher for it", file=err)
    print(command, file=out)
    return 0
