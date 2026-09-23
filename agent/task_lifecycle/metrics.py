"""Pure comparison of supplied lifecycle samples against predeclared targets.

Ratios compare per-task means; missing verification/delivery limits are total
counts. Wall regression is (after - before) / before (0.1 permits 10%).
Zero baselines allow only zero candidate values. These calculations do not
authenticate input provenance or establish causal/statistical significance.
"""

from dataclasses import dataclass, fields
from math import fsum, inf, isfinite

from .types import LifecycleError


@dataclass(frozen=True)
class Sample:
    task_id: str
    task_class: str
    user_interventions: int
    duplicate_runs: int
    verification_missing: int
    delivery_missing: int
    correction_repeats: int
    wall_seconds: float
    quality_ok: bool


@dataclass(frozen=True)
class Baseline:
    samples: tuple[Sample, ...]

    def __post_init__(self):
        object.__setattr__(self, "samples", tuple(self.samples))


@dataclass(frozen=True)
class Candidate(Baseline):
    pass


@dataclass(frozen=True)
class Targets:
    max_interventions_ratio: float
    max_duplicate_ratio: float
    max_verification_missing: int
    max_delivery_missing: int
    max_correction_repeat_ratio: float
    max_wall_regression_ratio: float


@dataclass(frozen=True)
class Report:
    metrics: dict[str, dict[str, float]]
    quality_regressed: bool
    verdict: str
    unmet: list[str]
    sample_sizes: dict[str, int]
    insufficient_evidence: list[str]

    def summary_lines(self) -> list[str]:
        labels = {"accepted": "입력 표본에서 목표 충족",
                  "implemented_effect_unproven": "구현됨 · 효과 미입증",
                  "regressed": "품질 회귀 또는 필수 품질·안전 기준 위반"}
        lines = [
            f"판정: {self.verdict} — {labels[self.verdict]}",
            "출처 미확인: 아래는 입력 표본의 계산값입니다. 실측 여부는 검증하지 않습니다.",
            "추정치·예측 시나리오를 입력했다면 결과도 추정이며 실측 효과의 증거가 아닙니다.",
            f"표본 수: baseline={self.sample_sizes['baseline']}, "
            f"candidate={self.sample_sizes['candidate']}",
        ]
        for name, metric in self.metrics.items():
            lines.append(f"{name}: before={metric['before']!r}, "
                         f"after={metric['after']!r}, delta={metric['delta']!r}")
        lines.extend([
            f"품질 회귀: {self.quality_regressed}",
            "미달 target: " + (", ".join(self.unmet) or "없음"),
            "표본 부족 task_class: " + (", ".join(self.insufficient_evidence) or "없음"),
        ])
        return lines


_COUNTS = ("user_interventions", "duplicate_runs", "verification_missing",
           "delivery_missing", "correction_repeats")
_TOTALS = {"verification_missing", "delivery_missing", "quality_failures"}


def _nonnegative_finite(value) -> bool:
    return (type(value) in (int, float) and isfinite(value) and value >= 0)


def _validate(samples):
    seen = set()
    for sample in samples:
        if not isinstance(sample, Sample):
            raise LifecycleError("expected Sample")
        if (not isinstance(sample.task_id, str) or not sample.task_id.strip()
                or "\0" in sample.task_id):
            raise LifecycleError("task_id must be nonempty text without NUL")
        if sample.task_id in seen:
            raise LifecycleError(f"duplicate task_id: {sample.task_id}")
        seen.add(sample.task_id)
        if not isinstance(sample.task_class, str) or not sample.task_class.strip():
            raise LifecycleError("task_class must be nonempty")
        if type(sample.quality_ok) is not bool:
            raise LifecycleError("quality_ok must be bool")
        for name in _COUNTS:
            value = getattr(sample, name)
            if type(value) not in (int, bool) or value < 0:
                raise LifecycleError(f"{name} must be a nonnegative count")
        if not _nonnegative_finite(sample.wall_seconds):
            raise LifecycleError("wall_seconds must be finite and nonnegative")


def _ratio(before, after):
    if before == 0:
        return 0.0 if after == 0 else inf
    return after / before


def compare(baseline: Baseline, candidate: Candidate, targets: Targets,
            *, min_samples: int = 3) -> Report:
    """Apply exact thresholds without rounding or mutating targets.

    Any candidate quality_ok=False violates the mandatory quality/safety
    gate, even if the baseline also failed it. quality_regressed separately
    follows the specified absolute failure-count comparison.
    """
    if type(min_samples) is not int or min_samples < 1:
        raise LifecycleError("min_samples must be a positive integer")
    for field in fields(targets):
        if not _nonnegative_finite(getattr(targets, field.name)):
            raise LifecycleError(f"{field.name} must be finite and nonnegative")
    before, after = baseline.samples, candidate.samples
    _validate(before)
    _validate(after)
    classes = {s.task_class for s in before}
    if classes != {s.task_class for s in after}:
        raise LifecycleError("baseline and candidate task_class sets must match")
    before_ids = {s.task_id: s.task_class for s in before}
    after_ids = {s.task_id: s.task_class for s in after}
    if before_ids.keys() != after_ids.keys():
        raise LifecycleError("baseline and candidate task_id sets must match exactly")
    if before_ids != after_ids:
        raise LifecycleError("task_class must match for each paired task_id")
    if not before or not after:
        raise LifecycleError("comparison requires nonempty samples")
    insufficient = sorted(c for c in classes if any(
        sum(s.task_class == c for s in group) < min_samples
        for group in (before, after)))

    metrics = {}
    for name in (*_COUNTS, "wall_seconds", "quality_failures"):
        values = []
        for group in (before, after):
            total = (sum(not s.quality_ok for s in group) if name == "quality_failures"
                     else fsum(getattr(s, name) for s in group))
            values.append(total if name in _TOTALS else total / len(group))
        metrics[name] = {"before": values[0], "after": values[1],
                         "delta": values[1] - values[0]}

    checks = {}
    for name, target in (("user_interventions", "max_interventions_ratio"),
                         ("duplicate_runs", "max_duplicate_ratio"),
                         ("correction_repeats", "max_correction_repeat_ratio")):
        checks[target] = _ratio(metrics[name]["before"], metrics[name]["after"])
    for name in ("verification_missing", "delivery_missing"):
        checks[f"max_{name}"] = metrics[name]["after"]
    wall = metrics["wall_seconds"]
    checks["max_wall_regression_ratio"] = (
        wall["delta"] / wall["before"] if wall["before"] else
        _ratio(wall["before"], wall["after"]))
    unmet = [field.name for field in fields(targets)
             if checks[field.name] > getattr(targets, field.name)]
    quality = metrics["quality_failures"]
    regressed = quality["after"] > quality["before"]
    if regressed or quality["after"] > 0:
        verdict = "regressed"
    elif unmet or insufficient:
        verdict = "implemented_effect_unproven"
    else:
        verdict = "accepted"
    return Report(metrics, regressed, verdict, unmet,
                  {"baseline": len(before_ids), "candidate": len(after_ids)}, insufficient)
