#!/usr/bin/env python3
"""Read-only weekly ledger/session correlation; Python 3.9+, stdlib only."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        return None


def deploy_state(host):
    """Read the last install receipt without needing a checkout or sibling modules."""
    manifest = Path.home() / ".local/state/review-gate/deploy-manifest.json"
    empty = dict(manifest="missing", source_commit=None, deployed_at=None, targets=[])
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
        expected = {host[0].upper() + str(i) for i in range(1, 5)}
        if (not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("host") != host
                or not isinstance(raw.get("source_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", raw["source_commit"])
                or not isinstance(raw.get("deployed_at"), str)
                or timestamp(raw["deployed_at"]) is None
                or not isinstance(raw.get("targets"), list)):
            raise ValueError("invalid manifest")
        targets = raw["targets"]
        if len(targets) != 4 or {t["id"] for t in targets} != expected:
            raise ValueError("incomplete manifest")
        for target in targets:
            if (not isinstance(target["path"], str) or not Path(target["path"]).is_absolute()
                    or not re.fullmatch(r"[0-9a-f]{64}", target["sha256"])
                    or not re.fullmatch(r"0[0-7]{3}", target["mode"])):
                raise ValueError("invalid target")
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, KeyError, TypeError):
        return dict(empty, manifest="invalid")
    rows = []
    for target in targets:
        path = Path(target["path"])
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            mode = format(stat.S_IMODE(path.stat().st_mode), "04o")
            status = "ok" if (not path.is_symlink() and current == target["sha256"]
                              and mode == target["mode"]) else "modified"
        except FileNotFoundError:
            status = "missing"
        except OSError:
            status = "modified"
        rows.append(dict(id=target["id"], path=target["path"], status=status))
    return dict(manifest="ok", source_commit=raw["source_commit"],
                deployed_at=raw["deployed_at"], targets=rows)


def since_date(value):
    try:
        if len(value) != 10:
            raise ValueError(value)
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")


def rows(path):
    """Damaged/partial JSONL lines do not discard the rest of a session."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def objects(value):
    """Walk only JSON containers; do not search text that quotes protocol fields."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            yield current
            pending.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            pending.extend(current)


def codex_session(path):
    meta, sandbox = None, None
    for index, row in enumerate(rows(path)):
        if index == 0 and row.get("type") == "session_meta":
            meta = row.get("payload", {})
        if sandbox is None:
            for obj in objects(row):
                policy = obj.get("sandbox_policy")
                if isinstance(policy, dict) and isinstance(policy.get("type"), str):
                    sandbox = policy["type"]
                    break
        if meta is not None and sandbox is not None:
            break
    if isinstance(meta, dict) and sandbox == "read-only" and timestamp(meta.get("timestamp")):
        return dict(cli="codex", start=meta["timestamp"], cwd=meta.get("cwd"), file=str(path))
    return None


def claude_session(path):
    start, cwd, sdk, writes = None, None, False, False
    for row in rows(path):
        parsed = timestamp(row.get("timestamp"))
        if parsed is not None and (start is None or parsed < start):
            start = parsed
        if cwd is None and isinstance(row.get("cwd"), str):
            cwd = row["cwd"]
        sdk = sdk or row.get("entrypoint") == "sdk-cli"
        if row.get("type") == "assistant":
            writes = writes or any(obj.get("type") == "tool_use" and obj.get("name") in WRITE_TOOLS
                                   for obj in objects(row.get("message", {})))
    if sdk and not writes and start is not None:
        return dict(cli="claude", start=start.isoformat(), cwd=cwd, file=str(path))
    return None


def cwd_matches(session_cwd, run_cwd):
    if not run_cwd:
        return True  # The original ledger did not record run cwd.
    if not session_cwd:
        return False
    first, second = os.path.normpath(session_cwd), os.path.normpath(run_cwd)
    return first == second or first.startswith(second.rstrip(os.sep) + os.sep)


def matched(session, runs, now):
    start = timestamp(session["start"])
    slack = timedelta(minutes=3)
    for run in runs:
        begin = timestamp(run.get("started_at"))
        end = timestamp(run.get("ended_at")) or now
        if begin is not None and begin - slack <= start <= end + slack and cwd_matches(
                session.get("cwd"), run.get("cwd")):
            return True
    return False


def group(cwd):
    if cwd:
        for root in (Path.home() / ".fido-automation", Path.home() / "Documents/fido-wt"):
            if cwd_matches(cwd, str(root)):
                return "fido"
    return "other"


def ledger_issues(raw, now):
    events = raw.get("events", [])
    result = {kind: [e for e in events if e.get("kind") == kind]
              for kind in ("bypass", "legacy-exempt", "rejected")}
    result["ticket-reuse"] = [e for e in events if e.get("kind") == "ticket-reuse"
                              or "ticket-reuse" in e.get("detail", "")]
    result["items"] = raw.get("items", [])
    result["over_round_items"] = raw.get("over_round_items", [])
    result["stale_tickets"] = [t for t in raw.get("stale_tickets", [])
                               if t.get("status") != "closed" and timestamp(t.get("issued_at"))
                               and timestamp(t["issued_at"]) < now - timedelta(hours=24)]
    return result


def collect(since, raw, codex_root=None, claude_root=None, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = since_date(since)
    codex_root = Path(codex_root or os.environ.get("CODEX_SESSIONS_DIR", "~/.codex/sessions")).expanduser()
    claude_root = Path(claude_root or os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")).expanduser()
    sessions, errors = [], []
    for path in sorted(codex_root.glob("*/*/*/*.jsonl")):
        date = "-".join(path.parts[-4:-1])
        if date < since:
            continue
        try:
            session = codex_session(path)
        except OSError as exc:
            errors.append(dict(cli="codex", file=str(path), error=type(exc).__name__))
            continue
        if session:
            sessions.append(session)
    for path in sorted(claude_root.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < cutoff.timestamp():
                continue
            session = claude_session(path)
        except OSError as exc:
            errors.append(dict(cli="claude", file=str(path), error=type(exc).__name__))
            continue
        if session:
            sessions.append(session)
    unledgered = [dict(s, group=group(s.get("cwd"))) for s in sessions
                  if not matched(s, raw.get("runs", []), now)]
    result = dict(since=since, ledger=ledger_issues(raw, now), sessions=dict(
        codex_review_like=sum(s["cli"] == "codex" for s in sessions),
        claude_review_like=sum(s["cli"] == "claude" for s in sessions),
        unledgered=sorted(unledgered, key=lambda s: timestamp(s["start"]), reverse=True)))
    if errors:
        result["sessions"]["errors"] = errors
    return result


def load_ledger(since):
    gate = Path(os.environ.get("REVIEW_GATE_PY", "~/.local/share/review-gate/review_gate.py")).expanduser()
    result = subprocess.run([sys.executable, str(gate), "audit", "--since", since,
                             "--json", "--run-context"], capture_output=True, text=True, timeout=30)
    result.check_returncode()
    raw = json.loads(result.stdout)
    if not isinstance(raw, dict) or not isinstance(raw.get("runs"), list):
        raise ValueError("ledger audit missing run context")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=since_date)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    since = args.since.date().isoformat()
    try:
        result = collect(since, load_ledger(since))
        result["deploy"] = deploy_state("mac")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("review-audit: " + type(exc).__name__, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
