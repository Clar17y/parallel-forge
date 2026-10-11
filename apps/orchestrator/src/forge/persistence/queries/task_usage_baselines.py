"""Authorized historical source facts and immutable baseline projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import exists, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Exists

from forge.domain.subscription import LogicalTaskContract, decode_subscription_record
from forge.domain.task_usage_baselines import (
    BaselineSample,
    SamplingRules,
    select_cohort,
    summarize_selection,
)
from forge.domain.task_usage_contract import (
    BaselineReference,
    RouteCohort,
    RouteMember,
    SubjectKey,
    WorkUnitBinding,
)
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.models.execution import AgentExecution, EvidenceSet, ModelUsage, Step
from forge.persistence.models.project import Project
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.task_usage import (
    TaskUsageBaseline,
    TaskUsagePolicyRevision,
    TaskUsageWorkUnit,
)
from forge.persistence.queries.task_usage_totals import FinalizedSourceFact, TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import decode_binding
from forge.persistence.repositories.task_usage_policy import policy_from_row

_BATCH_SIZE = 1000


def _candidate_batches(
    keys: set[tuple[UUID, str, UUID]],
) -> Iterator[list[tuple[UUID, str, UUID]]]:
    ordered = sorted(keys, key=lambda key: (str(key[0]), key[1], str(key[2])))
    for start in range(0, len(ordered), _BATCH_SIZE):
        yield ordered[start : start + _BATCH_SIZE]


def _id_batches(identities: set[UUID] | list[UUID]) -> Iterator[list[UUID]]:
    ordered = sorted(identities, key=str)
    for start in range(0, len(ordered), _BATCH_SIZE):
        yield ordered[start : start + _BATCH_SIZE]


def _validate_report_window(as_of: datetime, window_days: int | None) -> None:
    if as_of.tzinfo is None or (
        window_days is not None and (type(window_days) is not int or window_days < 1)
    ):
        raise ValueError("report time window is invalid")


async def _candidate_units(
    session: AsyncSession, keys: set[tuple[UUID, str, UUID]], *, as_of: datetime,
) -> dict[tuple[UUID, str, UUID], list[TaskUsageWorkUnit]]:
    units: dict[tuple[UUID, str, UUID], list[TaskUsageWorkUnit]] = {}
    for batch in _candidate_batches(keys):
        scalar_rows = await session.scalars(
            select(TaskUsageWorkUnit).where(
                tuple_(TaskUsageWorkUnit.project_id, TaskUsageWorkUnit.subject_kind,
                       TaskUsageWorkUnit.subject_id).in_(batch),
                TaskUsageWorkUnit.created_at <= as_of,
            ).order_by(TaskUsageWorkUnit.created_at, TaskUsageWorkUnit.id)
        )
        for unit in scalar_rows.all():
            units.setdefault((unit.project_id, unit.subject_kind, unit.subject_id), []).append(unit)
    for subject_units in units.values():
        subject_units.sort(key=lambda unit: (unit.created_at, str(unit.id)))
    return units


class TaskUsageBaselineQuery:
    """Read reports through authenticated-operator project scope and final facts."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._totals = TaskUsageTotalsQuery(factory)

    @asynccontextmanager
    async def report_snapshot(self) -> AsyncIterator[_TaskUsageReportSnapshot]:
        """Keep access, samples and exclusions on one repeatable-read transaction."""
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            yield _TaskUsageReportSnapshot(self, session)

    async def accessible_project_ids(self) -> set[UUID]:
        # Current product authorization grants operators access to all projects.
        # Keep this one seam central so a future project ACL can narrow it safely.
        async with self._factory() as session:
            return set((await session.scalars(select(Project.id))).all())

    async def freeze_baseline(
        self, binding: WorkUnitBinding, *, frozen_at: datetime | None = None
    ) -> BaselineReference | None:
        for attempt in range(3):
            try:
                return await self._freeze_baseline_once(binding, frozen_at=frozen_at)
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) != "40001" or attempt == 2:
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise RuntimeError("baseline freeze retry loop exhausted")

    async def _freeze_baseline_once(
        self, binding: WorkUnitBinding, *, frozen_at: datetime | None = None
    ) -> BaselineReference | None:
        """Freeze the current measured cohort exactly once for an admitted subject."""
        if not isinstance(binding, WorkUnitBinding):
            raise TypeError("baseline admission requires a typed work-unit binding")
        frozen_at = frozen_at or datetime.now(UTC)
        if frozen_at.tzinfo is None:
            raise ValueError("baseline freeze time must be timezone aware")
        async with self._factory() as session, session.begin():
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            existing = await session.scalar(
                select(TaskUsageBaseline).where(
                    TaskUsageBaseline.project_id == binding.subject.project_id,
                    TaskUsageBaseline.subject_kind == binding.subject.kind,
                    TaskUsageBaseline.subject_id == binding.subject.subject_id,
                )
            )
            if existing is not None:
                unit = await session.get(TaskUsageWorkUnit, existing.work_unit_id)
                if unit is None:
                    raise RuntimeError("frozen baseline lost its admitted work unit")
                return _reference_from_row(existing, decode_binding(unit))
            unit = await session.get(TaskUsageWorkUnit, binding.work_unit_id)
            if unit is None or (
                unit.project_id,
                unit.subject_kind,
                unit.subject_id,
            ) != (
                binding.subject.project_id,
                binding.subject.kind,
                binding.subject.subject_id,
            ):
                raise ValueError("baseline binding is not the durable admitted work unit")
            policy_row = await session.scalar(
                select(TaskUsagePolicyRevision)
                .where(TaskUsagePolicyRevision.project_id == binding.subject.project_id)
                .order_by(TaskUsagePolicyRevision.revision.desc())
                .limit(1)
            )
            if policy_row is None:
                return None
            policy = policy_from_row(policy_row)
            all_projects = await session.scalars(select(Project.id))
            accessible_projects = set(all_projects.all())
            history = await self._samples(
                session,
                project_ids=accessible_projects,
                as_of=frozen_at,
                window_days=policy.window_days,
            )
            history = tuple(
                sample
                for sample in history
                if sample.subject_id != binding.subject.subject_id
                and sample.subject_kind == binding.subject.kind
                and sample.task_kind == _task_kind(binding.subject.kind)
            )
            rules = SamplingRules(
                history_limit=policy.history_limit,
                minimum_comparables=policy.minimum_comparables,
                allow_cross_project_fallback=policy.allow_cross_project_fallback,
                manual_estimate_tokens=policy.manual_estimate_tokens,
                window_days=policy.window_days,
                warning_multiplier=policy.warning_multiplier,
                checkpoint_multiplier=policy.checkpoint_multiplier,
            )
            selection = select_cohort(
                history,
                rules=rules,
                as_of=frozen_at,
                project_ids=accessible_projects,
                subject_kind=binding.subject.kind,
                task_kind=_task_kind(binding.subject.kind),
                configured_route=binding.configured_route.fingerprint,
                effective_route=binding.effective_route.fingerprint,
                target_project_id=binding.subject.project_id,
            )
            summary = summarize_selection(selection, rules=rules)
            selected_history = selection.samples if summary.status != "manual" else ()
            history_facts = [
                {
                    "project_id": str(sample.project_id),
                    "subject_kind": sample.subject_kind,
                    "subject_id": str(sample.subject_id),
                    "completed_at": sample.completed_at.isoformat(),
                    "outcome": sample.outcome,
                    "total_tokens": sample.total_tokens,
                    "finalized": sample.finalized,
                    "source_count": sample.source_count,
                    "configured_route": sample.configured_route,
                    "effective_route": sample.effective_route,
                }
                for sample in selected_history
            ]
            history_digest = _digest(history_facts)
            payload: dict[str, object] = {
                "schema_version": 1,
                "source": "manual" if summary.status == "manual" else "measured_history",
                "status": summary.status,
                "completeness": (
                    "unknown"
                    if summary.status == "manual"
                    else "complete" if summary.coverage == 1 else "lower_bound"
                ),
                "cohort_source": summary.cohort_source,
                "labels": list(summary.labels),
                "policy_revision": policy.revision,
                "window_days": policy.window_days,
                "policy_actor_id": str(policy_row.actor_id) if policy_row is not None else None,
                "history_digest": history_digest,
                "sample_count": summary.sample_count,
                "outcome_count": summary.eligible_count,
                "coverage_count": summary.coverage_count,
                "coverage": summary.coverage,
                "mean_tokens": summary.mean_tokens,
                "median_tokens": summary.median_tokens,
                "p90_tokens": summary.p90_tokens,
                "reference_tokens": summary.reference_tokens,
                "window_started_at": summary.window_started_at.isoformat()
                if summary.window_started_at
                else None,
                "window_ended_at": summary.window_ended_at.isoformat()
                if summary.window_ended_at
                else None,
                "warning_threshold_tokens": summary.warning_threshold_tokens,
                "checkpoint_threshold_tokens": summary.checkpoint_threshold_tokens,
            }
            snapshot_digest = _digest(payload)
            reference = BaselineReference(
                binding=binding,
                policy_revision=policy.revision,
                sample_count=summary.sample_count,
                status=summary.status,
                history_digest=history_digest,
                snapshot_digest=snapshot_digest,
                estimator_version="mean-median-linear-p90-v1",
                window_limit=policy.history_limit,
                minimum_comparables=policy.minimum_comparables,
                cohort_digest=binding.configured_route.fingerprint,
                reference_tokens=summary.reference_tokens,
                median_tokens=summary.median_tokens,
                p90_tokens=summary.p90_tokens,
                mean_tokens=summary.mean_tokens,
                coverage_count=summary.coverage_count,
                outcome_count=0 if summary.status == "manual" else summary.eligible_count,
                source_count=sum(sample.source_count for sample in selected_history),
                window_started_at=summary.window_started_at,
                window_ended_at=summary.window_ended_at,
                warning_threshold_tokens=summary.warning_threshold_tokens,
                checkpoint_threshold_tokens=summary.checkpoint_threshold_tokens,
                frozen_at=frozen_at,
                completeness=str(payload["completeness"]),
            )
            values = _baseline_values(reference, payload)
            statement = (
                insert(TaskUsageBaseline)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=("project_id", "subject_kind", "subject_id")
                )
            )
            await session.execute(statement)
            row = await session.scalar(
                select(TaskUsageBaseline).where(
                    TaskUsageBaseline.project_id == binding.subject.project_id,
                    TaskUsageBaseline.subject_kind == binding.subject.kind,
                    TaskUsageBaseline.subject_id == binding.subject.subject_id,
                )
            )
            if row is None:
                raise RuntimeError("frozen baseline insert was not retained")
            persisted_unit = await session.get(TaskUsageWorkUnit, row.work_unit_id)
            if persisted_unit is None:
                raise RuntimeError("frozen baseline work unit was not retained")
            return _reference_from_row(row, decode_binding(persisted_unit))

    async def samples(
        self,
        *,
        project_ids: set[UUID] | None,
        as_of: datetime,
        window_days: int | None,
    ) -> tuple[BaselineSample, ...]:
        _validate_report_window(as_of, window_days)
        if project_ids is None:
            project_ids = await self.accessible_project_ids()
        if not project_ids:
            return ()
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return await self._samples(
                session,
                project_ids=project_ids,
                as_of=as_of,
                window_days=window_days,
            )

    async def _samples(
        self,
        session: AsyncSession,
        *,
        project_ids: set[UUID],
        as_of: datetime,
        window_days: int | None,
    ) -> tuple[BaselineSample, ...]:
        lower = None
        if window_days is not None:
            try:
                lower = as_of - timedelta(days=window_days)
            except OverflowError:
                # Every representable sample is in range; omit the SQL lower bound.
                lower = None
        run_filters = [Run.updated_at <= as_of]
        job_filters = [
            BrainstormJobRow.updated_at <= as_of,
        ]
        if lower is not None:
            run_filters.append(Run.updated_at >= lower)
            job_filters.append(BrainstormJobRow.updated_at >= lower)
        runs: list[Row[tuple[Any, ...]]] = []
        jobs: list[Row[tuple[Any, ...]]] = []
        for project_batch in _id_batches(project_ids):
            runs.extend((await session.execute(
                select(Run.id, Run.project_id, Run.state, Run.updated_at,
                       _has_current_acceptance(as_of)).where(
                    Run.project_id.in_(project_batch), *run_filters
                )
            )).all())
            jobs.extend((await session.execute(
                select(BrainstormJobRow.id, BrainstormJobRow.project_id,
                       BrainstormJobRow.state, BrainstormJobRow.proposal_digest,
                       BrainstormJobRow.updated_at).where(
                    BrainstormJobRow.project_id.in_(project_batch), *job_filters
                )
            )).all())
        candidates = [
            (
                project,
                "run",
                identity,
                "implementation",
                state,
                state == "COMPLETED",
                accepted,
                at,
            )
            for identity, project, state, at, accepted in runs
        ] + [
            (
                project,
                "authoring_job",
                identity,
                "authoring",
                    state,
                    state == "proposed" and proposal_digest is not None,
                    False,
                    at,
            )
            for identity, project, state, proposal_digest, at in jobs
        ]
        if not candidates:
            return ()
        candidate_keys = {(project, kind, identity) for project, kind, identity, *_ in candidates}
        units_by_subject = await _candidate_units(session, candidate_keys, as_of=as_of)
        inventory = await _source_inventory(session, candidate_keys, as_of=as_of)
        totals = await self._totals.totals_for_units_in(
            session,
            {SubjectKey(project_id=project, kind=kind, subject_id=identity): units
             for (project, kind, identity), units in units_by_subject.items()},
            as_of=as_of,
        )
        roles = await _source_roles(
            session, [unit for units in units_by_subject.values() for unit in units]
        )
        failed_counts = await _failed_run_attempt_counts(
            session, {identity for _, kind, identity, *_ in candidates if kind == "run"},
            as_of=as_of,
        )
        legacy = await _legacy_model_usage_samples(
            session,
            {identity: (project, completed_at, state, successful, accepted)
             for project, kind, identity, _, state, successful, accepted, completed_at in candidates
             if kind == "run" and (project, kind, identity) not in units_by_subject},
            as_of=as_of,
        )
        # The owning query's final projection is authoritative. Returned facts
        # are per subject and already replace live/provisional values per source.
        samples: list[BaselineSample] = []
        for project, kind, identity, task_kind, state, successful, accepted, completed_at in candidates:
            key = (project, kind, identity)
            units = units_by_subject.get(key, [])
            outcome = _outcome(state, successful, accepted)
            if not units:
                legacy_sample = legacy.get(identity) if kind == "run" else None
                if legacy_sample is not None:
                    samples.append(legacy_sample)
                    continue
                failed_attempt_count = failed_counts.get(identity, 0) if kind == "run" else 0
                samples.append(
                    BaselineSample(
                        project_id=project,
                        subject_kind=kind,
                        subject_id=identity,
                        completed_at=completed_at,
                        total_tokens=None,
                        outcome=outcome,
                        finalized=False,
                        measured=False,
                        configured_route=None,
                        effective_route=None,
                        task_kind=task_kind,
                        source_count=0,
                        failed_attempt_count=failed_attempt_count,
                    )
                )
                continue
            subject_totals = totals[SubjectKey(project_id=project, kind=kind, subject_id=identity)]
            facts = tuple(
                fact
                for fact in subject_totals.source_facts
                if fact.evidence_kind != "live"
            )
            failed_attempt_count = failed_counts.get(identity, 0) if kind == "run" else 0
            bound_sources = {(unit.source_kind, unit.source_id) for unit in units}
            inventory_complete = bound_sources == inventory.get(key, set())
            source_final = bool(subject_totals.source_count) and subject_totals.final_source_count == subject_totals.source_count
            fully_measured = (
                source_final
                and inventory_complete
                and subject_totals.completeness == "complete"
                and subject_totals.input_tokens is not None
                and subject_totals.output_tokens is not None
            )
            total_tokens = (
                subject_totals.input_tokens + subject_totals.output_tokens
                if subject_totals.input_tokens is not None
                and subject_totals.output_tokens is not None
                and fully_measured
                else None
            )
            configured = _route_composition(facts, "configured_route") if inventory_complete else None
            effective = _route_composition(facts, "effective_route") if inventory_complete else None
            samples.append(
                BaselineSample(
                    project_id=project,
                    subject_kind=kind,
                    subject_id=identity,
                    completed_at=completed_at,
                    total_tokens=total_tokens,
                    outcome=outcome,
                    finalized=source_final and inventory_complete,
                    measured=fully_measured,
                    configured_route=configured,
                    effective_route=effective,
                    task_kind=task_kind,
                    route_members=_routes(facts) if inventory_complete else (),
                    phase_role_breakdown=(
                        _phase_role_facts(facts, roles) if inventory_complete else ()
                    ),
                    source_count=max(subject_totals.source_count, len(inventory.get(key, set()))),
                    failed_attempt_count=failed_attempt_count,
                )
            )
        return tuple(samples)

    async def exclusions(
        self,
        *,
        project_ids: set[UUID] | None,
        as_of: datetime,
        window_days: int | None,
        subjects: set[tuple[UUID, str, UUID]],
    ) -> dict[str, int]:
        _validate_report_window(as_of, window_days)
        projects = project_ids if project_ids is not None else await self.accessible_project_ids()
        if not projects:
            return {}
        async with self.report_snapshot() as snapshot:
            return await snapshot.exclusions(
                project_ids=projects, as_of=as_of, window_days=window_days,
                subjects=subjects,
            )

    async def _exclusions_in(
        self, session: AsyncSession, *, projects: set[UUID], as_of: datetime,
        window_days: int | None, subjects: set[tuple[UUID, str, UUID]],
    ) -> dict[str, int]:
        lower = None
        if window_days is not None:
            try:
                lower = as_of - timedelta(days=window_days)
            except OverflowError:
                lower = None
        run_filters = [Run.updated_at <= as_of]
        job_filters = [
            BrainstormJobRow.updated_at <= as_of,
        ]
        run_ids = {identity for project, kind, identity in subjects if kind == "run" and project in projects}
        job_ids = {
            identity
            for project, kind, identity in subjects
            if kind == "authoring_job" and project in projects
        }
        if lower is not None:
            run_filters.append(Run.updated_at >= lower)
            job_filters.append(BrainstormJobRow.updated_at >= lower)
        runs: list[Run] = []
        jobs: list[BrainstormJobRow] = []
        for batch in _id_batches(run_ids):
            runs.extend((await session.scalars(
                select(Run).where(Run.id.in_(batch), *run_filters)
            )).all())
        for batch in _id_batches(job_ids):
            jobs.extend((await session.scalars(
                select(BrainstormJobRow).where(BrainstormJobRow.id.in_(batch), *job_filters)
            )).all())
        runs = [run for run in runs if run.project_id in projects
                and (run.project_id, "run", run.id) in subjects]
        jobs = [job for job in jobs if job.project_id in projects
                and (job.project_id, "authoring_job", job.id) in subjects]
        count = {
            "failed": 0,
            "cancelled": 0,
            "in_progress": 0,
            "incomplete_usage": 0,
            "truncated": 0,
            "unclassified_legacy": 0,
        }
        run_ids = {identity for project, kind, identity in subjects if kind == "run" and project in projects}
        accepted_run_ids: set[UUID] = set()
        for batch in _id_batches({run.id for run in runs}):
            accepted_run_ids.update((await session.scalars(
                select(Run.id).where(Run.id.in_(batch), _has_current_acceptance(as_of))
            )).all())
        for run in runs:
            if run.state == "FAILED":
                count["failed"] += 1
            elif run.state == "CANCELLED":
                count["cancelled"] += 1
            elif run.state != "COMPLETED" and run.id not in accepted_run_ids:
                count["in_progress"] += 1
        for job in jobs:
            if job.state == "failed":
                count["failed"] += 1
            elif job.state == "cancelled":
                count["cancelled"] += 1
            elif job.state != "proposed":
                count["in_progress"] += 1
        candidate_keys = {
            (run.project_id, "run", run.id)
            for run in runs
            if run.state == "COMPLETED" or run.id in accepted_run_ids
        } | {
            (job.project_id, "authoring_job", job.id)
            for job in jobs
            if job.state == "proposed" and job.proposal_digest is not None
        }
        candidate_keys &= subjects
        units_by_subject = await _candidate_units(session, candidate_keys, as_of=as_of)
        known = set(units_by_subject)
        run_by_id = {run.id: run for run in runs}
        inventory = await _source_inventory(session, candidate_keys, as_of=as_of)
        legacy = await _legacy_model_usage_samples(
            session,
            {identity: (project, run_by_id[identity].updated_at,
                        run_by_id[identity].state,
                        run_by_id[identity].state == "COMPLETED",
                        identity in accepted_run_ids)
             for project, kind, identity in candidate_keys
             if kind == "run" and (project, kind, identity) not in known},
            as_of=as_of,
        )
        totals = await self._totals.totals_for_units_in(
            session,
            {SubjectKey(project_id=project, kind=kind, subject_id=identity): units
             for (project, kind, identity), units in units_by_subject.items()},
            as_of=as_of,
        )
        for project, kind, subject in candidate_keys:
            if (project, kind, subject) in known:
                continue
            if kind != "run" or subject not in legacy:
                count["unclassified_legacy"] += 1
        for project, kind, subject in candidate_keys:
            if (project, kind, subject) not in known:
                continue
            subject_totals = totals[SubjectKey(project_id=project, kind=kind, subject_id=subject)]
            bound_rows = units_by_subject[(project, kind, subject)]
            bound_sources = {(unit.source_kind, unit.source_id) for unit in bound_rows}
            if (
                subject_totals.completeness != "complete"
                or bound_sources != inventory.get((project, kind, subject), set())
            ):
                count["incomplete_usage"] += 1
            if any(
                fact.final_status in {"truncated", "incomplete", "partial"}
                for fact in subject_totals.source_facts
            ):
                count["truncated"] += 1
        return {name: amount for name, amount in count.items() if amount}


