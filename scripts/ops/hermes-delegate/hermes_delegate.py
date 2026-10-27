#!/usr/bin/python3
"""hermes-delegate: Mac entry point for koharu Codex delegations, Claude runs and device QA.

  codex   validate + dry-run the runner, then print the bare argv the gateway owns
  review  issue a review-gate ticket and launch a gated read-only Claude or Codex review
          (Claude: cmux by default, --via direct; Codex: direct by default, --via cmux)
  claude  launch a write-capable Claude Code implementation run (cmux by default)
  qa      launch a device/simulator QA run (Codex gpt-6-astra/high, fixed) in cmux
  smoke   tiny auth/exec check of the codex or claude CLI (not a review, no ticket)
  wait    block until a run dir has its rc file (for a background watcher)
  status  instant, non-blocking state of a run dir or codex output dir (check once, never poll)
  cancel  stop a launched run: SIGTERM its inner.zsh, which stops the worker and records the rc
"""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HOME = Path("/Users/choegeun-won")
RELEASE = HOME / "hermes-workspaces/delegation-notifications-20261013/releases/mac-20261027-terra-report"
RUNNER = RELEASE / "scripts/run_codex_task.py"
# Not resolved: realpath would drop the venv and with it psutil.
RUNNER_PYTHON = HOME / "Documents/hermes-agent-upstream/.venv/bin/python"
SENDER_PROFILE = "koharu"
EXPECTED_BOT_ID = "1554717538376753244"
HD_BIN = HOME / ".local/bin/hermes-delegate"
DEFAULT_STATE_ROOT = HOME / ".local/state/hermes-delegate"
DEFAULT_REVIEW_GATE = HOME / ".local/bin/review-gate"
DEFAULT_CMUX = Path("/Applications/cmux.app/Contents/Resources/bin/cmux")
CLAUDE_BIN = HOME / ".local/bin/claude"
# Absolute paths on purpose: inside cmux, PATH starts with cmux's claude/codex shims, and the
# codex shim adds --dangerously-bypass-hook-trust, whose warning event breaks runs.
CODEX_NODE_BIN = HOME / ".asdf/installs/nodejs/22.19.0/bin"
CODEX_BIN = CODEX_NODE_BIN / "codex"
GTIMEOUT = Path("/opt/homebrew/bin/gtimeout")

# One place for reviewer model/effort. Opus/high matches the proven sprint review script;
# reviews are judgment-heavy and read-only, so a cheaper tier saves little and misses more.
# Codex reviews are fixed at gpt-6.1-sol/high (koharu codex skill policy); the config.toml default
# gpt-6-astra is reserved for device QA.
REVIEW_POLICY = {"claude": {"model": "opus", "effort": "high", "via": "cmux"},
                 "codex": {"model": "gpt-6.1-sol", "effort": "high", "via": "direct"}}
VIA = ("direct", "cmux")
CLAUDE_IMPL_DIRECT = True
CLAUDE_IMPL_DIRECT_REFUSAL = "--via direct is not available for claude implementation runs"
CLAUDE_TOKEN_FILE = HOME / ".config/hermes-delegate/claude-oauth-token"
TOKEN_PREFIX = "sk-ant-oat"
TOKEN_PATTERN = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]*")
EXIT_NEVER_STARTED = 90
EXIT_NO_RC = 91
EXIT_STALLED = 92
STALL_DEFAULT_MINUTES = {"claude": 20, "review": 20, "qa": 30}
STALL_SAMPLE_SECONDS = 30.0
SLEEP_GAP_SECONDS = 60.0
REWATCH_MIN_SECONDS = 300
# Each stall re-arm spends one gateway continuation of the approval's process_continue_max_chain (default 10).
STALL_REARM_LIMIT = 2
EXIT_LOCKED = 75
APPROVAL_LINE = "APPROVED: no P0/P1"
CLAUDE_READ_ONLY = ("--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                    "--permission-mode", "dontAsk", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
                    "--output-format", "stream-json", "--verbose")

# One place for Claude implementation model/effort (policy v2, 2026-10-27, provisional):
# Sonnet 5.5 for spec-mechanical lanes that must run on Claude anyway; Opus 5.5 for ambiguous
# bugs and unclear specs (medium) and for hard design/root-cause judgment (high).
CLAUDE_IMPL_POLICY = {
    "mechanical": {"model": "sonnet", "effort": "medium"},
    "ambiguous": {"model": "opus", "effort": "medium"},
    "judgment": {"model": "opus", "effort": "high"},
}
CLAUDE_ADVISORS = ("opus",)
# Per-run only, same as the runner: the advisor tool needs feature-flag fetching, which the
# user-level CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 turns off. Never edit settings.json.
ADVISOR_SETTINGS = '{"env":{"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":""}}'
# acceptEdits auto-approves file edits only inside the working directories (the worktree);
# with --permission-prompts none every other prompt is denied instead of hanging -p.
CLAUDE_WRITE = ("--tools", "Read,Glob,Grep,Edit,Write,Bash", "--allowedTools", "Bash",
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
                "--output-format", "stream-json", "--verbose")

# User pin: every device/simulator QA pass runs on Astra/high, which the runner rejects.
# danger-full-access: simulators, Xcode DerivedData and Metro's localhost sockets are outside
# what workspace-write allows.
QA_POLICY = {"model": "gpt-6-astra", "effort": "high", "sandbox": "danger-full-access"}
WORKER_OVERRIDES = ("--model", "--effort", "--tier", "--selection", "--pinned-tier", "--pinned-model",
                    "--pinned-effort", "--cli", "--sandbox", "--permission-mode",
                    "--dangerously-skip-permissions", "--allowedTools", "--tools", "--settings")

FORBIDDEN_CODEX = ("--model", "--effort", "--tier", "--selection", "--pinned-tier",
                   "--pinned-model", "--pinned-effort", "--cli", "--advisor", "--dry-run")
SAFE_TOKEN = re.compile(r"[A-Za-z0-9_./@%+=:,-]+")
FREE_TEXT_FORBIDDEN = set("$`~;|&*?[]{}()#<>\\'\"!\n\r\t")
CHOICE = re.compile(r"[a-z][a-z0-9._-]*")
EXIT_SKIPPED = 3
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

POLICY_CHOICE_OPTIONS = ("risk", "ambiguity", "phase", "implementation_class", "failure_kind",
                         "prior_model", "prior_effort")
POLICY_TEXT_OPTIONS = ("choice_reason", "deeper_analysis_evidence", "prior_action",
                       "correction_evidence")
POLICY_FLAGS = ("contract_resolved", "hard_judgment", "high_failure_cost", "missing_context",
                "broken_environment", "deterministic_tool_sufficient")


class Fail(Exception):
    def __init__(self, reason, code=1):
        super().__init__(reason)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(message, 2)


SPEC_SECTIONS = {
    "External contracts": ("external contracts", "외부 계약"),
    "Failure paths": ("failure paths", "실패 경로"),
    "Acceptance": ("acceptance", "완료 검증"),
}
SPEC_TOPICS = {
    "ordering": ("order", "concurren", "순서", "동시성"),
    "interruption": ("interrupt", "중단"),
    "rejection": ("reject", "partial", "거절", "부분 실패"),
    "repeat": ("repeat", "retry", "반복", "재시도"),
    "external": ("external", "외부"),
}
SPEC_VAGUE = ("1건 이상", "한 건 이상", "하나 이상", "1개 이상", "최소 1", "at least one",
              "at least 1", "one or more", "1 or more", ">= 1", ">=1", "≥ 1", "≥1", "etc.",
              "=> 1", "1+", "non-empty", "nonempty", "비어 있지 않", "적어도 하나", "적어도 1",
              "최소 한 개", "최소 한 건", "최소 하나", "1 이상", "한 개 이상", "한 번 이상", "1번 이상",
              "1회 이상", "한 회 이상", "not empty")
SPEC_NONZERO = ("non-zero", "nonzero", "0이 아님", "> 0", ">0", "!= 0", "≠ 0", "greater than 0",
                "greater than zero", "more than 0", "more than zero", "0보다 크", "0보다 큼", "0보다 커", "0보다 많", "0 초과",
                "0건 초과", "0개 초과")
SPEC_EXIT_BEFORE = re.compile(r"(?:\b(?:exit(?:s|ed)?|exit\s+(?:code|status)|status|rc|return\s*code|returncode)"
                              r"|종료\s*코드|종료)\s*(?:is|was|=|:|가|는|이)?\s*$", re.I)
SPEC_HOST = re.compile(r"localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]", re.I)


def spec_phrase_re(phrase):
    body = r" ?".join(re.escape(part) for part in phrase.split(" "))
    before = r"(?<![0-9A-Za-z.])" if phrase[0].isalnum() else r"(?<![0-9])"
    return re.compile(before + body + r"(?![0-9]|\.[0-9])", re.I)


SPEC_VAGUE_RE = tuple((phrase, spec_phrase_re(phrase)) for phrase in SPEC_VAGUE)
SPEC_NONZERO_RE = tuple((phrase, spec_phrase_re(phrase)) for phrase in SPEC_NONZERO)


def spec_vague_phrases(item):
    item = re.sub(r"\s+", " ", re.sub(r"[`*_~]+", "", item))
    found = [phrase for phrase, pattern in SPEC_VAGUE_RE if pattern.search(item)]
    for phrase, pattern in SPEC_NONZERO_RE:
        if any(not SPEC_EXIT_BEFORE.search(item[:match.start()]) for match in pattern.finditer(item)):
            found.append(phrase)
    return found
SPEC_TEMPLATE = (
    "## External contracts\n"
    "- `<path>:<line>` — <observed contract and captured output>\n\n"
    "## Failure paths\n"
    "- Ordering/concurrency: <behavior and verification>\n"
    "- Interruption: <behavior and verification>\n"
    "- Rejection/partial failure: <behavior and verification>\n"
    "- Repeat/retry: <behavior and verification>\n"
    "- External: <behavior and verification>\n\n"
    "## Acceptance\n"
    "- <each expected result and its exact verification>\n"
)


def spec_na(text):
    match = re.fullmatch(r"(?:N/A|해당 없음)[:：]\s*(.*)", text.strip(), re.I | re.S)
    return bool(match and len(re.sub(r"\s", "", match[1])) >= 8)


