"""PostgreSQL persistence for caller-owned controller step transactions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.controller_steps import (
    VALID_CONTROLLER_STEP_KINDS,
    ControllerStepRecord,
    ControllerStepUnsettledError,
)
from forge.application.ports.executions import ExecutionStatus
from forge.domain.event import RunEvent
from forge.observability.redaction import Redactor
from forge.persistence.models import AgentExecution, ArtifactLineage, Run, Step
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.runs import PersistenceError

_TERMINAL_STATUSES: Final[frozenset[ExecutionStatus]] = frozenset(
    {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)


class ControllerStepRepositoryError(RuntimeError):
    """Base class for stable controller-step persistence failures."""

    _MESSAGE = "controller step persistence failed"

    def __init__(self, _detail: object = None) -> None:
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


ControllerStepPersistenceError = ControllerStepRepositoryError


class ControllerStepConflict(ControllerStepRepositoryError):
    """The requested controller step evidence conflicts with existing state."""

    _MESSAGE = "controller step evidence conflicts with existing state"


class ControllerStepNotFound(ControllerStepRepositoryError):
    """The requested run or controller step evidence was not found."""

    _MESSAGE = "controller step evidence was not found"


class ControllerStepDataError(ControllerStepRepositoryError):
    """Persisted controller step evidence is malformed or internally inconsistent."""

    _MESSAGE = "persisted controller step evidence is malformed"


class PostgresControllerStepRepository:
    """Persist controller step evidence without beginning, committing, or rolling back."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        events: PostgresEventRepository | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._session = session
        self._redactor = redactor or Redactor()
        self._events = events or PostgresEventRepository(session, redactor=self._redactor)

    @property
    def session(self) -> AsyncSession:
        """Return the exact session supplied by the active unit of work."""
        return self._session

    async def admit(
        self,
        run_id: UUID,
        step_id: UUID,
        kind: str,
        attempt: int,
        *,
        started_at: datetime | None = None,
    ) -> ControllerStepRecord:
        """Admit one running controller step in the caller's transaction."""
        requested_started_at = started_at
        timestamp = _validate_admission_arguments(
            run_id=run_id,
            step_id=step_id,
            kind=kind,
            attempt=attempt,
            started_at=started_at,
        )

        try:
            run = await self._locked_run(run_id)
            step = await self._locked_step_for_key(run_id, kind, attempt)
            if step is None:
                # Check for existing step_id under any run or kind/attempt
                existing_step = await self._locked_step_by_id(step_id)
                linked_executions = await self._locked_executions_for_step(step_id)
                if existing_step is not None or linked_executions:
                    raise ControllerStepConflict("step identifier is already in use")

                step = Step(
                    id=step_id,
                    run_id=run_id,
                    kind=kind,
                    attempt=attempt,
                    status=ExecutionStatus.RUNNING.value,
                    started_at=timestamp,
                )
                self._session.add(step)
                await self._session.flush()

                record = ControllerStepRecord(
                    run_id=run_id,
                    step_id=step_id,
                    kind=kind,
                    attempt=attempt,
                    status=ExecutionStatus.RUNNING,
                    started_at=timestamp,
                    is_new=True,
                )
                await self._append_admitted_event(record, run_version=run.version)
                return record

            # Exact replay or conflict check
            if step.id != step_id or step.run_id != run_id:
                raise ControllerStepConflict("replay identity mismatch")
            if requested_started_at is not None and step.started_at != requested_started_at:
                raise ControllerStepConflict("replay timestamp mismatch")

            linked = await self._locked_executions_for_step(step.id)
            if linked:
                raise ControllerStepConflict("step is linked to an agent execution")

            return _record_from_step(step, is_new=False)
        except ControllerStepConflict, ControllerStepDataError, ControllerStepNotFound:
            raise
        except IntegrityError:
            raise ControllerStepConflict() from None
        except PersistenceError, SQLAlchemyError:
            raise ControllerStepRepositoryError() from None

    async def get(
        self,
        run_id: UUID,
        step_id: UUID,
    ) -> ControllerStepRecord | None:
        """Resolve one controller step without exposing the session to callers."""
        _validate_uuid(run_id, "run identifier")
        _validate_uuid(step_id, "step identifier")

        try:
            await self._locked_run(run_id)
            step = await self._locked_step(run_id, step_id)
            if step is None:
                existing = await self._locked_step_by_id(step_id)
                if existing is not None:
                    raise ControllerStepConflict("step belongs to another run")
                return None

            linked = await self._locked_executions_for_step(step.id)
            if linked:
                raise ControllerStepConflict("step is linked to an agent execution")

            if step.kind not in VALID_CONTROLLER_STEP_KINDS:
                raise ControllerStepConflict("unsupported controller step kind")

            return _record_from_step(step, is_new=False)
        except ControllerStepConflict, ControllerStepDataError, ControllerStepNotFound:
            raise
        except PersistenceError, SQLAlchemyError:
            raise ControllerStepRepositoryError() from None

    async def next_attempt(
        self,
        run_id: UUID,
        kind: str,
    ) -> int:
        """Return the next attempt number, rejecting unresolved running attempts."""
        _validate_uuid(run_id, "run identifier")
        _validate_kind(kind)

        try:
            await self._locked_run(run_id)

            # Reject if any Step for this run and kind is linked to an AgentExecution
            agent_linked = await self._session.scalar(
                select(Step.id)
                .join(AgentExecution, AgentExecution.step_id == Step.id)
                .where(Step.run_id == run_id, Step.kind == kind)
                .limit(1)
            )
            if agent_linked is not None:
                raise ControllerStepConflict("step is linked to an agent execution")

            # Check for any unresolved RUNNING attempt
            unsettled = await self._session.scalar(
                select(Step.id)
                .where(
                    Step.run_id == run_id,
                    Step.kind == kind,
                    Step.status == ExecutionStatus.RUNNING.value,
                )
                .limit(1)
            )
            if unsettled is not None:
                raise ControllerStepUnsettledError(
                    "previous controller step attempt is still running"
                )

            max_attempt = await self._session.scalar(
                select(func.max(Step.attempt)).where(Step.run_id == run_id, Step.kind == kind)
            )
            return (max_attempt or 0) + 1
        except (
            ControllerStepConflict,
            ControllerStepDataError,
            ControllerStepNotFound,
            ControllerStepUnsettledError,
        ):
            raise
        except PersistenceError, SQLAlchemyError:
            raise ControllerStepRepositoryError() from None

    async def finalize(
        self,
        run_id: UUID,
        step_id: UUID,
        status: ExecutionStatus,
        *,
        output_artifact_id: UUID | None = None,
        outcome: str | None = None,
        completed_at: datetime | None = None,
    ) -> ControllerStepRecord:
        """Finalize one controller step in the caller's transaction."""
        requested_completed_at = completed_at
        timestamp = _validate_finalization_arguments(
            run_id=run_id,
            step_id=step_id,
            status=status,
            output_artifact_id=output_artifact_id,
            outcome=outcome,
            completed_at=completed_at,
        )

        redacted_outcome: str | None = None
        if outcome is not None:
            redacted = self._redactor.redact(outcome)
            if not isinstance(redacted, str):
                raise ControllerStepDataError("redacted outcome is not a string")
            redacted_outcome = redacted

        try:
            run = await self._locked_run(run_id)
            step = await self._locked_step(run_id, step_id)
            if step is None:
                existing = await self._locked_step_by_id(step_id)
                if existing is not None:
                    raise ControllerStepConflict("step belongs to another run")
                raise ControllerStepNotFound("controller step was not found")

            linked = await self._locked_executions_for_step(step.id)
            if linked:
                raise ControllerStepConflict("step is linked to an agent execution")

            if (
                step.kind not in VALID_CONTROLLER_STEP_KINDS
                or type(step.attempt) is not int
                or step.attempt < 1
            ):
                raise ControllerStepDataError()

            if step.started_at is None:
                raise ControllerStepDataError()
            _validate_timestamp(step.started_at, "step start timestamp")

            if timestamp < step.started_at:
                raise ControllerStepConflict("completion timestamp cannot precede start timestamp")

            if output_artifact_id is not None:
                lineage = await self._session.scalar(
                    select(ArtifactLineage.id)
                    .where(
                        ArtifactLineage.artifact_id == output_artifact_id,
                        ArtifactLineage.run_id == run_id,
                    )
                    .limit(1)
                )
                if lineage is None:
                    raise ControllerStepConflict("output artifact has no lineage in the active run")

            current_status = _parse_execution_status(step.status)
            if current_status is ExecutionStatus.RUNNING:
                if (
                    step.completed_at is not None
                    or step.outcome is not None
                    or step.output_artifact_id is not None
                ):
                    raise ControllerStepDataError()

                step.status = status.value
                step.completed_at = timestamp
                step.outcome = redacted_outcome
                step.output_artifact_id = output_artifact_id
                await self._session.flush()

                record = ControllerStepRecord(
                    run_id=run_id,
                    step_id=step_id,
                    kind=step.kind,
                    attempt=step.attempt,
                    status=status,
                    started_at=step.started_at,
                    completed_at=timestamp,
                    outcome=redacted_outcome,
                    output_artifact_id=output_artifact_id,
                    is_new=False,
                )
                await self._append_finalized_event(record, run_version=run.version)
                return record

            if current_status in _TERMINAL_STATUSES:
                if current_status is not status:
                    raise ControllerStepConflict("finalized step status mismatch")
                if step.output_artifact_id != output_artifact_id:
                    raise ControllerStepConflict("finalized step output artifact mismatch")
                if step.outcome != redacted_outcome:
                    raise ControllerStepConflict("finalized step outcome mismatch")
                if (
                    requested_completed_at is not None
                    and step.completed_at != requested_completed_at
                ):
                    raise ControllerStepConflict("finalized step completion timestamp mismatch")
                if step.completed_at is None or step.completed_at < step.started_at:
                    raise ControllerStepDataError()

                return _record_from_step(step, is_new=False)

            raise ControllerStepDataError(f"unsupported step status: {step.status!r}")
        except ControllerStepConflict, ControllerStepDataError, ControllerStepNotFound:
            raise
        except IntegrityError:
            raise ControllerStepConflict() from None
        except PersistenceError, SQLAlchemyError:
            raise ControllerStepRepositoryError() from None

    async def _locked_run(self, run_id: UUID) -> Run:
        result = await self._session.execute(select(Run).where(Run.id == run_id).with_for_update())
        run = result.scalar_one_or_none()
        if run is None:
            raise ControllerStepNotFound()
        if run.id != run_id or type(run.version) is not int or run.version < 0:
            raise ControllerStepDataError()
        return run

    async def _locked_step_for_key(self, run_id: UUID, kind: str, attempt: int) -> Step | None:
        result = await self._session.execute(
            select(Step)
            .where(Step.run_id == run_id, Step.kind == kind, Step.attempt == attempt)
            .with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ControllerStepDataError()
        return rows[0] if rows else None

    async def _locked_step(self, run_id: UUID, step_id: UUID) -> Step | None:
        result = await self._session.execute(
            select(Step).where(Step.run_id == run_id, Step.id == step_id).with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ControllerStepDataError()
        return rows[0] if rows else None

    async def _locked_step_by_id(self, step_id: UUID) -> Step | None:
        result = await self._session.execute(
            select(Step).where(Step.id == step_id).with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ControllerStepDataError()
        return rows[0] if rows else None

    async def _locked_executions_for_step(self, step_id: UUID) -> list[AgentExecution]:
        result = await self._session.execute(
            select(AgentExecution)
            .where(AgentExecution.step_id == step_id)
            .order_by(AgentExecution.id)
            .with_for_update()
        )
        return list(result.scalars().all())

    async def _append_admitted_event(
        self, record: ControllerStepRecord, *, run_version: int
    ) -> None:
        await self._events.append(
            RunEvent(
                run_id=record.run_id,
                run_version=run_version,
                event_type="controller_step.admitted",
                payload=_admission_event_payload(record),
                occurred_at=record.started_at,
            )
        )

    async def _append_finalized_event(
        self, record: ControllerStepRecord, *, run_version: int
    ) -> None:
        await self._events.append(
            RunEvent(
                run_id=record.run_id,
                run_version=run_version,
                event_type="controller_step.finalized",
                payload=_finalized_event_payload(record),
                occurred_at=record.completed_at or datetime.now(UTC),
            )
        )


def _validate_admission_arguments(
    *,
    run_id: UUID,
    step_id: UUID,
    kind: str,
    attempt: int,
    started_at: datetime | None,
) -> datetime:
    _validate_uuid(run_id, "run identifier")
    _validate_uuid(step_id, "step identifier")
    _validate_kind(kind)
    if type(attempt) is not int or attempt < 1:
        raise ValueError("execution attempt must be a positive integer")
    timestamp = datetime.now(UTC) if started_at is None else started_at
    return _validate_timestamp(timestamp, "admission timestamp")


def _validate_finalization_arguments(
    *,
    run_id: UUID,
    step_id: UUID,
    status: ExecutionStatus,
    output_artifact_id: UUID | None,
    outcome: str | None,
    completed_at: datetime | None,
) -> datetime:
    _validate_uuid(run_id, "run identifier")
    _validate_uuid(step_id, "step identifier")
    if not isinstance(status, ExecutionStatus):
        raise TypeError("status must be an ExecutionStatus")
    if status is ExecutionStatus.RUNNING:
        raise ValueError("finalization requires a terminal status")
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"unsupported terminal status: {status!r}")
    if output_artifact_id is not None:
        _validate_uuid(output_artifact_id, "output artifact identifier")
    if outcome is not None and not isinstance(outcome, str):
        raise TypeError("outcome must be a string")
    timestamp = datetime.now(UTC) if completed_at is None else completed_at
    return _validate_timestamp(timestamp, "completion timestamp")


def _validate_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be nil")


def _validate_kind(kind: str) -> None:
    if not isinstance(kind, str):
        raise TypeError("controller step kind must be a string")
    if not kind or not kind.strip() or kind != kind.strip():
        raise ValueError("controller step kind must be non-blank and trimmed")
    if kind not in VALID_CONTROLLER_STEP_KINDS:
        raise ValueError(f"unsupported controller step kind: {kind!r}")


def _validate_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _parse_execution_status(value: object) -> ExecutionStatus:
    if not isinstance(value, str):
        raise ControllerStepDataError()
    try:
        return ExecutionStatus(value)
    except ValueError:
        raise ControllerStepDataError() from None


def _record_from_step(step: Step, *, is_new: bool) -> ControllerStepRecord:
    try:
        _validate_uuid(step.id, "step identifier")
        _validate_uuid(step.run_id, "run identifier")
        _validate_kind(step.kind)
        if type(step.attempt) is not int or step.attempt < 1:
            raise ControllerStepDataError()
        status = _parse_execution_status(step.status)
        if step.started_at is None:
            raise ControllerStepDataError()
        _validate_timestamp(step.started_at, "step started_at")

        if step.completed_at is not None:
            _validate_timestamp(step.completed_at, "step completed_at")
            if step.completed_at < step.started_at:
                raise ControllerStepDataError()

        if step.output_artifact_id is not None:
            _validate_uuid(step.output_artifact_id, "step output_artifact_id")

        if status is ExecutionStatus.RUNNING:
            if (
                step.completed_at is not None
                or step.outcome is not None
                or step.output_artifact_id is not None
            ):
                raise ControllerStepDataError()
        elif status in _TERMINAL_STATUSES:
            if step.completed_at is None:
                raise ControllerStepDataError()
        else:
            raise ControllerStepDataError()

        return ControllerStepRecord(
            run_id=step.run_id,
            step_id=step.id,
            kind=step.kind,
            attempt=step.attempt,
            status=status,
            started_at=step.started_at,
            completed_at=step.completed_at,
            outcome=step.outcome,
            output_artifact_id=step.output_artifact_id,
            is_new=is_new,
        )
    except ControllerStepDataError, TypeError, ValueError:
        raise ControllerStepDataError() from None


def _admission_event_payload(record: ControllerStepRecord) -> dict[str, object]:
    return {
        "step_id": str(record.step_id),
        "kind": record.kind,
        "attempt": record.attempt,
        "status": record.status.value,
    }


def _finalized_event_payload(record: ControllerStepRecord) -> dict[str, object]:
    return {
        "step_id": str(record.step_id),
        "kind": record.kind,
        "attempt": record.attempt,
        "status": record.status.value,
        "output_artifact_id": (
            None if record.output_artifact_id is None else str(record.output_artifact_id)
        ),
        "outcome": record.outcome,
    }


__all__ = [
    "ControllerStepConflict",
    "ControllerStepDataError",
    "ControllerStepNotFound",
    "ControllerStepPersistenceError",
    "ControllerStepRepositoryError",
    "PostgresControllerStepRepository",
]
