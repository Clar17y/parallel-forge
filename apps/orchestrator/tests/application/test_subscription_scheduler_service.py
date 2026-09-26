from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.scheduling import SubscriptionScheduler
from forge.domain.scheduling import TaskLease


@pytest.mark.asyncio
async def test_provider_callback_runs_after_claim_and_settles_its_lease() -> None:
    calls: list[str] = []
    lease = TaskLease(
        run_id=uuid4(),
        task_id=uuid4(),
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC),
    )

    class Repo:
        async def claim_ready(self, owner, lease_for):
            calls.append("claim")
            return lease

        async def finish(self, value, *, successful):
            calls.append(f"finish:{successful}")

    scheduler = SubscriptionScheduler(Repo(), lease_for=timedelta(seconds=1))

    async def provider(value):
        calls.append("provider")
        return True

    claimed = await scheduler.claim("worker")
    assert claimed == lease
    await scheduler.settle(lease, provider)
    assert calls == ["claim", "provider", "finish:True"]
