"""The worker-facing planning command handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.planning import PlanningOutcome, PlanningService
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.domain.command import CommandEnvelope


class PlanningHandler:
    """Adapt the explicit two-argument worker handler contract to the service."""

    def __init__(
        self,
        service: PlanningService,
        *,
        subscription_service: SubscriptionPlanningService | None = None,
    ) -> None:
        self._service = service
        self._subscription = subscription_service

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> PlanningOutcome | UUID:
        if self._subscription is not None:
            envelope = await work.subscription.envelope_for_run(command.run_id)
            if envelope is not None:
                return await self._subscription.execute(command, work)
        return await self._service.execute(command, work)


PlanningCommandHandler = Callable[[CommandEnvelope, UnitOfWork], Awaitable[PlanningOutcome | UUID]]

__all__ = ["PlanningCommandHandler", "PlanningHandler"]
