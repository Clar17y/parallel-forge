"""Versioned, transparent metrics for deterministic agent evaluation."""

import math
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import cast

from forge.domain.plan import PlanOutput

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
