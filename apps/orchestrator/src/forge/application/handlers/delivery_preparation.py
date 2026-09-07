"""Worker adapter for worktree preparation."""

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.domain.command import CommandEnvelope


class DeliveryPreparationHandler:
    def __init__(self, service: DeliveryPreparationService) -> None:
        self._service = service

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await self._service.execute(command, work)


__all__ = ["DeliveryPreparationHandler"]
