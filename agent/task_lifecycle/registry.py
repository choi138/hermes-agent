"""Durable reservations with an append-only lifecycle event ledger."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import json
import math
import sqlite3
import threading
import time
from uuid import uuid4

from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

from .contract import TaskContract
from .types import ALLOWED_TRANSITIONS, LifecycleError, Phase


@dataclass(frozen=True)
class Submission:
    run_id: str
    outcome: str
    phase: Phase


@dataclass(frozen=True)
class Run:
    run_id: str
    phase: Phase
    contract: TaskContract
    start_evidence: dict | None = None
    exit_code: int | None = None
    evidence: dict = field(default_factory=dict)


def _start_evidence(evidence):
    if not isinstance(evidence, Mapping):
        raise LifecycleError("start_evidence must be a mapping")
    pid, started, executor = (evidence.get(key) for key in ("pid", "started_at", "executor"))
    if type(pid) is not int or pid <= 0:
        raise LifecycleError("start_evidence requires a positive pid")
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        raise LifecycleError("start_evidence requires a finite process start timestamp")
    if not isinstance(executor, str) or not executor.strip():
        raise LifecycleError("start_evidence requires an executor identifier")
    return dict(evidence)


def _encoded(evidence):
    try:
        return json.dumps(evidence, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("Evidence must be finite JSON data") from exc


def _contract_encoded(value):
    return _encoded({k:v for k,v in value.items()
                     if not (k in {"execution_digest", "base_revision"} and v is None)})


def _decoded(encoded):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LifecycleError("Duplicate JSON evidence key")
            result[key] = value
        return result

    def nonfinite(value):
        raise LifecycleError("Nonfinite JSON evidence")

    return json.loads(encoded, object_pairs_hook=pairs, parse_constant=nonfinite)


def _evidence(phase, evidence):
    if evidence is not None and not isinstance(evidence, Mapping):
        raise LifecycleError("Evidence must be a mapping or null")
    if evidence is not None:
        evidence = json.loads(_encoded(dict(evidence)))
    if phase is Phase.RUNNING:
        _start_evidence(evidence)
    if phase is Phase.EXECUTION_FINISHED:
        if not isinstance(evidence, dict) or type(evidence.get("exit_code")) is not int:
            raise LifecycleError("Execution finish requires an integer exit_code")
    required = {
        Phase.VERIFYING: ("verification_ref",),
        Phase.VERIFIED: ("artifact_revision", "acceptance_digest"),
        Phase.DELIVERING: ("delivery_ref",),
        # A transport ACK, never a claim that a human read the content.
        Phase.DELIVERED: ("message_id", "channel_id", "content_digest"),
    }
    for key in required.get(phase, ()):
        if (not isinstance(evidence, dict) or not isinstance(evidence.get(key), str)
                or not evidence[key].strip() or "\0" in evidence[key]):
            raise LifecycleError(f"{phase.value} requires nonempty {key}")
    return evidence


def _transition(current, phase, evidence, previous_evidence):
    if (current is Phase.UNKNOWN and phase is Phase.EXECUTION_FINISHED
            and isinstance(evidence, dict) and evidence.get("reconciled_receipt") is True):
        return
    if phase == current:
        if _encoded(evidence) != _encoded(previous_evidence):
            raise LifecycleError("Same-phase evidence conflict")
        return
    if phase not in ALLOWED_TRANSITIONS[current]:
        raise LifecycleError(f"Invalid transition: {current.value} -> {phase.value}")


class Registry:
    SCOPE = "task_lifecycle"

    def __init__(self, db_path=None):
        if db_path is None:
            from hermes_constants import get_hermes_home
            db_path = get_hermes_home() / "state.db"
        self.db_path = str(db_path)
        if str(db_path) == ":memory:":
            raise LifecycleError("Registry requires a durable database path")
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                evidence TEXT,
                recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS lifecycle_events_v2_run ON lifecycle_events(run_id, id);
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_v2_no_update
                BEFORE UPDATE ON lifecycle_events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_v2_no_delete
                BEFORE DELETE ON lifecycle_events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
        """)
        # Migrate the earlier opt-in draft ledger only when its exact schema
        # and lifecycle-owned marker are present. Never adopt an unrelated
        # shared state.db table named events. Preserve the original table.
        columns = tuple(row[1] for row in self._conn.execute("PRAGMA table_info(events)"))
        marker = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='lifecycle_events_no_update' AND tbl_name='events'").fetchone()
        if columns == ("id", "run_id", "phase", "evidence", "recorded_at") and marker:
            with self._conn:
                self._conn.execute("INSERT INTO lifecycle_events(run_id,phase,evidence,recorded_at) "
                    "SELECT run_id,phase,evidence,recorded_at FROM events "
                    "WHERE run_id NOT IN (SELECT run_id FROM lifecycle_events) ORDER BY id")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS lifecycle_jobs (
            run_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            owner TEXT, worker TEXT, result TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
        )""")
        self._conn.commit()
        self._store = RunIdempotencyStore(str(db_path))
        if not self._store.durable:
            self.close()
            raise LifecycleError("Idempotency storage is not durable")

    def close(self):
        self._store.close()
        self._conn.close()

    def submit(self, contract: TaskContract) -> Submission:
        if not isinstance(contract, TaskContract):
            raise LifecycleError("Validated TaskContract is required")
        contract.validate()
        outcome, stored = self._store.reserve(
            self.SCOPE, contract.idempotency_key(), contract.digest(), str(uuid4()),
            {"phase": Phase.ACCEPTED.value, "contract": asdict(contract)},
        )
        run = self.lookup(stored["run_id"])
        return Submission(run.run_id, outcome, run.phase)

    def find_by_contract(self, contract):
        contract.validate()
        outcome, stored = self._store.lookup(self.SCOPE, contract.idempotency_key(), contract.digest())
        if stored is None:
            return None
        run = self.lookup(stored["run_id"])
        return Submission(run.run_id, outcome, run.phase)

    def _append(self, run_id, phase, evidence):
        self._conn.execute(
            "INSERT INTO lifecycle_events(run_id, phase, evidence, recorded_at) VALUES(?, ?, ?, ?)",
            (run_id, phase.value, json.dumps(evidence, sort_keys=True, allow_nan=False), time.time()),
        )

    def _replay(self, run_id):
        rows = self._conn.execute(
            "SELECT phase, evidence FROM lifecycle_events WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()
        if not rows:
            return None
        try:
            if rows[0][0] != Phase.ACCEPTED.value:
                raise LifecycleError("Ledger must begin with accepted")
            initial = _decoded(rows[0][1])
            if not isinstance(initial, dict) or set(initial) != {"contract"}:
                raise LifecycleError("Accepted event requires a contract")
            values = dict(initial["contract"])
            for key in ("allowed_paths", "forbidden_actions", "acceptance_checks"):
                if not isinstance(values[key], list):
                    raise LifecycleError("Malformed stored contract sequence")
                values[key] = tuple(values[key])
            contract = TaskContract(**values)
            if _contract_encoded(asdict(contract)) != _contract_encoded(initial["contract"]):
                raise LifecycleError("Stored contract paths are no longer canonical")
            current, previous = Phase.ACCEPTED, initial
            retained = {current: initial}
            for value, encoded in rows[1:]:
                phase = Phase(value)
                evidence = _evidence(phase, _decoded(encoded))
                _transition(current, phase, evidence, previous)
                retained[phase] = evidence
                current, previous = phase, evidence
            return Run(run_id, current, contract, retained.get(Phase.RUNNING),
                       (retained.get(Phase.EXECUTION_FINISHED) or {}).get("exit_code"), retained)
        except (TypeError, ValueError, KeyError) as exc:
            raise LifecycleError("Malformed lifecycle ledger") from exc

    def lookup(self, run_id):
        # Reserve commits independently. Recover its accepted event if a caller
        # crashed between reservation and journaling; never admit another launch.
        try:
            stored = self._store.status_for_run(self.SCOPE, run_id)
            if stored is not None:
                status = stored["status"]
                if status["phase"] != Phase.ACCEPTED.value:
                    raise LifecycleError("Invalid reservation phase")
                contract = status["contract"]
        except (TypeError, ValueError, KeyError) as exc:
            raise LifecycleError("Malformed lifecycle reservation") from exc
        if stored is None:
            return None
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            run = self._replay(run_id)
            if run is None:
                self._append(run_id, Phase.ACCEPTED, {"contract": contract})
                run = self._replay(run_id)
            if _contract_encoded(asdict(run.contract)) != _contract_encoded(contract):
                raise LifecycleError("Ledger contract does not match reservation")
            return run

    def open_runs(self):
        with self._lock:
            ids = self._conn.execute(
                "SELECT run_id FROM run_idempotency WHERE scope=? ORDER BY created_at", (self.SCOPE,),
            ).fetchall()
        return [run for (run_id,) in ids if (run := self.lookup(run_id)).phase not in {Phase.DELIVERED, Phase.CANCELLED}]

    def mark_ready(self, run_id):
        return self.record_phase(run_id, Phase.READY)

    def mark_running(self, run_id, *, start_evidence=None):
        return self.record_phase(run_id, Phase.RUNNING, evidence=start_evidence)

    def mark_execution_finished(self, run_id, *, exit_code):
        return self.record_phase(run_id, Phase.EXECUTION_FINISHED, evidence={"exit_code": exit_code})

    def record_phase(self, run_id, phase, *, evidence=None):
        try:
            phase = Phase(phase)
        except (TypeError, ValueError) as exc:
            raise LifecycleError("Unknown lifecycle phase") from exc
        evidence = _evidence(phase, evidence)
        if isinstance(evidence, dict) and "reconciled_receipt" in evidence:
            raise LifecycleError("Recovery evidence requires durable receipt reconciliation")
        if self.lookup(run_id) is None:
            raise LifecycleError(f"Unknown run: {run_id}")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._replay(run_id)
            if current.contract.execution_digest is not None:
                self._validate_managed_evidence(current, phase, evidence)
            _transition(current.phase, phase, evidence, current.evidence[current.phase])
            if phase == current.phase:
                return phase
            self._append(run_id, phase, evidence)
        return phase

    def _validate_managed_evidence(self, current, phase, evidence):
        from .contract import _digest
        if phase is Phase.VERIFIED:
            from .verification import snapshot
            if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_verifications'").fetchone():
                raise LifecycleError("No durable acceptance evidence")
            row = self._conn.execute("SELECT contract_digest,evidence FROM lifecycle_verifications WHERE run_id=?", (current.run_id,)).fetchone()
            if not row:
                raise LifecycleError("No durable acceptance evidence")
            verified = _decoded(row[1])
            remote = self.job(current.run_id)['payload'].get('remote_snapshot')
            actual = remote['artifact'] if remote else snapshot(current.contract.workdir, tuple(verified["artifact"]["files"]))
            if (row[0] != current.contract.digest() or not verified.get("accepted")
                    or evidence.get("acceptance_digest") != _digest(verified)
                    or evidence.get("artifact_revision") != verified["artifact"]["revision"]
                    or actual != verified["artifact"]):
                raise LifecycleError("Acceptance does not match current artifact/contract")
        if phase in {Phase.DELIVERING, Phase.DELIVERED}:
            if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_handoffs'").fetchone():
                raise LifecycleError("No matching durable handoff")
            row = self._conn.execute("SELECT obligation_id,receipt FROM lifecycle_handoffs WHERE run_id=?", (current.run_id,)).fetchone()
            if row is None or (phase is Phase.DELIVERING and evidence.get("delivery_ref") != row[0]):
                raise LifecycleError("No matching durable handoff")
            if phase is Phase.DELIVERED and (row[1] is None or _decoded(row[1]) != evidence):
                raise LifecycleError("No matching transport readback receipt")

    def reconcile_execution(self, run_id):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            current = self._replay(run_id)
            result = self.job(run_id)["result"]
            if (current is None or current.phase not in {Phase.RUNNING, Phase.UNKNOWN}
                    or not current.start_evidence or not result
                    or type(result.get("process_returncode")) is not int):
                raise LifecycleError("No durable execution receipt to reconcile")
            self._append(run_id, Phase.EXECUTION_FINISHED,
                         {"exit_code": result["process_returncode"], "reconciled_receipt": True})


    def prepare_job(self, run_id, payload):
        """Durable launch intent. It is immutable after the first submit."""
        if self.lookup(run_id) is None:
            raise LifecycleError("Unknown run")
        encoded = _encoded(payload)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT payload FROM lifecycle_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row and row[0] != encoded:
                raise LifecycleError("Execution payload conflict")
            self._conn.execute("INSERT OR IGNORE INTO lifecycle_jobs(run_id,payload) VALUES(?,?)", (run_id, encoded))

    def job(self, run_id):
        with self._lock:
            row = self._conn.execute("SELECT payload,owner,worker,result,cancel_requested FROM lifecycle_jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise LifecycleError("No durable execution intent")
        return dict(payload=_decoded(row[0]), owner=row[1],
                    worker=_decoded(row[2]) if row[2] else None,
                    result=_decoded(row[3]) if row[3] else None, cancel_requested=bool(row[4]))

    def claim_job(self, run_id, owner, worker):
        """Only one detached supervisor may launch the workload; never expire."""
        with self._lock, self._conn:
            return self._conn.execute("UPDATE lifecycle_jobs SET owner=?,worker=? WHERE run_id=? AND owner IS NULL",
                                      (owner, _encoded(worker), run_id)).rowcount == 1

    def finish_job(self, run_id, owner, result):
        with self._lock, self._conn:
            row = self._conn.execute("SELECT owner,result FROM lifecycle_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row[0] != owner:
                raise LifecycleError("Execution generation mismatch")
            encoded = _encoded(result)
            if row[1] is not None and row[1] != encoded:
                raise LifecycleError("Execution receipt is immutable")
            self._conn.execute("UPDATE lifecycle_jobs SET result=? WHERE run_id=? AND owner=?", (encoded, run_id, owner))

    def request_cancel(self, run_id):
        with self._lock, self._conn:
            self._conn.execute("UPDATE lifecycle_jobs SET cancel_requested=1 WHERE run_id=?", (run_id,))
