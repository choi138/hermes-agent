#!/usr/bin/env python3
"""Compare or deploy the repository's review-gate files. Python 3.9+, stdlib only."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from deploy_io import absolute, operate

SOURCE = Path(__file__).resolve().parent
DEFAULT_MAGI = "/Users/choegeun-won/.local/share/uv/tools/magi/bin/magi"


def raw(source, args):
    return source.read_bytes()


def render(source, args):
    values = {"@MAGI_REAL_BIN@": str(absolute(args.magi_real_bin)),
              "@REVIEW_GATE@": str(absolute(args.review_gate)),
              "@PYTHON@": str(absolute(args.python))}
    # The shim encloses placeholders in single quotes; the small wrapper does not.
    quoted = source.name == "magi-shim.sh"
    values = {k: v.replace("'", "'\"'\"'") if quoted else shlex.quote(v) for k, v in values.items()}
    return re.sub(r"@(?:MAGI_REAL_BIN|REVIEW_GATE|PYTHON)@",
                  lambda match: values[match.group()], source.read_text(encoding="utf-8-sig")).encode("utf-8")


# The only deployment target declaration: id, source, renderer, host, destination, mode.
TARGETS = (
    ("M1", "review_gate.py", raw, "mac", "~/.local/share/review-gate/review_gate.py", 0o755),
    ("M2", "audit/review_audit_mac.py", raw, "mac", "~/.local/share/review-gate/review_audit_mac.py", 0o755),
    ("M3", "magi-shim.sh", render, "mac", "~/.local/bin/magi", 0o755),
    ("M4", "review-gate-wrapper.sh", render, "mac", "~/.local/bin/review-gate", 0o755),
    ("S1", "hooks/guard_review_gate.py", raw, "server", "~/.hermes/scripts/guard_review_gate.py", 0o755),
    ("S2", "audit/review_audit_report.py", raw, "server", "~/.hermes/scripts/review_audit_report.py", 0o755),
    ("S3", "../bg-continue-hook/guard_background_continue.py", raw, "server", "~/.hermes/scripts/guard_background_continue.py", 0o755),
    ("S4", "audit/review_audit_weekly.sh", raw, "server", "~/.hermes/profiles/koharu/scripts/review_audit_weekly.sh", 0o755),
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(3, f"install: {message}\n")


def parser():
    result = Parser(description=__doc__)
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--apply", action="store_true")
    result.add_argument("--host", choices=("mac", "server", "all"), default="all")
    result.add_argument("--server-host", default="choi138-ri")
    result.add_argument("--magi-real-bin", default=DEFAULT_MAGI)
    result.add_argument("--review-gate", default=str(Path.home() / ".local/share/review-gate/review_gate.py"))
    result.add_argument("--python", default="/usr/bin/python3")
    return result


def source_commit():
    root = SOURCE.parents[2]
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    if git("status", "--porcelain", "--", "scripts/ops"):
        raise ValueError("scripts/ops is dirty; commit all ops changes before --apply")
    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a 40-hex source commit")
    return commit


def remote(payload, server_host):
    prefix = shlex.split(os.environ.get("REVIEW_GATE_SSH", "ssh"))
    if not prefix:
        raise ValueError("REVIEW_GATE_SSH must not be empty")
    program = (SOURCE / "deploy_io.py").read_text(encoding="utf-8-sig")
    program += f"\nprint(json.dumps(operate(json.loads({json.dumps(payload)!r}))))\n"
    result = subprocess.run(prefix + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                            server_host, "/usr/bin/python3", "-"],
                            input=program, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    if result.returncode:
        raise ValueError(f"server deployment failed (exit {result.returncode}): {result.stderr.strip()}")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != len(payload["targets"]):
        raise ValueError("Invalid server deployment response")
    return rows


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        # This gate precedes even the first SSH call or destination inspection.
        commit = source_commit() if args.apply else None
        hosts = ("mac", "server") if args.host == "all" else (args.host,)
        payloads, results = {}, {}
        for host in hosts:
            targets = [dict(id=ident, path=dest, mode=mode,
                            content=base64.b64encode(renderer(SOURCE / source, args)).decode("ascii"))
                       for ident, source, renderer, owner, dest, mode in TARGETS if owner == host]
            payloads[host] = dict(action="read", host=host, source_commit=commit, targets=targets)
        for host in hosts:
            payload = payloads[host]
            results[host] = operate(payload) if host == "mac" else remote(payload, args.server_host)
        if args.apply:
            for host in hosts:
                payload = dict(payloads[host], action="apply")
                results[host] = operate(payload) if host == "mac" else remote(payload, args.server_host)
        for host in hosts:
            for row in results[host]:
                print("{}  {}:{} repo={} deployed={}".format(row["status"], host, row["path"],
                      row["sha256"][:12], row["deployed"][:12] if row["deployed"] else "missing"))
        if args.apply and any(r["id"] in ("S1", "S3") and r["status"] != "unchanged"
                              for r in results.get("server", [])):
            print("Note: hermes hooks doctor will show mtime drift; hooks keep running.")
        return int(args.check and any(r["status"] != "unchanged" for rows in results.values() for r in rows))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        cli.exit(3, f"install: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
