"""Durable delivery-obligation ledger for gateway final responses (rows in the shared ``state.db``;
WAL, owner pid + process-start liveness, bounded retention) so a crash between finalize and
platform ACK cannot lose a response silently. Checkpoints: record_obligation() 'pending' before
any send | mark_attempting() 'attempting' right before the await | mark_delivered() 'delivered'
only on SendResult.success | mark_failed() 'failed' on a definitive rejection. Crash semantics
(never silently resend an ambiguous send): pending = never started, redeliver plainly; attempting
= crashed mid-await, platform MAY have it, redeliver WITH a visible recovered marker; failed =
rejected once, restart is a retry boundary, also marked; delivered = prune. Attempts are capped
and stale rows expire, both -> 'abandoned' (kept briefly, then pruned). Everything is
best-effort: ledger failures must never block a send; callers wrap every call in try/except.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from gateway.dead_targets import classify_dead_error
from hermes_cli.sqlite_util import add_column_if_missing
from hermes_constants import get_process_hermes_home

logger = logging.getLogger(__name__)
_DB_LOCK = threading.Lock()

# Redelivery policy knobs (deliberately not config — the ledger is gated by
# ``gateway.delivery_ledger`` and these only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500

# Visible prefixes for redeliveries that might duplicate an already-received message (crash mid-send /
# post-rejection retry) — honest at-least-once. Runtime recovery uses a distinct marker: no restart
# occurred, but a network rejection's acknowledgement can still have been lost independently.
RECOVERED_MARKER = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
RECONNECTED_MARKER = ("♻️ Recovered reply — the messaging platform reconnected after the original "
                      "delivery failed, so this may be a duplicate:\n\n")
# A reply refused by flood control may have gone out as several requests (the adapter chunks long replies,
# and MarkdownV2 escaping alone can push a reply that fits one message into two), and the platform may have
# accepted the first chunk(s) before refusing the rest; the send result does not say which landed. The raw
# length of the stored text says nothing about that, so every flood redelivery carries a marker, and neither
# of the markers above tells the truth here (no restart, no reconnect): the rate limit gets its own.
FLOOD_MARKER = ("♻️ Recovered reply — the messaging platform's rate limit refused the original, so part of "
                "it may already have arrived above:\n\n")

# Errors whose send contract proves the platform never saw the request: retried as soon as the adapter
# is back, no backoff. Every other rejection is retried too (#91653: a 5xx or a transient parse error
# used to strand the reply in ``failed`` until the next restart), but only after a backoff that grows
# with the attempts already spent, so a platform-side outage is not hammered by the redelivery timer.
# A whole-chat death (blocked bot, deleted group, deactivated user) is never retried: the target is gone.
_RUNTIME_RETRYABLE_ERRORS = frozenset({"send_path_degraded", "lifecycle_deferred"})
# One tier per in-process retry; the last budgeted attempt is left to the boot sweep (retry_not_before).
_RETRY_BACKOFF_SECONDS = (30.0, 120.0)
assert len(_RETRY_BACKOFF_SECONDS) == MAX_ATTEMPTS - 1

# A final send the platform refused with flood control is the other transient case: a 429 means the
# refused request was never accepted, and the platform said how long to wait. Adapters fail such sends
# closed as ``flood_control:<seconds>`` on purpose (#91969) so that this ledger owns the wait instead of
# the send coroutine sleeping through it. Before this the row simply sat in ``failed`` until the next
# restart's sweep, which then redelivered it hours late under the "gateway restarted during delivery"
# marker.
FLOOD_ERROR_PREFIX = "flood_control:"
FLOOD_RETRY_DEFAULT_SECONDS = 60.0
FLOOD_RETRY_CAP_SECONDS = 15 * 60.0
FLOOD_RETRY_SLACK_SECONDS = 2.0

# The canonical prefix above is what the adapters produce for a flood they decide not to sleep. It is
# not the only shape that reaches this ledger. PTB raises ``RetryAfter``, whose own text reads
# "Flood control exceeded. Retry in 185 seconds", and that text is what lands in ``last_error``
# whenever a send fails on a path that has not been normalized, or was written by an older build
# before it was. Such a row has to be recognised too: unrecognised, it is treated as an ordinary
# failure, so no redelivery timer is armed for it and a boot sweep claims it immediately instead of
# adopting it until its deadline, spending the one attempt inside the penalty that caused it.
# Matching requires the "flood control" wording as well as the delay, so an unrelated error that
# merely suggests retrying is never mistaken for a flood.
_RAW_FLOOD_RE = re.compile(r"flood control exceeded.*?retry in\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _raw_flood_wait(text: str) -> Optional[float]:
    """Seconds asked for by a flood error still carrying the platform's own wording, else ``None``."""
    match = _RAW_FLOOD_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def is_reconnect_only(error: Any) -> bool:
    """True for a row that only an adapter reconnect may retry (no timer, no backoff)."""
    return str(error or "").strip().lower() in _RUNTIME_RETRYABLE_ERRORS


