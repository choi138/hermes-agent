#!/usr/bin/python3
"""Transactional, local review ledger. Python 3.9+, standard library only."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import uuid


REVIEWERS = ("magi", "codex", "claude", "pr-bot", "other")
TTL = timedelta(hours=6)
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY, item TEXT NOT NULL REFERENCES items(id),
        round INTEGER NOT NULL, reviewer TEXT NOT NULL, reviewed_commit TEXT NOT NULL,
        status TEXT NOT NULL, issued_at TEXT NOT NULL,
        joined INTEGER NOT NULL DEFAULT 0, fallback INTEGER NOT NULL DEFAULT 0,
        fallback_for TEXT REFERENCES tickets(id), owner_approval TEXT, note TEXT,
        started_at TEXT, ended_at TEXT, exit_code INTEGER, argv TEXT,
        closed_at TEXT, verdict TEXT, blockers INTEGER, evidence TEXT, reason TEXT)""",
    "CREATE INDEX IF NOT EXISTS tickets_item_round ON tickets(item, round)",
    """CREATE TABLE IF NOT EXISTS ticket_consumptions (
        ticket TEXT PRIMARY KEY REFERENCES tickets(id), consumed_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL,
        cwd TEXT NOT NULL, user TEXT NOT NULL, created_at TEXT NOT NULL)""",
)


class GateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GateError(3, message)


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat(timespec="microseconds")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@contextmanager
def transaction(db, write=True):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def connect():
    path = Path(os.environ.get(
        "REVIEW_GATE_DB", "~/.local/state/review-gate/ledger.sqlite")).expanduser()
    # Only newly created directories are ours to set permissions on.
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA foreign_keys = ON")
        with transaction(db):
            for statement in SCHEMA:
                db.execute(statement)
    except BaseException:
        db.close()
        raise
    return db


def require_row(db, table, identifier, missing_code=4):
    # Table names are internal constants, never CLI input.
    row = db.execute("SELECT * FROM " + table + " WHERE id = ?", (identifier,)).fetchone()
    if row is None:
        if missing_code == 2:
            refuse("Ticket not found: {}; the owner must issue a valid ticket within the round "
                   "policy.".format(identifier))
        raise GateError(missing_code, "{} not found: {}".format(table, identifier))
    return row


def refuse(message):
    raise GateError(2, message)


def current_tickets(db, item):
    return db.execute("""SELECT rowid, * FROM tickets WHERE item = ? AND round =
        (SELECT MAX(round) FROM tickets WHERE item = ?) ORDER BY rowid""",
                      (item, item)).fetchall()


def recompute(db, item):
    tickets = current_tickets(db, item)
    result = "open"
    if any(t["verdict"] == "BLOCK" for t in tickets):
        result = "cap-reached" if tickets[0]["round"] >= 2 else "open"
    elif any(t["fallback"] and t["verdict"] == "UNREVIEWED" for t in tickets):
        result = "stopped-unreviewed"
    elif tickets and all(t["status"] == "closed" for t in tickets):
        resolved = {t["fallback_for"] for t in tickets
                    if t["fallback"] and t["verdict"] == "PASS"}
        unresolved = any(t["verdict"] == "UNREVIEWED" and t["id"] not in resolved
                         for t in tickets)
        if not unresolved and any(t["verdict"] == "PASS" for t in tickets):
            result = "passed"
    db.execute("UPDATE items SET status = ? WHERE id = ?", (result, item))
    return result, tickets[0]["round"] if tickets else 0


def open_item(db, args):
    with transaction(db):
        if db.execute("SELECT 1 FROM items WHERE id = ?", (args.item,)).fetchone():
            refuse("Item already exists; the owner must choose a new item ID for a new goal.")
        db.execute("INSERT INTO items VALUES (?, ?, 'open', ?)",
                   (args.item, args.goal, stamp()))


