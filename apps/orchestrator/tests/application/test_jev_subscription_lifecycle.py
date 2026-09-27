"""Review preparation keeps its scheduler authority throughout slow scoring."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from forge.application.ports.scheduling import SchedulingLeaseRevoked
from forge.worker.subscription_invocation import SubscriptionInvocationWorker


class _Work:
    def __init__(self, renew):
        self.scheduler = SimpleNamespace(renew=renew)
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


@pytest.mark.asyncio
async def test_slow_focus_preparation_renews_lease_until_complete():
    release = asyncio.Event()
    started = asyncio.Event()
    renew = AsyncMock()

    async def build(_):
        started.set()
        await release.wait()
        return "prepared"

    worker = SubscriptionInvocationWorker.__new__(SubscriptionInvocationWorker)
    worker._requests = SimpleNamespace(build=build)
    worker._work_factory = lambda: _Work(renew)
    admission = SimpleNamespace(lease=object())
    pending = asyncio.create_task(
        worker._build_with_renewal(admission, asyncio.Event(), heartbeat_seconds=0.01)
    )
    await started.wait()
    for _ in range(100):
        if renew.await_count >= 2:
            break
        await asyncio.sleep(0.01)
    assert renew.await_count >= 2
    release.set()
    assert await pending == "prepared"


@pytest.mark.asyncio
async def test_revoked_lease_cancels_slow_focus_preparation():
    cancelled = asyncio.Event()
    renew = AsyncMock(side_effect=SchedulingLeaseRevoked("revoked"))

    async def build(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = SubscriptionInvocationWorker.__new__(SubscriptionInvocationWorker)
    worker._requests = SimpleNamespace(build=build)
    worker._work_factory = lambda: _Work(renew)
    admission = SimpleNamespace(lease=object())
    with pytest.raises(SchedulingLeaseRevoked):
        await worker._build_with_renewal(admission, asyncio.Event(), heartbeat_seconds=0.01)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_stop_cancels_slow_focus_before_gateway_creation():
    started = asyncio.Event()
    cancelled = asyncio.Event()
    renew = AsyncMock()

    async def build(_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = SubscriptionInvocationWorker.__new__(SubscriptionInvocationWorker)
    worker._requests = SimpleNamespace(build=build)
    worker._work_factory = lambda: _Work(renew)
    stop = asyncio.Event()
    pending = asyncio.create_task(
        worker._build_with_renewal(
            SimpleNamespace(lease=object()), stop, heartbeat_seconds=0.01
        )
    )
    await started.wait()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cancelled.is_set()
