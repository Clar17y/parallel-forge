"""Durable attempt reservations and immutable usage projections."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.domain.subscription import AttemptTelemetry, TaskBudget
from forge.domain.subscription_budget import AttemptCharge, UsageAmounts


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptUsageReceipt:
    attempt_id: UUID
    telemetry: AttemptTelemetry | None
    charge: AttemptCharge


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionUsage:
    consumed: UsageAmounts
    outstanding: UsageAmounts
    uncertain_attempts: int
    policy_violations: tuple[str, ...]


class SubscriptionBudgetRepository(Protocol):
    async def fit_reservation(
        self, run_id: UUID, task_id: UUID, ceiling: TaskBudget
    ) -> TaskBudget | None: ...
    async def reserved_budget(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID
    ) -> TaskBudget: ...
    async def try_debit_repair(self, run_id: UUID, task_id: UUID, attempt_id: UUID) -> bool: ...
    async def reserve_attempt(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        reservation: TaskBudget,
        *,
        idempotency_key: str,
    ) -> None: ...
    async def settle_attempt(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, telemetry: AttemptTelemetry | None
    ) -> AttemptUsageReceipt: ...
    async def usage(self, run_id: UUID, task_id: UUID | None = None) -> SubscriptionUsage: ...
