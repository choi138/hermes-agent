from dataclasses import asdict, replace

import pytest

from agent.task_lifecycle.metrics import Baseline, Candidate, Sample, Targets, compare
from agent.task_lifecycle.types import LifecycleError


def samples(n=3, task_class="code", **changes):
    return [replace(Sample(f"{task_class}:{i}", task_class, 4, 2, 0, 0, 2, 100, True), **changes)
            for i in range(n)]


def targets(**changes):
    return replace(Targets(0.5, 0.5, 0, 0, 0.5, 0.1), **changes)


def candidate(n=3, **changes):
    return samples(n, user_interventions=2, duplicate_runs=1,
                   correction_repeats=1, **changes)


def test_mismatched_classes_rejected():
    with pytest.raises(LifecycleError, match="task_class"):
        compare(Baseline(samples()), Candidate(samples(task_class="research")), targets())


@pytest.mark.parametrize("before,after", [(2, 2), (1, 1)])
def test_insufficient_class_evidence(before, after):
    report = compare(Baseline(samples(before)), Candidate(candidate(after)), targets())
    assert report.insufficient_evidence == ["code"]
    assert report.sample_sizes == {"baseline": before, "candidate": after}
    assert report.verdict == "implemented_effect_unproven"


def test_all_targets_met_with_enough_samples():
    report = compare(Baseline(samples()), Candidate(candidate()), targets())
    assert report.verdict == "accepted"
    assert not report.quality_regressed
    assert report.unmet == report.insufficient_evidence == []
    assert report.metrics["user_interventions"] == {"before": 4, "after": 2, "delta": -2}


def test_quality_regression_overrides_improvement():
    report = compare(Baseline(samples()), Candidate(candidate(quality_ok=False)), targets())
    assert report.quality_regressed
    assert report.verdict == "regressed"


def test_existing_quality_or_safety_failure_still_blocks_acceptance():
    report = compare(Baseline(samples(quality_ok=False)),
                     Candidate(candidate(quality_ok=False)), targets())
    assert not report.quality_regressed
    assert report.verdict == "regressed"


@pytest.mark.parametrize("field,value,target", [
    ("user_interventions", 3, "max_interventions_ratio"),
    ("duplicate_runs", 2, "max_duplicate_ratio"),
    ("verification_missing", 1, "max_verification_missing"),
    ("delivery_missing", 1, "max_delivery_missing"),
    ("correction_repeats", 2, "max_correction_repeat_ratio"),
    ("wall_seconds", 111, "max_wall_regression_ratio"),
])
def test_each_unmet_target_is_reported(field, value, target):
    after = [replace(s, **{field: value}) for s in candidate()]
    report = compare(Baseline(samples()), Candidate(after), targets())
    assert report.verdict == "implemented_effect_unproven"
    assert report.unmet == [target]
    assert target in "\n".join(report.summary_lines())


def test_targets_unchanged_and_boundary_not_rounded():
    fixed = targets()
    original = asdict(fixed)
    report = compare(Baseline(samples()), Candidate(candidate(wall_seconds=110)), fixed)
    assert report.verdict == "accepted"
    assert asdict(fixed) == original
    report = compare(Baseline(samples()), Candidate(candidate(wall_seconds=110.000001)), fixed)
    assert report.verdict == "implemented_effect_unproven"
    assert asdict(fixed) == original


def test_zero_baseline_does_not_hide_new_failures():
    before = samples(user_interventions=0, duplicate_runs=0, correction_repeats=0,
                     wall_seconds=0)
    assert compare(Baseline(before), Candidate(before), targets()).verdict == "accepted"
    report = compare(Baseline(before), Candidate(candidate()), targets())
    assert set(report.unmet) == {"max_interventions_ratio", "max_duplicate_ratio",
                                 "max_correction_repeat_ratio", "max_wall_regression_ratio"}


def test_empty_comparison_rejected():
    with pytest.raises(LifecycleError):
        compare(Baseline([]), Candidate([]), targets())


def test_min_samples_is_configurable_and_applies_to_every_class():
    before = samples(4) + samples(3, task_class="research")
    after = candidate(4) + candidate(3, task_class="research")
    report = compare(Baseline(before), Candidate(after), targets(), min_samples=4)
    assert report.insufficient_evidence == ["research"]
    assert report.verdict == "implemented_effect_unproven"


def test_summary_discloses_provenance_and_insufficient_evidence():
    report = compare(Baseline(samples(1)), Candidate(candidate(1)), targets())
    summary = "\n".join(report.summary_lines())
    for text in ("실측", "추정", "미확인", "code", "implemented_effect_unproven"):
        assert text in summary


@pytest.mark.parametrize("changes", [{"wall_seconds": float("nan")},
                                    {"user_interventions": -1},
                                    {"quality_ok": "yes"}])
def test_invalid_evidence_is_rejected(changes):
    with pytest.raises(LifecycleError):
        compare(Baseline(samples()), Candidate([replace(s, **changes) for s in candidate()]), targets())


@pytest.mark.parametrize("side", ["baseline", "candidate", "both"])
@pytest.mark.parametrize("different_values", [False, True])
def test_duplicate_task_id_is_rejected(side, different_values):
    before, after = samples(), candidate()
    if side in {"baseline", "both"}:
        before = [before[0]] * 3
        if different_values:
            before[1] = replace(before[0], wall_seconds=3)
    if side in {"candidate", "both"}:
        after = [after[0]] * 3
        if different_values:
            after[1] = replace(after[0], wall_seconds=3)
    with pytest.raises(LifecycleError, match="duplicate"):
        compare(Baseline(before), Candidate(after), targets())


@pytest.mark.parametrize("before,after", [(samples(2), candidate(3)),
                                         (samples(3), candidate(2)),
                                         (samples(), [replace(s, task_id='other:' + s.task_id)
                                                      for s in candidate()])])
def test_exact_task_ids_must_be_paired(before, after):
    with pytest.raises(LifecycleError, match="task_id"):
        compare(Baseline(before), Candidate(after), targets())


def test_same_class_sets_do_not_allow_task_class_swap():
    before = [replace(s, task_class="research" if i == 0 else "code") for i, s in enumerate(samples())]
    after = [replace(s, task_class="research" if i == 1 else "code") for i, s in enumerate(candidate())]
    with pytest.raises(LifecycleError, match="task_class"):
        compare(Baseline(before), Candidate(after), targets())


def test_three_unique_pairs_are_accepted_in_any_order():
    report = compare(Baseline(samples()), Candidate(candidate()[::-1]), targets())
    assert report.verdict == "accepted"
    assert report.sample_sizes == {"baseline": 3, "candidate": 3}
    assert "출처 미확인" in "\n".join(report.summary_lines())