class _TaskUsageReportSnapshot:
    def __init__(self, query: TaskUsageBaselineQuery, session: AsyncSession) -> None:
        self._query = query
        self._session = session

    async def accessible_project_ids(self) -> set[UUID]:
        return set((await self._session.scalars(select(Project.id))).all())

    async def samples(
        self, *, project_ids: set[UUID] | None, as_of: datetime,
        window_days: int | None,
    ) -> tuple[BaselineSample, ...]:
        _validate_report_window(as_of, window_days)
        projects = project_ids if project_ids is not None else await self.accessible_project_ids()
        if not projects:
            return ()
        return await self._query._samples(
            self._session, project_ids=projects, as_of=as_of, window_days=window_days,
        )

    async def exclusions(
        self, *, project_ids: set[UUID] | None, as_of: datetime,
        window_days: int | None, subjects: set[tuple[UUID, str, UUID]],
    ) -> dict[str, int]:
        _validate_report_window(as_of, window_days)
        projects = project_ids if project_ids is not None else await self.accessible_project_ids()
        if not projects:
            return {}
        return await self._query._exclusions_in(
            self._session, projects=projects, as_of=as_of, window_days=window_days,
            subjects=subjects,
        )


async def _legacy_model_usage_samples(
    session: AsyncSession,
    candidates: dict[UUID, tuple[UUID, datetime, str, bool, bool]],
    *, as_of: datetime,
) -> dict[UUID, BaselineSample]:
    """Classify unbound runs with bounded reads and the old exact coverage rule."""
    executions: dict[UUID, set[UUID]] = {}
    subscriptions: set[UUID] = set()
    rows_by_run: dict[UUID, list[Row[tuple[Any, ...]]]] = {}
    for batch in _id_batches(set(candidates)):
        for execution_id, run_id in (
            await session.execute(select(AgentExecution.id, AgentExecution.run_id)
                                  .where(AgentExecution.run_id.in_(batch),
                                         AgentExecution.created_at <= as_of))
        ).all():
            executions.setdefault(run_id, set()).add(execution_id)
        subscriptions.update((await session.scalars(
            select(SubscriptionAttempt.run_id).where(
                SubscriptionAttempt.run_id.in_(batch),
                SubscriptionAttempt.created_at <= as_of,
            )
        )).all())
        eligible = [identity for identity in batch
                    if executions.get(identity) and identity not in subscriptions]
        if not eligible:
            continue
        for row in (
            await session.execute(
                select(
                    ModelUsage.run_id,
                    ModelUsage.agent_execution_id,
                    ModelUsage.provider,
                    ModelUsage.model,
                    ModelUsage.input_tokens,
                    ModelUsage.cached_input_tokens,
                    ModelUsage.output_tokens,
                    AgentExecution.role,
                    AgentExecution.status,
                    Step.kind,
                )
                .join(AgentExecution, AgentExecution.id == ModelUsage.agent_execution_id)
                .outerjoin(Step, Step.id == AgentExecution.step_id)
                .where(
                    ModelUsage.run_id.in_(eligible),
                    ModelUsage.created_at <= as_of,
                    AgentExecution.created_at <= as_of,
                )
                .order_by(ModelUsage.id)
            )
        ).all():
            rows_by_run.setdefault(row.run_id, []).append(row)
    result: dict[UUID, BaselineSample] = {}
    for subject_id, (project_id, completed_at, state, successful, accepted) in candidates.items():
        rows = rows_by_run.get(subject_id, [])
        if (not executions.get(subject_id) or subject_id in subscriptions or not rows
            or {row.agent_execution_id for row in rows} != executions[subject_id]):
            continue
        result[subject_id] = _legacy_sample_from_rows(
            rows, project_id=project_id, subject_id=subject_id,
            completed_at=completed_at, state=state, successful=successful, accepted=accepted,
        )
    return result


