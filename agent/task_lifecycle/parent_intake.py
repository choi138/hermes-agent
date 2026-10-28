"""Serialized authenticated admission and additive parent requirements."""
from hashlib import sha256
import time
from uuid import uuid4

from .contract import _digest
from .registry import _decoded, _encoded
from .types import LifecycleError


def _append(ledger, parent, *, owner, origin, source_text):
    from .parent import _outcomes
    if owner != parent['owner']:
        raise LifecycleError('An authenticated parent owner is required')
    sources = parent['contract']['sources']
    prior = next((source for source in sources if source['origin'] == origin), None)
    if prior:
        if prior['text'] != source_text:
            raise LifecycleError('Authenticated source text conflict')
        return parent
    outcome = dict(id='requirement_' + sha256(origin.encode()).hexdigest()[:24],
                   description=source_text, checks=[])
    contract = dict(revision=parent['contract']['revision'] + 1,
        original_request=parent['original_request'],
        sources=[*sources, dict(origin=origin, text=source_text)],
        outcomes=_outcomes([*parent['contract']['outcomes'], outcome]))
    ledger.registry._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
        (parent['parent_id'], contract['revision'], _encoded(contract), _digest(contract)))
    return ledger.get(parent['parent_id'])


def append_requirement(ledger, parent_id, *, owner, origin, source_text):
    from .parent import _text
    _text('scope origin', origin)
    _text('scope source', source_text)
    registry = ledger.registry
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        return _append(ledger, ledger.get(parent_id), owner=owner, origin=origin, source_text=source_text)


def intake_requirement(ledger, *, session_key, profile, owner, origin, source_text, previous_accepted=False):
    """A stale caller's accepted flag cannot fork the newly admitted objective."""
    from .parent import _text
    identity = dict(session_key=session_key, profile=profile, owner=owner)
    for key, value in dict(identity, origin=origin, source_text=source_text).items():
        _text(key, value)
    registry = ledger.registry
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        parent = ledger.current(**identity)
        issued = False
        if parent:
            if any(source['origin'] == origin for source in parent['contract']['sources']):
                return _append(ledger, parent, owner=owner, origin=origin, source_text=source_text)
            row = registry._conn.execute('SELECT evidence FROM lifecycle_parent_decisions '
                'WHERE parent_id=? ORDER BY recorded_at DESC LIMIT 1', (parent['parent_id'],)).fetchone()
            decision = _decoded(row[0]) if row else {}
            issued = (previous_accepted is True and decision.get('accepted') is True
                      and decision.get('contract_digest') == parent['contract_digest'])
            if not issued:
                return _append(ledger, parent, owner=owner, origin=origin, source_text=source_text)
        existing = registry._conn.execute('SELECT parent_id,owner,original_request FROM lifecycle_parents '
            'WHERE session_key=? AND profile=? AND origin=?', (session_key, profile, origin)).fetchone()
        if existing:
            if existing[1:] != (owner, source_text):
                raise LifecycleError('Authenticated parent request conflict')
            return ledger.get(existing[0])
        parent_id = str(uuid4())
        contract = dict(revision=1, original_request=source_text,
            sources=[dict(origin=origin, text=source_text)],
            outcomes=[dict(id='original_request', description=source_text, checks=[])])
        registry._conn.execute('INSERT INTO lifecycle_parents VALUES(?,?,?,?,?,?,?)',
            (parent_id, session_key, profile, owner, origin, source_text, time.time()))
        registry._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
            (parent_id, 1, _encoded(contract), _digest(contract)))
        return ledger.get(parent_id)
