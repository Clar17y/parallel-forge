"""Production factories for run-scoped delivery dependencies."""

from __future__ import annotations

import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock
from forge.application.ports.operations import OperationRepository
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import DatabaseBinding, ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.domain.event import RunEvent
from forge.domain.operation import OperationIntent
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.domain.run import RunSnapshot
from forge.observability.redaction import Redactor
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.tools.database import DatabaseProvisioner
from forge.tools.environment import EnvironmentStager
from forge.tools.git import ControlledGit, ControlledGitError
from forge.tools.paths import CanonicalRoot
from forge.tools.repository import RepositoryReader
from forge.tools.repository_writer import WorktreeRepositoryWriter
from forge.tools.secrets import LocalSecretStore
from forge.tools.worktree import WorktreeProvisioner
from forge.tools.worktree_runner import WorktreeBoundRunner, WorktreeRunnerFactory

_ENV_REFERENCE = re.compile(r"secret://environment/([A-Z][A-Z0-9_]{0,127})\Z")


class DeliveryRuntimeError(RuntimeError):
    """Stable, redacted failure while building delivery dependencies."""

    def __init__(self) -> None:
        super().__init__("delivery runtime is unavailable")


class _EnvironmentSecretResolver:
    """Resolve one explicit operator environment reference without exposing it."""

    async def resolve(self, reference: str) -> str:
        if not isinstance(reference, str):
            raise DeliveryRuntimeError()
        match = _ENV_REFERENCE.fullmatch(reference)
        if match is None:
            raise DeliveryRuntimeError()
        value = os.environ.get(match.group(1))
        if not value:
            raise DeliveryRuntimeError()
        return value


