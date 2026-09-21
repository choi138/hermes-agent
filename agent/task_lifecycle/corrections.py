"""In-memory correction evidence, separate from context delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .types import LifecycleError


@dataclass(frozen=True)
class Correction:
    correction_id: str
    statement: str
    rule_text: str
    environments: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "environments", tuple(self.environments))


@dataclass(frozen=True)
class CheckOutcome:
    correction_id: str
    environment: str
    observed: str
    evidence: str


class CorrectionLedger:
    """Track reports with evidence, without claiming to independently judge them.

    Latest observation per environment determines current support. Regression
    history is retained even if a later observation reports recovery.
    """

    def __init__(self) -> None:
        self._corrections: dict[str, Correction] = {}
        self._deliveries: dict[str, list[str]] = {}
        self._outcomes: dict[str, list[CheckOutcome]] = {}

    def _get(self, correction_id: str) -> Correction:
        if correction_id not in self._corrections:
            raise LifecycleError(f"unknown correction: {correction_id}")
        return self._corrections[correction_id]

    def record(self, correction: Correction) -> None:
        if correction.correction_id in self._corrections:
            raise LifecycleError("correction already recorded")
        self._corrections[correction.correction_id] = correction
        self._deliveries[correction.correction_id] = []
        self._outcomes[correction.correction_id] = []

    def mark_delivered(self, correction_id: str, *, pack_digest: str) -> None:
        self._get(correction_id)
        if not isinstance(pack_digest, str) or not pack_digest.strip():
            raise LifecycleError("delivery requires a pack digest")
        self._deliveries[correction_id].append(pack_digest)

    def observe(self, outcome: CheckOutcome) -> None:
        correction = self._get(outcome.correction_id)
        if outcome.environment not in correction.environments:
            raise LifecycleError("observation environment is not in correction scope")
        if outcome.observed not in {"followed", "violated", "not_exercised"}:
            raise LifecycleError("unknown observation result")
        if not isinstance(outcome.evidence, str) or not outcome.evidence.strip():
            raise LifecycleError("observation requires evidence")
        self._outcomes[outcome.correction_id].append(outcome)

    def status(self, correction_id: str) -> dict:
        """Here means at least one scoped environment's latest check followed.

        Everywhere requires a latest followed check in every declared environment;
        an empty environment set never implies verified compliance.
        """
        correction = self._get(correction_id)
        latest = {o.environment: o.observed for o in self._outcomes[correction_id]}
        return {
            "recorded": True,
            "delivered": bool(self._deliveries[correction_id]),
            "followed_here": "followed" in latest.values(),
            "followed_everywhere": bool(correction.environments) and all(
                latest.get(env) == "followed" for env in correction.environments
            ),
        }

    def regressions(self) -> list[Correction]:
        """Return corrections with any recorded violation, including history."""
        return [c for key, c in self._corrections.items()
                if any(o.observed == "violated" for o in self._outcomes[key])]

    def unverified(self) -> list[Correction]:
        """Return delivered corrections with no observation of any kind."""
        return [c for key, c in self._corrections.items()
                if self._deliveries[key] and not self._outcomes[key]]


class PersistentCorrectionLedger:
    """Profile-scoped evidence, not a new policy authority or auto-learning claim.

    New policy revisions retire prior delivery/compliance evidence. Sourced
    proposals can be recorded, but only explicitly confirmed rules are rendered.
    """
    def __init__(self, db_path=None):
        import sqlite3
        from hermes_constants import get_hermes_home
        self.db_path = str(db_path or get_hermes_home() / 'state.db')
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS lifecycle_corrections (
                correction_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(correction_id, revision));
            CREATE TABLE IF NOT EXISTS lifecycle_correction_evidence (
                correction_id TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT NOT NULL,
                environment TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(correction_id,revision,run_id,kind));
        ''')

    def close(self):
        self._conn.close()

    def record(self, *, correction_id, revision, source, rule_text, scope, environments,
               confirmed=False, work_classes=(), exceptions=()):
        import json
        if (not correction_id or type(revision) is not int or revision < 1 or not source or not rule_text
                or set(scope) != {'owner', 'project', 'profile'} or not all(scope.values())
                or not environments or len(set(environments)) != len(environments) or type(confirmed) is not bool):
            raise LifecycleError('Correction requires source, version, exact scope and evaluation environments')
        data = dict(correction_id=correction_id, revision=revision, source=source, rule_text=rule_text,
                    scope=scope, environments=list(environments), confirmed=confirmed,
                    work_classes=list(work_classes), exceptions=list(exceptions))
        with self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            prior = self._conn.execute('SELECT MAX(revision) FROM lifecycle_corrections WHERE correction_id=?', (correction_id,)).fetchone()[0]
            if prior is not None and revision <= prior:
                raise LifecycleError('Correction revision must increase; preserve the source history')
            self._conn.execute('INSERT INTO lifecycle_corrections VALUES(?,?,?)',
                               (correction_id, revision, json.dumps(data, sort_keys=True)))
        return data

    def get(self, correction_id):
        import json
        row = self._conn.execute('SELECT data FROM lifecycle_corrections WHERE correction_id=? ORDER BY revision DESC LIMIT 1', (correction_id,)).fetchone()
        if not row:
            raise LifecycleError('Unknown correction')
        return json.loads(row[0])

    def for_task(self, ids, *, scope, work_class):
        items = []
        for correction_id in ids:
            item = self.get(correction_id)
            if item['scope'] != scope or not item['confirmed']:
                continue
            if item['work_classes'] and work_class not in item['work_classes']:
                continue
            items.append(item)
        return items

    def observe_run(self, correction_id, run_id, *, check_name=None):
        """Derive delivery/behavior from real runner/check receipts, never a score."""
        import hashlib
        import json
        from .registry import Registry
        from .verification import evidence
        registry = Registry(self.db_path)
        try:
            correction = self.get(correction_id)
            job, run = registry.job(run_id), registry.lookup(run_id)
            grant = job['payload']
            scope = dict(owner=run.contract.owner, project=run.contract.repo_root, profile=run.contract.profile)
            selected = grant['context'].get('corrections', [])
            if correction['scope'] != scope or not any(i == correction for i in selected):
                raise LifecycleError('Correction version/scope was not in this task input')
            receipt = (job['result'] or {}).get('input_receipt', {})
            if not receipt.get('pipe_complete') or receipt.get('sha256') != hashlib.sha256(grant['prompt'].encode()).hexdigest():
                raise LifecycleError('No complete executor input receipt')
            environment = run.contract.repo_root + ':' + grant['request']['cli']
            if environment not in correction['environments']:
                raise LifecycleError('Unplanned correction evaluation environment')
            kind, result = 'delivered', dict(input_receipt=receipt)
            if check_name is not None:
                check = next((c for c in grant['checks'] if c['name'] == check_name), None)
                expected = {'id':correction_id, 'revision':correction['revision']}
                if check is None or check.get('correction') != expected or check['kind'] not in {'test', 'boundary'}:
                    raise LifecycleError('Behavior check was not bound to the correction before execution')
                verification = evidence(registry, run_id)
                observed = next((c for c in (verification or {}).get('checks', []) if c['name'] == check_name), None)
                if not observed:
                    raise LifecycleError('Behavior check has not executed')
                fresh = observed['revision'] == verification['artifact']['revision']
                result = dict(observed=('followed' if observed['passed'] else 'violated') if fresh else 'not_exercised',
                              evidence=observed, artifact_revision=verification['artifact']['revision'])
                # A run may be verified again after a failed check. Preserve
                # each observation and deduplicate only the identical receipt.
                kind = 'behavior:' + hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
            with self._conn:
                self._conn.execute('INSERT OR IGNORE INTO lifecycle_correction_evidence VALUES(?,?,?,?,?,?)',
                    (correction_id, correction['revision'], run_id, environment, kind, json.dumps(result, sort_keys=True)))
            return result
        finally:
            registry.close()

    def status(self, correction_id):
        import json
        item = self.get(correction_id)
        rows = self._conn.execute('SELECT environment,kind,data FROM lifecycle_correction_evidence WHERE correction_id=? AND revision=? ORDER BY rowid',
                                  (correction_id,item['revision'])).fetchall()
        observations = [(env, json.loads(data)) for env, kind, data in rows
                        if kind == 'behavior' or kind.startswith('behavior:')]
        # Re-reading an older run must not supersede a newer executed check.
        observations.sort(key=lambda item: item[1]['evidence'].get('finished_at', 0))
        latest = {env:data['observed'] for env, data in observations}
        return dict(recorded=True, confirmed=item['confirmed'], revision=item['revision'],
                    delivered=bool(rows), followed_here='followed' in latest.values(),
                    followed_everywhere=bool(item['environments']) and all(latest.get(e)=='followed' for e in item['environments']),
                    environments=latest)
