"""Scheduler orchestration that deliberately keeps provider work outside a UoW."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from forge.application.ports.scheduling import SchedulingRepository
from forge.domain.scheduling import TaskLease


class SubscriptionScheduler:
    """Claims in one short transaction; callers execute effects after it returns."""

    def __init__(self, repository: SchedulingRepository, *, lease_for: timedelta) -> None:
        self._repository, self._lease_for = repository, lease_for

    async def claim(self, owner: str) -> TaskLease | None:
        return await self._repository.claim_ready(owner, self._lease_for)

    async def settle(
        self, lease: TaskLease, execute: Callable[[TaskLease], Awaitable[bool]]
    ) -> None:
        """Provider callback receives only a lease, never an open repository transaction."""
        try:
            successful = await execute(lease)
        except BaseException:
            await self._repository.finish(lease, successful=False)
            raise
        await self._repository.finish(lease, successful=successful)