def is_flood_error(error: Any) -> bool:
    """True for a flood refusal: the adapters' fail-closed ``flood_control:<seconds>`` result, or a
    row still carrying the platform's own flood wording (see ``_RAW_FLOOD_RE``)."""
    text = str(error or "").strip().lower()
    return text.startswith(FLOOD_ERROR_PREFIX) or _raw_flood_wait(text) is not None


def flood_wait_seconds(error: Any, default: float = FLOOD_RETRY_DEFAULT_SECONDS) -> float:
    """The wait the platform asked for, read from ``flood_control:<seconds>``; ``default`` when unreadable."""
    text = str(error or "").strip().lower()
    wait = default
    if text.startswith(FLOOD_ERROR_PREFIX):
        try:
            wait = float(text[len(FLOOD_ERROR_PREFIX):].strip())
        except ValueError:
            wait = default
    else:
        # A row still carrying the platform's own flood wording states its delay just as precisely,
        # and the deadline must use it rather than the generic default.
        raw = _raw_flood_wait(text)
        if raw is not None:
            wait = raw
    return wait if wait > 0 else default


def flood_retry_delay(seconds: Any) -> float:
    """How long a redelivery timer sleeps for a wait of ``seconds``: capped so a huge or bogus value cannot
    park the timer for hours (the row's own deadline, not the timer, decides eligibility when it fires),
    plus a little slack so the timer lands after the deadline rather than on it."""
    try:
        wait = float(seconds)
    except (TypeError, ValueError):
        wait = FLOOD_RETRY_DEFAULT_SECONDS
    return min(max(wait, 0.0), FLOOD_RETRY_CAP_SECONDS) + FLOOD_RETRY_SLACK_SECONDS


def _failed_stamp(updated_at: Any) -> float:
    try:
        return float(updated_at or 0.0)
    except (TypeError, ValueError):
        return 0.0


def flood_not_before(updated_at: Any, last_error: Any) -> float:
    """Earliest moment a flood-refused row may be resent: the refusal's timestamp (``mark_failed`` sets
    ``updated_at``) plus the platform's wait. Enforced by the sweeps so neither an early timer nor a
    reconnect sweep spends a redelivery attempt inside the penalty window."""
    return _failed_stamp(updated_at) + flood_wait_seconds(last_error)


def retry_not_before(updated_at: Any, last_error: Any, attempts: Any) -> Optional[float]:
    """Earliest moment a failed row may be resent, or ``None`` for a row the runtime must leave alone:
    a flood refusal keeps the platform's own wait, an allowlisted reconnect error is due at once, a
    whole-chat death is final, and any other rejection backs off by the attempts already spent — but
    never spends the LAST budgeted attempt. An unclassified outage can outlast any timer, and a row
    the timer abandoned would be lost for good; leaving one attempt keeps it recoverable by the boot
    sweep after a restart, which is a real recovery signal."""
    if is_flood_error(last_error):
        return flood_not_before(updated_at, last_error)
    text = str(last_error or "").strip().lower()
    if is_reconnect_only(text):
        return _failed_stamp(updated_at)
    if classify_dead_error(text):
        return None
    spent = int(attempts or 0)
    if spent >= MAX_ATTEMPTS - 1:
        return None
    return _failed_stamp(updated_at) + _RETRY_BACKOFF_SECONDS[spent]


def _db_path():
    # Launch home, not get_hermes_home(): a multiplexed gateway records a served profile's replies
    # under that profile's home override, but the boot sweep reads from the launch context, so both
    # must open the one shared store (adapter_profile tells the bots apart). No get_hermes_home()
    # fallback for an unset HERMES_HOME: a default gateway run in the foreground has none.
    return get_process_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    from hermes_cli.sqlite_util import open_db

    # Shared state.db: SessionDB owns the durable PRAGMA set; this opener keeps the plain-tuple rows
    # and the 10 s busy timeout it always had.
    return open_db(_db_path(), db_label="state.db (delivery_ledger)", busy_timeout_ms=10_000,
                   row_factory=None, initialize=_initialize_schema)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT,
            turn_id TEXT
        )"""
    )
    if "adapter_profile" not in {row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")}:
        add_column_if_missing(conn, "delivery_obligations", "adapter_profile", "adapter_profile TEXT")
    if "turn_id" not in {row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")}:
        add_column_if_missing(conn, "delivery_obligations", "turn_id", "turn_id TEXT")
    if "response_kind" not in {row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")}:
        # NULL is deliberately ambiguous: old rows must still prevent unsafe replay.
        add_column_if_missing(conn, "delivery_obligations", "response_kind", "response_kind TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_obligations_turn "
        "ON delivery_obligations(session_key, turn_id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligation_receipts (
            obligation_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            observed_at REAL NOT NULL
        )"""
    )


