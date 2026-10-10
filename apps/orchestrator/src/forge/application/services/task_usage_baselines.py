"""Application projections for task usage history and frozen references."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from forge.domain.task_usage_baselines import (
    BaselineSample,
    BaselineSummary,
    SamplingRules,
    matches_member,
    matches_route_member,
    summarize_samples,
)
from forge.domain.task_usage_contract import BaselineReference, WorkUnitBinding


class TaskUsageBaselineSource(Protocol):
    async def samples(
        self,
        *,
        project_ids: set[UUID] | None,
        as_of: datetime,
        window_days: int | None,
    ) -> tuple[BaselineSample, ...]: ...

    async def exclusions(
        self,
        *,
        project_ids: set[UUID] | None,
        as_of: datetime,
        window_days: int | None,
        subjects: set[tuple[UUID, str, UUID]],
    ) -> dict[str, int]: ...

    async def accessible_project_ids(self) -> set[UUID]: ...

    async def freeze_baseline(
        self, binding: WorkUnitBinding, *, frozen_at: datetime | None = None
    ) -> BaselineReference | None: ...


@dataclass(frozen=True, slots=True)
class UsageReportFilters:
    project_ids: frozenset[UUID] | None = None
    subject_kind: str | None = None
    task_kind: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    as_of: datetime | None = None
    rules: SamplingRules = field(default_factory=SamplingRules)

    def __post_init__(self) -> None:
        if self.subject_kind not in {None, "run", "authoring_job"}:
            raise ValueError("unknown subject kind filter")
        for name in ("task_kind", "provider", "model", "effort"):
            value = getattr(self, name)
            if value is not None and (not value.strip() or len(value) > 255):
                raise ValueError(f"invalid {name} filter")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone aware")


class TaskUsageBaselineService:
    def __init__(self, source: TaskUsageBaselineSource) -> None:
        self._source = source

    async def freeze_baseline(
        self, binding: WorkUnitBinding, *, frozen_at: datetime | None = None
    ) -> BaselineReference | None:
        return await self._source.freeze_baseline(binding, frozen_at=frozen_at)

    async def report(self, filters: UsageReportFilters | None = None) -> dict[str, object]:
        filters = filters or UsageReportFilters()
        accessible = await self._source.accessible_project_ids()
        requested = filters.project_ids
        project_ids = accessible if requested is None else accessible.intersection(requested)
        as_of = filters.as_of or datetime.now(UTC)
        samples = await self._source.samples(
            project_ids=project_ids,
            as_of=as_of,
            window_days=filters.rules.window_days,
        )
        scoped = [
            sample
            for sample in samples
            if _matches_report_filters(sample, filters, project_ids, as_of)
        ]
        overview = summarize_samples(
            scoped,
            rules=filters.rules,
            as_of=as_of,
            project_ids=project_ids,
            subject_kind=filters.subject_kind,
            task_kind=filters.task_kind,
            provider=filters.provider,
            model=filters.model,
            effort=filters.effort,
            overview=True,
        )
        task_types = sorted({sample.task_kind for sample in scoped})
        by_task_type = {
            kind: _summary_payload(
                summarize_samples(
                    scoped,
                    rules=filters.rules,
                    as_of=as_of,
                    project_ids=project_ids,
                    subject_kind=filters.subject_kind,
                    task_kind=kind,
                    provider=filters.provider,
                    model=filters.model,
                    effort=filters.effort,
                    overview=True,
                )
            )
            for kind in task_types
        }
        by_project = _breakdown(
            scoped,
            rules=filters.rules,
            as_of=as_of,
            group_key=lambda sample: (str(sample.project_id),),
            project_ids=project_ids,
            filters=filters,
        )
        by_provider = _member_breakdown(scoped, dimension="provider", filters=filters, projects=project_ids, as_of=as_of)
        by_model = _member_breakdown(scoped, dimension="model", filters=filters, projects=project_ids, as_of=as_of)
        by_effort = _member_breakdown(scoped, dimension="effort", filters=filters, projects=project_ids, as_of=as_of)
        comparable = _comparable_references(scoped, filters=filters, projects=project_ids, as_of=as_of)
        exclusions = await self._source.exclusions(
            project_ids=project_ids,
            as_of=as_of,
            window_days=filters.rules.window_days,
            subjects={(sample.project_id, sample.subject_kind, sample.subject_id) for sample in scoped},
        )
        return {
            "as_of": as_of.isoformat(),
            "filters": {
                "project_ids": sorted(str(value) for value in project_ids),
                "subject_kind": filters.subject_kind,
                "task_kind": filters.task_kind,
                "provider": filters.provider,
                "model": filters.model,
                "effort": filters.effort,
                "window_days": filters.rules.window_days,
                "history_limit": filters.rules.history_limit,
                "minimum_comparables": filters.rules.minimum_comparables,
            },
            "overview": _summary_payload(overview),
            "by_task_type": by_task_type,
            "by_project": by_project,
            "by_provider": by_provider,
            "by_model": by_model,
            "by_effort": by_effort,
            "comparable_references": comparable,
            "excluded_outcomes": exclusions,
            "failed_attempt_count": sum(sample.failed_attempt_count for sample in scoped),
        }


def _matches_report_filters(
    sample: BaselineSample,
    filters: UsageReportFilters,
    projects: set[UUID],
    as_of: datetime,
) -> bool:
    if sample.project_id not in projects or sample.completed_at > as_of:
        return False
    if filters.rules.window_days is not None:
        try:
            lower = as_of - timedelta(days=filters.rules.window_days)
        except OverflowError:
            lower = datetime.min.replace(tzinfo=as_of.tzinfo)
        if sample.completed_at < lower:
            return False
    if filters.subject_kind is not None and sample.subject_kind != filters.subject_kind:
        return False
    if filters.task_kind is not None and sample.task_kind != filters.task_kind:
        return False
    return matches_route_member(sample, provider=filters.provider, model=filters.model,
                                effort=filters.effort)


def _summary_payload(summary: BaselineSummary) -> dict[str, object]:
    return {
        "status": summary.status,
        "mean_tokens": summary.mean_tokens,
        "median_tokens": summary.median_tokens,
        "p90_tokens": summary.p90_tokens,
        "reference_tokens": summary.reference_tokens,
        "sample_count": summary.sample_count,
        "eligible_count": summary.eligible_count,
        "excluded_count": summary.excluded_count,
        "measured_coverage": summary.coverage,
        "coverage_count": summary.coverage_count,
        "window_started_at": summary.window_started_at.isoformat()
        if summary.window_started_at
        else None,
        "window_ended_at": summary.window_ended_at.isoformat()
        if summary.window_ended_at
        else None,
        "cohort_source": summary.cohort_source,
        "project_ids": [str(value) for value in summary.project_ids],
        "labels": list(summary.labels),
        "phase_role_breakdown": [
            {"phase": phase, "role": role, "tokens": tokens}
            for phase, role, tokens in summary.phase_role_breakdown
        ],
        "warning_threshold_tokens": summary.warning_threshold_tokens,
        "checkpoint_threshold_tokens": summary.checkpoint_threshold_tokens,
    }


def _breakdown(
    samples: list[BaselineSample],
    *,
    rules: SamplingRules,
    as_of: datetime,
    group_key: Callable[[BaselineSample], tuple[str, ...]],
    project_ids: set[UUID],
    filters: UsageReportFilters,
) -> dict[str, dict[str, object]]:
    groups: dict[tuple[str, ...], list[BaselineSample]] = {}
    for sample in samples:
        groups.setdefault(group_key(sample), []).append(sample)
    return {
        "/".join(key): _summary_payload(
            summarize_samples(
                rows,
                rules=rules,
                as_of=as_of,
                project_ids=project_ids,
                subject_kind=filters.subject_kind,
                task_kind=filters.task_kind,
                provider=filters.provider,
                model=filters.model,
                effort=filters.effort,
                overview=True,
            )
        )
        for key, rows in sorted(groups.items())
    }


def _member_breakdown(
    samples: list[BaselineSample],
    *,
    dimension: str,
    filters: UsageReportFilters,
    projects: set[UUID],
    as_of: datetime,
) -> dict[str, dict[str, object]]:
    values: set[str] = set()
    for sample in samples:
        if sample.route_members:
            index = {"provider": 1, "model": 2, "effort": 3}[dimension]
            for member in sample.route_members:
                if not matches_member(member, provider=filters.provider,
                                      model=filters.model, effort=filters.effort):
                    continue
                value = member[index]
                if isinstance(value, str) and value:
                    values.add(value)
        else:
            value = getattr(sample, dimension)
            if value and matches_route_member(sample, provider=filters.provider,
                                              model=filters.model, effort=filters.effort):
                values.add(value)
    return {
        value: _summary_payload(
            summarize_samples(
                samples,
                rules=filters.rules,
                as_of=as_of,
                project_ids=projects,
                subject_kind=filters.subject_kind,
                task_kind=filters.task_kind,
                provider=value if dimension == "provider" else filters.provider,
                model=value if dimension == "model" else filters.model,
                effort=value if dimension == "effort" else filters.effort,
                overview=True,
            )
        )
        for value in sorted(values)
    }


def _comparable_references(
    samples: list[BaselineSample],
    *,
    filters: UsageReportFilters,
    projects: set[UUID],
    as_of: datetime,
) -> dict[str, dict[str, object]]:
    cohorts = {
        (
            sample.project_id,
            sample.subject_kind,
            sample.task_kind,
            sample.configured_route,
            sample.effective_route,
        )
        for sample in samples
        if sample.configured_route is not None and sample.effective_route is not None
    }
    output: dict[str, dict[str, object]] = {}
    for project, subject_kind, task_kind, configured, effective in sorted(
        cohorts, key=lambda value: tuple(str(item) for item in value)
    ):
        key = ":".join(
            (str(project), subject_kind, task_kind, str(configured), str(effective))
        )
        output[key] = _summary_payload(
            summarize_samples(
                samples,
                rules=filters.rules,
                as_of=as_of,
                project_ids=projects,
                subject_kind=subject_kind,
                task_kind=task_kind,
                configured_route=configured,
                effective_route=effective,
                target_project_id=project,
            )
        )
    return output
