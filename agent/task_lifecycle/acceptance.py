"""Revision-bound acceptance, separate from execution and delivery."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Mapping

from agent.task_lifecycle.types import LifecycleError, Phase

_KINDS = frozenset({'test', 'review', 'boundary'})


@dataclass(frozen=True)
class Artifact:
    """Produced paths and their caller-computed content hash, not a branch name."""

    revision: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    name: str
    kind: str
    passed: bool
    revision: str
    detail: str

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise LifecycleError(f'Unknown check kind: {self.kind}')


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    missing: tuple[str, ...]
    stale: tuple[str, ...]


@dataclass(frozen=True)
class Verification:
    run_id: str
    artifact: Artifact
    checks: tuple[CheckResult, ...]
    phase: Phase = Phase.VERIFIED


class AcceptanceGate:
    """Only current, explicitly required checks can issue verification evidence.

    The existing verification ledger receives the aggregate verdict, so an
    individual test PASS cannot mark an unmet boundary requirement as passed.
    State is process-local; the injected sink retains the existing durable log.
    """

    def __init__(self, *, root: str | Path, sink: Callable | None = None):
        if sink is None:
            from agent.verification_evidence import record_verify_run
            sink = record_verify_run
        self.root = root
        self.sink = sink
        self._checks = {}
        self._evaluated = {}
        self._verified = {}

    def record(self, run_id: str, check: CheckResult) -> None:
        self._checks.setdefault(run_id, {})[(check.kind, check.name)] = check
        self._evaluated.pop(run_id, None)
        self._verified.pop(run_id, None)

    def evaluate(self, run_id: str, *, artifact: Artifact,
                 required: Mapping[str, tuple[str, ...]]) -> Verdict:
        if set(required) - _KINDS:
            raise LifecycleError('Unknown required check kind')
        missing, stale, checks = [], [], []
        recorded = self._checks.get(run_id, {})
        for kind, names in required.items():
            for name in names:
                label = f'{kind}:{name}'
                check = recorded.get((kind, name))
                if check is None:
                    missing.append(label)
                elif check.revision != artifact.revision:
                    stale.append(label)
                elif not check.passed:
                    missing.append(label)
                else:
                    checks.append(check)
        if not any(required.values()):
            missing.append('required checks')
        verdict = Verdict(not missing and not stale, tuple(missing), tuple(stale))
        self._verified.pop(run_id, None)
        self._evaluated.pop(run_id, None)
        self.sink(root=self.root, session_id=run_id, ok=verdict.accepted,
                  command='task lifecycle acceptance', scope='targeted',
                  output=json.dumps({'revision': artifact.revision,
                                     'missing': missing, 'stale': stale,
                                     'checks': [check.__dict__ for check in checks]}))
        self._evaluated[run_id] = (verdict, artifact, tuple(checks))
        return verdict

    def mark_verified(self, run_id: str, verdict: Verdict) -> Verification:
        evaluated = self._evaluated.get(run_id)
        if not verdict.accepted or evaluated is None or evaluated[0] is not verdict:
            raise LifecycleError('A current accepted verdict for this run is required')
        evidence = Verification(run_id, evaluated[1], evaluated[2])
        self._verified[run_id] = evidence
        return evidence

    def verified(self, run_id: str) -> Verification | None:
        return self._verified.get(run_id)
