"""Capability-bound repository file publication."""

from __future__ import annotations

from forge.application.ports.repository import MAX_REPOSITORY_WRITE_BYTES, FileWrite
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.artifact import validate_artifact_digest
from forge.domain.policy import ProjectPolicy
from forge.tools.git import ControlledGit


class RepositoryWriteError(RuntimeError):
    """A controlled repository write could not be completed safely."""

    def __init__(self) -> None:
        super().__init__("repository write failed")


class WorktreeRepositoryWriter:
    """Write only through one exact retained Forge-managed worktree."""

    def __init__(
        self,
        git: ControlledGit,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> None:
        if not isinstance(git, ControlledGit):
            raise TypeError("repository writer requires ControlledGit")
        if not isinstance(worktree, ManagedWorktree):
            raise TypeError("repository writer requires a ManagedWorktree")
        if not isinstance(policy, ProjectPolicy):
            raise TypeError("repository writer requires a ProjectPolicy")
        self._git = git
        self._worktree = worktree
        self._policy = policy

    def is_bound_to(
        self,
        controlled_git: ControlledGitPort,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> bool:
        """Prove this adapter retains the exact Forge-owned authority objects."""

        return controlled_git is self._git and worktree == self._worktree and policy == self._policy

    def write_file(self, path: str, content: str) -> FileWrite:
        if not isinstance(path, str) or not isinstance(content, str) or "\x00" in content:
            raise RepositoryWriteError()
        encoded: bytes | None = None
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeError:
            pass
        if encoded is None or len(encoded) > MAX_REPOSITORY_WRITE_BYTES:
            raise RepositoryWriteError()
        failed = False
        result: tuple[str | None, str, int, str] | None = None
        try:
            with self._git.open_worktree_capability(self._worktree, self._policy) as capability:
                result = capability.write_repository_file(
                    path,
                    encoded,
                    maximum=MAX_REPOSITORY_WRITE_BYTES,
                )
        except Exception:  # noqa: BLE001 - expose one context-free safe category
            failed = True
        if failed or result is None:
            raise RepositoryWriteError()
        previous, output, byte_count, normalized = result
        return FileWrite(
            path=normalized,
            previous_digest=previous,
            output_digest=output,
            byte_count=byte_count,
            created=previous is None,
        )

    def inspect_file(self, path: str, expected_digest: str) -> FileWrite | None:
        valid_digest = True
        try:
            validate_artifact_digest(expected_digest)
        except TypeError, ValueError:
            valid_digest = False
        if not valid_digest:
            raise RepositoryWriteError()
        failed = False
        result: tuple[str, int, str] | None = None
        try:
            with self._git.open_worktree_capability(
                self._worktree,
                self._policy,
                read_only=True,
            ) as capability:
                result = capability.inspect_repository_file(
                    path,
                    maximum=MAX_REPOSITORY_WRITE_BYTES,
                )
        except Exception:  # noqa: BLE001 - expose one context-free safe category
            failed = True
        if failed:
            raise RepositoryWriteError()
        if result is None or result[0] != expected_digest:
            return None
        digest, byte_count, normalized = result
        return FileWrite(
            path=normalized,
            previous_digest=digest,
            output_digest=digest,
            byte_count=byte_count,
            created=False,
        )


__all__ = ["RepositoryWriteError", "WorktreeRepositoryWriter"]
