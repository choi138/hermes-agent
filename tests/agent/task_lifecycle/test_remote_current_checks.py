"""Remote publication requires the current approved verifier bytes."""
import sys

import pytest

from agent.task_lifecycle import workflow
from agent.task_lifecycle.intake import create_grant
from agent.task_lifecycle.remote_result import export_result
from agent.task_lifecycle.types import LifecycleError
from agent.task_lifecycle.verification import verify
from tests.agent.task_lifecycle import test_workflow

case = test_workflow.case


def test_remote_export_rejects_changed_acceptance_script_and_recovers(case, tmp_path):
    check = tmp_path / 'trusted-boundary.py'
    original = 'import json; assert json.load(open("answer.json"))["status"] == "전달 미확인"\n'
    check.write_text(original)
    grant = create_grant(request=case['request'], request_text='check current verifier',
        objective='preserve current check identity', owner='operator',
        origin='discord:thread:123:current-check', request_revision='1', profile='default',
        checks=[dict(name='current-boundary', kind='boundary', argv=[sys.executable, str(check)])],
        artifacts=['answer.json'], destination=dict(session_key='test:current-check',
            platform='discord', channel_id='123', thread_id='123', adapter_profile='default'))
    run_id = workflow.submit(grant, popen=test_workflow.detached_popen(case,
        'import sys; sys.stdin.read(); print("done")'))['run_id']
    test_workflow.await_exit(run_id)
    assert verify(run_id)['accepted']
    accepted = export_result(run_id, content='current verified bytes')
    check.write_text('raise SystemExit("changed verifier")\n')
    with pytest.raises(LifecycleError, match='acceptance.*changed'):
        export_result(run_id, content='must be withheld')
    check.unlink()
    with pytest.raises(LifecycleError, match='acceptance.*changed'):
        export_result(run_id, content='still withheld')
    check.write_text(original)
    assert export_result(run_id, content='current verified bytes') == accepted
