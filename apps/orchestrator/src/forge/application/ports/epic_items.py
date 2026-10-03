"""Transactional graph snapshot persistence contract."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from forge.domain.epic_brief import EpicRecord
from forge.domain.epic_items import GraphRevisionRecord, ItemSnapshot


class EpicItemsRepository(Protocol):
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
    ) -> GraphRevisionRecord: ...
    async def adopt_revision(
        self, epic_id: UUID, *, version: int, revision: GraphRevisionRecord
    ) -> EpicRecord: ...
    async def get_revision(self, epic_id: UUID, graph_revision_id: UUID) -> GraphRevisionRecord: ...
    async def list_revisions(self, epic_id: UUID) -> Sequence[GraphRevisionRecord]: ...
