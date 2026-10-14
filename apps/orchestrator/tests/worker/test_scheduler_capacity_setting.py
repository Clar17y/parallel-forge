from __future__ import annotations

import pytest
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.settings import Settings
from forge.worker.scheduler_capacity import apply_capacity_setting


class Repository:
    def __init__(self, current: SchedulerCapacityPolicy) -> None:
        self.current = current
        self.configured: list[SchedulerCapacityPolicy] = []

    async def current_capacity_policy(self) -> SchedulerCapacityPolicy:
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
