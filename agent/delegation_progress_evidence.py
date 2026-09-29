"""Coordinator gate tickets: local, run-bound evidence, never shell execution."""
import json
import re
import uuid
import time
from contextlib import nullcontext

from agent.delegation_progress import EXCLUDED, SECRET, _atomic, _digest, _json, _read, _test_summary, collect
from agent.delegation_progress_policy import fingerprint


def scope_of(observation):
    return _digest(_json(sorted(observation['files'])))


def applicable_validation(progress, state, observation):
    value = dict(state.get('validation', {}))
    if not value:
        return {}
    value['applicable'] = False
    try:
        raw = _read(progress.manifest.artifact_root, value['evidence_ref'], 4 * 1024 * 1024)
        summary = _test_summary(raw.decode(), value['exit_code'], canonical=value['gate'] == 'canonical')
        value['applicable'] = bool(value['run_id'] == progress.manifest.run_id and
            value['code_fingerprint'] == fingerprint(observation) and fingerprint(observation) and
            value['scope'] == scope_of(observation) and value['evidence_digest'] == _digest(raw) and
            summary['status'] == 'passed' and summary.get('passed') == value['passed'] and
            summary.get('skipped', 0) == value['skipped'])
    except (OSError, ValueError, KeyError, UnicodeError):
        pass
    return value


def begin(progress, *, dry_run=False):
    with nullcontext() if dry_run else progress._transaction():
        state = progress._load()
        if state['stopped'] or state.get('closing'):
            raise ValueError('run_closed')
        observation = collect(progress.manifest)
        code = fingerprint(observation)
        if not code:
            raise ValueError('complete_code_inventory_required')
        ticket = dict(ticket=uuid.uuid4().hex, started_at_ns=time.time_ns(), run_id=progress.manifest.run_id,
                      code_fingerprint=code, scope=scope_of(observation))
        if not dry_run:
            state['validation_ticket'] = ticket
            state.pop('validation', None)
            _atomic(progress.path, state)
        return ticket


def record(progress, ticket, evidence_ref, exit_code, gate, *, dry_run=False):
    # Only bounded relative log names under the already approved artifact root.
    if (not isinstance(evidence_ref, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,160}', evidence_ref)
            or evidence_ref.startswith('/') or '..' in evidence_ref.split('/') or not evidence_ref.endswith('.log')
            or any(p in EXCLUDED or p.startswith('.') or SECRET.search(p) for p in evidence_ref.split('/'))
            or gate not in ('pytest', 'canonical') or type(exit_code) is not int):
        raise ValueError('validation_arguments')
    with nullcontext() if dry_run else progress._transaction():
        state = progress._load()
        if state['stopped'] or state.get('closing'):
            raise ValueError('run_closed')
        observation = collect(progress.manifest)
        expected = state.get('validation_ticket', {})
        if (ticket != expected.get('ticket') or expected.get('run_id') != progress.manifest.run_id or
                expected.get('code_fingerprint') != fingerprint(observation) or
                not fingerprint(observation) or expected.get('scope') != scope_of(observation)):
            raise ValueError('validation_run_code_scope_mismatch')
        from agent.delegation_progress import _open
        with _open(progress.manifest.artifact_root, evidence_ref) as (_, info):
            if info.st_mtime_ns < expected['started_at_ns']:
                raise ValueError('gate_log_predates_ticket')
        raw = _read(progress.manifest.artifact_root, evidence_ref, 4 * 1024 * 1024)
        summary = _test_summary(raw.decode(), exit_code, canonical=gate == 'canonical')
        value = dict(expected, evidence_ref=evidence_ref, evidence_digest=_digest(raw),
                     exit_code=exit_code, gate=gate, status=summary['status'],
                     passed=summary.get('passed', 0), skipped=summary.get('skipped', 0))
        if not dry_run:
            state['validation'] = value
            _atomic(progress.path, state)
        return value


def migrate(progress, *, dry_run=False):
    """Archive unsent legacy reports; never discard an attempted uncertain write."""
    from agent.delegation_progress import _lock
    with (nullcontext() if dry_run else progress._transaction()), \
            (nullcontext() if dry_run else _lock(progress.directory / 'watch.lock')), \
            (nullcontext() if dry_run else _lock(progress.directory / 'delivery.lock')):
        state = json.loads(_read(progress.directory, 'state.json', 16 * 1024 * 1024))
        if state.get('schema_version') != 1 or state.get('binding') != progress.manifest.binding():
            raise ValueError('legacy_identity')
        try:
            delivery = json.loads(_read(progress.directory, 'delivery.json', 16 * 1024 * 1024))
        except FileNotFoundError:
            delivery = {'records': {}}
        if any(r.get('status') == 'uncertain' for r in delivery.get('records', {}).values()):
            raise ValueError('legacy_uncertain_requires_v1_get_only_drain')
        if any(str(m['sequence']) in delivery.get('records', {}) and
               delivery['records'][str(m['sequence'])].get('status') != 'rejected' for m in state['pending']):
            raise ValueError('legacy_verified_requires_v1_ack')
        count = len(state['pending'])
        if not dry_run:
            _atomic(progress.directory / 'legacy-state.json', state)
            state.update(schema_version=2, pending=[], accumulated={}, tests={'status': 'unknown'})
            # Stopped jobs stay stopped. Sequence stays monotonic across migration.
            if state['stopped']:
                state['final_snapshot'].pop('validation', None)
                if state['final_snapshot'].get('coordinator_stage') == 'final_verified':
                    state['final_snapshot']['coordinator_stage'] = 'verifying'
            _atomic(progress.path, state)
        return {'status': 'migrated', 'archived_unsent': count, 'stopped': state['stopped']}
