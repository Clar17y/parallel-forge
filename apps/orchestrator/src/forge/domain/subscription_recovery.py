"""Closed operator recovery requests and safe projections."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RecoveryAction(StrEnum):
    RETRY_APPLICATION = "retry_application"
    REJECT_AND_RETRY_STEP = "reject_and_retry_step"
    REPAIR_APPROVED_PLAN_CONTRACT = "repair_approved_plan_contract"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    binding: str
    eligible: bool
    reason_code: str
    message: str
    changes: tuple[str, ...]
    retained_evidence: tuple[str, ...]
    provider_attempts: int
    repair_units: int


@dataclass(frozen=True, slots=True)
class RecoveryReceiptRecord:
    receipt: RecoveryReceipt
    request_digest: str


class RecoveryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: RecoveryAction


class RecoveryBudgetImpact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_attempts: int = Field(ge=0)
    repair_units: int = Field(ge=0)


class RecoveryPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    action: RecoveryAction
    eligible: bool
    reason_code: str
    message: str
    changes: tuple[str, ...]
    retained_evidence: tuple[str, ...]
    budget_impact: RecoveryBudgetImpact
    preview_token: str
    expires_at: datetime


class RecoveryApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: RecoveryAction
    preview_token: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=1, max_length=1000)


class RecoveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt_id: UUID
    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    action: RecoveryAction
    status: str
    observed_at: datetime
    reason_code: str
