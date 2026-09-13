from datetime import timedelta

import pytest
from forge.application.services.scheduling import SubscriptionScheduler
from forge.domain.scheduling import TaskLease
from forge.worker.subscription import DurableSubscriptionTaskWorker, SubscriptionTaskWorker


@pytest.mark.asyncio
async def test_worker_does_not_call_gateway_when_no_task_is_ready() -> None:
    class Repo:
        async def claim_ready(self, owner, lease_for):
            return None

    async def gateway(lease):
        raise AssertionError("gateway called without task")

    worker = SubscriptionTaskWorker(
        SubscriptionScheduler(Repo(), lease_for=timedelta(seconds=1)), gateway, owner="fake"
    )
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_durable_worker_closes_claim_uow_before_gateway_and_settles_in_new_uow() -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime
    from uuid import uuid4

    lease = TaskLease(
        run_id=uuid4(),
        task_id=uuid4(),
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC),
    )
    events: list[str] = []

    class Scheduler:
        async def claim_ready(self, owner, lease_for):
            events.append("claim")
            return lease

        async def finish(self, value, *, successful):
            assert value == lease
            events.append(f"finish:{successful}")

    class Work:
        scheduler = Scheduler()

        async def commit(self):
            events.append("commit")

    @asynccontextmanager
    async def factory():
        events.append("enter")
        yield Work()
        events.append("exit")

    async def gateway(value):
        assert value == lease
        assert events == ["enter", "claim", "commit", "exit"]
        events.append("gateway")
        return True

    worker = DurableSubscriptionTaskWorker(factory, gateway, owner="worker")
    assert await worker.run_once() is True
    assert events == [
        "enter",
        "claim",
        "commit",
        "exit",
        "gateway",
        "enter",
        "finish:True",
        "commit",
        "exit",
    ]
