"""Keep global command admission closed until owned startup recovery completes."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from uuid import uuid4

from forge.domain.lease import validate_lease_seconds
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier


async def run_startup_recovery(
    barrier: PostgresRecoveryBarrier,
    recover: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    *,
    lease_seconds: float = 30,
) -> bool:
    validate_lease_seconds(lease_seconds)
    owner = uuid4()
    lease = None
    while not stop.is_set():
        lease = await barrier.acquire(owner_id=owner, lease_seconds=lease_seconds)
        if lease is not None:
            break
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 0.25)
    if lease is None:
        return False

    async def renew() -> None:
        while True:
            await asyncio.sleep(lease_seconds / 3)
            await barrier.renew(lease, lease_seconds=lease_seconds)

    action = asyncio.ensure_future(recover())
    renewal = asyncio.create_task(renew())
    stopped = asyncio.create_task(stop.wait())
    finished = False
    try:
        done, _ = await asyncio.wait(
            (action, renewal, stopped), return_when=asyncio.FIRST_COMPLETED
        )
        if renewal in done:
            await renewal
        if stopped in done or stop.is_set():
            return False
        await action
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
        await barrier.finish(lease)
        finished = True
        return True
    finally:
        # Retained recovery work must stop before abandoning ownership. Expiry
        # alone never reopens dispatch, and a successor generation is fenced.
        for task in (action, renewal, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(action, renewal, stopped, return_exceptions=True)
        if not finished:
            await barrier.abandon(lease)
