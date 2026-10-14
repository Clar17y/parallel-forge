"""Apply the operator's scheduler capacity setting as a new policy version."""

from __future__ import annotations

from typing import Protocol

from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.settings import SubscriptionCapacityLimits


class CapacityPolicyStore(Protocol):
    async def current_capacity_policy(self) -> SchedulerCapacityPolicy: ...

    async def configure_capacity(self, policy: SchedulerCapacityPolicy) -> None: ...


async def apply_capacity_setting(
    store: CapacityPolicyStore, limits: SubscriptionCapacityLimits | None
) -> SchedulerCapacityPolicy | None:
    """Version the configured limits when they differ; omission keeps the stored policy."""
    if limits is None:
        return None
    current = await store.current_capacity_policy()
    wanted = (limits.global_limit, limits.run_limit, limits.provider_limit)
    if (current.global_limit, current.run_limit, current.provider_limit) == wanted:
        return current
    policy = SchedulerCapacityPolicy(
        version=current.version + 1,
        global_limit=limits.global_limit,
        run_limit=limits.run_limit,
        provider_limit=limits.provider_limit,
    )
    await store.configure_capacity(policy)
    return policy


__all__ = ["CapacityPolicyStore", "apply_capacity_setting"]
