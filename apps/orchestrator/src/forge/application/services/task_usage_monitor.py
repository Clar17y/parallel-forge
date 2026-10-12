"""Durable run checkpoint evaluation and process settlement."""

from __future__ import annotations

from uuid import UUID, uuid5

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.task_usage import TaskUsageTotalsReader
from forge.domain.task_usage_contract import (
    CheckpointRecord,
    CheckpointState,
    CounterKind,
    Observation,
    SubjectKey,
    TokenCounts,
)
from forge.domain.task_usage_monitor import evaluate_run_usage
from forge.persistence.models.execution import AgentExecution, ModelUsage, RunCommand
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.models.task_usage import (
    TaskUsageBaseline,
    TaskUsageCheckpoint,
    TaskUsageObservation,
    TaskUsageWorkUnit,
)
from forge.persistence.queries.task_usage_baselines import _reference_from_row
from forge.persistence.queries.task_usage_totals import complete_model_usage_tokens
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription_resumption import (
    _pending_decision_is_admissible,
    _source,
)
from forge.persistence.repositories.task_usage_checkpoints import (
    PostgresTaskUsageCheckpointRepository,
)
from forge.persistence.repositories.task_usage_observations import (
    PostgresTaskUsageObservations,
    UsageBindingConflict,
    decode_binding,
    decode_observation,
)
from forge.persistence.repositories.task_usage_policy import PostgresTaskUsagePolicyRepository

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
_ACTIVE = {CheckpointState.REQUESTED, CheckpointState.PAUSING, CheckpointState.PAUSED}


def _measured_token(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


async def run_checkpoint_quiescent(session: AsyncSession, run_id: UUID) -> bool:
    """Fenced pending commands can wait; leased work and effects cannot."""
    runs = PostgresRunRepository(session)
    proof = await runs.prove_quiescent(run_id)
    run = await runs.get(run_id)
    # A settled provider decision intentionally keeps its task RECONCILING
    # until the typed application resumes. Reuse the existing full source,
    # launch, consumption and decision proof before exempting these two rows.
    candidates = (
        await session.scalars(
            select(SubscriptionAttempt.id)
            .join(
                SubscriptionAttemptResult,
                SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
            )
            .where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.status == "reconciling",
                SubscriptionAttemptResult.disposition == "decision_pending",
            )
        )
    ).all()
    safe_attempts: set[UUID] = set()
    safe_tasks: set[UUID] = set()
    for attempt_id in candidates:
        try:
            source = await _source(
                session, run, attempt_id, historical=False, pending_decision=True
            )
            if _pending_decision_is_admissible(source, run, phase=run.state):
                safe_attempts.add(source.attempt.id)
                safe_tasks.add(source.task.id)
        except CommandRecoveryRequired, KeyError, TypeError, ValueError:
            continue
    leased = await session.scalar(
        select(func.count())
        .select_from(RunCommand)
        .where(RunCommand.run_id == run_id, RunCommand.status == "LEASED")
    )
    return not any(
        (
            leased,
            proof.running_steps,
            proof.running_executions,
            proof.running_tools,
            proof.unresolved_operations,
            proof.unsettled_subscription_work - len(safe_attempts) - len(safe_tasks),
        )
    )


