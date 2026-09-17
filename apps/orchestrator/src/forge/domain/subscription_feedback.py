"""Bounded operator feedback and credential-free delivery receipts."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.payload import contains_credential

MAX_FEEDBACK_BYTES = 4096
type TaskFeedbackStatus = Literal["pending_primary", "forwarded", "delivered", "closed"]


class TaskFeedbackConflict(RuntimeError):
    """Feedback lacks current operator, task, or delivery authority."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SubscriptionTaskFeedbackRequest(_ClosedModel):
    expected_run_version: int = Field(ge=0)
    expected_task_version: int = Field(ge=0)
    feedback: str = Field(min_length=1, max_length=MAX_FEEDBACK_BYTES)

    @field_validator("feedback")
    @classmethod
    def safe_bounded_feedback(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("task feedback must be nonblank text")
        if len(value.encode("utf-8")) > MAX_FEEDBACK_BYTES:
            raise ValueError("task feedback exceeds its byte bound")
        if contains_credential(value):
            raise ValueError("task feedback contains a raw credential")
        return value


class TaskFeedbackReceipt(_ClosedModel):
    receipt_id: UUID
    operator_id: UUID
    run_id: UUID
    primary_task_id: UUID
    task_id: UUID
    status: TaskFeedbackStatus
    run_version: int = Field(ge=0)
    task_version: int = Field(ge=0)
    primary_task_version: int = Field(ge=0)
    feedback_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    feedback_bytes: int = Field(ge=1, le=MAX_FEEDBACK_BYTES)
    observed_at: datetime

    @model_validator(mode="after")
    def consistent_receipt(self) -> Self:
        identities = (
            self.receipt_id,
            self.operator_id,
            self.run_id,
            self.primary_task_id,
            self.task_id,
        )
        if any(identity.int == 0 for identity in identities):
            raise ValueError("task feedback receipt identity is invalid")
        if self.primary_task_id == self.task_id:
            raise ValueError("task feedback target must be a worker")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("task feedback clock must be timezone-aware")
        return self


class StoredTaskFeedback(_ClosedModel):
    receipt: TaskFeedbackReceipt


__all__ = [
    "MAX_FEEDBACK_BYTES",
    "StoredTaskFeedback",
    "SubscriptionTaskFeedbackRequest",
    "TaskFeedbackConflict",
    "TaskFeedbackReceipt",
    "TaskFeedbackStatus",
]
