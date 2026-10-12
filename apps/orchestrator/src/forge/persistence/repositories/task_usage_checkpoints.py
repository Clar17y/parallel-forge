"""Transactional checkpoint identity, settlement and owner-command fencing."""

from __future__ import annotations

from dataclasses import asdict, replace
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.task_usage import OwnerCommandReceipt
from forge.domain.task_usage_contract import (
    CheckpointRecord,
    CheckpointState,
    OwnerCommand,
    SubjectKey,
    _digest,
    _positive,
)
from forge.persistence.models.epic_brainstorm import BrainstormJobRow
from forge.persistence.models.run import Run
from forge.persistence.models.task_usage import (
    TaskUsageCheckpoint,
    TaskUsageOwnerCommand,
    TaskUsageWorkUnit,
)
from forge.persistence.repositories.task_usage_observations import decode_binding


class CheckpointConflict(ValueError):
    """Checkpoint identity, active generation or version differs from durable state."""


_ACTIVE = (CheckpointState.REQUESTED.value, CheckpointState.PAUSING.value, CheckpointState.PAUSED.value)
_EDGES = {
    CheckpointState.REQUESTED: {CheckpointState.PAUSING, CheckpointState.RESOLVED},
    CheckpointState.PAUSING: {CheckpointState.PAUSED, CheckpointState.RESOLVED},
    CheckpointState.PAUSED: {CheckpointState.RESUMED, CheckpointState.RESOLVED},
    CheckpointState.RESUMED: {CheckpointState.RESOLVED},
    CheckpointState.RESOLVED: set(),
}


