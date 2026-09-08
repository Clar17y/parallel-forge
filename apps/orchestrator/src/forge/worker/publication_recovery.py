"""Observe admitted original publication effects without a write capability."""

from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.recovery import RecoveryError
from forge.domain.approval import canonical_digest
from forge.domain.operation import OperationIntent, OperationOutcome
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.controller import (
    Publication,
    PullRequestOperation,
    PushOperation,
    ReviewedPushOperation,
)


def publication_recovery_adapters(
    factory: async_sessionmaker[AsyncSession],
    evidence: PrEvidenceValidator,
    github: GitHubWritePort,
) -> dict[str, OperationAdapter]:
    return {
        kind: _PublicationRecovery(factory, evidence, github, kind)
        for kind in ("push_branch", "create_pr")
    }


class _NoPush:
    async def push(
        self, worktree: ManagedWorktree, policy: ProjectPolicy, approved_sha: str
    ) -> None:
        raise RecoveryError("startup recovery cannot push a branch")


class _PublicationRecovery:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        evidence: PrEvidenceValidator,
        github: GitHubWritePort,
        kind: str,
    ) -> None:
        self._factory, self._evidence, self._github, self._kind = factory, evidence, github, kind

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup publication adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if intent.kind != self._kind:
            raise RecoveryError("startup publication operation kind differs")
        approval_id = UUID(str(intent.request_payload.get("approval_id")))
        candidate = intent.request_payload.get("candidate_evidence_digest")
        record = None
        async with PostgresUnitOfWork(self._factory) as work:
            if candidate is None:
                frozen = await self._evidence.for_recovery(
                    cast(UnitOfWork, work), intent.run_id, approval_id
                )
                approval_digest = canonical_digest(frozen.evidence)
            else:
                if self._kind != "push_branch" or not isinstance(candidate, str):
                    raise RecoveryError("startup reviewed publication kind differs")
                frozen, approval_digest = await self._evidence.for_reviewed_recovery(
                    cast(UnitOfWork, work), intent.run_id, approval_id, candidate
                )
                record = await work.releases.get_for_run(intent.run_id)
                if record is None:
                    raise RecoveryError("startup reviewed PR identity is absent")
        run, policy = frozen.approved.run, frozen.approved.policy
        if not run.branch_name or not run.worktree_path or not run.base_sha:
            raise RecoveryError("startup publication worktree identity is absent")
        publication = Publication(
            run_id=run.id,
            approval_id=approval_id,
            policy_version=policy.version,
            branch=run.branch_name,
            evidence=frozen.evidence,
        )
        operation: OperationAdapter
        if self._kind == "create_pr":
            operation = PullRequestOperation(publication, self._github, frozen.body)
        else:
            tree = ManagedWorktree(
                identity=WorktreeIdentity.for_run(
                    run.project_id, run.id, run.branch_name, policy.database.enabled
                ),
                path=Path(run.worktree_path),
                base_sha=run.base_sha,
            )
            if record is None:
                operation = PushOperation(publication, self._github, _NoPush(), tree, policy)
            else:
                operation = ReviewedPushOperation(
                    record, approval_id, approval_digest, frozen.evidence,
                    self._github, _NoPush(), tree, policy,
                )
        # Controller reconciliation checks the entire admitted request before reading remote state.
        return await operation.reconcile(intent)
