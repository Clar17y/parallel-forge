"""Epic brief persistence contract within a caller transaction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.epic_brief import AcceptedBrief, BriefContent, BriefRevisionRecord, EpicRecord


class EpicBriefPort(Protocol):
    async def accepted(self, epic_id: UUID) -> AcceptedBrief: ...


class EpicBriefRepository(EpicBriefPort, Protocol):
    async def create(
        self, *, epic_id: UUID, project_id: UUID, title: str, draft: BriefContent
    ) -> EpicRecord: ...
    async def get(self, epic_id: UUID, *, for_update: bool = False) -> EpicRecord: ...
    async def list(self, project_id: UUID) -> Sequence[EpicRecord]: ...
    async def update_draft(
        self, epic_id: UUID, *, version: int, title: str, draft: BriefContent
    ) -> EpicRecord: ...
    async def save_revision(
        self,
        epic_id: UUID,
        *,
        version: int,
        content: BriefContent,
        content_digest: str,
        source_job_id: UUID | None = None,
    ) -> BriefRevisionRecord: ...
    async def get_revision(self, epic_id: UUID, brief_revision_id: UUID) -> BriefRevisionRecord: ...
    async def list_revisions(self, epic_id: UUID) -> Sequence[BriefRevisionRecord]: ...
    async def adopt_revision(
        self, epic_id: UUID, *, version: int, revision: BriefRevisionRecord
    ) -> EpicRecord: ...
