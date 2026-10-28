"""Approved execution material shared by parent coverage and child validation."""
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re

from .contract import _digest
from .registry import _encoded
from .types import LifecycleError


def source_context(parent, context=None):
    """Trusted gateway binds additive user sources to the immutable worker input."""
    result = json.loads(_encoded(context or {'text': '', 'manifest': {}}))
    if len(parent['contract']['sources']) > 1:
        sources = parent['contract']['sources']
        result['manifest']['parent_sources'] = sources
        suffix = '\n\nAuthenticated parent requirement sources:\n' + json.dumps(sources, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if not result['text'].endswith(suffix):
            result['text'] += suffix
    return result


def execution_payload(envelope, spec_text, check_files):
    context = envelope['context'] or {'text': '', 'manifest': {}}
    prompt = (f'Original request:\n{envelope["request_text"]}\n\nObjective:\n{envelope["objective"]}\n\n'
              f'Forbidden actions: {list(envelope["forbidden_actions"])}\n'
              f'Required acceptance: {[c["kind"] + ":" + c["name"] for c in envelope["checks"]]}\n\n'
              f'{spec_text}\n\n{context["text"]}')
    return dict(request=envelope['request'], prompt=prompt, checks=envelope['checks'],
        artifacts=envelope['artifacts'], context=context, destination=envelope['destination'],
        check_files=check_files, spec_sha256=envelope['spec_sha256'])


def approved_plan(parent, *, envelope, spec_text, policy, check_files):
    from .verification import validate_checks
    from gateway.task_completion_policy import GatewayExecutionPolicy
    expected = {'version', 'owner', 'origin', 'request_revision', 'profile', 'request_text',
        'objective', 'request', 'checks', 'artifacts', 'spec_sha256', 'forbidden_actions',
        'context', 'destination', 'approval_ref'}
    if not isinstance(envelope, dict) or set(envelope) != expected or envelope['version'] != 1:
        raise LifecycleError('Complete authenticated execution envelope is required')
    if (envelope['owner'], envelope['profile'], envelope['request_text']) != (
            parent['owner'], parent['profile'], parent['original_request']):
        raise LifecycleError('Approved plan does not bind the original parent request')
    if (envelope['origin'] not in {s['origin'] for s in parent['contract']['sources']}
            or envelope['destination']['session_key'] != parent['session_key']):
        raise LifecycleError('Approved plan source/session mismatch')
    if len(parent['contract']['sources']) > 1:
        context = envelope['context']
        sources = parent['contract']['sources']
        suffix = '\n\nAuthenticated parent requirement sources:\n' + json.dumps(sources, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if (not context or context.get('manifest', {}).get('parent_sources') != sources
                or not context.get('text', '').endswith(suffix)):
            raise LifecycleError('Approved execution context omits authenticated parent requirements')
    if not isinstance(spec_text, str) or sha256(spec_text.encode()).hexdigest() != envelope['spec_sha256']:
        raise LifecycleError('Reviewable SPEC bytes do not match the approved SPEC digest')
    if (not isinstance(check_files, dict) or not check_files
            or any(not PurePosixPath(path).is_absolute() or not re.fullmatch('[a-f0-9]{64}', digest)
                   for path, digest in check_files.items())):
        raise LifecycleError('Trusted acceptance executable/script hashes are required')
    validate_checks(envelope['checks'])
    GatewayExecutionPolicy(**policy).validate(envelope['request'])
    observed = {(c['kind'], c['name']) for c in envelope['checks']}
    required = {(c['kind'], c['name']) for outcome in parent['contract']['outcomes'] for c in outcome['checks']}
    if not required <= observed:
        raise LifecycleError('Approved plan omits a required parent acceptance check')
    payload = execution_payload(envelope, spec_text, check_files)
    return json.loads(_encoded(dict(envelope=envelope, spec_text=spec_text, policy=policy,
                                    check_files=check_files, execution_digest=_digest(payload))))


def bind_plan(ledger, parent_id, **material):
    registry = ledger.registry
    with registry._lock, registry._conn:
        registry._conn.execute('BEGIN IMMEDIATE')
        parent = ledger.get(parent_id)
        plan = approved_plan(parent, **material)
        prior = parent['contract'].get('plan')
        if prior:
            # Internal enrichment revisions are not a new execution request.
            # Preserve the first immutable request revision on an exact replay.
            replay = json.loads(_encoded(plan))
            replay['envelope']['request_revision'] = prior['envelope']['request_revision']
            if replay == prior:
                return parent
        contract = dict(parent['contract'], revision=parent['contract']['revision'] + 1,
                        plan=plan, plan_digest=_digest(plan))
        registry._conn.execute('INSERT INTO lifecycle_parent_contracts VALUES(?,?,?,?)',
            (parent_id, contract['revision'], _encoded(contract), _digest(contract)))
    return ledger.get(parent_id)


def validate_child(ledger, parent, run):
    plan = parent['contract'].get('plan')
    if not plan or parent['contract'].get('plan_digest') != _digest(plan):
        raise LifecycleError('Approved execution plan is not bound to the parent')
    envelope = plan['envelope']
    if not run or not run.contract.execution_digest:
        raise LifecycleError('Managed child is required')
    for name in ('owner', 'origin', 'profile', 'request_text', 'objective', 'request_revision', 'approval_ref'):
        if getattr(run.contract, name) != envelope[name]:
            raise LifecycleError('Child differs from approved source/plan: ' + name)
    request = envelope['request']
    if (run.contract.repo_root, run.contract.workdir, run.contract.execution_digest) != (
            request['allowed_root'], request['workdir'], plan['execution_digest']):
        raise LifecycleError('Child repo/workdir/execution digest differs from approved plan')
    payload = ledger.registry.job(run.run_id)['payload']
    expected = execution_payload(envelope, plan['spec_text'], plan['check_files'])
    if any(payload.get(key) != value for key, value in expected.items()):
        raise LifecycleError('Child grant differs from approved SPEC/checks/artifacts/session')
    if tuple(envelope['forbidden_actions']) != run.contract.forbidden_actions:
        raise LifecycleError('Child changed approved forbidden actions')
