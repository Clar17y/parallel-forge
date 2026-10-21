"""Persistence boundary for one recovery preview and locked transition."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from forge.application.ports.tools import ToolCallRecord
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryReceipt,
    RecoveryReceiptRecord,
    RecoverySnapshot,
)


@dataclass(frozen=True, slots=True)
class PriorAttemptEvidence:
    """Persisted evidence for one earlier attempt of the same task."""

    attempt_number: int
    disposition: str | None
    failure: str | None
    tool_calls: tuple[ToolCallRecord, ...]
    # "current" for the current contract digest, "pre_plan" for attempts
    # crossed over the recorded approved-plan boundary.
    contract: str = "current"


class SubscriptionRecoveryRepository(Protocol):
    async def correction_feedback(
        self, task_id: UUID, contract_digest: str
    ) -> dict[str, str] | None: ...
    async def prior_attempts(
        self,
        run_id: UUID,
        task_id: UUID,
        *,
        before_attempt_number: int,
        task_digest: str,
        source_attempt_id: UUID | None = None,
    ) -> tuple[PriorAttemptEvidence, ...]: ...
    async def attempt_run_id(self, attempt_id: UUID) -> UUID: ...
    async def due(self, attempt_id: UUID) -> bool: ...
    async def due_version(self, attempt_id: UUID) -> int | None: ...
    async def role_violation(self, attempt_id: UUID) -> str | None: ...
    async def reject_role_violation(
        self, attempt_id: UUID, reason_code: str, *, observed_task_version: int | None = None
    ) -> None: ...
    async def record_failure(
        self,
        attempt_id: UUID,
        *,
        classification: str,
        reason_code: str,
        observed_task_version: int | None = None,
    ) -> None: ...
    async def schedule_pending_resume(self, attempt_id: UUID) -> None: ...
    async def record_success(self, attempt_id: UUID) -> None: ...
    async def signing_key(self) -> bytes: ...

    async def receipt_for(
        self, run_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> RecoveryReceiptRecord | None: ...

    async def preview(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        action: RecoveryAction,
        *,
        locked: bool = False,
    ) -> RecoverySnapshot: ...

    async def apply(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        action: RecoveryAction,
        *,
        actor_id: UUID,
        idempotency_key: str,
        request_digest: str,
        binding: str,
        expires_at: datetime,
        reason: str,
    ) -> RecoveryReceipt: ...
