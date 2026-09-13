"""Durable scheduler boundary."""

from datetime import timedelta
from typing import Protocol
from uuid import UUID

from forge.domain.scheduling import (
    SchedulerCapacityPolicy,
    ScheduleTask,
    TaskEffectLease,
    TaskLease,
)
from forge.domain.subscription import RouteSpec, TaskBudget


class SchedulingConflict(ValueError):
    """Scheduler state does not authorize the requested transition."""


class SchedulingLeaseRevoked(SchedulingConflict):
    """A current run/task stop has revoked this lease."""


class SchedulingLeaseLost(SchedulingConflict):
    """The caller can no longer prove ownership of a live lease."""


class SchedulingRepository(Protocol):
    async def enqueue(self, task: ScheduleTask) -> ScheduleTask: ...
    async def admit_run(self, run_id: UUID) -> None: ...
    async def configure_capacity(self, policy: SchedulerCapacityPolicy) -> None: ...
    async def claim_ready(self, owner: str, lease_for: timedelta) -> TaskLease | None: ...

    async def claim_execution_ready(
        self,
        owner: str,
        lease_for: timedelta,
        *,
        eligible_routes: frozenset[RouteSpec] | None = None,
        reservation_ceiling: TaskBudget | None = None,
    ) -> TaskLease | None: ...
    async def renew(self, lease: TaskLease, lease_for: timedelta) -> TaskLease: ...
    async def finish(
        self, lease: TaskLease, *, successful: bool, allow_repair: bool = True
    ) -> None: ...
    async def admit_effect(
        self,
        lease: TaskLease,
        effect_id: UUID,
        *,
        owned_paths: tuple[str, ...] = (),
        whole_worktree_exclusive: bool = False,
        expected_candidate_epoch: int | None = None,
    ) -> TaskEffectLease: ...
    async def settle_effect(self, effect: TaskEffectLease, *, accepted: bool) -> bool: ...
    async def reconcile_effect(self, effect: TaskEffectLease) -> None: ...
    async def request_stop(self, run_id: UUID, task_id: UUID, *, cancel: bool) -> None: ...
    async def yield_to_children(
        self, lease: TaskLease, children: tuple[ScheduleTask, ...]
    ) -> None: ...
    async def reconcile_expired(self, run_id: UUID, task_id: UUID, *, retry: bool) -> None: ...
    async def begin_candidate(self, run_id: UUID) -> int: ...
    async def close_candidate(self, run_id: UUID, epoch: int) -> None: ...
