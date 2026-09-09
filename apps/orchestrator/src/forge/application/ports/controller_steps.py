"""Caller-transaction-bound controller step persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from forge.application.ports.executions import ExecutionStatus

_TERMINAL_STATUSES: Final[frozenset[ExecutionStatus]] = frozenset(
    {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
)
VALID_CONTROLLER_STEP_KINDS: Final[frozenset[str]] = frozenset({"validate"})
_MAX_KIND_LENGTH: Final[int] = 96


class ControllerStepUnsettledError(RuntimeError):
    """A prior controller step attempt requires resolution before another attempt is admissible."""


def _non_nil_uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be nil")
    return value


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _validate_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise TypeError("controller step kind must be a string")
    trimmed = kind.strip()
    if not trimmed or trimmed != kind or len(kind) > _MAX_KIND_LENGTH:
        raise ValueError("controller step kind must be non-blank and trimmed")
    if kind not in VALID_CONTROLLER_STEP_KINDS:
        raise ValueError(f"unsupported controller step kind: {kind!r}")
    return kind


@dataclass(frozen=True, slots=True, kw_only=True)
class ControllerStepRecord:
    """Detached evidence that one controller step was admitted or finalized."""

    run_id: UUID
    step_id: UUID
    kind: str
    attempt: int
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime | None = None
    outcome: str | None = None
    output_artifact_id: UUID | None = None
    is_new: bool = False

    def __post_init__(self) -> None:
        _non_nil_uuid(self.run_id, "run identifier")
        _non_nil_uuid(self.step_id, "step identifier")
        _validate_kind(self.kind)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("step attempt must be a positive integer")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError("step status must be an ExecutionStatus")
        _aware_datetime(self.started_at, "start timestamp")
        if self.completed_at is not None:
            _aware_datetime(self.completed_at, "completion timestamp")
        if self.outcome is not None and not isinstance(self.outcome, str):
            raise TypeError("step outcome must be a string")
        if self.output_artifact_id is not None:
            _non_nil_uuid(self.output_artifact_id, "output artifact identifier")
        if type(self.is_new) is not bool:
            raise TypeError("step is_new must be a boolean")

        if self.status is ExecutionStatus.RUNNING:
            if self.completed_at is not None:
                raise ValueError("running step cannot have a completion timestamp")
            if self.outcome is not None:
                raise ValueError("running step cannot have an outcome")
            if self.output_artifact_id is not None:
                raise ValueError("running step cannot have an output artifact identifier")
        elif self.status in _TERMINAL_STATUSES:
            if self.is_new:
                raise ValueError("a newly admitted step must be running")
            if self.completed_at is None:
                raise ValueError("terminal step requires a completion timestamp")
            if self.completed_at < self.started_at:
                raise ValueError("completion timestamp cannot precede start timestamp")
        else:
            raise ValueError(f"unsupported step status: {self.status!r}")

    @property
    def id(self) -> UUID:
        """Alias for the step identifier matching Step.id."""
        return self.step_id

    @property
    def is_terminal(self) -> bool:
        """Return True when the step has completed in a terminal state."""
        return self.status in _TERMINAL_STATUSES


@runtime_checkable
class ControllerStepRepository(Protocol):
    """Persistence operations for controller steps that never own the caller's transaction."""

    async def admit(
        self,
        run_id: UUID,
        step_id: UUID,
        kind: str,
        attempt: int,
        *,
        started_at: datetime | None = None,
    ) -> ControllerStepRecord: ...

    async def get(
        self,
        run_id: UUID,
        step_id: UUID,
    ) -> ControllerStepRecord | None: ...

    async def next_attempt(
        self,
        run_id: UUID,
        kind: str,
    ) -> int: ...

    async def finalize(
        self,
        run_id: UUID,
        step_id: UUID,
        status: ExecutionStatus,
        *,
        output_artifact_id: UUID | None = None,
        outcome: str | None = None,
        completed_at: datetime | None = None,
    ) -> ControllerStepRecord: ...


__all__ = [
    "VALID_CONTROLLER_STEP_KINDS",
    "ControllerStepRecord",
    "ControllerStepRepository",
    "ControllerStepUnsettledError",
    "ExecutionStatus",
]
