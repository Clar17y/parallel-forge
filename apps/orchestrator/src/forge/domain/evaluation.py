"""Versioned, transparent metrics for deterministic agent evaluation."""

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import cast

from forge.domain.plan import PlanOutput
from forge.domain.review import FindingSeverity, ReviewFinding

PLANNER_METRIC_VERSION = "planner-exact-v1"


@dataclass(frozen=True)
class PlanScores:
    component_recall: float
    check_recall: float
    risk_recall: float
    dependency_disclosure: float
    policy_compliance: float
    schema_validity: float


def _recall(expected: AbstractSet[str], actual: Sequence[str]) -> float:
    """Exact declared labels; an empty expected set imposes no requirement."""
    return len(expected & set(actual)) / len(expected) if expected else 1.0


def score_plan(
    *, expected_components: AbstractSet[str], expected_checks: AbstractSet[str], expected_risks: AbstractSet[str],
    actual: PlanOutput | None, denied_tool_calls: Sequence[str],
    expected_dependencies: AbstractSet[str] = frozenset(),
) -> PlanScores:
    """Score a schema-validated plan, or None for invalid/missing structured output.

    Fixtures declare exact component/check/risk/dependency labels. No substring,
    prose similarity or provider judgment affects the score. A denied tool attempt
    fails policy compliance independently of schema validity and content recall.
    """
    compliant = float(not denied_tool_calls)
    if actual is None:
        return PlanScores(0.0, 0.0, 0.0, 0.0, compliant, 0.0)
    return PlanScores(
        component_recall=_recall(expected_components, actual.affected_components),
        check_recall=_recall(expected_checks, actual.required_checks),
        risk_recall=_recall(expected_risks, actual.risks),
        dependency_disclosure=_recall(expected_dependencies, actual.dependency_changes),
        policy_compliance=compliant,
        schema_validity=1.0,
    )


def regression_failures(
    *, fixture_version: str, metric_version: str,
    baseline_fixture_version: str, baseline_metric_version: str,
    metrics: Mapping[str, object], floors: Mapping[str, float], ceilings: Mapping[str, float],
) -> tuple[str, ...]:
    """Return failed metric names; unavailable scores never satisfy a declared bound."""
    if (
        not fixture_version or not metric_version
        or fixture_version != baseline_fixture_version or metric_version != baseline_metric_version
    ):
        raise ValueError("evaluation fixture or metric versions differ")
    failed: list[str] = []
    for bounds, lower in ((floors, True), (ceilings, False)):
        for name, threshold in bounds.items():
            if not _finite_number(threshold):
                raise ValueError("evaluation thresholds must be finite numeric values")
            value = metrics.get(name)
            valid = _finite_number(value)
            outside = not valid or (cast(float, value) < threshold if lower else cast(float, value) > threshold)
            if outside and name not in failed:
                failed.append(name)
    return tuple(failed)


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(cast(float, value))
    except OverflowError:
        return False


REVIEWER_METRIC_VERSION = "reviewer-grounded-v1"


@dataclass(frozen=True)
class SeededDefect:
    severity: FindingSeverity
    path: str
    start_line: int
    evidence_anchor: str
    missing_test: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.severity, FindingSeverity)
            or not self.path.strip() or not self.evidence_anchor.strip()
            or type(self.start_line) is not int or self.start_line < 1
            or type(self.missing_test) is not bool
        ):
            raise ValueError("seeded defect requires severity, location and evidence")


@dataclass(frozen=True)
class ReviewScores:
    defect_recall: float
    blocker_recall: float
    major_recall: float
    minor_recall: float
    suggestion_recall: float
    false_positive_count: int
    blocker_false_positive_count: int
    evidence_quality: float
    missing_test_recall: float
    policy_compliance: float
    schema_validity: float


def score_review(
    *, seeded_defects: Mapping[str, SeededDefect], findings: Sequence[ReviewFinding] | None,
    denied_tool_calls: Sequence[str] = (),
) -> ReviewScores:
    """Match exact fixture location/severity and its declared evidence anchor.

    Finding IDs are agent-generated and do not earn credit. Fixtures must enumerate
    ground truth: unmatched findings count as false positives. Evidence quality is
    the fraction of findings grounded in that truth, not a language-model judgment.
    Duplicate reports cannot increase seeded-defect recall. None means invalid output.
    """
    compliant = float(not denied_tool_calls)
    if findings is None:
        return ReviewScores(0, 0, 0, 0, 0, 0, 0, 0, 0, compliant, 0)
    detected: set[str] = set()
    unmatched: list[ReviewFinding] = []
    for finding in findings:
        matches = {key for key, seed in seeded_defects.items() if (
            finding.path == seed.path and finding.start_line == seed.start_line
            and finding.severity == seed.severity and seed.evidence_anchor in finding.evidence
        )}
        if matches:
            detected.update(matches)
        else:
            unmatched.append(finding)

    def recall(keys: AbstractSet[str]) -> float:
        return _recall(keys, tuple(detected))

    severity_recalls = {severity: recall({key for key, seed in seeded_defects.items() if seed.severity == severity})
                        for severity in FindingSeverity}
    return ReviewScores(
        defect_recall=recall(set(seeded_defects)),
        blocker_recall=severity_recalls[FindingSeverity.BLOCKER], major_recall=severity_recalls[FindingSeverity.MAJOR],
        minor_recall=severity_recalls[FindingSeverity.MINOR], suggestion_recall=severity_recalls[FindingSeverity.SUGGESTION],
        false_positive_count=len(unmatched),
        blocker_false_positive_count=sum(f.severity is FindingSeverity.BLOCKER for f in unmatched),
        evidence_quality=(len(findings) - len(unmatched)) / len(findings) if findings else float(not seeded_defects),
        missing_test_recall=recall({key for key, seed in seeded_defects.items() if seed.missing_test}),
        policy_compliance=compliant, schema_validity=1.0,
    )
