"""Observation-only startup reconciliation of admitted release operations."""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.github_write import GitHubMergeQueuePort
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.recovery import RecoveryError
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.operation import OperationIntent, OperationOutcome
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController, MergeOperation
from forge.release.queue import EnqueueOperation


def merge_recovery_adapters(
    factory: async_sessionmaker[AsyncSession],
    evidence: MergeEvidenceValidator,
    controller: MergeController,
    queue: GitHubMergeQueuePort | None = None,
) -> dict[str, OperationAdapter]:
    adapters: dict[str, OperationAdapter] = {"merge_pr": _MergeRecovery(factory, evidence, controller)}
    if queue is not None:
        adapters["enqueue_pr"] = _MergeRecovery(factory, evidence, controller, queue)
    return adapters


class _MergeRecovery:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        evidence: MergeEvidenceValidator,
        controller: MergeController,
        queue: GitHubMergeQueuePort | None = None,
    ) -> None:
        self._factory, self._evidence, self._controller = factory, evidence, controller
        self._queue = queue

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup release adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if intent.kind != ("enqueue_pr" if self._queue is not None else "merge_pr"):
            raise RecoveryError("startup merge operation kind differs")
        approval_id = UUID(str(intent.request_payload.get("approval_id")))
        async with PostgresUnitOfWork(self._factory) as work:
            approved = await self._evidence.for_recovery(
                cast(UnitOfWork, work), intent.run_id, approval_id
            )
            record = await work.releases.get_for_run(intent.run_id)
            if record is None:
                raise RecoveryError("startup merge PR identity is absent")
            if self._queue is not None and not await self._evidence.queue_required(
                cast(UnitOfWork, work), intent.run_id, approval_id, approved
            ):
                raise RecoveryError("startup enqueue approval mode differs")

        async def no_new_authority() -> MergeApprovalEvidence:
            raise RecoveryError("startup recovery cannot authorize a merge")

        operation: OperationAdapter = (
            EnqueueOperation(self._controller, self._queue, record, approval_id, approved, no_new_authority)
            if self._queue is not None
            else MergeOperation(self._controller, record, approval_id, approved, no_new_authority)
        )
        return await operation.reconcile(intent)
