"""Authenticated owner actions on settled run checkpoints."""

from __future__ import annotations

from math import ceil
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.task_usage import (
    OwnerCommandReceipt,
    TaskUsageTotalsReader,
    UsageReport,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.task_usage_monitor import run_checkpoint_quiescent
from forge.application.services.task_usage_reports import build_usage_report
from forge.domain.task_usage_contract import (
    MAX_SIGNED_BIGINT,
    BaselineReference,
    CheckpointRecord,
    CheckpointState,
    OwnerCommand,
    SubjectKey,
)
from forge.domain.task_usage_monitor import evaluate_run_usage
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.task_usage import TaskUsageBaseline, TaskUsageWorkUnit
from forge.persistence.queries.task_usage_baselines import _reference_from_row
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.task_usage_admissions import (
    PostgresTaskUsageAdmissionRepository,
)
from forge.persistence.repositories.task_usage_checkpoints import (
    CheckpointConflict,
    PostgresTaskUsageCheckpointRepository,
)
from forge.persistence.repositories.task_usage_observations import decode_binding
from forge.persistence.repositories.task_usage_policy import PostgresTaskUsagePolicyRepository


def next_threshold(reference: BaselineReference, measured_tokens: int) -> int:
    """Advance by one frozen reference interval past the currently known total."""
    if type(measured_tokens) is not int or not 0 <= measured_tokens <= MAX_SIGNED_BIGINT:
        raise ValueError("measured usage is invalid")
    step = max(1, ceil(reference.reference_tokens or 0))
    threshold = measured_tokens + step
    if threshold > MAX_SIGNED_BIGINT:
        raise ValueError("next checkpoint threshold exceeds signed-64-bit range")
    return threshold


class RunTaskUsageControls:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        totals: TaskUsageTotalsReader,
    ) -> None:
        self._sessions, self._totals = session_factory, totals

    async def get(self, run_id: UUID) -> CheckpointRecord | None:
        async with self._sessions() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise ValueError("run is missing")
            subject = SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id)
            return await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(subject)

    async def get_with_report(
        self,
        run_id: UUID,
    ) -> tuple[CheckpointRecord | None, UsageReport | None]:
        async with self._sessions() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise ValueError("run is missing")
            subject = SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id)
            checkpoint = await PostgresTaskUsageCheckpointRepository(session).latest_for_subject(
                subject
            )
            baseline_row = await session.scalar(
                select(TaskUsageBaseline).where(
                    TaskUsageBaseline.project_id == subject.project_id,
                    TaskUsageBaseline.subject_kind == subject.kind,
                    TaskUsageBaseline.subject_id == subject.subject_id,
                )
            )
            baseline = None
            if baseline_row is not None:
                unit = await session.get(TaskUsageWorkUnit, baseline_row.work_unit_id)
                if unit is None:
                    raise RuntimeError("frozen run reference is missing")
                baseline = _reference_from_row(baseline_row, decode_binding(unit))
            totals = await self._totals.totals(subject)
            admission = await PostgresTaskUsageAdmissionRepository(session).get(subject)
            revision = (
                checkpoint.monitoring_policy_revision
                if checkpoint is not None and checkpoint.monitoring_policy_revision is not None
                else baseline.policy_revision
                if baseline is not None
                else admission.policy_revision
                if admission is not None
                else None
            )
            policy = (
                None
                if admission is not None and revision is None
                else await PostgresTaskUsagePolicyRepository(session).get(
                    subject.project_id, revision
                )
            )
            decision = (
                evaluate_run_usage(
                    baseline,
                    policy,
                    measured_tokens=totals.known_total_tokens,
                    completeness=totals.completeness,
                    next_checkpoint_threshold_tokens=(
                        checkpoint.next_checkpoint_threshold_tokens
                        if checkpoint is not None
                        else None
                    ),
                    usage_pauses_disabled=(
                        checkpoint.usage_pauses_disabled if checkpoint is not None else False
                    ),
                )
                if policy is not None
                else None
            )
            report = await build_usage_report(
                session,
                totals=totals,
                policy_revision=policy.revision if policy is not None else None,
                baseline=baseline,
                checkpoint=checkpoint,
                warning="usage exceeds the frozen warning threshold"
                if decision == "warning"
                else None,
            )
            return checkpoint, report

    async def apply(
        self,
        *,
        run_id: UUID,
        actor: AuthenticatedActor,
        command: OwnerCommand,
    ) -> tuple[OwnerCommandReceipt, CheckpointRecord]:
        if actor.actor_id != command.actor_id:
            raise CheckpointConflict("owner command actor differs from authenticated session")
        async with self._sessions() as session, session.begin():
            run = await session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                raise ValueError("run is missing")
            subject = SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id)
            repository = PostgresTaskUsageCheckpointRepository(session)
            current = await repository.get(command.checkpoint_id)
            if current is None or current.binding.subject != subject:
                raise CheckpointConflict("checkpoint does not belong to the authorized run")
            replay = await repository.receipt_for_command(command)
            if replay is not None:
                return replay, current
            latest = await repository.latest_for_subject(subject)
            if latest is None or latest.checkpoint_id != current.checkpoint_id:
                raise CheckpointConflict("checkpoint is no longer the current generation")
            if command.action == "set_policy":
                if current.state is not CheckpointState.PAUSED:
                    raise CheckpointConflict("run must settle before changing checkpoint policy")
                requested = command.requested_policy
                assert requested is not None
                if requested.project_id != subject.project_id:
                    raise CheckpointConflict("policy belongs to another project")
                await PostgresTaskUsagePolicyRepository(session).append_policy_revision(
                    requested,
                    expected_revision=requested.revision - 1,
                    actor_id=actor.actor_id,
                )
                state: CheckpointState = current.state
                next_limit = None
                bypass = None
                revision = requested.revision
            elif command.action == "continue":
                if current.state is CheckpointState.PAUSED:
                    if run.state in {"PAUSED", "CANCELLED", "FAILED", "COMPLETED"}:
                        raise CheckpointConflict("independent run control blocks continuation")
                    if not await run_checkpoint_quiescent(session, run.id):
                        raise CheckpointConflict("run process or effect is unsettled")
                state = CheckpointState.RESUMED
                bypass = command.continue_mode == "without_usage_pauses"
                if bypass:
                    next_limit = None
                else:
                    totals = await self._totals.totals(subject)
                    if totals.known_total_tokens is None:
                        raise CheckpointConflict(
                            "measured usage is unavailable for next checkpoint"
                        )
                    baseline_row = await session.get(
                        TaskUsageBaseline, current.baseline_binding.work_unit_id
                    )
                    baseline_unit = await session.get(
                        TaskUsageWorkUnit, current.baseline_binding.work_unit_id
                    )
                    if baseline_row is None or baseline_unit is None:
                        raise RuntimeError("frozen checkpoint reference is missing")
                    next_limit = next_threshold(
                        _reference_from_row(baseline_row, decode_binding(baseline_unit)),
                        totals.known_total_tokens,
                    )
                revision = None
            elif command.action == "stop":
                if current.state is not CheckpointState.PAUSED:
                    raise CheckpointConflict("run must settle before stop")
                await PostgresCommandRepository(session=session).enqueue(
                    run_id=run.id,
                    command_type="cancel",
                    idempotency_key=f"usage-stop:{command.checkpoint_id}:{command.idempotency_key}",
                    payload={},
                    expected_run_version=run.version,
                    actor_id=actor.actor_id,
                )
                scheduler = await session.get(
                    SubscriptionSchedulerRun, run.id, with_for_update=True
                )
                if scheduler is not None:
                    scheduler.admitted = False
                state, next_limit, bypass, revision = CheckpointState.RESOLVED, None, None, None
            else:
                raise CheckpointConflict("unsupported run checkpoint owner action")
            snapshot = (await self._totals.totals(subject)).snapshot_digest
            receipt = await repository.apply_owner_command(
                command,
                state=state,
                affected_snapshot=snapshot,
                next_checkpoint_threshold_tokens=next_limit,
                usage_pauses_disabled=bypass,
                monitoring_policy_revision=revision,
            )
            updated = await repository.get(current.checkpoint_id)
            assert updated is not None
            return receipt, updated
