"""Trusted intake for the opt-in runner; not exposed as a model tool.

The gateway calls this AFTER its normal authentication/authorization checks,
with the original SessionSource and request. Models may propose objectives,
not source identities, approval outcomes, paths or check commands. The caller
installs the returned private grant on the Mac over authenticated SSH.
"""
from dataclasses import asdict
import hashlib
from pathlib import Path
import socket
import subprocess
from uuid import uuid4

from agent.codex_task_runner import TaskRequest
from hermes_constants import get_hermes_home
from .contract import ExecutionAuthority, TaskContract, _digest
from .types import LifecycleError
from .workflow import grant_root, request_data, write_private


def create_grant(*, request: TaskRequest, request_text, objective, owner, origin,
                 request_revision, profile, checks, artifacts, forbidden_actions=(),
                 context=None, destination=None, approval_ref=None,
                 note_bindings=None, correction_ids=(), work_class='code', agentsx=None,
                 expected_spec_sha256=None):
    """Local trusted operator boundary. Keep this outside model dispatch.

    Every execution-affecting field, including check argv, memory and final
    destination, is bound into the contract. A different payload cannot reuse
    an approved request revision. Approvals must already have been resolved.
    """
    from .verification import validate_checks, snapshot, check_files
    validate_checks(checks)
    artifacts = tuple(artifacts)
    # Validate declared paths even when artifacts are not created yet.
    snapshot(request.workdir, artifacts, allow_missing=True)
    import os
    import stat
    from agent.codex_task_runner import MAX_SPEC_BYTES
    from .directory_handoff import open_directory
    authority = ExecutionAuthority(owner=owner, origin=origin, request_revision=request_revision,
        profile=profile, repo_root=str(request.allowed_root), workdir=str(request.workdir),
        allowed_paths=(str(request.allowed_root),), requires_approval=approval_ref is not None,
        approval_ref=approval_ref)
    root_identity = authority._directory_identity[:len(request.allowed_root.parts)]
    with open_directory(str(request.allowed_root), root_identity) as (root_fd, _):
        parent = os.dup(root_fd)
        try:
            relative = request.spec.relative_to(request.allowed_root)
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise LifecycleError('SPEC must be a regular file')
                fd_prompt = stream.read(MAX_SPEC_BYTES+1)
        finally:
            os.close(parent)
    if len(fd_prompt)>MAX_SPEC_BYTES or (expected_spec_sha256 is not None
            and hashlib.sha256(fd_prompt).hexdigest()!=expected_spec_sha256):
        raise LifecycleError('SPEC differs from approved input or exceeds the size limit')
    if not fd_prompt:
        raise LifecycleError('Empty SPEC')
    if note_bindings is not None:
        from agent.notes_store import NotesStore
        from .notes import load_context
        context = load_context(NotesStore(), note_bindings, owner=owner,
            project=str(request.allowed_root), profile=profile,
            task_digest=_digest({'origin':origin, 'revision':request_revision, 'request':request_text}))
    context = dict(context or {'text': '', 'manifest': {}})
    if agentsx is not None:
        from .adoption import inspect_adoption
        adoption = inspect_adoption(request.workdir, agentsx)
        context['agentsx'] = {k:v for k,v in adoption.items() if k != 'policy_text'}
        context['text'] += '\n\nRepository policy from verified agentsx adoption:\n' + adoption['policy_text']
    if correction_ids:
        from .corrections import PersistentCorrectionLedger
        ledger = PersistentCorrectionLedger()
        try:
            items = ledger.for_task(correction_ids, scope=dict(owner=owner,
                project=str(request.allowed_root), profile=profile), work_class=work_class)
        finally:
            ledger.close()
        context['corrections'] = items
        context['text'] += '\n\n'.join(
            f"Confirmed correction {i['correction_id']} revision={i['revision']} source={i['source']}: "
            f"{i['rule_text']} Exceptions: {i['exceptions']}" for i in items)
    prompt = (f'Original request:\n{request_text}\n\nObjective:\n{objective}\n\n'
              f'Forbidden actions: {list(forbidden_actions)}\n'
              f'Required acceptance: {[c["kind"] + ":" + c["name"] for c in checks]}\n\n'
              f'{fd_prompt.decode("utf-8")}\n\n{context["text"]}')
    payload = dict(request=request_data(request), prompt=prompt, checks=list(checks),
                   artifacts=list(artifacts), context=context, destination=destination,
                   spec_sha256=hashlib.sha256(fd_prompt).hexdigest(),
                   check_files=check_files(checks, request.allowed_root))
    base = subprocess.run(['git', '-C', str(request.workdir), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    contract = TaskContract(request_text=request_text, objective=objective,
        forbidden_actions=tuple(forbidden_actions), acceptance_checks=tuple(f'{c["kind"]}:{c["name"]}' for c in checks),
        **asdict(authority), execution_digest=_digest(payload), base_revision=base)
    name = uuid4().hex + '.json'
    grant = dict(version=1, grant_name=name, host=socket.gethostname(),
                 profile_home=str(get_hermes_home().resolve()), contract=asdict(contract),
                 authority=asdict(authority), directory_identity=authority._directory_identity, **payload)
    path = grant_root() / name
    write_private(path, grant)
    return path


def from_gateway(*, source, session_key, request_revision, request_text, decision,
                 request, objective, checks, artifacts, forbidden_actions=(), context=None):
    """Same-host adapter for the shared authenticated gateway intake contract."""
    envelope = gateway_envelope(source=source, session_key=session_key,
        request_revision=request_revision, request_text=request_text, decision=decision,
        request=request_data(request), objective=objective, checks=checks, artifacts=artifacts,
        spec_sha256=hashlib.sha256(Path(request.spec).read_bytes()).hexdigest(),
        forbidden_actions=forbidden_actions, context=context)
    return import_gateway_envelope(envelope)


def gateway_envelope(*, source, session_key, request_revision, request_text, decision,
                     request, objective, checks, artifacts, spec_sha256,
                     forbidden_actions=(), context=None):
    """Build on Linux from a trusted gateway turn without opening Mac paths.

    `request` contains operator-configured Mac paths/limits, never a model's
    arbitrary command. The approved immutable SPEC hash is rechecked on Mac.
    `decision` must come from the existing deterministic policy router.
    """
    from gateway.session import SessionSource
    from agent.direct_agent_policy import ExecutionDecision
    from tools.approval import get_current_session_key, request_tool_approval
    from .verification import validate_checks
    validate_checks(checks)
    if not isinstance(source, SessionSource) or not isinstance(decision, ExecutionDecision):
        raise LifecycleError('Trusted original source and resolved execution policy required')
    if (get_current_session_key(default='') != session_key or not source.user_id
            or not source.message_id or source.is_bot):
        raise LifecycleError('Active authenticated original user turn required')
    if (decision.lane not in {'codex','claude'} or decision.host != 'mac' or decision.refusal_reason
            or request['cli'] != decision.lane or request['workdir'] != decision.workdir
            or request['timeout'] > decision.timeout_seconds
            or request['sandbox'] != ('read-only' if decision.permissions=='read_only' else 'workspace-write')):
        raise LifecycleError('Request exceeds deterministic execution policy')
    if not isinstance(spec_sha256,str) or len(spec_sha256)!=64:
        raise LifecycleError('Approved SPEC digest is required')
    envelope=dict(version=1,owner=source.user_id,origin=f'{session_key}:message:{source.message_id}',
        request_revision=request_revision,profile=source.profile or 'default',request_text=request_text,
        objective=objective,request=request,checks=checks,artifacts=artifacts,spec_sha256=spec_sha256,
        forbidden_actions=list(forbidden_actions),context=context,
        destination=dict(session_key=session_key,platform=source.platform.value,channel_id=source.chat_id,
                         thread_id=source.thread_id,adapter_profile=source.profile or 'default'))
    scope_digest=_digest(envelope)
    if decision.approval=='required':
        ref='lifecycle:'+scope_digest
        answer=request_tool_approval('task_lifecycle',
            f'Execute {objective} in {request["workdir"]}; scope digest {scope_digest}',rule_key=ref)
        if answer.get('approved') is not True:
            raise LifecycleError('Approval denied or unavailable')
        envelope['approval_ref']=ref
    else:
        envelope['approval_ref']=None
    return envelope


def import_gateway_envelope(envelope):
    """Mac side of an OS-authenticated SSH call, never a model tool endpoint.

    A single immutable envelope for each origin/revision is saved outside the
    worker root before grant creation. Reply loss reuses that input and grant.
    Changing the model text cannot change authority in the original envelope.
    """
    import fcntl
    import json
    import os
    from .workflow import decode_request, read_private, submit
    expected={'version','owner','origin','request_revision','profile','request_text','objective','request',
              'checks','artifacts','spec_sha256','forbidden_actions','context','destination','approval_ref'}
    if not isinstance(envelope,dict) or set(envelope)!=expected or envelope['version']!=1:
        raise LifecycleError('Invalid gateway envelope schema')
    request=decode_request(envelope['request'])
    if hashlib.sha256(Path(request.spec).read_bytes()).hexdigest()!=envelope['spec_sha256']:
        raise LifecycleError('Mac SPEC differs from gateway-approved bytes')
    key=_digest({k:envelope[k] for k in ('origin','request_revision','profile')})
    root=grant_root().parent/'imports';root.mkdir(mode=0o700,exist_ok=True)
    lock=os.open(root/(key+'.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        source=root/(key+'.json');pointer=root/(key+'.grant')
        if source.exists():
            if json.loads(read_private(source))!=envelope:
                raise LifecycleError('Gateway origin/revision payload conflict')
        else:
            write_private(source,envelope)
        if pointer.exists():
            grant=Path(read_private(pointer).decode())
        else:
            values={k:v for k,v in envelope.items() if k not in {'version','request','spec_sha256'}}
            grant=create_grant(request=request,expected_spec_sha256=envelope['spec_sha256'],**values)
            # Crash here leaves an unused grant, not a launched workload.
            write_private(pointer,str(grant).encode())
        return submit(grant)
    finally:
        os.close(lock)
