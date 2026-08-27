"""The worker-facing planning command handler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.planning import PlanningOutcome, PlanningService
from forge.domain.command import CommandEnvelope


class PlanningHandler:
    """Adapt the explicit two-argument worker handler contract to the service."""

    def __init__(self, service: PlanningService) -> None:
        self._service = service

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> PlanningOutcome:
        return await self._service.execute(command, work)


PlanningCommandHandler = Callable[[CommandEnvelope, UnitOfWork], Awaitable[PlanningOutcome]]

__all__ = ["PlanningCommandHandler", "PlanningHandler"]
