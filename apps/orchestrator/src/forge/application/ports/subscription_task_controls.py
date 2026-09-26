"""Atomic backend transition for an already authenticated operator task control."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.domain.subscription_task_controls import (
    AttemptTaskControlProof,
    StoredTaskControl,
    SubscriptionTaskControlRequest,
    TaskControlProof,
    TaskControlStatus,
)


@dataclass(frozen=True, slots=True)
class TaskControlTransition:
    status: TaskControlStatus
    run_version: int
    task_version: int
    proof: AttemptTaskControlProof | TaskControlProof


class SubscriptionTaskControlRepository(Protocol):
    async def verify_receipt(
        self,
        stored: StoredTaskControl,
        *,
        actor_id: UUID,
        request_digest: str,
    ) -> None: ...

    async def apply(
        self,
        run_id: UUID,
        task_id: UUID,
        request: SubscriptionTaskControlRequest,
        *,
        pause: StoredTaskControl | None,
        receipt_id: UUID,
    ) -> TaskControlTransition: ...

    async def pending_stops(
        self, after_id: UUID | None, limit: int
    ) -> tuple[tuple[UUID, UUID], ...]: ...

    async def reconcile_stop(self, run_id: UUID, receipt_id: UUID) -> bool | None: ...
