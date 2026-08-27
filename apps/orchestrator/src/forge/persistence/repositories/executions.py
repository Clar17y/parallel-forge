"""PostgreSQL persistence for caller-owned agent execution transactions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.executions import (
    ExecutionAdmission,
    ExecutionOutcome,
    ExecutionStatus,
    database_status_for_finish,
)
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.event import RunEvent
from forge.observability.redaction import Redactor
from forge.observability.usage import UsageRecord
from forge.persistence.models import AgentExecution, ModelUsage, Run, Step
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.runs import PersistenceError


class ExecutionRepositoryError(RuntimeError):
    """Base class for stable execution-persistence failures."""

    _MESSAGE = "execution persistence failed"

    def __init__(self, _detail: object = None) -> None:
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


# The longer name is useful to application callers while retaining the short
# repository error name used by the other persistence adapters.
ExecutionPersistenceError = ExecutionRepositoryError


class ExecutionConflict(ExecutionRepositoryError):
    """The requested immutable or lifecycle evidence conflicts with storage."""

    _MESSAGE = "execution evidence conflicts with existing state"


class ExecutionNotFound(ExecutionRepositoryError):
    """The requested run or execution evidence does not exist."""

    _MESSAGE = "execution evidence was not found"


class ExecutionDataError(ExecutionRepositoryError):
    """Persisted execution evidence is malformed or internally inconsistent."""

    _MESSAGE = "persisted execution evidence is malformed"


_TERMINAL_STATUSES: Final[frozenset[ExecutionStatus]] = frozenset(
    {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)
_FAILED_FINISH_STATUSES: Final[frozenset[AgentFinishStatus]] = frozenset(
    {
        AgentFinishStatus.BUDGET_EXCEEDED,
        AgentFinishStatus.INVALID_OUTPUT,
        AgentFinishStatus.TIMED_OUT,
        AgentFinishStatus.TOOL_DENIED,
        AgentFinishStatus.FAILED,
    }
)


class PostgresExecutionRepository:
    """Persist execution evidence without beginning, committing, or rolling back."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        events: PostgresEventRepository | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._session = session
        self._events = events or PostgresEventRepository(session, redactor=redactor)

    @property
    def session(self) -> AsyncSession:
        """Return the exact session supplied by the active unit of work."""

        return self._session

    async def admit(
        self,
        run_id: UUID,
        step_id: UUID,
        agent_execution_id: UUID,
        kind: str,
        attempt: int,
        role: AgentRole,
        instruction_version: str,
        provider: str,
        model: str,
        *,
        input_artifact_id: UUID | None = None,
        transition_from: str | None = None,
        transition_to: str | None = None,
        admitted_at: datetime | None = None,
    ) -> ExecutionAdmission:
        """Admit one running step and execution in the caller's transaction."""

        requested_admitted_at = admitted_at
        timestamp = _validate_admission_arguments(
            run_id,
            step_id,
            agent_execution_id,
            kind,
            attempt,
            role,
            instruction_version,
            provider,
            model,
            input_artifact_id,
            transition_from,
            transition_to,
            admitted_at,
        )
        try:
            run = await self._locked_run(run_id)
            step = await self._locked_step_for_key(run_id, kind, attempt)
            if step is None:
                # Lock both caller-supplied identities before inserting.  A
                # reused identity from another lineage is a conflict, never a
                # reason to infer or repair a missing row.
                existing_step = await self._locked_step_by_id(step_id)
                existing_execution = await self._locked_execution_by_id(agent_execution_id)
                linked_execution = await self._locked_executions_for_step(step_id)
                if existing_step is not None or existing_execution is not None or linked_execution:
                    raise ExecutionConflict()
                step = Step(
                    id=step_id,
                    run_id=run_id,
                    kind=kind,
                    attempt=attempt,
                    status=ExecutionStatus.RUNNING.value,
                    transition_from=transition_from,
                    transition_to=transition_to,
                    started_at=timestamp,
                )
                execution = AgentExecution(
                    id=agent_execution_id,
                    run_id=run_id,
                    step_id=step_id,
                    role=role.value,
                    instruction_version=instruction_version,
                    provider=provider,
                    model=model,
                    status=ExecutionStatus.RUNNING.value,
                    input_artifact_id=input_artifact_id,
                    started_at=timestamp,
                )
                self._session.add(step)
                await self._session.flush()
                self._session.add(execution)
                await self._session.flush()
                admission = _admission_from_rows(
                    run,
                    step,
                    execution,
                    is_new=True,
                    finish_status=None,
                )
                await self._append_admitted_event(admission, run_version=run.version)
                return admission

            linked = await self._locked_executions_for_step(step.id)
            if len(linked) != 1:
                raise ExecutionConflict()
            execution = linked[0]
            if execution.id != agent_execution_id:
                raise ExecutionConflict()
            _validate_admission_identity(
                step,
                execution,
                run_id=run_id,
                step_id=step_id,
                agent_execution_id=agent_execution_id,
                kind=kind,
                attempt=attempt,
                role=role,
                instruction_version=instruction_version,
                provider=provider,
                model=model,
                input_artifact_id=input_artifact_id,
                transition_from=transition_from,
                transition_to=transition_to,
                requested_admitted_at=requested_admitted_at,
            )
            finish_status, _usage = await self._inspect_pair(step, execution)
            return _admission_from_rows(
                run,
                step,
                execution,
                is_new=False,
                finish_status=finish_status,
            )
        except ExecutionConflict, ExecutionDataError, ExecutionNotFound:
            raise
        except IntegrityError:
            raise ExecutionConflict() from None
        except PersistenceError, SQLAlchemyError:
            raise ExecutionRepositoryError() from None

    async def finalize(
        self,
        run_id: UUID,
        step_id: UUID,
        agent_execution_id: UUID,
        finish_status: AgentFinishStatus,
        usage: UsageRecord,
        *,
        output_artifact_id: UUID | None = None,
        completed_at: datetime | None = None,
        provider: str | None = None,
        model: str | None = None,
        instruction_version: str | None = None,
        kind: str | None = None,
        attempt: int | None = None,
        role: AgentRole | None = None,
    ) -> ExecutionOutcome:
        """Finalize one execution and usage row in the caller's transaction."""

        requested_completed_at = completed_at
        timestamp = _validate_finalization_arguments(
            run_id,
            step_id,
            agent_execution_id,
            finish_status,
            usage,
            output_artifact_id,
            completed_at,
            provider,
            model,
            instruction_version,
            kind,
            attempt,
            role,
        )
        target_status = database_status_for_finish(finish_status)
        try:
            run = await self._locked_run(run_id)
            step = await self._locked_step(run_id, step_id)
            execution = await self._locked_execution(run_id, agent_execution_id)
            if step is None or execution is None:
                raise ExecutionNotFound()
            if step.id != step_id or execution.id != agent_execution_id:
                raise ExecutionConflict()
            if execution.step_id != step.id:
                raise ExecutionConflict()
            _validate_optional_finalization_identity(
                step,
                execution,
                provider=provider,
                model=model,
                instruction_version=instruction_version,
                kind=kind,
                attempt=attempt,
                role=role,
            )
            stored_finish_status, stored_usage = await self._inspect_pair(step, execution)
            current_status = _execution_status(step.status)
            if current_status is not ExecutionStatus.RUNNING:
                if stored_finish_status is None or stored_usage is None:
                    raise ExecutionDataError()
                _validate_repeated_finalization(
                    step,
                    execution,
                    stored_finish_status=stored_finish_status,
                    requested_finish_status=finish_status,
                    stored_usage=stored_usage,
                    requested_usage=usage,
                    requested_output_artifact_id=output_artifact_id,
                    requested_completed_at=requested_completed_at,
                    run_id=run_id,
                    agent_execution_id=agent_execution_id,
                )
                return _outcome_from_rows(
                    step,
                    execution,
                    stored_usage,
                    finish_status=stored_finish_status,
                    changed=False,
                )
            if stored_finish_status is not None or stored_usage is not None:
                raise ExecutionDataError()
            if step.completed_at is not None or execution.completed_at is not None:
                raise ExecutionDataError()
            if step.outcome is not None:
                raise ExecutionDataError()
            if timestamp < _required_started_at(step, execution):
                raise ExecutionConflict()
            normalized_usage = _prepare_usage(
                usage,
                run_id=run_id,
                agent_execution_id=agent_execution_id,
                provider=execution.provider,
                model=execution.model,
                instruction_version=execution.instruction_version,
            )
            usage_row = _usage_row(normalized_usage)
            self._session.add(usage_row)
            step.status = target_status.value
            step.outcome = finish_status.value
            step.output_artifact_id = output_artifact_id
            step.completed_at = timestamp
            execution.status = target_status.value
            execution.output_artifact_id = output_artifact_id
            execution.completed_at = timestamp
            await self._session.flush()
            await self._session.refresh(usage_row)
            stored_usage = _usage_from_row(usage_row)
            outcome = _outcome_from_rows(
                step,
                execution,
                stored_usage,
                finish_status=finish_status,
                changed=True,
            )
            await self._append_finalized_event(
                outcome,
                run_version=run.version,
                input_artifact_id=execution.input_artifact_id,
            )
            return outcome
        except ExecutionConflict, ExecutionDataError, ExecutionNotFound:
            raise
        except IntegrityError:
            raise ExecutionConflict() from None
        except PersistenceError, SQLAlchemyError:
            raise ExecutionRepositoryError() from None

    async def _locked_run(self, run_id: UUID) -> Run:
        result = await self._session.execute(select(Run).where(Run.id == run_id).with_for_update())
        run = result.scalar_one_or_none()
        if run is None:
            raise ExecutionNotFound()
        if run.id != run_id or type(run.version) is not int or run.version < 0:
            raise ExecutionDataError()
        return run

    async def _locked_step_for_key(self, run_id: UUID, kind: str, attempt: int) -> Step | None:
        result = await self._session.execute(
            select(Step)
            .where(Step.run_id == run_id, Step.kind == kind, Step.attempt == attempt)
            .with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ExecutionDataError()
        return rows[0] if rows else None

    async def _locked_step(self, run_id: UUID, step_id: UUID) -> Step | None:
        result = await self._session.execute(
            select(Step).where(Step.run_id == run_id, Step.id == step_id).with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ExecutionDataError()
        return rows[0] if rows else None

    async def _locked_step_by_id(self, step_id: UUID) -> Step | None:
        result = await self._session.execute(
            select(Step).where(Step.id == step_id).with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ExecutionDataError()
        return rows[0] if rows else None

    async def _locked_execution(self, run_id: UUID, execution_id: UUID) -> AgentExecution | None:
        result = await self._session.execute(
            select(AgentExecution)
            .where(AgentExecution.run_id == run_id, AgentExecution.id == execution_id)
            .with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ExecutionDataError()
        return rows[0] if rows else None

    async def _locked_execution_by_id(self, execution_id: UUID) -> AgentExecution | None:
        result = await self._session.execute(
            select(AgentExecution).where(AgentExecution.id == execution_id).with_for_update()
        )
        rows = result.scalars().all()
        if len(rows) > 1:
            raise ExecutionDataError()
        return rows[0] if rows else None

    async def _locked_executions_for_step(self, step_id: UUID) -> list[AgentExecution]:
        result = await self._session.execute(
            select(AgentExecution)
            .where(AgentExecution.step_id == step_id)
            .order_by(AgentExecution.id)
            .with_for_update()
        )
        return list(result.scalars().all())

    async def _locked_usage(self, execution: AgentExecution) -> list[ModelUsage]:
        result = await self._session.execute(
            select(ModelUsage)
            .where(
                ModelUsage.run_id == execution.run_id,
                ModelUsage.agent_execution_id == execution.id,
            )
            .order_by(ModelUsage.id)
            .with_for_update()
        )
        return list(result.scalars().all())

    async def _inspect_pair(
        self, step: Step, execution: AgentExecution
    ) -> tuple[AgentFinishStatus | None, UsageRecord | None]:
        _validate_stored_lineage(step, execution)
        status = _execution_status(step.status)
        if status is ExecutionStatus.RUNNING:
            if (
                step.started_at is None
                or execution.started_at is None
                or step.started_at != execution.started_at
                or step.completed_at is not None
                or execution.completed_at is not None
                or step.outcome is not None
                or step.output_artifact_id is not None
                or execution.output_artifact_id is not None
            ):
                raise ExecutionDataError()
            rows = await self._locked_usage(execution)
            if rows:
                raise ExecutionDataError()
            return None, None
        if status not in _TERMINAL_STATUSES:
            raise ExecutionDataError()
        finish_status = _finish_status_from_row(status, step.outcome)
        rows = await self._locked_usage(execution)
        if len(rows) != 1:
            raise ExecutionDataError()
        usage = _usage_from_row(rows[0])
        _validate_stored_usage(usage, execution)
        if (
            step.started_at is None
            or execution.started_at is None
            or step.completed_at is None
            or execution.completed_at is None
            or step.started_at != execution.started_at
            or step.completed_at != execution.completed_at
            or step.completed_at < step.started_at
        ):
            raise ExecutionDataError()
        return finish_status, usage

    async def _append_admitted_event(
        self, admission: ExecutionAdmission, *, run_version: int
    ) -> None:
        await self._events.append(
            RunEvent(
                run_id=admission.run_id,
                run_version=run_version,
                event_type="agent_execution.admitted",
                payload=_admission_event_payload(admission),
                occurred_at=admission.admitted_at,
            )
        )

    async def _append_finalized_event(
        self,
        outcome: ExecutionOutcome,
        *,
        run_version: int,
        input_artifact_id: UUID | None,
    ) -> None:
        await self._events.append(
            RunEvent(
                run_id=outcome.run_id,
                run_version=run_version,
                event_type="agent_execution.finalized",
                payload=_finalized_event_payload(outcome, input_artifact_id=input_artifact_id),
                occurred_at=outcome.completed_at,
            )
        )


def _validate_admission_arguments(
    run_id: UUID,
    step_id: UUID,
    agent_execution_id: UUID,
    kind: str,
    attempt: int,
    role: AgentRole,
    instruction_version: str,
    provider: str,
    model: str,
    input_artifact_id: UUID | None,
    transition_from: str | None,
    transition_to: str | None,
    admitted_at: datetime | None,
) -> datetime:
    for value, name in (
        (run_id, "run identifier"),
        (step_id, "step identifier"),
        (agent_execution_id, "agent execution identifier"),
    ):
        _validate_uuid(value, name)
    _validate_text(kind, "execution kind", 96)
    if type(attempt) is not int or attempt < 1:
        raise ValueError("execution attempt must be a positive integer")
    if not isinstance(role, AgentRole):
        raise TypeError("execution role must be an AgentRole")
    _validate_text(instruction_version, "instruction version", 96)
    _validate_text(provider, "provider", 96)
    _validate_text(model, "model", 255)
    _validate_optional_uuid(input_artifact_id, "input artifact identifier")
    _validate_optional_text(transition_from, "transition-from metadata", 48)
    _validate_optional_text(transition_to, "transition-to metadata", 48)
    timestamp = datetime.now(UTC) if admitted_at is None else admitted_at
    return _validate_timestamp(timestamp, "admission timestamp")


def _validate_finalization_arguments(
    run_id: UUID,
    step_id: UUID,
    agent_execution_id: UUID,
    finish_status: AgentFinishStatus,
    usage: UsageRecord,
    output_artifact_id: UUID | None,
    completed_at: datetime | None,
    provider: str | None,
    model: str | None,
    instruction_version: str | None,
    kind: str | None,
    attempt: int | None,
    role: AgentRole | None,
) -> datetime:
    for value, name in (
        (run_id, "run identifier"),
        (step_id, "step identifier"),
        (agent_execution_id, "agent execution identifier"),
    ):
        _validate_uuid(value, name)
    if not isinstance(finish_status, AgentFinishStatus):
        raise TypeError("finish status must be an AgentFinishStatus")
    if not isinstance(usage, UsageRecord):
        raise TypeError("execution finalization requires a UsageRecord")
    if usage.pricing_version is None or usage.currency is None:
        raise ValueError("execution finalization requires priced or explicitly unknown usage")
    if usage.id is not None:
        _validate_uuid(usage.id, "usage identifier")
    _validate_optional_uuid(output_artifact_id, "output artifact identifier")
    timestamp = datetime.now(UTC) if completed_at is None else completed_at
    timestamp = _validate_timestamp(timestamp, "completion timestamp")
    _validate_optional_text(provider, "provider", 96)
    _validate_optional_text(model, "model", 255)
    _validate_optional_text(instruction_version, "instruction version", 96)
    _validate_optional_text(kind, "execution kind", 96)
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        raise ValueError("execution attempt must be a positive integer")
    if role is not None and not isinstance(role, AgentRole):
        raise TypeError("execution role must be an AgentRole")
    return timestamp


def _validate_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be nil")


def _validate_optional_uuid(value: UUID | None, field_name: str) -> None:
    if value is not None:
        _validate_uuid(value, field_name)


def _validate_text(value: str, field_name: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")


def _validate_optional_text(value: str | None, field_name: str, maximum: int) -> None:
    if value is not None:
        _validate_text(value, field_name, maximum)


def _validate_timestamp(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _validate_admission_identity(
    step: Step,
    execution: AgentExecution,
    *,
    run_id: UUID,
    step_id: UUID,
    agent_execution_id: UUID,
    kind: str,
    attempt: int,
    role: AgentRole,
    instruction_version: str,
    provider: str,
    model: str,
    input_artifact_id: UUID | None,
    transition_from: str | None,
    transition_to: str | None,
    requested_admitted_at: datetime | None,
) -> None:
    if (
        step.id != step_id
        or step.run_id != run_id
        or step.kind != kind
        or step.attempt != attempt
        or execution.id != agent_execution_id
        or execution.run_id != run_id
        or execution.step_id != step.id
        or execution.role != role.value
        or execution.instruction_version != instruction_version
        or execution.provider != provider
        or execution.model != model
        or execution.input_artifact_id != input_artifact_id
        or step.transition_from != transition_from
        or step.transition_to != transition_to
        or (
            requested_admitted_at is not None
            and (
                step.started_at != requested_admitted_at
                or execution.started_at != requested_admitted_at
            )
        )
    ):
        raise ExecutionConflict()


def _validate_optional_finalization_identity(
    step: Step,
    execution: AgentExecution,
    *,
    provider: str | None,
    model: str | None,
    instruction_version: str | None,
    kind: str | None,
    attempt: int | None,
    role: AgentRole | None,
) -> None:
    if (
        (provider is not None and provider != execution.provider)
        or (model is not None and model != execution.model)
        or (
            instruction_version is not None and instruction_version != execution.instruction_version
        )
        or (kind is not None and kind != step.kind)
        or (attempt is not None and attempt != step.attempt)
        or (role is not None and role.value != execution.role)
    ):
        raise ExecutionConflict()


def _validate_stored_lineage(step: Step, execution: AgentExecution) -> None:
    try:
        _validate_uuid(step.id, "step identifier")
        _validate_uuid(step.run_id, "step run identifier")
        _validate_uuid(execution.id, "agent execution identifier")
        _validate_uuid(execution.run_id, "execution run identifier")
        if execution.step_id != step.id or execution.run_id != step.run_id:
            raise ExecutionDataError()
        if execution.status != step.status:
            raise ExecutionDataError()
        _validate_text(step.kind, "execution kind", 96)
        if type(step.attempt) is not int or step.attempt < 1:
            raise ExecutionDataError()
        if execution.role not in {role.value for role in AgentRole}:
            raise ExecutionDataError()
        _validate_text(execution.instruction_version, "instruction version", 96)
        _validate_text(execution.provider, "provider", 96)
        _validate_text(execution.model, "model", 255)
        _validate_optional_uuid(execution.input_artifact_id, "input artifact identifier")
        _validate_optional_uuid(step.output_artifact_id, "output artifact identifier")
        _validate_optional_uuid(execution.output_artifact_id, "output artifact identifier")
        if step.output_artifact_id != execution.output_artifact_id:
            raise ExecutionDataError()
    except ExecutionDataError, TypeError, ValueError:
        raise ExecutionDataError() from None


def _execution_status(value: object) -> ExecutionStatus:
    if not isinstance(value, str):
        raise ExecutionDataError()
    try:
        return ExecutionStatus(value)
    except ValueError:
        raise ExecutionDataError() from None


def _finish_status_from_row(status: ExecutionStatus, outcome: object) -> AgentFinishStatus:
    if not isinstance(outcome, str):
        raise ExecutionDataError()
    try:
        finish_status = AgentFinishStatus(outcome)
    except ValueError:
        raise ExecutionDataError() from None
    if database_status_for_finish(finish_status) is not status:
        raise ExecutionDataError()
    if status is ExecutionStatus.SUCCEEDED and finish_status is not AgentFinishStatus.SUCCEEDED:
        raise ExecutionDataError()
    if status is ExecutionStatus.CANCELLED and finish_status is not AgentFinishStatus.CANCELLED:
        raise ExecutionDataError()
    if status is ExecutionStatus.FAILED and finish_status not in _FAILED_FINISH_STATUSES:
        raise ExecutionDataError()
    return finish_status


def _required_started_at(step: Step, execution: AgentExecution) -> datetime:
    if step.started_at is None or execution.started_at is None:
        raise ExecutionDataError()
    _validate_timestamp(step.started_at, "step start timestamp")
    _validate_timestamp(execution.started_at, "execution start timestamp")
    if step.started_at != execution.started_at:
        raise ExecutionDataError()
    return step.started_at


def _prepare_usage(
    usage: UsageRecord,
    *,
    run_id: UUID,
    agent_execution_id: UUID,
    provider: str,
    model: str,
    instruction_version: str,
) -> UsageRecord:
    if usage.run_id is not None and usage.run_id != run_id:
        raise ExecutionConflict()
    if usage.agent_execution_id is not None and usage.agent_execution_id != agent_execution_id:
        raise ExecutionConflict()
    if usage.provider != provider or usage.model != model:
        raise ExecutionConflict()
    if usage.prompt_version != instruction_version:
        raise ExecutionConflict()
    if usage.pricing_version is None or usage.currency is None:
        raise ExecutionConflict()
    record_id = usage.id or uuid4()
    return replace(usage, id=record_id, run_id=run_id, agent_execution_id=agent_execution_id)


def _usage_row(usage: UsageRecord) -> ModelUsage:
    if usage.id is None or usage.run_id is None or usage.agent_execution_id is None:
        raise ExecutionDataError()
    row = ModelUsage(
        id=usage.id,
        run_id=usage.run_id,
        agent_execution_id=usage.agent_execution_id,
        provider=usage.provider,
        model=usage.model,
        prompt_version=usage.prompt_version,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        duration_ms=usage.duration_ms,
        tool_call_count=usage.tool_call_count,
        provider_request_id=usage.provider_request_id,
        pricing_version=usage.pricing_version,
        estimated_cost_minor=usage.estimated_cost_minor,
        currency=usage.currency,
        unknown_price_reason=usage.unknown_price_reason,
    )
    if usage.created_at is not None:
        row.created_at = usage.created_at
    return row


def _usage_from_row(row: ModelUsage) -> UsageRecord:
    try:
        return UsageRecord(
            id=row.id,
            run_id=row.run_id,
            agent_execution_id=row.agent_execution_id,
            provider=row.provider,
            model=row.model,
            prompt_version=row.prompt_version,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_input_tokens=row.cached_input_tokens,
            duration_ms=row.duration_ms,
            tool_call_count=row.tool_call_count,
            provider_request_id=row.provider_request_id,
            pricing_version=row.pricing_version,
            estimated_cost_minor=row.estimated_cost_minor,
            currency=row.currency,
            unknown_price_reason=row.unknown_price_reason,
            created_at=row.created_at,
        )
    except TypeError, ValueError:
        raise ExecutionDataError() from None


def _validate_stored_usage(usage: UsageRecord, execution: AgentExecution) -> None:
    if (
        usage.run_id != execution.run_id
        or usage.agent_execution_id != execution.id
        or usage.provider != execution.provider
        or usage.model != execution.model
        or usage.prompt_version != execution.instruction_version
        or usage.pricing_version is None
        or usage.currency is None
    ):
        raise ExecutionDataError()


def _admission_from_rows(
    run: Run,
    step: Step,
    execution: AgentExecution,
    *,
    is_new: bool,
    finish_status: AgentFinishStatus | None,
) -> ExecutionAdmission:
    del run
    _validate_stored_lineage(step, execution)
    status = _execution_status(step.status)
    if status not in {ExecutionStatus.RUNNING, *list(_TERMINAL_STATUSES)}:
        raise ExecutionDataError()
    if step.started_at is None or execution.started_at is None:
        raise ExecutionDataError()
    if step.started_at != execution.started_at:
        raise ExecutionDataError()
    _validate_timestamp(step.started_at, "admission timestamp")
    return ExecutionAdmission(
        run_id=step.run_id,
        step_id=step.id,
        agent_execution_id=execution.id,
        kind=step.kind,
        attempt=step.attempt,
        role=AgentRole(execution.role),
        instruction_version=execution.instruction_version,
        provider=execution.provider,
        model=execution.model,
        input_artifact_id=execution.input_artifact_id,
        transition_from=step.transition_from,
        transition_to=step.transition_to,
        admitted_at=step.started_at,
        is_new=is_new,
        status=status,
        finish_status=finish_status,
    )


def _outcome_from_rows(
    step: Step,
    execution: AgentExecution,
    usage: UsageRecord,
    *,
    finish_status: AgentFinishStatus,
    changed: bool,
) -> ExecutionOutcome:
    _validate_stored_lineage(step, execution)
    status = _execution_status(step.status)
    if step.completed_at is None or execution.completed_at is None:
        raise ExecutionDataError()
    if step.completed_at != execution.completed_at:
        raise ExecutionDataError()
    return ExecutionOutcome(
        run_id=step.run_id,
        step_id=step.id,
        agent_execution_id=execution.id,
        kind=step.kind,
        attempt=step.attempt,
        role=AgentRole(execution.role),
        instruction_version=execution.instruction_version,
        provider=execution.provider,
        model=execution.model,
        finish_status=finish_status,
        status=status,
        output_artifact_id=step.output_artifact_id,
        completed_at=step.completed_at,
        usage=usage,
        changed=changed,
    )


def _validate_repeated_finalization(
    step: Step,
    execution: AgentExecution,
    *,
    stored_finish_status: AgentFinishStatus,
    requested_finish_status: AgentFinishStatus,
    stored_usage: UsageRecord,
    requested_usage: UsageRecord,
    requested_output_artifact_id: UUID | None,
    requested_completed_at: datetime | None,
    run_id: UUID,
    agent_execution_id: UUID,
) -> None:
    if stored_finish_status is not requested_finish_status:
        raise ExecutionConflict()
    if step.output_artifact_id != requested_output_artifact_id:
        raise ExecutionConflict()
    if requested_completed_at is not None and step.completed_at != requested_completed_at:
        raise ExecutionConflict()
    if requested_usage.id is not None and requested_usage.id != stored_usage.id:
        raise ExecutionConflict()
    if (
        requested_usage.created_at is not None
        and requested_usage.created_at != stored_usage.created_at
    ):
        raise ExecutionConflict()
    try:
        normalized = replace(
            requested_usage,
            id=stored_usage.id,
            run_id=run_id,
            agent_execution_id=agent_execution_id,
            created_at=stored_usage.created_at,
        )
    except TypeError, ValueError:
        raise ExecutionConflict() from None
    if normalized != stored_usage:
        raise ExecutionConflict()
    _validate_stored_usage(stored_usage, execution)


def _admission_event_payload(admission: ExecutionAdmission) -> dict[str, object]:
    return {
        "step_id": str(admission.step_id),
        "agent_execution_id": str(admission.agent_execution_id),
        "kind": admission.kind,
        "attempt": admission.attempt,
        "role": admission.role.value,
        "status": admission.status.value,
        "instruction_version": admission.instruction_version,
        "provider": admission.provider,
        "model": admission.model,
        "input_artifact_id": (
            None if admission.input_artifact_id is None else str(admission.input_artifact_id)
        ),
    }


def _finalized_event_payload(
    outcome: ExecutionOutcome, *, input_artifact_id: UUID | None
) -> dict[str, object]:
    return {
        "step_id": str(outcome.step_id),
        "agent_execution_id": str(outcome.agent_execution_id),
        "kind": outcome.kind,
        "attempt": outcome.attempt,
        "role": outcome.role.value,
        "status": outcome.status.value,
        "finish_status": outcome.finish_status.value,
        "instruction_version": outcome.instruction_version,
        "provider": outcome.provider,
        "model": outcome.model,
        "input_artifact_id": None if input_artifact_id is None else str(input_artifact_id),
        "output_artifact_id": (
            None if outcome.output_artifact_id is None else str(outcome.output_artifact_id)
        ),
        "usage_id": None if outcome.usage.id is None else str(outcome.usage.id),
    }


__all__ = [
    "ExecutionConflict",
    "ExecutionDataError",
    "ExecutionNotFound",
    "ExecutionPersistenceError",
    "ExecutionRepositoryError",
    "PostgresExecutionRepository",
]