def _legacy_sample_from_rows(
    rows: Sequence[Row[tuple[Any, ...]]], *, project_id: UUID, subject_id: UUID,
    completed_at: datetime, state: str, successful: bool, accepted: bool,
) -> BaselineSample:
    total = sum(row.input_tokens + row.cached_input_tokens + row.output_tokens for row in rows)
    if total > 2**63 - 1:
        total = -1
    members = tuple(
        sorted(
            {
                (row.role, row.provider, row.model, None)
                for row in rows
            }
        )
    )
    route = RouteCohort.from_members(
        tuple(
            RouteMember(role=role, provider=provider, model=model, ordinal=index)
            for index, (role, provider, model, _) in enumerate(members)
        )
    )
    phase_role: dict[tuple[str, str], int] = {}
    failed_attempt_count = len(
        {row.agent_execution_id for row in rows if row.status in {"FAILED", "CANCELLED"}}
    )
    for row in rows:
        key = (row.kind or "unknown", row.role)
        phase_role[key] = (
            phase_role.get(key, 0) + row.input_tokens + row.cached_input_tokens + row.output_tokens
        )
    return BaselineSample(
        project_id=project_id,
        subject_kind="run",
        subject_id=subject_id,
        completed_at=completed_at,
        total_tokens=None if total < 0 else total,
        outcome=_outcome(state, successful, accepted),
        finalized=True,
        measured=total >= 0,
        configured_route=None,
        effective_route=route.fingerprint,
        task_kind="implementation",
        route_members=members,
        phase_role_breakdown=tuple(
            (phase, role, amount) for (phase, role), amount in sorted(phase_role.items())
        ),
        source_count=len(rows),
        failed_attempt_count=failed_attempt_count,
    )


