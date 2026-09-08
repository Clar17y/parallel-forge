"""Versioned, transparent metrics for deterministic agent evaluation."""

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import cast

from forge.domain.agent import DeveloperOutput
from forge.domain.plan import PlanOutput
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.observability.usage import UsageRecord

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


DEVELOPER_METRIC_VERSION = "developer-observed-v1"


@dataclass(frozen=True)
class DeveloperScores:
    required_test_pass: float
    diff_scope_precision: float
    named_check_success: float
    policy_compliance: float
    remediation_count: int
    task_assertion_pass: float
    schema_validity: float


def score_development(
    *, actual: DeveloperOutput | None, changed_paths: AbstractSet[str], allowed_paths: AbstractSet[str],
    required_tests: AbstractSet[str], test_results: Mapping[str, object],
    required_checks: AbstractSet[str], check_results: Mapping[str, object],
    required_assertions: AbstractSet[str], assertion_results: Mapping[str, object],
    denied_tool_calls: Sequence[str], remediation_count: int,
) -> DeveloperScores:
    """Score independent harness observations; output claims are not check evidence.

    Paths are exact fixture-declared repository paths. Missing/unknown outcomes fail
    their individual requirement. Empty expected sets impose no requirement, and
    an empty diff has vacuous scope precision; task assertions still decide success.
    """
    if type(remediation_count) is not int or remediation_count < 0:
        raise ValueError("remediation count must be a nonnegative integer")

    def passed(required: AbstractSet[str], results: Mapping[str, object]) -> float:
        return _recall(required, tuple(name for name, result in results.items() if result is True))

    return DeveloperScores(
        required_test_pass=passed(required_tests, test_results),
        diff_scope_precision=len(changed_paths & allowed_paths) / len(changed_paths) if changed_paths else 1.0,
        named_check_success=passed(required_checks, check_results),
        policy_compliance=float(not denied_tool_calls), remediation_count=remediation_count,
        task_assertion_pass=passed(required_assertions, assertion_results),
        schema_validity=float(actual is not None),
    )


COMMON_METRIC_VERSION = "usage-observed-v1"


@dataclass(frozen=True)
class CommonScores:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    estimated_cost_minor: int | None
    currency: str | None
    duration_ms: int
    tool_count: int
    denied_calls: int
    human_acceptance: bool | None


def score_usage(
    usage: UsageRecord, *, denied_tool_calls: Sequence[str], human_acceptance: bool | None = None,
) -> CommonScores:
    """Retain measured units; cached input is a subset, unknown cost is not zero."""
    if human_acceptance is not None and type(human_acceptance) is not bool:
        raise ValueError("human acceptance must be explicit boolean or unknown")
    return CommonScores(
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        total_tokens=usage.input_tokens + usage.output_tokens,
        estimated_cost_minor=usage.estimated_cost_minor, currency=usage.currency,
        duration_ms=usage.duration_ms, tool_count=usage.tool_call_count,
        denied_calls=len(denied_tool_calls), human_acceptance=human_acceptance,
    )
