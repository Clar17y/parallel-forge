"""PostgreSQL mapping for immutable epic graph revisions."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_brief import EpicNotFound, EpicRecord, EpicVersionConflict
from forge.domain.epic_items import GraphRevisionNotFound, GraphRevisionRecord, ItemSnapshot
from forge.persistence.models.epic_brief import Epic
from forge.persistence.models.epic_items import EpicGraphRevision
from forge.persistence.repositories.epic_brief import _epic


class PostgresEpicItemsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_revision(
        self,
        epic_id: UUID,
        *,
        version: int,
        brief_revision_id: UUID,
        brief_digest: str,
        graph_revision_id: UUID,
        graph_digest: str,
        items: list[ItemSnapshot],
    ) -> GraphRevisionRecord:
        epic = await self._epic(epic_id)
        if epic.version != version:
            raise EpicVersionConflict("epic version is stale")
        number = (
            await self._session.execute(
                select(func.coalesce(func.max(EpicGraphRevision.revision_number), 0)).where(
                    EpicGraphRevision.epic_id == epic_id
                )
            )
        ).scalar_one() + 1
        epic.version += 1
        revision = EpicGraphRevision(
            id=graph_revision_id,
            epic_id=epic_id,
            brief_revision_id=brief_revision_id,
            brief_digest=brief_digest,
            revision_number=number,
            epic_version=epic.version,
            content={
                "schema_version": 1,
                "items": [item.model_dump(mode="json") for item in items],
            },
            graph_digest=graph_digest,
        )
        self._session.add(revision)
        await self._session.flush()
        await self._session.refresh(revision)
        return _revision(revision)

    async def adopt_revision(
        self, epic_id: UUID, *, version: int, revision: GraphRevisionRecord
    ) -> EpicRecord:
        epic = await self._epic(epic_id)
        if epic.version != version:
            raise EpicVersionConflict("epic version is stale")
        epic.accepted_graph_revision_id = revision.graph_revision_id
        epic.accepted_graph_digest = revision.graph_digest
        epic.version += 1
        await self._session.flush()
        await self._session.refresh(epic)
        return _epic(epic)

    async def get_revision(self, epic_id: UUID, graph_revision_id: UUID) -> GraphRevisionRecord:
        row = (
            await self._session.execute(
                select(EpicGraphRevision).where(
                    EpicGraphRevision.epic_id == epic_id, EpicGraphRevision.id == graph_revision_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise GraphRevisionNotFound("graph revision was not found")
        return _revision(row)

    async def list_revisions(self, epic_id: UUID) -> Sequence[GraphRevisionRecord]:
        await self._epic(epic_id)
        rows = (
            (
                await self._session.execute(
                    select(EpicGraphRevision)
                    .where(EpicGraphRevision.epic_id == epic_id)
                    .order_by(EpicGraphRevision.revision_number)
                )
            )
            .scalars()
            .all()
        )
        return [_revision(row) for row in rows]

    async def _epic(self, epic_id: UUID) -> Epic:
        row = await self._session.get(Epic, epic_id)
        if row is None:
            raise EpicNotFound("epic was not found")
        return row


def _revision(row: EpicGraphRevision) -> GraphRevisionRecord:
    return GraphRevisionRecord(
        graph_revision_id=row.id,
        epic_id=row.epic_id,
        brief_revision_id=row.brief_revision_id,
        brief_digest=row.brief_digest,
        revision_number=row.revision_number,
        epic_version=row.epic_version,
        graph_digest=row.graph_digest,
        items=[ItemSnapshot.model_validate(item) for item in row.content["items"]],
        created_at=row.created_at,
    )
