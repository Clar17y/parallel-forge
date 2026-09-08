"""Controller-only publication of an exact, previously approved candidate."""

from typing import Protocol

from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.policy import ProjectPolicy


class ManagedPushPort(Protocol):
    async def push(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, approved_sha: str
    ) -> None: ...
