from unittest.mock import Mock

import pytest

from agent.task_lifecycle.acceptance import AcceptanceGate, Artifact, CheckResult, Verdict
from agent.task_lifecycle.types import LifecycleError, Phase


ARTIFACT = Artifact('hash-v1', ('result.txt',))
REQUIRED = {'test': ('unit',), 'review': ('review',), 'boundary': ('api',)}


def make_gate():
    return AcceptanceGate(root='.', sink=Mock())


def populate(gate, revision='hash-v1'):
    for kind, names in REQUIRED.items():
        gate.record('run', CheckResult(names[0], kind, True, revision, 'checked'))


def test_exit_zero_and_test_pass_do_not_replace_boundary():
    gate = make_gate()
    gate.record('run', CheckResult('unit', 'test', True, ARTIFACT.revision, 'exit_code=0'))
    verdict = gate.evaluate('run', artifact=ARTIFACT,
                            required={'test': ('unit',), 'boundary': ('api',)})
    assert not verdict.accepted
    assert 'boundary:api' in verdict.missing
    with pytest.raises(LifecycleError):
        gate.mark_verified('run', verdict)


def test_changed_revision_invalidates_all_old_passes():
    gate = make_gate()
    populate(gate)
    verdict = gate.evaluate('run', artifact=Artifact('hash-v2', ARTIFACT.paths), required=REQUIRED)
    assert not verdict.accepted
    assert set(verdict.stale) == {'test:unit', 'review:review', 'boundary:api'}


def test_missing_review_is_reported():
    gate = make_gate()
    verdict = gate.evaluate('run', artifact=ARTIFACT, required={'review': ('review',)})
    assert not verdict.accepted
    assert 'review:review' in verdict.missing


def test_latest_failure_overrides_pass():
    gate = make_gate()
    populate(gate)
    gate.record('run', CheckResult('review', 'review', False, ARTIFACT.revision, 'bug'))
    verdict = gate.evaluate('run', artifact=ARTIFACT, required=REQUIRED)
    assert not verdict.accepted
    assert 'review:review' in verdict.missing


def test_verified_evidence_and_ledger_sink():
    gate = make_gate()
    populate(gate)
    verdict = gate.evaluate('run', artifact=ARTIFACT, required=REQUIRED)
    evidence = gate.mark_verified('run', verdict)
    assert evidence.phase is Phase.VERIFIED
    assert evidence.artifact == ARTIFACT
    assert len(evidence.checks) == 3
    call = gate.sink.call_args.kwargs
    assert call['ok'] is True
    assert call['session_id'] == 'run'
    assert call['scope'] == 'targeted'
    assert ARTIFACT.revision in call['output']


@pytest.mark.parametrize('required', [{}, {'test': ()}])
def test_empty_requirements_cannot_verify_without_evidence(required):
    gate = make_gate()
    assert not gate.evaluate('run', artifact=ARTIFACT, required=required).accepted


def test_forged_or_other_run_verdict_cannot_verify():
    gate = make_gate()
    populate(gate)
    verdict = gate.evaluate('run', artifact=ARTIFACT, required=REQUIRED)
    for run_id, candidate in [('other', verdict), ('run', Verdict(True, (), ()))]:
        with pytest.raises(LifecycleError):
            gate.mark_verified(run_id, candidate)


def test_new_check_invalidates_evaluated_verdict():
    gate = make_gate()
    populate(gate)
    verdict = gate.evaluate('run', artifact=ARTIFACT, required=REQUIRED)
    gate.record('run', CheckResult('unit', 'test', False, ARTIFACT.revision, 'failed'))
    with pytest.raises(LifecycleError):
        gate.mark_verified('run', verdict)


def test_invalid_check_kind_rejected():
    with pytest.raises(LifecycleError):
        CheckResult('shell', 'exit_code', True, ARTIFACT.revision, '0')


def test_unknown_required_kind_rejected():
    with pytest.raises(LifecycleError):
        make_gate().evaluate('run', artifact=ARTIFACT, required={'shell': ('x',)})
