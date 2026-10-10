"""Authorized historical source facts and immutable baseline projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
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
    MonitoringMode,
    MonitoringPolicy,
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


class TaskUsageBaselineQuery:
    """Read reports through authenticated-operator project scope and final facts."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._totals = TaskUsageTotalsQuery(factory)

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
            policy = MonitoringPolicy(
                project_id=binding.subject.project_id,
                revision=policy_row.revision,
                mode=MonitoringMode(policy_row.mode),
                warning_multiplier=policy_row.warning_multiplier,
                checkpoint_multiplier=policy_row.checkpoint_multiplier,
                history_limit=policy_row.history_limit,
                minimum_comparables=policy_row.minimum_comparables,
                hard_token_cap=policy_row.hard_token_cap,
                hard_context_cap=policy_row.hard_context_cap,
                manual_estimate_tokens=policy_row.manual_estimate_tokens,
                allow_cross_project_fallback=policy_row.allow_cross_project_fallback,
                window_days=policy_row.window_days,
            )
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
        if as_of.tzinfo is None or (
            window_days is not None and (type(window_days) is not int or window_days < 1)
        ):
            raise ValueError("report time window is invalid")
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
        runs = (
            await session.execute(
                select(
                    Run.id,
                    Run.project_id,
                    Run.state,
                    Run.updated_at,
                    _has_current_acceptance(),
                ).where(
                    Run.project_id.in_(project_ids), Run.updated_at <= as_of
                )
            )
        ).all()
        jobs = (
            await session.execute(
                select(
                    BrainstormJobRow.id,
                    BrainstormJobRow.project_id,
                    BrainstormJobRow.state,
                    BrainstormJobRow.proposal_digest,
                    BrainstormJobRow.updated_at,
                ).where(
                    BrainstormJobRow.project_id.in_(project_ids),
                    BrainstormJobRow.updated_at <= as_of,
                )
            )
        ).all()
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
        if window_days is not None:
            try:
                lower = as_of - timedelta(days=window_days)
            except OverflowError:
                lower = datetime.min.replace(tzinfo=as_of.tzinfo)
            candidates = [candidate for candidate in candidates if candidate[-1] >= lower]
        subjects = {
            SubjectKey(project_id=project, kind=kind, subject_id=identity)
            for project, kind, identity, *_ in candidates
        }
        unit_rows = (
            await session.scalars(
                select(TaskUsageWorkUnit)
                .where(
                    TaskUsageWorkUnit.project_id.in_(project_ids),
                    TaskUsageWorkUnit.created_at <= as_of,
                )
                .order_by(TaskUsageWorkUnit.created_at, TaskUsageWorkUnit.id)
            )
        ).all()
        units_by_subject: dict[tuple[UUID, str, UUID], list[TaskUsageWorkUnit]] = {}
        for unit in unit_rows:
            key = (unit.project_id, unit.subject_kind, unit.subject_id)
            if SubjectKey(project_id=key[0], kind=key[1], subject_id=key[2]) in subjects:
                units_by_subject.setdefault(key, []).append(unit)
        candidate_keys = {(project, kind, identity) for project, kind, identity, *_ in candidates}
        inventory = await _source_inventory(session, candidate_keys, as_of=as_of)
        # The owning query's final projection is authoritative. Returned facts
        # are per subject and already replace live/provisional values per source.
        samples: list[BaselineSample] = []
        for project, kind, identity, task_kind, state, successful, accepted, completed_at in candidates:
            key = (project, kind, identity)
            units = units_by_subject.get(key, [])
            outcome = _outcome(state, successful, accepted)
            if not units:
                legacy = (
                    await _legacy_model_usage_sample(
                        session,
                        project_id=project,
                        subject_id=identity,
                        completed_at=completed_at,
                        as_of=as_of,
                        state=state,
                        successful=successful,
                        accepted=accepted,
                    )
                    if kind == "run"
                    else None
                )
                if legacy is not None:
                    samples.append(legacy)
                    continue
                failed_attempt_count = await _failed_run_attempt_count(
                    session, identity, as_of=as_of
                )
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
            totals = await self._totals.totals_in(
                session, SubjectKey(project_id=project, kind=kind, subject_id=identity), as_of=as_of
            )
            facts = tuple(
                fact
                for fact in totals.source_facts
                if fact.evidence_kind != "live"
            )
            roles = await _source_roles(session, units)
            failed_attempt_count = await _failed_run_attempt_count(
                session, identity, as_of=as_of
            )
            bound_sources = {(unit.source_kind, unit.source_id) for unit in units}
            inventory_complete = bound_sources == inventory.get(key, set())
            source_final = bool(totals.source_count) and totals.final_source_count == totals.source_count
            fully_measured = (
                source_final
                and inventory_complete
                and totals.completeness == "complete"
                and totals.input_tokens is not None
                and totals.output_tokens is not None
            )
            total_tokens = (
                totals.input_tokens + totals.output_tokens
                if totals.input_tokens is not None
                and totals.output_tokens is not None
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
                    source_count=max(totals.source_count, len(inventory.get(key, set()))),
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
        if as_of.tzinfo is None or (
            window_days is not None and (type(window_days) is not int or window_days < 1)
        ):
            raise ValueError("report time window is invalid")
        projects = project_ids if project_ids is not None else await self.accessible_project_ids()
        if not projects:
            return {}
        lower = None
        if window_days is not None:
            try:
                lower = as_of - timedelta(days=window_days)
            except OverflowError:
                lower = datetime.min.replace(tzinfo=as_of.tzinfo)
        async with self._factory() as session:
            run_filters = [Run.project_id.in_(projects), Run.updated_at <= as_of]
            job_filters = [
                BrainstormJobRow.project_id.in_(projects),
                BrainstormJobRow.updated_at <= as_of,
            ]
            run_ids = {identity for project, kind, identity in subjects if kind == "run" and project in projects}
            job_ids = {
                identity
                for project, kind, identity in subjects
                if kind == "authoring_job" and project in projects
            }
            run_filters.append(Run.id.in_(run_ids))
            job_filters.append(BrainstormJobRow.id.in_(job_ids))
            if lower is not None:
                run_filters.append(Run.updated_at >= lower)
                job_filters.append(BrainstormJobRow.updated_at >= lower)
            runs = (
                await session.scalars(
                    select(Run).where(*run_filters)
                )
            ).all()
            jobs = (
                await session.scalars(
                    select(BrainstormJobRow).where(*job_filters)
                )
            ).all()
            count = {
                "failed": 0,
                "cancelled": 0,
                "in_progress": 0,
                "incomplete_usage": 0,
                "truncated": 0,
                "unclassified_legacy": 0,
            }
            run_ids = {identity for project, kind, identity in subjects if kind == "run" and project in projects}
            accepted_run_ids = set(
                (
                    await session.scalars(
                        select(Run.id).where(
                            Run.id.in_(run_ids),
                            _has_current_acceptance(),
                        )
                    )
                ).all()
            ) if run_ids else set()
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
            known = set(
                (
                    await session.execute(
                        select(
                            TaskUsageWorkUnit.project_id,
                            TaskUsageWorkUnit.subject_kind,
                            TaskUsageWorkUnit.subject_id,
                        ).where(
                            TaskUsageWorkUnit.project_id.in_(projects),
                            TaskUsageWorkUnit.created_at <= as_of,
                        )
                    )
                ).all()
            )
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
            run_by_id = {run.id: run for run in runs}
            inventory = await _source_inventory(session, candidate_keys, as_of=as_of)
            for project, kind, subject in candidate_keys:
                if (project, kind, subject) in known:
                    continue
                candidate_run = run_by_id.get(subject) if kind == "run" else None
                legacy_sample = (
                    await _legacy_model_usage_sample(
                        session,
                        project_id=project,
                        subject_id=subject,
                        completed_at=candidate_run.updated_at,
                        as_of=as_of,
                        state=candidate_run.state,
                        successful=candidate_run.state == "COMPLETED",
                        accepted=candidate_run.id in accepted_run_ids,
                    )
                    if candidate_run is not None
                    else None
                )
                if legacy_sample is None:
                    count["unclassified_legacy"] += 1
            for project, kind, subject in candidate_keys:
                if (project, kind, subject) not in known:
                    continue
                totals = await self._totals.totals_in(
                    session, SubjectKey(project_id=project, kind=kind, subject_id=subject), as_of=as_of
                )
                bound_rows = (
                    await session.scalars(
                        select(TaskUsageWorkUnit).where(
                            TaskUsageWorkUnit.project_id == project,
                            TaskUsageWorkUnit.subject_kind == kind,
                            TaskUsageWorkUnit.subject_id == subject,
                        )
                    )
                ).all()
                bound_sources = {(unit.source_kind, unit.source_id) for unit in bound_rows}
                if (
                    totals.completeness != "complete"
                    or bound_sources != inventory.get((project, kind, subject), set())
                ):
                    count["incomplete_usage"] += 1
                if any(
                    fact.final_status in {"truncated", "incomplete", "partial"}
                    for fact in totals.source_facts
                ):
                    count["truncated"] += 1
            return {name: amount for name, amount in count.items() if amount}


async def _legacy_model_usage_sample(
    session: AsyncSession,
    *,
    project_id: UUID,
    subject_id: UUID,
    completed_at: datetime,
    as_of: datetime,
    state: str,
    successful: bool,
    accepted: bool,
) -> BaselineSample | None:
    """Use legacy rows only when every provider execution has durable usage."""
    executions = set(
        (
            await session.scalars(
                select(AgentExecution.id).where(AgentExecution.run_id == subject_id)
            )
        ).all()
    )
    if not executions:
        return None
    if await session.scalar(
        select(SubscriptionAttempt.id)
        .where(SubscriptionAttempt.run_id == subject_id)
        .limit(1)
    ) is not None:
        # Without the new source bindings we cannot prove that historical
        # subscription consumption is complete or route-comparable.
        return None
    rows = (
        await session.execute(
            select(
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
            .where(ModelUsage.run_id == subject_id, ModelUsage.created_at <= as_of)
            .order_by(ModelUsage.id)
        )
    ).all()
    if not rows or {row.agent_execution_id for row in rows} != executions:
        return None
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
    if not members:
        return None
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


async def _failed_run_attempt_count(
    session: AsyncSession, run_id: UUID, *, as_of: datetime
) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(AgentExecution).where(
                AgentExecution.run_id == run_id,
                AgentExecution.status.in_(("FAILED", "CANCELLED")),
                AgentExecution.created_at <= as_of,
            )
        )
        or 0
    )


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
    if run_ids:
        executions = (
            await session.execute(
                select(AgentExecution.id, AgentExecution.run_id).where(
                    AgentExecution.run_id.in_(run_ids), AgentExecution.created_at <= as_of
                )
            )
        ).all()
        attempts = (
            await session.execute(
                select(SubscriptionAttempt.id, SubscriptionAttempt.run_id).where(
                    SubscriptionAttempt.run_id.in_(run_ids), SubscriptionAttempt.created_at <= as_of
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
    if job_ids:
        attempts = (
            await session.execute(
                select(BrainstormAttemptRow.id, BrainstormAttemptRow.job_id).where(
                    BrainstormAttemptRow.job_id.in_(job_ids), BrainstormAttemptRow.created_at <= as_of
                )
            )
        ).all()
        for source_id, job_id in attempts:
            inventory.setdefault((job_projects[job_id], "authoring_job", job_id), set()).add(
                ("authoring_attempt", source_id)
            )
    return inventory


def _has_current_acceptance() -> Exists:
    acceptance = aliased(EvidenceSet)
    validation = aliased(EvidenceSet)
    accepted_task = aliased(SubscriptionTask)
    return exists(
        select(1)
        .select_from(acceptance)
        .join(validation, acceptance.validation_evidence_set_id == validation.id)
        .join(
            accepted_task,
            (accepted_task.run_id == acceptance.run_id)
            & (accepted_task.task_id == acceptance.producer_task_id),
        )
        .where(
            acceptance.run_id == Run.id,
            acceptance.kind == "acceptance",
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
    roles: dict[UUID, str],
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
        output.append((phase, roles.get(binding.source.source_id, "unknown"), amount))
    return tuple(output)


async def _source_roles(
    session: AsyncSession, units: list[TaskUsageWorkUnit]
) -> dict[UUID, str]:
    execution_ids = [unit.source_id for unit in units if unit.source_kind == "agent_execution"]
    task_ids = [unit.subscription_task_id for unit in units if unit.subscription_task_id]
    roles = {
        identity: role
        for identity, role in (
            await session.execute(
                select(AgentExecution.id, AgentExecution.role).where(
                    AgentExecution.id.in_(execution_ids)
                )
            )
        ).all()
    } if execution_ids else {}
    if task_ids:
        purposes: dict[UUID, str] = {}
        for identity, payload in (
            await session.execute(
                select(SubscriptionTask.id, SubscriptionTask.payload).where(
                    SubscriptionTask.id.in_(task_ids)
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
                roles[unit.source_id] = purposes[unit.subscription_task_id]
    for unit in units:
        if unit.source_kind == "authoring_attempt":
            roles[unit.source_id] = "authoring"
    return roles
