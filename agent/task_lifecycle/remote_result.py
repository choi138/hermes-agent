"""Verified Mac receipt transfer into the authenticated source gateway ledger.

SSH authenticates the producing host; its installed verifier remains trusted.
The gateway compares original source/request scope before accepting any result.
No side effect is executed on import, and no message is sent by this module.
"""
from base64 import b64decode, b64encode
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from gateway import delivery_ledger
from hermes_constants import get_hermes_home
from .contract import _digest
from .handoff import _durable_schema
from .registry import Registry
from .types import LifecycleError, Phase
from .verification import evidence, snapshot, run_lock, read_artifact, _schema
from .workflow import decode_contract, write_private, host_identity


def export_result(run_id, *, content, attachments=()):
    if not isinstance(content,str) or not content.strip() or len(content)>1750:
        raise LifecycleError('Result must fit one message')
    with run_lock(run_id):
        registry=Registry()
        try:
            run,job=registry.lookup(run_id),registry.job(run_id)
            verified=evidence(registry,run_id)
            grant=job['payload']
            if run.phase not in {Phase.VERIFIED,Phase.DELIVERING,Phase.DELIVERY_UNCONFIRMED,Phase.DELIVERED} or not verified or not verified['accepted']:
                raise LifecycleError('Remote result is not verified')
            if snapshot(run.contract.workdir,grant['artifacts'])!=verified['artifact']:
                raise LifecycleError('Remote artifact changed since acceptance')
            if len(attachments)>1 or any(p not in grant['artifacts'] for p in attachments):
                raise LifecycleError('Only one contract-verified attachment is supported')
            exported=[]
            for relative in attachments:
                path=Path(run.contract.workdir)/relative
                info=verified['artifact']['files'][relative]
                data=read_artifact(run.contract.workdir,relative,info)
                exported.append(dict(name=path.name,relative=relative,sha256=info['sha256'],bytes=len(data),data=b64encode(data).decode()))
            return dict(version=1,run_id=run_id,host=host_identity(),contract=asdict(run.contract),grant=grant,
                        start_evidence=run.start_evidence,result=job['result'],verification=verified,
                        content=content,attachments=exported)
        finally:
            registry.close()


