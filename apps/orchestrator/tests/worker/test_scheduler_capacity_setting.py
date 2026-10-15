from __future__ import annotations

import pytest
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.settings import Settings
from forge.worker.scheduler_capacity import apply_capacity_setting


class Repository:
    def __init__(self, current: SchedulerCapacityPolicy) -> None:
        self.current = current
        self.configured: list[SchedulerCapacityPolicy] = []
        self.calls: list[str] = []

    async def lock_capacity_policy(self) -> None:
        self.calls.append("lock")

    async def current_capacity_policy(self) -> SchedulerCapacityPolicy:
        self.calls.append("read")
        return self.current

    async def configure_capacity(self, policy: SchedulerCapacityPolicy) -> None:
        self.configured.append(policy)
        self.current = policy


@pytest.mark.asyncio
async def test_owner_capacity_setting_creates_the_next_policy_version_once() -> None:
    """Capacity was fixed at 3/3/3 with no owner-facing control."""
    repo = Repository(SchedulerCapacityPolicy(version=1))
    limits = Settings(
        process_role="worker",
        subscription_capacity={"global_limit": 12, "run_limit": 6, "provider_limit": 8},
    ).subscription_capacity
    assert limits is not None

    applied = await apply_capacity_setting(repo, limits)
    assert applied == SchedulerCapacityPolicy(
        version=2, global_limit=12, run_limit=6, provider_limit=8
    )
    assert await apply_capacity_setting(repo, limits) == applied
    assert len(repo.configured) == 1


@pytest.mark.asyncio
async def test_without_a_capacity_setting_the_stored_policy_is_untouched() -> None:
    repo = Repository(SchedulerCapacityPolicy(version=4, global_limit=5))
    assert await apply_capacity_setting(repo, None) is None
    assert repo.configured == []


def test_capacity_setting_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError):
        Settings(
            process_role="worker",
            subscription_capacity={"global_limit": 0, "run_limit": 1, "provider_limit": 1},
        )


@pytest.mark.asyncio
async def test_capacity_setting_locks_before_reading_the_current_version() -> None:
    """Two workers starting together must not both allocate the same next version."""
    repo = Repository(SchedulerCapacityPolicy(version=1))
    limits = Settings(
        process_role="worker",
        subscription_capacity={"global_limit": 12, "run_limit": 6, "provider_limit": 8},
    ).subscription_capacity
    await apply_capacity_setting(repo, limits)
    assert repo.calls[:2] == ["lock", "read"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_worker_starts_create_one_capacity_version(session_factory) -> None:
    import asyncio

    from forge.persistence.models.scheduling import SubscriptionSchedulerCapacityPolicy
    from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
    from sqlalchemy import func, select

    limits = Settings(
        process_role="worker",
        subscription_capacity={"global_limit": 21, "run_limit": 7, "provider_limit": 9},
    ).subscription_capacity
    async with session_factory() as session:
        before = await session.scalar(
            select(func.count()).select_from(SubscriptionSchedulerCapacityPolicy)
        )

    async def start_worker() -> SchedulerCapacityPolicy | None:
        async with session_factory() as session:
            applied = await apply_capacity_setting(PostgresSchedulingRepository(session), limits)
            await asyncio.sleep(0.05)  # widen the window between read and commit
            await session.commit()
            return applied

    first, second = await asyncio.gather(start_worker(), start_worker())
    assert first == second
    async with session_factory() as session:
        after = await session.scalar(
            select(func.count()).select_from(SubscriptionSchedulerCapacityPolicy)
        )
    assert after == before + 1
