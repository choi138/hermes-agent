"""Select canonical NotesStore sources using trusted per-note scope bindings.

NotesStore has no owner/project column. Do not guess that every note in a
profile belongs to the current task. Intake supplies explicit source bindings;
unknown scope is omitted, and required policy sources cannot be truncated.
"""
from datetime import datetime
import hashlib

from hermes_constants import get_hermes_home
from .context_pack import MemoryItem, build_pack, delivery_manifest, render_for_executor
from .contract import _digest
from .types import LifecycleError


def load_context(store, bindings, *, owner, project, profile, task_digest, limit=12):
    if store.base_dir.resolve() != (get_hermes_home() / 'notes').resolve():
        raise LifecycleError('NotesStore does not belong to the active profile')
    scope = dict(owner=owner, project=project, profile=profile)
    selected, reasons, required = [], [], set()
    canonical = {(n['kind'], n['topic_key']): n for n in store.list_notes()}
    for binding in bindings:
        if binding.get('scope') != scope:
            continue
        key = (binding['kind'], binding['topic_key'])
        ref = '/'.join(key)
        meta = canonical.get(key)
        mandatory = binding.get('required') is True
        if (meta is None or meta.get('status') not in {'active', 'unconfirmed'}
                or meta.get('superseded_by')):
            if mandatory:
                raise LifecycleError(f'Required source is missing or inactive: {ref}')
            continue
        note = store.read(*key)
        # Recheck after the listing/read boundary; a concurrent correction may
        # retire a note between calls. Never return a demoted predecessor.
        if note.get('status') not in {'active', 'unconfirmed'} or note.get('superseded_by'):
            if mandatory:
                raise LifecycleError(f'Required source changed: {ref}')
            continue
        if not note.get('evidence'):
            raise LifecycleError('Selected note has no provenance')
        source_digest = _digest({k: v for k, v in note.items() if k not in {'path', 'usage'}})
        policy = binding.get('policy_digest')
        policy_confirmed = (note.get('origin') == 'user' and note.get('status') == 'active'
                            and note.get('confidence') != 'contested' and policy == source_digest)
        if policy and not policy_confirmed:
            raise LifecycleError(f'Approved policy revision changed: {ref}')
        # Factual confirmation and approved behavioral policy are separate.
        kind = 'rule' if policy_confirmed else ('decision' if key[0] == 'decision' else 'fact')
        status = ('unconfirmed' if note['status'] == 'unconfirmed' else
                  'advisory' if note.get('confidence') == 'contested' else 'confirmed')
        stamp = datetime.fromisoformat(note['valid_from'].replace('Z', '+00:00'))
        revision = int(stamp.timestamp() * 1000000)
        item = MemoryItem(ref, kind, note['body'],
            f"{note['path']} sha256={source_digest} evidence={note['evidence']}", revision, scope, status)
        selected.append(item)
        if mandatory:
            required.add(ref)
        reasons.append(dict(ref=ref, source_digest=source_digest, evidence=note['evidence'],
                            valid_from=note['valid_from'], reason='explicit scoped source binding', required=mandatory))
    pack = build_pack(selected, task_digest=task_digest, scope=scope, limit=limit)
    if not required <= {i.item_id for i in pack.items}:
        raise LifecycleError('Context limit would omit a required source')
    manifest = delivery_manifest(pack)
    manifest['sources'] = [r for r in reasons if r['ref'] in {i.item_id for i in pack.items}]
    text = render_for_executor(pack)
    manifest['rendered_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    return {'text': text, 'manifest': manifest}
