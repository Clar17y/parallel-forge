"""PostgreSQL-backed artifact metadata queries for the API."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.artifact_queries import ArtifactReadQuery
from forge.domain.artifact import ArtifactDescriptor, validate_artifact_digest
from forge.persistence.models import Artifact, ArtifactLineage, ArtifactLineageParent
from forge.persistence.repositories.artifacts import (
    ArtifactNotFound,
    _descriptor_from_rows,
)

MAX_LINEAGES = 64
MAX_PARENTS = 64


class PostgresArtifactReadQuery(ArtifactReadQuery):
    """Load artifact content rows and their normalized run lineage."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_digest(self, digest: str) -> Sequence[ArtifactDescriptor]:
        validate_artifact_digest(digest)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Artifact, ArtifactLineage)
                    .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
                    .where(Artifact.digest == digest)
                    .order_by(
                        ArtifactLineage.run_id, ArtifactLineage.created_at, ArtifactLineage.id
                    )
                    .limit(MAX_LINEAGES + 1)
                )
            ).all()
            if not rows:
                raise ArtifactNotFound("artifact was not found")
            if len(rows) > MAX_LINEAGES:
                raise RuntimeError("artifact has too many lineages")
            records: list[ArtifactDescriptor] = []
            for content, lineage in rows:
                parents = tuple(
                    (
                        await session.execute(
                            select(Artifact.digest)
                            .join(
                                ArtifactLineageParent,
                                (ArtifactLineageParent.parent_artifact_id == Artifact.id)
                                & (ArtifactLineageParent.artifact_id == content.id)
                                & (ArtifactLineageParent.run_id == lineage.run_id),
                            )
                            .order_by(Artifact.digest)
                            .limit(MAX_PARENTS + 1)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(parents) > MAX_PARENTS:
                    raise RuntimeError("artifact has too many parents")
                records.append(_descriptor_from_rows(content, lineage, parents))
            return tuple(records)


ArtifactQuery = PostgresArtifactReadQuery

__all__ = ["MAX_LINEAGES", "MAX_PARENTS", "ArtifactQuery", "PostgresArtifactReadQuery"]
