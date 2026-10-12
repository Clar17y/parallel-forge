"""Production PostgreSQL monitoring services shared by API and worker factories."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.task_usage_baselines import TaskUsageBaselineService
from forge.domain.agent import AgentRequest
from forge.domain.policy import ProjectPolicy
from forge.domain.subscription import ExecutionEnvelope
from forge.domain.task_usage_contract import (
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    WorkUnitBinding,
)
from forge.persistence.queries.task_usage_baselines import TaskUsageBaselineQuery
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.task_usage_observations import PostgresTaskUsageObservations


@dataclass(frozen=True, slots=True)
class TaskUsageComposition:
    observations: PostgresTaskUsageObservations
    totals: TaskUsageTotalsQuery
    baselines: TaskUsageBaselineService

    @property
    def admitted_observations(self) -> AdmissionFreezingObservationStore:
        return AdmissionFreezingObservationStore(self.observations, self.baselines)


class AdmissionFreezingObservationStore:
    """Bind a real source and materialize its subject's admitted reference before launch."""

    def __init__(
        self, observations: PostgresTaskUsageObservations, baselines: TaskUsageBaselineService
    ) -> None:
        self._observations, self._baselines = observations, baselines

    async def bind(self, binding: WorkUnitBinding) -> None:
        await self._observations.bind(binding)
        await self._baselines.freeze_baseline(binding)

    async def append_observation(self, observation: Observation) -> bool:
        return await self._observations.append_observation(observation)

    async def binding_for_source(
        self, project_id: UUID, source_kind: str, source_id: UUID
    ) -> WorkUnitBinding | None:
        return await self._observations.binding_for_source(project_id, source_kind, source_id)


def compose_task_usage(
    session_factory: async_sessionmaker[AsyncSession],
) -> TaskUsageComposition:
    """Readers and short observation writes share one durable database factory."""

    return TaskUsageComposition(
        observations=PostgresTaskUsageObservations(session_factory),
        totals=TaskUsageTotalsQuery(session_factory),
        baselines=TaskUsageBaselineService(TaskUsageBaselineQuery(session_factory)),
    )


def configured_run_cohort(envelope: ExecutionEnvelope) -> RouteCohort:
    """Freeze the complete admitted primary and delegated route mixture."""

    if not isinstance(envelope, ExecutionEnvelope):
        raise TypeError("frozen run execution envelope is required")
    members = tuple(
        RouteMember(
            role=purpose.value,
            provider=binding.requested.provider,
            model=binding.requested.model,
            reasoning_effort=binding.requested.effort.value,
            ordinal=ordinal,
        )
        for ordinal, (purpose, binding) in enumerate(
            sorted(envelope.routes, key=lambda item: item[0].value)
        )
    )
    if not members:
        raise ValueError("run envelope has no admitted routes")
    return RouteCohort.from_members(members)


def configured_project_cohort(policy: ProjectPolicy) -> RouteCohort:
    """Freeze the full configured ADK planning/development/review route."""

    if not isinstance(policy, ProjectPolicy):
        raise TypeError("frozen project policy is required")
    models = (
        ("planner", policy.planner_model),
        ("developer", policy.developer_model),
        ("reviewer", policy.reviewer_model),
    )
    return RouteCohort.from_members(
        tuple(
            RouteMember(
                role=role,
                provider=model.provider,
                model=model.model,
                reasoning_effort=model.reasoning_effort,
                ordinal=ordinal,
            )
            for ordinal, (role, model) in enumerate(models)
        )
    )


async def admitted_run_cohort(
    work: UnitOfWork, *, project_id: UUID, run_id: UUID, policy: ProjectPolicy
) -> RouteCohort:
    """Use the immutable subject snapshot, with frozen legacy inputs as fallback."""

    admission = await work.task_usage_admissions.get(
        SubjectKey(project_id=project_id, kind="run", subject_id=run_id)
    )
    if admission is not None:
        return admission.configured_route
    envelope = await work.subscription.envelope_for_run(run_id)
    return (
        configured_run_cohort(envelope)
        if envelope is not None
        else configured_project_cohort(policy)
    )


async def bind_agent_execution_usage(
    observations: AdmissionFreezingObservationStore,
    *,
    request: AgentRequest,
    project_id: UUID,
    configured_route: RouteCohort,
    admitted_phase_id: str,
) -> WorkUnitBinding:
    """Bind the validated root execution before launching its provider."""

    effective_route = RouteCohort.from_members(
        (
            RouteMember(
                role=request.role.value,
                provider=request.provider,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
                ordinal=0,
            ),
        )
    )
    prior = await observations.binding_for_source(
        project_id, "agent_execution", request.execution_id
    )
    binding = prior or WorkUnitBinding(
        subject=SubjectKey(project_id=project_id, kind="run", subject_id=request.run_id),
        work_unit_id=uuid4(),
        admitted_phase_id=admitted_phase_id,
        configured_route=configured_route,
        effective_route=effective_route,
        source=SourceBinding(kind="agent_execution", source_id=request.execution_id),
    )
    if (
        binding.subject
        != SubjectKey(project_id=project_id, kind="run", subject_id=request.run_id)
        or binding.configured_route != configured_route
        or binding.effective_route != effective_route
        or binding.admitted_phase_id != admitted_phase_id
    ):
        raise ValueError("stored root usage binding differs from admitted execution")
    await observations.bind(binding)
    return binding
