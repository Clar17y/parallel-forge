"""Immutable artifact metadata and normalized run lineage persistence."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.artifact import (
    ArtifactDescriptor,
    canonical_storage_pointer,
    thaw_metadata,
    validate_artifact_digest,
)
from forge.observability.redaction import Redactor
from forge.persistence.models import Artifact, ArtifactLineage, ArtifactLineageParent
from forge.persistence.repositories.runs import PersistenceDataError


class ArtifactRepositoryError(RuntimeError):
    """Artifact metadata or lineage cannot be persisted safely."""


class ArtifactNotFound(ArtifactRepositoryError):
    """No artifact lineage exists for the requested digest and run."""


class ArtifactMetadataConflict(ArtifactRepositoryError):
    """An immutable content or same-run lineage field differs on replay."""


class ArtifactRepository:
    """Persist artifact lineage in short transactions or a caller-owned session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        session: AsyncSession | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        if (session_factory is None) == (session is None):
            raise ValueError("provide exactly one artifact persistence boundary")
        self._session_factory = session_factory
        self._session = session
        self._redactor = redactor or Redactor()

    async def record(
        self,
        descriptor: ArtifactDescriptor,
        *,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID | None = None,
        parent_digests: Sequence[str] = (),
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactDescriptor:
        if not isinstance(run_id, UUID):
            raise TypeError("artifact run id must be a UUID")
        if (
            not isinstance(producer_type, str)
            or not producer_type
            or producer_type != producer_type.strip()
            or len(producer_type) > 96
        ):
            raise ValueError("artifact producer type must contain 1-96 characters")
        if producer_id is not None and not isinstance(producer_id, UUID):
            raise TypeError("artifact producer id must be a UUID")
        parents = _normalize_parents(parent_digests, descriptor.digest)
        raw_metadata = descriptor.metadata if metadata is None else metadata
        bounded_metadata = self._redactor.redact(dict(raw_metadata))
        if not isinstance(bounded_metadata, Mapping):
            raise TypeError("artifact metadata must be an object")
        metadata_snapshot = _with_truncation_metadata(dict(bounded_metadata), descriptor)
        try:
            async with self._session_scope(transaction=True) as session:
                content = await self._content_row(descriptor, metadata_snapshot, session)
                parent_ids = await self._parent_ids(parents, run_id, session)
                lineage = await self._lineage_row(
                    content,
                    run_id,
                    producer_type,
                    producer_id,
                    parents,
                    parent_ids,
                    session,
                )
                return _descriptor_from_rows(content, lineage, parents)
        except ArtifactRepositoryError:
            raise
        except IntegrityError:
            pass
        raise ArtifactRepositoryError("artifact metadata violated a database invariant") from None

    async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor:
        validate_artifact_digest(digest)
        if not isinstance(run_id, UUID):
            raise TypeError("artifact run id must be a UUID")
        async with self._session_scope(transaction=False) as session:
            row = (
                await session.execute(
                    select(Artifact, ArtifactLineage)
                    .join(
                        ArtifactLineage,
                        (ArtifactLineage.artifact_id == Artifact.id)
                        & (ArtifactLineage.run_id == run_id),
                    )
                    .where(Artifact.digest == digest)
                )
            ).one_or_none()
            if row is None:
                raise ArtifactNotFound("artifact lineage was not found")
            content, lineage = row
            parents = await self._parent_digests(content.id, run_id, session)
            return _descriptor_from_rows(content, lineage, parents)

    async def lineages(self, digest: str) -> tuple[ArtifactDescriptor, ...]:
        """Return all run lineages in deterministic run/time order."""

        validate_artifact_digest(digest)
        async with self._session_scope(transaction=False) as session:
            rows = (
                await session.execute(
                    select(Artifact, ArtifactLineage)
                    .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
                    .where(Artifact.digest == digest)
                    .order_by(
                        ArtifactLineage.run_id, ArtifactLineage.created_at, ArtifactLineage.id
                    )
                )
            ).all()
            result: list[ArtifactDescriptor] = []
            for content, lineage in rows:
                parents = await self._parent_digests(content.id, lineage.run_id, session)
                result.append(_descriptor_from_rows(content, lineage, parents))
            return tuple(result)

    @asynccontextmanager
    async def _session_scope(self, *, transaction: bool) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
            return
        session_factory = self._session_factory
        if session_factory is None:
            raise ArtifactRepositoryError("artifact persistence boundary is not configured")
        async with session_factory() as session:
            if transaction:
                async with session.begin():
                    yield session
            else:
                yield session

    async def _content_row(
        self,
        descriptor: ArtifactDescriptor,
        metadata: Mapping[str, object],
        session: AsyncSession,
    ) -> Artifact:
        row = (
            await session.execute(
                select(Artifact).where(Artifact.digest == descriptor.digest).with_for_update()
            )
        ).scalar_one_or_none()
        expected_metadata = _canonical_json(metadata)
        if row is None:
            candidate_id = uuid4()
            inserted = await session.execute(
                insert(Artifact)
                .values(
                    id=candidate_id,
                    digest=descriptor.digest,
                    media_type=descriptor.media_type,
                    storage_pointer=canonical_storage_pointer(descriptor.digest),
                    size_bytes=descriptor.byte_count,
                    metadata_schema_version=descriptor.schema_version,
                    artifact_metadata=thaw_metadata(metadata),
                )
                .on_conflict_do_nothing(index_elements=[Artifact.digest])
                .returning(Artifact.id)
            )
            inserted_id = inserted.scalar_one_or_none()
            row = await session.get(Artifact, inserted_id or candidate_id)
            if row is None:
                row = (
                    await session.execute(
                        select(Artifact)
                        .where(Artifact.digest == descriptor.digest)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            if row is None:
                raise ArtifactRepositoryError(
                    "artifact content disappeared before it could be loaded"
                )
            if inserted_id is not None:
                return row
        if (
            row.media_type != descriptor.media_type
            or row.storage_pointer != canonical_storage_pointer(descriptor.digest)
            or row.size_bytes != descriptor.byte_count
            or row.metadata_schema_version != descriptor.schema_version
            or _canonical_json(row.artifact_metadata) != expected_metadata
        ):
            raise ArtifactMetadataConflict("artifact content metadata differs for this digest")
        return row

    async def _parent_ids(
        self, parents: tuple[str, ...], run_id: UUID, session: AsyncSession
    ) -> dict[str, UUID]:
        if not parents:
            return {}
        rows = (
            await session.execute(
                select(Artifact.digest, Artifact.id)
                .join(ArtifactLineage, ArtifactLineage.artifact_id == Artifact.id)
                .where(Artifact.digest.in_(parents), ArtifactLineage.run_id == run_id)
            )
        ).all()
        found = {digest: artifact_id for digest, artifact_id in rows}
        if set(found) != set(parents):
            raise ArtifactMetadataConflict("all parent artifacts must have lineage in the run")
        return found

    async def _lineage_row(
        self,
        content: Artifact,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID | None,
        parents: tuple[str, ...],
        parent_ids: Mapping[str, UUID],
        session: AsyncSession,
    ) -> ArtifactLineage:
        lineage = (
            await session.execute(
                select(ArtifactLineage)
                .where(
                    ArtifactLineage.artifact_id == content.id,
                    ArtifactLineage.run_id == run_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lineage is None:
            candidate_id = uuid4()
            inserted = await session.execute(
                insert(ArtifactLineage)
                .values(
                    id=candidate_id,
                    artifact_id=content.id,
                    run_id=run_id,
                    producer_kind=producer_type,
                    producer_id=producer_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[ArtifactLineage.artifact_id, ArtifactLineage.run_id]
                )
                .returning(ArtifactLineage.id)
            )
            inserted_id = inserted.scalar_one_or_none()
            lineage = await session.get(ArtifactLineage, inserted_id or candidate_id)
            if lineage is None:
                lineage = (
                    await session.execute(
                        select(ArtifactLineage)
                        .where(
                            ArtifactLineage.artifact_id == content.id,
                            ArtifactLineage.run_id == run_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            if lineage is None:
                raise ArtifactRepositoryError(
                    "artifact lineage disappeared before it could be loaded"
                )
            if inserted_id is None:
                if lineage.producer_kind != producer_type or lineage.producer_id != producer_id:
                    raise ArtifactMetadataConflict("artifact producer lineage differs for this run")
                existing = await self._parent_digests(content.id, run_id, session)
                if existing != parents:
                    raise ArtifactMetadataConflict("artifact parent lineage differs for this run")
                return lineage
            for parent in parents:
                session.add(
                    ArtifactLineageParent(
                        artifact_id=content.id,
                        run_id=run_id,
                        parent_artifact_id=parent_ids[parent],
                    )
                )
            await session.flush()
            return lineage
        if lineage.producer_kind != producer_type or lineage.producer_id != producer_id:
            raise ArtifactMetadataConflict("artifact producer lineage differs for this run")
        existing = await self._parent_digests(content.id, run_id, session)
        if existing != parents:
            raise ArtifactMetadataConflict("artifact parent lineage differs for this run")
        return lineage

    async def _parent_digests(
        self, artifact_id: UUID, run_id: UUID, session: AsyncSession
    ) -> tuple[str, ...]:
        rows = (
            (
                await session.execute(
                    select(Artifact.digest)
                    .join(
                        ArtifactLineageParent,
                        (ArtifactLineageParent.parent_artifact_id == Artifact.id)
                        & (ArtifactLineageParent.artifact_id == artifact_id)
                        & (ArtifactLineageParent.run_id == run_id),
                    )
                    .order_by(Artifact.digest)
                )
            )
            .scalars()
            .all()
        )
        return tuple(rows)


def _descriptor_from_rows(
    content: Artifact, lineage: ArtifactLineage, parents: Sequence[str]
) -> ArtifactDescriptor:
    try:
        stored_metadata = dict(content.artifact_metadata)
        truncated_value = stored_metadata.pop("__forge_artifact_truncated", False)
        if type(truncated_value) is not bool:
            raise PersistenceDataError("stored artifact truncation flag is malformed")
        truncated = truncated_value
        original_count = stored_metadata.pop("__forge_artifact_original_byte_count", None)
        if type(original_count) is not int or original_count < 0:
            raise PersistenceDataError("stored artifact original byte count is malformed")
        truncation_policy = stored_metadata.pop("__forge_artifact_truncation_policy", "none")
        if not isinstance(truncation_policy, str):
            raise PersistenceDataError("stored artifact truncation policy is malformed")
        return ArtifactDescriptor(
            digest=content.digest,
            media_type=content.media_type,
            byte_count=content.size_bytes,
            storage_path=Path(content.storage_pointer),
            producer_type=lineage.producer_kind,
            producer_id=lineage.producer_id,
            run_id=lineage.run_id,
            parent_digests=tuple(parents),
            schema_version=content.metadata_schema_version,
            created_at=content.created_at,
            metadata=stored_metadata,
            truncated=truncated,
            original_byte_count=original_count,
            truncation_policy=truncation_policy,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("stored artifact metadata is malformed") from error


def _normalize_parents(parents: Sequence[str], digest: str) -> tuple[str, ...]:
    validate_artifact_digest(digest)
    normalized = tuple(parents)
    if normalized != tuple(sorted(set(normalized))):
        raise ArtifactMetadataConflict("artifact parent digests must be unique and sorted")
    for parent in normalized:
        validate_artifact_digest(parent)
        if parent == digest:
            raise ArtifactMetadataConflict("artifact cannot parent itself")
    return normalized


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(thaw_metadata(value), sort_keys=True, separators=(",", ":"))


def _with_truncation_metadata(
    metadata: dict[str, object], descriptor: ArtifactDescriptor
) -> dict[str, object]:
    values = {
        "__forge_artifact_truncated": descriptor.truncated,
        "__forge_artifact_original_byte_count": descriptor.original_byte_count,
        "__forge_artifact_truncation_policy": descriptor.truncation_policy,
    }
    for key, value in values.items():
        if key in metadata and metadata[key] != value:
            raise ArtifactMetadataConflict("artifact truncation metadata is immutable")
        metadata[key] = value
    return metadata


__all__ = [
    "ArtifactMetadataConflict",
    "ArtifactNotFound",
    "ArtifactRepository",
    "ArtifactRepositoryError",
]