def _transaction():
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect())


def _start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time  # lazy: tests monkeypatch gateway.status
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    return pid, _start_time(pid)


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    current_start = _start_time(pid)
    if current_start is None:
        # Start time unreadable: alive iff the pid exists. Route through the cross-platform probe — on Windows
        # ``os.kill(pid, 0)`` is NOT a no-op (bpo-14484: it maps to ``GenerateConsoleCtrlEvent(0, pid)`` and could
        # Ctrl+C the gateway's own console group). ``_pid_exists`` keeps EPERM-means-alive (pid owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                return False  # never fall back to a raw sig-0 probe on Windows
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
                return True
            except OSError as exc:  # incl. ProcessLookupError; EPERM means the pid exists
                return isinstance(exc, PermissionError)
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    try:
        from gateway.status import start_time_fingerprints_match
        return started_at is None or start_time_fingerprints_match(started_at, current_start)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while distinct threads/topics on one
    chat never collide (session_key carries platform/chat/thread; ``message_ref`` = inbound message id)."""
    return hashlib.sha256(f"{session_key}|{message_ref}|{content}".encode("utf-8", "replace")).hexdigest()[:24]


def record_obligation(*, obligation_id: str, session_key: str, platform: str, chat_id: str,
                      thread_id: Optional[str], content: str, adapter_profile: Optional[str] = None,
                      preserve_existing: bool = False, turn_id: Optional[str] = None,
                      response_kind: Optional[str] = None) -> None:
    """Record an answer or typed notice as owed to the platform (state='pending')."""
    now, (pid, started) = time.time(), _owner_stamp()
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    with _DB_LOCK, _transaction() as conn:
        if preserve_existing:
            existing = conn.execute(
                "SELECT session_key,platform,chat_id,thread_id,content,adapter_profile,turn_id,response_kind "
                "FROM delivery_obligations WHERE obligation_id=?", (obligation_id,),
            ).fetchone()
            expected = (session_key, platform, str(chat_id), str(thread_id) if thread_id else None,
                        content, stored_profile)
            if existing is not None:
                if (tuple(existing[:6]) != expected or (turn_id and existing[6] not in (None, turn_id))
                        or existing[7] != response_kind):
                    raise ValueError("Delivery obligation payload conflict")
                if turn_id and existing[6] is None:
                    conn.execute(
                        "UPDATE delivery_obligations SET turn_id=? WHERE obligation_id=?",
                        (turn_id, obligation_id),
                    )
                return
        conn.execute(
            """INSERT OR REPLACE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile, turn_id, response_kind)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?)""",
            (obligation_id, session_key, platform, str(chat_id), str(thread_id) if thread_id else None,
             content, now, now, pid, started, stored_profile, turn_id, response_kind))
        _prune_unlocked(conn, now)


def record_crash_left_reply(*, obligation_id: str, session_key: str, platform: str, chat_id: str,
                            thread_id: Optional[str], content: str, since: float,
                            adapter_profile: Optional[str] = None) -> None:
    """Adopt a reply a killed process persisted but never ledgered. Unowned, so this boot's sweep
    claims it, and 'attempting', because a streamed reply may already be on screen: it is
    redelivered once, with the recovered marker. A no-op when the same reply was already ledgered
    since *since* (the turn start), and idempotent across boots that die before their sweep."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, adapter_profile)
               SELECT ?, ?, ?, ?, ?, ?, 'attempting', 0, ?, ?, NULL, NULL, ?
               WHERE NOT EXISTS (SELECT 1 FROM delivery_obligations
                                 WHERE session_key = ? AND content = ? AND created_at >= ?)""",
            (obligation_id, session_key, platform, str(chat_id), str(thread_id) if thread_id else None,
             content, now, now, str(adapter_profile).strip() if adapter_profile else "default",
             session_key, content, since))


def has_turn_obligation(session_key: str, turn_id: str) -> bool:
    """Whether this turn has an answer (or legacy ambiguous output), never just a notice.

    Successful answers remain conclusive across attempts; replaying a task that already
    produced one risks duplicate effects. Only explicitly typed notices are excluded.
    """
    if not session_key or not turn_id:
        return False
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(
            "SELECT 1 FROM delivery_obligations WHERE session_key=? AND turn_id=? "
            "AND (response_kind IS NULL OR response_kind NOT IN ('failure_notice', 'interruption_notice')) LIMIT 1",
            (session_key, turn_id),
        ).fetchone() is not None


def turn_delivery_state(session_key: str, turn_id: str) -> Optional[str]:
    """Exact turn's delivery evidence for durable process follow-up reconciliation."""
    if not session_key or not turn_id:
        return None
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            "SELECT state FROM delivery_obligations WHERE session_key=? AND turn_id=?",
            (session_key, turn_id),
        ).fetchall()
    if not rows:
        return None
    if any(row[0] == 'abandoned' for row in rows):
        return 'abandoned'
    if any(row[0] == 'uncertain' for row in rows):
        return 'uncertain'
    return 'delivered' if all(row[0] == 'delivered' for row in rows) else 'pending'