class RunTaskUsageMonitor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        totals: TaskUsageTotalsReader,
    ) -> None:
        self._sessions, self._totals = session_factory, totals
        self._final_cursor: UUID | None = None

    async def is_active(self, subject: SubjectKey) -> bool:
        if subject.kind != "run":
            raise ValueError("run checkpoint subject is required")
        async with self._sessions() as session:
            return (
                await PostgresTaskUsageCheckpointRepository(session).active_for_subject(subject)
                is not None
            )

    async def reconcile_final_sources(self, *, limit: int = 100) -> tuple[CheckpointRecord, ...]:
        """Materialize finalized provider usage so restarts detect late crossings."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("final source sweep limit must be 1..1000")
        async with self._sessions() as session:
            units = (
                await session.scalars(
                    select(TaskUsageWorkUnit)
                    .join(
                        AgentExecution,
                        AgentExecution.id == TaskUsageWorkUnit.agent_execution_id,
                    )
                    .where(
                        TaskUsageWorkUnit.subject_kind == "run",
                        TaskUsageWorkUnit.source_kind == "agent_execution",
                        AgentExecution.status.in_(("SUCCEEDED", "FAILED", "CANCELLED")),
                        exists(
                            select(ModelUsage.id).where(
                                ModelUsage.run_id == TaskUsageWorkUnit.subject_id,
                                ModelUsage.agent_execution_id == TaskUsageWorkUnit.source_id,
                            )
                        ),
                        ~exists(
                            select(TaskUsageObservation.id).where(
                                TaskUsageObservation.work_unit_id == TaskUsageWorkUnit.id,
                                TaskUsageObservation.final.is_(True),
                            )
                        ),
                    )
                    .order_by(TaskUsageWorkUnit.created_at, TaskUsageWorkUnit.id)
                    .limit(limit)
                )
            ).all()
            child_units = (
                await session.scalars(
                    select(TaskUsageWorkUnit)
                    .where(
                        TaskUsageWorkUnit.subject_kind == "run",
                        TaskUsageWorkUnit.source_kind == "subscription_attempt",
                        exists(
                            select(SubscriptionAttemptConsumption.attempt_id).where(
                                SubscriptionAttemptConsumption.attempt_id
                                == TaskUsageWorkUnit.source_id
                            )
                        ),
                        ~exists(
                            select(TaskUsageObservation.id).where(
                                TaskUsageObservation.work_unit_id == TaskUsageWorkUnit.id,
                                TaskUsageObservation.final.is_(True),
                            )
                        ),
                    )
                    .order_by(TaskUsageWorkUnit.created_at, TaskUsageWorkUnit.id)
                    .limit(max(0, limit - len(units)))
                )
            ).all()
        results: list[CheckpointRecord] = []
        observations = PostgresTaskUsageObservations(self._sessions)
        for unit in units:
            async with self._sessions() as session:
                rows = (
                    await session.scalars(
                        select(ModelUsage)
                        .where(
                            ModelUsage.run_id == unit.subject_id,
                            ModelUsage.agent_execution_id == unit.source_id,
                        )
                        .order_by(ModelUsage.id)
                    )
                ).all()
                agent = await session.get(AgentExecution, unit.source_id)
                previous = await session.scalar(
                    select(TaskUsageObservation)
                    .where(TaskUsageObservation.work_unit_id == unit.id)
                    .order_by(TaskUsageObservation.sequence.desc())
                    .limit(1)
                )
            if (
                not rows
                or agent is None
                or agent.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}
            ):
                continue
            binding = decode_binding(unit)
            context = (
                decode_observation(previous, binding).context if previous is not None else None
            )
            observation = Observation(
                binding=binding,
                event_id="model-usage-final",
                sequence=1 if previous is None else previous.sequence + 1,
                counter_kind=CounterKind.CUMULATIVE,
                tokens=complete_model_usage_tokens(list(rows)),
                context=context,
                final=True,
                final_status=agent.status.lower(),
            )
            checkpoint = await self._accept_final_observation(observations, observation)
            if checkpoint is not None:
                results.append(checkpoint)
        for unit in child_units:
            async with self._sessions() as session:
                consumption = await session.get(SubscriptionAttemptConsumption, unit.source_id)
                previous = await session.scalar(
                    select(TaskUsageObservation)
                    .where(TaskUsageObservation.work_unit_id == unit.id)
                    .order_by(TaskUsageObservation.sequence.desc())
                    .limit(1)
                )
            if consumption is None:
                continue
            binding = decode_binding(unit)
            context = (
                decode_observation(previous, binding).context if previous is not None else None
            )
            measured = consumption.observed
            observation = Observation(
                binding=binding,
                event_id="subscription-consumption-final",
                sequence=1 if previous is None else previous.sequence + 1,
                counter_kind=CounterKind.CUMULATIVE,
                tokens=TokenCounts(
                    input_tokens=_measured_token(measured.get("input_tokens")),
                    output_tokens=_measured_token(measured.get("output_tokens")),
                    cached_input_tokens=_measured_token(measured.get("cached_input_tokens")),
                ),
                context=context,
                final=True,
                final_status="settled",
            )
            checkpoint = await self._accept_final_observation(observations, observation)
            if checkpoint is not None:
                results.append(checkpoint)
        # A crash can occur after an observation commits but before evaluate.
        # Rotate through committed finals, including finals produced by gateways.
        async with self._sessions() as session:
            query = (
                select(TaskUsageObservation, TaskUsageWorkUnit)
                .join(
                    TaskUsageWorkUnit,
                    TaskUsageWorkUnit.id == TaskUsageObservation.work_unit_id,
                )
                .where(
                    TaskUsageWorkUnit.subject_kind == "run",
                    TaskUsageObservation.final.is_(True),
                )
                .order_by(TaskUsageWorkUnit.id, TaskUsageObservation.sequence.desc())
                .limit(limit)
            )
            if self._final_cursor is not None:
                query = query.where(TaskUsageWorkUnit.id > self._final_cursor)
            finals = (await session.execute(query)).all()
        if not finals:
            self._final_cursor = None
        else:
            self._final_cursor = finals[-1][1].id
            seen = {checkpoint.checkpoint_id for checkpoint in results}
            for final, unit in finals:
                checkpoint = await self.observe(decode_observation(final, decode_binding(unit)))
                if checkpoint is not None and checkpoint.checkpoint_id not in seen:
                    results.append(checkpoint)
                    seen.add(checkpoint.checkpoint_id)
        return tuple(results)

    async def _accept_final_observation(
        self, observations: PostgresTaskUsageObservations, observation: Observation
    ) -> CheckpointRecord | None:
        try:
            await observations.observe_usage(observation)
        except UsageBindingConflict:
            # Another worker may have finalized this source after our read.
            # A genuinely conflicting live event is retried on the next sweep.
            async with self._sessions() as session:
                final = await session.scalar(
                    select(TaskUsageObservation)
                    .where(
                        TaskUsageObservation.work_unit_id == observation.binding.work_unit_id,
                        TaskUsageObservation.final.is_(True),
                    )
                    .limit(1)
                )
            if final is None:
                return None
            observation = decode_observation(final, observation.binding)
        return await self.observe(observation)

    async def observe(self, observation: Observation) -> CheckpointRecord | None:
        """Evaluate a persisted event; the run row serializes request and admission."""
        subject = observation.binding.subject
        if subject.kind != "run":
            raise ValueError("run monitor requires a run observation")
        totals = await self._totals.totals(subject)
        async with self._sessions() as session, session.begin():
            run = await session.scalar(
                select(Run)
                .where(Run.id == subject.subject_id, Run.project_id == subject.project_id)
                .with_for_update()
            )
            if run is None:
                raise ValueError("run checkpoint subject is missing")
            checkpoints = PostgresTaskUsageCheckpointRepository(session)
            active = await checkpoints.active_for_subject(subject)
            if active is not None:
                return active
            baseline_row = await session.scalar(
                select(TaskUsageBaseline).where(
                    TaskUsageBaseline.project_id == subject.project_id,
                    TaskUsageBaseline.subject_kind == "run",
                    TaskUsageBaseline.subject_id == subject.subject_id,
                )
            )
            if baseline_row is None:
                return None
            baseline_unit = await session.get(TaskUsageWorkUnit, baseline_row.work_unit_id)
            if baseline_unit is None:
                raise RuntimeError("frozen run reference lost its source")
            baseline = _reference_from_row(baseline_row, decode_binding(baseline_unit))
            latest = await checkpoints.latest_for_subject(subject)
            if latest is not None and latest.state is CheckpointState.RESOLVED:
                # A terminal or owner-stopped subject has no new execution
                # authority and cannot start another checkpoint generation.
                return None
            revision = (
                latest.monitoring_policy_revision
                if latest is not None and latest.monitoring_policy_revision is not None
                else baseline.policy_revision
            )
            policy = await PostgresTaskUsagePolicyRepository(session).get(
                subject.project_id, revision
            )
            if policy is None:
                return None
            choice = evaluate_run_usage(
                baseline,
                policy,
                measured_tokens=totals.known_total_tokens,
                completeness=totals.completeness,
                next_checkpoint_threshold_tokens=(
                    latest.next_checkpoint_threshold_tokens if latest is not None else None
                ),
                usage_pauses_disabled=(
                    latest.usage_pauses_disabled if latest is not None else False
                ),
            )
            if choice != "checkpoint":
                return None
            if observation.binding.subject != baseline.binding.subject:
                raise ValueError("trigger belongs to a different run")
            # The event identity is immutable. A replay reaches the same request.
            identity = uuid5(observation.binding.work_unit_id, observation.event_id)
            requested = await checkpoints.request(
                CheckpointRecord(
                    checkpoint_id=identity,
                    binding=observation.binding,
                    baseline_binding=baseline.binding,
                    state=CheckpointState.REQUESTED,
                    policy_revision=baseline.policy_revision,
                    reference_digest=baseline.snapshot_digest,
                    trigger_event_id=observation.event_id,
                )
            )
            if run.state in _TERMINAL:
                return await checkpoints.transition(
                    requested.checkpoint_id,
                    expected_version=requested.version,
                    state=CheckpointState.RESOLVED,
                    process_settled=await run_checkpoint_quiescent(session, subject.subject_id),
                )
            return requested

    async def advance(self, checkpoint_id: UUID) -> CheckpointRecord:
        """Progress a fenced checkpoint only after durable work has settled."""
        async with self._sessions() as session, session.begin():
            checkpoints = PostgresTaskUsageCheckpointRepository(session)
            current = await checkpoints.get(checkpoint_id)
            if current is None or current.binding.subject.kind != "run":
                raise ValueError("run checkpoint is missing")
            subject = current.binding.subject
            run = await session.scalar(
                select(Run).where(Run.id == subject.subject_id).with_for_update()
            )
            if run is None:
                raise ValueError("run checkpoint subject is missing")
            current = await checkpoints.get(checkpoint_id)
            assert current is not None
            if run.state in _TERMINAL and current.state in {
                CheckpointState.REQUESTED,
                CheckpointState.PAUSING,
            }:
                return await checkpoints.transition(
                    checkpoint_id,
                    expected_version=current.version,
                    state=CheckpointState.RESOLVED,
                    process_settled=await run_checkpoint_quiescent(session, subject.subject_id),
                )
            if current.state is CheckpointState.REQUESTED:
                current = await checkpoints.transition(
                    checkpoint_id,
                    expected_version=current.version,
                    state=CheckpointState.PAUSING,
                    process_settled=False,
                )
            if current.state is not CheckpointState.PAUSING:
                return current
            if not await run_checkpoint_quiescent(session, subject.subject_id):
                return current
            return await checkpoints.transition(
                checkpoint_id,
                expected_version=current.version,
                state=CheckpointState.PAUSED,
                process_settled=True,
            )

    async def advance_subject(self, subject: SubjectKey) -> CheckpointRecord | None:
        if subject.kind != "run":
            raise ValueError("run checkpoint subject is required")
        async with self._sessions() as session:
            current = await PostgresTaskUsageCheckpointRepository(session).active_for_subject(
                subject
            )
        return None if current is None else await self.advance(current.checkpoint_id)

    async def reconcile(self, *, limit: int = 100) -> tuple[CheckpointRecord, ...]:
        """Restart-safe bounded sweep; each item settles in its own short transaction."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("checkpoint sweep limit must be 1..1000")
        async with self._sessions() as session:
            ids = tuple(
                (
                    await session.scalars(
                        select(TaskUsageCheckpoint.id)
                        .where(
                            TaskUsageCheckpoint.subject_kind == "run",
                            TaskUsageCheckpoint.state.in_(
                                (
                                    CheckpointState.REQUESTED.value,
                                    CheckpointState.PAUSING.value,
                                )
                            ),
                        )
                        .order_by(TaskUsageCheckpoint.created_at, TaskUsageCheckpoint.id)
                        .limit(limit)
                    )
                ).all()
            )
        return tuple([await self.advance(checkpoint_id) for checkpoint_id in ids])