def spec_problems(text, kind, workdir, spec_dir):
    sections = {name: [] for name in SPEC_SECTIONS}
    current = None
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            if current is not None:
                current.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
        heading = re.match(r"^(#{1,2}) +(.*)$", line)
        if heading:
            current = None
            if heading[1] == "##":
                for name, aliases in SPEC_SECTIONS.items():
                    if heading[2].strip().lower() in aliases:
                        current = []
                        sections[name].append(current)
                        break
        elif current is not None:
            current.append(line)
    problems = []

    def problem(section, reason, item=None):
        excerpt = " {!r}".format(item[:60]) if item is not None else ""
        problems.append("{}{}: {}".format(section, excerpt, reason))

    required = ("Acceptance",) if kind == "qa" else tuple(SPEC_SECTIONS)
    for section in required:
        bodies = sections[section]
        if not bodies:
            problem(section, "missing section")
        if len(bodies) > 1:
            problem(section, "duplicated section")
        for body in bodies:
            items = []
            fence = None
            active = False
            for line in body:
                marker = re.match(r"^\s*(`{3,}|~{3,})", line)
                item = re.match(r"^(?:[-*]|\d+[.)]) (.*)$", line) if not fence else None
                if item:
                    items.append(item[1])
                    active = True
                elif re.match(r"^#{1,6}\s", line) and not fence:
                    active = False
                    continue
                elif active:
                    items[-1] += "\n" + line
                if marker:
                    if fence is None:
                        fence = marker[1]
                    elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                        fence = None
            items = [item.strip() for item in items]
            if not items:
                problem(section, "no items")
            covered = set()
            for item in items:
                if section == "External contracts":
                    if spec_na(item):
                        if len(items) != 1:
                            problem(section, "N/A must be the only item", item)
                        continue
                    resolved = False
                    for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", item):
                        match = re.fullmatch(r"(.+):(\d+)(?:-(\d+))?", span)
                        try:
                            path = Path(match[1] if match else span).expanduser()
                        except (RuntimeError, ValueError):
                            problem(section, "invalid file reference {!r}".format(span), item)
                            continue
                        candidates = [path] if path.is_absolute() else [Path(workdir) / path, Path(spec_dir) / path]
                        if match and SPEC_HOST.fullmatch(match[1]) and not any(
                                candidate.is_file() for candidate in candidates):
                            continue
                        valid = False
                        for candidate in candidates:
                            try:
                                if not candidate.is_file():
                                    continue
                                with candidate.open("rb") as stream:
                                    if match:
                                        count = sum(1 for _ in stream)
                                        start, end = int(match[2]), int(match[3] or match[2])
                                        valid = 1 <= start <= end <= count
                                    else:
                                        stream.read(1)
                                        valid = True
                            except (OSError, ValueError):
                                continue
                            if valid:
                                break
                        resolved |= valid
                        if match and not valid:
                            problem(section, "invalid file/line reference {!r}".format(span), item)
                    if not resolved:
                        problem(section, "no resolving file reference", item)
                elif section == "Failure paths":
                    match = re.match(r"^([^:：]+)[:：]\s*(.*)$", item, re.S)
                    if not match:
                        problem(section, "expected label: answer", item)
                        continue
                    label, answer = match[1].lower(), match[2]
                    for topic, aliases in SPEC_TOPICS.items():
                        if any(alias in label for alias in aliases):
                            covered.add(topic)
                            if not spec_na(answer) and len(re.sub(r"\s", "", answer)) < 15:
                                problem(section, "too-short {} answer".format(topic), item)
                else:
                    if re.match(r"^(?:N/A|해당 없음)[:：]", item, re.I):
                        problem(section, "N/A is not allowed", item)
                    for phrase in spec_vague_phrases(item):
                        problem(section, "vague phrase {!r}".format(phrase), item)
            if section == "Failure paths":
                for topic in SPEC_TOPICS:
                    if topic not in covered:
                        problem(section, "missing {} topic".format(topic))
    return problems


def read_spec_gate(spec, kind, workdir):
    try:
        data = Path(spec).read_bytes()
    except OSError as exc:
        return b"", ["SPEC: cannot read file: {}".format(exc)]
    problems = []
    if not 0 < len(data) <= 1024 * 1024:
        problems.append("SPEC: must be non-empty and at most 1 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("SPEC: not UTF-8")
        return data, problems
    problems.extend(spec_problems(text, kind, workdir, Path(spec).parent))
    return data, problems


def require_spec_gate(spec, kind, workdir):
    data, problems = read_spec_gate(spec, kind, workdir)
    if problems:
        details = "; ".join("({}) {}".format(i, problem) for i, problem in enumerate(problems, 1))
        raise Fail("SPEC gate refused {}: {} problem(s): {}; check with: hermes-delegate spec-check "
                   "--spec {} --workdir {} --kind {}".format(
                       spec, len(problems), details, shlex.quote(str(spec)), shlex.quote(str(workdir)), kind), 2)
    return data


def cmd_spec_check(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = Parser(prog="hermes-delegate spec-check", allow_abbrev=False)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec")
    source.add_argument("--template", action="store_true")
    parser.add_argument("--workdir")
    parser.add_argument("--kind", choices=("codex", "claude", "qa"), default="codex")
    args = parser.parse_args(argv)
    if args.template:
        out.write(SPEC_TEMPLATE)
        return 0
    spec = Path(args.spec).absolute()
    _, problems = read_spec_gate(spec, args.kind, args.workdir or spec.parent)
    for problem in problems:
        print(problem.replace("\n", " ").replace("\r", " "), file=err)
    if problems:
        return 2
    print("ok", file=out)
    return 0


def flag(name):
    return "--" + name.replace("_", "-")


def canonical(value, what, *, must_exist=True, kind=None):
    if not value or not os.path.isabs(value):
        raise Fail("{} must be an absolute path: {}".format(what, value))
    if os.path.normpath(value) != value or "//" in value:
        raise Fail("{} is not canonical: {}".format(what, value))
    if must_exist and not os.path.lexists(value):
        raise Fail("{} does not exist: {}".format(what, value))
    if os.path.realpath(value) != value:
        raise Fail("{} is a symlink or passes through one (realpath {}): {}".format(
            what, os.path.realpath(value), value))
    if kind == "dir" and not os.path.isdir(value):
        raise Fail("{} is not a directory: {}".format(what, value))
    if kind == "file" and not os.path.isfile(value):
        raise Fail("{} is not a regular file: {}".format(what, value))
    if not SAFE_TOKEN.fullmatch(value):
        raise Fail("{} contains characters the gateway argv cannot carry: {}".format(what, value))
    return value


def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_free_text(name, value):
    if not value or any(char in FREE_TEXT_FORBIDDEN for char in value) or len(value) > 512:
        raise Fail("{} must be a single line of at most 512 chars without shell metacharacters"
                   .format(flag(name)))
    return value


def reject_forbidden(argv):
    for word in argv:
        name = word.split("=", 1)[0]
        if name == "--progress-code-scope":
            raise Fail("--progress-code-scope is not accepted here; pass --code-scope (files or "
                       "directories relative to --workdir) and hermes-delegate emits the runner flag", 2)
        if name in FORBIDDEN_CODEX or name.startswith("--progress-"):
            raise Fail("{} is not accepted: model/effort come only from the runner policy and "
                       "progress options are gateway-owned".format(name), 2)


def run(argv, *, cwd=None, stdin=None, timeout=120, env=None):
    try:
        return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        raise Fail("cannot execute {}".format(argv[0]))
    except subprocess.TimeoutExpired:
        raise Fail("{} timed out after {}s".format(os.path.basename(str(argv[0])), timeout))


def classify_scope(paths):
    """Ask the runner's own predicate so directory expansion cannot drift from it."""
    script = ("import json,re,sys\n"
              "sys.path.insert(0, sys.argv[1])\n"
              "from agent.delegation_progress import file_class\n"
              "print(json.dumps([bool(re.fullmatch(r'[A-Za-z0-9_./-]{1,200}', p)) and not p.startswith('/')"
              " and '..' not in p.split('/') and bool(file_class(p)) for p in json.load(sys.stdin)]))\n")
    result = run([str(RUNNER_PYTHON), "-I", "-c", script, str(RELEASE)], cwd="/",
                 stdin=json.dumps(paths))
    if result.returncode:
        raise Fail("code scope classification failed: " + last_line(result.stderr))
    return json.loads(result.stdout)


BASELINE_TOLERATED = {"files_truncated", "content_truncated", "symlink_or_special"}
BASELINE_TRANSIENT = {"file_changed_during_observation", "git_changed_during_observation"}


def check_worktree_root(workdir):
    result = run(["git", "-C", workdir, "rev-parse", "--show-toplevel"])
    top = result.stdout.strip()
    if result.returncode or os.path.realpath(top) != workdir:
        raise Fail("--workdir must be a git worktree root; the runner baseline needs it (git toplevel: {})"
                   .format(top or last_line(result.stderr)))


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
                          "profile": SENDER_PROFILE, "bot": EXPECTED_BOT_ID})
    for attempt in (1, 2):
        result = run([str(RUNNER_PYTHON), "-I", "-c", script, str(RELEASE)], cwd="/", stdin=payload)
        if result.returncode:
            raise Fail("runner baseline check failed: " + last_line(result.stderr))
        errors = set(json.loads(result.stdout)) - BASELINE_TOLERATED
        if not errors or not errors <= BASELINE_TRANSIENT or attempt == 2:
            return sorted(errors)
        time.sleep(1)


def git_files(workdir, relative_dir):
    result = run(["git", "-C", workdir, "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard", "--", relative_dir])
    if result.returncode:
        raise Fail("git ls-files failed in {}: {}".format(workdir, last_line(result.stderr)))
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
                raise Fail("code scope is not canonical: {}".format(entry))
            path = os.path.join(workdir, entry)
        canonical(path, "code scope", must_exist=False)
        if not inside(path, workdir):
            raise Fail("code scope is outside the workdir: {}".format(entry))
        relative = os.path.relpath(path, workdir)
        if os.path.isdir(path):
            files = git_files(workdir, "." if relative == "." else relative)
            if not files:
                raise Fail("code scope directory has no git-visible files: {}".format(entry))
            expanded.extend(files)
        else:
            if not os.path.exists(path):
                notes.append("code scope {} does not exist yet (allowed for new files)".format(relative))
            explicit.append(relative)
    accepted = []
    if explicit:
        for name, ok in zip(explicit, classify_scope(explicit)):
            if not ok:
                raise Fail("code scope file is not a runner progress source (code_scope): {}".format(name))
            accepted.append(name)
    if expanded:
        verdicts = classify_scope(expanded)
        kept = [name for name, ok in zip(expanded, verdicts) if ok]
        skipped = len(expanded) - len(kept)
        if not kept:
            raise Fail("code scope directories contain no runner-accepted source/doc/build files")
        if skipped:
            notes.append("code scope: skipped {} non-source files from directories".format(skipped))
        accepted.extend(kept)
    accepted = sorted(set(accepted))
    if len(accepted) > 1024:
        raise Fail("code scope has {} files; the runner accepts at most 1024".format(len(accepted)))
    return accepted


def ensure_output_dir(value, workdir, notes):
    created = None
    if value is None:
        root = Path(os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(DEFAULT_STATE_ROOT)))
        value = str(root / "codex" / "{}-{}".format(time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3)))
    if not os.path.exists(value):
        parent = os.path.dirname(value)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        canonical(parent, "output dir parent", kind="dir")
        os.mkdir(value, 0o700)
        created = value
        notes.append("created private output dir " + value)
    canonical(value, "--output-dir", kind="dir")
    info = os.stat(value)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Fail("output dir must be owned by you and not group/other writable: " + value)
    if inside(value, workdir):
        raise Fail("output dir must be outside the workdir (runner: artifacts_must_be_outside_worktree)")
    return value, created


def build_codex_argv(args, *, spec, workdir, allowed_root, output_dir, code_scope):
    argv = [str(RUNNER_PYTHON), str(RUNNER), "--spec", spec, "--workdir", workdir,
            "--allowed-root", allowed_root, "--output-dir", output_dir,
            "--task-class", args.task_class]
    for name in POLICY_CHOICE_OPTIONS:
        value = getattr(args, name)
        if value is not None:
            argv += [flag(name), value]
    for name in POLICY_TEXT_OPTIONS:
        value = getattr(args, name)
        if value is not None:
            argv += [flag(name), value]
    for name in POLICY_FLAGS:
        if getattr(args, name):
            argv.append(flag(name))
    for ref in args.handoff_ref:
        argv += ["--handoff-ref", ref]
    for name in ("attempt", "max_attempts", "sandbox", "timeout"):
        value = getattr(args, name)
        if value is not None:
            argv += [flag(name), value]
    if args.report is not None:
        argv += ["--report", args.report]
    argv += ["--progress-login-shell", "zsh", "--progress-sender-profile", SENDER_PROFILE,
             "--progress-expected-bot-id", EXPECTED_BOT_ID]
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
        raise Fail("constructed command contains shell metacharacters")
    if shlex.split(command) != argv or not gateway_accepts(command):
        raise Fail("constructed command would not be recognized as a gateway-owned runner launch")
    return command


def last_line(text):
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else "(no output)"


