"""Deterministic one-task fake-gateway worker loop for scheduler integration tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import cast

from forge.application.ports.scheduling import SchedulingRepository
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.scheduling import SubscriptionScheduler
from forge.domain.scheduling import TaskLease

TaskGateway = Callable[[TaskLease], Awaitable[bool]]


class SubscriptionTaskWorker:
    def __init__(
        self, scheduler: SubscriptionScheduler, gateway: TaskGateway, *, owner: str
    ) -> None:
        self._scheduler, self._gateway, self._owner = scheduler, gateway, owner

    async def run_once(self) -> bool:
        lease = await self._scheduler.claim(self._owner)
        if lease is None:
            return False
        await self._scheduler.settle(lease, self._gateway)
        return True


class DurableSubscriptionTaskWorker:
    """Opens separate UoWs around admission and settlement, never a provider wait."""

    def __init__(
        self,
        work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        gateway: TaskGateway,
        *,
        owner: str,
        lease_for: timedelta = timedelta(seconds=30),
    ) -> None:
        self._work_factory, self._gateway = work_factory, gateway
        self._owner, self._lease_for = owner, lease_for

    async def run_once(self) -> bool:
        async with self._work_factory() as work:
            lease = await work.scheduler.claim_ready(self._owner, self._lease_for)
            await work.commit()
        if lease is None:
            return False
        try:
            successful = await self._gateway(lease)
        except BaseException:
            async with self._work_factory() as work:
                await work.scheduler.finish(lease, successful=False)
                await work.commit()
            raise
        async with self._work_factory() as work:
            await work.scheduler.finish(lease, successful=successful)
            await work.commit()
        return True


def scheduler_for(repository: object, *, lease_seconds: float = 30) -> SubscriptionScheduler:
    """Tiny test-only composition helper; production worker wiring follows this slice."""
    return SubscriptionScheduler(
        cast(SchedulingRepository, repository), lease_for=timedelta(seconds=lease_seconds)
    )
