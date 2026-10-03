"""Transactional PostgreSQL epic and brief persistence."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.epic_brief import (
    AcceptedBrief,
    BriefContent,
    BriefNotAccepted,
    BriefRevisionNotFound,
    BriefRevisionRecord,
    EpicNotFound,
    EpicRecord,
    EpicVersionConflict,
)
from forge.persistence.models.epic_brief import Epic, EpicBriefRevision


class PostgresEpicBriefRepository:
    """Map saved snapshots; caller owns row locking and transaction commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, epic_id: UUID, project_id: UUID, title: str, draft: BriefContent
    ) -> EpicRecord:
        row = Epic(
            id=epic_id, project_id=project_id, title=title, draft=draft.model_dump(mode="json")
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _epic(row)

    async def get(self, epic_id: UUID, *, for_update: bool = False) -> EpicRecord:
        statement = select(Epic).where(Epic.id == epic_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise EpicNotFound("epic was not found")
        return _epic(row)

    async def list(self, project_id: UUID) -> Sequence[EpicRecord]:
        rows = (
            (
                await self._session.execute(
                    select(Epic)
                    .where(Epic.project_id == project_id)
                    .order_by(Epic.created_at, Epic.id)
                )
            )
            .scalars()
            .all()
        )
        return [_epic(row) for row in rows]

    async def update_draft(
        self, epic_id: UUID, *, version: int, title: str, draft: BriefContent
    ) -> EpicRecord:
        row = await self._row(epic_id)
        if row.version != version:
            raise EpicVersionConflict("epic version is stale")
        row.title = title
        row.draft = draft.model_dump(mode="json")
        row.version += 1
        await self._session.flush()
        await self._session.refresh(row)
        return _epic(row)

    async def save_revision(
        self, epic_id: UUID, *, version: int, content: BriefContent, content_digest: str
    ) -> BriefRevisionRecord:
        row = await self._row(epic_id)
        if row.version != version:
            raise EpicVersionConflict("epic version is stale")
        number = (
            await self._session.execute(
                select(func.coalesce(func.max(EpicBriefRevision.revision_number), 0)).where(
                    EpicBriefRevision.epic_id == epic_id
                )
            )
        ).scalar_one() + 1
        content_json = content.model_dump(mode="json")
        row.version += 1
        row.draft = content_json
        revision = EpicBriefRevision(
            id=uuid4(),
            epic_id=epic_id,
            revision_number=number,
            epic_version=row.version,
            content=content_json,
            content_digest=content_digest,
        )
        self._session.add(revision)
        await self._session.flush()
        await self._session.refresh(revision)
        return _revision(revision)

    async def get_revision(self, epic_id: UUID, brief_revision_id: UUID) -> BriefRevisionRecord:
        row = (
            await self._session.execute(
                select(EpicBriefRevision).where(
                    EpicBriefRevision.epic_id == epic_id, EpicBriefRevision.id == brief_revision_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise BriefRevisionNotFound("brief revision was not found")
        return _revision(row)

    async def list_revisions(self, epic_id: UUID) -> Sequence[BriefRevisionRecord]:
        await self.get(epic_id)
        rows = (
            (
                await self._session.execute(
                    select(EpicBriefRevision)
                    .where(EpicBriefRevision.epic_id == epic_id)
                    .order_by(EpicBriefRevision.revision_number)
                )
            )
            .scalars()
            .all()
        )
        return [_revision(row) for row in rows]

    async def adopt_revision(
        self, epic_id: UUID, *, version: int, revision: BriefRevisionRecord
    ) -> EpicRecord:
        row = await self._row(epic_id)
        if row.version != version:
            raise EpicVersionConflict("epic version is stale")
        changed = (row.accepted_brief_revision_id, row.accepted_brief_digest) != (
            revision.brief_revision_id,
            revision.content_digest,
        )
        row.accepted_brief_revision_id = revision.brief_revision_id
        row.accepted_brief_digest = revision.content_digest
        if changed:
            row.accepted_graph_revision_id = None
            row.accepted_graph_digest = None
        row.version += 1
        await self._session.flush()
        await self._session.refresh(row)
        return _epic(row)

    async def accepted(self, epic_id: UUID) -> AcceptedBrief:
        epic = await self.get(epic_id)
        if epic.accepted_brief_revision_id is None or epic.accepted_brief_digest is None:
            raise BriefNotAccepted("brief is not accepted")
        revision = await self.get_revision(epic_id, epic.accepted_brief_revision_id)
        if revision.content_digest != epic.accepted_brief_digest:
            raise BriefNotAccepted("brief binding is unavailable")
        return AcceptedBrief(
            epic_id=epic_id,
            project_id=epic.project_id,
            epic_version=epic.version,
            brief_revision_id=revision.brief_revision_id,
            brief_digest=revision.content_digest,
            **revision.content.model_dump(mode="python"),
        )

    async def _row(self, epic_id: UUID) -> Epic:
        row = await self._session.get(Epic, epic_id)
        if row is None:
            raise EpicNotFound("epic was not found")
        return row


def _epic(row: Epic) -> EpicRecord:
    return EpicRecord(
        epic_id=row.id,
        project_id=row.project_id,
        version=row.version,
        title=row.title,
        draft=BriefContent.model_validate(row.draft),
        accepted_brief_revision_id=row.accepted_brief_revision_id,
        accepted_brief_digest=row.accepted_brief_digest,
        accepted_graph_revision_id=row.accepted_graph_revision_id,
        accepted_graph_digest=row.accepted_graph_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: EpicBriefRevision) -> BriefRevisionRecord:
    return BriefRevisionRecord(
        brief_revision_id=row.id,
        epic_id=row.epic_id,
        revision_number=row.revision_number,
        epic_version=row.epic_version,
        content_digest=row.content_digest,
        source_job_id=row.source_job_id,
        content=BriefContent.model_validate(row.content),
        created_at=row.created_at,
    )