async def _failed_run_attempt_counts(
    session: AsyncSession, run_ids: set[UUID], *, as_of: datetime,
) -> dict[UUID, int]:
    counts: dict[UUID, int] = {}
    for batch in _id_batches(run_ids):
        for run_id, amount in (
            await session.execute(
                select(AgentExecution.run_id, func.count()).where(
                    AgentExecution.run_id.in_(batch),
                    AgentExecution.status.in_(("FAILED", "CANCELLED")),
                    AgentExecution.created_at <= as_of,
                ).group_by(AgentExecution.run_id)
            )
        ).all():
            counts[run_id] = int(amount)
    return counts


async def _source_inventory(
    session: AsyncSession,
    subjects: set[tuple[UUID, str, UUID]],
    *,
    as_of: datetime,
) -> dict[tuple[UUID, str, UUID], set[tuple[str, UUID]]]:
    inventory: dict[tuple[UUID, str, UUID], set[tuple[str, UUID]]] = {}
    run_projects = {subject: project for project, kind, subject in subjects if kind == "run"}
    job_projects = {subject: project for project, kind, subject in subjects if kind == "authoring_job"}
    run_ids = set(run_projects)
    job_ids = set(job_projects)
    for batch in _id_batches(run_ids):
        executions = (
            await session.execute(
                select(AgentExecution.id, AgentExecution.run_id).where(
                    AgentExecution.run_id.in_(batch), AgentExecution.created_at <= as_of
                )
            )
        ).all()
        attempts = (
            await session.execute(
                select(SubscriptionAttempt.id, SubscriptionAttempt.run_id).where(
                    SubscriptionAttempt.run_id.in_(batch), SubscriptionAttempt.created_at <= as_of
                )
            )
        ).all()
        for source_id, run_id in executions:
            inventory.setdefault((run_projects[run_id], "run", run_id), set()).add(
                ("agent_execution", source_id)
            )
        for source_id, run_id in attempts:
            inventory.setdefault((run_projects[run_id], "run", run_id), set()).add(
                ("subscription_attempt", source_id)
            )
    for batch in _id_batches(job_ids):
        attempts = (
            await session.execute(
                select(BrainstormAttemptRow.id, BrainstormAttemptRow.job_id).where(
                    BrainstormAttemptRow.job_id.in_(batch), BrainstormAttemptRow.created_at <= as_of
                )
            )
        ).all()
        for source_id, job_id in attempts:
            inventory.setdefault((job_projects[job_id], "authoring_job", job_id), set()).add(
                ("authoring_attempt", source_id)
            )
    return inventory


