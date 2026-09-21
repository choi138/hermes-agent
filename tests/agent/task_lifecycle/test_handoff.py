import hashlib
from unittest.mock import Mock

import pytest

from agent.task_lifecycle.acceptance import AcceptanceGate, Artifact, CheckResult
from agent.task_lifecycle.handoff import Handoff, Origin, TransportReceipt
from agent.task_lifecycle.types import LifecycleError, Phase, PhaseTransitionError, is_complete


ARTIFACT = Artifact('hash-v1', ('result.txt',))
ORIGIN = Origin(session_key='session', platform='discord', channel_id='channel',
                thread_id='thread', content='Verified result')
DIGEST = hashlib.sha256(ORIGIN.content.encode()).hexdigest()
RECEIPT = TransportReceipt('message', 'channel', DIGEST)


def setup_handoff(verified=True):
    gate = AcceptanceGate(root='.', sink=Mock())
    if verified:
        gate.record('run', CheckResult('unit', 'test', True, ARTIFACT.revision, 'pass'))
        verdict = gate.evaluate('run', artifact=ARTIFACT, required={'test': ('unit',)})
        gate.mark_verified('run', verdict)
    sink = Mock()
    return gate, Handoff(gate, sink=sink), sink


def test_unverified_open_rejected():
    _, handoff, sink = setup_handoff(False)
    with pytest.raises(PhaseTransitionError):
        handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    sink.record_obligation.assert_not_called()


@pytest.mark.parametrize('field', ['message_id', 'channel_id', 'content_digest'])
def test_incomplete_receipt_rejected_without_transition(field):
    _, handoff, sink = setup_handoff()
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    values = dict(message_id='message', channel_id='channel', content_digest=DIGEST)
    values[field] = ''
    with pytest.raises(LifecycleError):
        handoff.confirm('run', TransportReceipt(**values))
    assert handoff.phase('run') is Phase.DELIVERING
    sink.mark_delivered.assert_not_called()


def test_unconfirmed_preserves_evidence_and_is_recoverable():
    gate, handoff, sink = setup_handoff()
    evidence = gate.verified('run')
    assert handoff.open('run', origin=ORIGIN, artifact=ARTIFACT) is Phase.DELIVERING
    assert handoff.mark_unconfirmed('run', 'timeout') is Phase.DELIVERY_UNCONFIRMED
    pending = handoff.pending()
    assert len(pending) == 1
    assert pending[0].verification is evidence
    assert pending[0].status == '검증 완료 · 전달 미확인'
    assert pending[0].reason == 'timeout'
    assert gate.verified('run') is evidence
    assert not is_complete(handoff.phase('run'))
    sink.mark_failed.assert_called_once()
    assert handoff.confirm('run', RECEIPT) is Phase.DELIVERED
    assert is_complete(handoff.phase('run'))
    assert handoff.pending() == []


def test_same_receipt_is_idempotent_and_origin_is_preserved():
    _, handoff, sink = setup_handoff()
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    assert not is_complete(handoff.phase('run'))
    assert handoff.confirm('run', RECEIPT) is Phase.DELIVERED
    assert handoff.confirm('run', RECEIPT) is Phase.DELIVERED
    sink.record_obligation.assert_called_once()
    sink.mark_delivered.assert_called_once()
    values = sink.record_obligation.call_args.kwargs
    assert values['chat_id'] == 'channel'
    assert values['thread_id'] == 'thread'
    assert values['content'] == ORIGIN.content


@pytest.mark.parametrize('receipt', [TransportReceipt('m', 'wrong', DIGEST),
                                      TransportReceipt('m', 'channel', 'wrong')])
def test_receipt_for_other_content_or_channel_rejected(receipt):
    _, handoff, _ = setup_handoff()
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    with pytest.raises(LifecycleError):
        handoff.confirm('run', receipt)
    assert handoff.phase('run') is Phase.DELIVERING


def test_cannot_deliver_unverified_artifact_revision():
    _, handoff, sink = setup_handoff()
    with pytest.raises(LifecycleError):
        handoff.open('run', origin=ORIGIN, artifact=Artifact('hash-v2', ARTIFACT.paths))
    sink.record_obligation.assert_not_called()


def test_ledger_failure_does_not_claim_delivery_or_erase_verification():
    gate, handoff, sink = setup_handoff()
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    evidence = gate.verified('run')
    sink.mark_delivered.side_effect = OSError('disk failure')
    with pytest.raises(OSError):
        handoff.confirm('run', RECEIPT)
    assert handoff.phase('run') is Phase.DELIVERING
    assert gate.verified('run') is evidence


def test_confirm_before_open_and_regression_after_delivery_rejected():
    _, handoff, _ = setup_handoff()
    with pytest.raises(PhaseTransitionError):
        handoff.confirm('run', RECEIPT)
    handoff.open('run', origin=ORIGIN, artifact=ARTIFACT)
    handoff.confirm('run', RECEIPT)
    with pytest.raises(PhaseTransitionError):
        handoff.mark_unconfirmed('run', 'late failure')
    assert is_complete(handoff.phase('run'))