class _TrustedHostAudit:
    """Persist the required trusted-host disclosure against one exact run."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._worktree = worktree
        self._policy = policy

    async def record(
        self, event_type: str, *, priority: str, payload: Mapping[str, object]
    ) -> None:
        if priority != "high" or event_type not in {
            "runner.trusted_host.attempt",
            "runner.trusted_host.completed",
        }:
            raise DeliveryRuntimeError()
        self._validate_payload(event_type, payload)
        identity = self._worktree.identity
        if identity.run_id is None:
            raise DeliveryRuntimeError()
        async with self._uow_factory() as work:
            run = await work.runs.get(identity.run_id)
            policy_record = await work.projects.get_policy(self._policy.id, self._policy.version)
            if (
                run is None
                or run.project_id != self._policy.id
                or run.policy_version != self._policy.version
                or run.branch_name != identity.branch
                or run.worktree_path != str(self._worktree.path)
                or run.base_sha != self._worktree.base_sha
                or policy_record is None
                or ProjectPolicy.model_validate(policy_record.document) != self._policy
            ):
                raise DeliveryRuntimeError()
            await work.events.append(
                RunEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type=event_type,
                    payload={"priority": "high", **dict(payload)},
                    actor_class="worker",
                )
            )
            await work.commit()

    def _validate_payload(self, event_type: str, payload: Mapping[str, object]) -> None:
        required = {
            "command_kind",
            "command_name",
            "network_containment",
            "policy_version",
            "runner_mode",
            "unsandboxed",
        }
        permitted = required | (
            {"evidence_digest", "caller_cancelled"} if event_type.endswith("completed") else set()
        )
        if set(payload) - permitted or not required.issubset(payload):
            raise DeliveryRuntimeError()
        if (
            not isinstance(payload["command_kind"], str)
            or not payload["command_kind"]
            or not isinstance(payload["command_name"], str)
            or not payload["command_name"]
            or payload["network_containment"] is not False
            or payload["policy_version"] != self._policy.version
            or payload["runner_mode"] != RunnerMode.TRUSTED_HOST.value
            or payload["unsandboxed"] is not True
        ):
            raise DeliveryRuntimeError()
        if event_type.endswith("completed"):
            digest = payload.get("evidence_digest")
            if not isinstance(digest, str) or len(digest) != 64:
                raise DeliveryRuntimeError()
            cancelled = payload.get("caller_cancelled")
            if cancelled is not None and cancelled is not True:
                raise DeliveryRuntimeError()


class DeliveryRuntime:
    """Build exact policy/run-bound production adapters on demand.

    Nothing is cached by project: callers that need shared authority can pass the
    Git instance returned by :meth:`git` to :meth:`writer`.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_store: ArtifactStore,
        redactor: Redactor | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(settings, Settings):
            raise TypeError("settings must be Settings")
        self._settings = settings
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._redactor = redactor or Redactor()
        self._clock = clock
        self._operations = PostgresOperationRepository(session_factory)
        self._operation_executor = OperationExecutor(self._operations)

    @property
    def operation_executor(self) -> OperationExecutor:
        return self._operation_executor

    def git(self, policy: ProjectPolicy) -> ControlledGit:
        self._validate_policy(policy)
        try:
            repository = CanonicalRoot(policy.repository_path)
            data_root = self._settings.data_root.resolve()
            if _overlaps(repository.path, data_root):
                raise DeliveryRuntimeError()
            executable = shutil.which("git")
            if executable is None:
                raise DeliveryRuntimeError()
            return ControlledGit(
                repository,
                default_branch=policy.default_branch,
                state_root=data_root / "git" / policy.id.hex,
                git_executable=Path(executable).resolve(strict=True),
            )
        except DeliveryRuntimeError:
            raise
        except OSError, TypeError, ValueError:
            raise DeliveryRuntimeError() from None

    def reader(self, policy: ProjectPolicy, worktree: ManagedWorktree) -> RepositoryReader:
        git = self.git(policy)
        self._validate_worktree(git, policy, worktree)
        return RepositoryReader(root=worktree.path, secret_paths=policy.effective_secret_paths)

    def writer(
        self,
        policy: ProjectPolicy,
        worktree: ManagedWorktree,
        *,
        controlled_git: ControlledGit | None = None,
    ) -> WorktreeRepositoryWriter:
        git = controlled_git or self.git(policy)
        self._validate_worktree(git, policy, worktree)
        return WorktreeRepositoryWriter(git, worktree, policy)

    async def prepare(self, run_id: UUID, policy: ProjectPolicy) -> ManagedWorktree:
        git = self.git(policy)
        provisioner = self._provisioner(policy, git)
        return await provisioner.prepare(run_id, policy)

    async def teardown(self, run_id: UUID, policy: ProjectPolicy) -> RunSnapshot:
        git = self.git(policy)
        return await self._provisioner(policy, git).teardown(run_id, policy)

    async def reconcile(self, intent_id: UUID, policy: ProjectPolicy) -> OperationIntent:
        git = self.git(policy)
        return await self._provisioner(policy, git).reconcile(intent_id, policy)

    def create(self, worktree: ManagedWorktree, policy: ProjectPolicy) -> WorktreeBoundRunner:
        git = self.git(policy)
        self._validate_worktree(git, policy, worktree)
        image = self._settings.runner_image if policy.runner_mode is RunnerMode.DOCKER else None
        return WorktreeRunnerFactory(
            git,
            image_digest=image,
            artifact_store=self._artifact_store,
            clock=self._clock,
            audit=(
                _TrustedHostAudit(self._uow_factory, worktree, policy)
                if policy.runner_mode is RunnerMode.TRUSTED_HOST
                else None
            ),
        ).create(worktree, policy)

    async def environment(
        self, run: RunSnapshot, policy: ProjectPolicy, worktree: ManagedWorktree
    ) -> Mapping[str, str]:
        self._validate_policy(policy)
        if not isinstance(run, RunSnapshot):
            raise DeliveryRuntimeError()
        identity = self._run_identity(run, policy, worktree)
        if not policy.database.enabled:
            if (
                run.database_state is not ResourceState.DISABLED
                or run.database_name is not None
                or run.database_role is not None
                or run.secret_id is not None
            ):
                raise DeliveryRuntimeError()
            return {}
        binding = DatabaseBinding(
            state=run.database_state,
            database_name=run.database_name,
            database_role=run.database_role,
            secret_id=run.secret_id,
        )
        if binding.state is not ResourceState.ACTIVE:
            raise DeliveryRuntimeError()
        rematerialized = await self._database(policy).rematerialize_active(
            identity, policy.database, binding, policy_version=policy.version
        )
        if rematerialized.state is not ResourceState.ACTIVE:
            raise DeliveryRuntimeError()
        return rematerialized.environment

    def _provisioner(self, policy: ProjectPolicy, git: ControlledGit) -> WorktreeProvisioner:
        return WorktreeProvisioner(
            self._uow_factory,
            operations=cast(OperationRepository, self._operations),
            git=git,
            database=self._database(policy),
            operation_executor=self._operation_executor,
            environment_stager=EnvironmentStager(git),
            runner_factory=self,
        )

    @property
    def _uow_factory(self) -> Callable[[], UnitOfWork]:
        return cast(
            Callable[[], UnitOfWork],
            lambda: PostgresUnitOfWork(self._session_factory, redactor=self._redactor),
        )

    def _database(self, policy: ProjectPolicy) -> DatabaseProvisioner:
        self._validate_policy(policy)
        try:
            return DatabaseProvisioner(
                operation_executor=self._operation_executor,
                operation_repository=self._operations,
                admin_secret_resolver=_EnvironmentSecretResolver(),
                secret_store=LocalSecretStore(self._settings.data_root),
                password_source=secrets,
                connection_factory=asyncpg.connect,
            )
        except OSError, TypeError, ValueError:
            raise DeliveryRuntimeError() from None

    @staticmethod
    def _validate_policy(policy: ProjectPolicy) -> None:
        if not isinstance(policy, ProjectPolicy):
            raise DeliveryRuntimeError()

    @staticmethod
    def _validate_worktree(
        git: ControlledGit, policy: ProjectPolicy, worktree: ManagedWorktree
    ) -> None:
        if not isinstance(worktree, ManagedWorktree):
            raise DeliveryRuntimeError()
        if worktree.identity.project_id != policy.id:
            raise DeliveryRuntimeError()
        try:
            expected = git.expected_worktree(worktree.identity, worktree.base_sha)
        except ControlledGitError, TypeError, ValueError:
            raise DeliveryRuntimeError() from None
        if expected != worktree:
            raise DeliveryRuntimeError()
        try:
            if git.inspect_worktree(worktree.identity, worktree.base_sha) != worktree:
                raise DeliveryRuntimeError()
        except DeliveryRuntimeError:
            raise
        except ControlledGitError, TypeError, ValueError:
            raise DeliveryRuntimeError() from None

    def _run_identity(
        self, run: RunSnapshot, policy: ProjectPolicy, worktree: ManagedWorktree
    ) -> WorktreeIdentity:
        if (
            run.project_id != policy.id
            or run.policy_version != policy.version
            or run.branch_name is None
            or run.worktree_path is None
            or run.base_sha is None
        ):
            raise DeliveryRuntimeError()
        try:
            identity = WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, policy.database.enabled
            )
        except TypeError, ValueError:
            raise DeliveryRuntimeError() from None
        if (
            worktree.identity != identity
            or worktree.base_sha != run.base_sha
            or worktree.path != Path(run.worktree_path)
        ):
            raise DeliveryRuntimeError()
        self._validate_worktree(self.git(policy), policy, worktree)
        return identity


def _overlaps(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


__all__ = ["DeliveryRuntime", "DeliveryRuntimeError"]
