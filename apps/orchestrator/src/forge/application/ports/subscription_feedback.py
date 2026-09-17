"""Persistence contract for durable worker-specific operator feedback."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.application.ports.subscription_execution import SubscriptionAdmission
from forge.domain.subscription_feedback import (
    StoredTaskFeedback,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackStatus,
)


@dataclass(frozen=True, slots=True)
class FeedbackTransition:
    primary_task_id: UUID
    status: TaskFeedbackStatus
    run_version: int
    task_version: int
    primary_task_version: int
    feedback_digest: str
    binding_digest: str
    feedback_bytes: int


@dataclass(frozen=True, slots=True)
class FeedbackInvocationContext:
    pending_primary: dict[str, object] | None = None
    worker_feedback: tuple[dict[str, object], ...] = ()


class SubscriptionFeedbackRepository(Protocol):
    async def verify_receipt(
        self,
        stored: StoredTaskFeedback,
        *,
        actor_id: UUID,
        request_digest: str,
    ) -> None: ...

    async def submit(
        self,
        run_id: UUID,
        task_id: UUID,
        request: SubscriptionTaskFeedbackRequest,
        *,
        actor_id: UUID,
        request_digest: str,
        receipt_id: UUID,
    ) -> FeedbackTransition: ...

    async def invocation_context(
        self, admission: SubscriptionAdmission
    ) -> FeedbackInvocationContext: ...

    async def close_cancelled(self, run_id: UUID, task_id: UUID) -> int: ...

    async def close_run_cancelled(self, run_id: UUID) -> int: ...

    async def close_exhausted(self, run_id: UUID) -> int: ...


__all__ = [
    "FeedbackInvocationContext",
    "FeedbackTransition",
    "SubscriptionFeedbackRepository",
]
