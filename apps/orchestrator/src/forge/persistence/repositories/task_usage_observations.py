"""Short, independent transactions for source binding and usage evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.services.task_usage_observations import project_source
from forge.domain.task_usage_contract import (
    ContextWindow,
    CounterKind,
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.persistence.models.task_usage import TaskUsageObservation, TaskUsageWorkUnit


class UsageBindingConflict(ValueError):
    """A source, subject, phase or event identity differs from its admitted fact."""


def _members(route: RouteCohort) -> list[dict[str, object]]:
    return [
        {
            "role": member.role,
            "provider": member.provider,
            "model": member.model,
            "ordinal": member.ordinal,
            "reasoning_effort": member.reasoning_effort,
        }
        for member in route.members
    ]


def _member(value: dict[str, object]) -> RouteMember:
    role, provider, model = value.get("role"), value.get("provider"), value.get("model")
    ordinal, effort = value.get("ordinal"), value.get("reasoning_effort")
    if not (
        isinstance(role, str)
        and isinstance(provider, str)
        and isinstance(model, str)
        and type(ordinal) is int
        and (effort is None or isinstance(effort, str))
    ):
        raise UsageBindingConflict("stored route member is malformed")
    return RouteMember(
        role=role, provider=provider, model=model, ordinal=ordinal, reasoning_effort=effort
    )


def decode_binding(row: TaskUsageWorkUnit) -> WorkUnitBinding:
    configured = RouteCohort(
        fingerprint=row.configured_route_digest,
        members=tuple(_member(member) for member in row.configured_route_members),
    )
    effective = RouteCohort(
        fingerprint=row.effective_route_digest,
        members=tuple(_member(member) for member in row.effective_route_members),
    )
    return WorkUnitBinding(
        subject=SubjectKey(
            project_id=row.project_id, kind=row.subject_kind, subject_id=row.subject_id
        ),
        work_unit_id=row.id,
        admitted_phase_id=row.admitted_phase_id,
        configured_route=configured,
        effective_route=effective,
        source=SourceBinding(
            kind=row.source_kind,
            source_id=row.source_id,
            parent_id=row.subscription_task_id,
        ),
    )


def decode_observation(row: TaskUsageObservation, binding: WorkUnitBinding) -> Observation:
    context = (
        None
        if all(
            value is None
            for value in (
                row.context_occupied_tokens,
                row.context_capacity_tokens,
                row.compaction_count,
            )
        )
        else ContextWindow(
            occupied_tokens=row.context_occupied_tokens,
            capacity_tokens=row.context_capacity_tokens,
            compaction_count=row.compaction_count,
        )
    )
    return Observation(
        binding=binding,
        event_id=row.event_id,
        sequence=row.sequence,
        counter_kind=CounterKind(row.counter_kind),
        counter_epoch=row.counter_epoch,
        tokens=TokenCounts(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_input_tokens=row.cached_input_tokens,
            reasoning_output_tokens=row.reasoning_output_tokens,
        ),
        context=context,
        scope=row.scope,
        unknown_dimensions=tuple(row.unknown_dimensions),
        final=row.final,
        final_status=row.final_status,
        reconciles_event_id=row.reconciles_event_id,
        observed_at=row.observed_at,
    )


class PostgresTaskUsageObservations:
    """Observation writer; no provider call or process wait occurs in its transaction."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def observe_usage(self, observation: Observation) -> None:
        """Gateway sink: replay is harmless and does not settle a source."""
        await self.append_observation(observation)

    async def bind(self, binding: WorkUnitBinding) -> None:
        if not isinstance(binding, WorkUnitBinding):
            raise TypeError("binding must be typed")
        subject, source = binding.subject, binding.source
        values = {
            "id": binding.work_unit_id,
            "project_id": subject.project_id,
            "subject_kind": subject.kind,
            "subject_id": subject.subject_id,
            "run_id": subject.subject_id if subject.kind == "run" else None,
            "authoring_job_id": subject.subject_id if subject.kind == "authoring_job" else None,
            "admitted_phase_id": binding.admitted_phase_id,
            "configured_route_digest": binding.configured_route.fingerprint,
            "configured_route_members": _members(binding.configured_route),
            "effective_route_digest": binding.effective_route.fingerprint,
            "effective_route_members": _members(binding.effective_route),
            "source_kind": source.kind,
            "source_id": source.source_id,
            "agent_execution_id": source.source_id if source.kind == "agent_execution" else None,
            "subscription_attempt_id": source.source_id
            if source.kind == "subscription_attempt"
            else None,
            "subscription_task_id": source.parent_id,
            "authoring_attempt_id": source.source_id
            if source.kind == "authoring_attempt"
            else None,
            "schema_version": binding.schema_version,
        }
        try:
            async with self._factory() as session, session.begin():
                # Database constraints are the final authority on parent lineage.
                await session.execute(
                    insert(TaskUsageWorkUnit).values(**values).on_conflict_do_nothing()
                )
                row = await session.scalar(
                    select(TaskUsageWorkUnit).where(
                        TaskUsageWorkUnit.project_id == subject.project_id,
                        TaskUsageWorkUnit.subject_kind == subject.kind,
                        TaskUsageWorkUnit.subject_id == subject.subject_id,
                        TaskUsageWorkUnit.source_kind == source.kind,
                        TaskUsageWorkUnit.source_id == source.source_id,
                    )
                )
                if row is None or decode_binding(row) != binding:
                    raise UsageBindingConflict("source binding replay conflicts")
        except IntegrityError as error:
            raise UsageBindingConflict("binding source lineage conflicts") from error

    async def append_observation(self, observation: Observation) -> bool:
        if not isinstance(observation, Observation):
            raise TypeError("observation must be typed")
        binding = observation.binding
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(TaskUsageWorkUnit)
                .where(TaskUsageWorkUnit.id == binding.work_unit_id)
                .with_for_update()
            )
            if row is None or decode_binding(row) != binding:
                raise UsageBindingConflict("observation binding was not admitted")
            existing = (
                await session.scalars(
                    select(TaskUsageObservation)
                    .where(TaskUsageObservation.work_unit_id == binding.work_unit_id)
                    .order_by(TaskUsageObservation.sequence)
                )
            ).all()
            same = next((item for item in existing if item.event_id == observation.event_id), None)
            if same is not None:
                decoded = decode_observation(same, binding)
                if observation.observed_at is None:
                    decoded = replace(decoded, observed_at=None)
                if decoded != observation:
                    raise UsageBindingConflict("observation event replay conflicts")
                return False
            if any(item.sequence == observation.sequence for item in existing):
                raise UsageBindingConflict("observation sequence conflicts")
            # Validate the complete ordered history before the immutable insert.
            project_source(
                tuple(decode_observation(item, binding) for item in existing) + (observation,)
            )
            context = observation.context
            session.add(
                TaskUsageObservation(
                    id=uuid4(),
                    project_id=binding.subject.project_id,
                    work_unit_id=binding.work_unit_id,
                    event_id=observation.event_id,
                    sequence=observation.sequence,
                    counter_kind=observation.counter_kind.value,
                    counter_epoch=observation.counter_epoch,
                    scope=observation.scope,
                    input_tokens=observation.tokens.input_tokens,
                    output_tokens=observation.tokens.output_tokens,
                    cached_input_tokens=observation.tokens.cached_input_tokens,
                    reasoning_output_tokens=observation.tokens.reasoning_output_tokens,
                    context_occupied_tokens=None if context is None else context.occupied_tokens,
                    context_capacity_tokens=None if context is None else context.capacity_tokens,
                    compaction_count=None if context is None else context.compaction_count,
                    unknown_dimensions=list(observation.unknown_dimensions),
                    final=observation.final,
                    final_status=observation.final_status,
                    reconciles_event_id=observation.reconciles_event_id,
                    observed_at=observation.observed_at or datetime.now(UTC),
                    schema_version=observation.schema_version,
                )
            )
            await session.flush()
            return True

    async def binding_for_source(
        self, project_id: UUID, source_kind: str, source_id: UUID
    ) -> WorkUnitBinding | None:
        async with self._factory() as session:
            row = await session.scalar(
                select(TaskUsageWorkUnit).where(
                    TaskUsageWorkUnit.project_id == project_id,
                    TaskUsageWorkUnit.source_kind == source_kind,
                    TaskUsageWorkUnit.source_id == source_id,
                )
            )
            return None if row is None else decode_binding(row)
