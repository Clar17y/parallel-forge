"""Caller-owned PostgreSQL transaction for immutable epic launch bindings."""

from collections.abc import Sequence
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_run_bridge import (
    DependencyEvidence,
    EpicAttempt,
    EpicAttemptNotFound,
    EpicExecutionNotFound,
    EpicExecutionSnapshot,
)
from forge.persistence.models.epic_run_bridge import EpicExecution, EpicItemAttempt


class PostgresEpicRunBridgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_execution(
        self,
        *,
        epic_id: UUID,
        brief_revision_id: UUID,
        brief_digest: str,
        graph_revision_id: UUID,
        graph_digest: str,
    ) -> EpicExecutionSnapshot:
        row = EpicExecution(
            id=uuid4(),
            epic_id=epic_id,
            brief_revision_id=brief_revision_id,
            brief_digest=brief_digest,
            graph_revision_id=graph_revision_id,
            graph_digest=graph_digest,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _execution(row)

    async def get_execution(self, execution_id: UUID) -> EpicExecutionSnapshot:
        row = await self._session.get(EpicExecution, execution_id)
        if row is None:
            raise EpicExecutionNotFound("execution was not found")
        return _execution(row)

    async def create_attempt(self, attempt: EpicAttempt) -> None:
        data = attempt.model_dump(mode="python", exclude={"attempt_id", "dependency_evidence"})
        self._session.add(
            EpicItemAttempt(
                id=attempt.attempt_id,
                **data,
                dependency_evidence=[
                    value.model_dump(mode="json") for value in attempt.dependency_evidence
                ],
            )
        )
        await self._session.flush()

    async def get_attempt(self, epic_id: UUID, attempt_id: UUID) -> EpicAttempt:
        row = await self._session.scalar(
            select(EpicItemAttempt).where(
                EpicItemAttempt.epic_id == epic_id, EpicItemAttempt.id == attempt_id
            )
        )
        if row is None:
            raise EpicAttemptNotFound("epic attempt was not found")
        return _attempt(row)

    async def list_attempts(self, epic_id: UUID) -> Sequence[EpicAttempt]:
        rows = (
            await self._session.scalars(
                select(EpicItemAttempt)
                .where(EpicItemAttempt.epic_id == epic_id)
                .order_by(EpicItemAttempt.created_at, EpicItemAttempt.id)
            )
        ).all()
        return [_attempt(row) for row in rows]


def _attempt(row: EpicItemAttempt) -> EpicAttempt:
    return EpicAttempt(
        attempt_id=row.id,
        execution_id=row.execution_id,
        epic_id=row.epic_id,
        item_id=row.item_id,
        attempt_number=row.attempt_number,
        item_disposition=cast(Literal["required", "deferred"], row.item_disposition),
        actor_id=row.actor_id,
        expected_epic_version=row.expected_epic_version,
        actual_epic_version=row.actual_epic_version,
        task_id=row.task_id,
        run_id=row.run_id,
        brief_revision_id=row.brief_revision_id,
        brief_digest=row.brief_digest,
        graph_revision_id=row.graph_revision_id,
        graph_digest=row.graph_digest,
        item_digest=row.item_digest,
        context_digest=row.context_digest,
        task_digest=row.task_digest,
        base_ref=row.base_ref,
        base_sha=row.base_sha,
        owner_override=row.owner_override,
        override_note=row.override_note,
        blocker_codes=row.blocker_codes,
        dependency_evidence=[
            DependencyEvidence.model_validate(value) for value in row.dependency_evidence
        ],
        created_at=row.created_at,
    )


def _execution(row: EpicExecution) -> EpicExecutionSnapshot:
    return EpicExecutionSnapshot(
        execution_id=row.id,
        epic_id=row.epic_id,
        brief_revision_id=row.brief_revision_id,
        brief_digest=row.brief_digest,
        graph_revision_id=row.graph_revision_id,
        graph_digest=row.graph_digest,
        created_at=row.created_at,
    )