def claim_pending_obligation(obligation_id: str) -> bool:
    """Admit a first send once; ambiguous/rejected sends belong to existing recovery."""
    allowed = process_attention_authorized(obligation_id)
    if allowed is not True:
        if allowed is False:
            abandon_obligation(obligation_id)
        return False
    with _DB_LOCK, _transaction() as conn:
        return conn.execute("UPDATE delivery_obligations SET state='attempting',updated_at=? WHERE obligation_id=? AND state='pending'",
                            (time.time(), obligation_id)).rowcount == 1


def abandon_obligation(obligation_id: str) -> None:
    """Fence retries after cancellation; preserve an already observed delivery."""
    with _DB_LOCK, _transaction() as conn:
        conn.execute("UPDATE delivery_obligations SET state='abandoned',updated_at=? WHERE obligation_id=? AND state!='delivered'",
                     (time.time(), obligation_id))


def public_obligation_id(obligation_id):
    """Log execution identity without the attention authorization capability."""
    if obligation_id.startswith('process-attention:'):
        return ':'.join(obligation_id.split(':')[:2])
    return obligation_id


def _followups_home_override(session_key):
    """Home whose state DB holds the follow-ups of the profile that RUNS *session_key*.

    The ledger lives in the launch home (see ``_db_path``) while follow-ups are written under the
    runtime profile's home, so a multiplexed gateway must not read follow-ups through the ledger
    connection: that table is empty there and every report would be abandoned unsent. The owner
    comes from the ``agent:<profile>:...`` key, never from ``adapter_profile`` (the delivery bot,
    which differs from the runtime profile for a shared bot routing to a satellite). Everything
    else -- ``agent:main`` and unparseable keys -- belongs to the launch home, exactly the store
    the ledger connection used to read; resolving it explicitly keeps a sweep that inherited a
    satellite's scope from reading the satellite's store for default-profile rows.
    """
    parts = str(session_key or '').split(':')
    if len(parts) < 2 or parts[0] != 'agent' or parts[1] in ('', 'main'):
        return get_process_hermes_home()
    from gateway.session import profile_from_session_key_namespace
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir(profile_from_session_key_namespace(parts[1]))


def _read_followups_as(session_key, read):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(_followups_home_override(session_key))
    try:
        return read()
    finally:
        reset_hermes_home_override(token)


def _continuation_notice_as(session_key, execution, connection, *, delivered=False):
    from tools.process_registry_continuation import settings, notice_pending, acknowledge_notice
    from tools.process_registry_followups import _db

    def access():
        if not delivered and not settings()[0]:
            return None
        operation = acknowledge_notice if delivered else notice_pending
        if _followups_home_override(session_key) / 'state.db' == _db_path():
            return operation(execution, connection=connection)
        with _db(read_only=not delivered) as followups:
            return operation(execution, connection=followups)

    return _read_followups_as(session_key, access)