def _has_current_acceptance(as_of: datetime) -> Exists:
    acceptance = aliased(EvidenceSet)
    validation = aliased(EvidenceSet)
    accepted_task = aliased(SubscriptionTask)
    accepted_attempt = aliased(SubscriptionAttempt)
    return exists(
        select(1)
        .select_from(acceptance)
        .join(validation, acceptance.validation_evidence_set_id == validation.id)
        .join(
            accepted_task,
            (accepted_task.run_id == acceptance.run_id)
            & (accepted_task.id == acceptance.producer_task_id),
        )
        .join(
            accepted_attempt,
            (accepted_attempt.run_id == acceptance.run_id)
            & (accepted_attempt.task_row_id == acceptance.producer_task_id)
            & (accepted_attempt.id == acceptance.producer_attempt_id),
        )
        .where(
            acceptance.run_id == Run.id,
            acceptance.kind == "acceptance",
            acceptance.created_at <= as_of,
            validation.created_at <= as_of,
            accepted_task.created_at <= as_of,
            accepted_attempt.created_at <= as_of,
            accepted_task.parent_task_id.is_(None),
            acceptance.policy_version == Run.policy_version,
            acceptance.validation_parent_policy_version == validation.policy_version,
            acceptance.validation_parent_kind == "validation",
            acceptance.validation_parent_head_sha == validation.head_sha,
            acceptance.head_sha == validation.head_sha,
            or_(Run.candidate_commit.is_(None), acceptance.head_sha == Run.candidate_commit),
            acceptance.candidate_tree_digest.is_not(None),
            acceptance.candidate_tree_digest == validation.candidate_tree_digest,
            validation.run_id == Run.id,
            validation.kind == "validation",
            validation.policy_version == Run.policy_version,
        )
    )


