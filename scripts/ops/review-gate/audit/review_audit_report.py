#!/usr/bin/env python3
"""Print a bounded Korean weekly report for the coordinator's Discord cron."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

DEFAULT_LOG = "~/.hermes/profiles/koharu/logs/review-gate-hook.jsonl"
DEFAULT_MAC_CMD = ("ssh -o BatchMode=yes -o ConnectTimeout=15 geunwon-mac /usr/bin/python3 "
                   "~/.local/share/review-gate/review_audit_mac.py --since {since} --json")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def since_date(value):
    try:
        if len(value) != 10:
            raise ValueError(value)
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")


def hook_records(since):
    path = Path(os.environ.get("REVIEW_GATE_HOOK_LOG", DEFAULT_LOG)).expanduser()
    if not path.exists():
        return []
    cutoff = since_date(since)
    records = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and timestamp(row.get("ts")) and timestamp(row["ts"]) >= cutoff:
                records.append(row)
    return records


def mac_json(since):
    command = os.environ.get("REVIEW_AUDIT_MAC_CMD", DEFAULT_MAC_CMD).replace("{since}", since)
    result = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=120)
    result.check_returncode()
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict) or not isinstance(data.get("ledger"), dict):
        raise ValueError("invalid Mac audit JSON")
    return data


def one_line(value, limit=160):
    return " ".join(str(value).split())[:limit]


def format_report(since, records, data=None, mac_error=None, hook_error=None):
    counts = Counter(r.get("decision") for r in records)
    ledger = data.get("ledger", {}) if data else {}
    sessions = data.get("sessions", {}) if data else {}
    unledgered = sessions.get("unledgered", [])
    notable = any(counts[k] for k in ("blocked", "bypass", "error")) or any(ledger.values()) or unledgered or sessions.get("errors")
    if not notable and not mac_error and not hook_error:
        return "이번 주 ledger를 벗어난 리뷰가 없습니다."
    lines = ["리뷰 게이트 주간 점검 ({} 이후 UTC)".format(since),
             "hook: 차단 {} / wrapped {} / bypass {} / 오류 {}".format(
                 counts["blocked"], counts["wrapped"], counts["bypass"], counts["error"])]
    reasons = Counter(one_line(r.get("bypass_reason") or "사유 없음", 100)
                      for r in records if r.get("decision") == "bypass")
    for reason, count in reasons.most_common(3):
        lines.append("bypass: {} ({}건)".format(reason, count))
    if len(reasons) > 3:
        lines.append("bypass 사유 외 {}종".format(len(reasons) - 3))
    if hook_error:
        lines.append("hook 로그 읽기 실패: " + hook_error)
    if mac_error:
        lines.append("Mac 감사 실패: " + mac_error)
    elif data:
        lines.append("ledger: bypass {} / legacy-exempt {} / rejected {} / ticket-reuse {}".format(
            *(len(ledger.get(k, [])) for k in ("bypass", "legacy-exempt", "rejected", "ticket-reuse"))))
        lines.append("ledger: 2라운드 초과 {} / 24시간 미종결 {} / 중단 항목 {}".format(
            *(len(ledger.get(k, [])) for k in ("over_round_items", "stale_tickets", "items"))))
        if sessions.get("errors"):
            lines.append("세션 파일 읽기 오류: {}건".format(len(sessions["errors"])))
        other = sorted((s for s in unledgered if s.get("group") != "fido"),
                       key=lambda s: timestamp(s.get("start")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        lines.append("미등록 other: {}건".format(len(other)))
        for session in other[:10]:
            lines.append("- {} {} {}".format(one_line(session.get("cli", "?"), 12),
                                             one_line(session.get("start", "?"), 32),
                                             one_line(session.get("cwd") or "cwd 없음", 120)))
        if len(other) > 10:
            lines.append("외 {}건".format(len(other) - 10))
        lines.append("미등록 fido: {}건".format(sum(s.get("group") == "fido" for s in unledgered)))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=since_date, default=datetime.now(timezone.utc) - timedelta(days=7))
    args = parser.parse_args(argv)
    since = args.since.date().isoformat()
    records, data, mac_error, hook_error = [], None, None, None
    try:
        records = hook_records(since)
    except OSError as exc:
        hook_error = type(exc).__name__
    try:
        data = mac_json(since)
    except Exception as exc:
        mac_error = type(exc).__name__
        if isinstance(exc, subprocess.CalledProcessError):
            mac_error += " (exit {})".format(exc.returncode)
    print(format_report(since, records, data, mac_error, hook_error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
