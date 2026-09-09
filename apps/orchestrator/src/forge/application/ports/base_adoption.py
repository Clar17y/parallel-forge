"""Trusted release-only fetch/adoption and read-only reconciliation."""

from typing import Protocol

from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.policy import ProjectPolicy


class BaseAdoptionPort(Protocol):
    async def adopt(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None: ...

    async def inspect(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None: ...