def _outcome(
    state: str, successful: bool, accepted: bool
) -> Literal["accepted", "completed", "failed", "cancelled", "truncated", "unknown"]:
    normalized = state.lower()
    if normalized in {"failed", "cancelled", "truncated"}:
        return normalized  # type: ignore[return-value]
    if accepted:
        return "accepted"
    if successful:
        return "completed"
    return "unknown"


def _task_kind(subject_kind: str) -> str:
    return "authoring" if subject_kind == "authoring_job" else "implementation"


def _digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _baseline_values(
    reference: BaselineReference, frozen_payload: dict[str, object]
) -> dict[str, object]:
    return {
        "work_unit_id": reference.binding.work_unit_id,
        "project_id": reference.binding.subject.project_id,
        "subject_kind": reference.binding.subject.kind,
        "subject_id": reference.binding.subject.subject_id,
        "policy_revision": reference.policy_revision,
        "history_digest": reference.history_digest,
        "snapshot_digest": reference.snapshot_digest,
        "estimator_version": reference.estimator_version,
        "window_limit": reference.window_limit,
        "minimum_comparables": reference.minimum_comparables,
        "cohort_digest": reference.cohort_digest,
        "window_started_at": reference.window_started_at,
        "window_ended_at": reference.window_ended_at,
        "frozen_at": reference.frozen_at,
        "sample_count": reference.sample_count,
        "status": reference.status,
        "reference_tokens": reference.reference_tokens,
        "median_tokens": reference.median_tokens,
        "p90_tokens": reference.p90_tokens,
        "mean_tokens": reference.mean_tokens,
        "warning_threshold_tokens": reference.warning_threshold_tokens,
        "checkpoint_threshold_tokens": reference.checkpoint_threshold_tokens,
        "coverage_count": reference.coverage_count,
        "outcome_count": reference.outcome_count,
        "source_count": reference.source_count,
        "frozen_payload": frozen_payload,
        "schema_version": reference.schema_version,
    }


