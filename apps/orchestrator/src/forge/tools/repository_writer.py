"""Capability-bound repository file publication."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from uuid import UUID

from forge.application.ports.repository import MAX_REPOSITORY_WRITE_BYTES, FileWrite
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.artifact import validate_artifact_digest
from forge.domain.policy import ProjectPolicy
from forge.tools.git import ControlledGit, ControlledGitBusy, WorktreeCapability

_LOCK_WAIT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.05


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

    @contextmanager
    def _open_capability(self, *, read_only: bool = False) -> Iterator[WorktreeCapability]:
        """Retry only lock admission, releasing all handles between attempts.

        Disjoint task effects may briefly need the same filesystem lock. Each
        retry reacquires and revalidates the complete capability. Once admitted,
        neither the operation body nor its release validation is ever retried.
        """
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        with ExitStack() as stack:
            while True:
                try:
                    capability = stack.enter_context(
                        self._git.open_worktree_capability(
                            self._worktree,
                            self._policy,
                            read_only=read_only,
                            allow_committed_changes=True,
                        )
                    )
                    break
                except ControlledGitBusy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
            yield capability

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
            with self._open_capability() as capability:
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
            with self._open_capability(read_only=True) as capability:
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

    def delete_file(self, path: str, expected_digest: str, mutation_id: UUID) -> FileWrite:
        try:
            validate_artifact_digest(expected_digest)
            if not isinstance(mutation_id, UUID) or mutation_id.int == 0:
                raise ValueError("mutation identity is invalid")
            with self._open_capability() as capability:
                digest, byte_count, normalized = capability.delete_repository_file(
                    path,
                    expected_digest=expected_digest,
                    maximum=MAX_REPOSITORY_WRITE_BYTES,
                    mutation_id=mutation_id,
                )
        except Exception as error:
            raise RepositoryWriteError() from error
        return FileWrite(
            path=normalized,
            output_digest=digest,
            byte_count=byte_count,
            previous_digest=digest,
            created=False,
        )

    def rename_file(
        self, source: str, destination: str, expected_digest: str, mutation_id: UUID
    ) -> FileWrite:
        try:
            validate_artifact_digest(expected_digest)
            if not isinstance(mutation_id, UUID) or mutation_id.int == 0:
                raise ValueError("mutation identity is invalid")
            with self._open_capability() as capability:
                digest, byte_count, _source, normalized_destination = (
                    capability.rename_repository_file(
                        source,
                        destination,
                        expected_digest=expected_digest,
                        maximum=MAX_REPOSITORY_WRITE_BYTES,
                        mutation_id=mutation_id,
                    )
                )
        except Exception as error:
            raise RepositoryWriteError() from error
        return FileWrite(
            path=normalized_destination,
            output_digest=digest,
            byte_count=byte_count,
            previous_digest=digest,
            created=False,
        )


__all__ = ["RepositoryWriteError", "WorktreeRepositoryWriter"]
