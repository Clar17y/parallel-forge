"""Controller-only exact-object fetch and ancestry-checked local adoption."""

import asyncio

from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.policy import ProjectPolicy
from forge.release.credentials import (
    GitHubCredentialResolverPort,
    validate_github_credential_reference,
)
from forge.release.git_push import _SHA, _remote_configuration
from forge.tools.git import ControlledGit


class ManagedAdoptionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("managed base adoption requires reconciliation")


class ManagedBaseAdoption:
    def __init__(
        self, git: ControlledGit, credentials: GitHubCredentialResolverPort, reference: str
    ) -> None:
        self._git, self._credentials = git, credentials
        self._reference = validate_github_credential_reference(reference)

    async def inspect(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._inspect, worktree, policy, previous_sha, new_sha, base_sha
            )
        except OSError, RuntimeError, TypeError, ValueError:
            raise ManagedAdoptionError() from None

    def _inspect(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None:
        if (
            any(
                not isinstance(sha, str) or _SHA.fullmatch(sha) is None
                for sha in (previous_sha, new_sha, base_sha)
            )
            or previous_sha == new_sha
        ):
            raise ManagedAdoptionError()
        with self._git.open_worktree_capability(
            worktree, policy, read_only=True, allow_committed_changes=True
        ) as capability:
            self._git._reject_incomplete_history_overlays()
            if self._git.head_sha(worktree) != new_sha:
                raise ManagedAdoptionError()
            for ancestor in (previous_sha, base_sha):
                result = self._git._run(
                    worktree.path, ("merge-base", "--is-ancestor", ancestor, new_sha)
                )
                if result.stdout_truncated or result.stderr_truncated:
                    raise ManagedAdoptionError()
            status = self._git._run(
                worktree.path, ("status", "--porcelain=v1", "--untracked-files=all", "-z", "--")
            )
            if status.stdout or status.stdout_truncated or status.stderr_truncated:
                raise ManagedAdoptionError()
            capability.revalidate()

    async def adopt(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
    ) -> None:
        try:
            if (
                any(
                    not isinstance(sha, str) or _SHA.fullmatch(sha) is None
                    for sha in (previous_sha, new_sha, base_sha)
                )
                or previous_sha == new_sha
            ):
                raise ManagedAdoptionError()
            token = await self._credentials.resolve(self._reference)
            await asyncio.to_thread(
                self._adopt, worktree, policy, previous_sha, new_sha, base_sha, token
            )
        except asyncio.CancelledError:
            # The thread may still finish; its durable intent must be reconciled.
            raise
        except OSError, RuntimeError, TypeError, ValueError:
            raise ManagedAdoptionError() from None

    def _adopt(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
        previous_sha: str,
        new_sha: str,
        base_sha: str,
        token: str,
    ) -> None:
        with self._git.open_worktree_capability(
            worktree, policy, allow_committed_changes=True
        ) as capability:
            current = self._git.head_sha(worktree)
            if current not in {previous_sha, new_sha}:
                raise ManagedAdoptionError()
            configuration = _remote_configuration(self._git, worktree, policy, token)
            if current == previous_sha:
                capability.revalidate()
                result = self._git._run(
                    worktree.path,
                    (
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--no-write-fetch-head",
                        "--no-auto-maintenance",
                        "--refmap=",
                        "origin",
                        new_sha,
                    ),
                    configuration=configuration,
                )
                if result.stdout_truncated or result.stderr_truncated:
                    raise ManagedAdoptionError()
                capability.revalidate()
        # The fixed-head fetch capability is released before the separately
        # locked adoption; adoption repeats HEAD/ancestry/cleanliness checks.
        self._git.adopt_head(worktree, previous_sha, new_sha, base_sha)