def _reference_from_row(row: TaskUsageBaseline, binding: WorkUnitBinding) -> BaselineReference:
    completeness = row.frozen_payload.get("completeness", "complete")
    return BaselineReference(
        binding=binding,
        policy_revision=row.policy_revision,
        sample_count=row.sample_count,
        status=row.status,
        history_digest=row.history_digest,
        snapshot_digest=row.snapshot_digest,
        estimator_version=row.estimator_version,
        window_limit=row.window_limit,
        minimum_comparables=row.minimum_comparables,
        cohort_digest=row.cohort_digest,
        reference_tokens=row.reference_tokens,
        median_tokens=row.median_tokens,
        p90_tokens=row.p90_tokens,
        mean_tokens=row.mean_tokens,
        coverage_count=row.coverage_count,
        outcome_count=row.outcome_count,
        source_count=row.source_count,
        window_started_at=row.window_started_at,
        window_ended_at=row.window_ended_at,
        warning_threshold_tokens=row.warning_threshold_tokens,
        checkpoint_threshold_tokens=row.checkpoint_threshold_tokens,
        frozen_at=row.frozen_at,
        completeness=str(completeness),
    )


def _routes(facts: tuple[FinalizedSourceFact, ...]) -> tuple[tuple[str, str, str, str | None], ...]:
    members: set[tuple[str, str, str, str | None]] = set()
    for fact in facts:
        binding = fact.binding
        route = binding.effective_route
        for member in route.members:
            members.add((member.role, member.provider, member.model, member.reasoning_effort))
    return tuple(
        sorted(members, key=lambda member: (*member[:3], member[3] is not None, member[3] or ""))
    )


