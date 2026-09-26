"""Operator-approved defaults for subscription scheduling."""

from forge.domain.scheduling import SchedulerCapacityPolicy


def test_default_capacity_allows_three_workers_per_run() -> None:
    policy = SchedulerCapacityPolicy(version=1)
    assert (policy.run_limit, policy.global_limit, policy.provider_limit) == (3, 3, 3)
