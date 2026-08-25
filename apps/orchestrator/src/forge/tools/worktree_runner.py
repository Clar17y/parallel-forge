"""Capability-bound command runners for retained Forge worktrees."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    RunCommandRequest,
    RunnerPort,
    TerminalRunnerPort,
)
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.tools.docker import DockerRunner
from forge.tools.git import ControlledGitError
from forge.tools.host import TrustedHostRunner
from forge.tools.paths import CanonicalRoot
from forge.tools.runner import RunnerExecutionError


class WorktreeRunnerFactoryError(RunnerExecutionError):
    """A bound runner could not be constructed from exact Forge inputs."""

    def __init__(self) -> None:
        super().__init__()


class WorktreeBoundRunner(RunnerPort, TerminalRunnerPort):
    """One command adapter retaining one managed-worktree capability per call."""

    __slots__ = ("_delegate", "_git", "_policy", "_worktree")

    def __init__(
        self,
        *,
        delegate: DockerRunner | TrustedHostRunner,
        controlled_git: ControlledGitPort,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> None:
        self._delegate = delegate
        self._git = controlled_git
        self._worktree = worktree
        self._policy = policy

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(runner_mode={self._policy.runner_mode.value!r}, "
            f"policy_version={self._policy.version})"
        )

    async def run(self, request: RunCommandRequest) -> CommandResult:
        """Compatibility method that raises only after terminal evidence exists."""

        terminal = await self.run_terminal(request)
        if terminal.caller_cancelled:
            raise asyncio.CancelledError()
        return terminal.result

    async def run_terminal(self, request: RunCommandRequest) -> CommandTerminalResult:
        """Run while the mutation lock and exact registration remain retained."""

        try:
            if not isinstance(request, RunCommandRequest):
                raise RunnerExecutionError()
            with self._git.open_worktree_capability(self._worktree, self._policy) as capability:
                capability.revalidate()
                if self._policy.runner_mode is RunnerMode.DOCKER:
                    terminal = await cast(DockerRunner, self._delegate)._run_terminal_at(
                        request,
                        self._worktree.path,
                        managed=True,
                        before_launch=capability.revalidate,
                    )
                else:
                    terminal = await cast(TrustedHostRunner, self._delegate)._run_terminal_at(
                        request,
                        self._worktree.path,
                        before_launch=capability.revalidate,
                    )
                if not isinstance(terminal, CommandTerminalResult):
                    raise RunnerExecutionError()
                capability.revalidate()
                return terminal
        except asyncio.CancelledError:
            # The concrete terminal adapters defer ordinary caller cancellation.  A
            # cancellation escaping this boundary therefore has no trustworthy
            # terminal evidence and must not be converted into a fake result.
            raise
        except RunnerExecutionError:
            raise
        except ControlledGitError:
            raise RunnerExecutionError() from None
        except Exception:  # noqa: BLE001 - capability failures are one safe category
            raise RunnerExecutionError() from None


class WorktreeRunnerFactory:
    """Create Docker or explicitly trusted-host runners for exact worktree handles."""

    def __init__(
        self,
        controlled_git: ControlledGitPort,
        *,
        image_digest: str | None = None,
        process_runner: Any | None = None,
        artifact_store: ArtifactStore | None = None,
        audit: Any | None = None,
        telemetry: Any | None = None,
        clock: Clock | None = None,
        docker_environment: Mapping[str, str] | None = None,
    ) -> None:
        repository = getattr(controlled_git, "_repository", None)
        if not isinstance(repository, CanonicalRoot):
            raise TypeError("worktree runner factory requires controlled Git")
        if image_digest is not None and not isinstance(image_digest, str):
            raise TypeError("runner image digest must be text")
        self._git = controlled_git
        self._root = repository
        self._image_digest = image_digest
        self._process_runner = process_runner
        self._artifact_store = artifact_store
        self._audit = audit
        self._telemetry = telemetry
        self._clock = clock
        self._docker_environment = docker_environment

    def __repr__(self) -> str:
        return f"{type(self).__name__}(configured=True)"

    def create(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> WorktreeBoundRunner:
        """Bind one exact managed worktree to the immutable policy."""

        _validate_binding(self._git, worktree, policy)
        if policy.runner_mode is RunnerMode.DOCKER:
            if not self._image_digest:
                raise WorktreeRunnerFactoryError()
            try:
                delegate: DockerRunner | TrustedHostRunner = DockerRunner(
                    policy=policy,
                    root=self._root,
                    image_digest=self._image_digest,
                    process_runner=self._process_runner,
                    artifact_store=self._artifact_store,
                    telemetry=self._telemetry,
                    clock=self._clock,
                    docker_environment=self._docker_environment,
                )
            except TypeError, ValueError, RunnerExecutionError:
                raise WorktreeRunnerFactoryError() from None
        elif policy.runner_mode is RunnerMode.TRUSTED_HOST:
            if self._image_digest is not None:
                raise WorktreeRunnerFactoryError()
            if not policy.trusted_project:
                raise WorktreeRunnerFactoryError()
            try:
                delegate = TrustedHostRunner(
                    policy=policy,
                    root=self._root,
                    process_runner=self._process_runner,
                    artifact_store=self._artifact_store,
                    audit=self._audit,
                    telemetry=self._telemetry,
                    clock=self._clock,
                )
            except TypeError, ValueError, RunnerExecutionError:
                raise WorktreeRunnerFactoryError() from None
        else:  # pragma: no cover - ProjectPolicy already closes this enum
            raise WorktreeRunnerFactoryError()
        return WorktreeBoundRunner(
            delegate=delegate,
            controlled_git=self._git,
            worktree=worktree,
            policy=policy,
        )

    bind = create


ManagedWorktreeRunnerFactory = WorktreeRunnerFactory
BoundWorktreeRunner = WorktreeBoundRunner


def _validate_binding(
    controlled_git: ControlledGitPort,
    worktree: ManagedWorktree,
    policy: ProjectPolicy,
) -> None:
    if not isinstance(worktree, ManagedWorktree) or not isinstance(policy, ProjectPolicy):
        raise WorktreeRunnerFactoryError()
    try:
        if worktree.identity.project_id != policy.id:
            raise WorktreeRunnerFactoryError()
        if Path(policy.repository_path) != controlled_git.repository_path:
            raise WorktreeRunnerFactoryError()
        expected = controlled_git.expected_worktree(worktree.identity, worktree.base_sha)
        if expected != worktree:
            raise WorktreeRunnerFactoryError()
        if (worktree.identity.database_name is not None) != policy.database.enabled:
            raise WorktreeRunnerFactoryError()

        if policy.runner_mode is RunnerMode.TRUSTED_HOST and not policy.trusted_project:
            raise WorktreeRunnerFactoryError()
    except WorktreeRunnerFactoryError:
        raise
    except Exception:  # noqa: BLE001 - caller input cannot cross this boundary
        raise WorktreeRunnerFactoryError() from None


__all__ = [
    "BoundWorktreeRunner",
    "ManagedWorktreeRunnerFactory",
    "WorktreeBoundRunner",
    "WorktreeRunnerFactory",
    "WorktreeRunnerFactoryError",
]