def _route_composition(facts: tuple[FinalizedSourceFact, ...], name: str) -> str | None:
    fingerprints = {
        (
            fact.binding.configured_route
            if name == "configured_route"
            else fact.binding.effective_route
        ).fingerprint
        for fact in facts
    }
    if not fingerprints:
        return None
    if len(fingerprints) == 1:
        return next(iter(fingerprints))
    canonical = json.dumps(sorted(fingerprints), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _phase_role_facts(
    facts: tuple[FinalizedSourceFact, ...],
    roles: dict[tuple[str, UUID], str],
) -> tuple[tuple[str, str, int | None], ...]:
    output: list[tuple[str, str, int | None]] = []
    for fact in facts:
        amount = (
            fact.input_tokens + fact.output_tokens
            if fact.input_tokens is not None and fact.output_tokens is not None
            else None
        )
        binding = fact.binding
        phase = binding.admitted_phase_id
        output.append((phase, roles.get((binding.source.kind, binding.source.source_id), "unknown"), amount))
    return tuple(output)


async def _source_roles(
    session: AsyncSession, units: list[TaskUsageWorkUnit]
) -> dict[tuple[str, UUID], str]:
    execution_ids = [unit.source_id for unit in units if unit.source_kind == "agent_execution"]
    task_ids = [unit.subscription_task_id for unit in units if unit.subscription_task_id]
    roles: dict[tuple[str, UUID], str] = {}
    for batch in _id_batches(execution_ids):
        roles.update({
            ("agent_execution", identity): role for identity, role in (
            await session.execute(
                select(AgentExecution.id, AgentExecution.role).where(
                    AgentExecution.id.in_(batch)
                )
            )
            ).all()
        })
    if task_ids:
        purposes: dict[UUID, str] = {}
        for batch in _id_batches(task_ids):
            for identity, payload in (
                await session.execute(
                    select(SubscriptionTask.id, SubscriptionTask.payload).where(
                        SubscriptionTask.id.in_(batch)
                    )
                )
            ).all():
                try:
                    contract = decode_subscription_record(payload)
                except (TypeError, ValueError, KeyError):
                    continue
                if isinstance(contract, LogicalTaskContract):
                    purposes[identity] = contract.purpose.value
        for unit in units:
            if unit.source_kind == "subscription_attempt" and unit.subscription_task_id in purposes:
                roles[(unit.source_kind, unit.source_id)] = purposes[unit.subscription_task_id]
    for unit in units:
        if unit.source_kind == "authoring_attempt":
            roles[(unit.source_kind, unit.source_id)] = "authoring"
    return roles
