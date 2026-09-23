"""Delivery is not behavioral evidence; one environment is not all."""

from datetime import datetime, timezone

import pytest

from agent.task_lifecycle.context_pack import MemoryItem, build_pack
from agent.task_lifecycle.corrections import Correction, CheckOutcome, CorrectionLedger
from agent.task_lifecycle.types import LifecycleError


@pytest.fixture
def ledger():
    result = CorrectionLedger()
    result.record(Correction("c1", "Do not announce success early", "Verify first",
                             ("cli", "desktop"), datetime.now(timezone.utc)))
    return result


def observe(ledger, environment, observed="followed", evidence="trace:42"):
    ledger.observe(CheckOutcome("c1", environment, observed, evidence))


def test_record_delivery_and_observation_are_separate(ledger):
    status = ledger.status("c1")
    assert status["recorded"] is True
    assert status["delivered"] is False
    assert status["followed_here"] is False
    assert status["followed_everywhere"] is False
    assert ledger.unverified() == []
    entry = MemoryItem("c1", "rule", "Verify first", "correction:c1", 1,
                       {"owner": "alice", "project": "hermes"}, "confirmed")
    pack = build_pack([entry], task_digest="task", scope=entry.scope, limit=1)
    ledger.mark_delivered("c1", pack_digest=pack.pack_digest())
    status = ledger.status("c1")
    assert status["delivered"] is True
    assert status["followed_here"] is False
    assert status["followed_everywhere"] is False
    assert [c.correction_id for c in ledger.unverified()] == ["c1"]


def test_partial_then_complete_then_regression(ledger):
    observe(ledger, "cli")
    assert ledger.status("c1")["followed_here"] is True
    assert ledger.status("c1")["followed_everywhere"] is False
    observe(ledger, "desktop")
    assert ledger.status("c1")["followed_everywhere"] is True
    observe(ledger, "desktop", "violated")
    assert ledger.status("c1")["followed_everywhere"] is False
    assert [c.correction_id for c in ledger.regressions()] == ["c1"]


def test_not_exercised_is_observation_but_not_success(ledger):
    ledger.mark_delivered("c1", pack_digest="pack")
    observe(ledger, "cli", "not_exercised")
    assert ledger.unverified() == []
    assert ledger.status("c1")["followed_here"] is False
    assert ledger.status("c1")["followed_everywhere"] is False
    assert ledger.regressions() == []


@pytest.mark.parametrize("evidence", [None, "", " \n "])
def test_evidence_required(ledger, evidence):
    with pytest.raises(LifecycleError):
        observe(ledger, "cli", evidence=evidence)
    assert ledger.status("c1")["followed_here"] is False


@pytest.mark.parametrize("environment,observed", [
    ("unknown", "followed"), ("cli", "probably"),
])
def test_invalid_observation_rejected(ledger, environment, observed):
    with pytest.raises(LifecycleError):
        observe(ledger, environment, observed)


def test_unknown_corrections_and_empty_delivery_rejected(ledger):
    with pytest.raises(LifecycleError):
        ledger.status("missing")
    with pytest.raises(LifecycleError):
        ledger.mark_delivered("missing", pack_digest="pack")
    with pytest.raises(LifecycleError):
        ledger.observe(CheckOutcome("missing", "cli", "followed", "trace"))
    with pytest.raises(LifecycleError):
        ledger.mark_delivered("c1", pack_digest=" ")


def test_duplicate_record_cannot_reset_history(ledger):
    observe(ledger, "cli", "violated")
    with pytest.raises(LifecycleError):
        ledger.record(Correction("c1", "Changed", "Changed", ("cli",),
                                 datetime.now(timezone.utc)))
    assert len(ledger.regressions()) == 1


def test_no_environments_is_not_vacuous_success():
    ledger = CorrectionLedger()
    ledger.record(Correction("c1", "Style", "Use plain words", (),
                             datetime.now(timezone.utc)))
    assert ledger.status("c1")["followed_everywhere"] is False