def process_attention_authorized(obligation_id, *, connection=None):
    """The state DB fences process-report retries even if ledger cleanup failed."""
    if obligation_id.startswith('process-continuation-stop-write:'):
        from tools.process_registry_continuation import settings
        try:
            if connection is not None:
                row = connection.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                                         (obligation_id,)).fetchone()
            else:
                with _DB_LOCK, _transaction() as conn:
                    row = conn.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                                       (obligation_id,)).fetchone()
            if not row:
                return False
            return _read_followups_as(row[0], lambda: True if settings()[0] else None)
        except (sqlite3.Error, OSError, ValueError):
            logger.warning('Stop-write notice authorization unavailable: %s', obligation_id, exc_info=True)
            return None
    if obligation_id.startswith('process-continuation-stop:'):
        execution = obligation_id.removeprefix('process-continuation-stop:')
        def read(conn):
            row = conn.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                               (obligation_id,)).fetchone()
            return _continuation_notice_as(row[0], execution, conn) if row else False
        try:
            if connection is not None:
                return read(connection)
            with _DB_LOCK, _transaction() as conn:
                return read(conn)
        except (sqlite3.Error, OSError, ValueError):
            logger.warning('Continuation notice authorization unavailable: %s', execution, exc_info=True)
            return None
    if obligation_id.startswith('process-report:'):
        try:
            from tools.process_registry_followups import report_authorized
            def read(conn):
                return conn.execute('SELECT session_key,turn_id FROM delivery_obligations WHERE obligation_id=?', (obligation_id,)).fetchone()
            if connection is not None:
                row = read(connection)
            else:
                with _DB_LOCK, _transaction() as conn:
                    row = read(conn)
            if not row:
                return False
            return _read_followups_as(row[0], lambda: report_authorized(row[0], row[1]))
        except (sqlite3.Error, OSError, ValueError):
            logger.warning('Process report authorization unavailable: %s', obligation_id, exc_info=True)
            return None
    if not obligation_id.startswith('process-attention:'):
        return True
    parts = obligation_id.split(':')
    if len(parts) != 3:
        return False
    from tools.process_registry_followups import attention_authorized
    from gateway.process_followups import attention_inflight
    if attention_inflight(obligation_id):
        return None
    try:
        if connection is not None:
            row = connection.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                                     (obligation_id,)).fetchone()
        else:
            with _DB_LOCK, _transaction() as conn:
                row = conn.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                                   (obligation_id,)).fetchone()
        return _read_followups_as(row[0] if row else None, lambda: attention_authorized(parts[1], parts[2]))
    except (sqlite3.Error, OSError, ValueError) as exc:
        from tools.environments.ssh_process import safe_error
        logger.warning('Process attention authorization unavailable: %s: %s', public_obligation_id(obligation_id), safe_error(exc))
        return None  # Retry later; unavailable is neither authorized nor cancelled.


def mark_attempting(obligation_id: str) -> None:
    _update_state(obligation_id, "attempting")


def mark_delivered(obligation_id: str, *, message_id=None) -> None:
    if obligation_id.startswith('process-continuation-stop:'):
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute('SELECT session_key FROM delivery_obligations WHERE obligation_id=?',
                               (obligation_id,)).fetchone()
            conn.execute("UPDATE delivery_obligations SET state='delivered',updated_at=?,last_error=NULL WHERE obligation_id=?",
                         (time.time(), obligation_id))
            if message_id is not None:
                conn.execute('INSERT OR REPLACE INTO delivery_obligation_receipts VALUES (?,?,?)',
                             (obligation_id, str(message_id), time.time()))
            if row:
                _continuation_notice_as(row[0], obligation_id.removeprefix('process-continuation-stop:'),
                                        conn, delivered=True)
        return
    if message_id is None:
        _update_state(obligation_id, "delivered")
        return
    with _DB_LOCK, _transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO delivery_obligation_receipts VALUES (?,?,?)",
                     (obligation_id, str(message_id), time.time()))
        conn.execute("UPDATE delivery_obligations SET state='delivered',updated_at=?,last_error=NULL WHERE obligation_id=? AND state!='abandoned'",
                     (time.time(), obligation_id))


def mark_uncertain(obligation_id: str, error: str = '') -> None:
    """Process-report transport may have accepted the send; require reconciliation."""
    if not obligation_id.startswith(('process-attention:', 'process-report:', 'delegation-progress:')):
        raise ValueError('uncertain state is reserved for process reports')
    with _DB_LOCK, _transaction() as conn:
        conn.execute("UPDATE delivery_obligations SET state='uncertain',updated_at=?,last_error=? WHERE obligation_id=? AND state NOT IN ('delivered','abandoned')",
                     (time.time(), error[:500] if error else None, obligation_id))


def mark_failed(obligation_id: str, error: str = "") -> None:
    _update_state(obligation_id, "failed", error=error)


