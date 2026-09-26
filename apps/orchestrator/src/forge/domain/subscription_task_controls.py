"""Closed operator task controls and credential-free causal receipts."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.run import RunState

type TaskControlAction = Literal["pause", "cancel", "resume"]
type TaskControlStatus = Literal[
    "pause_requested",
    "cancel_requested",
    "paused",
    "cancelled",
    "queued",
    "blocked",
    "decision_pending",
]


class TaskControlConflict(RuntimeError):
    """A task control lacks current or recoverable authority."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubscriptionTaskControlRequest(_ClosedModel):
    action: TaskControlAction
    expected_run_version: int = Field(ge=0)
    expected_task_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=512)
    pause_receipt_id: UUID | None = None

    @field_validator("reason")
    @classmethod
    def bounded_reason(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 512:
            raise ValueError("task control reason must be nonblank and bounded")
        return value

    @model_validator(mode="after")
    def causal_pause(self) -> Self:
        if (self.action == "resume") != (self.pause_receipt_id is not None):
            raise ValueError("only resume requires a pause receipt")
        if self.pause_receipt_id is not None and self.pause_receipt_id.int == 0:
            raise ValueError("pause receipt identity is invalid")
        return self


class TaskControlReceipt(_ClosedModel):
    receipt_id: UUID
    run_id: UUID
    task_id: UUID
    action: TaskControlAction
    status: TaskControlStatus
    run_version: int = Field(ge=0)
    task_version: int = Field(ge=1)
    observed_at: datetime
    reason: str = Field(min_length=1, max_length=512)
    pause_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def consistent_receipt(self) -> Self:
        if (
            self.status
            not in {
                "pause": {"pause_requested", "paused"},
                "cancel": {"cancel_requested", "cancelled"},
                "resume": {"queued", "blocked", "decision_pending"},
            }[self.action]
        ):
            raise ValueError("task control outcome differs")
        if (self.action == "resume") != (self.pause_receipt_id is not None):
            raise ValueError("task control pause binding differs")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("task control clock must be timezone-aware")
        if any(identity.int == 0 for identity in (self.receipt_id, self.run_id, self.task_id)):
            raise ValueError("task control receipt identity is invalid")
        return self


class TaskControlProof(_ClosedModel):
    task_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scheduling_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_state: RunState
    candidate_epoch: int = Field(ge=0)
    candidate_state: Literal["open", "draining", "closed"]


class TaskControlSource(_ClosedModel):
    kind: Literal["active", "pending", "stale"]
    attempt_id: UUID
    admission_version: int = Field(ge=1)
    task_version: int = Field(ge=1)
    lease_owner: str = Field(min_length=1, max_length=255)
    lease_generation: int = Field(ge=1)
    result_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    previous_stop_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def result_binding(self) -> Self:
        if (self.kind == "active") != (self.result_digest is None):
            raise ValueError("stopped task source result binding differs")
        if self.attempt_id.int == 0 or self.task_version < self.admission_version:
            raise ValueError("stopped task source identity differs")
        if (
            self.kind == "active"
            and self.previous_stop_receipt_id is None
            and self.task_version != self.admission_version
        ) or (self.kind != "active" and self.task_version <= self.admission_version):
            raise ValueError("stopped task source version differs")
        if self.previous_stop_receipt_id is not None and self.previous_stop_receipt_id.int == 0:
            raise ValueError("previous task stop identity differs")
        return self


class AttemptTaskControlProof(TaskControlProof):
    source: TaskControlSource


class IdleTaskControlProof(TaskControlProof):
    """A settled task keeps its exact scheduling meaning and retained history."""

    idle_state: Literal["queued", "blocked"]
    source_attempt_id: UUID
    history_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_attempt_id")
    @classmethod
    def nonzero_attempt(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("idle task source identity is invalid")
        return value


class StoredTaskControl(_ClosedModel):
    receipt: TaskControlReceipt
    # Keep the original queued receipt serialization valid across upgrades.
    proof: AttemptTaskControlProof | IdleTaskControlProof | TaskControlProof