def receive_result(package, *, envelope, expected_run_id, content, attachments=()):
    """Accept only from SSHExecutor.receive, with the original gateway envelope.

    An untrusted model JSON result must never call this authority boundary.
    There is no endpoint or model tool that accepts a package directly.
    """
    if package.get('version')!=1 or package.get('run_id')!=expected_run_id or package.get('content')!=content:
        raise LifecycleError('Unexpected remote result identity/content')
    contract=decode_contract(package['contract'])
    grant=package['grant']
    for name in ('owner','origin','request_revision','profile','request_text','objective','approval_ref'):
        if getattr(contract,name)!=envelope[name]:
            raise LifecycleError('Remote result changed original request identity')
    for name in ('request','checks','artifacts','destination','forbidden_actions'):
        actual=contract.forbidden_actions if name=='forbidden_actions' else grant[name]
        expected=envelope[name]
        if name=='forbidden_actions': expected=tuple(expected)
        if actual!=expected:
            raise LifecycleError('Remote result changed approved execution scope')
    if grant['context']!=(envelope['context'] or {'text':'','manifest':{}}):
        raise LifecycleError('Remote result changed approved context')
    if grant.get('spec_sha256')!=envelope['spec_sha256']:
        raise LifecycleError('Remote result changed the approved SPEC')
    if (contract.repo_root!=envelope['request']['allowed_root'] or contract.workdir!=envelope['request']['workdir']
            or contract.allowed_paths!=(contract.repo_root,)):
        raise LifecycleError('Remote result changed approved paths')
    digest_values={k:grant[k] for k in ('request','prompt','checks','artifacts','context','destination')}
    if 'check_files' in grant: digest_values['check_files']=grant['check_files']
    if 'spec_sha256' in grant: digest_values['spec_sha256']=grant['spec_sha256']
    if _digest(digest_values)!=contract.execution_digest:
        raise LifecycleError('Remote contract digest mismatch')
    result,verified=package['result'],package['verification']
    if (result.get('status')!='cli_completed' or result.get('exit_code')!=0
            or result.get('process_returncode')!=0 or result.get('cli')!=grant['request']['cli']
            or result.get('sandbox')!=grant['request']['sandbox'] or result.get('model')!=grant['request']['model']
            or not result.get('input_receipt',{}).get('pipe_complete')
            or result['input_receipt']['sha256']!=hashlib.sha256(grant['prompt'].encode()).hexdigest()
            or not verified.get('accepted') or not verified.get('checks')):
        raise LifecycleError('Remote execution/acceptance receipts incomplete')
    if any(package['start_evidence'].get(k)!=v for k,v in package['host'].items()):
        raise LifecycleError('Remote receipt host identity mismatch')
    required={(c['kind'],c['name']) for c in grant['checks']}
    observed={(c['kind'],c['name']) for c in verified['checks'] if c['passed'] and c['revision']==verified['artifact']['revision']}
    if required!=observed or tuple(f'{c["kind"]}:{c["name"]}' for c in grant['checks'])!=contract.acceptance_checks:
        raise LifecycleError('Remote acceptance does not cover the original required checks')
    if tuple(a['relative'] for a in package['attachments'])!=tuple(attachments):
        raise LifecycleError('Remote attachment selection mismatch')
    # Validate everything before reserving state or queuing delivery.
    downloaded=[]
    for item in package['attachments']:
        if Path(item['name']).name!=item['name'] or item['name'] in {'.','..'}:
            raise LifecycleError('Invalid attachment filename')
        if len(item['data'])>12*1024*1024:
            raise LifecycleError('Remote attachment transfer too large')
        data=b64decode(item['data'],validate=True)
        expected=verified['artifact']['files'].get(item['relative'])
        if (not expected or len(data)!=item['bytes'] or item['bytes']>8*1024*1024
                or hashlib.sha256(data).hexdigest()!=item['sha256']
                or item['sha256']!=expected['sha256'] or item['bytes']!=expected['bytes']):
            raise LifecycleError('Downloaded attachment does not match verified remote bytes')
        downloaded.append((item,data))
    with run_lock(expected_run_id):
        registry=Registry()
        try:
            outcome,stored=registry._store.reserve(registry.SCOPE,contract.idempotency_key(),contract.digest(),expected_run_id,
                {'phase':Phase.ACCEPTED.value,'contract':asdict(contract)})
            if outcome=='conflict' or stored['run_id']!=expected_run_id:
                raise LifecycleError('Original request already owns another execution')
            run=registry.lookup(expected_run_id)
            payload={**grant,'remote_snapshot':{'host':package['host'],'artifact':verified['artifact']}}
            registry.prepare_job(expected_run_id,payload)
            registry.claim_job(expected_run_id,'remote:'+expected_run_id,None)
            registry.finish_job(expected_run_id,'remote:'+expected_run_id,result)
            if run.phase is Phase.ACCEPTED:
                registry.mark_ready(expected_run_id)
                registry.mark_running(expected_run_id,start_evidence=package['start_evidence'])
                registry.mark_execution_finished(expected_run_id,exit_code=result['process_returncode'])
            run=registry.lookup(expected_run_id)
            if run.phase is Phase.READY:
                registry.mark_running(expected_run_id,start_evidence=package['start_evidence'])
                run=registry.lookup(expected_run_id)
            if run.phase is Phase.RUNNING:
                registry.mark_execution_finished(expected_run_id,exit_code=result['process_returncode'])
                run=registry.lookup(expected_run_id)
            _schema(registry)
            with registry._conn:
                prior=registry._conn.execute('SELECT evidence FROM lifecycle_verifications WHERE run_id=?',(expected_run_id,)).fetchone()
                if prior and json.loads(prior[0])!=verified:
                    raise LifecycleError('Imported verification cannot be replaced')
                registry._conn.execute('INSERT OR IGNORE INTO lifecycle_verifications VALUES(?,?,?)',
                                      (expected_run_id,contract.digest(),json.dumps(verified,sort_keys=True)))
            if run.phase is Phase.EXECUTION_FINISHED:
                registry.record_phase(expected_run_id,Phase.VERIFYING,evidence={'verification_ref':expected_run_id})
                run=registry.lookup(expected_run_id)
            if run.phase is Phase.VERIFYING:
                registry.record_phase(expected_run_id,Phase.VERIFIED,evidence={
                    'artifact_revision':verified['artifact']['revision'],'acceptance_digest':_digest(verified)})
            directory=get_hermes_home()/'lifecycle'/'received'/expected_run_id
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            copies=[]
            for item,data in downloaded:
                target=directory/item['name']
                if target.exists():
                    from .workflow import read_private
                    if read_private(target,limit=8*1024*1024)!=data:
                        raise LifecycleError('Received attachment snapshot conflict')
                else: write_private(target,data)
                copies.append({k:item[k] for k in ('name','bytes','sha256')}|{'path':str(target)})
            destination=envelope['destination']
            text=content+f'\n\n참조: {expected_run_id}'
            oid=delivery_ledger.compute_obligation_id(destination['session_key'],expected_run_id,text)
            handoff=dict(destination=destination,content=text,attachments=copies,artifact_revision=verified['artifact']['revision'])
            encoded=json.dumps(handoff,sort_keys=True)
            _durable_schema(registry)
            with registry._conn:
                prior=registry._conn.execute('SELECT obligation_id,payload FROM lifecycle_handoffs WHERE run_id=?',(expected_run_id,)).fetchone()
                if prior and prior!=(oid,encoded): raise LifecycleError('Imported result is immutable')
                registry._conn.execute('INSERT OR IGNORE INTO lifecycle_handoffs(obligation_id,run_id,payload) VALUES(?,?,?)',
                                       (oid,expected_run_id,encoded))
            delivery_ledger.record_obligation(obligation_id=oid,session_key=destination['session_key'],platform=destination['platform'],
                chat_id=destination['channel_id'],thread_id=destination['thread_id'],content=text,
                adapter_profile=destination['adapter_profile'],preserve_existing=True)
            if registry.lookup(expected_run_id).phase is Phase.VERIFIED:
                registry.record_phase(expected_run_id,Phase.DELIVERING,evidence={'delivery_ref':oid})
            phase = registry.lookup(expected_run_id).phase
            return {'run_id':expected_run_id,'obligation_id':oid,'phase':phase.value,'complete':phase is Phase.DELIVERED}
        finally:
            registry.close()