def parse_json(text):
    for line in reversed((text or "").strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def dry_run(argv):
    tmp = tempfile.mkdtemp(prefix="hd-dry-", dir="/private/tmp")
    try:
        if os.path.realpath(tmp) != tmp:
            raise Fail("temporary dry-run dir is not canonical: " + tmp)
        probe = argv + ["--dry-run", "--progress-manifest", tmp + "/manifest.json",
                        "--progress-state-dir", tmp + "/state", "--progress-thread", "1",
                        "--progress-label", "hermes-delegate dry-run", "--progress-owner", "gateway"]
        result = run(probe, cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out, err = parse_json(result.stdout), parse_json(result.stderr)
    if result.returncode == 0 and out and out.get("status") == "planned":
        return out
    if out and out.get("policy"):
        policy = out["policy"]
        if result.returncode == 0 and out.get("status") == "skipped":
            raise Fail("runner status=skipped: no worker needed, nothing to launch; reason={} next_action={}"
                       .format(policy.get("reason"), policy.get("next_action")), EXIT_SKIPPED)
        raise Fail("runner status={} exit={} reason={} next_action={}".format(
            out.get("status"), result.returncode, policy.get("reason"), policy.get("next_action")),
            74 if result.returncode == 74 else 1)
    if err:
        raise Fail("runner exit {} {}{}".format(result.returncode, err.get("status"),
                                                 " reason=" + err["reason"] if err.get("reason") else ""),
                   74 if result.returncode == 74 else 1)
    raise Fail("runner exit {}: {}".format(result.returncode, last_line(result.stderr or result.stdout)),
               74 if result.returncode == 74 else 1)


def codex_parser():
    p = Parser(prog="hermes-delegate codex", allow_abbrev=False,
               description="Validate a gateway-owned Codex delegation and print its bare runner argv.")
    p.add_argument("--spec", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--class", "--task-class", dest="task_class", required=True)
    p.add_argument("--allowed-root")
    p.add_argument("--output-dir")
    p.add_argument("--report", metavar="REL")
    p.add_argument("--code-scope", nargs="+", action="extend", default=[])
    for name in POLICY_CHOICE_OPTIONS + ("sandbox",):
        p.add_argument(flag(name), dest=name)
    for name in POLICY_TEXT_OPTIONS:
        p.add_argument(flag(name), dest=name)
    for name in POLICY_FLAGS:
        p.add_argument(flag(name), dest=name, action="store_true")
    p.add_argument("--handoff-ref", action="append", default=[])
    p.add_argument("--attempt")
    p.add_argument("--max-attempts")
    p.add_argument("--timeout", required=True,
                   help="worker timeout seconds; required because the runner default is only 600")
    return p


def cmd_codex(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    reject_forbidden(argv)
    args = codex_parser().parse_args(argv)
    for name in ("task_class", "sandbox") + POLICY_CHOICE_OPTIONS:
        value = getattr(args, name)
        if value is not None and not CHOICE.fullmatch(value):
            raise Fail("{} has an invalid value: {}".format(flag(name), value), 2)
    for name in POLICY_TEXT_OPTIONS:
        if getattr(args, name) is not None:
            check_free_text(name, getattr(args, name))
    for ref in args.handoff_ref:
        check_free_text("handoff_ref", ref)
    for name in ("attempt", "max_attempts"):
        value = getattr(args, name)
        if value is not None and not re.fullmatch(r"[1-9][0-9]{0,2}", value):
            raise Fail("{} must be a positive integer".format(flag(name)), 2)
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", args.timeout) or not (
            math.isfinite(float(args.timeout)) and 0 < float(args.timeout) <= 86400):
        raise Fail("--timeout must be a finite number of seconds in (0, 86400]", 2)
    if args.task_class in ("mechanical", "bounded") and args.report is None:
        raise Fail("--report is required for --class mechanical and bounded", 2)
    if args.report is not None:
        if not args.report or len(args.report) > 256:
            raise Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        if any(char in args.report for char in "\0\n\r"):
            raise Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        report = Path(args.report)
        if report.is_absolute() or ".." in report.parts or os.path.normpath(args.report) == ".":
            raise Fail("--report must be a relative path inside the workdir: " + args.report, 2)
        if args.sandbox != "workspace-write":
            raise Fail("--report needs --sandbox workspace-write", 2)
    notes = []
    if args.sandbox == "read-only":
        # guard_review_gate blocks a runner argv carrying an explicit read-only sandbox as a review;
        # the runner default is the same read-only mode, so the flag is dropped instead.
        args.sandbox = None
        notes.append("--sandbox read-only dropped from the printed line; the runner default is read-only")
    elif args.sandbox not in (None, "workspace-write"):
        raise Fail("--sandbox must be workspace-write (omit it for read-only)", 2)
    for path, what in ((RUNNER, "runner"), (RUNNER_PYTHON, "runner python")):
        if not os.path.exists(str(path)):
            raise Fail("{} is missing: {}".format(what, path))
    workdir = canonical(args.workdir, "--workdir", kind="dir")
    allowed_root = canonical(args.allowed_root or workdir, "--allowed-root", kind="dir")
    spec = canonical(args.spec, "--spec", kind="file")
    if not inside(workdir, allowed_root):
        raise Fail("--workdir is outside --allowed-root")
    if not inside(spec, allowed_root):
        raise Fail("--spec must be inside --allowed-root ({})".format(allowed_root))
    spec_data = require_spec_gate(spec, "codex", workdir)
    if args.report is not None and args.report not in spec_data.decode("utf-8"):
        raise Fail("SPEC does not name the --report path: " + args.report, 2)
    check_worktree_root(workdir)
    code_scope = resolve_code_scope(args.code_scope, workdir, notes)
    if args.output_dir is not None:
        canonical(args.output_dir, "--output-dir", must_exist=False)
    output_dir, created = ensure_output_dir(args.output_dir, workdir, notes)
    try:
        argv_out = build_codex_argv(args, spec=spec, workdir=workdir, allowed_root=allowed_root,
                                    output_dir=output_dir, code_scope=code_scope)
        command = render_command(argv_out)
        planned = dry_run(argv_out)
        effective = (planned.get("policy") or {}).get("effective_task_class")
        if effective in ("mechanical", "bounded") and args.report is None:
            raise Fail("--report is required when the effective task class is " + effective, 2)
        errors = baseline_errors(workdir, allowed_root, output_dir, code_scope)
        if errors:
            raise Fail("the real launch would fail with registration_baseline_unavailable; runner "
                       "baseline errors: {}".format(",".join(errors)))
    except Fail:
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


def review_parser():
    p = Parser(prog="hermes-delegate review", allow_abbrev=False,
               description="Issue a review-gate ticket and launch a gated read-only Claude or Codex review.")
    p.add_argument("--reviewer", required=True, choices=sorted(REVIEW_POLICY))
    p.add_argument("--item", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--base", help="optional base commit; diff.patch becomes git diff --binary BASE COMMIT")
    p.add_argument("--label")
    p.add_argument("--timeout", type=int, default=2700, help="review-gate run timeout seconds")
    p.add_argument("--via", choices=VIA,
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


def git_value(workdir, *args):
    result = run(["git", "-C", workdir] + list(args))
    if result.returncode:
        raise Fail("git {} failed: {}".format(" ".join(args), last_line(result.stderr)))
    return result.stdout.strip()


def dirty_entries(workdir):
    return git_value(workdir, "status", "--porcelain", "--untracked-files=normal")


def script_header(run_dir, kind, hangup, group):
    """Lock, rc helpers and signal traps shared by every inner.zsh.

    The target runs with & and wait so a trap fires at once (zsh defers traps until a foreground
    child exits). A worker runs under gtimeout, which leads its own process group, so the TERM goes
    to that group; review-gate is not a group leader and stops its reviewer group itself."""
    q = shlex.quote
    rc, tmp_rc, errors = run_dir + "/{}.rc".format(kind), run_dir + "/{}.rc.tmp".format(kind), \
        run_dir + "/{}.err".format(kind)
    # Until gtimeout has made its group, only the pid exists; fall back to it.
    target = "-- -$child 2>/dev/null || kill -TERM $child" if group else "$child"
    return [
        "#!/bin/zsh",
        "mkdir {} 2>/dev/null || {{ print -r -- 'hermes-delegate inner: run dir already launched (lock exists); "
        "not started again' >> {}; exit {}; }}".format(q(run_dir + "/lock"), q(errors), EXIT_LOCKED),
        "set -u",
        "child=",
        "print -r -- $$ > {}".format(q(run_dir + "/inner.pid")),
        "finish() {{ [[ -e {rc} ]] || {{ print -r -- $1 > {tmp} && mv -f {tmp} {rc}; }}; }}".format(
            rc=q(rc), tmp=q(tmp_rc)),
        "fail() {{ print -r -- \"hermes-delegate inner: $2\" >> {}; finish $1; exit $1; }}".format(q(errors)),
        "interrupted() {",
        "  if [[ -n $child ]]; then",
        "    kill -TERM {} 2>/dev/null".format(target),
        "    wait $child",
        "    local code=$?",
        "    child=",
        "    (( code == 127 )) && code=$1",
        "    fail $code \"$2; sent SIGTERM to the {}, which exited $code\"".format(
            "worker group" if group else "review-gate run"),
        "  fi",
        "  fail $1 \"$2\"",
        "}",
        "trap 'interrupted 129 \"interrupted by SIGHUP (workspace closed?); {}\"' HUP".format(hangup),
        "trap 'interrupted 143 \"interrupted by SIGTERM\"' TERM",
        "trap 'interrupted 130 \"interrupted by SIGINT\"' INT",
    ]


def clean_checks(run_dir, workdir, git, head, what):
    q = shlex.quote
    return [
        "date '+%Y-%m-%dT%H:%M:%S%z' > {}".format(q(run_dir + "/started_at")),
        "cd {} || fail 2 'cannot cd to the workdir'".format(q(workdir)),
        "[[ \"$({} rev-parse HEAD)\" == {} ]] || fail 2 'workdir HEAD moved away from the {} "
        "before launch; nothing {}'".format(q(git), q(head), what[0], what[1]),
        "[[ -z \"$({} status --porcelain --untracked-files=normal)\" ]] || fail 2 'workdir became dirty "
        "before launch; nothing {}'".format(q(git), what[1]),
    ]


def token_checks(token_file):
    q = shlex.quote
    return [
        "[[ -f {0} && ! -L {0} ]] || fail 2 'claude OAuth token file is missing: '{0}".format(q(token_file)),
        "[[ \"$(/usr/bin/stat -f '%Lp:%u' {0})\" == \"600:$UID\" ]] || fail 2 'claude OAuth token file must be "
        "mode 0600 and owned by you: '{0}".format(q(token_file)),
        # 2026-08-25: an exported ANTHROPIC_API_KEY/BASE_URL silently replaced the OAuth login.
        "unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL",
    ]


def codex_env():
    return ["export ASDF_NODEJS_VERSION=22.19.0",
            "export PATH={}:$PATH".format(shlex.quote(str(CODEX_NODE_BIN)))]


def background_target(command, *, stdin, stdout, stderr, token_file=None):
    """Start the target in the background; the token is read here, at run time, and exported only
    inside the subshell that execs the target, so it never lands in argv or a file."""
    q = shlex.quote
    redirects = "< {} > {} 2>> {} &".format(q(stdin), q(stdout), q(stderr))
    if token_file:
        command = "( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {})\"; exec {} ) {}".format(
            q(token_file), command, redirects)
    else:
        command = "{} {}".format(command, redirects)
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
        "{} run --ticket {} --timeout {} -- {}".format(q(review_gate), q(ticket), timeout,
                                                       shlex.join(reviewer_argv)),
        stdin=run_dir + "/prompt.md", stdout=run_dir + "/review.out", stderr=errors, token_file=token_file)
    lines += [
        "date '+%Y-%m-%dT%H:%M:%S%z' > {}".format(q(run_dir + "/ended_at")),
        "finish $code",
        "exit $code",
    ]
    return "\n".join(lines) + "\n"


def state_root(kind):
    root = Path(os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(DEFAULT_STATE_ROOT))) / kind
    os.makedirs(str(root), mode=0o700, exist_ok=True)
    return canonical(str(root), kind + " state root", kind="dir")


def review_state_root():
    return state_root("review")


def tools():
    return (os.environ.get("HERMES_DELEGATE_REVIEW_GATE", str(DEFAULT_REVIEW_GATE)),
            os.environ.get("HERMES_DELEGATE_CMUX", str(DEFAULT_CMUX)))


def worker_binary(kind):
    if kind in ("qa", "codex"):
        return os.environ.get("HERMES_DELEGATE_CODEX_BIN", str(CODEX_BIN))
    return os.environ.get("HERMES_DELEGATE_CLAUDE_BIN", str(CLAUDE_BIN))


def token_file():
    return os.environ.get("HERMES_DELEGATE_CLAUDE_TOKEN_FILE", str(CLAUDE_TOKEN_FILE))


def check_token_file(path):
    """stat only: the token value is never read by the entry."""
    try:
        info = os.lstat(path)
    except OSError:
        raise Fail("claude OAuth token file is missing: {} (needed for --via direct)".format(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise Fail("claude OAuth token file is not a regular file: " + path)
    if info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
        raise Fail("claude OAuth token file must be mode 0600 and owned by you: {} (mode {:o})".format(
            path, info.st_mode & 0o777))
    if info.st_size == 0:
        raise Fail("claude OAuth token file is empty: " + path)


def check_launcher(cmux, binary=None):
    binary = binary or str(CLAUDE_BIN)
    if not os.access(cmux, os.X_OK):
        raise Fail("cmux is not executable: " + cmux)
    if not os.access(binary, os.X_OK):
        raise Fail("{} CLI is missing: {}".format(os.path.basename(binary), binary))
    ping = run([cmux, "ping"], timeout=15)
    if ping.returncode:
        raise Fail("cmux is not reachable (cmux ping exit {}): {}".format(
            ping.returncode, last_line(ping.stderr or ping.stdout)))


def check_binary(binary):
    if not os.access(binary, os.X_OK):
        raise Fail("{} CLI is missing: {}".format(os.path.basename(binary), binary))


def zsh_command(script_path):
    # zsh -i loads .zshrc, where the codex-lb provider key lives; -l alone would skip it.
    return "/bin/zsh -ic {}".format(shlex.quote("exec /bin/zsh " + shlex.quote(script_path)))


def launch_direct(run_dir, script_path, workdir):
    """Detach inner.zsh into its own session and return at once; the watcher line follows the rc."""
    log = run_dir + "/launcher.log"
    command = "{} < /dev/null >> {} 2>&1 &!".format(zsh_command(script_path), shlex.quote(log))
    try:
        result = subprocess.run(["/bin/zsh", "-fc", command], cwd=workdir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Fail("direct launch failed: {}".format(exc))
    if result.returncode:
        raise Fail("direct launch failed (exit {}): {}".format(result.returncode, last_line(result.stderr)))


def update_meta(run_dir, **values):
    path = Path(run_dir, "meta.json")
    meta = json.loads(path.read_text())
    meta.update(values)
    tmp = path.with_name("meta.json.tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    os.replace(str(tmp), str(path))


def ledger_status(review_gate, item):
    result = run([review_gate, "status", "--item", item, "--json"], timeout=60)
    if result.returncode:
        raise Fail((result.stderr.strip() or "review-gate status exit {}".format(result.returncode))
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
            return ("Fallback already used in round {}; the owner must resolve this round or approve another "
                    "with --owner-approval.".format(current_round))
        if latest is None or latest["verdict"] != "UNREVIEWED":
            return FALLBACK_LATEST_REFUSAL
        if reviewer and latest.get("reviewer") == reviewer:
            return "--fallback needs a different reviewer than the failed ticket's ({}).".format(reviewer)
        return None
    if any(t["status"] != "closed" or t["verdict"] is None for t in tickets):
        return PENDING_REFUSAL
    if (latest is not None and latest["verdict"] == "UNREVIEWED" and not owner_approval
            and not any(t["verdict"] == "BLOCK" or t.get("fallback") for t in current)):
        return ("Round {} ended UNREVIEWED; retry it in the same round with hermes-delegate review --fallback "
                "--reviewer <the other CLI> --commit <the failed ticket's commit>. A new round instead needs "
                "--owner-approval.".format(current_round))
    next_round = state["rounds_used"] + 1
    if (next_round > 2 or state["item"]["status"] in ("cap-reached", "stopped-unreviewed")) and not owner_approval:
        return ("Item {} stopped at round {}; the owner must supply --owner-approval <Discord message ID> "
                "for a further round (ask the user in Discord, then pass the ID of their approval message "
                "with hermes-delegate review --owner-approval ID).".format(state["item"]["id"], next_round - 1))
    if owner_approval and any(t.get("owner_approval") == owner_approval for t in tickets):
        return ("Owner approval {} was already used; each further round needs a new owner approval "
                "message ID.".format(owner_approval))
    return None


def watcher_line(run_dir, timeout, stall_minutes=None, label="watcher"):
    command = "{} wait {} --timeout {}".format(HD_BIN, run_dir, timeout)
    if stall_minutes is not None:
        command += " --stall-minutes {:g}".format(stall_minutes)
    return ("{}: terminal(command=\"{}\", background=true, notify_on_complete=true, "
            "continue_on_complete=true, persist_on_release=true)".format(label, command))


def rewatch_timeout(timeout, begin):
    # Floor so a re-arm near the old deadline still covers the worker's own gtimeout teardown.
    return max(int(math.ceil(timeout - (time.monotonic() - begin))), REWATCH_MIN_SECONDS)


def print_launch(out, run_dir, ticket, launched, timeout, kind="review", via="cmux"):
    print("run_dir=" + run_dir, file=out)
    print("rc={}/{}.rc".format(run_dir, kind), file=out)
    if ticket:
        print("ticket=" + ticket, file=out)
    print("via=" + via, file=out)
    print("launched=" + ("yes" if launched else "no"), file=out)
    print(watcher_line(run_dir, timeout + 900), file=out)


WORKSPACE_REF = re.compile(r"\bworkspace:[0-9A-Za-z-]+")


def launch_workspace(launch, ticket=None, kind="review", run_dir="RUN_DIR"):
    result = run(launch, timeout=30, env=dict(os.environ, CMUX_QUIET="1"))
    if result.returncode:
        detail = "cmux new-workspace failed (exit {}): {}".format(
            result.returncode, last_line(result.stderr or result.stdout))
        if ticket:
            raise Fail("{}; ticket {} is still open and unused: retry with hermes-delegate review --launch-run "
                       "RUN_DIR, or report it and close it as UNREVIEWED".format(detail, ticket))
        raise Fail("{}; nothing ran: retry with hermes-delegate {} --launch-run {}".format(detail, kind, run_dir))
    found = WORKSPACE_REF.findall(result.stdout)
    return found[-1] if found else None


def start_run(*, via, launch, run_dir, script_path, workdir, ticket=None, kind="review"):
    if via == "direct":
        try:
            launch_direct(run_dir, script_path, workdir)
        except Fail as exc:
            if ticket:
                raise Fail("{}; ticket {} is still open and unused: retry with hermes-delegate review "
                           "--launch-run {}".format(exc, ticket, run_dir))
            raise Fail("{}; nothing ran: retry with hermes-delegate {} --launch-run {}".format(exc, kind, run_dir))
        return
    workspace = launch_workspace(launch, ticket, kind, run_dir)
    if workspace:
        update_meta(run_dir, cmux_workspace=workspace)


def review_template(run_dir, commit, base):
    what = "git diff --binary {} {}".format(base[:12], commit[:12]) if base else "git show {}".format(commit[:12])
    return """

---
Review rules (added by hermes-delegate; they override anything above that conflicts):
- The change under review is in {run}/diff.patch ({what}); the commit log and file stats are in
  {run}/log.txt. Read both first. The working tree is a clean checkout of {commit}.
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
{approval}
""".format(run=run_dir, what=what, commit=commit, approval=APPROVAL_LINE)


def write_review_inputs(run_dir, workdir, commit, base):
    if base:
        diff = ["diff", "--binary", base, commit]
        log = ["log", "--stat", "--format=fuller", "{}..{}".format(base, commit)]
    else:
        diff = ["show", commit]
        log = ["show", "--stat", "--format=fuller", commit]
    for name, args in (("diff.patch", diff), ("log.txt", log)):
        result = subprocess.run(["git", "-C", workdir] + args, capture_output=True)
        if result.returncode:
            raise Fail("git {} failed while writing {}".format(" ".join(args[:2]), name))
        Path(run_dir, name).write_bytes(result.stdout)


MCP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def codex_mcp_off():
    """Codex merges `-c mcp_servers={}` into the user table instead of replacing it, so each
    configured server is disabled by name; a failed listing only means MCP stays as configured."""
    env = dict(os.environ, ASDF_NODEJS_VERSION="22.19.0",
               PATH="{}:{}".format(CODEX_NODE_BIN, os.environ.get("PATH", "")))
    try:
        result = subprocess.run([worker_binary("codex"), "mcp", "list", "--json"], capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=10, env=env)
        servers = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if not isinstance(servers, list):
        return []
    names = sorted({s.get("name") for s in servers if isinstance(s, dict) and s.get("enabled")
                    and isinstance(s.get("name"), str) and MCP_NAME.fullmatch(s["name"])})
    return [arg for name in names for arg in ("-c", "mcp_servers.{}.enabled=false".format(name))]


def reviewer_command(reviewer, run_dir, workdir):
    policy = REVIEW_POLICY[reviewer]
    if reviewer == "codex":
        return [worker_binary("codex"), "exec", "--ephemeral", "-s", "read-only", "-m", policy["model"],
                "-c", 'model_reasoning_effort="{}"'.format(policy["effort"]), *codex_mcp_off(),
                "-C", workdir, "--json",
                "-o", run_dir + "/review.final.md", "-"]
    return [worker_binary("claude"), "-p", "--model", policy["model"], "--effort", policy["effort"],
            *CLAUDE_READ_ONLY, "--add-dir", run_dir]


CLOSE_GUIDANCE = ("hermes-delegate: the verdict is not recorded automatically; when the watcher ends, reproduce "
                  "each finding in review.result.md yourself, then record the verdict with review-gate close "
                  "(PASS: --blockers 0 or omitted; BLOCK: --blockers N>=1; UNREVIEWED: --reason). The user only "
                  "gives --owner-approval for round 3+: ask in Discord, then pass the approval message ID with "
                  "hermes-delegate review --owner-approval ID")


def check_launch_tools(via, cmux, binary):
    if via == "cmux":
        check_launcher(cmux, binary)
    else:
        check_binary(binary)


def cmd_review(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    if argv and argv[0].split("=", 1)[0] == "--launch-run":
        return cmd_launch_run(argv, out, err)
    args = review_parser().parse_args(argv)
    via = args.via or REVIEW_POLICY[args.reviewer]["via"]
    label = args.label or args.item
    if not LABEL.fullmatch(label):
        raise Fail("--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}", 2)
    if not 60 <= args.timeout <= 86400:
        raise Fail("--timeout must be between 60 and 86400 seconds", 2)
    if args.owner_approval is not None and not OWNER_APPROVAL_ID.fullmatch(args.owner_approval):
        raise Fail("--owner-approval must be a Discord message ID (15-22 digits)", 2)
    workdir = canonical(args.workdir, "--workdir", kind="dir")
    prompt = canonical(args.prompt, "--prompt", kind="file")
    if os.path.getsize(prompt) == 0:
        raise Fail("--prompt is empty")
    for name in ("commit", "base"):
        value = getattr(args, name)
        if value is not None and not re.fullmatch(r"[0-9a-f]{7,40}", value):
            raise Fail("--{} must be a 7-40 char lowercase hex SHA".format(name), 2)
    full = git_value(workdir, "rev-parse", "--verify", "--quiet", args.commit + "^{commit}")
    head = git_value(workdir, "rev-parse", "HEAD")
    if head != full:
        raise Fail("workdir HEAD {} is not --commit {}; check out the reviewed commit first".format(
            head[:12], full[:12]))
    dirty = dirty_entries(workdir)
    if dirty:
        raise Fail("workdir has uncommitted or untracked changes ({} entries, first: {}); the reviewer reads "
                   "the working tree, so review only a clean checkout of --commit".format(
                       len(dirty.splitlines()), dirty.splitlines()[0].strip()))
    tree = git_value(workdir, "rev-parse", full + "^{tree}")
    diff_sha, base = None, None
    if args.base:
        base = git_value(workdir, "rev-parse", "--verify", "--quiet", args.base + "^{commit}")
        diff = subprocess.run(["git", "-C", workdir, "diff", "--binary", base, full], capture_output=True)
        if diff.returncode:
            raise Fail("git diff {} {} failed".format(base[:12], full[:12]))
        diff_sha = hashlib.sha256(diff.stdout).hexdigest()
    review_gate, cmux = tools()
    if not os.access(review_gate, os.X_OK):
        raise Fail("review-gate is not executable: " + review_gate)
    binary = worker_binary(args.reviewer)
    use_token = args.reviewer == "claude" and via == "direct"
    if use_token:
        check_token_file(token_file())
    if args.check:
        check_launch_tools(via, cmux, binary)
        refusal = start_refusal(ledger_status(review_gate, args.item), args.owner_approval, args.fallback,
                                args.reviewer)
        if refusal:
            raise Fail("review-gate start would refuse: " + refusal, 2)
        print("check=ok item={} reviewer={} via={} commit={} tree={}{} (no ticket issued; auth not checked, "
              "use hermes-delegate smoke {} --via {})".format(
                  args.item, args.reviewer, via, full[:12], tree[:12],
                  " diff_sha256=" + diff_sha if diff_sha else "", args.reviewer, via), file=out)
        return 0
    if not args.no_launch:
        check_launch_tools(via, cmux, binary)
    root = review_state_root()
    run_dir = "{}/{}-{}-{}".format(root, label, time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))
    os.mkdir(run_dir, 0o700)
    approval = ["--owner-approval", args.owner_approval] if args.owner_approval else []
    approval += ["--fallback"] if args.fallback else []
    started = run([review_gate, "start", "--item", args.item, "--reviewer", args.reviewer,
                   "--commit", args.commit] + approval, timeout=60)
    if started.returncode:
        shutil.rmtree(run_dir, ignore_errors=True)
        err.write(started.stderr if started.stderr.endswith("\n") or not started.stderr
                  else started.stderr + "\n")
        if not started.stderr.strip():
            print("hermes-delegate: review-gate start exit {}".format(started.returncode), file=err)
        return started.returncode or 1
    ticket = started.stdout.strip()
    try:
        if not re.fullmatch(r"[0-9a-f]{32}", ticket):
            raise Fail("review-gate start returned an unexpected ticket: {!r}".format(ticket[:80]))
        policy = REVIEW_POLICY[args.reviewer]
        reviewer_argv = reviewer_command(args.reviewer, run_dir, workdir)
        write_review_inputs(run_dir, workdir, full, base)
        Path(run_dir, "prompt.md").write_bytes(Path(prompt).read_bytes()
                                               + review_template(run_dir, full, base).encode())
        script = inner_script(run_dir=run_dir, workdir=workdir, ticket=ticket, review_gate=review_gate,
                              timeout=args.timeout, reviewer_argv=reviewer_argv,
                              gate_db=os.environ.get("REVIEW_GATE_DB"), commit=full,
                              git=shutil.which("git") or "/usr/bin/git", reviewer=args.reviewer,
                              token_file=token_file() if use_token else None)
        script_path = run_dir + "/inner.zsh"
        with open(script_path, "w") as stream:
            stream.write(script)
        os.chmod(script_path, 0o700)
        with open(run_dir + "/ticket", "w") as stream:
            stream.write(ticket + "\n")
        with open(run_dir + "/meta.json", "w") as stream:
            json.dump({"kind": "review", "item": args.item, "reviewer": args.reviewer, "commit": full,
                       "tree": tree, "base": args.base, "diff_sha256": diff_sha, "workdir": workdir,
                       "prompt_source": prompt, "ticket": ticket, "model": policy["model"],
                       "effort": policy["effort"], "timeout": args.timeout, "label": label, "via": via,
                       "owner_approval": args.owner_approval, "fallback": args.fallback,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, stream, indent=1)
        launch = [cmux, "new-workspace", "--name", "review-" + label, "--cwd", workdir, "--focus", "false",
                  "--command", zsh_command(script_path)]
        with open(run_dir + "/launch.json", "w") as stream:
            json.dump(launch, stream)
        if not args.no_launch:
            start_run(via=via, launch=launch, run_dir=run_dir, script_path=script_path, workdir=workdir,
                      ticket=ticket)
    except (Fail, OSError) as exc:
        message = str(exc)
        if ticket not in message:
            message += ("; ticket {} stays open (run dir {}); report it and close it as UNREVIEWED with the "
                        "reason".format(ticket, run_dir))
        raise Fail(message, getattr(exc, "code", 1))
    print_launch(out, run_dir, ticket, not args.no_launch, args.timeout, via=via)
    print(CLOSE_GUIDANCE, file=err)
    return 0


def cmd_launch_run(argv, out, err, kind="review"):
    p = Parser(prog="hermes-delegate {} --launch-run".format(kind), allow_abbrev=False)
    p.add_argument("--launch-run", dest="run_dir", required=True)
    args = p.parse_args(argv)
    run_dir = canonical(args.run_dir, "--launch-run", kind="dir")
    if os.path.dirname(run_dir) != state_root(kind):
        raise Fail("--launch-run must be a run dir created by hermes-delegate " + kind)
    try:
        meta = json.loads(Path(run_dir, "meta.json").read_text())
        launch = json.loads(Path(run_dir, "launch.json").read_text())
    except (OSError, ValueError) as exc:
        raise Fail("run dir is incomplete: {}".format(exc))
    if (os.path.exists(run_dir + "/lock") or os.path.exists(run_dir + "/started_at")
            or os.path.exists("{}/{}.rc".format(run_dir, kind))):
        if kind == "review":
            raise Fail("this run already started (lock, started_at or rc present); a ticket runs once, so "
                       "record its verdict and start a new review with a new ticket")
        raise Fail("this run already started (lock, started_at or rc present); start a new run instead")
    review_gate, cmux = tools()
    ticket = meta.get("ticket")
    via = meta.get("via", "cmux")
    if kind == "review":
        state = ledger_status(review_gate, meta["item"])
        row = next((t for t in state["tickets"] if t["id"] == ticket), None)
        if row is None or row["status"] != "open":
            raise Fail("ticket {} is not open in the ledger (status {})".format(
                ticket, row["status"] if row else "missing"), 2)
        binary = worker_binary(meta.get("reviewer", "claude"))
        if meta.get("reviewer", "claude") == "claude" and via == "direct":
            check_token_file(token_file())
    else:
        binary = worker_binary(kind)
        if via == "direct":
            check_token_file(token_file())
    head = meta["commit"] if kind == "review" else meta["head"]
    if git_value(meta["workdir"], "rev-parse", "HEAD") != head or dirty_entries(meta["workdir"]):
        raise Fail("workdir is no longer a clean checkout of {}".format(head[:12]))
    if launch[0] != cmux:
        launch = [cmux] + launch[1:]
    check_launch_tools(via, cmux, binary)
    start_run(via=via, launch=launch, run_dir=run_dir, script_path=run_dir + "/inner.zsh",
              workdir=meta["workdir"], ticket=ticket, kind=kind)
    print_launch(out, run_dir, ticket, True, int(meta["timeout"]), kind, via)
    return 0


def worker_parser(kind):
    if kind == "claude":
        description = ("Launch a write-capable Claude Code implementation run (cmux workspace by default, "
                       "--via direct for a detached run with the OAuth token file).")
    else:
        description = ("Launch a device/simulator QA run (Codex {model}/{effort}, sandbox {sandbox}; "
                       "fixed) in a new cmux workspace.".format(**QA_POLICY))
    p = Parser(prog="hermes-delegate " + kind, allow_abbrev=False, description=description)
    p.add_argument("--spec", required=True)
    p.add_argument("--workdir", required=True)
    if kind == "claude":
        p.add_argument("--class", dest="task_class", required=True, choices=sorted(CLAUDE_IMPL_POLICY))
        p.add_argument("--advisor", choices=CLAUDE_ADVISORS,
                       help="opt-in per run: consult Opus as advisor (extra Opus tokens, unverified benefit)")
        p.add_argument("--via", choices=VIA, default="cmux", help="cmux (default) or direct")
    p.add_argument("--timeout", type=int, required=True, help="worker timeout seconds (60-86400)")
    p.add_argument("--label")
    p.add_argument("--no-launch", action="store_true",
                   help="prepare the run dir and skip the launch; launch later with --launch-run RUN_DIR")
    return p


def reject_worker_overrides(kind, argv):
    fixed = WORKER_OVERRIDES + (("--class", "--task-class", "--advisor", "--via") if kind == "qa" else ())
    for word in argv:
        name = word.split("=", 1)[0]
        if name in fixed:
            if kind == "qa":
                raise Fail("{} is not accepted: device QA is fixed at Codex {model}/{effort} with sandbox "
                           "{sandbox} in cmux (user pin)".format(name, **QA_POLICY), 2)
            raise Fail("{} is not accepted: model/effort come only from --class ({}) and the permission "
                       "flags are fixed".format(name, ", ".join(sorted(CLAUDE_IMPL_POLICY))), 2)


def worker_argv(kind, args, run_dir, workdir, binary):
    if kind == "claude":
        policy = CLAUDE_IMPL_POLICY[args.task_class]
        argv = [binary, "-p", "--model", policy["model"], "--effort", policy["effort"], *CLAUDE_WRITE]
        if args.advisor:
            argv += ["--advisor", args.advisor, "--settings", ADVISOR_SETTINGS]
        return argv, policy
    return [binary, "exec", "--ephemeral", "-m", QA_POLICY["model"],
            "-c", 'model_reasoning_effort="{}"'.format(QA_POLICY["effort"]),
            "--sandbox", QA_POLICY["sandbox"], "-C", workdir, "--json",
            "-o", run_dir + "/qa.final.md", "-"], QA_POLICY


def worker_script(*, kind, run_dir, workdir, head, git, argv, timeout, token_file=None):
    q = shlex.quote
    prefix = "{}/{}".format(run_dir, kind)
    lines = script_header(run_dir, kind, "the worker process may still be alive", group=True)
    if kind == "qa":
        lines += codex_env()
    if token_file:
        lines += token_checks(token_file)
    lines += clean_checks(run_dir, workdir, git, head, ("recorded HEAD", "ran"))
    lines += background_target("{} -k 60 {} {}".format(q(str(GTIMEOUT)), timeout, shlex.join(argv)),
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
        return cmd_launch_run(argv, out, err, kind)
    reject_worker_overrides(kind, argv)
    args = worker_parser(kind).parse_args(argv)
    via = getattr(args, "via", "cmux")
    if kind == "claude" and via == "direct" and not CLAUDE_IMPL_DIRECT:
        raise Fail(CLAUDE_IMPL_DIRECT_REFUSAL, 2)
    if not 60 <= args.timeout <= 86400:
        raise Fail("--timeout must be between 60 and 86400 seconds", 2)
    if kind == "claude" and args.advisor and CLAUDE_IMPL_POLICY[args.task_class]["model"] == args.advisor:
        raise Fail("--advisor {} is pointless: class {} already runs on {}".format(
            args.advisor, args.task_class, args.advisor), 2)
    workdir = canonical(args.workdir, "--workdir", kind="dir")
    label = args.label or os.path.basename(workdir)
    if not LABEL.fullmatch(label):
        if args.label:
            raise Fail("--label must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}", 2)
        label = kind
    spec = canonical(args.spec, "--spec", kind="file")
    spec_bytes = require_spec_gate(spec, kind, workdir)
    check_worktree_root(workdir)
    dirty = dirty_entries(workdir)
    if dirty:
        raise Fail("workdir has uncommitted or untracked changes ({} entries, first: {}); start the worker from "
                   "a clean worktree so its diff is attributable (keep the SPEC outside it or gitignored)"
                   .format(len(dirty.splitlines()), dirty.splitlines()[0].strip()))
    head = git_value(workdir, "rev-parse", "HEAD")
    binary = worker_binary(kind)
    for path, what in ((binary, kind + " worker CLI"), (str(GTIMEOUT), "gtimeout")):
        if not os.access(path, os.X_OK):
            raise Fail("{} is not executable: {}".format(what, path))
    if via == "direct":
        check_token_file(token_file())
    cmux = tools()[1]
    if not args.no_launch and via == "cmux":
        check_launcher(cmux, binary)
    root = state_root(kind)
    run_dir = "{}/{}-{}-{}".format(root, label, time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))
    os.mkdir(run_dir, 0o700)
    try:
        argv_out, policy = worker_argv(kind, args, run_dir, workdir, binary)
        Path(run_dir, "spec.md").write_bytes(spec_bytes)
        script = worker_script(kind=kind, run_dir=run_dir, workdir=workdir, head=head,
                               git=shutil.which("git") or "/usr/bin/git", argv=argv_out, timeout=args.timeout,
                               token_file=token_file() if via == "direct" else None)
        script_path = run_dir + "/inner.zsh"
        with open(script_path, "w") as stream:
            stream.write(script)
        os.chmod(script_path, 0o700)
        meta = {"kind": kind, "workdir": workdir, "head": head, "spec_source": spec,
                "model": policy["model"], "effort": policy["effort"], "timeout": args.timeout, "label": label,
                "via": via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if kind == "claude":
            meta.update({"class": args.task_class, "advisor": args.advisor})
        else:
            meta["sandbox"] = policy["sandbox"]
        with open(run_dir + "/meta.json", "w") as stream:
            json.dump(meta, stream, indent=1)
        launch = [cmux, "new-workspace", "--name", "{}-{}".format(kind, label), "--cwd", workdir,
                  "--focus", "false", "--command", zsh_command(script_path)]
        with open(run_dir + "/launch.json", "w") as stream:
            json.dump(launch, stream)
    except OSError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    if not args.no_launch:
        start_run(via=via, launch=launch, run_dir=run_dir, script_path=script_path, workdir=workdir, kind=kind)
    if kind == "claude":
        print("hermes-delegate: class={} model={} effort={} advisor={} via={}; Claude edits the worktree "
              "(acceptEdits, Bash allowed); review its diff in a separate session with another CLI before "
              "accepting".format(args.task_class, policy["model"], policy["effort"], args.advisor or "off", via),
              file=err)
    else:
        print("hermes-delegate: QA model={model} effort={effort} sandbox={sandbox} (fixed user pin); verify the "
              "screenshots and logs yourself before reporting".format(**QA_POLICY), file=err)
    print_launch(out, run_dir, None, not args.no_launch, args.timeout, kind, via)
    return 0


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
        minutes = STALL_DEFAULT_MINUTES[kind]
    if not math.isfinite(minutes) or minutes < 0:
        raise Fail("--stall-minutes must be 0 (off) or a positive number of minutes", 2)
    return minutes * 60.0


def stall_count(run_dir):
    try:
        text = Path(run_dir, "stall_count").read_text().strip()
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
        until = min(deadline, quiet_since + stall, mono + STALL_SAMPLE_SECONDS)
        if wait_until(ready, run_dir, until):
            return None
        now, now_wall = time.monotonic(), time.time()
        excess = (now - mono) - max(until - mono, 0.0)
        if excess > SLEEP_GAP_SECONDS:
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
    text = TOKEN_PATTERN.sub(TOKEN_PREFIX + "[REDACTED]", " ".join(str(text).split()))
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
                status, "" if code is None else ", exit {}".format(code), detail)
        return name in ("mcp_tool_call", "web_search", "file_change"), "{}: {}".format(name, detail)
    if kind in ("error", "turn.failed"):
        return False, "{}: {}".format(kind, item.get("message") or item.get("error"))
    return False, "{}{}".format(kind, " " + str(item.get("subtype")) if item.get("subtype") else "")


def stall_summary(output):
    events, last_raw = tail_events(output)
    if not events:
        return ["last_event=" + (clip(last_raw) if last_raw else "none ({} is empty or missing)".format(
            os.path.basename(output)))]
    described = [describe_event(item) for item in events]
    calls = [text for is_call, text in described if is_call]
    lines = []
    if calls:
        recent = calls[-10:]
        lines.append("last_tool_call={} (same call {}x in the last {} tool calls)".format(
            clip(calls[-1]), recent.count(calls[-1]), len(recent)))
    lines.append("last_event=" + clip(described[-1][1]))
    return lines


def report_stall(out, kind, output, stalled, last_wall, run_dir, timeout, stall_minutes):
    if last_wall is not None:
        growth = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(last_wall))
    else:
        state = stream_state(output)
        growth = ("{} ({} mtime; no growth seen by this wait)".format(
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(state[1] / 1e9)), kind + ".out")
            if state else "none ({}.out not written yet)".format(kind))
    print("status=stalled", file=out)
    print("last_growth={}".format(growth), file=out)
    print("stalled_minutes={:.1f} ({}.out has not grown for this long while this wait watched)".format(
        stalled / 60.0, kind), file=out)
    for line in stall_summary(output):
        print(line, file=out)
    count = stall_count(run_dir) + 1
    try:
        Path(run_dir, "stall_count").write_text("{}\n".format(count))
    except OSError:
        pass
    print("stall_count={} of {} (each re-arm uses one continuation of the approval's chain cap)".format(
        count, STALL_REARM_LIMIT), file=out)
    if count < STALL_REARM_LIMIT:
        print(watcher_line(run_dir, timeout, stall_minutes), file=out)
    else:
        print("stall_limit_reached: re-arm only with watcher_no_stall: (later waits default to --stall-minutes 0)",
              file=out)
    print(watcher_line(run_dir, timeout, 0, label="watcher_no_stall"), file=out)
    print("the run keeps going; re-run hermes-delegate wait RUN_DIR to keep watching (optionally --stall-minutes N) "
          "or stop it with hermes-delegate cancel RUN_DIR", file=out)


def json_lines(output):
    try:
        lines = Path(output).read_text(errors="replace").splitlines()
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
        return ["result=missing (no stream-json result event in {}.out)".format(kind)]
    text = final.get("result") if isinstance(final.get("result"), str) else ""
    result = "{}/{}.result.md".format(run_dir, kind)
    Path(result).write_text(text)
    summary = ["result={} turns={} tool_uses={} ({}) is_error={} permission_denials={}".format(
        result, final.get("num_turns"), tool_uses,
        ",".join("{}:{}".format(k, v) for k, v in sorted(names.items())) or "none",
        final.get("is_error"), len(final.get("permission_denials") or [])),
        ("verdict_line=" if kind == "review" else "last_line=") + last_line(text)]
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
    final = "{}/{}.final.md".format(run_dir, kind)
    text = Path(final).read_text(errors="replace") if os.path.exists(final) else ""
    summary = ["result={} items=({}) usage={}".format(
        final if text else "missing", ",".join("{}:{}".format(k, v) for k, v in sorted(items.items())) or "none",
        json.dumps(usage) if usage else "none"), ("verdict_line=" if kind == "review" else "last_line=")
        + last_line(text)]
    if kind == "review" and text:
        Path(run_dir, "review.result.md").write_text(text)
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
        if TOKEN_PREFIX.encode() not in data:
            continue
        text = data.decode("utf-8", errors="surrogateescape")
        path.write_bytes(TOKEN_PATTERN.sub(TOKEN_PREFIX + "[REDACTED]", text).encode(
            "utf-8", errors="surrogateescape"))
        touched.append(path.name)
    return touched


def close_workspace(meta, out):
    workspace = meta.get("cmux_workspace")
    if meta.get("via", "cmux") != "cmux" or not workspace:
        return
    cmux = tools()[1]
    detail = "cmux not executable"
    # inner.zsh writes the rc just before it exits; cmux refuses to close a workspace whose process is
    # still running, and --force could kill something else, so retry briefly instead.
    for attempt in range(6):
        if not os.access(cmux, os.X_OK):
            break
        result = run([cmux, "close-workspace", "--workspace", workspace], timeout=15,
                     env=dict(os.environ, CMUX_QUIET="1"))
        if result.returncode == 0:
            print("cmux_workspace={} closed".format(workspace), file=out)
            return
        detail = last_line(result.stderr or result.stdout)
        if "not_found" in detail:
            print("cmux_workspace={} already gone".format(workspace), file=out)
            return
        time.sleep(1 + attempt)
    print("WARNING: cmux_workspace={} left open: {}".format(workspace, detail), file=out)


def worktree_summary(run_dir, meta):
    try:
        after = Path(run_dir, "head_after").read_text().strip()
        status = Path(run_dir, "status_after").read_text().splitlines()
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
        pid = int(Path(run_dir, "inner.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    result = run(["/bin/ps", "-o", "command=", "-p", str(pid)], timeout=10)
    script = os.path.realpath(os.path.join(run_dir, "inner.zsh"))
    words = result.stdout.split() if result.returncode == 0 else []
    return pid if any(w.endswith("inner.zsh") and os.path.realpath(w) == script for w in words) else None


def cancel_run(run_dir, kind, grace):
    """SIGTERM this run's inner.zsh (its traps stop the worker group or review-gate and record the rc).

    A run whose inner.zsh has not taken the lock yet is fenced off by taking the lock here, so a late
    launcher exits without starting anything. Returns the lines to print and whether an rc exists."""
    rc_path = "{}/{}.rc".format(run_dir, kind)
    if os.path.exists(rc_path):
        return ["cancel: {} already finished".format(kind)], True
    lines = []
    try:
        os.mkdir(run_dir + "/lock")
    except FileExistsError:
        pass
    else:
        tmp = rc_path + ".tmp"
        with open("{}/{}.err".format(run_dir, kind), "a") as handle:
            handle.write("hermes-delegate cancel: cancelled before inner.zsh started; nothing ran\n")
        Path(tmp).write_text("143\n")
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
            return ["cancel: {} finished meanwhile".format(kind)], True
        return ["cancel: lock held but no live inner.zsh for this run (inner.pid missing or stale)"], False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        lines.append("cancel: sent SIGTERM to inner.zsh pid {}".format(pid))
    found = wait_until(lambda: os.path.exists(rc_path), run_dir, deadline)
    if not found:
        lines.append("cancel: no {}.rc within {}s; inner.zsh pid {} may still be stopping".format(kind, grace, pid))
    return lines, found


WATCHER_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


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
    p = Parser(prog="hermes-delegate cancel", allow_abbrev=False)
    p.add_argument("run_dir")
    p.add_argument("--grace", type=int, default=30, help="seconds to wait for the rc after SIGTERM")
    args = p.parse_args(argv)
    run_dir = canonical(args.run_dir, "run dir", kind="dir")
    kind = run_kind(run_dir)
    lines, found = cancel_run(run_dir, kind, max(args.grace, 1))
    for line in lines:
        print(line, file=out)
    if not found:
        print("status=no_rc", file=out)
        return EXIT_NO_RC
    text = Path(run_dir, kind + ".rc").read_text().strip()
    print("status=finished", file=out)
    print("{} rc={}".format(kind, text), file=out)
    return min(int(text), 255) if re.fullmatch(r"[0-9]{1,3}", text) else 1


def run_kind(run_dir):
    try:
        meta = json.loads(Path(run_dir, "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    kind = meta.get("kind", "review")
    if kind not in ("review", "claude", "qa"):
        raise Fail("unknown run kind in meta.json: {!r}".format(kind))
    return kind


STATUS_KINDS = ("review", "claude", "qa", "codex")
EXIT_STATUS_UNKNOWN = 3
STATUS_START_GRACE_SECONDS = 30
CODEX_EMPTY_STALE_SECONDS = 300
HINT_RUNNING = "completion arrives via the watcher/gateway notification; do not poll"
HINT_FINISHED = "run the watcher line or read the result"
HINT_CODEX_FINISHED = "the gateway reports completion; read status.json in task_dir"
HINT_CODEX_NOT_STARTED = "the gateway has not started the printed argv yet (normal for a few seconds)"


def status_target(value):
    if value and len(value) > 1:
        value = value.rstrip("/") or "/"
    try:
        path = canonical(value, "status path", kind="dir")
    except Fail as exc:
        raise Fail(str(exc), 2)
    root = os.environ.get("HERMES_DELEGATE_STATE_ROOT", str(DEFAULT_STATE_ROOT)).rstrip("/")
    parent = os.path.dirname(path)
    kind = os.path.basename(parent)
    if os.path.dirname(parent) != root or kind not in STATUS_KINDS:
        raise Fail("status path must be {}/{{{}}}/<name>: {}".format(root, ",".join(STATUS_KINDS), path), 2)
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
    result = run(["/bin/ps", "-axww", "-o", "pid=,command="], timeout=10)
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        words = line.split()
        if len(words) < 3 or not any(os.path.basename(w) == "run_codex_task.py" for w in words[1:]):
            continue
        pairs = zip(words[1:], words[2:])
        if any((a == "--output-dir" and b == output_dir) for a, b in pairs) or \
                "--output-dir=" + output_dir in words:
            if words[0].isdigit() and int(words[0]) != os.getpid():
                return int(words[0])
    return None


def run_dir_status(path, kind, now):
    fields = [("kind", kind)]
    try:
        meta_kind = run_kind(path) if os.path.exists(path + "/meta.json") else kind
    except Fail:
        meta_kind = "invalid"
    rc_path = "{}/{}.rc".format(path, kind)
    started = read_small(path + "/started_at")
    ended = read_small(path + "/ended_at")
    extra = []
    if started:
        extra.append(("started_at", started))
    if ended:
        extra.append(("ended_at", ended))
    output_age = age_seconds("{}/{}.out".format(path, kind), now)
    if output_age is not None:
        extra.append(("last_output_age_s", output_age))
    if os.path.exists(path + "/stall_count"):
        extra.append(("stall_count", stall_count(path)))
    if meta_kind != kind:
        return fields + [("status", "unknown")] + extra, \
            "meta.json kind {} does not match the {} state dir".format(meta_kind, kind), EXIT_STATUS_UNKNOWN
    if os.path.exists(rc_path):
        text = read_small(rc_path) or ""
        if not re.fullmatch(r"[0-9]{1,3}", text):
            return fields + [("status", "unknown")] + extra, \
                "malformed {}.rc: {}".format(kind, text[:40]), EXIT_STATUS_UNKNOWN
        return fields + [("status", "finished"), ("rc", int(text))] + extra, HINT_FINISHED, 0
    lock = path + "/lock"
    if not os.path.exists(lock) and not started:
        return fields + [("status", "not_started")] + extra, (
            "inner.zsh has not started yet (normal for a few seconds after launched=yes; after launched=no "
            "run hermes-delegate {} --launch-run RUN_DIR)".format(kind)), 0
    pid = inner_pid(path)
    if pid:
        return fields + [("status", "running"), ("pid", pid)] + extra, HINT_RUNNING, 0
    lock_age = age_seconds(lock, now)
    if not os.path.exists(path + "/inner.pid") and lock_age is not None and lock_age < STATUS_START_GRACE_SECONDS:
        return fields + [("status", "running")] + extra, HINT_RUNNING, 0
    return fields + [("status", "unknown")] + extra, (
        "no live inner.zsh and no {}.rc (killed without its trap?); read {}.err and report it".format(kind, kind)), \
        EXIT_STATUS_UNKNOWN


def codex_status(path, now):
    fields = [("kind", "codex")]
    tasks = sorted((name for name in os.listdir(path) if name.startswith("codex-task-")
                    and os.path.isdir(os.path.join(path, name)) and not os.path.islink(os.path.join(path, name))),
                   key=lambda name: os.stat(os.path.join(path, name)).st_mtime)
    if not tasks:
        age = age_seconds(path, now)
        hint = HINT_CODEX_NOT_STARTED
        if age is not None and age > CODEX_EMPTY_STALE_SECONDS:
            hint += "; empty for {}s, so the launch probably never happened".format(age)
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
            receipt = json.loads(Path(status_path).read_text())
        except (OSError, ValueError):
            receipt = None
        if not isinstance(receipt, dict) or type(receipt.get("exit_code")) is not int:
            return fields + [("status", "unknown")] + extra, "status.json is unreadable or malformed", \
                EXIT_STATUS_UNKNOWN
        at = [key for key, _ in extra].index("started_at") + 1
        extra.insert(at, ("ended_at", stamp(os.stat(status_path).st_mtime)))
        extra.insert(0, ("rc", receipt["exit_code"]))
        extra.append(("runner_status", receipt.get("status")))
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        if execution.get("status") not in (None, receipt.get("status")):
            extra.append(("runner_phase", execution.get("status")))
        return fields + [("status", "finished")] + extra, HINT_CODEX_FINISHED, 0
    pid = runner_pid(path)
    if pid:
        return fields + [("status", "running"), ("pid", pid)] + extra + [("runner_status", "running")], \
            HINT_RUNNING, 0
    return fields + [("status", "unknown")] + extra, (
        "no live runner and no status.json (runner killed?); read stderr.log in task_dir and report it"), \
        EXIT_STATUS_UNKNOWN


def cmd_status(argv, out=None, err=None):
    out = out or sys.stdout
    p = Parser(prog="hermes-delegate status", allow_abbrev=False,
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
        print("{}={}".format(key, clip(value, 400)), file=out)
    print("hint=" + clip(hint, 400), file=out)
    return code


def wait_for_rc(args, run_dir, kind, via, launcher, out):
    rc_path, started_path = "{}/{}.rc".format(run_dir, kind), run_dir + "/started_at"
    begin = time.monotonic()
    deadline = begin + args.timeout
    grace = begin + min(args.start_grace, args.timeout)
    if not wait_until(lambda: os.path.exists(rc_path) or os.path.exists(started_path), run_dir, grace):
        print("status=never_started", file=out)
        if kind == "review":
            raise Fail("review never started: no started_at after {}s in {}; {} (ticket still open; "
                       "hermes-delegate review --launch-run {} can retry)".format(
                           min(args.start_grace, args.timeout), run_dir, launcher, run_dir), EXIT_NEVER_STARTED)
        raise Fail("{} run never started: no started_at after {}s in {}; {} (hermes-delegate {} --launch-run {} "
                   "can retry)".format(kind, min(args.start_grace, args.timeout), run_dir, launcher, kind, run_dir),
                   EXIT_NEVER_STARTED)
    if os.path.exists(started_path) and not os.path.exists(rc_path):
        print("status=started started_at={}".format(Path(started_path).read_text().strip()), file=out, flush=True)
    stalled = wait_for_rc_or_stall(rc_path, run_dir, "{}/{}.out".format(run_dir, kind), deadline,
                                   stall_seconds(kind, args.stall_minutes))
    if stalled:
        return stalled
    if not os.path.exists(rc_path):
        print("status=no_rc", file=out)
        print(watcher_line(run_dir, args.timeout, args.stall_minutes), file=out)
        raise Fail("{} started at {} but wrote no {}.rc after {}s in {}; it may still be running, or the {} was "
                   "killed without a trap or the {} is hung (re-watch with hermes-delegate wait {}, stop it with "
                   "hermes-delegate cancel {})".format(
                       kind, Path(started_path).read_text().strip() if os.path.exists(started_path) else "?",
                       kind, args.timeout, run_dir, "workspace" if via == "cmux" else "detached inner.zsh",
                       "reviewer" if kind == "review" else "worker", run_dir, run_dir), EXIT_NO_RC)


def cmd_wait(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    p = Parser(prog="hermes-delegate wait", allow_abbrev=False)
    p.add_argument("run_dir")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--start-grace", type=int, default=180,
                   help="fail early when inner.zsh has not written started_at by then")
    p.add_argument("--stall-minutes", type=float, default=None,
                   help="exit 92 (status=stalled) when <kind>.out has not grown for this long; 0 disables "
                        "(default: claude 20, review 20, qa 30)")
    args = p.parse_args(argv)
    run_dir = canonical(args.run_dir, "run dir", kind="dir")
    kind = run_kind(run_dir)
    stall_seconds(kind, args.stall_minutes)
    if args.stall_minutes is None and stall_count(run_dir) >= STALL_REARM_LIMIT:
        args.stall_minutes = 0.0
        print("stall_check=off ({} stalls already reported for this run; pass --stall-minutes M to check "
              "again)".format(stall_count(run_dir)), file=out)
    meta = json.loads(Path(run_dir, "meta.json").read_text()) if os.path.exists(run_dir + "/meta.json") else {}
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
        print(watcher_line(run_dir, rewatch_timeout(args.timeout, begin), args.stall_minutes), file=out)
        raise Fail("wait stopped by {}; the run keeps going (no {}.rc yet; re-watch with hermes-delegate wait {}, "
                   "stop it only with hermes-delegate cancel {})".format(
                       signal.Signals(stopped.signum).name, kind, run_dir, run_dir), EXIT_NO_RC)
    finally:
        restore_signals(previous)
    if stalled:
        report_stall(out, kind, "{}/{}.out".format(run_dir, kind), *stalled, run_dir=run_dir,
                     timeout=rewatch_timeout(args.timeout, begin), stall_minutes=args.stall_minutes)
        return EXIT_STALLED
    rc_path = "{}/{}.rc".format(run_dir, kind)
    text = Path(rc_path).read_text().strip()
    if not re.fullmatch(r"[0-9]{1,3}", text):
        raise Fail("malformed rc file: {!r}".format(text[:40]))
    code = int(text)
    leaked = redact_tokens(run_dir)
    output = "{}/{}.out".format(run_dir, kind)
    size = os.path.getsize(output) if os.path.exists(output) else 0
    print("status=finished", file=out)
    print("{} rc={} out={} bytes={}".format(kind, code, output, size), file=out)
    if leaked:
        print("WARNING: an OAuth token ({}...) appeared in {} and was masked in place; rotate the token".format(
            TOKEN_PREFIX, ",".join(leaked)), file=out)
    if kind == "qa" or meta.get("reviewer") == "codex":
        summary = summarize_codex(output, run_dir, kind)
    else:
        summary = summarize_stream(output, run_dir, kind)
    if kind != "review":
        summary += worktree_summary(run_dir, meta)
    for line in summary:
        print(line, file=out)
    if code:
        errors = "{}/{}.err".format(run_dir, kind)
        if os.path.exists(errors):
            tail = Path(errors).read_text(errors="replace").splitlines()[-20:]
            for line in tail:
                print(line, file=out)
    else:
        close_workspace(meta, out)
    return min(code, 255)


SMOKE_PROMPT = "Reply with exactly the single word ok and nothing else."


def smoke_script(target, smoke_dir, via):
    q = shlex.quote
    lines = ["#!/bin/zsh", "set -u", "cd {} || exit 2".format(q(smoke_dir))]
    out, err = q(smoke_dir + "/smoke.out"), q(smoke_dir + "/smoke.err")
    if target == "codex":
        lines += codex_env()
        command = shlex.join([worker_binary("codex"), "exec", "--ephemeral", "-s", "read-only",
                              "-m", REVIEW_POLICY["codex"]["model"], "-c", 'model_reasoning_effort="low"',
                              *codex_mcp_off(), "--skip-git-repo-check", "-C", smoke_dir, "-o", smoke_dir + "/smoke.final.md",
                              SMOKE_PROMPT])
    else:
        command = shlex.join([worker_binary("claude"), "-p", "--model", "haiku", "--tools", "Read",
                              "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config",
                              '{"mcpServers":{}}', "--no-session-persistence", "--output-format", "text",
                              SMOKE_PROMPT])
        if via == "direct":
            lines += ["fail() { print -r -- \"$2\" >> " + err + "; print -r -- $1 > "
                      + q(smoke_dir + "/smoke.rc") + "; exit $1; }"]
            lines += token_checks(token_file())
            command = "( export CLAUDE_CODE_OAUTH_TOKEN=\"$(< {})\"; exec {} )".format(q(token_file()), command)
    lines += ["{} < /dev/null > {} 2>> {}".format(command, out, err), "code=$?",
              "print -r -- $code > {}.tmp && mv -f {}.tmp {}".format(*[q(smoke_dir + "/smoke.rc")] * 3),
              "exit $code"]
    return "\n".join(lines) + "\n"


def cmd_smoke(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    p = Parser(prog="hermes-delegate smoke", allow_abbrev=False,
               description="Tiny auth/exec check of a CLI; not a review, issues no ticket.")
    p.add_argument("target", choices=("codex", "claude"))
    p.add_argument("--via", choices=VIA, help="default: codex direct, claude cmux")
    p.add_argument("--timeout", type=int, default=180)
    args = p.parse_args(argv)
    via = args.via or REVIEW_POLICY[args.target]["via"]
    if not 10 <= args.timeout <= 900:
        raise Fail("--timeout must be between 10 and 900 seconds", 2)
    binary = worker_binary(args.target)
    cmux = tools()[1]
    check_launch_tools(via, cmux, binary)
    if args.target == "claude" and via == "direct":
        check_token_file(token_file())
    smoke_dir = "{}/{}-{}-{}".format(state_root("smoke"), args.target, time.strftime("%Y%m%dT%H%M%S"),
                                     secrets.token_hex(3))
    os.mkdir(smoke_dir, 0o700)
    script = smoke_dir + "/smoke.zsh"
    Path(script).write_text(smoke_script(args.target, smoke_dir, via))
    os.chmod(script, 0o700)
    rc_path = smoke_dir + "/smoke.rc"
    workspace, passed = None, False
    try:
        if via == "direct":
            child = subprocess.Popen(["/bin/zsh", "-ic", "exec /bin/zsh " + shlex.quote(script)], cwd=smoke_dir,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                child.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                child.wait()
        else:
            workspace = launch_workspace([cmux, "new-workspace", "--name", "smoke-" + args.target, "--cwd",
                                          smoke_dir, "--focus", "false", "--command", zsh_command(script)],
                                         kind="smoke", run_dir=smoke_dir)
            wait_until(lambda: os.path.exists(rc_path), smoke_dir, time.monotonic() + args.timeout)
        leaked = redact_tokens(smoke_dir)
        if leaked:
            print("WARNING: an OAuth token ({}...) appeared in smoke output and was masked".format(TOKEN_PREFIX),
                  file=err)
        if not os.path.exists(rc_path):
            reason = "no result within {}s".format(args.timeout)
        else:
            code = Path(rc_path).read_text().strip()
            final = smoke_dir + ("/smoke.final.md" if args.target == "codex" else "/smoke.out")
            answer = Path(final).read_text(errors="replace").strip() if os.path.exists(final) else ""
            if code == "0" and answer.strip(" .\n`'\"").lower() == "ok":
                print("ok {} via={}".format(args.target, via), file=out)
                passed = True
                return 0
            errors = Path(smoke_dir, "smoke.err").read_text(errors="replace") if os.path.exists(
                smoke_dir + "/smoke.err") else ""
            reason = "rc={} answer={!r} stderr_last={}".format(code, answer[:80], last_line(errors)[:300])
        print("fail {} via={}: {} (kept {})".format(args.target, via, reason, smoke_dir), file=out)
        return 1
    finally:
        if workspace:
            close_workspace({"via": "cmux", "cmux_workspace": workspace}, io.StringIO())
        if passed:
            shutil.rmtree(smoke_dir, ignore_errors=True)


USAGE = """usage: hermes-delegate <command> [options]
  spec-check --spec F [--workdir D] [--kind codex|claude|qa]
          Read-only SPEC validation; workdir defaults to F's directory, kind to codex.
          Prints ok (exit 0), or each problem on stderr (exit 2).
  spec-check --template
          Print a fill-in SPEC skeleton (exit 0).

  codex   --spec F --workdir D --class C --timeout SEC [--risk R] [--ambiguity A] [--phase P]
          [--implementation-class I] [--contract-resolved] [--choice-reason T]
          [--deeper-analysis-evidence T] [--hard-judgment] [--high-failure-cost]
          [--failure-kind K --prior-model M --prior-effort E --attempt N]
          [--prior-action T] [--correction-evidence T] [--handoff-ref T]... [--max-attempts N]
          [--missing-context | --broken-environment | --deterministic-tool-sufficient]
          [--allowed-root D] [--output-dir D] [--sandbox workspace-write] [--code-scope P...]
          [--report REL]
          --report names an implementer report inside the workdir, verbatim in SPEC.
          Required for mechanical/bounded, including classes the runner remaps to them;
          needs --sandbox workspace-write.
          With --report, a worker exiting 0 without a new, non-empty, regular report
          under the workdir makes the runner exit 76 with acceptance.status=failed.
          --workdir must be a git worktree root. --code-scope takes files or directories
          relative to --workdir (directories expand to the runner-accepted files).
          --task-class is accepted as an alias of --class. Omit --sandbox for read-only.
          frontier (xhigh) needs --deeper-analysis-evidence with a real reference.
          Dry-runs the runner, checks its registration baseline, and prints ONE bare argv
          line for terminal(background=true, notify_on_complete=true). Never runs the worker.
          Rejects --model/--effort/--tier and every other override or --progress-* option.
          Exit: 0 line printed; 3 policy skipped (no worker needed, nothing printed);
          74 runner policy blocked; 2 usage; 1 other failure.
  review  --reviewer claude|codex --item ID --commit SHA --prompt F --workdir D [--base SHA]
          [--label L] [--timeout SEC] [--via direct|cmux] [--owner-approval ID]
          [--check | --no-launch]
          The item must already be opened with review-gate open. The workdir must be a clean
          checkout of SHA. Runs review-gate start, writes a run dir (diff.patch = git diff
          --binary BASE SHA with --base, else git show SHA; log.txt = git log/show --stat) and
          runs review-gate run -- <reviewer> with prompt.md = F + the standard review rules
          (P0/P1 only, file:line + concrete trigger, round-2 scope, exact approval line
          "APPROVED: no P0/P1").
          claude: claude -p --model opus --effort high, Read/Glob/Grep only, stream-json;
            --via cmux (default) or direct (OAuth token file, no cmux).
          codex: codex exec --ephemeral -s read-only -m gpt-6.1-sol, effort high (fixed);
            --via direct (default) or cmux.
          --check is a read-only preflight (no ticket, no auth check: use smoke). --no-launch
          issues a REAL ticket and skips the launch; that ticket stays open until launched.
          The verdict is never recorded automatically: koharu reproduces the findings, then
          runs review-gate close (PASS: --blockers 0 or omitted; BLOCK: --blockers N>=1).
          A pending verdict refuses a new round: koharu reproduces it, runs review-gate
          close itself and retries (it tells the user only if it cannot reproduce it).
          Round 3+ needs the user's approval: koharu asks in Discord and passes that
          message ID with --owner-approval ID (15-22 digits, passed to review-gate start;
          an ID already used on this item is refused). The user only gives that approval.
  review  --launch-run RUN_DIR
          Launches an already prepared run dir whose ticket is still open.
  claude  --spec F --workdir D --class {claude_choices} --timeout SEC
          [--advisor opus] [--label L] [--via cmux|direct] [--no-launch]
          Claude Code implementation run (claude -p, write-capable: acceptEdits inside the
          worktree, Bash allowed, no MCP, stream-json). --via cmux (default) runs in a new
          cmux workspace with the Keychain login. --class picks model/effort:
          {claude_classes}.
          {claude_direct}
          --advisor opus is opt-in per run (per-run settings override, never settings.json);
          refused for classes that already run on Opus.
          --workdir must be a clean git worktree root. Rejects --model/--effort and any
          permission/tool override. No review-gate: this is implementation, not review.
  qa      --spec F --workdir D --timeout SEC [--label L] [--no-launch]
          Device/simulator QA run: codex exec -m {qa[model]}, effort {qa[effort]}, sandbox
          {qa[sandbox]} (fixed user pin; no model/effort/sandbox flags accepted) in a
          new cmux workspace (GUI Keychain for private-registry tokens and code signing).
          --workdir must be a clean git worktree root.
  claude|qa --launch-run RUN_DIR
          Launches a prepared run dir (after --no-launch or a failed launch).
          review/claude/qa print run_dir=, rc=, via= and a watcher: line to run verbatim.
          A run dir launches once: inner.zsh takes RUN_DIR/lock, --launch-run refuses it.
  smoke   codex|claude [--via direct|cmux] [--timeout SEC]
          Tiny auth/exec check (codex: gpt-6.1-sol low; claude: haiku), prints ok or the
          reason; exit 0 ok, 1 failed. Not a review, no ticket. Default via: codex direct,
          claude cmux.
  wait    RUN_DIR [--timeout SEC] [--start-grace SEC] [--stall-minutes M]
          Blocks until RUN_DIR/<kind>.rc exists (kind from meta.json: review, claude or
          qa); prints status=started|finished|never_started|no_rc|stalled and exits with
          the worker rc, 90 when the run never started, 91 (status=no_rc) when no rc
          appeared: either --timeout passed or wait itself was signalled. In both cases
          the run may still be going: re-arm by running the printed watcher: line.
          92 (status=stalled) when <kind>.out has not grown for M minutes (default
          claude 20, review 20, qa 30; 0 disables), counted from the later of its last
          growth and this wait's start, so a re-run wait never fires at once. It prints
          the last growth time, the minutes stalled and the last tool call/event
          (tokens masked), stall_count=N (RUN_DIR/stall_count), a ready watcher: line
          (remaining --timeout, same --stall-minutes) and a watcher_no_stall: line
          (--stall-minutes 0), and never stops the run: re-arm with one of them or stop
          it with cancel. From the 2nd stall of a run only watcher_no_stall: is printed
          and a wait without --stall-minutes no longer checks for stalls (each re-arm
          spends one gateway continuation). An rc always wins over a stall.
          Writes RUN_DIR/review.result.md or claude.result.md; qa reports qa.final.md.
          Masks any sk-ant-oat token found in run dir outputs and warns. After rc 0 of a
          cmux run it closes that run's cmux workspace (failed runs keep theirs).
          A SIGTERM/SIGHUP/SIGINT to wait (watcher cancel, gateway stop/restart, ssh drop)
          only ends wait (status=no_rc, exit 91, "wait stopped by SIG...; the run keeps
          going"); the run keeps going, cmux or direct. Re-watch with wait; stop the run
          only with hermes-delegate cancel.
  status  PATH
          Instant, non-blocking check (no sleep, no wait) of PATH: a run dir under
          STATE/review|claude|qa/<name> or a codex output dir STATE/codex/<ts>-<rand>
          (a trailing slash is accepted). After launched=yes or the printed codex argv,
          check at most once with hermes-delegate status PATH; never sleep+peek.
          Prints key=value lines: kind=review|claude|qa|codex,
          status=not_started|running|finished|unknown, then where known rc=, pid= (live
          inner.zsh or codex runner), started_at=, ended_at=, last_output_age_s= (since
          <kind>.out or events.jsonl last changed), stall_count=, and for codex task_dir=,
          events=, runner_status= (from status.json); the last line is hint=.
          Exit 0 not_started/running/finished, 3 unknown (no rc and no live process,
          malformed rc/status.json, meta kind mismatch), 2 usage or a path outside the
          state root.
  cancel  RUN_DIR [--grace SEC]
          Sends SIGTERM to this run's inner.zsh (RUN_DIR/inner.pid, checked against the
          live process), which stops the worker group or review-gate run and records the
          real rc. A run whose inner.zsh has not started yet is fenced (lock taken, rc 143).
          Prints status=finished|no_rc; exits with the rc, 91 when none within --grace
          (default 30). The only way to stop a run. A cancelled review ticket still needs
          review-gate close.
""".format(claude_classes=", ".join("{}={}/{}".format(k, v["model"], v["effort"])
                                    for k, v in CLAUDE_IMPL_POLICY.items()),
           claude_choices="|".join(CLAUDE_IMPL_POLICY), qa=QA_POLICY,
           claude_direct=("--via direct runs detached without cmux, with the OAuth token file\n"
                          "          (0600) exported only to the claude process." if CLAUDE_IMPL_DIRECT else
                          "--via direct is refused here: the token would reach Bash children\n"
                          "          (direct is for claude reviews only)."))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0 if argv else 2
    commands = {"codex": cmd_codex, "review": cmd_review, "wait": cmd_wait, "smoke": cmd_smoke,
                "cancel": cmd_cancel, "status": cmd_status, "spec-check": cmd_spec_check,
                "claude": lambda rest: cmd_worker("claude", rest),
                "qa": lambda rest: cmd_worker("qa", rest)}
    if argv[0] not in commands:
        print("hermes-delegate: unknown command {!r}; see --help".format(argv[0]), file=sys.stderr)
        return 2
    try:
        return commands[argv[0]](argv[1:])
    except Fail as exc:
        print("hermes-delegate: " + str(exc).replace("\n", " "), file=sys.stderr)
        return exc.code
    except OSError as exc:
        print("hermes-delegate: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
