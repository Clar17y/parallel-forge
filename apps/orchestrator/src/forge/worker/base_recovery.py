"""Reconcile admitted remote base updates and local adoption without repeating writes."""

from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.base_adoption import BaseAdoptionPort
from forge.application.ports.github import GitHubPort
from forge.application.ports.github_write import GitHubWritePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.base_update_authority import base_update_origin
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.recovery import RecoveryError
from forge.domain.operation import OperationIntent, OperationOutcome, OperationStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.base_adoption import BaseAdoptionOperation
from forge.release.base_update import BaseUpdateOperation
from forge.release.controller import Publication, _validate_intent


def base_recovery_adapters(
    factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
    evidence: PrEvidenceValidator, reads: GitHubPort, writes: GitHubWritePort,
    adoption: Callable[[ProjectPolicy], BaseAdoptionPort],
) -> dict[str, OperationAdapter]:
    return {
        kind: _BaseRecovery(factory, store, evidence, reads, writes, adoption, kind)
        for kind in ("update_branch", "adopt_base")
    }


class _BaseRecovery:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], store: ArtifactStore,
        evidence: PrEvidenceValidator, reads: GitHubPort, writes: GitHubWritePort,
        adoption: Callable[[ProjectPolicy], BaseAdoptionPort], kind: str,
    ) -> None:
        self._factory, self._store, self._evidence = factory, store, evidence
        self._reads, self._writes, self._adoption, self._kind = reads, writes, adoption, kind

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup base adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if intent.kind != self._kind:
            raise RecoveryError("startup base operation kind differs")
        async with PostgresUnitOfWork(self._factory) as work:
            record = await work.releases.get_for_run(intent.run_id)
            if record is None:
                raise RecoveryError("startup base PR identity is absent")
            publication = await work.operations.get(record.publication_intent_id)
            approval_id = UUID(str(publication.request_payload.get("approval_id")))
            frozen = await self._evidence.for_recovery(
                cast(UnitOfWork, work), intent.run_id, approval_id
            )
            approved = frozen.approved
            _validate_intent(publication, Publication(
                run_id=intent.run_id, approval_id=approval_id, policy_version=approved.policy.version,
                branch=record.pull_request.head_ref, evidence=frozen.evidence,
            ).request("create_pr"))
            if (
                publication.status is not OperationStatus.SUCCEEDED
                or publication.remote_resource_id != record.pull_request.node_id
                or publication.outcome != asdict(replace(
                    record.pull_request, head_sha=frozen.evidence.candidate_commit,
                    base_sha=frozen.evidence.base_sha,
                ))
            ):
                raise RecoveryError("startup base publication receipt differs")
            attempt = intent.request_payload.get("remote_attempt")
            if type(attempt) is not int:
                raise RecoveryError("startup base attempt differs")
            command = await work.commands.get_by_idempotency_key(
                f"{intent.run_id}:remote-remediation:{attempt}"
            )
            if command is None:
                raise RecoveryError("startup base delivery is absent")
            digest, target = await base_update_origin(
                cast(UnitOfWork, work), self._store, command, approved, record
            )
            remote = BaseUpdateOperation(
                record, self._reads, self._writes, target, approved.policy.version, digest, attempt
            )
            updated = None
            if self._kind == "adopt_base":
                updated = await work.operations.get(UUID(str(intent.request_payload.get("update_intent_id"))))
                _validate_intent(updated, remote.request)
        if self._kind == "update_branch":
            return await remote.reconcile(intent)
        run, policy = approved.run, approved.policy
        if updated is None or not run.branch_name or not run.worktree_path or not run.base_sha:
            raise RecoveryError("startup base adoption identity differs")
        tree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, policy.database.enabled),
            path=Path(run.worktree_path), base_sha=run.base_sha,
        )
        return await BaseAdoptionOperation(
            record, updated, tree, policy, self._adoption(policy)
        ).reconcile(intent)
