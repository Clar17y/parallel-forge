"""Observation-only startup reconciliation of admitted release operations."""

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.merge_evidence import MergeEvidenceValidator
from forge.application.services.recovery import RecoveryError
from forge.domain.approval import MergeApprovalEvidence
from forge.domain.operation import OperationIntent, OperationOutcome
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.release.merge import MergeController, MergeOperation


def merge_recovery_adapters(
    factory: async_sessionmaker[AsyncSession],
    evidence: MergeEvidenceValidator,
    controller: MergeController,
) -> dict[str, OperationAdapter]:
    return {"merge_pr": _MergeRecovery(factory, evidence, controller)}


class _MergeRecovery:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        evidence: MergeEvidenceValidator,
        controller: MergeController,
    ) -> None:
        self._factory, self._evidence, self._controller = factory, evidence, controller

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        raise RecoveryError("startup release adapters cannot invoke effects")

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        if intent.kind != "merge_pr":
            raise RecoveryError("startup merge operation kind differs")
        approval_id = UUID(str(intent.request_payload.get("approval_id")))
        async with PostgresUnitOfWork(self._factory) as work:
            approved = await self._evidence.for_recovery(
                cast(UnitOfWork, work), intent.run_id, approval_id
            )
            record = await work.releases.get_for_run(intent.run_id)
            if record is None:
                raise RecoveryError("startup merge PR identity is absent")

        async def no_new_authority() -> MergeApprovalEvidence:
            raise RecoveryError("startup recovery cannot authorize a merge")

        operation = MergeOperation(
            self._controller, record, approval_id, approved, no_new_authority
        )
        return await operation.reconcile(intent)
