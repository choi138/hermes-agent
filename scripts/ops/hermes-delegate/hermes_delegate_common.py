"""Shared policy, validation and bounded command utilities."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


HOME = Path("/Users/choegeun-won")


RELEASE = HOME / "hermes-workspaces/delegation-notifications-20261013/releases/mac-20261027-terra-report"


RUNNER = RELEASE / "scripts/run_codex_task.py"


RUNNER_PYTHON = HOME / "Documents/hermes-agent-upstream/.venv/bin/python"


SENDER_PROFILE = "koharu"


EXPECTED_BOT_ID = "1554717538376753244"


HD_BIN = HOME / ".local/bin/hermes-delegate"


DEFAULT_STATE_ROOT = HOME / ".local/state/hermes-delegate"


DEFAULT_REVIEW_GATE = HOME / ".local/bin/review-gate"


DEFAULT_CMUX = Path("/Applications/cmux.app/Contents/Resources/bin/cmux")


CLAUDE_BIN = HOME / ".local/bin/claude"


CODEX_NODE_BIN = HOME / ".asdf/installs/nodejs/22.19.0/bin"


CODEX_BIN = CODEX_NODE_BIN / "codex"


GTIMEOUT = Path("/opt/homebrew/bin/gtimeout")


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


STALL_REARM_LIMIT = 2


EXIT_LOCKED = 75


APPROVAL_LINE = "APPROVED: no P0/P1"


CLAUDE_READ_ONLY = ("--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                    "--permission-mode", "dontAsk", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
                    "--output-format", "stream-json", "--verbose")


CLAUDE_IMPL_POLICY = {
    "mechanical": {"model": "sonnet", "effort": "medium"},
    "ambiguous": {"model": "opus", "effort": "medium"},
    "judgment": {"model": "opus", "effort": "high"},
}


CLAUDE_ADVISORS = ("opus",)


ADVISOR_SETTINGS = '{"env":{"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":""}}'


CLAUDE_WRITE = ("--tools", "Read,Glob,Grep,Edit,Write,Bash", "--allowedTools", "Bash",
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
                "--output-format", "stream-json", "--verbose")


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


def flag(name):
    return "--" + name.replace("_", "-")


def canonical(value, what, *, must_exist=True, kind=None):
    if not value or not os.path.isabs(value):
        raise Fail(f"{what} must be an absolute path: {value}")
    if os.path.normpath(value) != value or "//" in value:
        raise Fail(f"{what} is not canonical: {value}")
    if must_exist and not os.path.lexists(value):
        raise Fail(f"{what} does not exist: {value}")
    if os.path.realpath(value) != value:
        raise Fail(f"{what} is a symlink or passes through one (realpath {os.path.realpath(value)}): {value}")
    if kind == "dir" and not os.path.isdir(value):
        raise Fail(f"{what} is not a directory: {value}")
    if kind == "file" and not os.path.isfile(value):
        raise Fail(f"{what} is not a regular file: {value}")
    if not SAFE_TOKEN.fullmatch(value):
        raise Fail(f"{what} contains characters the gateway argv cannot carry: {value}")
    return value


def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_free_text(name, value):
    if not value or any(char in FREE_TEXT_FORBIDDEN for char in value) or len(value) > 512:
        raise Fail(f"{flag(name)} must be a single line of at most 512 chars without shell metacharacters"
                   )
    return value


def run(argv, *, cwd=None, stdin=None, timeout=120, env=None):
    try:
        return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, env=env, check=False)
    except FileNotFoundError:
        raise Fail(f"cannot execute {argv[0]}")
    except subprocess.TimeoutExpired:
        raise Fail(f"{os.path.basename(str(argv[0]))} timed out after {timeout}s")


def run_bytes(argv, *, cwd=None, timeout=120):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise Fail(f"cannot execute {argv[0]}")
    except subprocess.TimeoutExpired:
        raise Fail(f"{os.path.basename(str(argv[0]))} timed out after {timeout}s")


def numeric_uid():
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise Fail("this Mac entry requires numeric UID ownership checks")
    return getuid()


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


def git_value(workdir, *args):
    result = run(["git", "-C", workdir] + list(args))
    if result.returncode:
        raise Fail("git {} failed: {}".format(" ".join(args), last_line(result.stderr)))
    return result.stdout.strip()


def dirty_entries(workdir):
    return git_value(workdir, "status", "--porcelain", "--untracked-files=normal")


def run_kind(run_dir):
    try:
        meta = json.loads(Path(run_dir, "meta.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        meta = {}
    kind = meta.get("kind", "review")
    if kind not in ("review", "claude", "qa"):
        raise Fail(f"unknown run kind in meta.json: {kind!r}")
    return kind
