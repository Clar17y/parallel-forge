"""Durable, evidence-bound subscription plan approval proposal boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.domain.approval import SubscriptionPlanApprovalEvidence, SubscriptionPlanProducer
from forge.domain.plan import PlanOutput


class SubscriptionPlanGateError(RuntimeError):
    """A proposal is stale, forged, or conflicts with its immutable predecessor."""


@dataclass(frozen=True, slots=True)
class SubscriptionPlanGateRecord:
    attempt_id: UUID
    run_id: UUID
    task_id: UUID
    plan_digest: str
    evidence_digest: str
    result_digest: str
    envelope_digest: str
    budget_digest: str
    route_digest: str


@dataclass(frozen=True, slots=True)
class SettledSubscriptionPlan:
    """Publication inputs, still requiring locked authority verification."""

    plan: PlanOutput
    producer: SubscriptionPlanProducer
    result_digest: str


class SubscriptionPlanGateRepository(Protocol):
    async def rejection(self, attempt_id: UUID) -> SubscriptionSettlement | None: ...
    async def reject(
        self, evidence: SubscriptionPlanApprovalEvidence
    ) -> SubscriptionSettlement: ...
    async def proposal(self, attempt_id: UUID) -> SettledSubscriptionPlan: ...
    async def mark_applied(self, evidence: SubscriptionPlanApprovalEvidence) -> None: ...
    async def resume_prepared(
        self, evidence: SubscriptionPlanApprovalEvidence, *, worktree_id: str, approval_id: UUID
    ) -> UUID: ...

    async def record(
        self, evidence: SubscriptionPlanApprovalEvidence
    ) -> SubscriptionPlanGateRecord:
        """Lock and persist one immutable proposal, returning an equal replay only."""

    async def verify(
        self, evidence: SubscriptionPlanApprovalEvidence, *, historical: bool = False
    ) -> SubscriptionPlanGateRecord: ...

    async def get(self, attempt_id: UUID) -> SubscriptionPlanGateRecord | None: ...


__all__ = [
    "SubscriptionPlanGateError",
    "SubscriptionPlanGateRecord",
    "SubscriptionPlanGateRepository",
]