def defer_lifecycle_claim(obligation_id: str, *, attempts: int) -> bool:
    """Defer a busy lifecycle finalizer without changing its bounded claim budget.

    Only this process's exact claim may be deferred. A concurrent completion,
    newer claim, or another gateway's row is never overwritten.
    """
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', updated_at=?, last_error='lifecycle_deferred'
               WHERE obligation_id=? AND state IN ('pending', 'attempting', 'failed')
                 AND owner_pid IS ? AND owner_started_at IS ? AND attempts=?""",
            (time.time(), obligation_id, pid, started, attempts),
        )
    return bool(cursor.rowcount)


def release_runtime_claim(obligation_id: str, error: str = "") -> bool:
    """Return an unsent runtime claim to ``failed`` without spending an attempt.

    Runtime recovery claims before clearing ``resume_pending`` so two reconnect paths cannot send the
    same row; if the flag cannot be cleared no send was attempted and the claim must not consume the
    redelivery budget. Fail-closed to the exact current process instance and ``attempting`` state."""
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?, last_error=?
               WHERE obligation_id=? AND state='attempting'
                 AND owner_pid IS ? AND owner_started_at IS ?""",
            (time.time(), error[:500] if error else None, obligation_id, pid, started))
    return bool(cursor.rowcount)


def _update_state(obligation_id: str, state: str, error: str = "") -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?
               WHERE obligation_id=? AND (state!='abandoned' OR (obligation_id NOT LIKE 'process-attention:%' AND obligation_id NOT LIKE 'process-report:%'))""",
            (state, time.time(), error[:500] if error else None, obligation_id))


def _claimed_row(oid, session_key, platform, chat_id, thread_id, content, attempts, profile, *,
                 needs_marker: bool, runtime: bool = False, flood: bool = False,
                 last_error: Optional[str] = None, turn_id: Optional[str] = None,
                 response_kind: Optional[str] = None) -> dict[str, Any]:
    """Claimed-row dict handed back for redelivery. A marked row names its own cause: ``flood`` (a reply
    the rate limit refused, possibly after accepting part of it) gets FLOOD_MARKER at boot or at runtime, a
    ``runtime`` reconnect replay gets RECONNECTED_MARKER, and a boot-recovered crash keeps the runner's
    restart marker default. ``last_error`` is the row's pre-claim error, carried so a runtime claim that is
    released unsent goes back to ``failed`` with the same error and keeps its retry eligibility."""
    marker = FLOOD_MARKER if flood else (RECONNECTED_MARKER if runtime else None)
    return {"obligation_id": oid, "session_key": session_key, "platform": platform, "chat_id": chat_id,
            "thread_id": thread_id, "content": content, "needs_marker": needs_marker,
            "turn_id": turn_id, "response_kind": response_kind,
            **({"marker": marker} if needs_marker and marker else {}), "profile": profile,
            **({"runtime_recovery": True} if runtime else {}),
            **({"last_error": last_error} if last_error else {}), "attempts": attempts + 1}


def sweep_recoverable(now: Optional[float] = None, *, deliverable_platforms: Optional[set] = None,
                      deliverable_targets: Optional[set] = None) -> list[dict[str, Any]]:
    """Claim undelivered rows owned by dead processes; return them for redelivery.

    Claiming atomically re-stamps the owner to THIS process, moves the row to 'attempting' and increments
    ``attempts`` (the UPDATE is guarded on the previous owner stamp, so a second gateway racing the same
    sweep cannot double-claim).
    Rows over the attempts cap or stale cutoff become 'abandoned'. ``deliverable_platforms`` restricts
    claiming to platforms the caller can send on this boot: ``attempts`` is the redelivery budget and
    must only be spent on a real send, else a platform that failed to connect burns one attempt per boot
    and hits the cap having never been sent once (the stale cutoff still bounds untouched rows).
    ``deliverable_targets`` further scopes multiplexed gateways by exact ``(platform, adapter_profile)``
    so one connected bot cannot spend another disconnected bot's retry budget.

    A flood-refused row still inside its wait is adopted (owner re-stamped, no attempt spent) and
    returned flagged ``adopted`` with its ``not_before``: the caller clears its session's resume flag
    like any other claimed row, since the answer is in the ledger, but must not send it; the flood
    timer does once the wait has passed. A legacy row without ``adapter_profile`` is normalised to
    ``'default'`` on claim or adoption (the caller only accepts such rows when it is not multiplexed),
    because the runtime sweep matches profiles exactly and could otherwise never claim it."""
    now, (pid, started) = now if now is not None else time.time(), _owner_stamp()
    claimed: list[dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at,
                      owner_pid, owner_started_at, adapter_profile, last_error, updated_at, turn_id, response_kind
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state, attempts, created_at,
             owner_pid, owner_started_at, adapter_profile, last_error, updated_at, turn_id, response_kind) in rows:
            if oid.startswith("delegation-progress:"):
                continue  # Only the remote outbox observer may revalidate or acknowledge this generation.
            allowed = process_attention_authorized(oid, connection=conn)
            if allowed is not True:
                if allowed is False:
                    conn.execute("UPDATE delivery_obligations SET state='abandoned',updated_at=? WHERE obligation_id=?", (now, oid))
                continue
            if _owner_alive(owner_pid, owner_started_at):
                continue  # a live gateway still owns this row
            if oid.startswith(('process-attention:', 'process-report:')) and state == 'attempting':
                conn.execute("UPDATE delivery_obligations SET state='uncertain',updated_at=?,last_error=? WHERE obligation_id=? AND state='attempting'",
                             (now, 'Previous owner exited during process report send; inspect conversation before any manual resend', oid))
                continue
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:  # exhausted -> abandoned
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=? WHERE obligation_id=?""", (now, oid))
                continue
            if ((deliverable_platforms is not None and platform not in deliverable_platforms)
                    or (deliverable_targets is not None and (platform, adapter_profile) not in deliverable_targets)):
                continue  # no adapter this boot — claiming would spend an attempt on a no-op
            flood_row = state == "failed" and is_flood_error(last_error)
            if flood_row and now < flood_not_before(updated_at, last_error):
                # Still inside the platform's wait: adopt the dead owner's row without spending an attempt
                # (state and error kept) so this process's flood timer can claim it once the wait passes.
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET owner_pid=?, owner_started_at=?,
                           adapter_profile=COALESCE(adapter_profile, 'default')
                       WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                    (pid, started, oid, owner_pid, owner_pid))
                if cursor.rowcount:
                    claimed.append({
                        "obligation_id": oid, "session_key": session_key, "platform": platform,
                        "chat_id": chat_id, "thread_id": thread_id, "content": content,
                        "profile": adapter_profile or "default", "attempts": attempts,
                        "adopted": True, "not_before": flood_not_before(updated_at, last_error),
                        "turn_id": turn_id, "response_kind": response_kind})
                continue
            # Every claim starts a send, so the row leaves 'pending'/'failed' for 'attempting' in the same
            # CAS: a boot killed inside the redelivery then leaves proof the platform may have it (next boot
            # marks it), and the runtime sweep, which only takes 'failed', cannot re-claim it mid-send. A
            # claimed flood row also drops its stale refusal, so an interrupted resend has no error.
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET owner_pid=?, owner_started_at=?, attempts=attempts+1, updated_at=?,
                       adapter_profile=COALESCE(adapter_profile, 'default'), state='attempting',
                       last_error=CASE WHEN ? THEN NULL ELSE last_error END
                   WHERE obligation_id=? AND (owner_pid IS ? OR owner_pid=?)""",
                (pid, started, now, 1 if flood_row else 0, oid, owner_pid, owner_pid))
            if cursor.rowcount:
                # A never-claimed pending row was never sent: redeliver plainly. Anything else (crashed
                # mid-await, other rejection, a flood refusal whose earlier chunks the platform may have
                # accepted, or a pending row an older build already claimed and may have sent) carries
                # the marker.
                claimed.append(_claimed_row(oid, session_key, platform, chat_id, thread_id, content, attempts,
                                            adapter_profile or "default", needs_marker=state != "pending" or attempts > 0,
                                            flood=flood_row, turn_id=turn_id, response_kind=response_kind))
    return claimed


def sweep_failed_for_runtime(platform: str, now: Optional[float] = None, *,
                             profile: Optional[str] = None) -> list[dict[str, Any]]:
    """Claim this process's failed rows that are due for another send, for one adapter.

    ``profile`` scopes multiplexed gateways to the bot identity that owned the failed send (``None`` =
    primary/default adapter); unowned rows and rows owned by another process are left for the
    startup/dead-owner sweep. Startup recovery ignores rows owned by a live gateway, so a response
    rejected with ``send_path_degraded`` would stay stranded when only the adapter reconnects; this closes
    that gap without weakening ownership: only rows stamped to this exact process instance, only rows
    past their ``retry_not_before`` deadline, same attempts/staleness bounds, every update guarded by the
    prior owner stamp and ``failed`` state. Claimed rows always carry a marker (the failed send's ack is not safe to
    infer): the reconnect one, or the rate-limit one for a flood-refused row."""
    now, (pid, started) = now if now is not None else time.time(), _owner_stamp()
    if started is None:  # PID alone cannot distinguish this process from a stale row left after PID
        return []        # reuse; runtime replay is optional, so fail closed (startup recovery remains).
    expected_profile = "default" if not profile or profile == "default" else str(profile)
    claimed: list[dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile, updated_at, turn_id, response_kind
               FROM delivery_obligations
               WHERE state='failed' AND platform=?""", (platform,)).fetchall()
        for (oid, session_key, row_platform, chat_id, thread_id, content, attempts, created_at,
             owner_pid, owner_started_at, last_error, adapter_profile, updated_at, turn_id, response_kind) in rows:
            allowed = process_attention_authorized(oid, connection=conn)
            if allowed is not True:
                if allowed is False:
                    conn.execute("UPDATE delivery_obligations SET state='abandoned',updated_at=? WHERE obligation_id=?", (now, oid))
                continue
            # Exact process-start matching prevents PID reuse from stealing work.
            if adapter_profile != expected_profile or owner_pid != pid or owner_started_at != started:
                continue
            due = retry_not_before(updated_at, last_error, attempts)
            if due is None:
                continue
            owner_guard = (now, oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:  # exhausted -> abandoned
                conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""", owner_guard)
                continue
            if now < due:
                continue  # the platform's wait or the backoff has not passed; the timer comes back for it
            # The claim clears the stale error: this is a fresh attempt, and if it is interrupted the next
            # boot must see 'attempting' with no proof of non-delivery, hence the marker.
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='attempting', attempts=attempts+1, updated_at=?, last_error=NULL
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?""", owner_guard)
            if cursor.rowcount:
                # The failed send's ack may have been lost (reconnect) or its earlier chunks accepted
                # (flood): every runtime redelivery carries a marker. The pre-claim error rides along so a
                # claim released unsent keeps its flood retry eligibility.
                claimed.append(_claimed_row(oid, session_key, row_platform, chat_id, thread_id, content,
                                            attempts, adapter_profile, needs_marker=True, runtime=True,
                                            flood=is_flood_error(last_error), last_error=last_error,
                                            turn_id=turn_id, response_kind=response_kind))
    return claimed