def _json(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _payload(command: OwnerCommand) -> dict[str, object]:
    result = {
        "continue_mode": command.continue_mode,
        "override_note": command.override_note,
        "requested_policy": None
        if command.requested_policy is None
        else _json(asdict(command.requested_policy)),
    }
    return result


class PostgresTaskUsageCheckpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _record(self, row: TaskUsageCheckpoint) -> CheckpointRecord:
        work_unit = await self._session.get(TaskUsageWorkUnit, row.work_unit_id)
        baseline_unit = await self._session.get(TaskUsageWorkUnit, row.baseline_work_unit_id)
        if work_unit is None or baseline_unit is None:
            raise RuntimeError("checkpoint binding is missing")
        return CheckpointRecord(
            checkpoint_id=row.id,
            binding=decode_binding(work_unit),
            baseline_binding=decode_binding(baseline_unit),
            state=CheckpointState(row.state),
            policy_revision=row.policy_revision,
            reference_digest=row.reference_digest,
            trigger_event_id=row.trigger_event_id,
            version=row.version,
            generation=row.generation,
            process_settled=row.process_settled,
            next_checkpoint_threshold_tokens=row.next_checkpoint_threshold_tokens,
            usage_pauses_disabled=row.usage_pauses_disabled,
            monitoring_policy_revision=row.monitoring_policy_revision,
        )

    async def get(self, checkpoint_id: UUID) -> CheckpointRecord | None:
        row = await self._session.get(TaskUsageCheckpoint, checkpoint_id)
        return None if row is None else await self._record(row)

    async def receipt_for_command(self, command: OwnerCommand) -> OwnerCommandReceipt | None:
        prior = (
            await self._session.execute(
                select(TaskUsageOwnerCommand).where(
                    TaskUsageOwnerCommand.checkpoint_id == command.checkpoint_id,
                    TaskUsageOwnerCommand.idempotency_key == command.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if prior is None:
            return None
        if (
            prior.actor_id != command.actor_id
            or prior.action != command.action
            or prior.expected_version != command.expected_version
            or prior.command_payload != _payload(command)
        ):
            raise CheckpointConflict("idempotency key conflicts with prior command")
        return OwnerCommandReceipt(
            command_id=prior.id,
            checkpoint_id=prior.checkpoint_id,
            result_version=prior.result_version,
            state=prior.state,
            actor_id=prior.actor_id,
            action=prior.action,
            affected_snapshot=prior.affected_snapshot_digest,
            warnings=tuple(prior.warnings),
            replayed=True,
        )

    async def list_for_subject(self, subject: SubjectKey) -> tuple[CheckpointRecord, ...]:
        rows = (
            await self._session.execute(
                select(TaskUsageCheckpoint)
                .where(
                    TaskUsageCheckpoint.project_id == subject.project_id,
                    TaskUsageCheckpoint.subject_kind == subject.kind,
                    TaskUsageCheckpoint.subject_id == subject.subject_id,
                )
                .order_by(TaskUsageCheckpoint.generation, TaskUsageCheckpoint.created_at, TaskUsageCheckpoint.id)
            )
        ).scalars().all()
        return tuple([await self._record(row) for row in rows])

    async def active_for_subject(self, subject: SubjectKey) -> CheckpointRecord | None:
        row = (
            await self._session.execute(
                select(TaskUsageCheckpoint).where(
                    TaskUsageCheckpoint.project_id == subject.project_id,
                    TaskUsageCheckpoint.subject_kind == subject.kind,
                    TaskUsageCheckpoint.subject_id == subject.subject_id,
                    TaskUsageCheckpoint.state.in_(_ACTIVE),
                )
            )
        ).scalar_one_or_none()
        return None if row is None else await self._record(row)

    async def latest_for_subject(self, subject: SubjectKey) -> CheckpointRecord | None:
        row = (
            await self._session.execute(
                select(TaskUsageCheckpoint)
                .where(
                    TaskUsageCheckpoint.project_id == subject.project_id,
                    TaskUsageCheckpoint.subject_kind == subject.kind,
                    TaskUsageCheckpoint.subject_id == subject.subject_id,
                )
                .order_by(TaskUsageCheckpoint.generation.desc(), TaskUsageCheckpoint.created_at.desc(), TaskUsageCheckpoint.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else await self._record(row)

    async def _lock_subject(self, subject: SubjectKey) -> None:
        model = Run if subject.kind == "run" else BrainstormJobRow
        row = (
            await self._session.execute(
                select(model.id)
                .where(model.id == subject.subject_id, model.project_id == subject.project_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise CheckpointConflict("checkpoint subject does not exist")

    async def request(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        if checkpoint.state is not CheckpointState.REQUESTED or checkpoint.version != 0:
            raise ValueError("new checkpoint must be requested at version zero")
        await self._lock_subject(checkpoint.binding.subject)
        same_trigger = (
            await self._session.execute(
                select(TaskUsageCheckpoint).where(
                    TaskUsageCheckpoint.work_unit_id == checkpoint.binding.work_unit_id,
                    TaskUsageCheckpoint.trigger_event_id == checkpoint.trigger_event_id,
                )
            )
        ).scalar_one_or_none()
        if same_trigger is not None:
            stored = await self._record(same_trigger)
            if (
                stored.checkpoint_id == checkpoint.checkpoint_id
                and stored.binding == checkpoint.binding
                and stored.baseline_binding == checkpoint.baseline_binding
                and stored.policy_revision == checkpoint.policy_revision
                and stored.reference_digest == checkpoint.reference_digest
            ):
                return stored
            raise CheckpointConflict("checkpoint trigger identity differs from durable record")
        prior = await self.active_for_subject(checkpoint.binding.subject)
        if prior is not None:
            if prior == checkpoint:
                return prior
            raise CheckpointConflict("another checkpoint is active for the subject")
        latest = await self.latest_for_subject(checkpoint.binding.subject)
        generation = 1 if latest is None else latest.generation + 1
        _positive(generation, "checkpoint generation")
        created = replace(
            checkpoint,
            generation=generation,
            monitoring_policy_revision=(
                checkpoint.monitoring_policy_revision
                if checkpoint.monitoring_policy_revision is not None
                else latest.monitoring_policy_revision if latest is not None else None
            ),
            next_checkpoint_threshold_tokens=(
                checkpoint.next_checkpoint_threshold_tokens
                if checkpoint.next_checkpoint_threshold_tokens is not None
                else latest.next_checkpoint_threshold_tokens if latest is not None else None
            ),
            usage_pauses_disabled=(
                checkpoint.usage_pauses_disabled
                or (latest.usage_pauses_disabled if latest is not None else False)
            ),
        )
        self._session.add(
            TaskUsageCheckpoint(
                id=checkpoint.checkpoint_id,
                project_id=checkpoint.binding.subject.project_id,
                work_unit_id=checkpoint.binding.work_unit_id,
                baseline_work_unit_id=checkpoint.baseline_binding.work_unit_id,
                subject_kind=checkpoint.binding.subject.kind,
                subject_id=checkpoint.binding.subject.subject_id,
                policy_revision=checkpoint.policy_revision,
                reference_digest=checkpoint.reference_digest,
                trigger_event_id=checkpoint.trigger_event_id,
                state=checkpoint.state.value,
                version=checkpoint.version,
                generation=generation,
                process_settled=False,
                next_checkpoint_threshold_tokens=created.next_checkpoint_threshold_tokens,
                usage_pauses_disabled=created.usage_pauses_disabled,
                monitoring_policy_revision=created.monitoring_policy_revision,
            )
        )
        await self._session.flush()
        return created

    async def transition(
        self,
        checkpoint_id: UUID,
        *,
        expected_version: int,
        state: CheckpointState,
        process_settled: bool,
    ) -> CheckpointRecord:
        _positive(expected_version, "expected version", allow_zero=True)
        if not isinstance(state, CheckpointState) or type(process_settled) is not bool:
            raise TypeError("checkpoint transition state/settlement invalid")
        row = (
            await self._session.execute(
                select(TaskUsageCheckpoint)
                .where(TaskUsageCheckpoint.id == checkpoint_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.version != expected_version:
            raise CheckpointConflict("checkpoint version is stale")
        before = CheckpointState(row.state)
        if state not in _EDGES[before]:
            raise CheckpointConflict("checkpoint transition is invalid")
        if row.process_settled and not process_settled:
            raise CheckpointConflict("process settlement cannot be revoked")
        if state in {CheckpointState.PAUSED, CheckpointState.RESUMED} and not process_settled:
            raise CheckpointConflict("process/effect settlement is required")
        row.state = state.value
        row.process_settled = process_settled
        row.version += 1
        await self._session.flush()
        return await self._record(row)

    async def apply_owner_command(
        self,
        command: OwnerCommand,
        *,
        state: CheckpointState,
        affected_snapshot: str,
        warnings: tuple[str, ...] = (),
        next_checkpoint_threshold_tokens: int | None = None,
        usage_pauses_disabled: bool | None = None,
        monitoring_policy_revision: int | None = None,
    ) -> OwnerCommandReceipt:
        _digest(affected_snapshot, "affected snapshot")
        if not isinstance(state, CheckpointState) or any(type(item) is not str for item in warnings):
            raise TypeError("owner command result is invalid")
        row = (
            await self._session.execute(
                select(TaskUsageCheckpoint)
                .where(TaskUsageCheckpoint.id == command.checkpoint_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise CheckpointConflict("checkpoint does not exist")
        replay = await self.receipt_for_command(command)
        if replay is not None:
            return replay
        payload = _payload(command)
        if row.version != command.expected_version:
            raise CheckpointConflict("checkpoint version is stale")
        before = CheckpointState(row.state)
        if state != before and state not in _EDGES[before]:
            raise CheckpointConflict("owner command transition is invalid")
        if command.action in {"continue", "resume"} and (
            before is not CheckpointState.PAUSED or state is not CheckpointState.RESUMED or not row.process_settled
        ):
            raise CheckpointConflict("checkpoint is not safely paused")
        if command.action in {"stop", "resolve"} and state is not CheckpointState.RESOLVED:
            raise CheckpointConflict("stop/resolve must resolve the checkpoint")
        if command.action in {"set_policy", "override_checkpoint"} and state != before:
            raise CheckpointConflict("policy command cannot settle the checkpoint")
        if next_checkpoint_threshold_tokens is not None:
            _positive(next_checkpoint_threshold_tokens, "next checkpoint threshold")
        if monitoring_policy_revision is not None:
            _positive(monitoring_policy_revision, "monitoring policy revision")
        if usage_pauses_disabled is not None and type(usage_pauses_disabled) is not bool:
            raise TypeError("usage-pause bypass must be a boolean")
        if command.action == "continue" and (
            usage_pauses_disabled is None or
            (not usage_pauses_disabled and next_checkpoint_threshold_tokens is None)
        ):
            raise CheckpointConflict("continuation requires a durable threshold or bypass")
        if usage_pauses_disabled is not None:
            row.usage_pauses_disabled = usage_pauses_disabled
        if next_checkpoint_threshold_tokens is not None or row.usage_pauses_disabled:
            row.next_checkpoint_threshold_tokens = next_checkpoint_threshold_tokens
        if monitoring_policy_revision is not None:
            row.monitoring_policy_revision = monitoring_policy_revision
        if row.usage_pauses_disabled and row.next_checkpoint_threshold_tokens is not None:
            raise CheckpointConflict("bypassed usage pauses cannot retain a threshold")
        row.state = state.value
        row.version += 1
        self._session.add(
            TaskUsageOwnerCommand(
                id=command.command_id,
                project_id=row.project_id,
                checkpoint_id=row.id,
                idempotency_key=command.idempotency_key,
                action=command.action,
                expected_version=command.expected_version,
                result_version=row.version,
                actor_id=command.actor_id,
                state=state.value,
                command_payload=payload,
                warnings=list(warnings),
                affected_snapshot_digest=affected_snapshot,
            )
        )
        await self._session.flush()
        return OwnerCommandReceipt(
            command_id=command.command_id,
            checkpoint_id=row.id,
            result_version=row.version,
            state=row.state,
            actor_id=command.actor_id,
            action=command.action,
            affected_snapshot=affected_snapshot,
            warnings=warnings,
        )
