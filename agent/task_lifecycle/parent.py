"""Parent objectives and authoritative completion, in the profile lifecycle DB.

This module is a trusted intake boundary, never a model tool. Source identities
come from authenticated gateway messages, check commands from operator grants.
Child phases and process-followup verdicts cannot complete an objective.
"""
import json
import time
from uuid import uuid4

from .contract import _digest
from .registry import _decoded, _encoded
from .types import LifecycleError


def _text(name, value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise LifecycleError(f'{name} requires nonempty text')
    return value


def _outcomes(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise LifecycleError('Explicit required outcomes are necessary')
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {'id', 'description', 'checks'}:
            raise LifecycleError('Outcome requires id, description and checks')
        identity = _text('outcome id', value['id'])
        _text('outcome description', value['description'])
        if identity in seen or not isinstance(value['checks'], list):
            raise LifecycleError('Duplicate outcome or invalid checks')
        seen.add(identity)
        checks = value['checks']
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {'kind', 'name'}
                    or check['kind'] not in {'test', 'review', 'boundary'}):
                raise LifecycleError('Invalid required check')
            _text('check name', check['name'])
        if len({_digest(c) for c in checks}) != len(checks):
            raise LifecycleError('Duplicate required check')
        result.append(json.loads(_encoded(value)))
    return result


class ParentLedger:
    """Minimal additive extension of Registry; callers own its connection."""

    def __init__(self, registry, *, remote_snapshot=None):
        self.registry = registry
        # Trusted transport callback, never a model-provided artifact payload.
        self.remote_snapshot = remote_snapshot
        with registry._lock, registry._conn:
            registry._conn.executescript('''
                CREATE TABLE IF NOT EXISTS lifecycle_parents (
                    parent_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                    profile TEXT NOT NULL, owner TEXT NOT NULL, origin TEXT NOT NULL,
                    original_request TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(session_key,profile,origin)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_contracts (
                    parent_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    contract TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(parent_id,revision)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_executions (
                    execution_id TEXT PRIMARY KEY, parent_id TEXT,
                    session_key TEXT NOT NULL, profile TEXT NOT NULL,
                    owner TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT,
                    attribution TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_coverage (
                    parent_id TEXT NOT NULL, contract_digest TEXT NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(parent_id,contract_digest)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_execution_links (
                    execution_id TEXT NOT NULL, contract_digest TEXT NOT NULL,
                    parent_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    attribution TEXT NOT NULL,
                    PRIMARY KEY(execution_id,contract_digest)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_decisions (
                    decision_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                    evidence TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_terminal_submissions (
                    submission_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, evidence TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_parent_supersessions (
                    execution_id TEXT NOT NULL, contract_digest TEXT NOT NULL,
                    parent_id TEXT NOT NULL, run_id TEXT NOT NULL, evidence TEXT NOT NULL,
                    PRIMARY KEY(execution_id,contract_digest)
                );
            ''')
            for table in ('parents', 'parent_contracts', 'parent_executions', 'parent_execution_links',
                          'parent_coverage', 'parent_decisions', 'parent_terminal_submissions',
                          'parent_supersessions'):
                for action in ('UPDATE', 'DELETE'):
                    registry._conn.execute(f'''CREATE TRIGGER IF NOT EXISTS lifecycle_{table}_no_{action.lower()}
                        BEFORE {action} ON lifecycle_{table} BEGIN
                        SELECT RAISE(ABORT, 'parent evidence is append-only'); END''')

    def begin(self, *, session_key, profile, owner, origin, original_request, required_outcomes=None):
        """Authenticated request intake; duplicate messages preserve the contract."""
        for key, value in locals().copy().items():
            if key in {'session_key', 'profile', 'owner', 'origin', 'original_request'}:
                _text(key, value)
        outcomes = _outcomes(required_outcomes or [dict(
            id='original_request', description=original_request, checks=[])])
        contract = dict(revision=1, original_request=original_request,
                        sources=[dict(origin=origin, text=original_request)], outcomes=outcomes)
        r = self.registry
        with r._lock, r._conn:
            r._conn.execute('BEGIN IMMEDIATE')
            row = r._conn.execute('SELECT parent_id,owner,original_request FROM lifecycle_parents '
                                 'WHERE session_key=? AND profile=? AND origin=?', (session_key, profile, origin)).fetchone()
            if row:
                if row[1:] != (owner, original_request):
                    raise LifecycleError('Authenticated parent request conflict')
                parent_id = row[0]
            else:
                parent_id = str(uuid4())
                r._conn.execute('INSERT INTO lifecycle_parents VALUES(?,?,?,?,?,?,?)',
                    (parent_id, session_key, profile, owner, origin, original_request, time.time()))
                r._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
                    (parent_id, 1, _encoded(contract), _digest(contract)))
        return self.get(parent_id)

    def get(self, parent_id):
        r = self.registry
        with r._lock:
            row = r._conn.execute('SELECT session_key,profile,owner,origin,original_request '
                                 'FROM lifecycle_parents WHERE parent_id=?', (parent_id,)).fetchone()
            version = r._conn.execute('SELECT contract,digest FROM lifecycle_parent_contracts '
                                     'WHERE parent_id=? ORDER BY revision DESC LIMIT 1', (parent_id,)).fetchone()
        if not row or not version:
            raise LifecycleError('Unknown parent objective')
        contract = _decoded(version[0])
        if _digest(contract) != version[1]:
            raise LifecycleError('Parent contract digest mismatch')
        return dict(parent_id=parent_id, session_key=row[0], profile=row[1], owner=row[2],
                    origin=row[3], original_request=row[4], contract=contract, contract_digest=version[1])

    def current(self, *, session_key, profile, owner):
        with self.registry._lock:
            row = self.registry._conn.execute('SELECT parent_id FROM lifecycle_parents '
                'WHERE session_key=? AND profile=? AND owner=? ORDER BY created_at DESC LIMIT 1',
                (session_key, profile, owner)).fetchone()
        return self.get(row[0]) if row else None

    def bind_checks(self, parent_id, *, required_outcomes):
        """Trusted intake may strengthen checks without a new user scope message.

        Existing outcome text/IDs and every previously required check survive.
        Deletion or rewording must use authenticated scope revision instead.
        """
        r = self.registry
        proposed = _outcomes(required_outcomes)
        with r._lock, r._conn:
            r._conn.execute('BEGIN IMMEDIATE')
            parent = self.get(parent_id)
            old = {v['id']: v for v in parent['contract']['outcomes']}
            new = {v['id']: v for v in proposed}
            if set(old) - set(new):
                raise LifecycleError('Cannot remove required outcomes without a user scope source')
            for identity, value in old.items():
                if (new[identity]['description'] != value['description']
                        or {_digest(c) for c in value['checks']} - {_digest(c) for c in new[identity]['checks']}):
                    raise LifecycleError('Cannot weaken an outcome without a user scope source')
            if proposed != parent['contract']['outcomes']:
                contract = dict(parent['contract'], revision=parent['contract']['revision'] + 1, outcomes=proposed)
                r._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
                    (parent_id, contract['revision'], _encoded(contract), _digest(contract)))
        return self.get(parent_id)

    def revise(self, parent_id, *, owner, origin, source_text, required_outcomes):
        """Only authenticated user scope messages may replace required outcomes."""
        _text('scope origin', origin)
        _text('scope source', source_text)
        r = self.registry
        with r._lock, r._conn:
            r._conn.execute('BEGIN IMMEDIATE')
            parent = self.get(parent_id)
            if owner != parent['owner'] or origin in {s['origin'] for s in parent['contract']['sources']}:
                raise LifecycleError('A new authenticated owner scope source is required')
            contract = dict(revision=parent['contract']['revision'] + 1,
                original_request=parent['original_request'],
                sources=parent['contract']['sources'] + [dict(origin=origin, text=source_text)],
                outcomes=_outcomes(required_outcomes))
            r._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
                (parent_id, contract['revision'], _encoded(contract), _digest(contract)))
        return self.get(parent_id)

    def append_requirement(self, parent_id, *, owner, origin, source_text):
        """Atomically preserve existing requirements and append one user source."""
        from .parent_intake import append_requirement
        return append_requirement(self, parent_id, owner=owner, origin=origin, source_text=source_text)

    def intake_requirement(self, *, session_key, profile, owner, origin, source_text, previous_accepted=False):
        """Serialize current-or-begin-or-append across independent connections."""
        from .parent_intake import intake_requirement
        return intake_requirement(self, session_key=session_key, profile=profile, owner=owner,
            origin=origin, source_text=source_text, previous_accepted=previous_accepted)

    def record_terminal_submission(self, parent_id, *, submission_id, status):
        from .parent_supersession import record_terminal_submission
        return record_terminal_submission(self, parent_id, submission_id=submission_id, status=status)

    def supersede_execution(self, execution_id, *, parent_id, replacement_run_id, outcome_ids):
        from .parent_supersession import supersede_execution
        return supersede_execution(self, execution_id, parent_id=parent_id,
            replacement_run_id=replacement_run_id, outcome_ids=outcome_ids)

    def record_execution(self, *, execution_id, session_key, profile, owner, kind,
                         parent_id=None, run_id=None, outcome_ids=(), source_request=None):
        """Tool entry records even legacy/unattributed executions, before launch."""
        for name, value in [('execution_id', execution_id), ('kind', kind)]:
            _text(name, value)
        if source_request is not None:
            if (not isinstance(source_request, dict)
                    or set(source_request) != {'owner', 'profile', 'origin', 'text'}
                    or (source_request['owner'], source_request['profile']) != (owner, profile)):
                raise LifecycleError('Execution source identity mismatch')
            _text('execution source origin', source_request['origin'])
            # Captionless media has an empty authenticated original. Audit it
            # exactly; enriched text must never replace the user's source.
            if not isinstance(source_request['text'], str):
                raise LifecycleError('Execution source text must be a string')
        if parent_id:
            parent = self.get(parent_id)
            if (session_key, profile, owner) != tuple(parent[k] for k in ('session_key', 'profile', 'owner')):
                raise LifecycleError('Execution parent identity mismatch')
            if set(outcome_ids) - {v['id'] for v in parent['contract']['outcomes']}:
                raise LifecycleError('Unknown outcome attribution')
        elif run_id or outcome_ids:
            raise LifecycleError('Managed execution needs a parent')
        if run_id:
            from .parent_plan import validate_child
            run = self.registry.lookup(run_id)
            validate_child(self, parent, run)
        r = self.registry
        with r._lock, r._conn:
            r._conn.execute('BEGIN IMMEDIATE')
            row = r._conn.execute('SELECT * FROM lifecycle_parent_executions WHERE execution_id=?', (execution_id,)).fetchone()
            recorded_at = _decoded(row[7])['recorded_at'] if row else time.time()
            details = dict(outcome_ids=list(outcome_ids), recorded_at=recorded_at,
                contract_digest=parent['contract_digest'] if parent_id else None)
            if source_request is not None:
                details['source_request'] = source_request
            attribution = _encoded(details)
            values = (execution_id, parent_id, session_key, profile, owner, kind, run_id, attribution)
            if row and row != values:
                raise LifecycleError('Execution attribution is immutable')
            r._conn.execute('INSERT OR IGNORE INTO lifecycle_parent_executions VALUES(?,?,?,?,?,?,?,?)', values)

    def decision(self, parent_id):
        """Recompute authority; a persisted decision alone is never completion."""
        from .parent_verification import evaluate
        return evaluate(self, parent_id)

    def bind_plan(self, parent_id, *, envelope, spec_text, policy, check_files):
        """Bind complete trusted launch material before independent coverage."""
        from .parent_plan import bind_plan
        return bind_plan(self, parent_id, envelope=envelope, spec_text=spec_text,
                         policy=policy, check_files=check_files)

    def bind_execution(self, execution_id, *, parent_id, run_id, outcome_ids):
        """Trusted completion importer resolves a tool entry to actual child evidence.

        Preserve the original unverified event. Reattribution across owners,
        sessions or profiles and replacing a current revision link are refused.
        """
        parent = self.get(parent_id)
        r = self.registry
        with r._lock, r._conn:
            row = r._conn.execute('SELECT session_key,profile,owner,parent_id,attribution '
                'FROM lifecycle_parent_executions WHERE execution_id=?', (execution_id,)).fetchone()
        if not row or row[:3] != tuple(parent[k] for k in ('session_key', 'profile', 'owner')):
            raise LifecycleError('Cannot resolve an execution from a different authenticated session')
        if row[3] and row[3] != parent_id:
            raise LifecycleError('Cannot move an execution to another objective')
        # Reuse the same contract/source validation without creating a second
        # audit event or silently trusting a model-provided run identifier.
        from .parent_plan import validate_child
        run = r.lookup(run_id)
        validate_child(self, parent, run)
        if not outcome_ids or set(outcome_ids) - {v['id'] for v in parent['contract']['outcomes']}:
            raise LifecycleError('Explicit current outcome attribution is required')
        attribution = _encoded(dict(outcome_ids=list(outcome_ids), contract_digest=parent['contract_digest'],
                                    recorded_at=_decoded(row[4])['recorded_at']))
        values = (execution_id, parent['contract_digest'], parent_id, run_id, attribution)
        with r._lock, r._conn:
            r._conn.execute('BEGIN IMMEDIATE')
            prior = r._conn.execute('SELECT * FROM lifecycle_parent_execution_links '
                'WHERE execution_id=? AND contract_digest=?', values[:2]).fetchone()
            if prior and prior != values:
                raise LifecycleError('Execution resolution conflict')
            r._conn.execute('INSERT OR IGNORE INTO lifecycle_parent_execution_links VALUES(?,?,?,?,?)', values)

    def authoritative(self, parent_id, decision_id):
        decision = self.decision(parent_id)
        return decision['accepted'] and decision['decision_id'] == decision_id
