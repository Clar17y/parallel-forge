"""Small, immutable contracts for durable subscription task scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from forge.domain.lease import validate_lease_seconds
from forge.domain.paths import policy_path_key


class TaskScheduleState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    BLOCKED = "blocked"
    RECONCILING = "reconciling"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True, kw_only=True)
class SchedulerCapacityPolicy:
    """Operator-owned execution ceilings, versioned independently from task authority."""

    version: int
    global_limit: int = 3
    run_limit: int = 3
    provider_limit: int = 3

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError("capacity policy version must be an integer >= 1")
        for value in (self.global_limit, self.run_limit, self.provider_limit):
            if type(value) is not int or value < 1:
                raise ValueError("capacity limits must be integers >= 1")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleTask:
    run_id: UUID
    task_id: UUID
    worktree_id: str
    owned_paths: tuple[str, ...] = ()
    parent_task_id: UUID | None = None
    dependency_task_ids: tuple[UUID, ...] = ()
    read_only: bool = False
    max_repairs: int = 0

    def __post_init__(self) -> None:
        if self.run_id.int == 0 or self.task_id.int == 0:
            raise ValueError("run_id and task_id must not be nil")
        if not self.worktree_id.strip():
            raise ValueError("worktree_id must not be blank")
        if self.max_repairs < 0:
            raise ValueError("max_repairs must be non-negative")
        object.__setattr__(self, "owned_paths", tuple(policy_path_key(x) for x in self.owned_paths))
        object.__setattr__(self, "dependency_task_ids", tuple(self.dependency_task_ids))


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLease:
    run_id: UUID
    task_id: UUID
    owner: str
    generation: int
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.owner.strip() or self.generation < 1:
            raise ValueError("lease owner and generation are required")


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskEffectLease:
    """Fence for one external provider effect, distinct from a task lease."""

    effect_id: UUID
    task_lease: TaskLease
    candidate_epoch: int

    def __post_init__(self) -> None:
        if self.effect_id.int == 0 or self.candidate_epoch < 0:
            raise ValueError("effect identity and candidate epoch are required")


def validate_lease_duration(seconds: float) -> None:
    validate_lease_seconds(seconds)
