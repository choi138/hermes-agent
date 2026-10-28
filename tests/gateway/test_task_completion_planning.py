"""Automatic plan adapter can select only operator-owned execution grants."""
import json
from types import SimpleNamespace
import sys

import pytest

from agent.task_lifecycle.types import LifecycleError
from gateway.task_completion_planning import automatic_plan


def _scope(tmp_path, proposed):
    script = tmp_path / 'operator-planner.py'
    script.write_text('import json,sys\npayload=json.load(sys.stdin)\nprint(' + repr(json.dumps(proposed)) + ')\n')
    worker = tmp_path / 'worker'
    worker.mkdir()
    check = {'name': 'required', 'kind': 'test', 'argv': ['/usr/bin/true']}
    config = {'completion_gate': {'mode': 'canary', 'planner': {'argv': [sys.executable, str(script)],
        'worker_root': str(worker)}, 'transport': {'host': 'operator-mac'},
        'policy': {'lane': 'codex'}, 'request': {'workdir': '/operator/repo'},
        'coverage': {'reviewer': 'claude'}, 'checks': [check], 'check_files': {'/operator/check': 'a' * 64}}}
    return SimpleNamespace(ctx=SimpleNamespace(user_config=config),
        identity=dict(session_key='session', profile='profile', owner='human'),
        request=SimpleNamespace(origin='message', text='implement original'), parent={'parent_id': 'p'})


def test_real_planner_subprocess_preserves_fixed_operator_policy(tmp_path):
    check = {'name': 'required', 'kind': 'test', 'argv': ['/usr/bin/true']}
    scope = _scope(tmp_path, {'checks': [check], 'outcomes': [], 'artifacts': []})
    plan = automatic_plan(scope)
    assert plan['binding']['request_text'] == 'implement original'
    assert plan['transport'] == {'host': 'operator-mac'}
    assert plan['request'] == {'workdir': '/operator/repo'}
    assert plan['check_files'] == {'/operator/check': 'a' * 64}
    assert plan['planner_receipt']['files']


@pytest.mark.parametrize('proposed', [
    {'transport': {'host': 'attacker'}},
    {'checks': [{'name': 'injected', 'kind': 'test', 'argv': ['/bin/sh', '-c', 'injected']}]},
])
def test_planner_cannot_mint_transport_or_check_commands(tmp_path, proposed):
    scope = _scope(tmp_path, proposed)
    with pytest.raises(LifecycleError, match='may not select|operator-granted'):
        automatic_plan(scope)
