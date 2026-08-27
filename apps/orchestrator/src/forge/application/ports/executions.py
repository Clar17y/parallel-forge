"""Caller-transaction-bound agent execution persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.observability.usage import UsageRecord

_MAX_KIND_LENGTH = 96
_MAX_INSTRUCTION_VERSION_LENGTH = 96
_MAX_PROVIDER_LENGTH = 96
_MAX_MODEL_LENGTH = 255
_MAX_TRANSITION_LENGTH = 48
_FAILED_FINISH_STATUSES = frozenset(
    {
        AgentFinishStatus.BUDGET_EXCEEDED,
        AgentFinishStatus.INVALID_OUTPUT,
        AgentFinishStatus.TIMED_OUT,
        AgentFinishStatus.TOOL_DENIED,
        AgentFinishStatus.FAILED,
    }
)


class ExecutionStatus(StrEnum):
    """The closed lifecycle states shared by a step and its execution row."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def database_status_for_finish(status: AgentFinishStatus) -> ExecutionStatus:
    """Map the richer gateway result to the database's closed state set."""

    if not isinstance(status, AgentFinishStatus):
        raise TypeError("finish status must be an AgentFinishStatus")
    if status is AgentFinishStatus.SUCCEEDED:
        return ExecutionStatus.SUCCEEDED
    if status is AgentFinishStatus.CANCELLED:
        return ExecutionStatus.CANCELLED
    if status in _FAILED_FINISH_STATUSES:
        return ExecutionStatus.FAILED
    raise ValueError("finish status has no database mapping")


def _non_nil_uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be nil")
    return value


def _bounded_text(value: str, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be non-blank and trimmed")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds its maximum length")
    return value


def _optional_bounded_text(value: str | None, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field_name, maximum)


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionAdmission:
    """Detached evidence that one step/execution pair was admitted."""

    run_id: UUID
    step_id: UUID
    agent_execution_id: UUID
    kind: str
    attempt: int
    role: AgentRole
    instruction_version: str
    provider: str
    model: str
    input_artifact_id: UUID | None
    transition_from: str | None
    transition_to: str | None
    admitted_at: datetime
    is_new: bool
    status: ExecutionStatus = ExecutionStatus.RUNNING
    finish_status: AgentFinishStatus | None = None

    def __post_init__(self) -> None:
        _non_nil_uuid(self.run_id, "run identifier")
        _non_nil_uuid(self.step_id, "step identifier")
        _non_nil_uuid(self.agent_execution_id, "agent execution identifier")
        _bounded_text(self.kind, "execution kind", _MAX_KIND_LENGTH)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("execution attempt must be a positive integer")
        if not isinstance(self.role, AgentRole):
            raise TypeError("execution role must be an AgentRole")
        _bounded_text(
            self.instruction_version,
            "instruction version",
            _MAX_INSTRUCTION_VERSION_LENGTH,
        )
        _bounded_text(self.provider, "provider", _MAX_PROVIDER_LENGTH)
        _bounded_text(self.model, "model", _MAX_MODEL_LENGTH)
        if self.input_artifact_id is not None:
            _non_nil_uuid(self.input_artifact_id, "input artifact identifier")
        _optional_bounded_text(
            self.transition_from, "transition-from metadata", _MAX_TRANSITION_LENGTH
        )
        _optional_bounded_text(self.transition_to, "transition-to metadata", _MAX_TRANSITION_LENGTH)
        _aware_datetime(self.admitted_at, "admission timestamp")
        if type(self.is_new) is not bool:
            raise TypeError("admission is_new must be boolean")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError("admission status must be an ExecutionStatus")
        if self.status is ExecutionStatus.RUNNING:
            if self.finish_status is not None:
                raise ValueError("running admission cannot have a finish status")
        else:
            if self.is_new:
                raise ValueError("a newly admitted execution must be running")
            if not isinstance(self.finish_status, AgentFinishStatus):
                raise ValueError("terminal admission requires a finish status")
            if database_status_for_finish(self.finish_status) is not self.status:
                raise ValueError("admission finish status does not match database status")

    @property
    def execution_id(self) -> UUID:
        """Short alias for callers that name the execution ID directly."""

        return self.agent_execution_id

    @property
    def started_at(self) -> datetime:
        """Alias for the shared admission/start timestamp."""

        return self.admitted_at

    @property
    def step_status(self) -> ExecutionStatus:
        """The step's persisted lifecycle state."""

        return self.status


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionOutcome:
    """Detached evidence returned after one terminal finalization."""

    run_id: UUID
    step_id: UUID
    agent_execution_id: UUID
    kind: str
    attempt: int
    role: AgentRole
    instruction_version: str
    provider: str
    model: str
    finish_status: AgentFinishStatus
    status: ExecutionStatus
    output_artifact_id: UUID | None
    completed_at: datetime
    usage: UsageRecord
    changed: bool

    def __post_init__(self) -> None:
        _non_nil_uuid(self.run_id, "run identifier")
        _non_nil_uuid(self.step_id, "step identifier")
        _non_nil_uuid(self.agent_execution_id, "agent execution identifier")
        _bounded_text(self.kind, "execution kind", _MAX_KIND_LENGTH)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("execution attempt must be a positive integer")
        if not isinstance(self.role, AgentRole):
            raise TypeError("execution role must be an AgentRole")
        _bounded_text(
            self.instruction_version,
            "instruction version",
            _MAX_INSTRUCTION_VERSION_LENGTH,
        )
        _bounded_text(self.provider, "provider", _MAX_PROVIDER_LENGTH)
        _bounded_text(self.model, "model", _MAX_MODEL_LENGTH)
        if not isinstance(self.finish_status, AgentFinishStatus):
            raise TypeError("finish status must be an AgentFinishStatus")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError("outcome status must be an ExecutionStatus")
        if database_status_for_finish(self.finish_status) is not self.status:
            raise ValueError("outcome finish status does not match database status")
        if self.output_artifact_id is not None:
            _non_nil_uuid(self.output_artifact_id, "output artifact identifier")
        _aware_datetime(self.completed_at, "completion timestamp")
        if not isinstance(self.usage, UsageRecord):
            raise TypeError("outcome usage must be a UsageRecord")
        if self.usage.run_id != self.run_id:
            raise ValueError("outcome usage run does not match execution run")
        if self.usage.agent_execution_id != self.agent_execution_id:
            raise ValueError("outcome usage execution does not match execution")
        if self.usage.provider != self.provider or self.usage.model != self.model:
            raise ValueError("outcome usage provider/model does not match execution")
        if self.usage.prompt_version != self.instruction_version:
            raise ValueError("outcome usage prompt version does not match instruction version")
        if type(self.changed) is not bool:
            raise TypeError("outcome changed must be boolean")

    @property
    def execution_id(self) -> UUID:
        """Short alias for callers that name the execution ID directly."""

        return self.agent_execution_id

    @property
    def execution_status(self) -> ExecutionStatus:
        """The agent-execution row's persisted lifecycle state."""

        return self.status

    @property
    def step_status(self) -> ExecutionStatus:
        """The step row's persisted lifecycle state."""

        return self.status

    @property
    def finished_at(self) -> datetime:
        """Alias for the shared completion timestamp."""

        return self.completed_at


@runtime_checkable
class ExecutionRepository(Protocol):
    """Persistence operations that never own the caller's transaction."""

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
    ) -> ExecutionAdmission: ...

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
    ) -> ExecutionOutcome: ...


__all__ = [
    "ExecutionAdmission",
    "ExecutionOutcome",
    "ExecutionRepository",
    "ExecutionStatus",
    "database_status_for_finish",
]
