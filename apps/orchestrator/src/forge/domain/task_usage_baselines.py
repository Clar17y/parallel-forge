"""Deterministic statistics and cohort selection for measured task usage."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import fmean, median
from typing import Literal
from uuid import UUID

from pydantic import StrictInt

Outcome = Literal["accepted", "completed", "failed", "cancelled", "truncated", "unknown"]
MAX_SIGNED_BIGINT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class BaselineSample:
    project_id: UUID
    subject_kind: str
    subject_id: UUID
    completed_at: datetime
    total_tokens: StrictInt | None
    outcome: Outcome
    finalized: bool
    measured: bool
    configured_route: str | None
    effective_route: str | None
    task_kind: str = "implementation"
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    route_members: tuple[tuple[str, str, str, str | None], ...] = ()
    phase_role_breakdown: tuple[tuple[str, str, int | None], ...] = ()
    source_count: int = 1
    failed_attempt_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, UUID) or not isinstance(self.subject_id, UUID):
            raise TypeError("sample identities must be UUIDs")
        if self.subject_kind not in {"run", "authoring_job"}:
            raise ValueError("only top-level run and authoring subjects are samples")
        if self.completed_at.tzinfo is None:
            raise ValueError("sample time must be timezone aware")
        if self.total_tokens is not None and (
            type(self.total_tokens) is not int
            or not 0 <= self.total_tokens <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("measured total must be a nonnegative signed-64-bit integer")
        if type(self.finalized) is not bool or type(self.measured) is not bool:
            raise TypeError("sample evidence flags must be booleans")
        if self.measured != (self.total_tokens is not None):
            raise ValueError("measurement flag must agree with measured total")
        if type(self.source_count) is not int or not 0 <= self.source_count <= MAX_SIGNED_BIGINT:
            raise ValueError("source count must be nonnegative")
        if (
            type(self.failed_attempt_count) is not int
            or not 0 <= self.failed_attempt_count <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("failed attempt count must be nonnegative")


@dataclass(frozen=True, slots=True)
class SamplingRules:
    history_limit: StrictInt = 50
    minimum_comparables: StrictInt = 20
    window_days: StrictInt | None = None
    allow_cross_project_fallback: bool = True
    manual_estimate_tokens: StrictInt | None = None
    warning_multiplier: float = 1.5
    checkpoint_multiplier: float = 2.0

    def __post_init__(self) -> None:
        for name in ("history_limit", "minimum_comparables"):
            value = getattr(self, name)
            if type(value) is not int or value < 1 or value > 2**63 - 1:
                raise ValueError(f"{name} must be a positive signed-64-bit integer")
        if self.minimum_comparables > self.history_limit:
            raise ValueError("minimum comparables exceeds history limit")
        if self.window_days is not None and (
            type(self.window_days) is not int or not 1 <= self.window_days <= 2**63 - 1
        ):
            raise ValueError("window days must be a positive signed-64-bit integer")
        if self.manual_estimate_tokens is not None and (
            type(self.manual_estimate_tokens) is not int
            or not 1 <= self.manual_estimate_tokens <= 2**63 - 1
        ):
            raise ValueError("manual estimate must be a positive signed-64-bit integer")
        if (
            not math.isfinite(self.warning_multiplier)
            or not math.isfinite(self.checkpoint_multiplier)
            or self.warning_multiplier < 1
            or self.checkpoint_multiplier < self.warning_multiplier
        ):
            raise ValueError("threshold multipliers are invalid")


@dataclass(frozen=True, slots=True)
class BaselineSummary:
    status: str
    mean_tokens: float | None
    median_tokens: float | None
    p90_tokens: float | None
    reference_tokens: float | None
    sample_count: int
    eligible_count: int
    excluded_count: int
    coverage_count: int
    coverage: float | None
    window_started_at: datetime | None
    window_ended_at: datetime | None
    cohort_source: str
    project_ids: tuple[UUID, ...]
    labels: tuple[str, ...]
    phase_role_breakdown: tuple[tuple[str, str, int | None], ...]
    warning_threshold_tokens: float | None = None
    checkpoint_threshold_tokens: float | None = None
    failed_attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class CohortSelection:
    samples: tuple[BaselineSample, ...]
    excluded_count: int
    cohort_source: str
    inaccessible: bool = False


def matches_route_member(
    sample: BaselineSample, *, provider: str | None = None,
    model: str | None = None, effort: str | None = None,
) -> bool:
    if sample.route_members:
        return any(
            matches_member(member, provider=provider, model=model, effort=effort)
            for member in sample.route_members
        )
    return (
        (provider is None or sample.provider == provider)
        and (model is None or sample.model == model)
        and (effort is None or sample.effort == effort)
    )


def matches_member(
    member: tuple[str, str, str, str | None], *, provider: str | None = None,
    model: str | None = None, effort: str | None = None,
) -> bool:
    return (
        (provider is None or member[1] == provider)
        and (model is None or member[2] == model)
        and (effort is None or member[3] == effort)
    )


def _p90(values: list[int]) -> float:
    ordered = sorted(values)
    rank = 0.9 * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower))


def summarize_samples(
    samples: list[BaselineSample] | tuple[BaselineSample, ...],
    *,
    rules: SamplingRules,
    as_of: datetime,
    project_ids: set[UUID] | frozenset[UUID] | None = None,
    subject_kind: str | None = None,
    task_kind: str | None = None,
    configured_route: str | None = None,
    effective_route: str | None = None,
    target_project_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    overview: bool = False,
) -> BaselineSummary:
    """Summarize eligible source facts after project access and cohort filters."""
    selection = select_cohort(
        samples, rules=rules, as_of=as_of, project_ids=project_ids,
        subject_kind=subject_kind, task_kind=task_kind,
        configured_route=configured_route, effective_route=effective_route,
        target_project_id=target_project_id, provider=provider, model=model,
        effort=effort, overview=overview,
    )
    return summarize_selection(selection, rules=rules, overview=overview)


def select_cohort(
    samples: list[BaselineSample] | tuple[BaselineSample, ...],
    *, rules: SamplingRules, as_of: datetime,
    project_ids: set[UUID] | frozenset[UUID] | None = None,
    subject_kind: str | None = None, task_kind: str | None = None,
    configured_route: str | None = None, effective_route: str | None = None,
    target_project_id: UUID | None = None, provider: str | None = None,
    model: str | None = None, effort: str | None = None,
    overview: bool = False,
) -> CohortSelection:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone aware")
    if target_project_id is not None and project_ids is not None and target_project_id not in project_ids:
        # An inaccessible target is never included in a digest or sample count.
        return CohortSelection((), 0, "none", inaccessible=True)
    accessible = [
        sample
        for sample in samples
        if project_ids is None or sample.project_id in project_ids
    ]
    if rules.window_days is None:
        lower_bound = None
    else:
        try:
            lower_bound = as_of - timedelta(days=rules.window_days)
        except OverflowError:
            lower_bound = datetime.min.replace(tzinfo=as_of.tzinfo)
    in_window = [
        sample
        for sample in accessible
        if sample.completed_at <= as_of
        and (lower_bound is None or sample.completed_at >= lower_bound)
    ]
    filtered = [
        sample
        for sample in in_window
        if (subject_kind is None or sample.subject_kind == subject_kind)
        and (task_kind is None or sample.task_kind == task_kind)
        and matches_route_member(sample, provider=provider, model=model, effort=effort)
    ]
    finalized_outcomes = [
        sample
        for sample in filtered
        if sample.finalized and sample.outcome in {"accepted", "completed"}
    ]
    excluded_count = len(filtered) - len(finalized_outcomes)
    exact = finalized_outcomes
    if not overview:
        exact = [
            sample
            for sample in finalized_outcomes
            if (target_project_id is None or sample.project_id == target_project_id)
            and (configured_route is None or sample.configured_route == configured_route)
            and (effective_route is None or sample.effective_route == effective_route)
        ]
    selected = sorted(exact, key=lambda item: (item.completed_at, str(item.subject_id)))[
        -rules.history_limit :
    ]
    cohort_source = "overview" if overview else "exact_project_route"
    if (
        not overview
        and len([sample for sample in selected if sample.measured]) < rules.minimum_comparables
        and rules.allow_cross_project_fallback
        and target_project_id is not None
    ):
        cross = [
            sample
            for sample in finalized_outcomes
            if sample.project_id != target_project_id
            and (configured_route is None or sample.configured_route == configured_route)
            and (effective_route is None or sample.effective_route == effective_route)
        ]
        selected = sorted(exact + cross, key=lambda item: (item.completed_at, str(item.subject_id)))[
            -rules.history_limit :
        ]
        cohort_source = "accessible_cross_project"
    return CohortSelection(tuple(selected), excluded_count, cohort_source)


def summarize_selection(
    selection: CohortSelection, *, rules: SamplingRules, overview: bool = False,
) -> BaselineSummary:
    if selection.inaccessible:
        return _empty_summary("inaccessible", rules)
    selected = selection.samples
    excluded_count = selection.excluded_count
    cohort_source = selection.cohort_source
    measured = [sample for sample in selected if sample.measured and sample.total_tokens is not None]
    if len(measured) < rules.minimum_comparables:
        if not overview and rules.manual_estimate_tokens is not None:
            estimate = float(rules.manual_estimate_tokens)
            return BaselineSummary(
                status="manual",
                mean_tokens=None,
                median_tokens=None,
                p90_tokens=None,
                reference_tokens=estimate,
                sample_count=0,
                eligible_count=0,
                excluded_count=excluded_count,
                coverage_count=0,
                coverage=None,
                window_started_at=None,
                window_ended_at=None,
                cohort_source="manual",
                project_ids=(),
                labels=("manual_estimate",),
                phase_role_breakdown=(),
                warning_threshold_tokens=estimate * rules.warning_multiplier,
                checkpoint_threshold_tokens=estimate * rules.checkpoint_multiplier,
            )
        if not measured:
            result = _empty_summary("insufficient_history", rules)
            return replace(
                result,
                eligible_count=len(selected),
                excluded_count=excluded_count,
                coverage_count=0,
                coverage=0.0 if selected else None,
                window_started_at=min((item.completed_at for item in selected), default=None),
                window_ended_at=max((item.completed_at for item in selected), default=None),
                cohort_source=cohort_source,
                project_ids=tuple(sorted({sample.project_id for sample in selected}, key=str)),
                phase_role_breakdown=_breakdown(selected),
                failed_attempt_count=sum(sample.failed_attempt_count for sample in selected),
            )
    values = [sample.total_tokens for sample in measured]
    assert all(value is not None for value in values)
    numeric = [int(value) for value in values if value is not None]
    mean_value = fmean(numeric)
    median_value = float(median(numeric))
    p90_value = _p90(numeric)
    coverage = len(measured) / len(selected) if selected else None
    ready = len(numeric) >= rules.minimum_comparables
    return BaselineSummary(
        status="ready" if ready else "insufficient_history",
        mean_tokens=mean_value,
        median_tokens=median_value,
        p90_tokens=p90_value,
        reference_tokens=max(median_value, p90_value) if ready else None,
        sample_count=len(measured),
        eligible_count=len(selected),
        excluded_count=excluded_count,
        coverage_count=len(measured),
        coverage=coverage,
        window_started_at=min(item.completed_at for item in selected),
        window_ended_at=max(item.completed_at for item in selected),
        cohort_source=cohort_source,
        project_ids=tuple(sorted({sample.project_id for sample in selected}, key=str)),
        labels=(
            tuple(
                label
                for label, condition in (
                    ("overview", overview),
                    (
                        "mixed_route",
                        any(len(sample.route_members) > 1 for sample in selected)
                        or len({(s.configured_route, s.effective_route) for s in selected}) > 1,
                    ),
                    (
                        "legacy_route_unclassified",
                        any(sample.configured_route is None for sample in selected),
                    ),
                )
                if condition
            )
            if overview
            else tuple(
                label
                for label, condition in (
                    ("accessible_cross_project", cohort_source == "accessible_cross_project"),
                    (
                        "mixed_route",
                        len({(s.configured_route, s.effective_route) for s in selected}) > 1
                        or any(len(sample.route_members) > 1 for sample in selected),
                    ),
                )
                if condition
            )
        ),
        phase_role_breakdown=_breakdown(selected),
        warning_threshold_tokens=median_value * rules.warning_multiplier if ready else None,
        checkpoint_threshold_tokens=(
            max(median_value * rules.checkpoint_multiplier, p90_value) if ready else None
        ),
        failed_attempt_count=sum(sample.failed_attempt_count for sample in selected),
    )


def _breakdown(samples: list[BaselineSample] | tuple[BaselineSample, ...]) -> tuple[tuple[str, str, int | None], ...]:
    totals: dict[tuple[str, str], int] = {}
    unknown: set[tuple[str, str]] = set()
    for sample in samples:
        for phase, role, amount in sample.phase_role_breakdown:
            key = (phase, role)
            if amount is None:
                unknown.add(key)
            else:
                totals[key] = totals.get(key, 0) + amount
    keys = set(totals) | unknown
    return tuple(
        (phase, role, None if (phase, role) in unknown else totals[(phase, role)])
        for phase, role in sorted(keys)
    )


def _empty_summary(status: str, rules: SamplingRules) -> BaselineSummary:
    return BaselineSummary(
        status=status,
        mean_tokens=None,
        median_tokens=None,
        p90_tokens=None,
        reference_tokens=None,
        sample_count=0,
        eligible_count=0,
        excluded_count=0,
        coverage_count=0,
        coverage=None,
        window_started_at=None,
        window_ended_at=None,
        cohort_source="none",
        project_ids=(),
        labels=(),
        phase_role_breakdown=(),
        failed_attempt_count=0,
    )
