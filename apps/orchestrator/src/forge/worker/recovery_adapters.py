"""Reconstruct run-bound, observation-only adapters from durable authority."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.adapters.controller_check import (
    CONTROLLER_CHECK_KIND,
    ControllerCheckOperationAdapter,
)
from forge.application.adapters.git_commit import (
    PREPARE_GIT_COMMIT_KIND,
    PUBLISH_GIT_COMMIT_KIND,
    PrepareGitCommitAdapter,
    PublishGitCommitAdapter,
)
from forge.application.adapters.named_check import NAMED_CHECK_KIND, NamedCheckOperationAdapter
from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.repository import RepositoryWriter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ControlledGitPort, ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.recovery import RecoveryError
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.application.services.tools import _PreparedWrite, _RepositoryWriteOperationAdapter
from forge.domain.operation import OperationIntent, OperationOutcome, canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.tool import ToolName
from forge.persistence.repositories.artifacts import ArtifactRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork

_LOCAL_KINDS = (
    NAMED_CHECK_KIND,
    CONTROLLER_CHECK_KIND,
    PREPARE_GIT_COMMIT_KIND,
    PUBLISH_GIT_COMMIT_KIND,
)
WriterFactory = Callable[[ProjectPolicy, ManagedWorktree, ControlledGitPort], RepositoryWriter]


def local_recovery_adapters(
    factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    git_factory: Callable[[ProjectPolicy], ControlledGitPort],
    writer_factory: WriterFactory | None = None,
) -> dict[str, OperationAdapter]:
    kinds = _LOCAL_KINDS + (
        (
            ToolName.REPOSITORY_WRITE_FILE.value,
            ToolName.REPOSITORY_DELETE_FILE.value,
            ToolName.REPOSITORY_RENAME_FILE.value,
        )
        if writer_factory
        else ()
    )
    return {
        kind: _LocalRecovery(kind, factory, store, git_factory, writer_factory) for kind in kinds
    }


class _LocalRecovery:
    def __init__(
        self,
        kind: str,
        factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        git_factory: Callable[[ProjectPolicy], ControlledGitPort],
        writer_factory: WriterFactory | None,
    ) -> None:
        self._kind, self._factory, self._store, self._git = kind, factory, store, git_factory
        self._writer = writer_factory

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if (
            intent.kind != self._kind
            or intent.request_schema_version != 1
            or intent.request_digest != canonical_digest(intent.request_payload)
        ):
            raise RecoveryError("startup operation identity differs")
        async with PostgresUnitOfWork(self._factory) as work:
            approved = await ApprovedPlanLoader(self._store).load(
                cast(UnitOfWork, work), intent.run_id
            )
            run, policy = approved.run, approved.policy
            if run.worktree_path is None or run.branch_name is None or run.base_sha is None:
                raise RecoveryError("startup operation has no managed worktree")
            worktree = ManagedWorktree(
                identity=WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, policy.database.enabled
                ),
                path=Path(run.worktree_path),
                base_sha=run.base_sha,
            )
            if self._kind in {
                ToolName.REPOSITORY_WRITE_FILE.value,
                ToolName.REPOSITORY_DELETE_FILE.value,
                ToolName.REPOSITORY_RENAME_FILE.value,
            }:
                call = await work.tool_calls.get(UUID(intent.idempotency_key.removeprefix("tool:")))
                if (
                    not ToolRecoveryService.valid_repository_mutation(
                        call, intent, run, intent.outcome or {}
                    )
                    or call.resource_id != worktree.identity.worktree_name
                    or call.agent_execution_id is None
                    or call.step_id is None
                    or call.role is None
                    or not await work.tool_calls.validate_execution_context(
                        run_id=run.id,
                        agent_execution_id=call.agent_execution_id,
                        step_id=call.step_id,
                        role=call.role,
                    )
                ):
                    raise RecoveryError("startup write authority differs")
        git = self._git(policy)
        if (
            await asyncio.to_thread(git.inspect_worktree, worktree.identity, worktree.base_sha)
            != worktree
        ):
            raise RecoveryError("startup managed worktree differs")
        artifacts = ArtifactRepository(self._factory)
        adapter: OperationAdapter
        if self._kind == NAMED_CHECK_KIND:
            adapter = NamedCheckOperationAdapter.for_recovery(
                worktree=worktree,
                policy=policy,
                artifacts=artifacts,
                artifact_store=self._store,
            )
        elif self._kind == CONTROLLER_CHECK_KIND:
            payload = intent.request_payload
            candidate = payload.get("candidate_tree_digest")
            if candidate is not None and not isinstance(candidate, str):
                raise RecoveryError("startup controller candidate binding is invalid")
            adapter = ControllerCheckOperationAdapter.for_recovery(
                run_id=intent.run_id,
                step_id=UUID(str(payload["step_id"])),
                result_id=UUID(str(payload["result_id"])),
                worktree=worktree,
                policy=policy,
                command_name=str(payload["command_name"]),
                head_sha=str(payload["head_sha"]),
                candidate_tree_digest=candidate,
                artifacts=artifacts,
                artifact_store=self._store,
            )
        elif self._kind == PREPARE_GIT_COMMIT_KIND:
            adapter = PrepareGitCommitAdapter(git, worktree)
        elif self._kind == PUBLISH_GIT_COMMIT_KIND:
            adapter = PublishGitCommitAdapter(
                git, worktree, PostgresOperationRepository(self._factory)
            )
        else:
            if self._writer is None:
                raise RecoveryError("startup write inspector is unavailable")
            writer = self._writer(policy, worktree, git)
            if not writer.is_bound_to(git, worktree, policy):
                raise RecoveryError("startup write inspector binding differs")
            tool_name = ToolName(self._kind)
            if tool_name is ToolName.REPOSITORY_WRITE_FILE:
                adapter = _RepositoryWriteOperationAdapter.for_recovery(
                    writer,
                    path=cast(str, intent.request_payload["path"]),
                    content_digest=cast(str, intent.request_payload["content_digest"]),
                    byte_count=cast(int, intent.request_payload["content_byte_count"]),
                )
            else:
                adapter = _RepositoryWriteOperationAdapter(
                    writer=writer,
                    tool_name=tool_name,
                    prepared=_PreparedWrite(
                        path=cast(str, intent.request_payload["path"]),
                        content=None,
                        content_digest=cast(str, intent.request_payload["expected_digest"]),
                        byte_count=None,
                        destination=cast(str | None, intent.request_payload.get("destination")),
                    ),
                )
        return await adapter.reconcile(intent)
