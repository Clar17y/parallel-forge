"""Startup releases admission only after successful, still-owned recovery."""

import asyncio
from uuid import uuid4

import pytest
from forge.persistence.repositories.recovery import RecoveryBarrierLost, RecoveryLease
from forge.worker.startup import run_startup_recovery


class Barrier:
    def __init__(self):
        self.calls = []
        self.lose = False

    async def acquire(self, **kwargs):
        self.calls.append("acquire")
        return RecoveryLease(uuid4(), 1)

    async def renew(self, lease, **kwargs):
        self.calls.append("renew")
        if self.lose:
            raise RecoveryBarrierLost("lost")
        return lease

    async def finish(self, lease):
        self.calls.append("finish")

    async def abandon(self, lease):
        self.calls.append("abandon")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_startup_opens_only_after_successful_recovery(failure):
    barrier = Barrier()

    async def recover():
        barrier.calls.append("recover")
        if failure:
            raise RuntimeError("recovery failed")

    if failure:
        with pytest.raises(RuntimeError, match="recovery failed"):
            await run_startup_recovery(barrier, recover, asyncio.Event())
    else:
        assert await run_startup_recovery(barrier, recover, asyncio.Event())
    assert barrier.calls == ["acquire", "recover", "abandon" if failure else "finish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["lease", "stop", "cancel"])
async def test_interrupted_startup_drains_before_abandonment(trigger):
    barrier = Barrier()
    barrier.lose = trigger == "lease"
    cancelled, release, started, stop = (asyncio.Event() for _ in range(4))

    async def recover():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            barrier.calls.append("drained")
            raise

    running = asyncio.create_task(
        run_startup_recovery(barrier, recover, stop, lease_seconds=1)
    )
    await asyncio.wait_for(started.wait(), 1)
    if trigger == "stop":
        stop.set()
    elif trigger == "cancel":
        running.cancel()
    await asyncio.wait_for(cancelled.wait(), 2)
    assert not running.done() and "abandon" not in barrier.calls and "finish" not in barrier.calls
    release.set()
    if trigger == "stop":
        assert await running is False
    else:
        with pytest.raises(RecoveryBarrierLost if trigger == "lease" else asyncio.CancelledError):
            await running
    assert barrier.calls[-2:] == ["drained", "abandon"]