def start(db, args):
    with transaction(db):
        item = require_row(db, "items", args.item)
        tickets = current_tickets(db, args.item)
        round_number = tickets[0]["round"] if tickets else 0
        fallback_for = None
        if args.join:
            if not any(t["status"] in ("open", "running") for t in tickets):
                refuse("No active round to join; the owner must start a new round.")
            if not same_commit(args.commit, tickets[0]["reviewed_commit"]):
                refuse("Join must review the round's first commit; the owner must use that commit.")
            if len(tickets) >= 3:
                refuse("Round already has 3 tickets; the owner must wait and resolve this round.")
        elif args.fallback:
            closed = [t for t in tickets if t["status"] == "closed"]
            latest = max(closed, key=lambda t: (t["closed_at"], t["rowid"])) if closed else None
            if any(t["fallback"] for t in tickets):
                refuse("Fallback already used; the owner must resolve this round or approve another.")
            if latest is None or latest["verdict"] != "UNREVIEWED":
                refuse("Fallback requires the latest closed ticket to be UNREVIEWED; "
                       "the owner must record the failed review first.")
            if latest["reviewer"] == args.reviewer:
                refuse("Fallback requires a different reviewer CLI; the owner must select one.")
            if not same_commit(args.commit, latest["reviewed_commit"]):
                refuse("Fallback must review the failed ticket's commit; "
                       "the owner must use that commit.")
            fallback_for = latest["id"]
        else:
            if db.execute("""SELECT 1 FROM tickets WHERE item = ? AND
                    (status != 'closed' OR verdict IS NULL)""", (args.item,)).fetchone():
                refuse("Previous tickets must be fully closed; the owner must record "
                       "each pending verdict first.")
            round_number += 1
            if (round_number > 2 or item["status"] in ("cap-reached", "stopped-unreviewed")) \
                    and not args.owner_approval:
                refuse("Item {} stopped at round {}; the owner must supply --owner-approval "
                       "<Discord message ID> for a further round.".format(args.item, round_number - 1))
        ticket = uuid.uuid4().hex
        db.execute("""INSERT INTO tickets
            (id, item, round, reviewer, reviewed_commit, status, issued_at,
             joined, fallback, fallback_for, owner_approval, note)
            VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
                   (ticket, args.item, round_number, args.reviewer, args.commit, stamp(),
                    int(args.join), int(args.fallback), fallback_for, args.owner_approval, args.note))
        recompute(db, args.item)
    print(ticket)


def same_commit(left, right):
    return left.startswith(right) or right.startswith(left)


def kill_group(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def run(db, args):
    with transaction(db):
        ticket = require_row(db, "tickets", args.ticket)
        if ticket["status"] != "open":
            refuse("Ticket must be open; the owner must use a new ticket within the round policy.")
        db.execute("UPDATE tickets SET status='running', started_at=?, argv=? WHERE id=?",
                   (stamp(), json.dumps(args.argv), args.ticket))
    env = os.environ.copy()
    env["MAGI_REVIEW_TICKET"] = args.ticket
    try:
        child = subprocess.Popen(args.argv, env=env, start_new_session=True)
    except OSError as exc:
        print("review-gate: cannot execute child: {}".format(exc), file=sys.stderr)
        code = 127 if isinstance(exc, FileNotFoundError) else 126
    else:
        try:
            code = child.wait(timeout=args.timeout)
            if code < 0:
                code = 128 - code
        except subprocess.TimeoutExpired:
            kill_group(child)
            code = 124
        except KeyboardInterrupt:
            kill_group(child)
            code = 130
    with transaction(db):
        # A concurrent close must never be undone by completion of the child.
        db.execute("""UPDATE tickets SET status=CASE WHEN status='running' THEN 'ran' ELSE status END,
            ended_at=?, exit_code=? WHERE id=?""", (stamp(), code, args.ticket))
    return code


def close(db, args):
    with transaction(db):
        ticket = require_row(db, "tickets", args.ticket)
        if ticket["status"] == "closed":
            refuse("Ticket is already closed; the owner must preserve the recorded verdict.")
        db.execute("""UPDATE tickets SET status='closed', closed_at=?, verdict=?, blockers=?,
            evidence=?, reason=? WHERE id=?""",
                   (stamp(), args.verdict, args.blockers, args.evidence, args.reason, args.ticket))
        status, round_number = recompute(db, ticket["item"])
    if status in ("cap-reached", "stopped-unreviewed"):
        print("Owner notice: item {} {} at round {}; a further round needs --owner-approval "
              "<Discord message ID>.".format(ticket["item"], status, round_number), file=sys.stderr)


def check_ticket(db, args):
    reason = None
    reuse = False
    with transaction(db, write=args.consume):
        ticket = db.execute("SELECT * FROM tickets WHERE id=?", (args.ticket,)).fetchone()
        if ticket is None:
            reason = "Ticket not found; the owner must issue a valid ticket within the round policy."
        elif args.consume and db.execute(
                "SELECT 1 FROM ticket_consumptions WHERE ticket=?", (args.ticket,)).fetchone():
            reuse = True
            reason = "Ticket already consumed; the owner must issue a new ticket."
        elif args.consume and ticket["status"] != "running":
            reason = "Ticket must be running; the owner must invoke the review via review_gate.py run."
        elif ticket["status"] not in ("open", "running") or not timedelta(0) <= (
                now() - parse_time(ticket["issued_at"])) < TTL:
            reason = ("Ticket is closed, ran, expired, or future-dated; the owner must resolve "
                      "the ledger and issue a valid ticket within the round policy.")
        if args.consume:
            if reason:
                detail = "{} ticket={} reason={}".format(
                    "ticket-reuse" if reuse else "ticket-refused", args.ticket, reason)
                db.execute("""INSERT INTO events(kind, detail, cwd, user, created_at)
                    VALUES ('rejected', ?, ?, ?, ?)""",
                           (detail, os.getcwd(), os.environ.get("USER", ""), stamp()))
            else:
                db.execute("INSERT INTO ticket_consumptions VALUES (?, ?)", (args.ticket, stamp()))
    # Raise only after committing: a policy refusal must preserve its audit event.
    if reason:
        refuse(reason)


def status(db, args):
    with transaction(db, write=False):
        item = dict(require_row(db, "items", args.item))
        tickets = [dict(t) for t in db.execute(
            "SELECT * FROM tickets WHERE item=? ORDER BY round, rowid", (args.item,))]
    rounds = max((t["round"] for t in tickets), default=0)
    if args.json:
        print(json.dumps({"item": item, "tickets": tickets, "rounds_used": rounds}))
        return
    for ticket in tickets:
        line = "item={} round={} reviewer={} commit={} verdict={}".format(
            args.item, ticket["round"], ticket["reviewer"], ticket["reviewed_commit"][:12],
            ticket["verdict"] or "pending")
        for flag in ("fallback", "join"):
            if ticket["fallback" if flag == "fallback" else "joined"]:
                line += " " + flag
        if ticket["owner_approval"]:
            line += " owner-approval=" + ticket["owner_approval"]
        print(line)
    print("status={} rounds_used={}/2".format(item["status"], rounds))


def log_event(db, args):
    with transaction(db):
        db.execute("INSERT INTO events(kind, detail, cwd, user, created_at) VALUES (?, ?, ?, ?, ?)",
                   (args.kind, args.detail, os.getcwd(), os.environ.get("USER", ""), stamp()))


def audit(db, args):
    with transaction(db, write=False):
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]
        items = [dict(row) for row in db.execute(
            "SELECT * FROM items WHERE status IN ('cap-reached', 'stopped-unreviewed') ORDER BY id")]
        tickets = [dict(row) for row in db.execute(
            "SELECT * FROM tickets WHERE status IN ('open', 'running', 'ran') ORDER BY rowid")]
        # Include current stopped items even if they were opened before --since.
        if args.since:
            events = [e for e in events if e["created_at"][:10] >= args.since]
        cutoff = now() - TTL
        tickets = [t for t in tickets if parse_time(t["issued_at"]) < cutoff]
        if args.run_context:
            # Weekly audit only: read execution windows without exposing argv.
            runs = [dict(row) for row in db.execute("""SELECT id AS ticket, reviewer,
                started_at, ended_at FROM tickets WHERE started_at IS NOT NULL ORDER BY rowid""")]
            over_round_items = [dict(row) for row in db.execute("""SELECT item, MAX(round) AS round
                FROM tickets GROUP BY item HAVING MAX(round) > 2 ORDER BY item""")]
    result = {"events": events, "items": items, "stale_tickets": tickets}
    if args.run_context:
        result.update(runs=runs, over_round_items=over_round_items)
    if args.json:
        print(json.dumps(result))
    else:
        for kind, rows in result.items():
            for row in rows:
                print(kind + " " + json.dumps(row, sort_keys=True))


def parser():
    root = Parser(description="Review tickets and ledger: two rounds per item; owner approval beyond.")
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    p = commands.add_parser("open", help="create a new work item")
    p.add_argument("--item", required=True)
    p.add_argument("--goal", required=True)
    p = commands.add_parser("start", help="issue a review ticket")
    p.add_argument("--item", required=True)
    p.add_argument("--reviewer", required=True, choices=REVIEWERS)
    p.add_argument("--commit", required=True)
    flags = p.add_mutually_exclusive_group()
    flags.add_argument("--join", action="store_true")
    flags.add_argument("--fallback", action="store_true")
    p.add_argument("--owner-approval")
    p.add_argument("--note")
    p = commands.add_parser("run", help="run argv with a ticket, without a shell")
    p.add_argument("--ticket", required=True)
    p.add_argument("--timeout", type=float)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    p = commands.add_parser("close", help="record PASS, BLOCK, or UNREVIEWED")
    p.add_argument("--ticket", required=True)
    p.add_argument("--verdict", choices=("PASS", "BLOCK", "UNREVIEWED"), required=True)
    p.add_argument("--blockers", type=int)
    p.add_argument("--evidence")
    p.add_argument("--reason")
    p = commands.add_parser("check-ticket", help="silently validate an open/running ticket (<6h)")
    p.add_argument("--consume", action="store_true", help="atomically claim a running ticket once")
    p.add_argument("ticket")
    p = commands.add_parser("status", help="list an item's complete review ledger")
    p.add_argument("--item", required=True)
    p.add_argument("--json", action="store_true")
    p = commands.add_parser("log-event", help="append a shim audit event")
    p.add_argument("--kind", choices=("bypass", "legacy-exempt", "rejected"), required=True)
    p.add_argument("--detail", required=True)
    p = commands.add_parser("audit", help="list events, stopped items and stale tickets")
    p.add_argument("--run-context", action="store_true",
                   help="read-only execution windows and rounds for weekly session matching")
    p.add_argument("--since", help="UTC date YYYY-MM-DD (filters events; current issues always shown)")
    p.add_argument("--json", action="store_true")
    return root


def validate(args):
    def pattern(name, value, regex):
        if not re.fullmatch(regex, value):
            raise GateError(3, "Invalid {}: {}".format(name, value))

    if hasattr(args, "item"):
        pattern("item ID", args.item, r"[a-z0-9][a-z0-9._-]{2,63}")
    if args.command == "open" and not args.goal.strip():
        raise GateError(3, "--goal must not be blank")
    if args.command == "start":
        pattern("commit SHA", args.commit, r"[0-9a-f]{7,40}")
        if args.owner_approval is not None:
            pattern("owner approval ID", args.owner_approval, r"[0-9]{15,22}")
    if args.command == "close":
        if args.blockers is not None and args.blockers < 1:
            raise GateError(3, "--blockers must be >= 1")
        if args.verdict == "BLOCK" and args.blockers is None:
            raise GateError(3, "BLOCK requires --blockers N (>= 1)")
        if args.verdict == "UNREVIEWED" and not (args.reason and args.reason.strip()):
            raise GateError(3, "UNREVIEWED requires --reason")
    if args.command == "run":
        if args.argv[:1] == ["--"]:
            args.argv = args.argv[1:]
        if not args.argv:
            raise GateError(3, "run requires -- ARGV...")
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise GateError(3, "--timeout must be a finite number > 0")
    if args.command == "audit" and args.since is not None:
        pattern("since date", args.since, r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            raise GateError(3, "Invalid --since date")


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        validate(args)  # Invalid input must not create or modify the database.
        if args.command == "audit" and args.run_context:
            path = Path(os.environ.get(
                "REVIEW_GATE_DB", "~/.local/state/review-gate/ledger.sqlite")).expanduser().resolve()
            db = sqlite3.connect(path.as_uri() + "?mode=ro", isolation_level=None)
            db.row_factory = sqlite3.Row
        else:
            db = connect()
        try:
            commands = {"open": open_item, "start": start, "run": run, "close": close,
                        "check-ticket": check_ticket, "status": status,
                        "log-event": log_event, "audit": audit}
            return commands[args.command](db, args) or 0
        finally:
            db.close()
    except GateError as exc:
        print("review-gate: " + str(exc), file=sys.stderr)
        return exc.code
    except (OSError, sqlite3.Error, ValueError) as exc:
        print("review-gate: storage/execution error: {}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
