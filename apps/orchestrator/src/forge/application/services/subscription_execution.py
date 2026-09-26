"""Atomic subscription attempt admission; provider invocation is outside the UoW."""

from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

from forge.application.ports.subscription_execution import (
    SubscriptionAdmission,
    SubscriptionSettlement,
)
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.ports.unit_of_work import UnitOfWork
from forge.domain.subscription import RouteSpec, TaskBudget


class SubscriptionDecisionExecutor:
    def __init__(self, work_factory: Callable[[], UnitOfWork]) -> None:
        self._work_factory = work_factory

    async def admit_next(
        self,
        owner: str,
        reservation: TaskBudget,
        *,
        lease_for: timedelta = timedelta(seconds=30),
        eligible_routes: frozenset[RouteSpec] | None = None,
    ) -> SubscriptionAdmission | None:
        if eligible_routes == frozenset():
            return None
        async with self._work_factory() as work:
            lease = await work.scheduler.claim_execution_ready(
                owner, lease_for, eligible_routes=eligible_routes, reservation_ceiling=reservation
            )
            if lease is None:
                await work.commit()
                return None
            fitted = await work.subscription_budget.fit_reservation(
                lease.run_id, lease.task_id, reservation
            )
            if fitted is None:
                raise ValueError("attempt budget changed within atomic admission")
            admission = await work.subscription_execution.admit(lease, uuid4())
            await work.subscription_budget.reserve_attempt(
                lease.run_id,
                lease.task_id,
                admission.attempt.attempt_id,
                fitted,
                idempotency_key=f"attempt:{admission.attempt.attempt_id}",
            )
            await work.commit()
            return admission

    async def settle(
        self, admission: SubscriptionAdmission, result: SubscriptionInvocationResult
    ) -> SubscriptionSettlement:
        async with self._work_factory() as work:
            settlement = await work.subscription_execution.settle(admission, result)
            await work.commit()
            return settlement
