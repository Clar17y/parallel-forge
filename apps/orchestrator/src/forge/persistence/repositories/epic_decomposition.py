"""PostgreSQL repository and unit of work implementations for epic decomposition."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.audit import AuditRepository
from forge.application.ports.epic_brief import EpicBriefRepository
from forge.application.ports.epic_decomposition import (
    EpicDecompositionJobRepository,
    LockedDecompositionProposal,
)
from forge.application.ports.epic_items import EpicItemsRepository
from forge.application.ports.mutations import MutationRepository
from forge.domain.epic_decomposition import (
    DecompositionConflict,
    DecompositionNotFound,
    DecompositionProposal,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.repositories.audit import PostgresAuditRepository
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.persistence.repositories.epic_brief import PostgresEpicBriefRepository
from forge.persistence.repositories.epic_items import PostgresEpicItemsRepository
from forge.persistence.repositories.mutations import PostgresMutationRepository


class PostgresEpicDecompositionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_job_for_adoption(
        self,
        job_id: UUID,
        *,
        epic_id: UUID,
        project_id: UUID,
        expected_job_version: int,
        proposal_digest: str,
    ) -> LockedDecompositionProposal:
        row = (
            await self._session.execute(
                select(BrainstormJobRow)
                .where(BrainstormJobRow.id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

        if row is None:
            raise DecompositionNotFound("decomposition authoring job was not found")

        if row.epic_id != epic_id or row.project_id != project_id:
            raise DecompositionConflict("job does not belong to specified epic or project")

        if row.version != expected_job_version:
            raise DecompositionConflict("job version is stale")

        if row.state != "proposed":
            raise DecompositionConflict(f"job is in '{row.state}' state, must be 'proposed'")

        if row.proposal_digest != proposal_digest:
            raise DecompositionConflict("proposal digest does not match settled job proposal")

        if row.adopted_revision_id is not None:
            raise DecompositionConflict("job proposal was already adopted")

        if row.proposal is None:
            raise DecompositionConflict("job has no proposal payload")

        try:
            snapshot = PostgresBrainstormRepository.decode_snapshot(row)
            proposal = DecompositionProposal.model_validate(row.proposal)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DecompositionConflict("decomposition job binding is invalid") from None
        if (
            str(snapshot.kind) != "decomposition"
            or snapshot.job_id != row.id
            or snapshot.epic_id != row.epic_id
            or snapshot.project_id != row.project_id
            or snapshot.conversation_id != row.conversation_id
            or snapshot.input_brief_revision_id is None
            or snapshot.input_brief_digest is None
            or snapshot.accepted_content is None
            or proposal.epic_id != row.epic_id
            or proposal.project_id != row.project_id
            or proposal.brief_revision_id != snapshot.input_brief_revision_id
            or proposal.brief_digest != snapshot.input_brief_digest
            or proposal.digest != row.proposal_digest
            or row.current_attempt_id is None
        ):
            raise DecompositionConflict("decomposition job binding is invalid")
        attempt = await self._session.get(BrainstormAttemptRow, row.current_attempt_id)
        if (
            attempt is None
            or attempt.job_id != row.id
            or attempt.state != "settled"
            or attempt.failure is not None
            or not attempt.process_started
            or not attempt.process_settled
            or attempt.terminal_proof is None
        ):
            raise DecompositionConflict("decomposition attempt is not settled")
        try:
            proof = SubscriptionLaunchTerminalProof.model_validate(attempt.terminal_proof)
        except ValidationError:
            raise DecompositionConflict("decomposition attempt proof is invalid") from None
        if not proof.permits_decision:
            raise DecompositionConflict("decomposition attempt proof is invalid")

        repository = PostgresBrainstormRepository(self._session)
        conversation = await repository.conversation(
            epic_id, project_id, row.conversation_id, lock=True
        )
        history = await repository.turns(row.conversation_id)
        source_index = snapshot.conversation_version - 2
        if (
            conversation.version != len(history) + 1
            or source_index < 0
            or source_index >= len(history)
            or history[source_index].turn_id != snapshot.prompt_turn_id
            or history[source_index].role != "operator"
            or any(turn.role != "assistant" or turn.pending for turn in history[source_index + 1 :])
            or sum(
                turn.turn_id == proposal.turn_id
                and turn.text == proposal.problem
                and turn.proposal is not None
                and turn.proposal.model_dump(mode="json") == proposal.model_dump(mode="json")
                for turn in history[source_index + 1 :]
            ) != 1
        ):
            raise DecompositionConflict("conversation changed after proposal input")
        return LockedDecompositionProposal(snapshot=snapshot, proposal=proposal)

    async def mark_job_adopted(
        self,
        job_id: UUID,
        *,
        graph_revision_id: UUID,
        job_version: int,
    ) -> None:
        row = await self._session.get(BrainstormJobRow, job_id, with_for_update=True)
        if row is None:
            raise DecompositionNotFound("decomposition authoring job was not found")
        row.adopted_revision_id = graph_revision_id
        row.version = job_version
        await self._session.flush()


class PostgresEpicDecompositionUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self.session = self._session_factory()
        self.epics: EpicBriefRepository = PostgresEpicBriefRepository(self.session)
        self.epic_items: EpicItemsRepository = PostgresEpicItemsRepository(self.session)
        self.mutations: MutationRepository = PostgresMutationRepository(self.session)
        self.audit: AuditRepository = PostgresAuditRepository(self.session)
        self.jobs: EpicDecompositionJobRepository = PostgresEpicDecompositionJobRepository(
            self.session
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.session is not None:
            if exc_type is not None:
                await self.session.rollback()
            await self.session.close()

    async def commit(self) -> None:
        if self.session is not None:
            await self.session.commit()

    async def rollback(self) -> None:
        if self.session is not None:
            await self.session.rollback()


__all__ = [
    "PostgresEpicDecompositionJobRepository",
    "PostgresEpicDecompositionUnitOfWork",
]
