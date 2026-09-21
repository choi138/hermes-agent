import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent.notes_store import NotesStore
from agent.task_lifecycle.notes import load_context
from agent.task_lifecycle.contract import _digest
from agent.task_lifecycle.corrections import PersistentCorrectionLedger
from agent.task_lifecycle.types import LifecycleError


@pytest.fixture
def notes(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    return NotesStore()


def test_scoped_canonical_successor_and_required_policy(notes):
    scope=dict(owner='user',project='repo',profile='default')
    first=notes.create('preference','json.output','이전: 마크다운을 써도 됨',evidence=['fixture:user:1'],origin='user')
    notes.supersede('preference','json.output',body='최신 정정: JSON 코드 블록 금지',evidence=['fixture:user:2'],origin='user')
    current=notes.read('preference','json.output')
    digest=_digest({k:v for k,v in current.items() if k not in {'path','usage'}})
    bindings=[dict(kind='preference',topic_key='json.output',scope=scope,policy_digest=digest,required=True)]
    context=load_context(notes,bindings,**scope,task_digest='request')
    assert '최신 정정' in context['text'] and '이전:' not in context['text']
    assert 'policy_authoritative=true' in context['text']
    assert 'fixture:user:2' in context['text']
    assert not load_context(notes,bindings,owner='other',project='repo',profile='default',task_digest='x')['manifest']['items']
    with pytest.raises(LifecycleError,match='omit'):
        load_context(notes,bindings,**scope,task_digest='request',limit=0)
    notes.tombstone('preference','json.output')
    with pytest.raises(LifecycleError,match='inactive'):
        load_context(notes,bindings,**scope,task_digest='request')


def test_unconfirmed_cannot_be_policy_and_changed_policy_fails(notes):
    scope=dict(owner='user',project='repo',profile='default')
    notes.create('fact','project.status','도구 종료를 완료로 간주할지 미확인',evidence=['fixture:agent:1'],origin='agent',status='unconfirmed')
    binding=dict(kind='fact',topic_key='project.status',scope=scope)
    context=load_context(notes,[binding],**scope,task_digest='x')
    assert 'policy_authoritative=false' in context['text']
    with pytest.raises(LifecycleError,match='revision changed'):
        load_context(notes,[{**binding,'policy_digest':'forged'}],**scope,task_digest='x')


def test_corrections_persist_versions_and_scope(notes):
    ledger=PersistentCorrectionLedger()
    scope=dict(owner='user',project='repo',profile='default')
    ledger.record(correction_id='json',revision=1,source='fixture:user:1',rule_text='JSON만',scope=scope,
                  environments=['repo:codex','repo:claude'],confirmed=False)
    assert ledger.for_task(['json'],scope=scope,work_class='code') == []
    ledger.record(correction_id='json',revision=2,source='fixture:user:2',rule_text='코드 펜스 금지',scope=scope,
                  environments=['repo:codex','repo:claude'],confirmed=True,work_classes=['code'])
    ledger.close()
    reopened=PersistentCorrectionLedger()
    assert reopened.get('json')['source']=='fixture:user:2'
    assert reopened.status('json')['delivered'] is False
    assert len(reopened.for_task(['json'],scope=scope,work_class='code'))==1
    assert not reopened.for_task(['json'],scope={**scope,'project':'other'},work_class='code')
    assert not reopened.for_task(['json'],scope=scope,work_class='docs')
    with pytest.raises(LifecycleError,match='increase'):
        reopened.record(correction_id='json',revision=1,source='fixture:user:1',rule_text='old',scope=scope,
                        environments=['repo:codex'],confirmed=True)
    reopened.close()