def pending_retries(now: Optional[float] = None) -> list[dict[str, Any]]:
    """This process's failed rows that still await redelivery, one entry per adapter identity with the
    earliest deadline (``not_before``). The runner arms one redelivery timer per entry, so a row adopted
    at boot, skipped because its wait had not passed, or rejected again is never stranded. Rows past the
    attempts cap or stale cutoff are left for the sweeps to abandon."""
    now, (pid, started) = now if now is not None else time.time(), _owner_stamp()
    if started is None:
        return []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT platform, adapter_profile, updated_at, last_error, attempts, created_at
               FROM delivery_obligations
               WHERE state='failed' AND owner_pid IS ? AND owner_started_at IS ?""", (pid, started)).fetchall()
    earliest: dict[tuple, float] = {}
    for platform, adapter_profile, updated_at, last_error, attempts, created_at in rows:
        # Reconnect-only rows (a claim released because the adapter was gone) are re-claimed by the
        # reconnect sweep; a timer would claim and release them every tick until the adapter is back.
        if is_reconnect_only(last_error):
            continue
        due = retry_not_before(updated_at, last_error, attempts)
        if due is None or attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
            continue
        key = (platform, adapter_profile or "default")
        if key not in earliest or due < earliest[key]:
            earliest[key] = due
    return [{"platform": platform, "profile": profile, "not_before": due}
            for (platform, profile), due in sorted(earliest.items())]


def _prune_unlocked(conn, now: float) -> None:
    """Retention DELETEs on the caller's open connection — must run inside the caller's transaction."""
    conn.execute(
        """DELETE FROM delivery_obligations
           WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""", (now - _RETENTION_SECONDS,))
    total = conn.execute("SELECT COUNT(*) FROM delivery_obligations").fetchone()[0]
    if total > _MAX_ROWS:
        conn.execute(
            """DELETE FROM delivery_obligations WHERE obligation_id IN (
                 SELECT obligation_id FROM delivery_obligations
                 ORDER BY CASE state
                            WHEN 'delivered' THEN 0
                            WHEN 'abandoned' THEN 1
                            ELSE 2
                          END, updated_at ASC
                 LIMIT ?)""", (total - _MAX_ROWS,))
    # Receipt evidence remains while its obligation exists, including unsettled sends.
    conn.execute(
        """DELETE FROM delivery_obligation_receipts
           WHERE NOT EXISTS (
               SELECT 1 FROM delivery_obligations
               WHERE delivery_obligations.obligation_id = delivery_obligation_receipts.obligation_id
           )"""
    )


def ledger_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config
            config = load_config()
        value = (config.get("gateway") or {}).get("delivery_ledger", True)
        return value.strip().lower() not in {"false", "0", "no", "off"} if isinstance(value, str) else bool(value)
    except Exception:
        return True
